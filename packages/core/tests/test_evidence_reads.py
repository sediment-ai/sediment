# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exact evidence occurrence reads preserve scope, values, and capacity bounds."""

from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
import json
import math

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import event, func, select

from sediment_core import (
    FactTable,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)
from sediment_core.postgres_schema import inference_calls, sessions

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _contract():
    assert find_spec("sediment_core.evidence") is not None, "evidence contract missing"
    from sediment_core import evidence

    return evidence


def _call(identifier="call", *, org="acme", session="session", observed_at=T0):
    return InferenceCall(
        inference_call_id=identifier,
        org_id=org,
        session_id=session,
        gateway_provider=GatewayProvider.LITELLM,
        observed_at=observed_at,
        model="model",
        model_provider="provider",
        input_messages=[
            InferenceMessage(role="user\x00\ud800", parts=[]),
            InferenceMessage(
                role="user",
                parts=[
                    TextPart(content="goal\x00\ud800"),
                    ReasoningPart(content="plan"),
                ],
            ),
        ],
        output_messages=[
            InferenceMessage(
                role="assistant",
                finish_reason="tool_calls",
                parts=[
                    ToolCallPart(id="alias", name="Read", arguments={"huge": 2**100}),
                    ToolCallResponsePart(id="alias", result={"number": 0.25}),
                    TextPart(content="goal\x00\ud800"),
                ],
            )
        ],
        raw={"private": "raw-secret"},
        user_id="private-user",
    )


def _reference(identifier="call", side="output", message=0, part=0):
    return _contract().EvidenceReference(identifier, side, message, part)


def test_evidence_contract_rejects_invalid_occurrences_and_versions():
    evidence = _contract()
    adapter = TypeAdapter(evidence.EvidenceReference)
    reference = _reference(" call/%?# ")
    assert reference.inference_call_id == "call/%?#"
    for field, bad in (
        ("message_index", True),
        ("part_index", 1.0),
        ("part_index", -1),
    ):
        value = {
            "inference_call_id": "call",
            "side": "input",
            "message_index": 0,
            "part_index": 0,
            field: bad,
        }
        with pytest.raises(ValidationError):
            adapter.validate_python(value)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "inference_call_id": "call",
                "side": "input",
                "message_index": 0,
                "part_index": 0,
                "extra": 1,
            }
        )
    for version in (True, 1.0, "1", 2):
        with pytest.raises(ValidationError):
            TypeAdapter(evidence.EvidenceSchemaVersion).validate_python(version)
    with pytest.raises(ValueError, match="duplicate"):
        evidence.validate_evidence_references([reference, reference])
    for count in (0, 33):
        with pytest.raises(ValueError):
            evidence.validate_evidence_references(
                [_reference(part=i) for i in range(count)]
            )


def test_pure_evidence_projections_are_deterministic_and_keep_occurrences():
    evidence = _contract()
    call = _call()
    metadata = evidence.EvidenceCallMetadata("call", T0, "provider", "model")
    earlier = evidence.EvidenceCallMetadata(
        "earlier", T0 - timedelta(days=1), None, None
    )
    kwargs = dict(found=True, quarantined_inference_calls=2)
    assert evidence.project_evidence_inventory(
        "session", 7, calls=[metadata, earlier], **kwargs
    ) == evidence.project_evidence_inventory(
        "session", 7, calls=[earlier, metadata], **kwargs
    )
    manifest = evidence.project_evidence_manifest(
        "session", 7, metadata, call.input_messages, call.output_messages
    )
    assert [(m.side, m.message_index, len(m.parts)) for m in manifest.messages] == [
        ("input", 0, 0),
        ("input", 1, 2),
        ("output", 0, 3),
    ]
    refs = [p.reference for m in manifest.messages for p in m.parts]
    assert len(set(refs)) == 5
    sources = {
        (call.inference_call_id, side): evidence.EvidenceMessageSource(
            T0, tuple(messages)
        )
        for side, messages in (
            ("input", call.input_messages),
            ("output", call.output_messages),
        )
    }
    packet = evidence.project_evidence_read("session", 7, refs[::-1], sources)
    assert [item.reference for item in packet.items] == refs[::-1]
    assert packet.items[-1].part.content == "goal\x00\ud800"
    assert packet == evidence.project_evidence_read("session", 7, refs[::-1], sources)
    payload = TypeAdapter(evidence.EvidenceRead).dump_python(packet, mode="python")
    assert (
        len(json.loads(json.dumps(payload, default=str, ensure_ascii=True))["items"])
        == 5
    )


def test_inventory_distinguishes_absence_empty_quarantine_and_orders(
    postgres_store, postgres_engine
):
    evidence = _contract()
    with postgres_engine.begin() as connection:
        connection.execute(
            sessions.insert().values(
                org_id="acme",
                session_id="empty",
                first_observed_at=T0,
                last_observed_at=T0,
                user_id_conflict=False,
            )
        )
    for call in (
        _call("b"),
        _call("a"),
        _call("q", session="quarantined"),
        _call("foreign", org="other", session="foreign"),
    ):
        postgres_store.store_inference_call(call)
    postgres_store.quarantine_fact(
        "acme", FactTable.INFERENCE_CALLS, "q", reason="review"
    )
    assert postgres_store.read_evidence_inventory("acme", "missing").found is False
    assert postgres_store.read_evidence_inventory("acme", "foreign").found is False
    empty = postgres_store.read_evidence_inventory("acme", "empty")
    assert empty.found and empty.calls == ()
    quarantined = postgres_store.read_evidence_inventory("acme", "quarantined")
    assert quarantined.found and quarantined.quarantined_inference_calls == 1
    result = postgres_store.read_evidence_inventory("acme", "session")
    assert isinstance(result, evidence.EvidenceInventory)
    assert [call.inference_call_id for call in result.calls] == ["a", "b"]
    assert (
        result.visible_inference_calls == 2 and result.capture_completeness == "unknown"
    )


def test_parts_roundtrip_and_manifest_omit_content(postgres_store):
    evidence = _contract()
    call = _call("call/%?#", session="session/%?#")
    postgres_store.store_inference_call(call)
    manifest = postgres_store.read_evidence_manifest(
        "acme", call.session_id, call.inference_call_id
    )
    refs = [part.reference for message in manifest.messages for part in message.parts]
    result = postgres_store.read_evidence_parts("acme", call.session_id, refs)
    assert [item.part for item in result.items] == [
        part
        for message in call.input_messages + call.output_messages
        for part in message.parts
    ]
    assert result.quarantine_revision == 0
    assert manifest.messages[0].role == "user\x00\ud800"
    serialized = TypeAdapter(evidence.EvidenceManifest).dump_python(
        manifest, mode="python"
    )
    assert "content" not in json.dumps(serialized, default=str)
    assert "arguments" not in json.dumps(serialized, default=str)


@pytest.mark.parametrize(
    "kind", ["missing", "foreign", "different_session", "quarantined"]
)
def test_unavailable_references_share_one_failure(postgres_store, kind):
    evidence = _contract()
    if kind != "missing":
        call = _call(
            org="other" if kind == "foreign" else "acme",
            session="other" if kind == "different_session" else "session",
        )
        postgres_store.store_inference_call(call)
        if kind == "quarantined":
            postgres_store.quarantine_fact(
                "acme", FactTable.INFERENCE_CALLS, "call", reason="review"
            )
    with pytest.raises(evidence.EvidenceReadError) as manifest_error:
        postgres_store.read_evidence_manifest("acme", "session", "call")
    assert manifest_error.value.detail == {"reason": "evidence_unavailable"}
    with pytest.raises(evidence.EvidenceReadError) as part_error:
        postgres_store.read_evidence_parts("acme", "session", [_reference()])
    assert part_error.value.detail == {
        "reason": "evidence_unavailable",
        "reference_index": 0,
    }


def test_reference_errors_follow_request_order(postgres_store):
    evidence = _contract()
    postgres_store.store_inference_call(_call())
    with pytest.raises(evidence.EvidenceReadError) as error:
        postgres_store.read_evidence_parts(
            "acme", "session", [_reference(part=10), _reference("missing")]
        )
    assert error.value.detail == {
        "reason": "evidence_part_absent",
        "reference_index": 0,
    }
    with pytest.raises(evidence.EvidenceReadError) as error:
        postgres_store.read_evidence_parts(
            "acme", "session", [_reference(), _reference(part=10)]
        )
    assert error.value.detail == {
        "reason": "evidence_part_absent",
        "reference_index": 1,
    }


def test_later_read_observes_backdated_fact_and_quarantine(postgres_store):
    evidence = _contract()
    postgres_store.store_inference_call(_call())
    with postgres_store.read_snapshot() as snapshot:
        original = snapshot.read_evidence_inventory("acme", "session")
        postgres_store.store_inference_call(
            _call("late", observed_at=T0 - timedelta(days=1))
        )
        postgres_store.quarantine_fact(
            "acme", FactTable.INFERENCE_CALLS, "call", reason="review"
        )
        assert snapshot.read_evidence_inventory("acme", "session") == original
        assert snapshot.read_evidence_parts("acme", "session", [_reference()]).items
    later = postgres_store.read_evidence_inventory("acme", "session")
    assert [call.inference_call_id for call in later.calls] == ["late"]
    assert later.quarantine_revision > original.quarantine_revision
    with pytest.raises(evidence.EvidenceReadError):
        postgres_store.read_evidence_parts("acme", "session", [_reference()])


def _statements(engine):
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return statements, record


@pytest.mark.parametrize("operation", ["inventory", "manifest", "parts"])
def test_source_preflight_refuses_before_transfer(
    postgres_store, postgres_engine, monkeypatch, operation
):
    evidence = _contract()
    postgres_store.store_inference_call(_call())
    monkeypatch.setattr(evidence, "EVIDENCE_SOURCE_BYTES_LIMIT", 1)
    statements, record = _statements(postgres_engine)
    try:
        with pytest.raises(evidence.EvidenceReadError) as error:
            if operation == "inventory":
                postgres_store.read_evidence_inventory("acme", "session")
            elif operation == "manifest":
                postgres_store.read_evidence_manifest("acme", "session", "call")
            else:
                postgres_store.read_evidence_parts("acme", "session", [_reference()])
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert error.value.detail["reason"] == "evidence_source_limit"
    assert any("octet_length" in sql for sql in statements)
    assert not any(sql.startswith("SELECT inference_calls.") for sql in statements)


def test_fetch_selects_and_counts_each_source_column_once(
    postgres_store, postgres_engine, monkeypatch
):
    evidence = _contract()
    call = _call()
    call.input_messages = [
        InferenceMessage(role="user", parts=[TextPart(content="x" * 10000)])
    ]
    postgres_store.store_inference_call(call)
    with postgres_engine.connect() as connection:
        exact = connection.execute(
            select(
                func.octet_length(inference_calls.c.inference_call_id)
                + func.octet_length(inference_calls.c.output_messages)
            )
        ).scalar_one()
    monkeypatch.setattr(evidence, "EVIDENCE_SOURCE_BYTES_LIMIT", exact)
    statements, record = _statements(postgres_engine)
    try:
        result = postgres_store.read_evidence_parts(
            "acme", "session", [_reference(part=0), _reference(part=1)]
        )
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert len(result.items) == 2
    content_sql = [
        sql for sql in statements if sql.startswith("SELECT inference_calls.")
    ]
    assert content_sql
    assert all(
        "input_messages" not in sql
        and ".raw" not in sql
        and "user_id" not in sql
        and ".model" not in sql
        for sql in content_sql
    )
    assert all("output_messages" in sql for sql in content_sql)
    monkeypatch.setattr(evidence, "EVIDENCE_SOURCE_BYTES_LIMIT", exact - 1)
    with pytest.raises(evidence.EvidenceReadError) as error:
        postgres_store.read_evidence_parts("acme", "session", [_reference()])
    assert error.value.detail == {
        "reason": "evidence_source_limit",
        "bytes": exact,
        "limit": exact - 1,
    }


@pytest.mark.parametrize("field", ["model", "model_provider", "inference_call_id"])
def test_inventory_metadata_bytes_are_bounded(
    postgres_store, postgres_engine, monkeypatch, field
):
    evidence = _contract()
    call = _call()
    call = call.model_copy(update={field: "x" * 200})
    postgres_store.store_inference_call(call)
    monkeypatch.setattr(evidence, "EVIDENCE_SOURCE_BYTES_LIMIT", 100)
    with pytest.raises(evidence.EvidenceReadError, match="evidence_source_limit"):
        postgres_store.read_evidence_inventory("acme", "session")


def test_inventory_cardinality_is_exact_and_never_truncated(
    postgres_store, monkeypatch
):
    evidence = _contract()
    monkeypatch.setattr(evidence, "EVIDENCE_INVENTORY_LIMIT", 2)
    for identifier in ("a", "b"):
        postgres_store.store_inference_call(_call(identifier))
    assert len(postgres_store.read_evidence_inventory("acme", "session").calls) == 2
    postgres_store.store_inference_call(_call("c"))
    with pytest.raises(evidence.EvidenceReadError) as error:
        postgres_store.read_evidence_inventory("acme", "session")
    assert error.value.detail == {
        "reason": "evidence_inventory_limit",
        "count": 3,
        "limit": 2,
    }


def test_unselected_nonfinite_does_not_change_finite_projection(postgres_store):
    call = _call()
    call.output_messages[0].parts.append(
        ToolCallResponsePart(id="bad", result=float("inf"))
    )
    postgres_store.store_inference_call(call)
    assert (
        len(
            postgres_store.read_evidence_manifest("acme", "session", "call")
            .messages[-1]
            .parts
        )
        == 4
    )
    selected = postgres_store.read_evidence_parts(
        "acme", "session", [_reference(part=1)]
    )
    assert selected.items[0].part.result == {"number": 0.25}
    nonfinite = postgres_store.read_evidence_parts(
        "acme", "session", [_reference(part=3)]
    )
    assert math.isinf(nonfinite.items[0].part.result)


def test_response_contract_requires_every_declared_envelope_field():
    evidence = _contract()
    payload = {
        "schema_version": 1,
        "session_id": "session",
        "quarantine_revision": 0,
        "found": False,
        "visible_inference_calls": 0,
        "quarantined_inference_calls": 0,
        "capture_completeness": "unknown",
        "calls": [],
    }
    adapter = TypeAdapter(evidence.EvidenceInventory)
    assert adapter.validate_python(payload).found is False
    for field in payload:
        with pytest.raises(ValidationError):
            adapter.validate_python(
                {key: value for key, value in payload.items() if key != field}
            )


@pytest.mark.parametrize("operation", ["inventory", "manifest"])
def test_metadata_source_limit_exact_boundary(
    postgres_store, postgres_engine, monkeypatch, operation
):
    evidence = _contract()
    postgres_store.store_inference_call(_call())
    columns = ["inference_call_id", "model_provider", "model"]
    if operation == "manifest":
        columns.extend(["input_messages", "output_messages"])
    with postgres_engine.connect() as connection:
        exact = connection.execute(
            select(
                sum(
                    func.coalesce(func.octet_length(inference_calls.c[name]), 0)
                    for name in columns
                )
            )
        ).scalar_one()
    method = (
        postgres_store.read_evidence_inventory
        if operation == "inventory"
        else postgres_store.read_evidence_manifest
    )
    args = (
        ("acme", "session") if operation == "inventory" else ("acme", "session", "call")
    )
    monkeypatch.setattr(evidence, "EVIDENCE_SOURCE_BYTES_LIMIT", exact)
    method(*args)
    monkeypatch.setattr(evidence, "EVIDENCE_SOURCE_BYTES_LIMIT", exact - 1)
    with pytest.raises(evidence.EvidenceReadError) as error:
        method(*args)
    assert error.value.detail == {
        "reason": "evidence_source_limit",
        "bytes": exact,
        "limit": exact - 1,
    }


def test_all_selected_source_groups_preflight_before_any_transfer(
    postgres_store, postgres_engine, monkeypatch
):
    evidence = _contract()
    for identifier in ("input", "both", "output"):
        postgres_store.store_inference_call(_call(identifier))
    references = [
        _reference("input", "input", 1, 0),
        _reference("both", "input", 1, 0),
        _reference("both"),
        _reference("output"),
    ]
    statements, record = _statements(postgres_engine)
    try:
        packet = postgres_store.read_evidence_parts("acme", "session", references)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert len(packet.items) == 4
    preflights = [
        index for index, sql in enumerate(statements) if "octet_length" in sql
    ]
    transfers = [
        index
        for index, sql in enumerate(statements)
        if sql.startswith("SELECT inference_calls.")
    ]
    assert len(preflights) == 3 and len(transfers) == 3
    assert max(preflights) < min(transfers)
    for index in transfers:
        projection = statements[index].split("FROM", 1)[0]
        assert ".raw" not in projection and "user_id" not in projection
        assert "model_provider" not in projection and ".model" not in projection
    # One aggregate budget covers all three disjoint selections.
    with postgres_engine.connect() as connection:
        sizes = (
            connection.execute(
                select(
                    inference_calls.c.inference_call_id,
                    func.octet_length(inference_calls.c.inference_call_id).label(
                        "id_bytes"
                    ),
                    func.octet_length(inference_calls.c.input_messages).label(
                        "input_bytes"
                    ),
                    func.octet_length(inference_calls.c.output_messages).label(
                        "output_bytes"
                    ),
                )
            )
            .mappings()
            .all()
        )
    exact = sum(
        row["id_bytes"]
        + (row["input_bytes"] if row["inference_call_id"] in ("input", "both") else 0)
        + (row["output_bytes"] if row["inference_call_id"] in ("both", "output") else 0)
        for row in sizes
    )
    monkeypatch.setattr(evidence, "EVIDENCE_SOURCE_BYTES_LIMIT", exact)
    assert (
        len(postgres_store.read_evidence_parts("acme", "session", references).items)
        == 4
    )
    monkeypatch.setattr(evidence, "EVIDENCE_SOURCE_BYTES_LIMIT", exact - 1)
    with pytest.raises(evidence.EvidenceReadError):
        postgres_store.read_evidence_parts("acme", "session", references)


def test_inventory_never_selects_messages_raw_or_user_identifiers(
    postgres_store, postgres_engine
):
    postgres_store.store_inference_call(_call())
    statements, record = _statements(postgres_engine)
    try:
        postgres_store.read_evidence_inventory("acme", "session")
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert not any(
        "input_messages" in sql
        or "output_messages" in sql
        or ".raw" in sql
        or "user_id" in sql
        for sql in statements
    )


def test_empty_message_sides_and_null_metadata_remain_explicit(postgres_store):
    call = _call().model_copy(
        update={
            "input_messages": [],
            "output_messages": [InferenceMessage(role="assistant", parts=[])],
            "model": None,
            "model_provider": None,
        }
    )
    postgres_store.store_inference_call(call)
    manifest = postgres_store.read_evidence_manifest("acme", "session", "call")
    assert manifest.call.model is None and manifest.call.model_provider is None
    assert len(manifest.messages) == 1
    assert manifest.messages[0].side == "output"
    assert manifest.messages[0].parts == ()
    assert manifest.messages[0].finish_reason is None


def test_invalid_selection_refuses_before_sql_and_maximum_selection_succeeds(
    postgres_store, postgres_engine
):
    evidence = _contract()
    call = _call()
    call.output_messages[0].parts = [
        TextPart(content=str(index)) for index in range(32)
    ]
    postgres_store.store_inference_call(call)
    references = [_reference(part=index) for index in range(32)]
    result = postgres_store.read_evidence_parts("acme", "session", references)
    assert [item.part.content for item in result.items] == [
        str(index) for index in range(32)
    ]
    statements, record = _statements(postgres_engine)
    try:
        for invalid in (
            [],
            references + [_reference(part=32)],
            [references[0], references[0]],
        ):
            with pytest.raises(ValueError):
                postgres_store.read_evidence_parts("acme", "session", invalid)
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert statements == []
    assert evidence.EVIDENCE_REFERENCE_LIMIT == 32


def test_corrupt_selected_source_releases_snapshot(postgres_store, postgres_engine):
    postgres_store.store_inference_call(_call())
    with postgres_engine.begin() as connection:
        connection.execute(inference_calls.update().values(output_messages="not-json"))
    checked_out = postgres_engine.pool.checkedout()
    with pytest.raises(json.JSONDecodeError):
        postgres_store.read_evidence_parts("acme", "session", [_reference()])
    assert postgres_engine.pool.checkedout() == checked_out
    # The unselected corrupt side does not participate in an input-only read.
    packet = postgres_store.read_evidence_parts(
        "acme", "session", [_reference(side="input", message=1)]
    )
    assert packet.items[0].part.content == "goal\x00\ud800"


@pytest.mark.parametrize("operation", ["inventory", "manifest", "parts"])
def test_invalid_scope_rejects_before_sql(postgres_store, postgres_engine, operation):
    statements, record = _statements(postgres_engine)
    try:
        with pytest.raises(ValueError):
            if operation == "inventory":
                postgres_store.read_evidence_inventory("acme", "bad\x00id")
            elif operation == "manifest":
                postgres_store.read_evidence_manifest("acme", "session", "bad\ud800id")
            else:
                postgres_store.read_evidence_parts("bad org", "session", [_reference()])
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert statements == []

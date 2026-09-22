# SPDX-License-Identifier: AGPL-3.0-or-later
"""One bounded PostgreSQL snapshot supplies complete visible Session evidence."""

from datetime import UTC, datetime, timedelta
from dataclasses import asdict
import json

import pytest
from pydantic import TypeAdapter
from sqlalchemy import event, func, select

from sediment_core import (
    EvidenceContextSource,
    EvidenceReadItem,
    EvidenceReference,
    FactTable,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)
from sediment_core import evidence
from sediment_core.postgres_schema import inference_calls, sessions

T0 = datetime(2026, 9, 21, tzinfo=UTC)


def call(identifier="call", *, org="acme", session="session", when=T0):
    return InferenceCall(
        inference_call_id=identifier,
        org_id=org,
        session_id=session,
        gateway_provider=GatewayProvider.LITELLM,
        observed_at=when,
        model="model",
        model_provider="provider",
        user_id="private-user",
        input_messages=[
            InferenceMessage(role="empty", parts=[]),
            InferenceMessage(
                role="user\x00\ud800",
                parts=[
                    TextPart(content="parser\x00\ud800"),
                    ReasoningPart(content="private plan"),
                ],
            ),
        ],
        output_messages=[
            InferenceMessage(
                role="assistant",
                finish_reason="tool_calls",
                parts=[
                    ToolCallPart(
                        id="alias", name="RunParser", arguments={"large": 2**100}
                    ),
                    ToolCallResponsePart(id="alias", result={"value": 0.25}),
                ],
            )
        ],
        raw={"private": "raw-secret"},
    )


def record_sql(engine):
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return statements, record


def test_context_source_preserves_complete_canonical_content_and_scope(
    postgres_store, postgres_engine
):
    original = call()
    for value in (
        original,
        call("foreign", org="other"),
        call("other-session", session="other"),
        call("hidden"),
    ):
        postgres_store.store_inference_call(value)
    postgres_store.quarantine_fact(
        "acme", FactTable.INFERENCE_CALLS, "hidden", reason="review"
    )
    before_counts = postgres_store.count_facts("acme", FactTable.INFERENCE_CALLS)
    statements, record = record_sql(postgres_engine)
    try:
        result = postgres_store.read_context_source("acme", "session")
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert isinstance(result, EvidenceContextSource)
    assert result.visible_inference_calls == 1
    assert result.quarantined_inference_calls == 1
    assert result.quarantine_revision > 0
    assert len(result.items) == 4
    assert [i.part for i in result.items] == [
        p for m in original.input_messages + original.output_messages for p in m.parts
    ]
    assert result.items[0].reference == EvidenceReference("call", "input", 1, 0)
    assert result.items[-1].reference == EvidenceReference("call", "output", 0, 1)
    assert result.items[0].role == "user\x00\ud800"
    assert result.items[2].part.arguments["large"] == 2**100
    selections = [
        sql for sql in statements if sql.startswith("SELECT inference_calls.")
    ]
    assert selections and all(
        ".raw" not in sql and "user_id" not in sql for sql in selections
    )
    assert all(sql.startswith("SELECT") or sql.startswith("SET") for sql in statements)
    assert (
        postgres_store.count_facts("acme", FactTable.INFERENCE_CALLS) == before_counts
    )


def test_source_distinguishes_empty_quarantined_and_absent_sessions(
    postgres_store, postgres_engine
):
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
    for identifier in ("missing", "foreign"):
        if identifier == "foreign":
            postgres_store.store_inference_call(
                call("foreign-call", org="other", session=identifier)
            )
        with pytest.raises(evidence.EvidenceReadError) as error:
            postgres_store.read_context_source("acme", identifier)
        assert error.value.detail == {"reason": "evidence_unavailable"}
    empty = postgres_store.read_context_source("acme", "empty")
    assert empty.items == () and empty.visible_inference_calls == 0
    postgres_store.store_inference_call(call())
    postgres_store.quarantine_fact(
        "acme", FactTable.INFERENCE_CALLS, "call", reason="review"
    )
    hidden = postgres_store.read_context_source("acme", "session")
    assert hidden.items == () and hidden.visible_inference_calls == 0
    assert hidden.quarantined_inference_calls == 1


def test_snapshot_holds_inventory_preflight_and_content_across_concurrent_change(
    postgres_store, postgres_engine
):
    postgres_store.store_inference_call(call())
    changed = False

    def change_after_inventory(
        connection, cursor, statement, parameters, context, executemany
    ):
        nonlocal changed
        if not changed and statement.startswith(
            "SELECT inference_calls.inference_call_id"
        ):
            changed = True
            postgres_store.store_inference_call(
                call("backdated", when=T0 - timedelta(days=1))
            )
            postgres_store.quarantine_fact(
                "acme", FactTable.INFERENCE_CALLS, "call", reason="review"
            )

    event.listen(postgres_engine, "after_cursor_execute", change_after_inventory)
    try:
        initial = postgres_store.read_context_source("acme", "session")
    finally:
        event.remove(postgres_engine, "after_cursor_execute", change_after_inventory)
    assert changed and initial.quarantine_revision == 0
    assert {i.reference.inference_call_id for i in initial.items} == {"call"}
    later = postgres_store.read_context_source("acme", "session")
    assert {i.reference.inference_call_id for i in later.items} == {"backdated"}
    assert later.quarantine_revision > initial.quarantine_revision
    assert later.quarantined_inference_calls == 1


def test_aggregate_source_preflight_counts_each_column_once_before_transfer(
    postgres_store, postgres_engine, monkeypatch
):
    for identifier in ("a", "b"):
        postgres_store.store_inference_call(call(identifier))
    columns = (
        "inference_call_id",
        "model_provider",
        "model",
        "input_messages",
        "output_messages",
    )
    with postgres_engine.connect() as connection:
        exact = int(
            connection.execute(
                select(
                    func.sum(
                        sum(
                            func.coalesce(func.octet_length(inference_calls.c[name]), 0)
                            for name in columns
                        )
                    )
                )
            ).scalar_one()
        )
    monkeypatch.setattr(evidence, "EVIDENCE_SOURCE_BYTES_LIMIT", exact)
    assert len(postgres_store.read_context_source("acme", "session").items) == 8
    monkeypatch.setattr(evidence, "EVIDENCE_SOURCE_BYTES_LIMIT", exact - 1)
    statements, record = record_sql(postgres_engine)
    try:
        with pytest.raises(evidence.EvidenceReadError) as error:
            postgres_store.read_context_source("acme", "session")
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert error.value.detail == {
        "reason": "evidence_source_limit",
        "bytes": exact,
        "limit": exact - 1,
    }
    assert any("octet_length" in sql and "input_messages" in sql for sql in statements)
    assert not any(
        sql.startswith("SELECT inference_calls.")
        and ("input_messages" in sql or "output_messages" in sql)
        for sql in statements
    )


def test_inventory_cap_and_metadata_preflight_precede_content(
    postgres_store, postgres_engine, monkeypatch
):
    for identifier in ("a", "b"):
        postgres_store.store_inference_call(call(identifier))
    monkeypatch.setattr(evidence, "EVIDENCE_INVENTORY_LIMIT", 1)
    with pytest.raises(evidence.EvidenceReadError) as error:
        postgres_store.read_context_source("acme", "session")
    assert error.value.detail == {
        "reason": "evidence_inventory_limit",
        "count": 2,
        "limit": 1,
    }
    monkeypatch.setattr(evidence, "EVIDENCE_INVENTORY_LIMIT", 1000)
    monkeypatch.setattr(evidence, "EVIDENCE_SOURCE_BYTES_LIMIT", 1)
    statements, record = record_sql(postgres_engine)
    try:
        with pytest.raises(evidence.EvidenceReadError, match="evidence_source_limit"):
            postgres_store.read_context_source("acme", "session")
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert not any(sql.startswith("SELECT inference_calls.") for sql in statements)


def test_part_cap_refuses_complete_population_with_exact_count(postgres_store):
    original = call()
    original.input_messages = [
        InferenceMessage(
            role="user", parts=[TextPart(content="part") for _ in range(2044)]
        )
    ]
    # Four output parts make the exact 2,048 boundary.
    original.output_messages[0].parts *= 2
    postgres_store.store_inference_call(original)
    assert len(postgres_store.read_context_source("acme", "session").items) == 2048
    postgres_store.store_inference_call(call("overflow"))
    with pytest.raises(evidence.EvidenceReadError) as error:
        postgres_store.read_context_source("acme", "session")
    assert error.value.detail == {
        "reason": "retrieval_part_limit",
        "count": 2052,
        "limit": 2048,
    }


def test_encoder_matches_previous_json_representation_and_exact_bounds():
    value = EvidenceReadItem(
        EvidenceReference("a", "input", 0, 0),
        T0,
        "u\x00\ud800😀",
        None,
        ToolCallResponsePart(id="a", result={"n": 2**100, "f": -0.0}),
    )
    adapter = TypeAdapter(EvidenceReadItem)
    payload = adapter.dump_python(adapter.validate_python(asdict(value)), mode="python")
    expected = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        default=lambda item: TypeAdapter(datetime).dump_python(item, mode="json"),
    ).encode("ascii")
    assert evidence.encode_evidence_json(value, EvidenceReadItem) == expected
    assert (
        evidence.encode_evidence_json(value, EvidenceReadItem, max_bytes=len(expected))
        == expected
    )
    with pytest.raises(evidence.EvidenceReadError) as error:
        evidence.encode_evidence_json(
            value, EvidenceReadItem, max_bytes=len(expected) - 1
        )
    assert error.value.detail == {
        "reason": "evidence_response_limit",
        "limit": len(expected) - 1,
    }
    exceptional = {
        "value": [1, 0.25, "é" * 1025 + "\ud800", None, True],
        3: "number key",
        None: "null key",
    }
    expected = json.dumps(
        exceptional, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")
    assert evidence.encode_evidence_json(exceptional, dict) == expected


def test_bounded_encoder_does_not_escape_whole_large_scalar(monkeypatch):
    original = json.dumps
    escaped_lengths = []

    def observe(value, *args, **kwargs):
        if isinstance(value, str):
            escaped_lengths.append(len(value))
        return original(value, *args, **kwargs)

    monkeypatch.setattr(evidence.json, "dumps", observe)
    with pytest.raises(evidence.EvidenceReadError, match="evidence_response_limit"):
        evidence.encode_evidence_json(
            {"value": "\ud800" * 1_000_000}, dict, max_bytes=4096
        )
    assert max(escaped_lengths) <= 1024


@pytest.mark.parametrize(
    "value", [float("nan"), {"nested": [float("inf")]}, {float("inf"): 1}]
)
def test_shared_encoder_refuses_nonfinite_with_content_free_error(value):
    with pytest.raises(evidence.EvidenceReadError) as error:
        evidence.encode_evidence_json(value, object)
    assert error.value.detail == {"reason": "non_finite_number"}

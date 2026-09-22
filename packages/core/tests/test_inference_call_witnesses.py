# SPDX-License-Identifier: AGPL-3.0-or-later
"""Indexed attachment witnesses retain organization-wide ambiguity."""

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import random
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select, text

from sediment_core import (
    FactTable,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    OperationalReportLimitExceeded,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)
from sediment_core.postgres_schema import inference_call_aliases, inference_calls
from sediment_core.store import COMPOSITE_FILTER_KEY_LIMIT


BOUNDARY = datetime(2026, 9, 22, tzinfo=UTC)


def call(identity, aliases=(), **changes):
    values = dict(
        inference_call_id=identity,
        org_id="acme",
        session_id=f"session-{identity}",
        gateway_provider=GatewayProvider.LITELLM,
        model_call_id=f"provider-{identity}",
        input_messages=[],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[ToolCallPart(id=alias, name="Edit") for alias in aliases],
            )
        ],
        observed_at=BOUNDARY,
        raw={},
    )
    values.update(changes)
    return InferenceCall(**values)


def test_witnesses_keep_two_owners_per_alias_without_loading_content(
    postgres_store,
):
    for identity in ("c", "b", "a"):
        postgres_store.store_inference_call(call(identity, ["shared", "shared"]))
    postgres_store.store_inference_call(call("unique", ["wanted", "unrequested"]))
    witnesses = postgres_store.read_inference_call_identity_witnesses(
        "acme", call_ids={"shared", "wanted", "missing"}, observed_through=BOUNDARY
    )
    assert {item.inference_call_id: item.call_ids for item in witnesses} == {
        "a": ("shared",),
        "b": ("shared",),
        "unique": ("wanted",),
    }


def test_summary_filter_selects_exact_fact_ids_and_keeps_half_open_window(
    postgres_store,
):
    for identity in ("selected", "unrelated"):
        postgres_store.store_inference_call(call(identity))
    selected = postgres_store.read_inference_call_summaries(
        "acme",
        inference_call_ids={"selected"},
        observed_between=(BOUNDARY, BOUNDARY + timedelta(seconds=1)),
    )
    assert [item.inference_call_id for item in selected] == ["selected"]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_witnesses_equal_complete_alias_join_through_visibility_boundary(
    postgres_store, postgres_engine, seed
):
    facts = [
        call("old", ["shared"], observed_at=BOUNDARY - timedelta(days=365)),
        call("boundary", ["shared", "only-here"]),
        call("third", ["shared"]),
        call("future", ["only-here"], observed_at=BOUNDARY + timedelta(microseconds=1)),
        call("foreign", ["only-here"], org_id="other"),
        call("quarantined", ["only-here"]),
        call("provider", [], model_call_id="provider-tool"),
        call("tool", ["provider-tool"], gateway_provider=GatewayProvider.PORTKEY),
        call("empty", [], model_call_id=None),
    ]
    random.Random(seed).shuffle(facts)
    for fact in facts:
        postgres_store.store_inference_call(fact)
    postgres_store.quarantine_fact(
        "acme", FactTable.INFERENCE_CALLS, "quarantined", reason="test"
    )
    requested = {"shared", "only-here", "provider-tool", "absent"}
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with postgres_store.read_snapshot() as snapshot:
        complete = snapshot.read_inference_call_identities(
            "acme", observed_through=BOUNDARY, limit=20
        )
        event.listen(postgres_engine, "before_cursor_execute", capture)
        try:
            witnesses = snapshot.read_inference_call_identity_witnesses(
                "acme", call_ids=requested, observed_through=BOUNDARY
            )
            assert (
                snapshot.read_inference_call_identity_witnesses(
                    "acme", call_ids=requested, observed_through=BOUNDARY
                )
                == witnesses
            )
        finally:
            event.remove(postgres_engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert all(
        name not in statements[0]
        for name in ("input_messages", "output_messages", ".raw", "jsonb")
    )
    for identifier in requested:
        owners = sorted(
            item.inference_call_id for item in complete if identifier in item.call_ids
        )
        assert (
            sorted(
                item.inference_call_id
                for item in witnesses
                if identifier in item.call_ids
            )
            == owners[:2]
        )
    assert all(set(item.call_ids) <= requested for item in witnesses)
    postgres_store.release_fact(
        "acme", FactTable.INFERENCE_CALLS, "quarantined", reason="test"
    )
    assert (
        len(
            postgres_store.read_inference_call_identity_witnesses(
                "acme", call_ids={"only-here"}, observed_through=BOUNDARY
            )
        )
        == 2
    )


def test_witness_snapshot_preserves_visibility_across_concurrent_changes(
    postgres_store,
):
    postgres_store.store_inference_call(call("initial", ["shared"]))
    with postgres_store.read_snapshot() as snapshot:
        # Establish the snapshot before changing either source table.
        snapshot.read_inference_call_summaries("acme")
        postgres_store.store_inference_call(call("later", ["shared"]))
        postgres_store.quarantine_fact(
            "acme", FactTable.INFERENCE_CALLS, "initial", reason="test"
        )
        witnesses = snapshot.read_inference_call_identity_witnesses(
            "acme", call_ids={"shared"}, observed_through=BOUNDARY
        )
        assert [item.inference_call_id for item in witnesses] == ["initial"]
    assert [
        item.inference_call_id
        for item in postgres_store.read_inference_call_identity_witnesses(
            "acme", call_ids={"shared"}, observed_through=BOUNDARY
        )
    ] == ["later"]


def test_witness_cache_distinguishes_repeated_hour_instants(postgres_store):
    zone = ZoneInfo("America/New_York")
    before = datetime(2025, 11, 2, 1, 30, tzinfo=zone, fold=0)
    after = before.replace(fold=1)
    postgres_store.store_inference_call(
        call("between", ["tool"], observed_at=before + timedelta(minutes=15))
    )
    with postgres_store.read_snapshot() as snapshot:
        assert not snapshot.read_inference_call_identity_witnesses(
            "acme", call_ids={"tool"}, observed_through=before
        )
        assert [
            item.inference_call_id
            for item in snapshot.read_inference_call_identity_witnesses(
                "acme", call_ids={"tool"}, observed_through=after
            )
        ] == ["between"]


def test_witnesses_ignore_input_ids_responses_and_unrequested_large_output(
    postgres_store,
):
    fact = call("selected", ["output"])
    fact.input_messages = [
        InferenceMessage(role="user", parts=[ToolCallPart(id="input", name="Edit")])
    ]
    fact.output_messages[0].parts.append(ToolCallResponsePart(id="response", result={}))
    postgres_store.store_inference_call(fact)
    # No content preflight should reject this unrelated Fact.
    unrelated = call("large", [])
    unrelated.output_messages = [
        InferenceMessage(
            role="assistant", parts=[TextPart(content="x" * (65 * 1024 * 1024))]
        )
    ]
    postgres_store.store_inference_call(unrelated)
    witnesses = postgres_store.read_inference_call_identity_witnesses(
        "acme", call_ids={"output", "input", "response"}, observed_through=BOUNDARY
    )
    assert [(item.inference_call_id, item.call_ids) for item in witnesses] == [
        ("selected", ("output",))
    ]


def test_witnesses_accept_long_identifiers_and_recheck_native_hash_collisions(
    postgres_store, postgres_engine
):
    long_id = "".join(sha256(str(i).encode()).hexdigest() for i in range(200))
    postgres_store.store_inference_call(call("long", [long_id]))
    for fact_id, org, identifier in (
        ("a", "org-0", "call-1220"),
        ("b", "org-4", "call-69024"),
    ):
        postgres_store.store_inference_call(call(fact_id, [identifier], org_id=org))
    with postgres_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT hash_array(ARRAY['org-0','call-1220']) = hash_array(ARRAY['org-4','call-69024'])"
            )
        ).scalar_one()
    assert [
        item.inference_call_id
        for item in postgres_store.read_inference_call_identity_witnesses(
            "acme", call_ids={long_id}, observed_through=BOUNDARY
        )
    ] == ["long"]
    assert [
        item.inference_call_id
        for item in postgres_store.read_inference_call_identity_witnesses(
            "org-0", call_ids={"call-1220"}, observed_through=BOUNDARY
        )
    ] == ["a"]


def test_redelivery_keeps_retained_aliases_and_concurrent_writers_are_atomic(
    postgres_store, postgres_engine
):
    original = call("retained", ["original"])
    assert postgres_store.store_inference_call(original)
    assert not postgres_store.store_inference_call(call("retained", ["changed"]))
    assert not postgres_store.store_inference_call(
        call("alternate-id", ["changed"], model_call_id=original.model_call_id)
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts = list(
            executor.map(
                postgres_store.store_inference_call, [call("concurrent", ["same"])] * 4
            )
        )
    assert receipts.count(True) == 1
    with postgres_engine.connect() as connection:
        aliases = connection.execute(
            select(
                inference_call_aliases.c.inference_call_id,
                inference_call_aliases.c.call_id,
            )
        ).all()
        assert sorted(aliases) == [
            ("concurrent", "provider-concurrent"),
            ("concurrent", "same"),
            ("retained", "original"),
            ("retained", "provider-retained"),
        ]
        assert connection.execute(
            select(inference_calls.c.call_alias_count)
        ).scalars().all() == [2, 2]
    assert not postgres_store.read_inference_call_identity_witnesses(
        "acme", call_ids={"changed"}, observed_through=BOUNDARY
    )


def test_alias_write_failure_rolls_back_parent_and_session(
    postgres_store, postgres_engine
):
    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO inference_call_aliases"):
            raise RuntimeError("injected alias write failure")

    event.listen(postgres_engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            postgres_store.store_inference_call(call("rolled-back", ["tool"]))
    finally:
        event.remove(postgres_engine, "before_cursor_execute", fail)
    assert not postgres_store.read_inference_calls("acme")
    assert not postgres_store.read_sessions("acme")
    with postgres_engine.connect() as connection:
        assert not connection.execute(select(inference_call_aliases)).all()
    assert postgres_store.store_inference_call(call("rolled-back", ["tool"]))


def test_parent_deletion_cascades_physical_aliases(postgres_store, postgres_engine):
    postgres_store.store_inference_call(call("deleted", ["tool"]))
    with postgres_engine.begin() as connection:
        connection.execute(inference_calls.delete())
        assert not connection.execute(select(inference_call_aliases)).all()


@pytest.mark.parametrize("ids", [{" "}, {"bad\x00"}, {"bad\ud800"}])
def test_identity_filters_validate_before_sql(postgres_store, postgres_engine, ids):
    for read in (
        lambda: postgres_store.read_inference_call_identity_witnesses(
            "acme", call_ids=ids, observed_through=BOUNDARY
        ),
        lambda: postgres_store.read_inference_call_summaries(
            "acme", inference_call_ids=ids
        ),
    ):
        with pytest.raises(ValidationError):
            read()


def test_witness_requests_and_summary_filter_are_bounded_and_cache_distinct(
    postgres_store,
):
    for identity in ("a", "b"):
        postgres_store.store_inference_call(call(identity))
    with postgres_store.read_snapshot() as snapshot:
        assert (
            snapshot.read_inference_call_identity_witnesses(
                "acme", call_ids=set(), observed_through=BOUNDARY
            )
            == []
        )
        assert (
            snapshot.read_inference_call_summaries("acme", inference_call_ids=set())
            == []
        )
        for identity in ("a", "b"):
            assert [
                item.inference_call_id
                for item in snapshot.read_inference_call_summaries(
                    "acme", inference_call_ids={identity}
                )
            ] == [identity]
        with pytest.raises(OperationalReportLimitExceeded):
            snapshot.read_inference_call_summaries(
                "acme", inference_call_ids={"a", "b"}, limit=1
            )
        assert (
            len(
                snapshot.read_inference_call_summaries(
                    "acme", inference_call_ids={"a", "b"}, limit=2
                )
            )
            == 2
        )
    oversized = {str(i) for i in range(COMPOSITE_FILTER_KEY_LIMIT + 1)}
    with pytest.raises(OperationalReportLimitExceeded):
        postgres_store.read_inference_call_identity_witnesses(
            "acme", call_ids=oversized, observed_through=BOUNDARY
        )
    with pytest.raises(OperationalReportLimitExceeded):
        postgres_store.read_inference_call_summaries(
            "acme", inference_call_ids=oversized
        )
    with pytest.raises(ValueError, match="aware"):
        postgres_store.read_inference_call_identity_witnesses(
            "acme", call_ids={"valid"}, observed_through=BOUNDARY.replace(tzinfo=None)
        )
    with pytest.raises(ValidationError):
        postgres_store.read_inference_call_identity_witnesses(
            "invalid org", call_ids={"valid"}, observed_through=BOUNDARY
        )


def test_witness_query_uses_native_alias_index_on_unrelated_history(
    postgres_store, postgres_engine
):
    # Bulk-load real canonical Fact shapes; only index access is under test.
    with postgres_engine.begin() as connection:
        for start in range(0, 4000, 500):
            facts, aliases = [], []
            for index in range(start, start + 500):
                fact = call(f"history-{index}", [f"tool-{index}"])
                values = fact.model_dump(mode="python")
                for name in ("input_messages", "output_messages", "raw"):
                    values[name] = json.dumps(values[name], ensure_ascii=True)
                values["call_alias_count"] = 2
                facts.append(values)
                aliases.extend(
                    {
                        "inference_call_id": fact.inference_call_id,
                        "org_id": fact.org_id,
                        "ordinal": ordinal,
                        "call_id": identifier,
                    }
                    for ordinal, identifier in enumerate(
                        (fact.model_call_id, f"tool-{index}")
                    )
                )
            connection.execute(inference_calls.insert(), facts)
            connection.execute(inference_call_aliases.insert(), aliases)
        connection.exec_driver_sql("ANALYZE inference_calls")
        connection.exec_driver_sql("ANALYZE inference_call_aliases")
    captured = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        captured.append((statement, parameters))

    event.listen(postgres_engine, "before_cursor_execute", capture)
    try:
        witnesses = postgres_store.read_inference_call_identity_witnesses(
            "acme",
            call_ids={"tool-2000", "provider-history-2001"},
            observed_through=BOUNDARY,
        )
    finally:
        event.remove(postgres_engine, "before_cursor_execute", capture)
    assert {item.inference_call_id for item in witnesses} == {
        "history-2000",
        "history-2001",
    }
    queries = [
        (sql, params)
        for sql, params in captured
        if sql.startswith("SELECT alias_owners.")
    ]
    assert len(queries) == 1
    statement, parameters = queries[0]
    with postgres_engine.connect() as connection:
        plan = connection.exec_driver_sql(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
        ).scalar_one()
    assert "ix_inference_call_aliases_lookup" in json.dumps(plan)

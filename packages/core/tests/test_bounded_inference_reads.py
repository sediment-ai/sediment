# SPDX-License-Identifier: AGPL-3.0-or-later
"""Complete evidence reads bound payload lifetime without changing Fact values."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
import gc
import math
import weakref

import pytest
from sqlalchemy import event

from sediment_core import (
    FactTable,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    OperationalReportLimitExceeded,
    TextPart,
    ToolCallPart,
)
from sediment_core import store as store_module

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _call(identifier, *, session="session", observed_at=T0, org="acme"):
    return InferenceCall(
        inference_call_id=identifier,
        org_id=org,
        session_id=session,
        gateway_provider=GatewayProvider.LITELLM,
        model_call_id=identifier,
        model="model-a",
        input_messages=[InferenceMessage(role="user", parts=[TextPart(content="x")])],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[
                    ToolCallPart(
                        id=f"tool-{identifier}",
                        name="Edit",
                        arguments={"value": float("nan")},
                    )
                ],
            )
        ],
        raw={"nul": "\x00", "surrogate": "\ud800", "infinite": float("inf")},
        observed_at=observed_at,
    )


def test_selected_call_iterator_preserves_snapshot_order_values_and_lifetime(
    postgres_store,
):
    calls = [_call("b"), _call("a"), _call("other", org="other")]
    for call in calls:
        postgres_store.store_inference_call(call)
    with postgres_store.read_snapshot() as snapshot:
        method = getattr(snapshot, "iter_inference_calls_by_ids", None)
        assert callable(method), "selected full calls require an incremental read"
        iterator = method("acme", {"a", "b", "other", "late"})
        first = next(iterator)
        assert first.inference_call_id == "a"
        assert first.raw["nul"] == "\x00"
        assert first.raw["surrogate"] == "\ud800"
        assert math.isinf(first.raw["infinite"])
        assert math.isnan(first.output_messages[0].parts[0].arguments["value"])
        first_reference = weakref.ref(first)
        del first
        postgres_store.store_inference_call(
            _call("late", observed_at=T0 - timedelta(days=1))
        )
        postgres_store.quarantine_fact(
            "acme", FactTable.INFERENCE_CALLS, "b", reason="review"
        )
        second = next(iterator)
        gc.collect()
        assert first_reference() is None
        assert second.inference_call_id == "b"
        assert list(iterator) == []
        assert [
            item.inference_call_id for item in method("acme", {"a", "b", "late"})
        ] == ["a", "b"]
    with postgres_store.read_snapshot() as snapshot:
        assert [
            item.inference_call_id
            for item in snapshot.iter_inference_calls_by_ids("acme", {"a", "b", "late"})
        ] == ["late", "a"]
        assert list(snapshot.iter_inference_calls_by_ids("acme", set())) == []


def test_public_iterator_closing_releases_snapshot_after_consumer_error(
    postgres_store, postgres_engine
):
    for identifier in ("a", "b"):
        postgres_store.store_inference_call(_call(identifier))
    checked_out = postgres_engine.pool.checkedout()
    with pytest.raises(RuntimeError, match="consumer failed"):
        with closing(
            postgres_store.iter_inference_calls_by_ids("acme", {"a", "b"})
        ) as rows:
            assert next(rows).inference_call_id == "a"
            assert postgres_engine.pool.checkedout() == checked_out + 1
            raise RuntimeError("consumer failed")
    assert postgres_engine.pool.checkedout() == checked_out


def test_session_reader_keeps_complete_bounded_group_without_cache(postgres_store):
    for call in (
        _call("inside"),
        _call("earlier", observed_at=T0 - timedelta(days=7)),
        _call("later", observed_at=T0 + timedelta(days=1)),
        _call("unrelated", session="other"),
        _call("quarantined"),
    ):
        postgres_store.store_inference_call(call)
    postgres_store.quarantine_fact(
        "acme", FactTable.INFERENCE_CALLS, "quarantined", reason="review"
    )
    with postgres_store.read_snapshot() as snapshot:
        method = getattr(snapshot, "read_session_inference_calls", None)
        assert callable(method), "complete Sessions need a bounded content read"
        rows = method("acme", "session", observed_through=T0)
        assert [row.inference_call_id for row in rows] == ["earlier", "inside"]
        assert all(not hasattr(row, "raw") for row in rows)
        reference = weakref.ref(rows[0])
        del rows
        gc.collect()
        assert reference() is None
        assert method("acme", "absent", observed_through=T0) == []


@pytest.mark.parametrize(
    "reader", ["full", "session", "report", "identity", "attribution"]
)
def test_payload_budget_refuses_before_content_transfer(
    postgres_store, postgres_engine, monkeypatch, reader
):
    postgres_store.store_inference_call(_call("a"))
    monkeypatch.setattr(
        store_module, "INFERENCE_CALL_ROW_BYTES_LIMIT", 1, raising=False
    )
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", record)
    try:
        with postgres_store.read_snapshot() as snapshot:
            with pytest.raises(OperationalReportLimitExceeded, match="encoded.*bytes"):
                if reader == "full":
                    list(snapshot.iter_inference_calls_by_ids("acme", {"a"}))
                elif reader == "session":
                    snapshot.read_session_inference_calls("acme", "session")
                elif reader == "report":
                    snapshot.read_report_inference_calls("acme")
                elif reader == "identity":
                    snapshot.read_inference_call_identities(
                        "acme", observed_through=T0, limit=10
                    )
                else:
                    snapshot.read_attribution_candidates(
                        "acme", observed_between=(T0, T0), limit=10
                    )
    finally:
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert not any(
        "SELECT inference_calls." in statement
        and "inference_calls.output_messages," in statement
        for statement in statements
    )


def test_session_budget_counts_the_complete_group(postgres_store, monkeypatch):
    for index in range(3):
        postgres_store.store_inference_call(_call(str(index)))
    monkeypatch.setattr(store_module, "INFERENCE_SESSION_BYTES_LIMIT", 1, raising=False)
    with postgres_store.read_snapshot() as snapshot:
        with pytest.raises(
            OperationalReportLimitExceeded, match="Session.*encoded.*bytes"
        ):
            snapshot.read_session_inference_calls("acme", "session")


def test_report_projection_refuses_default_population_limit(
    postgres_store, monkeypatch
):
    postgres_store.store_inference_call(_call("a"))
    postgres_store.store_inference_call(_call("b"))
    monkeypatch.setattr(store_module, "INFERENCE_REPORT_ROW_LIMIT", 1, raising=False)
    with pytest.raises(OperationalReportLimitExceeded, match="report cohort exceeds 1"):
        postgres_store.read_report_inference_calls("acme")


def test_report_projection_selection_and_output_capacity(postgres_store, monkeypatch):
    for call in (_call("a"), _call("b"), _call("other", org="other")):
        postgres_store.store_inference_call(call)
    rows = postgres_store.read_report_inference_calls(
        "acme", inference_call_ids={"a", "other"}, observed_through=T0, limit=1
    )
    assert len(rows) == 1
    assert rows[0].inference_call_id == "a"
    assert rows[0].model == "model-a"
    assert rows[0].gateway_provider is GatewayProvider.LITELLM
    assert not hasattr(rows[0], "input_messages")
    assert not hasattr(rows[0], "raw")
    assert math.isnan(rows[0].output_messages[0].parts[0].arguments["value"])
    assert (
        postgres_store.read_report_inference_calls("acme", inference_call_ids=set())
        == []
    )
    monkeypatch.setattr(store_module, "INFERENCE_PROJECTION_BYTES_LIMIT", 1)
    with pytest.raises(OperationalReportLimitExceeded, match="encoded.*bytes"):
        postgres_store.read_report_inference_calls("acme")

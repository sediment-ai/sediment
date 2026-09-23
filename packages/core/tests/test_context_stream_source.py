# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keyword streams retain complete evidence inside one bounded snapshot."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json

import pytest
from psycopg import ServerCursor
from sqlalchemy import event, func, select

from sediment_core import (
    ContextCommitAnchor,
    ContextScanMetadata,
    EvidenceReference,
    FactTable,
    InferenceCall,
    InferenceMessage,
    Push,
    ReasoningPart,
    SessionCommitObservation,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)
from sediment_core import evidence
from sediment_core.postgres_schema import (
    inference_calls,
    session_commit_observations,
    sessions,
)

T0 = datetime(2026, 9, 23, tzinfo=UTC)
IDENTITY = dict(
    repository_provider="github", repository_host="github.com", repository_id="42"
)


def call(identifier="call", *, org="acme", session="session", when=T0):
    return InferenceCall(
        inference_call_id=identifier,
        org_id=org,
        session_id=session,
        gateway_provider="litellm",
        observed_at=when,
        model="model",
        model_provider="provider",
        user_id="private-user",
        input_messages=[
            InferenceMessage(role="empty", parts=[]),
            InferenceMessage(
                role="user\x00\ud800",
                parts=[TextPart(content="parser"), ReasoningPart(content="plan")],
            ),
        ],
        output_messages=[
            InferenceMessage(
                role="assistant",
                finish_reason="tool_calls",
                parts=[
                    ToolCallPart(id="alias", name="parser", arguments={"n": 2**100}),
                    ToolCallResponsePart(id="alias", result={"n": -0.0}),
                ],
            )
        ],
        raw={"private": "secret"},
    )


def stream(snapshot, discovery, *, scope=("session",), commit=None):
    if discovery:
        return snapshot.stream_context_discovery_source("acme", scope, commit)
    return snapshot.stream_context_source("acme", scope[0])


@contextmanager
def queries(engine):
    recorded = []

    def record(connection, cursor, statement, parameters, context, executemany):
        recorded.append((statement, context.execution_options, cursor))

    event.listen(engine, "after_cursor_execute", record)
    try:
        yield recorded
    finally:
        event.remove(engine, "after_cursor_execute", record)


def content_queries(recorded):
    return [
        entry
        for entry in recorded
        if entry[0].startswith("SELECT inference_calls.")
        and "input_messages" in entry[0].split("FROM", 1)[0]
    ]


def source_sizes(engine, discovery):
    metadata = ("inference_call_id", "model_provider", "model")
    if discovery:
        metadata = ("session_id", *metadata)
    size = sum(
        func.coalesce(func.octet_length(inference_calls.c[name]), 0)
        for name in (*metadata, "input_messages", "output_messages")
    )
    metadata_size = sum(
        func.coalesce(func.octet_length(inference_calls.c[name]), 0)
        for name in metadata
    )
    with engine.connect() as connection:
        return tuple(
            int(value)
            for value in connection.execute(
                select(func.sum(size), func.max(size), func.sum(metadata_size))
            ).one()
        )


def anchor():
    return ContextCommitAnchor(**IDENTITY, commit_sha="a" * 40)


def observe(store):
    store.store_push(
        Push(
            push_id="source-push",
            org_id="acme",
            provider="github",
            repo="acme/repo",
            clone_url="https://private.invalid/repo",
            ref="refs/heads/main",
            before_sha="0" * 40,
            after_sha="b" * 40,
            captured_at=T0,
            **IDENTITY,
        )
    )
    store.store_session_commit_observation(
        SessionCommitObservation(
            observation_id="witness",
            org_id="acme",
            repo="acme/repo",
            session_id="session",
            commit_sha="a" * 40,
            source_push_id="source-push",
            captured_at=T0,
            **IDENTITY,
        )
    )


@pytest.mark.parametrize("discovery", [False, True])
def test_stream_preserves_eager_content_and_orders_rows(postgres_store, discovery):
    for value in (
        call("later", when=T0 + timedelta(microseconds=1)),
        call("b"),
        call("a"),
    ):
        postgres_store.store_inference_call(value)
    eager = postgres_store.read_context_source("acme", "session")
    with postgres_store.read_snapshot() as snapshot:
        reader = getattr(
            snapshot,
            "stream_context_discovery_source" if discovery else "stream_context_source",
            None,
        )
        assert callable(reader), "snapshot must expose a bounded keyword stream"
        with reader("acme", ["session"] if discovery else "session") as (meta, items):
            assert isinstance(meta, ContextScanMetadata)
            assert meta.visible_inference_calls == 3
            assert meta.authorized_sessions == 1
            assert [s.session_id for s in meta.sessions] == ["session"]
            assert iter(items) is items
            actual = list(items)
    expected = sorted(
        eager.items,
        key=lambda item: (
            item.observed_at,
            item.reference.inference_call_id,
            item.reference.side,
            item.reference.message_index,
            item.reference.part_index,
        ),
    )
    assert actual == [("session", item) for item in expected]
    assert actual[0][1].reference == EvidenceReference("a", "input", 1, 0)


@pytest.mark.parametrize("discovery", [False, True])
def test_stream_uses_one_row_server_buffer_and_closes_cursor(
    postgres_store, postgres_engine, monkeypatch, discovery
):
    for identifier in ("b", "a", "c"):
        postgres_store.store_inference_call(call(identifier))
    fetched = []
    original_fetchmany = ServerCursor.fetchmany

    def fetchmany(cursor, size=0):
        fetched.append(size)
        return original_fetchmany(cursor, size)

    monkeypatch.setattr(ServerCursor, "fetchmany", fetchmany)
    with queries(postgres_engine) as recorded:
        with postgres_store.read_snapshot() as snapshot:
            with stream(snapshot, discovery) as (_, items):
                assert len(list(items)) == 12
            assert not snapshot._cache
    payload = content_queries(recorded)
    assert len(payload) == 1
    statement, options, cursor = payload[0]
    assert isinstance(cursor, ServerCursor)
    assert options["yield_per"] == 1
    assert fetched and set(fetched) == {1}
    assert cursor.closed
    assert postgres_engine.pool.checkedout() == 0
    columns = statement.split("FROM", 1)[0]
    assert ("inference_calls.session_id" in columns) == discovery
    assert ".raw" not in columns and ".user_id" not in columns
    assert "model_provider" in columns and "inference_calls.model," in columns
    assert "ORDER BY" in statement
    assert next(items, None) is None


@pytest.mark.parametrize("discovery", [False, True])
@pytest.mark.parametrize(
    ("constant", "index"),
    [
        ("CONTEXT_SCAN_SOURCE_BYTES_LIMIT", 0),
        ("CONTEXT_SCAN_ROW_BYTES_LIMIT", 1),
        ("CONTEXT_SCAN_METADATA_BYTES_LIMIT", 2),
    ],
)
def test_stream_byte_boundaries_precede_content_transfer(
    postgres_store, postgres_engine, monkeypatch, discovery, constant, index
):
    for identifier in ("a-😀", "b-😀"):
        value = call(identifier)
        value.model_provider = None
        postgres_store.store_inference_call(value)
    exact = source_sizes(postgres_engine, discovery)[index]
    monkeypatch.setattr(evidence, constant, exact)
    with postgres_store.read_snapshot() as snapshot:
        with stream(snapshot, discovery) as (_, items):
            assert len(list(items)) == 8
    monkeypatch.setattr(evidence, constant, exact - 1)
    with queries(postgres_engine) as recorded:
        with pytest.raises(evidence.EvidenceReadError) as caught:
            with postgres_store.read_snapshot() as snapshot:
                with stream(snapshot, discovery):
                    pytest.fail("oversized source reached consumer")
    assert caught.value.detail == {
        "reason": "evidence_source_limit",
        "bytes": exact,
        "limit": exact - 1,
    }
    assert not content_queries(recorded)


@pytest.mark.parametrize("discovery", [False, True])
def test_stream_call_limit_is_aggregate_before_content(
    postgres_store, postgres_engine, monkeypatch, discovery
):
    for identifier in ("a", "b"):
        postgres_store.store_inference_call(
            call(identifier, session=identifier if discovery else "session")
        )
    scope = ("a", "b") if discovery else ("session",)
    monkeypatch.setattr(evidence, "EVIDENCE_INVENTORY_LIMIT", 2)
    with postgres_store.read_snapshot() as snapshot:
        with stream(snapshot, discovery, scope=scope) as (meta, items):
            assert meta.visible_inference_calls == 2
            assert len(list(items)) == 8
    monkeypatch.setattr(evidence, "EVIDENCE_INVENTORY_LIMIT", 1)
    with queries(postgres_engine) as recorded:
        with pytest.raises(evidence.EvidenceReadError) as caught:
            with postgres_store.read_snapshot() as snapshot:
                with stream(snapshot, discovery, scope=scope):
                    pytest.fail("too many calls reached consumer")
    assert caught.value.detail == {
        "reason": "evidence_inventory_limit",
        "count": 2,
        "limit": 1,
    }
    assert not content_queries(recorded)


@pytest.mark.parametrize("discovery", [False, True])
def test_stream_part_limit_counts_complete_population(
    postgres_store, postgres_engine, monkeypatch, discovery
):
    for identifier in ("a", "b", "c"):
        value = call(identifier, session=identifier if discovery else "session")
        value.output_messages[0].parts[0].arguments["n"] = float("nan")
        postgres_store.store_inference_call(value)
    scope = ("a", "b", "c") if discovery else ("session",)
    monkeypatch.setattr(evidence, "CONTEXT_SCAN_PART_LIMIT", 12)
    with postgres_store.read_snapshot() as snapshot:
        with stream(snapshot, discovery, scope=scope) as (_, items):
            assert len(list(items)) == 12
    monkeypatch.setattr(evidence, "CONTEXT_SCAN_PART_LIMIT", 5)
    seen = []
    with queries(postgres_engine) as recorded:
        with pytest.raises(evidence.EvidenceReadError) as caught:
            with postgres_store.read_snapshot() as snapshot:
                with stream(snapshot, discovery, scope=scope) as (_, items):
                    for item in items:
                        seen.append(item)
    assert len(seen) <= 5
    assert caught.value.detail == {
        "reason": "retrieval_part_limit",
        "count": 12,
        "limit": 5,
    }
    assert content_queries(recorded)[0][2].closed


@pytest.mark.parametrize("discovery", [False, True])
def test_keyword_limits_do_not_reuse_eager_limits(
    postgres_store, monkeypatch, discovery
):
    postgres_store.store_inference_call(call())
    monkeypatch.setattr(evidence, "EVIDENCE_SOURCE_BYTES_LIMIT", 1)
    monkeypatch.setattr(evidence, "CONTEXT_SOURCE_PART_LIMIT", 1)
    with postgres_store.read_snapshot() as snapshot:
        with stream(snapshot, discovery) as (_, items):
            assert len(list(items)) == 4
    with pytest.raises(evidence.EvidenceReadError):
        postgres_store.read_context_source("acme", "session")


@pytest.mark.parametrize("discovery", [False, True])
@pytest.mark.parametrize("failure", ["early_return", "consumer", "decode"])
def test_stream_cleanup_on_incomplete_consumption(
    postgres_store, postgres_engine, discovery, failure
):
    postgres_store.store_inference_call(call())
    if failure == "decode":
        values = call("z").model_dump(mode="python")
        values.update(
            input_messages="invalid JSON",
            output_messages="[]",
            raw="{}",
            call_alias_count=0,
        )
        with postgres_engine.begin() as connection:
            connection.execute(inference_calls.insert().values(values))
    expected = json.JSONDecodeError if failure == "decode" else RuntimeError
    with queries(postgres_engine) as recorded:
        with pytest.raises(expected) as caught:
            with postgres_store.read_snapshot() as snapshot:
                with stream(snapshot, discovery) as (_, items):
                    next(items)
                    if failure == "consumer":
                        raise RuntimeError("consumer failure")
                    if failure == "decode":
                        list(items)
    if failure == "consumer":
        assert str(caught.value) == "consumer failure"
    elif failure == "early_return":
        assert "consumed" in str(caught.value)
    assert content_queries(recorded)[0][2].closed
    assert postgres_engine.pool.checkedout() == 0
    assert next(items, None) is None


@pytest.mark.parametrize("discovery", [False, True])
def test_stream_holds_snapshot_then_observes_quarantine_and_release(
    postgres_store, discovery
):
    postgres_store.store_inference_call(call())
    with postgres_store.read_snapshot() as snapshot:
        with stream(snapshot, discovery) as (before, items):
            postgres_store.store_inference_call(
                call("backdated", when=T0 - timedelta(1))
            )
            postgres_store.quarantine_fact(
                "acme", FactTable.INFERENCE_CALLS, "call", reason="review"
            )
            assert {item.reference.inference_call_id for _, item in items} == {"call"}
    with postgres_store.read_snapshot() as snapshot:
        with stream(snapshot, discovery) as (after, items):
            assert {item.reference.inference_call_id for _, item in items} == {
                "backdated"
            }
    assert before.quarantine_revision == 0
    assert after.quarantine_revision > before.quarantine_revision
    assert after.visible_inference_calls == after.quarantined_inference_calls == 1
    postgres_store.release_fact(
        "acme", FactTable.INFERENCE_CALLS, "call", reason="clear"
    )
    with postgres_store.read_snapshot() as snapshot:
        with stream(snapshot, discovery) as (released, items):
            assert len(list(items)) == 8
    assert released.quarantined_inference_calls == 0


def test_stream_distinguishes_missing_empty_and_hidden_sessions(
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
    postgres_store.store_inference_call(call(session="hidden"))
    postgres_store.quarantine_fact(
        "acme", FactTable.INFERENCE_CALLS, "call", reason="hide"
    )
    postgres_store.store_inference_call(call("foreign", org="other", session="foreign"))
    for session in ("missing", "foreign"):
        with postgres_store.read_snapshot() as snapshot:
            with pytest.raises(evidence.EvidenceReadError) as caught:
                with snapshot.stream_context_source("acme", session):
                    pytest.fail("missing singleton reached consumer")
        assert caught.value.detail == {"reason": "evidence_unavailable"}
    with postgres_store.read_snapshot() as snapshot:
        with snapshot.stream_context_discovery_source(
            "acme", ["missing", "foreign", "hidden", "empty"]
        ) as (meta, items):
            assert list(items) == []
    assert [s.session_id for s in meta.sessions] == ["empty", "hidden"]
    assert meta.authorized_sessions == 4
    assert meta.visible_inference_calls == 0
    assert meta.quarantined_inference_calls == 1


@pytest.mark.parametrize("scope", [[], ["a", "a"], ["a"] * 33, [""], "session"])
def test_stream_rejects_invalid_grant_before_source_sql(
    postgres_store, postgres_engine, scope
):
    with postgres_store.read_snapshot() as snapshot:
        with queries(postgres_engine) as recorded:
            with pytest.raises(ValueError):
                with snapshot.stream_context_discovery_source("acme", scope):
                    pytest.fail("invalid grant reached consumer")
    assert recorded == []


def test_stream_never_parses_content_outside_visible_grant(
    postgres_store, postgres_engine
):
    postgres_store.store_inference_call(call())
    for identifier, org, session in (
        ("foreign", "other", "session"),
        ("outside", "acme", "outside"),
        ("hidden", "acme", "session"),
    ):
        values = call(identifier, org=org, session=session).model_dump(mode="python")
        values.update(
            input_messages="invalid JSON",
            output_messages="[]",
            raw="{}",
            call_alias_count=0,
        )
        with postgres_engine.begin() as connection:
            connection.execute(inference_calls.insert().values(values))
    postgres_store.quarantine_fact(
        "acme", FactTable.INFERENCE_CALLS, "hidden", reason="hide"
    )
    with postgres_store.read_snapshot() as snapshot:
        with snapshot.stream_context_discovery_source("acme", ["session"]) as (
            meta,
            items,
        ):
            assert len(list(items)) == 4
    assert meta.visible_inference_calls == meta.quarantined_inference_calls == 1


@pytest.mark.parametrize(
    "constant", ["CONTEXT_SCAN_SOURCE_BYTES_LIMIT", "CONTEXT_SCAN_METADATA_BYTES_LIMIT"]
)
def test_stream_commit_witnesses_are_accounted_before_transfer(
    postgres_store, postgres_engine, monkeypatch, constant
):
    postgres_store.store_inference_call(call())
    observe(postgres_store)
    eager = postgres_store.read_context_discovery_source("acme", ["session"], anchor())
    with postgres_engine.connect() as connection:
        witness_bytes = int(
            connection.execute(
                select(
                    sum(
                        func.octet_length(session_commit_observations.c[name])
                        for name in ("session_id", "observation_id", "source_push_id")
                    )
                )
            ).scalar_one()
        )
    exact = (
        source_sizes(postgres_engine, True)[
            0 if constant == "CONTEXT_SCAN_SOURCE_BYTES_LIMIT" else 2
        ]
        + witness_bytes
    )
    monkeypatch.setattr(evidence, constant, exact)
    with postgres_store.read_snapshot() as snapshot:
        with snapshot.stream_context_discovery_source(
            "acme", ["session"], anchor()
        ) as (meta, items):
            assert len(list(items)) == 4
    assert meta.sessions[0].commit_match == eager.sessions[0].commit_match
    assert meta.commit == anchor()
    monkeypatch.setattr(evidence, constant, exact - 1)
    with queries(postgres_engine) as recorded:
        with pytest.raises(evidence.EvidenceReadError) as caught:
            with postgres_store.read_snapshot() as snapshot:
                with snapshot.stream_context_discovery_source(
                    "acme", ["session"], anchor()
                ):
                    pytest.fail("oversized witness source reached consumer")
    assert caught.value.detail == {
        "reason": "evidence_source_limit",
        "bytes": exact,
        "limit": exact - 1,
    }
    assert not content_queries(recorded)
    assert not any(
        sql.startswith("SELECT session_commit_observations.") for sql, _, _ in recorded
    )


@pytest.mark.parametrize(
    "table,identifier",
    [
        (FactTable.PUSHES, "source-push"),
        (FactTable.SESSION_COMMIT_OBSERVATIONS, "witness"),
    ],
)
def test_stream_commit_witness_shares_snapshot_and_parent_visibility(
    postgres_store, table, identifier
):
    observe(postgres_store)
    with postgres_store.read_snapshot() as snapshot:
        with snapshot.stream_context_discovery_source(
            "acme", ["session"], anchor()
        ) as (before, items):
            postgres_store.quarantine_fact("acme", table, identifier, reason="hide")
            assert list(items) == []
    assert before.sessions[0].commit_match is not None
    with postgres_store.read_snapshot() as snapshot:
        with snapshot.stream_context_discovery_source(
            "acme", ["session"], anchor()
        ) as (after, items):
            assert list(items) == []
    assert after.sessions[0].commit_match is None
    assert after.quarantine_revision > before.quarantine_revision

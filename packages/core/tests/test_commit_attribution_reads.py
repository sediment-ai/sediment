# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stored scalar Push iteration and exact Attribution candidate selection."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sediment_core import (
    FactTable,
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    TextPart,
)

T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)


def test_attribution_candidates_admit_only_notes_sessions_in_long_window(
    postgres_store,
):
    for identifier, session, at in (
        ("jaccard", "other", T0),
        ("noted", "noted-session", T0 - timedelta(days=3)),
        ("unrelated-old", "other", T0 - timedelta(days=3)),
        ("future", "noted-session", T0 + timedelta(seconds=1)),
    ):
        postgres_store.store_inference_call(
            InferenceCall(
                inference_call_id=identifier,
                org_id="target",
                session_id=session,
                gateway_provider=GatewayProvider.LITELLM,
                input_messages=[],
                observed_at=at,
                output_messages=[
                    InferenceMessage(role="assistant", parts=[TextPart(content="code")])
                ],
            )
        )
    rows = postgres_store.read_attribution_candidates(
        "target",
        observed_between=(T0 - timedelta(hours=1), T0),
        note_session_ids={"noted-session"},
        notes_observed_between=(T0 - timedelta(days=7), T0),
    )
    assert {row.inference_call_id for row in rows} == {"jaccard", "noted"}


@pytest.mark.parametrize("collation", ["C", "en-US-x-icu"])
def test_attribution_push_iterator_filters_scope_and_preserves_python_id_order(
    postgres_store_factory, collation
):
    _, postgres_store = postgres_store_factory()
    with postgres_store._engine.begin() as connection:
        if not connection.execute(
            text("SELECT 1 FROM pg_collation WHERE collname = :name"),
            {"name": collation},
        ).scalar():
            pytest.skip(f"PostgreSQL collation {collation} is unavailable")
        connection.execute(
            text(
                f'ALTER TABLE pushes ALTER COLUMN push_id TYPE text COLLATE "{collation}"'
            )
        )
    for identifier, repo, at in (
        ("é", "target/repo", T0),
        ("a", "target/repo", T0),
        ("Z", "target/repo", T0),
        ("foreign-repo", "target/other", T0),
        ("late", "target/repo", T0 + timedelta(seconds=1)),
    ):
        postgres_store.store_push(
            Push(
                push_id=identifier,
                org_id="target",
                provider=ForgeProvider.GITHUB,
                repo=repo,
                clone_url="secret://unused",
                ref=f"refs/heads/{identifier}",
                before_sha="0" * 40,
                after_sha="a" * 40,
                captured_at=at,
            )
        )
    rows = list(
        postgres_store.iter_attribution_pushes(
            "target",
            captured_through=T0,
            repository_key="target/repo",
        )
    )
    assert [row.push_id for row in rows] == ["Z", "a", "é"]
    assert all(row.org_id == "target" for row in rows)
    assert all(not hasattr(row, "clone_url") for row in rows)


def test_candidate_union_has_inclusive_bounds_quarantine_and_one_copy(postgres_store):
    lower = T0 - timedelta(hours=1)
    note_lower = T0 - timedelta(days=7)
    data = [
        ("lower", "other", lower, "target"),
        ("before", "other", lower - timedelta(microseconds=1), "target"),
        ("upper-overlap", "noted", T0, "target"),
        ("notes-lower", "noted", note_lower, "target"),
        ("notes-before", "noted", note_lower - timedelta(microseconds=1), "target"),
        ("foreign", "noted", T0, "foreign"),
        ("hidden", "noted", T0, "target"),
    ]
    for identifier, session, at, org in data:
        postgres_store.store_inference_call(
            InferenceCall(
                inference_call_id=identifier,
                org_id=org,
                session_id=session,
                gateway_provider=GatewayProvider.LITELLM,
                input_messages=[],
                output_messages=[],
                observed_at=at,
            )
        )
    postgres_store.quarantine_fact(
        "target", FactTable.INFERENCE_CALLS, "hidden", reason="test"
    )
    kwargs = dict(
        observed_between=(lower, T0),
        note_session_ids={"noted"},
        notes_observed_between=(note_lower, T0),
    )
    assert [
        row.inference_call_id
        for row in postgres_store.read_attribution_candidates("target", **kwargs)
    ] == ["notes-lower", "lower", "upper-overlap"]
    postgres_store.release_fact(
        "target", FactTable.INFERENCE_CALLS, "hidden", reason="test"
    )
    assert {
        row.inference_call_id
        for row in postgres_store.read_attribution_candidates("target", **kwargs)
    } == {"notes-lower", "lower", "upper-overlap", "hidden"}
    assert {
        row.inference_call_id
        for row in postgres_store.read_attribution_candidates(
            "target", **{**kwargs, "note_session_ids": set()}
        )
    } == {"lower", "upper-overlap", "hidden"}


def test_candidate_budgets_apply_only_to_exact_population(postgres_store, monkeypatch):
    from sediment_core import OperationalReportLimitExceeded, store as store_module

    for identifier, session, at, size in (
        ("small", "other", T0, 10),
        ("large", "unrelated", T0 - timedelta(days=2), 1000),
        ("future", "noted", T0 + timedelta(microseconds=1), 1000),
    ):
        postgres_store.store_inference_call(
            InferenceCall(
                inference_call_id=identifier,
                org_id="target",
                session_id=session,
                gateway_provider=GatewayProvider.LITELLM,
                input_messages=[],
                observed_at=at,
                output_messages=[
                    InferenceMessage(
                        role="assistant", parts=[TextPart(content="x" * size)]
                    )
                ],
            )
        )
    monkeypatch.setattr(store_module, "INFERENCE_CALL_ROW_BYTES_LIMIT", 500)
    kwargs = dict(
        observed_between=(T0 - timedelta(hours=1), T0),
        note_session_ids={"noted"},
        notes_observed_between=(T0 - timedelta(days=7), T0),
    )
    assert [
        row.inference_call_id
        for row in postgres_store.read_attribution_candidates("target", **kwargs)
    ] == ["small"]
    with pytest.raises(OperationalReportLimitExceeded):
        postgres_store.read_attribution_candidates(
            "target", **{**kwargs, "note_session_ids": {"unrelated"}}
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"note_session_ids": {"noted"}},
        {"notes_observed_between": (T0 - timedelta(days=7), T0)},
        {
            "note_session_ids": {""},
            "notes_observed_between": (T0 - timedelta(days=7), T0),
        },
        {
            "note_session_ids": set(),
            "notes_observed_between": (T0.replace(tzinfo=None), T0),
        },
        {
            "note_session_ids": set(),
            "notes_observed_between": (T0 + timedelta(seconds=1), T0),
        },
    ],
)
def test_candidate_note_filters_validate_before_selection(postgres_store, kwargs):
    with pytest.raises(ValueError):
        postgres_store.read_attribution_candidates(
            "target", observed_between=(T0 - timedelta(hours=1), T0), **kwargs
        )


def test_stored_push_stream_preserves_identity_and_live_visibility(postgres_store):
    common = dict(
        org_id="target",
        provider=ForgeProvider.GITHUB,
        repo="target/renamed",
        clone_url="unused",
        before_sha="0" * 40,
        after_sha="a" * 40,
        captured_at=T0,
        repository_provider=ForgeProvider.GITHUB,
        repository_host="github.com",
        repository_id="123",
    )
    for identifier, update in (
        ("first", {"repo": "target/old"}),
        ("hidden", {}),
        ("other", {"repository_id": "999"}),
        ("foreign", {"org_id": "foreign"}),
    ):
        postgres_store.store_push(
            Push(
                **{
                    **common,
                    **update,
                    "push_id": identifier,
                    "ref": f"refs/heads/{identifier}",
                }
            )
        )
    postgres_store.quarantine_fact("target", FactTable.PUSHES, "hidden", reason="test")
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "FROM pushes" in statement:
            statements.append(statement)

    event.listen(postgres_store._engine, "before_cursor_execute", record)
    try:
        with postgres_store.read_snapshot() as snapshot:
            iterator = snapshot.iter_attribution_pushes(
                "target",
                captured_through=T0,
                repository_key=(ForgeProvider.GITHUB, "github.com", "123"),
            )
            assert [row.push_id for row in iterator] == ["first"]
            assert (
                list(
                    snapshot.iter_attribution_pushes(
                        "target", captured_through=T0, repository_key="target/old"
                    )
                )
                == []
            )
    finally:
        event.remove(postgres_store._engine, "before_cursor_execute", record)
    assert statements
    assert all(
        "clone_url" not in statement and "pushes.ref" not in statement
        for statement in statements
    )

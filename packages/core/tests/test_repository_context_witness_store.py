# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repository metadata witnesses compact history while retaining exact sources."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, insert

from sediment_core import (
    CIOutcome,
    FactTable,
    OperationalReportLimitExceeded,
    Push,
    RepositoryRename,
    SessionCommitObservation,
)
from sediment_core.postgres_schema import ci_outcomes

T0 = datetime(2026, 9, 20, tzinfo=UTC)
ORG = "acme"
IDENTITY = dict(
    repository_provider="github", repository_host="github.com", repository_id="101"
)


def ci(identifier, **overrides):
    return CIOutcome(
        **dict(
            outcome_id=identifier,
            org_id=ORG,
            provider="github_actions",
            run_id=identifier,
            repo="acme/project",
            commit_sha="a" * 40,
            branch="main",
            result="passed",
            captured_at=T0,
            **IDENTITY,
        )
        | overrides
    )


def push(identifier, **overrides):
    return Push(
        **dict(
            push_id=identifier,
            org_id=ORG,
            provider="github",
            repo="acme/project",
            clone_url="https://github.com/acme/project.git",
            ref=f"refs/heads/{identifier}",
            before_sha="0" * 40,
            after_sha="a" * 40,
            captured_at=T0,
            **IDENTITY,
        )
        | overrides
    )


def observation(identifier, source, **overrides):
    return SessionCommitObservation(
        **dict(
            observation_id=identifier,
            org_id=ORG,
            repo="acme/project",
            commit_sha="a" * 40,
            session_id=identifier,
            source_push_id=source,
            captured_at=T0,
            **IDENTITY,
        )
        | overrides
    )


def read(store, *, source_keys=frozenset(), **kwargs):
    return store.read_repository_context_witnesses(
        ORG, captured_through=T0, source_keys=set(source_keys), **kwargs
    )


def test_metadata_is_compact_and_exact_sources_remain_available(postgres_store):
    for identifier in ("c", "b", "a"):
        postgres_store.store_ci_outcome(ci(identifier))
    postgres_store.store_ci_outcome(ci("renamed", repo="acme/earlier-name"))
    rows, renames = read(
        postgres_store, source_keys={(FactTable.CI_OUTCOMES, "c", "repo")}
    )
    assert {(row.source_fact_id, row.repo) for row in rows} == {
        ("a", "acme/project"),
        ("c", "acme/project"),
        ("renamed", "acme/earlier-name"),
    }
    assert renames == []
    assert len(postgres_store.read_repository_identities(ORG, captured_through=T0)) == 4
    with pytest.raises(OperationalReportLimitExceeded):
        read(
            postgres_store, source_keys={(FactTable.CI_OUTCOMES, "c", "repo")}, limit=2
        )
    assert len(read(postgres_store, limit=2)[0]) == 2


def test_observation_witnesses_retain_source_state_and_exact_anchors(postgres_store):
    for identifier in ("first", "second", "late"):
        postgres_store.store_push(
            push(identifier, captured_at=T0 + timedelta(seconds=identifier == "late"))
        )
    for identifier, source in (("a", "late"), ("b", "first"), ("c", "second")):
        postgres_store.store_session_commit_observation(observation(identifier, source))
    rows, _ = read(
        postgres_store,
        source_keys={(FactTable.SESSION_COMMIT_OBSERVATIONS, "c", "repo")},
    )
    assert {row.source_fact_id for row in rows} == {"first", "second", "a", "b", "c"}
    assert {row.source_push_id for row in rows if row.source_push_id} == {
        "first",
        "second",
        "late",
    }
    assert all(row.captured_at <= T0 for row in rows)


def test_rename_witnesses_bound_distinct_edges_not_receipts(postgres_store):
    for identifier in ("z", "a", "b"):
        postgres_store.store_repository_rename(
            RepositoryRename(
                rename_id=identifier,
                org_id=ORG,
                old_repo="acme/old",
                new_repo="acme/project",
                captured_at=T0,
                **IDENTITY,
            )
        )
    assert [row.rename_id for row in read(postgres_store, limit=1)[1]] == ["a"]


@pytest.mark.parametrize(
    "keys",
    [
        {(FactTable.INFERENCE_CALLS, "call", "repo")},
        {(FactTable.CI_OUTCOMES, "outcome", "head_repo")},
        {(FactTable.CI_OUTCOMES, "", "repo")},
        {(FactTable.CI_OUTCOMES, "outcome", "bogus")},
    ],
)
def test_witness_source_filters_reject_invalid_keys(postgres_store, keys):
    with pytest.raises(ValueError):
        read(postgres_store, source_keys=keys)


def test_witnesses_keep_snapshot_visibility_and_select_no_content(postgres_store):
    postgres_store.store_ci_outcome(ci("a"))
    statements = []

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(postgres_store._engine, "before_cursor_execute", capture)
    try:
        with postgres_store.read_snapshot() as snapshot:
            before = read(snapshot)
            postgres_store.quarantine_fact(
                ORG, FactTable.CI_OUTCOMES, "a", reason="test"
            )
            postgres_store.store_ci_outcome(
                ci("future", captured_at=T0 + timedelta(seconds=1))
            )
            postgres_store.store_ci_outcome(ci("foreign", org_id="elsewhere"))
            assert read(snapshot) == before
        assert read(postgres_store) == ([], [])
        postgres_store.release_fact(ORG, FactTable.CI_OUTCOMES, "a", reason="test")
        assert read(postgres_store) == before
    finally:
        event.remove(postgres_store._engine, "before_cursor_execute", capture)
    selects = [
        statement.lower() for statement in statements if statement.startswith("SELECT")
    ]
    witness_selects = [statement for statement in selects if "distinct on" in statement]
    assert witness_selects
    for statement in witness_selects:
        assert "clone_url" not in statement
        assert "raw" not in statement
        assert "input_messages" not in statement
        assert "output_messages" not in statement


def test_repeated_history_exceeds_complete_cap_but_needs_one_witness(postgres_store):
    # Real canonical scalar rows, inserted in batches to keep this capacity check
    # about reads rather than 50,001 individual receipt transactions.
    values = ci("seed").model_dump(mode="python") | {"raw": "{}"}
    with postgres_store._engine.begin() as connection:
        for start in range(0, 50_001, 1_000):
            connection.execute(
                insert(ci_outcomes),
                [
                    values | {"outcome_id": f"row-{index:05}", "run_id": f"run-{index}"}
                    for index in range(start, min(start + 1_000, 50_001))
                ],
            )
    with pytest.raises(OperationalReportLimitExceeded):
        postgres_store.read_repository_identities(ORG, captured_through=T0)
    rows, renames = read(postgres_store, limit=1)
    assert [row.source_fact_id for row in rows] == ["row-00000"]
    assert renames == []
    requested, _ = read(
        postgres_store,
        source_keys={(FactTable.CI_OUTCOMES, "row-50000", "repo")},
        limit=2,
    )
    assert [row.source_fact_id for row in requested] == ["row-00000", "row-50000"]
    # Each family binds one parameter per ID. Preserve valid source requests
    # above the unrelated two-column composite filter's 30,000-key ceiling.
    requested, _ = read(
        postgres_store,
        source_keys={
            (FactTable.CI_OUTCOMES, f"row-{index:05}", "repo")
            for index in range(40_000)
        },
    )
    assert len(requested) == 40_000


def test_witness_reader_validates_boundary_org_and_key_budget(postgres_store):
    with pytest.raises(ValueError):
        postgres_store.read_repository_context_witnesses(
            ORG, captured_through=T0.replace(tzinfo=None), source_keys=set()
        )
    with pytest.raises(ValueError):
        postgres_store.read_repository_context_witnesses(
            "bad org", captured_through=T0, source_keys=set()
        )
    with pytest.raises(OperationalReportLimitExceeded):
        read(
            postgres_store,
            source_keys={
                (FactTable.CI_OUTCOMES, str(index), "repo") for index in range(50_001)
            },
        )

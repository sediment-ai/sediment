# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exact repository filters through public store and snapshot reads."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event

import sediment_core as core

ORG = "acme"
NOW = datetime(2026, 9, 12, tzinfo=UTC)
SHA = "a" * 40
IDENTITY = (core.ForgeProvider.GITHUB, "github.com", "101")


def test_ci_lineage_projection_bounds_are_sql_scoped_and_cache_distinct(postgres_store):
    rows = (
        _ci("before", captured_at=NOW - timedelta(seconds=1)),
        _ci("at", captured_at=NOW),
        _ci("after", captured_at=NOW + timedelta(seconds=1)),
    )
    for row in rows:
        postgres_store.store_ci_outcome(row)
    statements = []

    def capture_sql(_conn, _cursor, statement, parameters, _context, _many):
        if "FROM ci_outcomes" in statement:
            statements.append((statement, parameters))

    event.listen(postgres_store._engine, "before_cursor_execute", capture_sql)
    try:
        with postgres_store.read_snapshot() as snapshot:
            assert len(snapshot.read_ci_outcome_projections(ORG)) == 3
            assert [
                row.outcome_id
                for row in snapshot.read_ci_outcome_projections(
                    ORG, captured_through=NOW, limit=2
                )
            ] == ["before", "at"]
            with pytest.raises(core.OperationalReportLimitExceeded):
                snapshot.read_ci_outcome_projections(ORG, captured_through=NOW, limit=1)
        assert [
            row.outcome_id
            for row in postgres_store.read_ci_outcome_projections(
                ORG, captured_through=NOW, limit=2
            )
        ] == ["before", "at"]
    finally:
        event.remove(postgres_store._engine, "before_cursor_execute", capture_sql)
    assert any("LIMIT" in sql and "captured_at <=" in sql for sql, _ in statements)
    assert all("ci_outcomes.raw" not in sql.split("FROM")[0] for sql, _ in statements)


def _ci(name, *, host="github.com", repository_id="101", repo="acme/old", **overrides):
    identity = {
        "repository_provider": "github" if repository_id else None,
        "repository_host": host if repository_id else None,
        "repository_id": repository_id,
    }
    return core.CIOutcome(
        **{
            "outcome_id": name,
            "org_id": ORG,
            "provider": "github_actions",
            "run_id": name,
            "run_attempt": 1,
            "repo": repo,
            "commit_sha": SHA,
            "branch": "main",
            "result": "failed",
            "captured_at": NOW,
            **identity,
            **overrides,
        }
    )


def _seed(store):
    rows = {}
    for name, host, repository_id, repo, number in (
        ("old", "github.com", "101", "acme/old", 1),
        ("renamed", "github.com", "101", "acme/new", 2),
        ("fork", "github.com", "202", "acme/old", 1),
        ("other-host", "forge.example.com", "101", "acme/old", 1),
        ("legacy", "github.com", None, "acme/old", 1),
    ):
        ci = _ci(name, host=host, repository_id=repository_id, repo=repo)
        fields = {
            field: getattr(ci, field)
            for field in (
                "org_id",
                "repo",
                "repository_provider",
                "repository_host",
                "repository_id",
                "captured_at",
            )
        }
        push = core.Push(
            **fields,
            push_id=f"push-{name}",
            provider="github",
            clone_url="https://example.com/repo.git",
            ref=f"refs/heads/{name}",
            before_sha="0" * 40,
            after_sha=SHA,
        )
        observation = core.SessionCommitObservation(
            **fields,
            observation_id=f"observation-{name}",
            session_id=f"session-{name}",
            commit_sha=SHA,
            source_push_id=push.push_id,
        )
        pr_fields = dict(
            **fields,
            provider="github",
            pr_number=number,
            head_repo="fork/project",
            head_ref="topic",
            head_sha=SHA,
            base_ref="main",
            base_sha="b" * 40,
        )
        merge = core.PullRequestMerge(
            **pr_fields,
            merge_id=f"merge-{name}",
            merged_at=NOW,
            merge_commit_sha="c" * 40,
        )
        revision = core.PullRequestRevision(**pr_fields, revision_id=f"revision-{name}")
        store.store_push(push)
        store.store_session_commit_observation(observation)
        store.store_ci_outcome(ci)
        store.store_pull_request_merge(merge)
        store.store_pull_request_revision(revision)
        rows[name] = (push, observation, ci, merge, revision)
    return rows


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.parametrize(
    "selector,names", [(IDENTITY, {"old", "renamed"}), ("acme/old", {"legacy"})]
)
def test_qualified_filters_preserve_one_lifetime_across_names(
    postgres_store, snapshot, selector, names
):
    rows = _seed(postgres_store)
    with postgres_store.read_snapshot() as frozen:
        reader = frozen if snapshot else postgres_store
        commits = {(selector, SHA)}
        prs = {(selector, 1), (selector, 2)}
        assert {
            r.push_id for r in reader.read_pushes(ORG, repository_commits=commits)
        } == {rows[n][0].push_id for n in names}
        assert {
            r.observation_id
            for r in reader.read_session_commit_observations(
                ORG, repository_commits=commits
            )
        } == {rows[n][1].observation_id for n in names}
        assert {
            r.outcome_id
            for r in reader.read_ci_outcomes(ORG, repository_commits=commits)
        } == {rows[n][2].outcome_id for n in names}
        assert {
            r.merge_id for r in reader.read_pull_request_merges(ORG, repository_prs=prs)
        } == {rows[n][3].merge_id for n in names}
        assert {
            r.revision_id
            for r in reader.read_pull_request_revisions(ORG, repository_prs=prs)
        } == {rows[n][4].revision_id for n in names}
        pushes, outcomes = reader.read_delivery_summaries(
            ORG, repository_commits=commits, captured_through=NOW
        )
        assert {r.push_id for r in pushes} == {rows[n][0].push_id for n in names}
        assert {r.outcome_id for r in outcomes} == {
            rows[n][2].outcome_id for n in names
        }
        assert {
            r.outcome_id
            for r in reader.read_ci_outcome_summaries(
                ORG,
                repository_key=selector,
                result=core.CIResult.FAILED,
                captured_between=(NOW, NOW + timedelta(days=1)),
                limit=100,
            )
        } == {rows[n][2].outcome_id for n in names}


@pytest.mark.parametrize("snapshot", [False, True])
def test_run_lookup_reports_ambiguity_and_selects_exact_namespace(
    postgres_store, snapshot
):
    first = _ci("first", run_id="shared-run")
    second = _ci("second", run_id="shared-run", host="forge.example.com")
    legacy = _ci("legacy", run_id="shared-run", repository_id=None)
    for row in (first, second, legacy):
        postgres_store.store_ci_outcome(row)
    assert hasattr(core, "RepositoryReadAmbiguous")
    with postgres_store.read_snapshot() as frozen:
        reader = frozen if snapshot else postgres_store
        with pytest.raises(
            core.RepositoryReadAmbiguous, match="repository_selector_ambiguous"
        ):
            reader.read_ci_outcome_by_run(
                ORG, core.CIProvider.GITHUB_ACTIONS, "shared-run", run_attempt=1
            )
        for selector, expected in (
            (IDENTITY, first),
            (("github", "forge.example.com", "101"), second),
            ("acme/old", legacy),
        ):
            actual = reader.read_ci_outcome_by_run(
                ORG,
                core.CIProvider.GITHUB_ACTIONS,
                "shared-run",
                run_attempt=1,
                repository_key=selector,
                captured_through=NOW,
            )
            assert actual.outcome_id == expected.outcome_id
        assert (
            reader.read_ci_outcome_by_run(
                ORG,
                core.CIProvider.GITHUB_ACTIONS,
                "shared-run",
                run_attempt=1,
                repository_key=("github", "github.com", "999"),
            )
            is None
        )


@pytest.mark.parametrize(
    "method,key_arg",
    [
        ("read_ci_outcomes", "repository_commits"),
        ("read_session_commit_observations", "repository_commits"),
        ("read_pushes", "repository_commits"),
        ("read_pull_request_merges", "repository_prs"),
        ("read_pull_request_revisions", "repository_prs"),
    ],
)
def test_empty_qualified_population_is_authoritative(postgres_store, method, key_arg):
    _seed(postgres_store)
    assert getattr(postgres_store, method)(ORG, **{key_arg: set()}) == []
    with postgres_store.read_snapshot() as frozen:
        assert getattr(frozen, method)(ORG, **{key_arg: set()}) == []


def test_qualified_reads_obey_boundary_quarantine_and_snapshot(postgres_store):
    rows = _seed(postgres_store)
    keys = {(IDENTITY, SHA)}
    assert (
        postgres_store.read_ci_outcomes(
            ORG,
            repository_commits=keys,
            captured_through=NOW - timedelta(microseconds=1),
        )
        == []
    )
    with postgres_store.read_snapshot() as frozen:
        assert (
            len(
                frozen.read_ci_outcomes(
                    ORG, repository_commits=keys, captured_through=NOW
                )
            )
            == 2
        )
        postgres_store.quarantine_fact(
            ORG, core.FactTable.CI_OUTCOMES, rows["old"][2].outcome_id, reason="test"
        )
        assert (
            len(
                frozen.read_ci_outcomes(
                    ORG, repository_commits=keys, captured_through=NOW
                )
            )
            == 2
        )
    assert [
        r.outcome_id
        for r in postgres_store.read_ci_outcomes(ORG, repository_commits=keys)
    ] == ["renamed"]


@pytest.mark.parametrize(
    "selector",
    [
        ("github", "github.com"),
        ("github", "../host", "101"),
        ("github", "github.com", True),
        "",
        None,
    ],
)
def test_invalid_qualified_selector_rejects_before_sql(postgres_store, selector):
    statements = []

    def observed(*args):
        statements.append(args[2])

    event.listen(postgres_store._engine, "before_cursor_execute", observed)
    try:
        with pytest.raises(ValueError):
            postgres_store.read_ci_outcomes(ORG, repository_commits={(selector, SHA)})
        assert statements == []
    finally:
        event.remove(postgres_store._engine, "before_cursor_execute", observed)


def test_qualified_filter_bound_and_conflicting_modes_reject_before_sql(postgres_store):
    statements = []

    def observed(*args):
        statements.append(args[2])

    event.listen(postgres_store._engine, "before_cursor_execute", observed)
    try:
        with pytest.raises(ValueError):
            postgres_store.read_ci_outcomes(
                ORG, repo_commits=set(), repository_commits={(IDENTITY, SHA)}
            )
        with pytest.raises(core.OperationalReportLimitExceeded):
            postgres_store.read_ci_outcomes(
                ORG,
                repository_commits={
                    (("github", "github.com", str(i)), SHA) for i in range(1, 15002)
                },
            )
        assert statements == []
    finally:
        event.remove(postgres_store._engine, "before_cursor_execute", observed)


def test_mixed_qualified_namespaces_remain_separate_and_normalized(postgres_store):
    _seed(postgres_store)
    keys = {(" ACME/OLD ", SHA), (("github", "GITHUB.COM", "101"), SHA)}
    assert {
        row.outcome_id
        for row in postgres_store.read_ci_outcomes(ORG, repository_commits=keys)
    } == {"old", "renamed", "legacy"}
    assert postgres_store.read_ci_outcomes("other-org", repository_commits=keys) == []


def test_qualified_filter_reserves_parameters_for_session_ids(postgres_store):
    keys = {(("github", "github.com", str(i)), SHA) for i in range(1, 15001)}
    statements = []

    def observed(*args):
        statements.append(args[2])

    event.listen(postgres_store._engine, "before_cursor_execute", observed)
    try:
        with pytest.raises(
            core.OperationalReportLimitExceeded, match="parameter bound"
        ):
            postgres_store.read_session_commit_observations(
                ORG, repository_commits=keys, session_ids={"session"}
            )
        assert statements == []
    finally:
        event.remove(postgres_store._engine, "before_cursor_execute", observed)


def test_qualified_pr_numbers_reject_bool_and_zero(postgres_store):
    for value in (True, 0, -1, 2**63):
        with pytest.raises(ValueError, match="invalid qualified repository filter"):
            postgres_store.read_pull_request_revisions(
                ORG, repository_prs={(IDENTITY, value)}
            )


def test_run_lookup_and_summaries_share_snapshot_visibility(postgres_store):
    first = _ci("first", run_id="shared-run")
    postgres_store.store_ci_outcome(first)
    with postgres_store.read_snapshot() as frozen:
        assert (
            frozen.read_ci_outcome_by_run(
                ORG, core.CIProvider.GITHUB_ACTIONS, "shared-run", run_attempt=1
            ).outcome_id
            == "first"
        )
        postgres_store.store_ci_outcome(
            _ci("second", run_id="shared-run", host="forge.example.com")
        )
        assert (
            frozen.read_ci_outcome_by_run(
                ORG, core.CIProvider.GITHUB_ACTIONS, "shared-run", run_attempt=1
            ).outcome_id
            == "first"
        )
        assert (
            frozen.read_ci_outcome_summaries(
                ORG,
                repository_key=("github", "forge.example.com", "101"),
                result=core.CIResult.FAILED,
                captured_between=(NOW, NOW + timedelta(days=1)),
                limit=100,
            )
            == []
        )
    with pytest.raises(core.RepositoryReadAmbiguous):
        postgres_store.read_ci_outcome_by_run(
            ORG, core.CIProvider.GITHUB_ACTIONS, "shared-run", run_attempt=1
        )


@pytest.mark.parametrize(
    "mode", ["identified_commit", "identified_pr", "legacy_commit"]
)
def test_accepted_filter_bound_executes_without_expression_stack_overflow(
    postgres_store, mode
):
    if mode == "identified_pr":
        assert (
            postgres_store.read_pull_request_revisions(
                ORG, repository_prs={(IDENTITY, n) for n in range(1, 15001)}
            )
            == []
        )
    elif mode == "legacy_commit":
        assert (
            postgres_store.read_ci_outcomes(
                ORG, repo_commits={("acme/old", f"{n:040x}") for n in range(30000)}
            )
            == []
        )
    else:
        assert (
            postgres_store.read_ci_outcomes(
                ORG, repository_commits={(IDENTITY, f"{n:040x}") for n in range(15000)}
            )
            == []
        )

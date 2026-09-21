# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repository lifetime identity survives the public FactStore boundary."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

import sediment_core as core

T0 = datetime(2026, 9, 12, tzinfo=UTC)
IDENTITY = {
    "repository_provider": "github",
    "repository_host": "github.com",
    "repository_id": "12345",
}


def push(**overrides):
    return core.Push(
        **{
            "org_id": "acme",
            "provider": "github",
            "repo": "acme/old",
            "clone_url": "https://github.com/acme/old.git",
            "ref": "refs/heads/main",
            "before_sha": "0" * 40,
            "after_sha": "a" * 40,
            "captured_at": T0,
            **IDENTITY,
            **overrides,
        }
    )


def test_repository_identity_is_captured_in_fact_contract():
    fact = push()
    assert fact.model_dump().items() >= IDENTITY.items()
    assert fact.schema_version == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"repository_id": None},
        {"repository_provider": None},
        {"repository_host": None},
        {"repository_host": "https://github.com"},
        {"repository_host": "github.com:443"},
        {"repository_host": "github.com."},
        {"repository_host": "github..com"},
        {"repository_id": "0"},
        {"repository_id": "001"},
        {"repository_id": True},
        {"repository_id": "1" * 21},
        {"repository_id": "1\x00"},
        {"schema_version": True},
        {"schema_version": 3},
        {"schema_version": 1},
    ],
)
def test_repository_identity_rejects_partial_or_unrepresentable_keys(overrides):
    with pytest.raises(ValidationError):
        push(**overrides)


def test_identity_roundtrip_retains_renamed_push_and_separates_reused_slug(
    postgres_store,
):
    original = push(push_id="first")
    assert postgres_store.store_push(original)
    assert not postgres_store.store_push(push(push_id="redelivery", repo="acme/new"))
    assert postgres_store.read_stored_push_id(push(repo="acme/new")) == "first"
    assert postgres_store.store_push(push(push_id="recreated", repository_id="67890"))
    assert postgres_store.store_push(
        push(push_id="another-host", repository_host="git.acme.test")
    )
    rows = postgres_store.read_pushes("acme")
    assert {item.push_id for item in rows} == {"first", "recreated", "another-host"}
    assert next(item for item in rows if item.push_id == "first") == original


def test_rename_receipt_is_immutable_bounded_and_quarantinable(postgres_store):
    assert hasattr(core, "RepositoryRename"), "canonical rename Fact is missing"
    fact = core.RepositoryRename(
        rename_id="rename",
        org_id="acme",
        old_repo="acme/old",
        new_repo="acme/new",
        source_event_id="github-delivery",
        captured_at=T0,
        **IDENTITY,
    )
    first = postgres_store.store_repository_rename_receipt(fact)
    duplicate = postgres_store.store_repository_rename_receipt(
        fact.model_copy(
            update={"rename_id": "duplicate", "captured_at": T0 + timedelta(days=1)}
        )
    )
    assert first.fact_id == duplicate.fact_id == "rename"
    assert first.stored and not duplicate.stored
    assert postgres_store.read_repository_renames("acme", captured_through=T0) == [fact]
    assert (
        postgres_store.read_repository_renames(
            "acme", captured_through=T0 - timedelta(microseconds=1)
        )
        == []
    )
    postgres_store.quarantine_fact(
        "acme", core.FactTable.REPOSITORY_RENAMES, "rename", reason="bad source"
    )
    assert postgres_store.read_repository_renames("acme") == []
    assert postgres_store.read_repository_renames("acme", include_quarantined=True) == [
        fact
    ]


def test_identity_population_is_complete_bounded_and_snapshot_consistent(
    postgres_store,
):
    first = push(push_id="source")
    postgres_store.store_push(first)
    with postgres_store.read_snapshot() as snapshot:
        rows = snapshot.read_repository_identities("acme", captured_through=T0, limit=1)
        assert len(rows) == 1
        assert rows[0].source_fact_id == "source"
        assert rows[0].repository_id == "12345"
        assert rows[0].captured_at == T0
        postgres_store.store_push(push(push_id="later", repository_id="67890"))
        assert (
            snapshot.read_repository_identities("acme", captured_through=T0, limit=1)
            == rows
        )
    with pytest.raises(core.OperationalReportLimitExceeded):
        postgres_store.read_repository_identities("acme", captured_through=T0, limit=1)
    postgres_store.quarantine_fact(
        "acme", core.FactTable.PUSHES, "source", reason="test"
    )
    rows = postgres_store.read_repository_identities(
        "acme", captured_through=T0, limit=1
    )
    assert [row.source_fact_id for row in rows] == ["later"]
    assert (
        postgres_store.read_repository_identities(
            "elsewhere", captured_through=T0, limit=1
        )
        == []
    )
    assert (
        postgres_store.read_repository_identities(
            "acme", captured_through=T0 - timedelta(microseconds=1), limit=1
        )
        == []
    )


def ci(**overrides):
    return core.CIOutcome(
        **{
            "org_id": "acme",
            "provider": "github_actions",
            "run_id": "run-1",
            "run_attempt": 1,
            "repo": "acme/old",
            "commit_sha": "a" * 40,
            "branch": "main",
            "result": "passed",
            "captured_at": T0,
            **IDENTITY,
            **overrides,
        }
    )


def revision(**overrides):
    return core.PullRequestRevision(
        **{
            "org_id": "acme",
            "provider": "github",
            "repo": "acme/old",
            "pr_number": 1,
            "head_repo": "fork/project",
            "head_ref": "topic",
            "head_sha": "a" * 40,
            "base_ref": "main",
            "base_sha": "b" * 40,
            "captured_at": T0,
            "head_repository_provider": "github",
            "head_repository_host": "github.com",
            "head_repository_id": "999",
            **IDENTITY,
            **overrides,
        }
    )


def test_ci_receipt_separates_hosts_and_refuses_conflicting_repository(postgres_store):
    first = ci(outcome_id="ci-first")
    assert postgres_store.store_ci_outcome(first)
    duplicate = postgres_store.store_ci_outcome_receipt(ci(repo="acme/new"))
    assert duplicate.fact == first and not duplicate.stored
    with pytest.raises(core.RepositoryIdentityConflict):
        postgres_store.store_ci_outcome(ci(repository_id="999"))
    assert postgres_store.store_ci_outcome(ci(repository_host="git.acme.test"))
    assert len(postgres_store.read_ci_outcomes("acme")) == 2


def test_pr_identity_keeps_independent_head_and_complete_population(postgres_store):
    first = revision(revision_id="pr-first")
    assert postgres_store.store_pull_request_revision(first)
    assert not postgres_store.store_pull_request_revision(revision(repo="acme/new"))
    with pytest.raises(core.RepositoryIdentityConflict):
        postgres_store.store_pull_request_revision(revision(head_repository_id="777"))
    assert postgres_store.store_pull_request_revision(revision(repository_id="777"))
    rows = postgres_store.read_repository_identities("acme", captured_through=T0)
    assert {(row.role, row.repository_id) for row in rows} == {
        ("repo", "12345"),
        ("repo", "777"),
        ("head_repo", "999"),
    }
    assert len(rows) == 4


def test_observation_source_identity_is_exact_and_never_crosses_lifetimes(
    postgres_store,
):
    postgres_store.store_push(push(push_id="source"))
    values = dict(
        org_id="acme",
        repo="acme/old",
        commit_sha="a" * 40,
        session_id="session",
        source_push_id="source",
        captured_at=T0,
        **IDENTITY,
    )
    fact = core.SessionCommitObservation(**values)
    assert postgres_store.store_session_commit_observation(fact)
    assert not postgres_store.store_session_commit_observation(
        core.SessionCommitObservation(**{**values, "repo": "acme/new"})
    )
    for change in (
        {"source_push_id": "absent"},
        {"repository_id": "999"},
        {"org_id": "foreign"},
    ):
        with pytest.raises(core.RepositoryIdentityConflict):
            postgres_store.store_session_commit_observation(
                core.SessionCommitObservation(**{**values, **change})
            )
    assert len(postgres_store.read_sessions("acme")) == 1
    assert postgres_store.read_sessions("foreign") == []


def test_concurrent_receipts_return_database_retained_identity(postgres_store):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(
            pool.map(
                lambda i: postgres_store.store_push_receipt(
                    push(push_id=f"candidate-{i}", repo=f"acme/name-{i}")
                ),
                range(8),
            )
        )
    assert sum(receipt.stored for receipt in receipts) == 1
    assert len({receipt.fact_id for receipt in receipts}) == 1
    assert len({receipt.fact.repo for receipt in receipts}) == 1


def test_primary_identity_conflict_does_not_disclose_foreign_fact(postgres_store):
    postgres_store.store_push(push(push_id="foreign-id", org_id="foreign"))
    with pytest.raises(core.RepositoryIdentityConflict) as caught:
        postgres_store.store_push_receipt(push(push_id="foreign-id"))
    assert "foreign" not in str(caught.value)
    assert postgres_store.read_pushes("acme") == []


def test_repository_projections_do_not_drop_lifetime_identity(postgres_store):
    postgres_store.store_push(push())
    postgres_store.store_ci_outcome(ci())
    pushes, outcomes = postgres_store.read_delivery_summaries(
        "acme", {("acme/old", "a" * 40)}
    )
    assert pushes[0].repository_id == outcomes[0].repository_id == "12345"
    gc_row = next(postgres_store.iter_push_gc_rows("acme"))
    assert gc_row.repository_id == "12345"
    # Projections read straight from a database row, not `model_validate`.
    # The enum contract only holds if the store coerces it explicitly.
    for projected in (pushes[0], outcomes[0], gc_row):
        assert projected.repository_provider is core.ForgeProvider.GITHUB
        assert projected.repository_provider.value == "github"
    ci_projections = postgres_store.read_ci_outcome_projections("acme")
    assert ci_projections[0].repository_provider is core.ForgeProvider.GITHUB


def test_observation_primary_key_cannot_acknowledge_another_session(postgres_store):
    postgres_store.store_push(push(push_id="source"))
    values = dict(
        observation_id="fixed-id",
        org_id="acme",
        repo="acme/old",
        commit_sha="a" * 40,
        session_id="first",
        source_push_id="source",
        captured_at=T0,
        **IDENTITY,
    )
    assert postgres_store.store_session_commit_observation(
        core.SessionCommitObservation(**values)
    )
    with pytest.raises(core.RepositoryIdentityConflict):
        postgres_store.store_session_commit_observation(
            core.SessionCommitObservation(**{**values, "session_id": "another"})
        )


def test_repository_rename_conflict_and_keyless_delivery_preserve_truth(postgres_store):
    values = dict(
        org_id="acme",
        old_repo="acme/old",
        new_repo="acme/new",
        captured_at=T0,
        source_event_id="delivery",
        **IDENTITY,
    )
    assert postgres_store.store_repository_rename(core.RepositoryRename(**values))
    with pytest.raises(core.RepositoryIdentityConflict):
        postgres_store.store_repository_rename(
            core.RepositoryRename(**{**values, "repository_id": "999"})
        )
    for _ in range(2):
        assert postgres_store.store_repository_rename(
            core.RepositoryRename(**{**values, "source_event_id": None})
        )
    assert len(postgres_store.read_repository_renames("acme")) == 3
    with pytest.raises(core.OperationalReportLimitExceeded):
        postgres_store.read_repository_renames("acme", limit=2)


def test_pr_merge_duplicate_uses_lifetime_and_retains_original_head(postgres_store):
    values = {
        **revision().model_dump(),
        "merge_id": "merged",
        "merged_at": T0,
        "merge_commit_sha": "c" * 40,
    }
    first = core.PullRequestMerge(**values)
    assert postgres_store.store_pull_request_merge(first)
    duplicate = postgres_store.store_pull_request_merge_receipt(
        core.PullRequestMerge(**{**values, "merge_id": "later", "repo": "acme/new"})
    )
    assert duplicate.fact == first and not duplicate.stored
    with pytest.raises(core.RepositoryIdentityConflict):
        postgres_store.store_pull_request_merge(
            core.PullRequestMerge(**{**values, "head_repository_id": "777"})
        )


def test_identity_migration_preserves_all_legacy_repository_facts(
    postgres_database_factory,
):
    import json
    from alembic import command
    from sqlalchemy import MetaData, Table, create_engine
    from sediment_core.postgres_migrations import _alembic_config, upgrade_database

    url = postgres_database_factory(migrated=False)
    engine = create_engine(url)
    absent = dict(repository_provider=None, repository_host=None, repository_id=None)
    prior_push = push(schema_version=1, push_id="source", **absent)
    prior_ci = ci(schema_version=1, **absent)
    prior_revision = revision(
        schema_version=1,
        head_repository_provider=None,
        head_repository_host=None,
        head_repository_id=None,
        **absent,
    )
    prior_merge = core.PullRequestMerge(
        **{**prior_revision.model_dump(), "merge_commit_sha": "c" * 40, "merged_at": T0}
    )
    prior_observation = core.SessionCommitObservation(
        schema_version=1,
        org_id="acme",
        repo="acme/old",
        commit_sha="a" * 40,
        session_id="session",
        source_push_id="source",
        captured_at=T0,
    )
    originals = dict(
        pushes=prior_push,
        ci_outcomes=prior_ci,
        pull_request_revisions=prior_revision,
        pull_request_merges=prior_merge,
        session_commit_observations=prior_observation,
    )
    try:
        with engine.connect() as connection:
            config = _alembic_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "0009_descriptive_text_encoding")
        old = MetaData()
        for name, fact in originals.items():
            table = Table(name, old, autoload_with=engine)
            values = {
                key: value
                for key, value in fact.model_dump(mode="python").items()
                if key in table.c
            }
            for key in (
                "raw",
                "clone_url",
                "workflow_name",
                "run_url",
                "provider_result",
                "error_type",
                "reason",
                "source_event_type",
                "source_spec_version",
            ):
                if key in values and values[key] is not None:
                    values[key] = json.dumps(values[key], ensure_ascii=True)
            with engine.begin() as connection:
                connection.execute(table.insert().values(**values))
        upgrade_database(url)
        upgrade_database(url)
        store = core.FactStore(engine)
        for name, original in originals.items():
            assert getattr(store, "read_" + name)("acme") == [original]
        population = store.read_repository_identities("acme", captured_through=T0)
        assert len(population) == 7
        assert all(item.repository_id is None for item in population)
    finally:
        engine.dispose()


def test_concurrent_competing_repository_claims_keep_one_ci_identity(postgres_store):
    from concurrent.futures import ThreadPoolExecutor

    def submit(identifier):
        try:
            return postgres_store.store_ci_outcome_receipt(ci(repository_id=identifier))
        except core.RepositoryIdentityConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(submit, ("101", "202")))
    assert sum(receipt is not None and receipt.stored for receipt in receipts) == 1
    assert receipts.count(None) == 1
    assert len(postgres_store.read_ci_outcomes("acme")) == 1

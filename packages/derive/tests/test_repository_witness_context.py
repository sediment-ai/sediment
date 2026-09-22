# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stored metadata witnesses preserve the complete repository resolver's answers."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert

from sediment_core import (
    CIOutcome,
    FactTable,
    PullRequestMerge,
    PullRequestRevision,
    Push,
    RepositoryReadAmbiguous,
    RepositoryRename,
    SessionCommitObservation,
)
from sediment_core.postgres_schema import session_commit_observations
from sediment_derive.repository_context import (
    read_repository_context,
    read_repository_witness_context,
)
from sediment_derive.repository_identity import (
    RepositoryIdentity,
    build_repository_context,
    repository_identity_evidence_of,
)

ORG = "acme"
T0 = datetime(2026, 9, 20, tzinfo=UTC)
IDENTITY = dict(
    repository_provider="github", repository_host="github.com", repository_id="101"
)
LEGACY = {field: None for field in IDENTITY}


def _push(identifier, **changes):
    return Push(
        **(
            dict(
                push_id=identifier,
                org_id=ORG,
                provider="github",
                repo="acme/original",
                clone_url="https://github.com/acme/original.git",
                ref=f"refs/heads/{identifier}",
                before_sha="0" * 40,
                after_sha="a" * 40,
                captured_at=T0,
                **IDENTITY,
            )
            | changes
        )
    )


def _observation(identifier, source, **changes):
    return SessionCommitObservation(
        **(
            dict(
                observation_id=identifier,
                org_id=ORG,
                repo="acme/original",
                commit_sha="a" * 40,
                session_id=identifier,
                source_push_id=source,
                captured_at=T0,
                **IDENTITY,
            )
            | changes
        )
    )


def _source_keys(facts):
    return {
        (row.source_table, row.source_fact_id, row.role)
        for row in map(repository_identity_evidence_of, facts)
    }


def _contexts(store, facts, as_of=T0):
    with store.read_snapshot() as snapshot:
        return (
            read_repository_context(snapshot, ORG, as_of=as_of),
            read_repository_witness_context(
                snapshot, ORG, as_of=as_of, source_keys=_source_keys(facts)
            ),
        )


def _metadata(context):
    return {key: context.observed_repo_slugs(key) for key in context.repository_keys()}


def _assert_parity(full, compact, sources):
    assert _metadata(compact) == _metadata(full)
    for fact in sources:
        assert compact.resolve_fact(fact) == full.resolve_fact(fact)
    names = {repo for names in _metadata(full).values() for repo in names}
    for name in names | {"acme/unknown"}:
        assert compact.resolve_reference(ORG, name) == full.resolve_reference(ORG, name)
        try:
            selected = full.select_repository(ORG, repo=name)
        except RepositoryReadAmbiguous:
            with pytest.raises(RepositoryReadAmbiguous):
                compact.select_repository(ORG, repo=name)
        else:
            assert compact.select_repository(ORG, repo=name) == selected
    for identifier in ("101", "202", "303", "999"):
        identity = RepositoryIdentity("github", "github.com", identifier)
        assert compact.select_repository(ORG, repository_identity=identity) == (
            full.select_repository(ORG, repository_identity=identity)
        )


def test_all_roles_renames_and_reused_slugs_preserve_metadata(postgres_store):
    store = postgres_store
    store.store_push(_push("first"))
    store.store_push(_push("second", repo="acme/second"))
    observed = _observation("observed", "second", repo="acme/inherited", **LEGACY)
    store.store_session_commit_observation(observed)
    requested = CIOutcome(
        outcome_id="z-requested",
        org_id=ORG,
        provider="github_actions",
        run_id="requested",
        repo="acme/second",
        commit_sha="a" * 40,
        branch="main",
        result="passed",
        captured_at=T0,
        **IDENTITY,
    )
    store.store_ci_outcome(requested)
    store.store_ci_outcome(
        requested.model_copy(update={"outcome_id": "a-copy", "run_id": "copy"})
    )
    pr = dict(
        org_id=ORG,
        provider="github",
        repo="acme/target",
        pr_number=1,
        head_repo="acme/aaa-head",
        head_repository_provider="github",
        head_repository_host="github.com",
        head_repository_id="101",
        head_ref="feature",
        head_sha="a" * 40,
        base_ref="main",
        base_sha="b" * 40,
        captured_at=T0,
        **(IDENTITY | {"repository_id": "202"}),
    )
    store.store_pull_request_revision(PullRequestRevision(revision_id="revision", **pr))
    store.store_pull_request_merge(
        PullRequestMerge(
            merge_id="merge",
            merge_commit_sha="c" * 40,
            merged_at=T0,
            **(pr | {"head_repo": "acme/merge-head"}),
        )
    )
    for identifier, old, new, repository_id in (
        ("first-edge", "acme/original", "acme/renamed", "101"),
        ("duplicate-edge", "acme/original", "acme/renamed", "101"),
        ("second-edge", "acme/renamed", "acme/last", "101"),
        ("reuse", "acme/second", "acme/other-lifetime", "303"),
    ):
        store.store_repository_rename(
            RepositoryRename(
                rename_id=identifier,
                org_id=ORG,
                old_repo=old,
                new_repo=new,
                captured_at=T0,
                **(IDENTITY | {"repository_id": repository_id}),
            )
        )
    full, compact = _contexts(store, [requested, observed])
    _assert_parity(full, compact, [requested, observed])
    key = compact.resolve_fact(requested).key
    assert compact.repo_for(key) == "acme/aaa-head"
    assert "acme/inherited" in compact.observed_repo_slugs(key)
    with pytest.raises(ValueError, match="not declared"):
        compact.select_repository(
            ORG, repo="acme/unknown", repository_identity=key.identity
        )
    with store.read_snapshot() as snapshot:
        evidence, renames = snapshot.read_repository_context_witnesses(
            ORG, captured_through=T0, source_keys=_source_keys([requested, observed])
        )
    shuffled = build_repository_context(
        reversed(evidence), reversed(renames), ORG, as_of=T0
    )
    _assert_parity(compact, shuffled, [requested, observed])


@pytest.mark.parametrize(
    "source_state", ["visible", "future", "quarantined", "released"]
)
def test_observation_source_visibility_preserves_claims_names_and_reasons(
    postgres_store, source_state
):
    store = postgres_store
    source = _push("source", repo="acme/source-name")
    if source_state == "future":
        source = source.model_copy(update={"captured_at": T0 + timedelta(seconds=1)})
    store.store_push(source)
    identified = _observation("identified", "source", repo="acme/claim")
    legacy = _observation("legacy", "source", repo="acme/inherited", **LEGACY)
    no_source = _observation("no-source", "absent", repo="acme/claim", **LEGACY)
    for row in (identified, legacy, no_source):
        store.store_session_commit_observation(row)
    if source_state in ("quarantined", "released"):
        store.quarantine_fact(ORG, FactTable.PUSHES, source.push_id, reason="test")
    if source_state == "released":
        store.release_fact(ORG, FactTable.PUSHES, source.push_id, reason="test")
    full, compact = _contexts(store, [identified, legacy, no_source])
    _assert_parity(full, compact, [identified, legacy, no_source])
    assert compact.resolve_fact(no_source).reason == "repository_identity_unresolved"
    assert compact.resolve_fact(identified).reason == (
        "repository_source_absent"
        if source_state in ("future", "quarantined")
        else None
    )
    if source_state == "future":
        later = source.captured_at
        _assert_parity(
            *_contexts(store, [identified, legacy], later), [identified, legacy]
        )


def test_observation_representatives_do_not_merge_invalid_and_valid_sources(
    postgres_store,
):
    store = postgres_store
    store.store_push(_push("identified"))
    store.store_push(_push("legacy", **LEGACY))
    variants = [
        _observation("a-missing", "absent", repo="acme/edge"),
        _observation("b-valid", "identified", repo="acme/edge"),
        _observation("c-invalid", "legacy", repo="acme/edge"),
        _observation("d-conflict", "identified", repo="acme/edge", repository_id="202"),
        _observation("e-legacy-conflict", "legacy", repo="acme/edge", **LEGACY),
        _observation("f-inherited", "identified", repo="acme/edge", **LEGACY),
    ]
    # Existing validated legacy/substrate rows may carry unusable source proofs.
    # Capture rejects identified contradictions; read-side resolver must still
    # preserve their claims and declined-source states instead of repairing them.
    with store._engine.begin() as connection:
        connection.execute(
            insert(session_commit_observations), [row.model_dump() for row in variants]
        )
    full, compact = _contexts(store, [])
    _assert_parity(full, compact, variants)
    expected = [
        "repository_source_absent",
        None,
        "repository_identity_unresolved",
        "repository_identity_conflict",
        "repository_identity_conflict",
        None,
    ]
    assert [compact.resolve_fact(row).reason for row in variants] == expected

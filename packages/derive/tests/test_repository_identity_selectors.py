# SPDX-License-Identifier: AGPL-3.0-or-later
"""Selectors choose a captured lifetime without reinterpreting legacy Facts."""

from datetime import UTC, datetime, timedelta
from dataclasses import replace
from itertools import permutations
from zoneinfo import ZoneInfo

import pytest
from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    OperationalReportLimitExceeded,
    RepositoryReadAmbiguous,
    RepositoryRename,
)
from sediment_derive.repository_identity import (
    IdentifiedRepositoryKey,
    LegacyRepositoryKey,
    RepositoryIdentity,
    build_repository_context,
    repository_identity_evidence_of,
    repository_read_key,
)
from sediment_derive import repository_context
from sediment_derive import repository_identity

ORG = "acme-corp"
T0 = datetime(2026, 9, 5, tzinfo=UTC)
IDENTITY = RepositoryIdentity("github", "github.com", "101")


def _fact(fact_id="outcome", **overrides):
    values = dict(
        outcome_id=fact_id,
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id=fact_id,
        repo="acme-corp/old",
        commit_sha="a" * 40,
        branch="main",
        result=CIResult.PASSED,
        workflow_name="CI",
        captured_at=T0,
        repository_provider="github",
        repository_host="github.com",
        repository_id="101",
    )
    values.update(overrides)
    return CIOutcome(**values)


def _context(*facts, renames=(), as_of=T0):
    return build_repository_context(
        [repository_identity_evidence_of(item) for item in facts],
        renames,
        ORG,
        as_of=as_of,
    )


def test_name_selector_does_not_qualify_legacy_fact_by_name():
    legacy = _fact(
        "legacy", repository_provider=None, repository_host=None, repository_id=None
    )
    identified = _fact()
    for facts in permutations((legacy, identified)):
        context = _context(*facts)
        assert context.select_repository(
            ORG, repo=" ACME-CORP/OLD "
        ) == IdentifiedRepositoryKey(ORG, IDENTITY)
        assert context.resolve_fact(legacy).reason == "repository_identity_unresolved"
        assert repository_read_key(
            context.select_repository(ORG, repo="acme-corp/old")
        ) == ("github", "github.com", "101")


def test_selector_ambiguity_is_order_independent_and_exact_identity_stays_available():
    for facts in permutations((_fact(), _fact("fork", repository_id="202"))):
        context = _context(*facts)
        with pytest.raises(RepositoryReadAmbiguous):
            context.select_repository(ORG, repo="acme-corp/old")
        assert context.select_repository(
            ORG, repository_identity=IDENTITY
        ) == IdentifiedRepositoryKey(ORG, IDENTITY)
        with pytest.raises(ValueError, match="not declared"):
            context.select_repository(
                ORG, repository_identity=IDENTITY, repo="acme-corp/unrelated"
            )
        assert (
            context.select_repository("foreign", repository_identity=IDENTITY) is None
        )
        assert (
            context.select_repository(
                ORG,
                repository_identity=RepositoryIdentity("github", "github.com", "303"),
            )
            is None
        )


def test_conflicting_source_cannot_authorize_a_selector():
    context = _context(_fact(), _fact(repository_id="202"))
    with pytest.raises(RepositoryReadAmbiguous):
        context.select_repository(ORG, repo="acme-corp/old")


def test_legacy_selector_and_rename_boundary_remain_distinct():
    rename = RepositoryRename(
        org_id=ORG,
        repository_provider="github",
        repository_host="github.com",
        repository_id="101",
        old_repo="acme-corp/old",
        new_repo="acme-corp/new",
        captured_at=T0 + timedelta(seconds=1),
    )
    before = _context(renames=(rename,))
    assert before.select_repository(ORG, repo="acme-corp/new") == LegacyRepositoryKey(
        ORG, "acme-corp/new"
    )
    assert (
        repository_read_key(before.select_repository(ORG, repo="acme-corp/new"))
        == "acme-corp/new"
    )
    after = _context(renames=(rename,), as_of=rename.captured_at)
    assert after.select_repository(
        ORG, repo="acme-corp/new"
    ) == IdentifiedRepositoryKey(ORG, IDENTITY)


def test_context_loader_bounds_rename_read_before_pure_resolution(
    postgres_store, monkeypatch
):
    store = postgres_store
    monkeypatch.setattr(
        repository_context, "REPOSITORY_IDENTITY_LIMIT", 2, raising=False
    )
    for index in range(3):
        store.store_repository_rename(
            RepositoryRename(
                rename_id=f"rename-{index}",
                org_id=ORG,
                repository_provider="github",
                repository_host="github.com",
                repository_id="101",
                old_repo=f"acme-corp/old{index}",
                new_repo=f"acme-corp/new{index}",
                captured_at=T0,
            )
        )
    with store.read_snapshot() as snapshot:
        with pytest.raises(OperationalReportLimitExceeded):
            repository_context.read_repository_context(snapshot, ORG, as_of=T0)


def _legacy(fact_id="legacy", **overrides):
    return _fact(
        fact_id,
        repository_provider=None,
        repository_host=None,
        repository_id=None,
        **overrides,
    )


def test_context_loader_counts_reused_store_projections_once(
    postgres_store, monkeypatch
):
    monkeypatch.setattr(repository_context, "REPOSITORY_IDENTITY_LIMIT", 2)
    monkeypatch.setattr(repository_identity, "REPOSITORY_IDENTITY_LIMIT", 2)
    facts = (_legacy("first"), _legacy("second"))
    for fact in facts:
        postgres_store.store_ci_outcome(fact)
    with postgres_store.read_snapshot() as snapshot:
        context = repository_context.read_repository_context(
            snapshot,
            ORG,
            supplemental_legacy_evidence=map(repository_identity_evidence_of, facts),
        )
    assert all(context.resolve_fact(fact).reason is None for fact in facts)


@pytest.mark.parametrize("changed_field", ["repo", "fold"])
def test_context_loader_keeps_conflicting_projection_copies(
    postgres_store, changed_field
):
    stamp = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"))
    fact = _legacy(captured_at=stamp)
    postgres_store.store_ci_outcome(fact)
    evidence = repository_identity_evidence_of(fact)
    changed = (
        replace(evidence, repo="acme-corp/different")
        if changed_field == "repo"
        else replace(evidence, captured_at=stamp.replace(fold=1))
    )
    with postgres_store.read_snapshot() as snapshot:
        context = repository_context.read_repository_context(
            snapshot, ORG, supplemental_legacy_evidence=(evidence, changed)
        )
    assert context.resolve_fact(fact).reason == "repository_identity_conflict"


def test_context_loader_refuses_oversized_duplicate_prefix(postgres_store, monkeypatch):
    monkeypatch.setattr(repository_context, "REPOSITORY_IDENTITY_LIMIT", 2)
    fact = _legacy()
    postgres_store.store_ci_outcome(fact)
    evidence = repository_identity_evidence_of(fact)
    with postgres_store.read_snapshot() as snapshot:
        with pytest.raises(OperationalReportLimitExceeded):
            repository_context.read_repository_context(
                snapshot,
                ORG,
                supplemental_legacy_evidence=iter(
                    (evidence, evidence, replace(evidence, repo="acme-corp/different"))
                ),
            )

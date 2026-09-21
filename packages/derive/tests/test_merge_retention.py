# SPDX-License-Identifier: AGPL-3.0-or-later
"""Merge-retention Derivation tests over real facts and git repositories."""

from datetime import UTC, datetime, timedelta
from contextlib import nullcontext
from pathlib import Path
from typing import get_type_hints

import pytest
from gitfixtures import CART, FIB, commit_all, make_remote, make_work_repo, run_git
from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    CommitSha,
    FactStore,
    ForgeProvider,
    NonEmptyId,
    OrgId,
    PullRequestMerge,
    PullRequestRevision,
    Push,
    RepoSlug,
    SessionCommitObservation,
)
from sediment_derive import (
    Attribution,
    AttributionSource,
    MergeMembershipOutcome,
    MergeRetention,
    MergeRetentionResult,
    MirrorManager,
    Provenance,
    RepoMirror,
    derive_merge_retention_result,
)

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
T0 = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


class _BoundaryStore:
    def read_repository_identities(self, org_id, **kwargs):
        return []

    def read_repository_renames(self, org_id, **kwargs):
        return []

    def read_snapshot(self):
        return nullcontext(self)

    def quarantine_revision(self, org_id: str) -> int:
        return 0

    def read_pull_request_merges(self, org_id: str):
        raise AssertionError("merge fallback read called")

    def read_pull_request_revisions(self, org_id: str):
        raise AssertionError("revision fallback read called")

    def read_ci_outcomes(self, org_id: str):
        raise AssertionError("CI outcome fallback read called")


def test_merge_retention_identity_fields_use_validated_domain_types() -> None:
    hints = get_type_hints(MergeRetention, include_extras=True)

    assert hints["org_id"] == OrgId
    assert hints["repo"] == RepoSlug
    assert hints["merge_id"] == NonEmptyId
    assert hints["inference_call_id"] == NonEmptyId
    assert hints["session_id"] == NonEmptyId
    assert hints["source_commit_sha"] == CommitSha
    assert hints["head_commit_sha"] == CommitSha
    assert hints["merge_commit_sha"] == CommitSha

    membership_hints = get_type_hints(MergeMembershipOutcome, include_extras=True)
    assert membership_hints["org_id"] == OrgId
    assert membership_hints["repo"] == RepoSlug
    assert membership_hints["inference_call_id"] == NonEmptyId
    assert membership_hints["session_id"] == NonEmptyId
    assert membership_hints["source_commit_sha"] == CommitSha
    assert membership_hints["merge_id"] == NonEmptyId | None


def _attribution(
    source_sha: str,
    *,
    file_path: str = "math_utils.py",
    suffix: str = "boundary",
) -> Attribution:
    return Attribution(
        org_id=ORG,
        repo=REPO,
        commit_sha=source_sha,
        file_path=file_path,
        inference_call_id=f"inference-{suffix}",
        session_id=f"session-{suffix}",
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )


def _observations(attributions):
    return [
        SessionCommitObservation(
            observation_id=f"observation/{item.commit_sha}/{item.session_id}",
            org_id=item.org_id,
            repo=item.repo,
            commit_sha=item.commit_sha,
            session_id=item.session_id,
            source_push_id="push",
            captured_at=T0,
        )
        for item in {
            (item.repo, item.commit_sha, item.session_id): item for item in attributions
        }.values()
    ]


def test_preloaded_merge_evidence_is_authoritative(tmp_path: Path) -> None:
    result = derive_merge_retention_result(
        _BoundaryStore(),
        MirrorManager(str(tmp_path / "mirrors")),
        ORG,
        attributions=[_attribution("a" * 40)],
        merges=[],
        revisions=[],
        ci_outcomes=[],
        session_commit_observations=_observations([_attribution("a" * 40)]),
    )

    assert result.attributed_candidates == 1
    assert result.membership == {"attribution_without_merge": 1}


def test_shuffled_preloaded_inputs_produce_identical_membership(
    tmp_path: Path,
) -> None:
    attributions = [
        _attribution("b" * 40, suffix="second"),
        _attribution("a" * 40, suffix="first"),
    ]
    kwargs = {"merges": [], "revisions": [], "ci_outcomes": []}

    first = derive_merge_retention_result(
        _BoundaryStore(),
        MirrorManager(str(tmp_path / "first")),
        ORG,
        attributions=attributions,
        **kwargs,
        session_commit_observations=_observations(attributions),
    )
    second = derive_merge_retention_result(
        _BoundaryStore(),
        MirrorManager(str(tmp_path / "second")),
        ORG,
        attributions=list(reversed(attributions)),
        **kwargs,
        session_commit_observations=_observations(list(reversed(attributions))),
    )

    assert first == second


def _derive_boundaries(
    tmp_path: Path,
    store: FactStore,
    work: Path,
    *,
    base_sha: str,
    source_sha: str,
    head_sha: str,
    merge_sha: str,
    file_path: str = "math_utils.py",
) -> MergeRetentionResult:
    remote = make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base_sha,
        after_sha=merge_sha,
        captured_at=T0,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    store.store_push(push)
    store.store_pull_request_merge(
        PullRequestMerge(
            merge_id="merge-boundary",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            pr_number=41,
            head_repo=REPO,
            head_ref="feature/query",
            head_sha=head_sha,
            base_ref="main",
            base_sha=base_sha,
            merge_commit_sha=merge_sha,
            merged_at=T0,
            captured_at=T0,
        )
    )
    return derive_merge_retention_result(
        store,
        mirrors,
        ORG,
        attributions=[_attribution(source_sha, file_path=file_path)],
        session_commit_observations=_observations(
            [_attribution(source_sha, file_path=file_path)]
        ),
    )


def test_merge_retention_scores_source_text_at_head_and_merge(
    tmp_path: Path, postgres_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    base = commit_all(work, "base")
    (work / "math_utils.py").write_text(FIB)
    (work / "cart.py").write_text(CART)
    source = commit_all(work, "add fibonacci")
    (work / "README.md").write_text("reviewed\n")
    head = commit_all(work, "address review")
    (work / "README.md").write_text("reviewed and merged\n")
    merged = commit_all(work, "merge boundary")
    remote = make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=merged,
        captured_at=T0,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    postgres_store.store_push(push)
    merge = PullRequestMerge(
        merge_id="merge-41",
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        pr_number=41,
        head_repo=REPO,
        head_ref="feature/query",
        head_sha=head,
        base_ref="main",
        base_sha=base,
        merge_commit_sha=merged,
        merged_at=T0,
        captured_at=T0,
    )
    postgres_store.store_pull_request_merge(merge)
    ancestry_calls = 0
    original_is_ancestor = RepoMirror.is_ancestor

    def count_is_ancestor(
        repo_mirror: RepoMirror, ancestor: str, descendant: str
    ) -> bool:
        nonlocal ancestry_calls
        ancestry_calls += 1
        return original_is_ancestor(repo_mirror, ancestor, descendant)

    monkeypatch.setattr(RepoMirror, "is_ancestor", count_is_ancestor)
    attribution = Attribution(
        org_id=ORG,
        repo=REPO,
        commit_sha=source,
        file_path="math_utils.py",
        inference_call_id="inference-1",
        session_id="session-1",
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )
    cart_attribution = Attribution(
        org_id=ORG,
        repo=REPO,
        commit_sha=source,
        file_path="cart.py",
        inference_call_id="inference-2",
        session_id="session-2",
        similarity_score=1.0,
        attribution_source=AttributionSource.JACCARD,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )

    result = derive_merge_retention_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=[attribution, cart_attribution],
        session_commit_observations=_observations([attribution, cart_attribution]),
    )

    assert result.attributed_candidates == 2
    assert result.joined_candidates == 2
    assert result.membership == {}
    assert result.skipped == {}
    row = next(row for row in result.rows if row.source_file_path == "math_utils.py")
    assert row.pr_number == 41
    assert row.merge_id == "merge-41"
    assert row.source_commit_sha == source
    assert row.source_file_path == "math_utils.py"
    assert row.head_commit_sha == head
    assert row.merge_commit_sha == merged
    assert row.head_retention_score == 1.0
    assert row.merge_retention_score == 1.0
    assert row.inference_call_id == "inference-1"
    assert row.session_id == "session-1"
    assert result.provenance == Provenance(policy_version="3", quarantine_revision=0)
    assert (
        derive_merge_retention_result(
            postgres_store,
            mirrors,
            ORG,
            attributions=[cart_attribution, attribution],
            session_commit_observations=_observations([cart_attribution, attribution]),
        )
        == result
    )
    assert ancestry_calls == 4


def test_merge_retention_uses_exact_ci_membership_when_ancestry_does_not_speak(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text("")
    base = commit_all(work, "scaffold")
    run_git(work, "checkout", "-q", "-b", "feature")
    (work / "math_utils.py").write_text(FIB)
    source = commit_all(work, "feature implementation")
    run_git(work, "checkout", "-q", "main")
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "independent integration")
    remote = make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=head,
        captured_at=T0,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    postgres_store.store_push(push)
    postgres_store.store_pull_request_merge(
        PullRequestMerge(
            merge_id="merge-ci",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            pr_number=41,
            head_repo=REPO,
            head_ref="feature",
            head_sha=head,
            base_ref="main",
            base_sha=base,
            merge_commit_sha=head,
            merged_at=T0,
            captured_at=T0,
        )
    )
    postgres_store.store_ci_outcome(
        CIOutcome(
            outcome_id="ci-41",
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id="run-41",
            repo=REPO,
            commit_sha=source,
            branch="feature",
            result=CIResult.PASSED,
            workflow_name="CI",
            pr_number=41,
            captured_at=T0,
        )
    )
    attribution = Attribution(
        org_id=ORG,
        repo=REPO,
        commit_sha=source,
        file_path="math_utils.py",
        inference_call_id="inference-ci",
        session_id="session-ci",
        similarity_score=1.0,
        attribution_source=AttributionSource.JACCARD,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )

    result = derive_merge_retention_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=[attribution],
        session_commit_observations=_observations([attribution]),
    )

    assert result.joined_candidates == 1
    assert result.membership == {}
    assert len(result.rows) == 1
    assert result.rows[0].merge_retention_score == 1.0


def test_merge_retention_joins_merge_reported_after_the_as_of_boundary(
    tmp_path: Path, postgres_store
) -> None:
    """A merge with a ``captured_at`` on time and a ``merged_at`` after
    ``as_of`` still joins. Eligibility is a captured-boundary and
    identity-resolution concern, not a policy filter on the merge's own
    reported timestamp."""
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text("")
    base = commit_all(work, "scaffold")
    run_git(work, "checkout", "-q", "-b", "feature")
    (work / "math_utils.py").write_text(FIB)
    source = commit_all(work, "feature implementation")
    run_git(work, "checkout", "-q", "main")
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "independent integration")
    remote = make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=head,
        captured_at=T0,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    postgres_store.store_push(push)
    postgres_store.store_pull_request_merge(
        PullRequestMerge(
            merge_id="merge-late-report",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            pr_number=42,
            head_repo=REPO,
            head_ref="feature",
            head_sha=head,
            base_ref="main",
            base_sha=base,
            merge_commit_sha=head,
            # Reported merged far after the boundary; captured on time.
            merged_at=T0 + timedelta(days=30),
            captured_at=T0,
        )
    )
    postgres_store.store_ci_outcome(
        CIOutcome(
            outcome_id="ci-42",
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id="run-42",
            repo=REPO,
            commit_sha=source,
            branch="feature",
            result=CIResult.PASSED,
            workflow_name="CI",
            pr_number=42,
            captured_at=T0,
        )
    )
    attribution = Attribution(
        org_id=ORG,
        repo=REPO,
        commit_sha=source,
        file_path="math_utils.py",
        inference_call_id="inference-late-report",
        session_id="session-late-report",
        similarity_score=1.0,
        attribution_source=AttributionSource.JACCARD,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )

    result = derive_merge_retention_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=[attribution],
        session_commit_observations=_observations([attribution]),
        as_of=T0,
    )

    assert result.joined_candidates == 1
    assert result.membership == {}
    assert len(result.rows) == 1


def test_merge_retention_uses_earlier_revision_after_rebase(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    base = commit_all(work, "base")
    run_git(work, "checkout", "-q", "-b", "original-head")
    (work / "math_utils.py").write_text(FIB)
    source = commit_all(work, "original implementation")
    run_git(work, "checkout", "-q", "main")
    (work / "README.md").write_text("updated base\n")
    rebased_base = commit_all(work, "advance base")
    run_git(work, "checkout", "-q", "-b", "rebased-head")
    (work / "math_utils.py").write_text(FIB)
    final_head = commit_all(work, "rebased implementation")
    remote = make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=final_head,
        captured_at=T0,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    postgres_store.store_push(push)
    postgres_store.store_pull_request_revision(
        PullRequestRevision(
            revision_id="revision-original",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            pr_number=41,
            head_repo=REPO,
            head_ref="feature/query",
            head_sha=source,
            base_ref="main",
            base_sha=base,
            captured_at=T0,
        )
    )
    postgres_store.store_pull_request_merge(
        PullRequestMerge(
            merge_id="merge-rebased",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            pr_number=41,
            head_repo=REPO,
            head_ref="feature/query",
            head_sha=final_head,
            base_ref="main",
            base_sha=rebased_base,
            merge_commit_sha=final_head,
            merged_at=T0,
            captured_at=T0,
        )
    )

    result = derive_merge_retention_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=[_attribution(source, suffix="earlier-revision")],
        session_commit_observations=_observations(
            [_attribution(source, suffix="earlier-revision")]
        ),
    )

    assert result.membership == {}
    assert result.joined_candidates == 1
    assert len(result.rows) == 1
    assert result.rows[0].pr_number == 41
    assert result.rows[0].head_commit_sha == final_head
    assert result.rows[0].merge_commit_sha == final_head
    assert result.rows[0].head_retention_score == 1.0
    assert result.rows[0].merge_retention_score == 1.0


def test_merge_retention_uses_previous_head_when_opened_event_is_absent(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    base = commit_all(work, "base")
    run_git(work, "checkout", "-q", "-b", "original-head")
    (work / "math_utils.py").write_text(FIB)
    source = commit_all(work, "original implementation")
    run_git(work, "checkout", "-q", "main")
    (work / "README.md").write_text("updated base\n")
    rebased_base = commit_all(work, "advance base")
    run_git(work, "checkout", "-q", "-b", "rebased-head")
    (work / "math_utils.py").write_text(FIB)
    final_head = commit_all(work, "rebased implementation")
    remote = make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=final_head,
        captured_at=T0,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors-previous"))
    mirrors.ensure(push)
    postgres_store.store_push(push)
    postgres_store.store_pull_request_revision(
        PullRequestRevision(
            revision_id="revision-sync",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            pr_number=41,
            head_repo=REPO,
            head_ref="feature/query",
            head_sha=final_head,
            base_ref="main",
            base_sha=rebased_base,
            previous_head_sha=source,
            captured_at=T0,
        )
    )
    postgres_store.store_pull_request_merge(
        PullRequestMerge(
            merge_id="merge-rebased-previous",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            pr_number=41,
            head_repo=REPO,
            head_ref="feature/query",
            head_sha=final_head,
            base_ref="main",
            base_sha=rebased_base,
            merge_commit_sha=final_head,
            merged_at=T0,
            captured_at=T0,
        )
    )

    result = derive_merge_retention_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=[_attribution(source, suffix="previous-head")],
        session_commit_observations=_observations(
            [_attribution(source, suffix="previous-head")]
        ),
    )

    assert result.membership == {}
    assert result.joined_candidates == 1
    assert len(result.rows) == 1
    assert result.rows[0].pr_number == 41


def test_merge_retention_drops_join_when_another_pull_request_head_is_unreachable(
    tmp_path: Path, postgres_store
) -> None:
    """An unreadable captured head leaves pull-request membership unresolved."""
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    base = commit_all(work, "base")
    (work / "math_utils.py").write_text(FIB)
    source = commit_all(work, "add fibonacci")
    (work / "cart.py").write_text(CART)
    later = commit_all(work, "add cart")
    remote = make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=later,
        captured_at=T0,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors-unreachable"))
    mirrors.ensure(push)
    postgres_store.store_push(push)
    for pr_number, merge_id, head_sha, base_sha in (
        (41, "merge-source", source, base),
        (42, "merge-later", later, source),
    ):
        postgres_store.store_pull_request_merge(
            PullRequestMerge(
                merge_id=merge_id,
                org_id=ORG,
                provider=ForgeProvider.GITHUB,
                repo=REPO,
                pr_number=pr_number,
                head_repo=REPO,
                head_ref=f"feature/{pr_number}",
                head_sha=head_sha,
                base_ref="main",
                base_sha=base_sha,
                merge_commit_sha=head_sha,
                merged_at=T0,
                captured_at=T0,
            )
        )
    postgres_store.store_pull_request_revision(
        PullRequestRevision(
            revision_id="revision-force-pushed-away",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            pr_number=42,
            head_repo=REPO,
            head_ref="feature/42",
            head_sha=later,
            base_ref="main",
            base_sha=source,
            previous_head_sha="0" * 39 + "1",
            captured_at=T0,
        )
    )

    result = derive_merge_retention_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=[_attribution(source, suffix="unreachable-sibling")],
        session_commit_observations=_observations(
            [_attribution(source, suffix="unreachable-sibling")]
        ),
    )

    assert result.membership == {"ancestry_check_failed": 1}
    assert [outcome.status for outcome in result.membership_outcomes] == [
        "ancestry_unresolved"
    ]
    assert result.joined_candidates == 0
    assert result.rows == []


def test_merge_retention_drops_ambiguous_pull_request_membership(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    base = commit_all(work, "base")
    (work / "math_utils.py").write_text(FIB)
    source = commit_all(work, "add fibonacci")
    (work / "README.md").write_text("later\n")
    later = commit_all(work, "later work")
    remote = make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=later,
        captured_at=T0,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    for number, head in ((41, source), (42, later)):
        postgres_store.store_pull_request_merge(
            PullRequestMerge(
                merge_id=f"merge-{number}",
                org_id=ORG,
                provider=ForgeProvider.GITHUB,
                repo=REPO,
                pr_number=number,
                head_repo=REPO,
                head_ref=f"feature-{number}",
                head_sha=head,
                base_ref="main",
                base_sha=base,
                merge_commit_sha=head,
                merged_at=T0,
                captured_at=T0,
            )
        )
    attribution = Attribution(
        org_id=ORG,
        repo=REPO,
        commit_sha=source,
        file_path="math_utils.py",
        inference_call_id="inference-ambiguous",
        session_id="session-ambiguous",
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )

    result = derive_merge_retention_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=[attribution],
        session_commit_observations=_observations([attribution]),
    )

    assert result.rows == []
    assert result.joined_candidates == 0
    assert result.membership == {"ambiguous_pr_membership": 1}
    assert [outcome.status for outcome in result.membership_outcomes] == ["ambiguous"]
    assert result.membership_outcomes[0].pr_number is None
    assert result.membership_outcomes[0].merge_id is None


def test_merge_retention_excludes_source_commit_already_in_later_pr_base(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    base = commit_all(work, "base")
    run_git(work, "checkout", "-q", "-b", "feature-one")
    (work / "math_utils.py").write_text(FIB)
    source = commit_all(work, "add fibonacci")
    run_git(work, "checkout", "-q", "main")
    run_git(work, "merge", "--no-ff", "feature-one", "-m", "merge feature one")
    first_merge = run_git(work, "rev-parse", "HEAD").strip()
    run_git(work, "checkout", "-q", "-b", "feature-two")
    (work / "cart.py").write_text(CART)
    second_head = commit_all(work, "add cart")
    run_git(work, "checkout", "-q", "main")
    run_git(work, "merge", "--no-ff", "feature-two", "-m", "merge feature two")
    second_merge = run_git(work, "rev-parse", "HEAD").strip()
    remote = make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=second_merge,
        captured_at=T0,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    postgres_store.store_push(push)
    for merge in (
        PullRequestMerge(
            merge_id="merge-41",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            pr_number=41,
            head_repo=REPO,
            head_ref="feature-one",
            head_sha=source,
            base_ref="main",
            base_sha=base,
            merge_commit_sha=first_merge,
            merged_at=T0,
            captured_at=T0,
        ),
        PullRequestMerge(
            merge_id="merge-42",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            pr_number=42,
            head_repo=REPO,
            head_ref="feature-two",
            head_sha=second_head,
            base_ref="main",
            base_sha=first_merge,
            merge_commit_sha=second_merge,
            merged_at=T0,
            captured_at=T0,
        ),
    ):
        postgres_store.store_pull_request_merge(merge)
    attribution = Attribution(
        org_id=ORG,
        repo=REPO,
        commit_sha=source,
        file_path="math_utils.py",
        inference_call_id="inference-sequential",
        session_id="session-sequential",
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )

    result = derive_merge_retention_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=[attribution],
        session_commit_observations=_observations([attribution]),
    )

    assert result.membership == {}
    assert result.joined_candidates == 1
    assert [row.pr_number for row in result.rows] == [41]
    assert result.rows[0].merge_commit_sha == first_merge
    assert result.rows[0].merge_retention_score == 1.0


def test_merge_retention_drops_ci_candidate_when_other_membership_is_unresolved(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    source = commit_all(work, "add fibonacci")
    remote = make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=source,
        captured_at=T0,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    postgres_store.store_push(push)
    for number, head_sha in ((41, source), (42, "f" * 40)):
        postgres_store.store_pull_request_merge(
            PullRequestMerge(
                merge_id=f"merge-{number}",
                org_id=ORG,
                provider=ForgeProvider.GITHUB,
                repo=REPO,
                pr_number=number,
                head_repo=REPO,
                head_ref=f"feature-{number}",
                head_sha=head_sha,
                base_ref="main",
                base_sha=source,
                merge_commit_sha=head_sha,
                merged_at=T0,
                captured_at=T0,
            )
        )
    postgres_store.store_ci_outcome(
        CIOutcome(
            outcome_id="ci-41",
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id="run-41",
            repo=REPO,
            commit_sha=source,
            branch="feature-41",
            result=CIResult.PASSED,
            workflow_name="CI",
            pr_number=41,
            captured_at=T0,
        )
    )
    attribution = Attribution(
        org_id=ORG,
        repo=REPO,
        commit_sha=source,
        file_path="math_utils.py",
        inference_call_id="inference-unresolved",
        session_id="session-unresolved",
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )

    result = derive_merge_retention_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=[attribution],
        session_commit_observations=_observations([attribution]),
    )

    assert result.rows == []
    assert result.joined_candidates == 0
    assert result.membership == {"ancestry_check_failed": 1}
    assert [outcome.status for outcome in result.membership_outcomes] == [
        "ancestry_unresolved"
    ]


def test_merge_retention_scores_squash_merge_copy(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    base = commit_all(work, "base")
    run_git(work, "checkout", "-q", "-b", "feature")
    (work / "math_utils.py").write_text(FIB)
    source = commit_all(work, "add fibonacci")
    run_git(work, "checkout", "-q", "main")
    run_git(work, "merge", "--squash", "feature")
    merged = commit_all(work, "squash feature")

    result = _derive_boundaries(
        tmp_path,
        postgres_store,
        work,
        base_sha=base,
        source_sha=source,
        head_sha=source,
        merge_sha=merged,
    )

    assert result.skipped == {}
    assert len(result.rows) == 1
    assert result.rows[0].head_retention_score == 1.0
    assert result.rows[0].merge_retention_score == 1.0


def test_merge_retention_scores_rebased_copy(tmp_path: Path, postgres_store) -> None:
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    original_base = commit_all(work, "base")
    run_git(work, "checkout", "-q", "-b", "feature")
    (work / "math_utils.py").write_text(FIB)
    source = commit_all(work, "add fibonacci")
    run_git(work, "checkout", "-q", "main")
    (work / "base.txt").write_text("base advanced\n")
    merge_base = commit_all(work, "advance base")
    run_git(work, "cherry-pick", source)
    merged = run_git(work, "rev-parse", "HEAD").strip()

    result = _derive_boundaries(
        tmp_path,
        postgres_store,
        work,
        base_sha=merge_base,
        source_sha=source,
        head_sha=source,
        merge_sha=merged,
    )

    assert original_base != merge_base
    assert not result.membership
    assert len(result.rows) == 1
    assert result.rows[0].merge_retention_score == 1.0


def test_merge_retention_scores_later_refactor_below_one(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    base = commit_all(work, "base")
    (work / "math_utils.py").write_text(FIB)
    source = commit_all(work, "add fibonacci")
    (work / "math_utils.py").write_text(
        "def fibonacci(n: int) -> int:\n"
        "    values = [0, 1]\n"
        "    for _ in range(2, n + 1):\n"
        "        values.append(values[-1] + values[-2])\n"
        "    return values[n]\n"
    )
    head = commit_all(work, "refactor fibonacci")

    result = _derive_boundaries(
        tmp_path,
        postgres_store,
        work,
        base_sha=base,
        source_sha=source,
        head_sha=head,
        merge_sha=head,
    )

    assert len(result.rows) == 1
    assert 0.0 < result.rows[0].head_retention_score < 1.0
    assert result.rows[0].merge_retention_score == result.rows[0].head_retention_score


def test_merge_retention_counts_deleted_source_file(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    base = commit_all(work, "base")
    (work / "math_utils.py").write_text(FIB)
    source = commit_all(work, "add fibonacci")
    (work / "math_utils.py").unlink()
    head = commit_all(work, "delete fibonacci")

    result = _derive_boundaries(
        tmp_path,
        postgres_store,
        work,
        base_sha=base,
        source_sha=source,
        head_sha=head,
        merge_sha=head,
    )

    assert result.rows == []
    assert result.joined_candidates == 1
    assert result.skipped == {"head_path_unresolved": 1}


def test_merge_retention_follows_unique_rename(tmp_path: Path, postgres_store) -> None:
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    base = commit_all(work, "base")
    (work / "old.py").write_text(FIB)
    source = commit_all(work, "add fibonacci")
    (work / "old.py").rename(work / "new.py")
    head = commit_all(work, "rename fibonacci")

    result = _derive_boundaries(
        tmp_path,
        postgres_store,
        work,
        base_sha=base,
        source_sha=source,
        head_sha=head,
        merge_sha=head,
        file_path="old.py",
    )

    assert result.skipped == {}
    assert len(result.rows) == 1
    assert result.rows[0].head_file_path == "new.py"
    assert result.rows[0].merge_file_path == "new.py"


def test_merge_retention_counts_missing_mirror_after_exact_ci_membership(
    tmp_path: Path, postgres_store
) -> None:
    source = "a" * 40
    postgres_store.store_pull_request_merge(
        PullRequestMerge(
            merge_id="merge-missing-mirror",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            pr_number=41,
            head_repo=REPO,
            head_ref="feature",
            head_sha="b" * 40,
            base_ref="main",
            base_sha="c" * 40,
            merge_commit_sha="d" * 40,
            merged_at=T0,
            captured_at=T0,
        )
    )
    postgres_store.store_ci_outcome(
        CIOutcome(
            outcome_id="ci-missing-mirror",
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id="run-missing-mirror",
            repo=REPO,
            commit_sha=source,
            branch="feature",
            result=CIResult.PASSED,
            workflow_name="CI",
            pr_number=41,
            captured_at=T0,
        )
    )

    result = derive_merge_retention_result(
        postgres_store,
        MirrorManager(str(tmp_path / "missing-mirrors")),
        ORG,
        attributions=[_attribution(source, suffix="missing-mirror")],
        session_commit_observations=_observations(
            [_attribution(source, suffix="missing-mirror")]
        ),
    )

    assert result.rows == []
    assert result.joined_candidates == 1
    assert result.skipped == {"mirror_absent": 1}
    assert len(result.membership_outcomes) == 1
    outcome = result.membership_outcomes[0]
    assert outcome.status == "joined"
    assert outcome.pr_number == 41
    assert outcome.merge_id == "merge-missing-mirror"
    assert outcome.source_commit_sha == source
    assert outcome.source_file_path == "math_utils.py"


def test_merge_retention_preserves_outcome_without_pull_request_membership(
    tmp_path: Path, postgres_store
) -> None:
    source = "a" * 40

    result = derive_merge_retention_result(
        postgres_store,
        MirrorManager(str(tmp_path / "missing-mirrors")),
        ORG,
        attributions=[_attribution(source, suffix="without-merge")],
        session_commit_observations=_observations(
            [_attribution(source, suffix="without-merge")]
        ),
    )

    assert result.rows == []
    assert result.membership == {"attribution_without_merge": 1}
    assert len(result.membership_outcomes) == result.attributed_candidates == 1
    outcome = result.membership_outcomes[0]
    assert outcome.status == "without_merge"
    assert outcome.pr_number is None
    assert outcome.merge_id is None


def test_merge_retention_is_independent_of_fact_insertion_order(
    tmp_path: Path, postgres_store_factory
) -> None:
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    base = commit_all(work, "base")
    (work / "math_utils.py").write_text(FIB)
    (work / "cart.py").write_text(CART)
    source = commit_all(work, "add fibonacci")
    (work / "README.md").write_text("reviewed\n")
    final_head = commit_all(work, "address review")
    remote = make_remote(tmp_path, work)
    push = Push(
        push_id="push-order",
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=final_head,
        captured_at=T0,
    )
    merge = PullRequestMerge(
        merge_id="merge-order",
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        pr_number=41,
        head_repo=REPO,
        head_ref="feature",
        head_sha=final_head,
        base_ref="main",
        base_sha=base,
        merge_commit_sha=final_head,
        merged_at=T0,
        captured_at=T0,
    )
    ci = CIOutcome(
        outcome_id="ci-order",
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run-order",
        repo=REPO,
        commit_sha=source,
        branch="feature",
        result=CIResult.PASSED,
        workflow_name="CI",
        pr_number=41,
        captured_at=T0,
    )
    first_revision = PullRequestRevision(
        revision_id="revision-order-first",
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        pr_number=41,
        head_repo=REPO,
        head_ref="feature",
        head_sha=source,
        base_ref="main",
        base_sha=base,
        captured_at=T0,
    )
    second_revision = PullRequestRevision(
        revision_id="revision-order-second",
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        pr_number=41,
        head_repo=REPO,
        head_ref="feature",
        head_sha=final_head,
        base_ref="main",
        base_sha=base,
        previous_head_sha=source,
        captured_at=T0,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    _, first = postgres_store_factory()
    _, second = postgres_store_factory()
    first.store_push(push)
    first.store_pull_request_merge(merge)
    first.store_pull_request_revision(first_revision)
    first.store_pull_request_revision(second_revision)
    first.store_ci_outcome(ci)
    second.store_ci_outcome(ci)
    second.store_pull_request_revision(second_revision)
    second.store_pull_request_revision(first_revision)
    second.store_pull_request_merge(merge)
    second.store_push(push)

    attributions = [
        _attribution(source, suffix="order-fib"),
        _attribution(source, file_path="cart.py", suffix="order-cart"),
    ]
    first_result = derive_merge_retention_result(
        first,
        mirrors,
        ORG,
        attributions=attributions,
        session_commit_observations=_observations(attributions),
    )
    second_result = derive_merge_retention_result(
        second,
        mirrors,
        ORG,
        attributions=list(reversed(attributions)),
        session_commit_observations=_observations(list(reversed(attributions))),
    )

    assert first_result == second_result


def test_unobserved_candidate_is_coverage_gap_before_merge_interpretation(
    tmp_path: Path, postgres_store
) -> None:
    result = derive_merge_retention_result(
        postgres_store,
        MirrorManager(str(tmp_path / "mirrors")),
        ORG,
        attributions=[
            _attribution("a" * 40),
            _attribution("a" * 40, file_path="other.py"),
        ],
        merges=[],
        revisions=[],
        ci_outcomes=[],
        session_commit_observations=[],
    )
    assert result.membership_outcomes == []
    assert result.rows == []
    assert result.skipped["session_commit_unobserved"] == 1
    assert result.membership == {}


def test_merge_retention_uses_exact_quoted_additions_and_counts_each_section_once(
    tmp_path: Path, postgres_store_factory
) -> None:
    _, postgres_store = postgres_store_factory()
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    base = commit_all(work, "base")
    paths = ['quoted"file.js', "space file.js"]
    for path in paths:
        (work / path).write_text("++counter;\n" * 4)
    (work / "binary.js").write_bytes(b"binary\x00text")
    source = commit_all(work, "authored counters")
    initial = _derive_boundaries(
        tmp_path,
        postgres_store,
        work,
        base_sha=base,
        source_sha=source,
        head_sha=source,
        merge_sha=source,
        file_path=paths[0],
    )
    assert len(initial.rows) == 1
    assert initial.skipped == {"unsupported_diff_section": 1}
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    attributions = [
        _attribution(source, file_path=path, suffix=str(index))
        for index, path in enumerate(paths)
    ]
    for observation in _observations(attributions):
        postgres_store.store_session_commit_observation(observation)
    result = derive_merge_retention_result(
        postgres_store, mirrors, ORG, attributions=attributions
    )
    assert len(result.rows) == 2
    assert result.skipped == {"unsupported_diff_section": 1}
    assert all(
        row.head_retention_score == row.merge_retention_score == 1.0
        for row in result.rows
    )
    assert result.provenance.policy_version == "3"
    assert (
        derive_merge_retention_result(
            postgres_store, mirrors, ORG, attributions=attributions
        )
        == result
    )
    assert (
        derive_merge_retention_result(
            postgres_store, mirrors, ORG, attributions=list(reversed(attributions))
        )
        == result
    )

    _, shuffled_store = postgres_store_factory()
    for merge in postgres_store.read_pull_request_merges(ORG):
        shuffled_store.store_pull_request_merge(merge)
    for push in postgres_store.read_pushes(ORG):
        shuffled_store.store_push(push)
    for observation in reversed(postgres_store.read_session_commit_observations(ORG)):
        shuffled_store.store_session_commit_observation(observation)
    assert (
        derive_merge_retention_result(
            shuffled_store, mirrors, ORG, attributions=list(reversed(attributions))
        )
        == result
    )


def test_preloaded_joined_pull_requests_count_qualified_repo_prs(tmp_path):
    """Preloaded merge boundaries can exceed the store's one-PR UNIQUE key."""
    from dataclasses import replace
    from sediment_core import ForgeProvider

    work = make_work_repo(tmp_path)
    (work / "README").write_text("base")
    base = commit_all(work, "base")
    run_git(work, "checkout", "-b", "first")
    (work / "math_utils.py").write_text(FIB)
    first = commit_all(work, "first head")
    run_git(work, "checkout", "-b", "second", base)
    (work / "math_utils.py").write_text(CART)
    second = commit_all(work, "second head")
    remote = make_remote(tmp_path, work)
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    other_repo = "acme-corp/other"
    for repo in (REPO, other_repo):
        mirrors.ensure(
            Push(
                org_id=ORG,
                provider=ForgeProvider.GITHUB,
                repo=repo,
                clone_url=str(remote),
                ref="refs/heads/second",
                before_sha=base,
                after_sha=second,
                captured_at=T0,
            )
        )
    attributions = [
        _attribution(first, suffix="first"),
        _attribution(second, suffix="second"),
        replace(_attribution(first, suffix="other"), repo=other_repo),
    ]
    merges = [
        PullRequestMerge(
            merge_id=f"merge-{index}",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=item.repo,
            pr_number=41,
            head_repo=item.repo,
            head_ref="feature",
            head_sha=item.commit_sha,
            base_ref="main",
            base_sha=base,
            merge_commit_sha=item.commit_sha,
            merged_at=T0,
            captured_at=T0,
        )
        for index, item in enumerate(attributions)
    ]

    def derive(attrs, facts):
        return derive_merge_retention_result(
            _BoundaryStore(),
            mirrors,
            ORG,
            attributions=attrs,
            merges=facts,
            revisions=[],
            ci_outcomes=[],
            session_commit_observations=_observations(attrs),
            as_of=T0,
        )

    result = derive(attributions, merges)
    assert len(result.rows) == 3
    assert result.joined_pull_requests == 2
    assert result == derive(list(reversed(attributions)), list(reversed(merges)))


def test_merge_candidate_file_and_unobserved_edge_counts_are_distinct(tmp_path):
    observed = [_attribution("a" * 40, file_path=name) for name in ("a.py", "b.py")]
    unobserved = [
        _attribution("b" * 40, file_path=name, suffix="unobserved")
        for name in ("a.py", "b.py")
    ]
    result = derive_merge_retention_result(
        _BoundaryStore(),
        MirrorManager(str(tmp_path / "mirrors")),
        ORG,
        attributions=[*observed, *unobserved],
        merges=[],
        revisions=[],
        ci_outcomes=[],
        session_commit_observations=_observations(observed),
        as_of=T0,
    )
    assert result.attributed_candidates == 2
    assert result.skipped["session_commit_unobserved"] == 1
    assert result.membership["attribution_without_merge"] == 2

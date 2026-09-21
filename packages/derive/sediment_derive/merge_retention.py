# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retention of attributed commit-file text through pull request merge."""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

from sediment_core import (
    CIOutcome,
    CommitSha,
    FactStore,
    FactTable,
    NonEmptyId,
    OrgId,
    PullRequestMerge,
    PullRequestRevision,
    RepoSlug,
    SessionCommitObservation,
)

from .attribution import Attribution, AttributionSource, derive_attribution_result
from .diff import DIFF_SKIP_REASONS, DiffParseResult, parse_diff_sections
from .mirror import FileRead, FileReadStatus, MirrorError, MirrorManager, RepoMirror
from .provenance import Provenance
from .repository_identity import (
    RepositoryIdentity,
    RepositoryContext,
    RepositoryKey,
    CommitKey,
    REPOSITORY_IDENTITY_SKIP_REASONS,
    commit_sort_key,
    repository_sort_key,
    repository_identity_evidence_of,
)
from .repository_context import read_repository_context
from .survival_scoring import four_gram_containment
from .session_commit import SESSION_COMMIT_UNOBSERVED, bind_session_commit_keys_result

logger = logging.getLogger("sediment.derive.merge_retention")

MERGE_RETENTION_IMPLEMENTATION_VERSION = "3"

MERGE_RETENTION_SKIP_REASONS = frozenset(
    {
        "session_commit_unobserved",
        *REPOSITORY_IDENTITY_SKIP_REASONS,
        *DIFF_SKIP_REASONS,
        "mirror_absent",
        "source_commit_absent",
        "head_commit_absent",
        "merge_commit_absent",
        "source_diff_unavailable",
        "source_file_diff_absent",
        "source_text_empty",
        "head_path_unresolved",
        "merge_path_unresolved",
        "head_file_absent",
        "merge_file_absent",
        "binary_file",
        "file_oversized",
        "file_read_failed",
        "scorer_error",
        "invalid_score",
    }
)
MERGE_MEMBERSHIP_REASONS = frozenset(
    {
        "attribution_without_merge",
        "ambiguous_pr_membership",
        "ancestry_check_failed",
    }
)

RetentionScorer = Callable[[str, str], float]

MergeMembershipStatus = Literal[
    "joined",
    "without_merge",
    "ambiguous",
    "ancestry_unresolved",
]
MERGE_MEMBERSHIP_STATUSES: tuple[MergeMembershipStatus, ...] = (
    "joined",
    "without_merge",
    "ambiguous",
    "ancestry_unresolved",
)


@dataclass(frozen=True)
class MergeRetentionPolicy:
    """Bounded file-read policy for merge-retention scoring."""

    max_file_bytes: int = 1_000_000
    policy_version: str = MERGE_RETENTION_IMPLEMENTATION_VERSION

    def __post_init__(self) -> None:
        if type(self.max_file_bytes) is not int or self.max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be a positive integer")
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise ValueError("policy_version must be non-empty")


@dataclass(frozen=True)
class MergeRetention:
    """One attributed file measured at final-head and merge boundaries."""

    org_id: OrgId
    repo: RepoSlug
    pr_number: int
    merge_id: NonEmptyId
    inference_call_id: NonEmptyId
    session_id: NonEmptyId
    source_commit_sha: CommitSha
    source_file_path: str
    head_commit_sha: CommitSha
    head_file_path: str
    merge_commit_sha: CommitSha
    merge_file_path: str
    head_retention_score: float
    merge_retention_score: float
    attribution_source: AttributionSource
    attribution_similarity_score: float
    provenance: Provenance
    session_commit_observation_ids: tuple[NonEmptyId, ...] = ()
    repository_identity: RepositoryIdentity | None = None


@dataclass(frozen=True)
class MergeMembershipOutcome:
    """One Attribution's pull-request membership classification."""

    org_id: OrgId
    repo: RepoSlug
    inference_call_id: NonEmptyId
    session_id: NonEmptyId
    source_commit_sha: CommitSha
    source_file_path: str
    attribution_source: AttributionSource
    attribution_similarity_score: float
    status: MergeMembershipStatus
    pr_number: int | None
    merge_id: NonEmptyId | None
    provenance: Provenance
    session_commit_observation_ids: tuple[NonEmptyId, ...] = ()
    repository_identity: RepositoryIdentity | None = None

    def __post_init__(self) -> None:
        has_merge_identity = self.pr_number is not None or self.merge_id is not None
        if self.status == "joined":
            if self.pr_number is None or self.pr_number < 1 or self.merge_id is None:
                raise ValueError("joined membership requires pull-request identity")
        elif has_merge_identity:
            raise ValueError("unjoined membership must omit pull-request identity")


@dataclass
class MergeRetentionResult:
    """Ordered retention rows plus membership and scoring tallies.

    Candidates count observation-qualified files; ``session_commit_unobserved``
    counts distinct repository/commit/Session edges. These units aren't additive.
    Joined pull requests count distinct (repository, PR number) pairs.
    """

    provenance: Provenance
    rows: list[MergeRetention] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    membership: Counter[str] = field(default_factory=Counter)
    session_commit_observations: tuple[SessionCommitObservation, ...] = ()
    as_of: datetime | None = None
    attributed_candidates: int = 0
    joined_candidates: int = 0
    joined_pull_requests: int = 0
    membership_outcomes: list[MergeMembershipOutcome] = field(default_factory=list)


def derive_merge_retentions(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: MergeRetentionPolicy | None = None,
    *,
    attributions: list[Attribution] | None = None,
    scorer: RetentionScorer = four_gram_containment,
    policy_digest: str | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> list[MergeRetention]:
    """Return merge-retention rows without diagnostic counters."""
    return derive_merge_retention_result(
        store,
        mirrors,
        org_id,
        policy,
        attributions=attributions,
        scorer=scorer,
        policy_digest=policy_digest,
        repository_context=repository_context,
        as_of=as_of,
    ).rows


def derive_merge_retention_result(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: MergeRetentionPolicy | None = None,
    *,
    attributions: list[Attribution] | None = None,
    merges: Iterable[PullRequestMerge] | None = None,
    revisions: Iterable[PullRequestRevision] | None = None,
    ci_outcomes: Iterable[CIOutcome] | None = None,
    session_commit_observations: Iterable[SessionCommitObservation] | None = None,
    as_of: datetime | None = None,
    scorer: RetentionScorer = four_gram_containment,
    policy_digest: str | None = None,
    repository_context: RepositoryContext | None = None,
) -> MergeRetentionResult:
    """Derive merge retention inside one repeatable-read Fact snapshot.

    Supplied merge, revision, and CI outcome collections are authoritative,
    including empty collections. Omitted inputs retain standalone store reads.
    """
    with store.read_snapshot() as snapshot:
        return _derive_merge_retention_result(
            snapshot,
            mirrors,
            org_id,
            policy,
            attributions=attributions,
            merges=merges,
            revisions=revisions,
            ci_outcomes=ci_outcomes,
            session_commit_observations=session_commit_observations,
            as_of=as_of,
            scorer=scorer,
            policy_digest=policy_digest,
            repository_context=repository_context,
        )


def _derive_merge_retention_result(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: MergeRetentionPolicy | None = None,
    *,
    attributions: list[Attribution] | None = None,
    merges: Iterable[PullRequestMerge] | None = None,
    revisions: Iterable[PullRequestRevision] | None = None,
    ci_outcomes: Iterable[CIOutcome] | None = None,
    session_commit_observations: Iterable[SessionCommitObservation] | None = None,
    as_of: datetime | None = None,
    scorer: RetentionScorer = four_gram_containment,
    policy_digest: str | None = None,
    repository_context: RepositoryContext | None = None,
) -> MergeRetentionResult:
    if as_of is not None and as_of.utcoffset() is None:
        raise ValueError("as_of must be an aware datetime")
    if as_of is None and repository_context is not None:
        as_of = repository_context.as_of
    policy = policy or MergeRetentionPolicy()
    provenance = Provenance(
        policy_version=policy.policy_version,
        quarantine_revision=store.quarantine_revision(org_id),
        policy_digest=policy_digest,
    )
    result = MergeRetentionResult(provenance=provenance)
    # Supplied iterables remain authoritative, including empty generators.
    merges = None if merges is None else tuple(merges)
    revisions = None if revisions is None else tuple(revisions)
    ci_outcomes = None if ci_outcomes is None else tuple(ci_outcomes)
    session_commit_observations = (
        None
        if session_commit_observations is None
        else tuple(session_commit_observations)
    )
    preloaded = tuple(
        item
        for values in (merges, revisions, ci_outcomes, session_commit_observations)
        if values is not None
        for item in values
    )
    merge_facts = store.read_pull_request_merges(org_id) if merges is None else merges
    revision_facts = (
        store.read_pull_request_revisions(org_id) if revisions is None else revisions
    )
    ci_facts = store.read_ci_outcomes(org_id) if ci_outcomes is None else ci_outcomes
    observation_facts = list(
        store.read_session_commit_observations(org_id, as_of=as_of)
        if session_commit_observations is None
        else session_commit_observations
    )
    supplements = []
    for item in preloaded:
        for role in (
            ("repo", "head_repo")
            if isinstance(item, (PullRequestMerge, PullRequestRevision))
            else ("repo",)
        ):
            evidence = repository_identity_evidence_of(item, role=role)
            if evidence.repository_id is None:
                supplements.append(evidence)
    context = repository_context or read_repository_context(
        store, org_id, as_of=as_of, supplemental_legacy_evidence=supplements
    )
    if context.org_id != org_id or (
        as_of is not None and context.as_of != as_of.astimezone(UTC)
    ):
        raise ValueError("repository context must match organization and boundary")
    if repository_context is None and as_of is None and attributions is None:
        # Preserve Attribution's post-Push grace when selecting the implicit
        # boundary. Explicit contexts remain exact across all consumers.
        latest_call = max(
            (
                row.observed_at.astimezone(UTC)
                for row in store.read_inference_call_summaries(org_id)
            ),
            default=context.as_of,
        )
        if latest_call > context.as_of:
            context = read_repository_context(
                store,
                org_id,
                as_of=latest_call,
                supplemental_legacy_evidence=supplements,
            )
    boundary = context.as_of
    result.as_of = boundary
    preloaded_identified = {
        id(item)
        for item in preloaded
        if item.repository_id is not None
        or getattr(item, "head_repository_id", None) is not None
    }

    def resolve(item, role="repo"):
        if repository_context is None and id(item) in preloaded_identified:
            reason, key = "repository_identity_unresolved", None
        else:
            resolution = context.resolve_fact(item, role=role)
            reason, key = resolution.reason, resolution.key
        if key is None:
            _skip(result.skipped, reason)
            logger.warning("merge_repository_declined reason=%s count=1", reason)
        return key

    def eligible(item):
        return item.org_id == org_id and item.captured_at.astimezone(UTC) <= boundary

    if repository_context is None:
        rejected = [
            item
            for item in observation_facts
            if id(item) in preloaded_identified and eligible(item)
        ]
        for item in rejected:
            resolve(item)
        observation_facts = [
            item for item in observation_facts if id(item) not in preloaded_identified
        ]
    binding_result = bind_session_commit_keys_result(
        observation_facts, org_id, as_of=boundary, repository_context=context
    )
    result.skipped.update(binding_result.skipped)
    bindings = binding_result.bindings
    result.session_commit_observations = tuple(
        item for values in bindings.values() for item in values
    )
    supplied_attributions = attributions is not None
    if attributions is None:
        attribution_result = derive_attribution_result(
            store, mirrors, org_id, repository_context=context, as_of=boundary
        )
        attributions = attribution_result.attributions
    qualified = []
    unobserved = set()
    for item in attributions:
        if item.org_id != org_id:
            continue
        resolved = context.resolve_reference(
            org_id, item.repo, repository_identity=item.repository_identity
        )
        reason = resolved.reason
        key = resolved.key
        if (
            supplied_attributions
            and repository_context is None
            and item.repository_identity is not None
        ):
            key, reason = None, "repository_identity_unresolved"
        if key is not None and item.repository_identity is not None:
            anchor = context.resolve_source(FactTable.PUSHES, item.source_push_id)
            if anchor.key != key:
                key, reason = None, anchor.reason or "repository_identity_conflict"
        if key is None:
            _skip(result.skipped, reason)
            logger.warning("merge_attribution_declined reason=%s count=1", reason)
            continue
        commit = CommitKey(key, item.commit_sha)
        if (commit, item.session_id) not in bindings:
            unobserved.add((commit, item.session_id))
            continue
        qualified.append((replace(item, repo=context.repo_for(key)), commit))
    if unobserved:
        result.skipped[SESSION_COMMIT_UNOBSERVED] += len(unobserved)
    qualified.sort(
        key=lambda pair: (
            commit_sort_key(pair[1]),
            pair[0].file_path,
            pair[0].inference_call_id,
        )
    )
    result.attributed_candidates = len(qualified)
    # ponytail: `merges_by_repo`/`revisions_by_pull_request` key on the
    # resolved `RepositoryKey`, not on `merge.provider`/`revision.provider`
    # directly. `IdentifiedRepositoryKey` carries provider inside its
    # identity, so an identified join stays provider-qualified. A
    # `LegacyRepositoryKey` carries only `(org_id, repo)`, matching every
    # other legacy join in this resolver (pushes, CI outcomes, session-commit
    # observations). Two providers sharing one legacy repo slug would
    # collide here. `ForgeProvider` has exactly one member today, so no Fact
    # can carry a second provider value; this path stays unexercisable until
    # a second forge exists. That forge needs `LegacyRepositoryKey` to carry
    # provider (a resolver change, not a local one) before this join is safe.
    merges_by_repo = {}
    unresolved_names = set()
    head_keys = {}
    revisions_by_pull_request = {}
    all_keys = {commit.repository for _, commit in qualified}
    for merge in sorted(
        merge_facts, key=lambda item: (item.repo, item.pr_number, item.merge_id)
    ):
        if not eligible(merge):
            continue
        key = resolve(merge)
        if key is None:
            unresolved_names.add(merge.repo)
            continue
        merges_by_repo.setdefault(key, []).append(merge)
        head_keys[id(merge)] = resolve(merge, "head_repo")
        all_keys.add(key)
        if head_keys[id(merge)] is not None:
            all_keys.add(head_keys[id(merge)])
    for revision in sorted(
        revision_facts,
        key=lambda item: (item.repo, item.pr_number, item.head_sha, item.revision_id),
    ):
        if not eligible(revision):
            continue
        key = resolve(revision)
        if key is None:
            unresolved_names.add(revision.repo)
            continue
        revisions_by_pull_request.setdefault((key, revision.pr_number), []).append(
            revision
        )
        head_keys[id(revision)] = resolve(revision, "head_repo")
        if head_keys[id(revision)] is not None:
            all_keys.add(head_keys[id(revision)])
    ci_memberships = set()
    for outcome in ci_facts:
        if not eligible(outcome) or outcome.pr_number is None:
            continue
        key = resolve(outcome)
        if key is not None:
            ci_memberships.add((CommitKey(key, outcome.commit_sha), outcome.pr_number))
        else:
            unresolved_names.add(outcome.repo)
    joined_repo_prs = set()
    source_diffs = {}
    membership_by_source = {}
    with mirrors.read_repository_snapshot(all_keys):
        opened = {key: mirrors.open_repository(key) for key in all_keys}
        for attribution, commit in qualified:
            if commit not in membership_by_source:
                candidates, uncertain = _membership_candidates(
                    attribution,
                    merges_by_repo.get(commit.repository, []),
                    revisions_by_pull_request,
                    opened,
                    head_keys,
                    ci_memberships,
                    commit,
                )
                uncertain = uncertain or bool(
                    unresolved_names.intersection(
                        context.observed_repo_slugs(commit.repository)
                    )
                )
                membership_by_source[commit] = candidates, uncertain
            candidates, uncertain = membership_by_source[commit]
            if uncertain:
                status, reason = "ancestry_unresolved", "ancestry_check_failed"
            elif not candidates:
                status, reason = "without_merge", "attribution_without_merge"
            elif len(candidates) != 1:
                status, reason = "ambiguous", "ambiguous_pr_membership"
            else:
                status, reason = "joined", None
            merge = candidates[0] if status == "joined" else None
            if reason is not None:
                result.membership[reason] += 1
            outcome = _membership_outcome(attribution, status, provenance, merge=merge)
            observation_ids = tuple(
                sorted(
                    {
                        item.observation_id
                        for item in bindings[(commit, attribution.session_id)]
                    }
                )
            )
            result.membership_outcomes.append(
                replace(outcome, session_commit_observation_ids=observation_ids)
            )
            if merge is None:
                continue
            result.joined_candidates += 1
            joined_repo_prs.add((commit.repository, merge.pr_number))
            head_key = head_keys[id(merge)]
            if head_key is None:
                continue  # Membership can be exact even when the head cannot score.
            row = _score_joined_candidate(
                attribution,
                merge,
                opened[commit.repository],
                opened.get(head_key),
                policy,
                provenance,
                scorer,
                result.skipped,
                source_diffs,
                commit,
            )
            if row is not None:
                result.rows.append(
                    replace(
                        row,
                        repo=context.repo_for(commit.repository),
                        repository_identity=attribution.repository_identity,
                        session_commit_observation_ids=observation_ids,
                    )
                )

    def row_order(row):
        repository = context.resolve_reference(
            org_id, row.repo, repository_identity=row.repository_identity
        ).key
        assert repository is not None
        return (
            repository_sort_key(repository),
            row.pr_number,
            row.source_commit_sha,
            row.source_file_path,
            row.inference_call_id,
        )

    result.rows.sort(key=row_order)
    result.joined_pull_requests = len(joined_repo_prs)
    return result


def _membership_outcome(
    attribution: Attribution,
    status: MergeMembershipStatus,
    provenance: Provenance,
    *,
    merge: PullRequestMerge | None = None,
) -> MergeMembershipOutcome:
    return MergeMembershipOutcome(
        org_id=attribution.org_id,
        repo=attribution.repo,
        inference_call_id=attribution.inference_call_id,
        session_id=attribution.session_id,
        source_commit_sha=attribution.commit_sha,
        source_file_path=attribution.file_path,
        attribution_source=attribution.attribution_source,
        attribution_similarity_score=attribution.similarity_score,
        status=status,
        pr_number=merge.pr_number if merge is not None else None,
        merge_id=merge.merge_id if merge is not None else None,
        provenance=provenance,
        repository_identity=attribution.repository_identity,
    )


def _membership_candidates(
    attribution: Attribution,
    merges: list[PullRequestMerge],
    revisions_by_pull_request,
    opened: dict[RepositoryKey, RepoMirror | None],
    head_keys,
    ci_memberships: set[tuple[CommitKey, int]],
    commit: CommitKey,
) -> tuple[list[PullRequestMerge], bool]:
    candidates = []
    ancestry_unresolved = False
    target = opened.get(commit.repository)
    for merge in merges:
        if (commit, merge.pr_number) in ci_memberships:
            candidates.append(merge)
            continue
        heads = [(head_keys[id(merge)], merge.head_sha)]
        for revision in revisions_by_pull_request.get(
            (commit.repository, merge.pr_number), ()
        ):
            heads.append((head_keys[id(revision)], revision.head_sha))
            if revision.previous_head_sha is not None:
                heads.append((head_keys[id(revision)], revision.previous_head_sha))
        matched, unresolved = False, False
        for head_key, head_sha in heads:
            head = opened.get(head_key)
            if head is None or target is None:
                unresolved = True
                continue
            try:
                in_head = head.is_ancestor(attribution.commit_sha, head_sha)
                in_base = in_head and target.is_ancestor(
                    attribution.commit_sha, merge.base_sha
                )
            except MirrorError as error:
                unresolved = True
                _log_mirror_failure(
                    "merge_retention_ancestry_check_failed", attribution, merge, error
                )
                continue
            if in_head and not in_base:
                matched = True
                break
        if matched:
            candidates.append(merge)
        elif unresolved:
            ancestry_unresolved = True
    return candidates, ancestry_unresolved


def _score_joined_candidate(
    attribution: Attribution,
    merge: PullRequestMerge,
    mirror: RepoMirror | None,
    head_mirror: RepoMirror | None,
    policy: MergeRetentionPolicy,
    provenance: Provenance,
    scorer: RetentionScorer,
    skipped: Counter[str],
    source_diffs: dict[CommitKey, DiffParseResult],
    source_key: CommitKey,
) -> MergeRetention | None:
    if mirror is None or head_mirror is None:
        return _skip(skipped, "mirror_absent")
    for commit_sha, reason in (
        (attribution.commit_sha, "source_commit_absent"),
        (merge.merge_commit_sha, "merge_commit_absent"),
    ):
        if not mirror.commit_exists(commit_sha):
            return _skip(skipped, reason)
    if not head_mirror.commit_exists(merge.head_sha):
        return _skip(skipped, "head_commit_absent")
    if source_key not in source_diffs:
        try:
            source_diff = mirror.fetch_commit_diff(
                attribution.repo, attribution.commit_sha
            )
        except MirrorError as exc:
            _log_mirror_failure(
                "merge_retention_source_diff_unavailable", attribution, merge, exc
            )
            return _skip(skipped, "source_diff_unavailable")
        parsed = parse_diff_sections(source_diff)
        # Attributions sort by source commit; retain only its patch text.
        source_diffs.clear()
        source_diffs[source_key] = parsed
        skipped.update(parsed.skipped)
    matching_diffs = [
        section
        for section in source_diffs[source_key].sections
        if section.file_path == attribution.file_path
    ]
    if len(matching_diffs) != 1:
        return _skip(skipped, "source_file_diff_absent")
    source_text = matching_diffs[0].added_lines
    if not source_text.strip():
        return _skip(skipped, "source_text_empty")
    try:
        head_path = head_mirror.resolve_path(
            attribution.commit_sha, merge.head_sha, attribution.file_path
        )
    except MirrorError as exc:
        _log_mirror_failure(
            "merge_retention_head_path_resolution_failed", attribution, merge, exc
        )
        head_path = None
    if head_path is None:
        return _skip(skipped, "head_path_unresolved")
    try:
        merge_path = mirror.resolve_path(
            attribution.commit_sha, merge.merge_commit_sha, attribution.file_path
        )
    except MirrorError as exc:
        _log_mirror_failure(
            "merge_retention_merge_path_resolution_failed", attribution, merge, exc
        )
        merge_path = None
    if merge_path is None:
        return _skip(skipped, "merge_path_unresolved")
    try:
        head_file = head_mirror.read_file(
            merge.head_sha, head_path, max_bytes=policy.max_file_bytes
        )
        merge_file = mirror.read_file(
            merge.merge_commit_sha,
            merge_path,
            max_bytes=policy.max_file_bytes,
        )
    except MirrorError as exc:
        _log_mirror_failure("merge_retention_file_read_failed", attribution, merge, exc)
        return _skip(skipped, "file_read_failed")
    file_reason = _file_skip_reason(head_file, "head_file_absent")
    if file_reason is None:
        file_reason = _file_skip_reason(merge_file, "merge_file_absent")
    if file_reason is not None:
        return _skip(skipped, file_reason)
    assert head_file.text is not None and merge_file.text is not None
    try:
        head_score = scorer(source_text, head_file.text)
        merge_score = scorer(source_text, merge_file.text)
    except Exception:
        logger.warning(
            "merge_retention_scorer_error",
            extra={
                "org_id": merge.org_id,
                "repo": merge.repo,
                "pr_number": merge.pr_number,
            },
        )
        return _skip(skipped, "scorer_error")
    normalized = _normalize_scores(head_score, merge_score)
    if normalized is None:
        return _skip(skipped, "invalid_score")
    head_score, merge_score = normalized
    return MergeRetention(
        org_id=merge.org_id,
        repo=merge.repo,
        pr_number=merge.pr_number,
        merge_id=merge.merge_id,
        inference_call_id=attribution.inference_call_id,
        session_id=attribution.session_id,
        source_commit_sha=attribution.commit_sha,
        source_file_path=attribution.file_path,
        head_commit_sha=merge.head_sha,
        head_file_path=head_path,
        merge_commit_sha=merge.merge_commit_sha,
        merge_file_path=merge_path,
        head_retention_score=head_score,
        merge_retention_score=merge_score,
        attribution_source=attribution.attribution_source,
        attribution_similarity_score=attribution.similarity_score,
        provenance=provenance,
    )


def _file_skip_reason(file: FileRead, absent_reason: str) -> str | None:
    if file.status is FileReadStatus.READABLE:
        return None
    if file.status is FileReadStatus.ABSENT:
        return absent_reason
    if file.status is FileReadStatus.BINARY:
        return "binary_file"
    return "file_oversized"


def _normalize_scores(head: object, merged: object) -> tuple[float, float] | None:
    scores: list[float] = []
    for score in (head, merged):
        if isinstance(score, bool) or not isinstance(score, int | float):
            return None
        numeric = float(score)
        if not math.isfinite(numeric):
            return None
        scores.append(min(1.0, max(0.0, numeric)))
    return scores[0], scores[1]


def _skip(skipped: Counter[str], reason: str) -> None:
    skipped[reason] += 1
    return None


def _log_mirror_failure(
    event: str,
    attribution: Attribution,
    merge: PullRequestMerge,
    error: MirrorError,
) -> None:
    logger.warning(
        event,
        extra={
            "org_id": merge.org_id,
            "repo": merge.repo,
            "pr_number": merge.pr_number,
            "source_commit_sha": attribution.commit_sha,
            "error": str(error),
        },
    )

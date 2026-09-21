# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Attribution-share metric + decline alert — the server-side backstop for
silent client-side attrition.

Every client-side attribution failure found so far (hookless clones, diverged
notes push, stale install paths, missing hook entries) shares one signature:
the share of commits attributed via
``attribution_source="git_notes"`` drops and jaccard quietly takes over, or drops
to zero. ``derive_attribution_share`` makes that share a first-class, pure
derivation (ADR 0001) over ``derive_attributions`` output;
``check_attribution_share_alerts`` is a pure comparison over two already
-derived reports that flags a decline as material only once it clears
sampling noise.

Population — "the population where a note was expected": a commit counts in
``agent_plausible_commits`` iff at least one completion falls inside the same
lookback windows ``attribution.py::_window`` already computes for that push —
the organization-wide `jaccard.lookback_window_minutes` window, or a named
session in the commit's note, regardless of whether that match cleared
`git_notes.min_similarity`. A commit with zero inference calls in either window is
presumed human-only and excluded — jaccard-only human commits must not dilute
the signal. Grain is commit, not file: a commit's class is notes if any of
its files attributed via git notes, else jaccard if any attributed via jaccard,
else unattributed (inference calls existed, but no file attributed at all — the
literal outage shape, invisible to ``derive_attributions`` itself, which only
emits rows for matches).

Window anchoring (ADR 0001 — never wall-clock): each repo's row covers
``policy.window_days`` ending at the latest push passed to
``derive_attribution_share`` for that repo, entirely data-driven. A caller
gets a trailing baseline row for the same repo by re-invoking with an
earlier-scoped ``pushes`` slice and ``dataclasses.replace(policy,
window_days=policy.baseline_window_days)`` — matching the 7-day-current /
28-day-baseline default and ``--trend``'s 7-day bucket convention.

Decline materiality follows ``threshold_drift_report()``'s Wilson-CI-overlap
pattern, not a fixed percentage-point drop: ``sustained_decline`` fires only
once the current window's notes-share
Wilson CI stops overlapping the trailing baseline's, in the declining
direction, and only once both windows clear
``policy.min_cases_for_decline_verdict``. ``zero_share_nonzero_activity`` is a
separate, ungated rule: it fires unconditionally whenever
``agent_plausible_commits > 0`` and git notes produced no attribution. Jaccard
activity doesn't suppress the alert because it is the fallback that can hide a
client-side stamper outage. The alert doesn't wait for a minimum sample.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sediment_core import FactStore, OrgId, Push, RepoSlug

from .attribution import (
    AttributionSource,
    AttributionPolicy,
    _scope_candidates,
    _qualify_legacy_note_sessions,
    _window,
    derive_attribution_result,
)
from .mirror import MirrorManager
from .inference_call import inference_observed_at
from .notes import read_commit_note
from .precision_harness import MIN_DRIFT_CASES, _wilson_score_interval
from .provenance import Provenance
from .repository_context import read_repository_context
from .repository_identity import (
    CommitKey,
    IdentifiedRepositoryKey,
    LegacyRepositoryKey,
    RepositoryContext,
    RepositoryIdentity,
    RepositoryKey,
    repository_identity_evidence_of,
    repository_sort_key,
)
from .session_commit import bind_session_commit_keys_result

logger = logging.getLogger("sediment.derive.attribution_share")


@dataclass(frozen=True, kw_only=True)
class AttributionSharePolicy:
    """The tunable attribution-share semantics. ``policy_version`` stamps
    every report row's provenance; bump it when tuning any knob so derived
    datasets stay distinguishable. ``attribution`` drives the population and
    attribution classification (the same derivation ``rollout.py`` and
    ``recovery.py`` thread explicitly rather than share tuning for)."""

    attribution: AttributionPolicy = field(default_factory=AttributionPolicy)
    window_days: int = 7
    baseline_window_days: int = 28
    # No default: callers calculate the required sample size via
    # scripts/corpus_sizing.py's planner (see sediment report attribution-share
    # --target-margin); derive_attribution_share's own default falls back to
    # MIN_DRIFT_CASES only because that function never reads the field.
    min_cases_for_decline_verdict: int
    policy_version: str = "2"

    def __post_init__(self) -> None:
        if self.window_days <= 0:
            raise ValueError("AttributionSharePolicy.window_days must be positive")
        if self.baseline_window_days <= 0:
            raise ValueError(
                "AttributionSharePolicy.baseline_window_days must be positive"
            )
        if self.min_cases_for_decline_verdict < 0:
            raise ValueError(
                "AttributionSharePolicy.min_cases_for_decline_verdict "
                "must be non-negative"
            )


@dataclass(frozen=True)
class RepoAttributionShare:
    """One repo's notes-attribution share over one window — NOT a fact,
    never persisted (ADR 0001)."""

    org_id: OrgId
    repo: RepoSlug
    window_start: datetime
    window_end: datetime
    agent_plausible_commits: int  # denominator
    git_notes_attributed: int
    jaccard_attributed: int
    unattributed: int
    git_notes_share: float  # point estimate
    git_notes_share_ci: tuple[float, float]  # Wilson 95% CI
    provenance: Provenance
    repository_identity: RepositoryIdentity | None = None


@dataclass
class AttributionShareResult:
    """Repository shares and counted source, edge, and substrate losses."""

    rows: list[RepoAttributionShare] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)


class AttributionShareAlertKind(StrEnum):
    SUSTAINED_DECLINE = "sustained_decline"
    ZERO_SHARE_NONZERO_ACTIVITY = "zero_share_nonzero_activity"


@dataclass(frozen=True)
class AttributionShareAlert:
    """A material attribution-share verdict for one repo. Typed data only —
    this never pushes, pages, or writes anywhere (ADR 0001: derivations are
    pure). ``baseline`` is None for a repo with no baseline-window row (a
    ``zero_share_nonzero_activity`` alert never needs one)."""

    org_id: OrgId
    repo: RepoSlug
    kind: AttributionShareAlertKind
    current: RepoAttributionShare
    baseline: RepoAttributionShare | None
    reason: str
    repository_identity: RepositoryIdentity | None = None


def derive_attribution_share(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: AttributionSharePolicy | None = None,
    *,
    pushes: list[Push] | None = None,
    candidate_limit: int | None = None,
    note_sessions_by_commit: dict[tuple[str, str], frozenset[str]] | None = None,
    note_session_ids_by_commit: Mapping[CommitKey, frozenset[str]] | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> list[RepoAttributionShare]:
    """Derive the org's per-repo notes-attribution share from its facts.

    ``pushes`` narrows the scope, exactly as ``derive_attributions`` accepts
    (default: every push the store holds for the org). One row per repo
    that has at least one agent-plausible commit in scope; a repo with
    pushes but zero agent-plausible commits is excluded from the result
    entirely, not emitted as a zero-share row —
    ``check_attribution_share_alerts`` never sees it and so never raises a
    zero-share alert for a repo with no agent activity to begin with.
    """
    return derive_attribution_share_result(
        store,
        mirrors,
        org_id,
        policy,
        pushes=pushes,
        candidate_limit=candidate_limit,
        note_sessions_by_commit=note_sessions_by_commit,
        note_session_ids_by_commit=note_session_ids_by_commit,
        repository_context=repository_context,
        as_of=as_of,
    ).rows


def derive_attribution_share_result(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: OrgId,
    policy: AttributionSharePolicy | None = None,
    *,
    pushes: list[Push] | None = None,
    candidate_limit: int | None = None,
    note_sessions_by_commit: dict[tuple[str, str], frozenset[str]] | None = None,
    note_session_ids_by_commit: Mapping[CommitKey, frozenset[str]] | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> AttributionShareResult:
    """Derive shares inside one caller-visible Fact snapshot."""
    with store.read_snapshot() as snapshot:
        return _derive_attribution_share_result(
            snapshot,
            mirrors,
            org_id,
            policy,
            pushes=pushes,
            candidate_limit=candidate_limit,
            note_sessions_by_commit=note_sessions_by_commit,
            note_session_ids_by_commit=note_session_ids_by_commit,
            repository_context=repository_context,
            as_of=as_of,
        )


def _derive_attribution_share_result(
    store,
    mirrors: MirrorManager,
    org_id: OrgId,
    policy: AttributionSharePolicy | None = None,
    *,
    pushes: list[Push] | None = None,
    candidate_limit: int | None = None,
    note_sessions_by_commit: dict[tuple[str, str], frozenset[str]] | None = None,
    note_session_ids_by_commit: Mapping[CommitKey, frozenset[str]] | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> AttributionShareResult:
    result = AttributionShareResult()
    implicit_boundary = as_of is None and repository_context is None
    if note_sessions_by_commit is not None and note_session_ids_by_commit is not None:
        raise ValueError("choose one repository-qualified note map")
    if as_of is not None and as_of.utcoffset() is None:
        raise ValueError("as_of must be an aware datetime")
    if as_of is None and repository_context is not None:
        as_of = repository_context.as_of
    preloaded_without_context = pushes is not None and repository_context is None
    preloaded_pushes = pushes
    repository_context = repository_context or read_repository_context(
        store,
        org_id,
        as_of=as_of,
        supplemental_legacy_evidence=(
            repository_identity_evidence_of(push)
            for push in pushes or ()
            if push.repository_id is None
        ),
    )
    if repository_context.org_id != org_id or (
        as_of is not None and repository_context.as_of != as_of.astimezone(UTC)
    ):
        raise ValueError("repository context must match organization and boundary")
    policy = policy or AttributionSharePolicy(
        min_cases_for_decline_verdict=MIN_DRIFT_CASES
    )
    attribution_policy = policy.attribution
    provenance = Provenance(
        policy_version=policy.policy_version,
        quarantine_revision=store.quarantine_revision(org_id),
    )
    if pushes is None:
        pushes = store.read_pushes(org_id)
    pushes = sorted(
        (
            push
            for push in pushes
            if push.org_id == org_id
            and (
                as_of is None
                or push.captured_at.astimezone(UTC) <= as_of.astimezone(UTC)
            )
        ),
        key=lambda p: (p.captured_at.astimezone(UTC), p.push_id),
    )
    by_repo: dict[RepositoryKey, list[Push]] = defaultdict(list)
    for push in pushes:
        resolved = repository_context.resolve_fact(push)
        reason = (
            "repository_identity_unresolved"
            if preloaded_without_context and push.repository_id is not None
            else resolved.reason
        )
        if reason is not None:
            result.skipped[reason] += 1
            logger.warning(
                "Attribution share repository declined reason=%s count=1", reason
            )
        else:
            by_repo[resolved.key].append(push)
    pushes = [push for group in by_repo.values() for push in group]
    if not pushes:
        return result

    candidates = _scope_candidates(
        store, org_id, pushes, attribution_policy, limit=candidate_limit
    )
    if implicit_boundary:
        # Repository capture can precede an eligible call in the grace window.
        # Extend only the implicit cutoff using the projections already scored.
        as_of = max(
            (inference_observed_at(call).astimezone(UTC) for call, _ in candidates),
            default=repository_context.as_of,
        )
        as_of = max(as_of, repository_context.as_of)
        if as_of > repository_context.as_of:
            repository_context = read_repository_context(
                store,
                org_id,
                as_of=as_of,
                supplemental_legacy_evidence=(
                    repository_identity_evidence_of(push)
                    for push in preloaded_pushes or ()
                    if push.repository_id is None
                ),
            )
    if as_of is not None:
        candidates = [
            (call, tokens)
            for call, tokens in candidates
            if inference_observed_at(call).astimezone(UTC) <= as_of.astimezone(UTC)
        ]

    explicit_notes = note_session_ids_by_commit
    if note_sessions_by_commit is not None:
        explicit_notes, skipped = _qualify_legacy_note_sessions(
            note_sessions_by_commit, repository_context, org_id
        )
        result.skipped.update(skipped)
    captured_notes = defaultdict(set)
    if explicit_notes is None and any(
        isinstance(key, IdentifiedRepositoryKey) for key in by_repo
    ):
        bindings = bind_session_commit_keys_result(
            store.read_session_commit_observations(
                org_id, as_of=repository_context.as_of
            ),
            org_id,
            as_of=repository_context.as_of,
            repository_context=repository_context,
        )
        result.skipped.update(bindings.skipped)
        for commit, session in bindings.bindings:
            captured_notes[commit].add(session)
    captured_notes = {
        commit: frozenset(sessions) for commit, sessions in captured_notes.items()
    }

    for key in sorted(by_repo, key=repository_sort_key):
        repo = repository_context.repo_for(key)
        all_repo_pushes = by_repo[key]
        window_end = all_repo_pushes[-1].captured_at.astimezone(UTC)
        window_start = window_end - timedelta(days=policy.window_days)
        repo_pushes = [
            p
            for p in all_repo_pushes
            if window_start <= p.captured_at.astimezone(UTC) <= window_end
        ]
        if not repo_pushes:
            continue
        mirror = mirrors.open_repository(key)
        if mirror is None:
            result.skipped["mirror_absent"] += len(repo_pushes)
            logger.debug("mirror_absent", extra={"repo": repo})
            continue
        notes = (
            explicit_notes
            if explicit_notes is not None
            else captured_notes
            if isinstance(key, IdentifiedRepositoryKey)
            else None
        )

        attributions_by_commit: dict[str, set[AttributionSource]] = defaultdict(set)
        attribution_result = derive_attribution_result(
            store,
            mirrors,
            org_id,
            attribution_policy,
            pushes=repo_pushes,
            candidates=candidates,
            note_session_ids_by_commit=notes,
            repository_context=repository_context,
            as_of=as_of,
        )
        result.skipped.update(attribution_result.skipped)
        for attribution in attribution_result.attributions:
            attributions_by_commit[attribution.commit_sha].add(
                attribution.attribution_source
            )

        agent_plausible = notes_n = jaccard_n = unattributed_n = 0
        seen_commits: set[str] = set()
        for push in repo_pushes:
            org_candidates = _window(
                candidates,
                push,
                attribution_policy.jaccard.lookback_window_minutes,
                attribution_policy,
            )
            git_notes_candidates = _window(
                candidates,
                push,
                attribution_policy.git_notes.lookback_window_minutes,
                attribution_policy,
            )
            org_window_has_activity = bool(org_candidates)
            for sha in mirror.list_push_commits(
                push, attribution_policy.max_commits_per_push
            ):
                if sha in seen_commits:
                    continue
                seen_commits.add(sha)

                plausible = org_window_has_activity
                if not plausible:
                    if notes is None:
                        note = read_commit_note(mirror.path, sha)  # fail-soft: None
                        session_ids = (
                            {session.session_id for session in note.sessions}
                            if note is not None
                            else frozenset()
                        )
                    else:
                        session_ids = notes.get(CommitKey(key, sha), frozenset())
                    plausible = any(
                        completion.session_id in session_ids
                        for completion, _ in git_notes_candidates
                    )
                if not plausible:
                    continue

                agent_plausible += 1
                sources = attributions_by_commit.get(sha, set())
                if AttributionSource.GIT_NOTES in sources:
                    notes_n += 1
                elif AttributionSource.JACCARD in sources:
                    jaccard_n += 1
                else:
                    unattributed_n += 1

        if agent_plausible == 0:
            continue

        result.rows.append(
            RepoAttributionShare(
                org_id=org_id,
                repo=repo,
                window_start=window_start,
                window_end=window_end,
                agent_plausible_commits=agent_plausible,
                git_notes_attributed=notes_n,
                jaccard_attributed=jaccard_n,
                unattributed=unattributed_n,
                git_notes_share=notes_n / agent_plausible,
                git_notes_share_ci=_wilson_score_interval(notes_n, agent_plausible),
                provenance=provenance,
                repository_identity=key.identity
                if isinstance(key, IdentifiedRepositoryKey)
                else None,
            )
        )
    return result


def check_attribution_share_alerts(
    current: list[RepoAttributionShare],
    baseline: list[RepoAttributionShare],
    policy: AttributionSharePolicy,
) -> list[AttributionShareAlert]:
    """Compare two already-derived report rows and flag material declines.

    Pure comparison, no facts or mirrors touched — split from
    ``derive_attribution_share`` so the decline rule can change without
    touching the derivation itself (the same split ``attribution.py`` /
    ``precision_harness.py`` already use for scorer vs. threshold).
    """
    baseline_by_repo = {_share_repository_key(row): row for row in baseline}
    alerts: list[AttributionShareAlert] = []
    for row in sorted(
        current, key=lambda r: repository_sort_key(_share_repository_key(r))
    ):
        base = baseline_by_repo.get(_share_repository_key(row))

        if row.agent_plausible_commits > 0 and row.git_notes_attributed == 0:
            # Ungated: a notes-dark repo must alert even when jaccard keeps
            # attributing commits and no baseline exists.
            alerts.append(
                AttributionShareAlert(
                    org_id=row.org_id,
                    repo=row.repo,
                    repository_identity=row.repository_identity,
                    kind=AttributionShareAlertKind.ZERO_SHARE_NONZERO_ACTIVITY,
                    current=row,
                    baseline=base,
                    reason=(
                        f"{row.agent_plausible_commits} agent-plausible commits "
                        "with zero git-notes attribution"
                    ),
                )
            )
            continue

        if base is None:
            continue
        if (
            row.agent_plausible_commits < policy.min_cases_for_decline_verdict
            or base.agent_plausible_commits < policy.min_cases_for_decline_verdict
        ):
            continue
        if row.git_notes_share >= base.git_notes_share:
            continue
        # Non-overlap in the declining direction only: current's upper bound
        # must sit below baseline's lower bound. threshold_delta-style fixed
        # drops fire on sampling noise at small counts; only a CI gap is
        # evidence.
        if row.git_notes_share_ci[1] >= base.git_notes_share_ci[0]:
            continue
        alerts.append(
            AttributionShareAlert(
                org_id=row.org_id,
                repo=row.repo,
                repository_identity=row.repository_identity,
                kind=AttributionShareAlertKind.SUSTAINED_DECLINE,
                current=row,
                baseline=base,
                reason=(
                    f"git_notes_share declined from {base.git_notes_share:.4f} "
                    f"{base.git_notes_share_ci} to {row.git_notes_share:.4f} "
                    f"{row.git_notes_share_ci}; Wilson CIs no longer overlap"
                ),
            )
        )
    return alerts


def _share_repository_key(row: RepoAttributionShare) -> RepositoryKey:
    return (
        IdentifiedRepositoryKey(row.org_id, row.repository_identity)
        if row.repository_identity is not None
        else LegacyRepositoryKey(row.org_id, row.repo)
    )

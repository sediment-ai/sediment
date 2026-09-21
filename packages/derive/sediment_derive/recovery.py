# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Recovery-pair derivation (ADR 0001) — mining red-to-green CI transitions into
error-recovery training rows.

``derive_recovery_pairs`` is a pure function of (facts, mirrors, policy): it
reads the org's ``CIOutcome`` facts, groups them by ``(repo, branch,
workflow)``, and pairs each FAILED run with the next PASSED run of the *same
check* in that lineage — the error-recovery signal (the error state at red,
plus the red-to-green diff that fixed it) used to train error recovery.
Nothing here is persisted: a ``RecoverySample`` is derived data,
recomputable over all history, versioned by the policy that produced it.
Repository identity comes from ``CIOutcome.repo``, never from parsing
``run_url``.

**Why the workflow is part of the group key.** The capture layer stores one
``CIOutcome`` per workflow per commit, so a repo with two checks (a
fast lint, a slow test suite) interleaves verdicts in one ``(repo, branch)``
stream. Pairing across checks fabricates fixes — a ``tests`` FAILED would
pair with the next ``lint`` PASSED on a commit whose tests still fail — and
the spent-FAILED rule would let a sibling check's green consume a real red.
``CIOutcome`` carries the check's identity precisely for this
through ``CIWorkflowResolution``. The stream partitions by org, repository,
branch, provider, and resolved definition: workflow ID first, then path in a
separate namespace. Names and URLs never establish definition identity.
A name-only clean resolution skips once as ``workflow_identity_absent``.

Purity constraints (ADR 0001): ``CIResolution`` first orders attempts only by
provider ``run_attempt``. Recovery then orders distinct commit verdicts by
``(captured_at, outcome_id)`` and walks repo/branch groups in sorted key order,
so shuffled ingest reproduces identical pairs. Fact metadata never orders
attempts. All fact reads go through the store's default
(quarantine-excluding) read path, and the org's ``quarantine_revision`` folds
into each sample's provenance alongside the policy version.

**The pairing rule.** For each clean resolved FAILED lineage, the next clean
resolved PASSED lineage is the candidate fix. Suspected-flake resolutions
cannot seed a recovery pair. CANCELLED outcomes are skipped over (neither
pass nor fail, not a transition boundary — a run someone aborted is not part
of the story). Consecutive FAILEDs collapse onto the *last* red before the
green — the tightest transition window, and the only pairing consistent with
"what fixed it": FAILED, FAILED, PASSED pairs only (2nd FAILED, PASSED). A
FAILED is "spent" the first time a PASSED follows it, whether or not that
candidate pair survives the ancestry/size gates below. The next PASSED is
the candidate fix; the search does not continue over subsequent PASSEDs.

**The ancestry guard.** ``git merge-base --is-ancestor`` (``RepoMirror.
is_ancestor``, already used by the task projection) confirms the fixed
commit actually descends from the failed one before anything is paired. This
guards against force-pushes, reordered webhook delivery, and branch-name
reuse producing a nonsense "fix" — two CI outcomes that happen to sit
adjacent in capture-time order but do not sit in a real ancestor/descendant
relationship on disk. A same-commit pair (the same workflow re-run to green
on the identical commit — a flaky-test rerun) is also declined:
``is_ancestor`` is reflexive (equal shas count as ancestors), but there is no
diff to recover from, so it is not a recovery pair — and it *spends* the
FAILED, correctly: the rerun proved the commit was never really red.

**No network fetch.** The diff (``RepoMirror.diff_range``, ``git diff -M
failed..fixed``) and the ancestry check are both read-only mirror queries — a
repo never mirrored contributes nothing, fail-soft (log + skip), matching
``attribution.py``'s and ``rollout.py``'s mirror-absence handling.

**Size cap.** ``RecoveryPolicy.max_recovery_diff_lines`` (default 200) caps
the diff's changed-line count — every ``+``/``-`` line *inside a hunk*, i.e.
added-plus-removed, the same count ``git diff --shortstat`` reports as
insertions + deletions. Counting is hunk-aware (not a bare prefix filter) so
removed content that itself begins ``--`` (an SQL comment, say) renders as
``---…`` in the hunk and still counts, instead of being mistaken for a file
header. The result records every candidate diff's actual line count at this
gate, split into kept (``<=`` cap) and dropped (``>`` cap). Oversized diffs
are skipped and logged, never silently dropped.

**Enrichment.** Each pair's ``failed_inference_call_ids`` are the inference calls
``derive_attributions`` (notes or jaccard) attaches to the *failed* commit,
and its ``fixed_inference_call_ids`` the inference calls attached to the *fixed*
commit — the prompt/response context around the mistake and around its fix,
when any exists. Both come from the same org-wide attribution map, computed
once; projections read both sides (the eval split checks session membership
over the failed *and* fixed sides). This is best-effort: a
pair with no attributed completions on either side is a fine, expected
outcome and is never dropped for lacking them.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

from sediment_core import CIOutcome, CIResult, FactStore, NonEmptyId

from .attribution import AttributionPolicy, AttributionSource, derive_attributions
from .ci_resolution import (
    CI_RESOLUTION_SKIP_REASONS,
    CIResolutionPolicy,
    CIResolutionSkipReason,
    derive_ci_resolution_result,
)
from .mirror import MirrorError, MirrorManager, RepoMirror
from .provenance import Provenance
from .repository_context import read_repository_context
from .repository_identity import (
    CommitKey,
    RepositoryContext,
    RepositoryIdentity,
    RepositoryKey,
    commit_sort_key,
    repository_sort_key,
)
from .session_commit import bind_session_commit_keys_result

logger = logging.getLogger("sediment.derive.recovery")

RECOVERY_IMPLEMENTATION_VERSION = "5"
DEFAULT_MAX_RECOVERY_DIFF_LINES = 200
type RecoverySkipReason = (
    CIResolutionSkipReason
    | Literal[
        "unreliable_ci_resolution",
        "workflow_identity_absent",
        "mirror_absent",
        "same_commit",
        "ancestry_check_failed",
        "not_ancestor",
        "diff_unavailable",
        "diff_oversized",
    ]
)
RECOVERY_SKIP_REASONS: tuple[RecoverySkipReason, ...] = (
    *CI_RESOLUTION_SKIP_REASONS,
    "unreliable_ci_resolution",
    "workflow_identity_absent",
    "mirror_absent",
    "same_commit",
    "ancestry_check_failed",
    "not_ancestor",
    "diff_unavailable",
    "diff_oversized",
)


@dataclass(frozen=True)
class RecoveryPolicy:
    """The tunable recovery-pairing semantics. ``policy_version`` stamps
    every sample's provenance; bump it when tuning any knob so derived
    datasets stay distinguishable. ``attribution`` drives the completion-id
    enrichment step (the same derivation `rollout.py` reuses for jaccard
    commit binding)."""

    max_recovery_diff_lines: int = DEFAULT_MAX_RECOVERY_DIFF_LINES
    policy_version: str = RECOVERY_IMPLEMENTATION_VERSION
    attribution: AttributionPolicy = field(default_factory=AttributionPolicy)
    ci_resolution: CIResolutionPolicy = field(default_factory=CIResolutionPolicy)


@dataclass(frozen=True)
class RecoveryAttributionEvidence:
    """Optional completion enrichment for one side of a Recovery pair."""

    inference_call_id: NonEmptyId
    session_id: NonEmptyId
    attribution_sources: tuple[AttributionSource, ...]
    session_commit_observation_ids: tuple[NonEmptyId, ...] = ()


@dataclass(frozen=True)
class RecoverySample:
    """One red-to-green CI transition and its fixing diff — NOT a fact,
    never persisted (ADR 0001). A projection output for error-recovery
    training rows, not a projected attributed completion — it reads CIOutcome
    facts and the mirror directly, not the attributed-completion layer."""

    org_id: str
    repo: str
    branch: str
    # The check that went red then green — the error state names *what* was
    # failing. Read off the FAILED outcome (the red run defines the error
    # state; both outcomes share the group's workflow identity).
    workflow_name: str
    workflow_path: str | None
    failed_commit_sha: str
    fixed_commit_sha: str
    failed_outcome_id: str
    fixed_outcome_id: str
    recovery_diff: str
    # Attributed inference calls on the failed commit (the mistake side) and on
    # the fixed commit (the fix side). Both sides feed the projection-time
    # eval split — a fix authored by a captured session is eval evidence too.
    failed_inference_call_ids: list[str]
    fixed_inference_call_ids: list[str]
    provenance: Provenance
    failed_attribution_evidence: tuple[RecoveryAttributionEvidence, ...] = ()
    fixed_attribution_evidence: tuple[RecoveryAttributionEvidence, ...] = ()
    repository_identity: RepositoryIdentity | None = None


@dataclass
class RecoveryResult:
    """The result of recovery-pair derivation: the pairs to project, plus the
    skip tally (reason -> count). Mirrors ``rlvr.Projection``'s shape while
    keeping the domain name ``pairs`` for the emitted recovery samples.
    ``kept_diff_line_counts`` and ``dropped_diff_line_counts`` record the
    actual changed-line count for every candidate whose diff was available
    and reached the size gate, split at ``max_recovery_diff_lines``."""

    pairs: list[RecoverySample] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    max_recovery_diff_lines: int = DEFAULT_MAX_RECOVERY_DIFF_LINES
    kept_diff_line_counts: list[int] = field(default_factory=list)
    dropped_diff_line_counts: list[int] = field(default_factory=list)


def derive_recovery_pairs(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: RecoveryPolicy | None = None,
    *,
    policy_digest: str | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> list[RecoverySample]:
    """Compatibility wrapper returning only the derived recovery pairs.

    Use ``derive_recovery_result`` when the caller also needs the structured
    skip-reason tally.
    """
    return derive_recovery_result(
        store,
        mirrors,
        org_id,
        policy,
        policy_digest=policy_digest,
        repository_context=repository_context,
        as_of=as_of,
    ).pairs


def derive_recovery_result(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: RecoveryPolicy | None = None,
    *,
    policy_digest: str | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> RecoveryResult:
    """Derive the org's recovery pairs from its CI-outcome facts and mirrors.

    CI outcomes are read quarantine-excluded (the default path, ADR 0001),
    resolved by provider run identity, then grouped by org, repository, branch,
    provider, and resolved workflow ID (or path when the ID is absent),
    and walked in ``(captured_at, outcome_id)`` order to find each FAILED->PASSED
    transition of the same check (see the module docstring for why the
    workflow partitions the stream). A repo with no local mirror contributes
    no pairs (fail-soft, logged) — a derivation never fetches from the
    network.

    Ordering is a function of the facts alone, so a re-run — or a run over
    the same facts ingested in a different order — reproduces identical
    samples.
    """
    with store.read_snapshot() as snapshot:
        return _derive_recovery_result(
            snapshot,
            mirrors,
            org_id,
            policy,
            policy_digest=policy_digest,
            repository_context=repository_context,
            as_of=as_of,
        )


def _derive_recovery_result(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: RecoveryPolicy | None = None,
    *,
    policy_digest: str | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> RecoveryResult:
    """Derive recovery pairs inside the caller's fact snapshot."""

    if as_of is not None and as_of.utcoffset() is None:
        raise ValueError("as_of must be an aware datetime")
    context = repository_context or read_repository_context(store, org_id, as_of=as_of)
    if context.org_id != org_id or (
        as_of is not None and context.as_of != as_of.astimezone(UTC)
    ):
        raise ValueError("repository context must match organization and boundary")
    if repository_context is None and as_of is None:
        # Attribution permits calls observed after a Push. A repository-only
        # boundary would cut that existing grace window at the latest Push.
        boundary = max(
            (
                row.observed_at.astimezone(UTC)
                for row in store.read_inference_call_summaries(org_id)
            ),
            default=context.as_of,
        )
        if boundary > context.as_of:
            context = read_repository_context(store, org_id, as_of=boundary)
    policy = policy or RecoveryPolicy()
    provenance = Provenance(
        policy_version=policy.policy_version,
        quarantine_revision=store.quarantine_revision(org_id),
        policy_digest=policy_digest,
    )
    result = RecoveryResult(max_recovery_diff_lines=policy.max_recovery_diff_lines)

    outcomes = [
        item
        for item in store.read_ci_outcome_projections(org_id)
        if item.org_id == org_id and item.captured_at.astimezone(UTC) <= context.as_of
    ]
    if not outcomes:
        return result

    outcome_by_id = {outcome.outcome_id: outcome for outcome in outcomes}
    ci_result = derive_ci_resolution_result(
        outcomes,
        policy.ci_resolution,
        quarantine_revision=provenance.quarantine_revision,
        repository_context=context,
    )
    result.skipped.update(ci_result.skipped)

    # One transition stream per resolved check. Suspected flakes cannot seed
    # recovery pairs: their categorical verdict remains available to other
    # projections, but a zero-trust red would fabricate a fixing diff.
    by_lineage: dict[tuple[RepositoryKey, str, str, str, str], list[CIOutcome]] = (
        defaultdict(list)
    )
    for resolution in ci_result.resolutions:
        if resolution.verdict is None:
            continue
        for workflow_resolution in resolution.workflow_resolutions:
            if workflow_resolution.verdict is None:
                continue
            if workflow_resolution.suspected_flake:
                result.skipped["unreliable_ci_resolution"] += 1
                continue
            verdict_outcome_id = workflow_resolution.verdict_outcome_id
            if verdict_outcome_id is None:
                continue
            outcome = outcome_by_id[verdict_outcome_id]
            if workflow_resolution.workflow_id:
                definition_kind = "id"
                definition_value = workflow_resolution.workflow_id
            elif workflow_resolution.workflow_path:
                definition_kind = "path"
                definition_value = workflow_resolution.workflow_path
            else:
                result.skipped["workflow_identity_absent"] += 1
                logger.info(
                    "recovery_workflow_identity_absent",
                    extra={
                        "org_id": resolution.org_id,
                        "repo": resolution.repo,
                        "provider": workflow_resolution.provider.value,
                        "run_id": workflow_resolution.run_id,
                    },
                )
                continue
            repository = context.resolve_fact(outcome).key
            assert repository is not None
            key = (
                repository,
                workflow_resolution.branch,
                workflow_resolution.provider.value,
                definition_kind,
                definition_value,
            )
            by_lineage[key].append(outcome)

    attribution_evidence_by_commit = _attribution_evidence_by_commit(
        store,
        mirrors,
        org_id,
        policy.attribution,
        policy_digest=policy_digest,
        repository_context=context,
        skipped=result.skipped,
    )

    completions_by_commit = {
        key: sorted({item.inference_call_id for item in value})
        for key, value in attribution_evidence_by_commit.items()
    }

    for lineage in sorted(
        by_lineage, key=lambda key: (repository_sort_key(key[0]), *key[1:])
    ):
        repository, branch, _, _, _ = lineage
        repo = context.repo_for(repository)
        ordered = sorted(
            by_lineage[lineage],
            key=lambda o: (o.captured_at.astimezone(UTC), o.outcome_id),
        )
        with mirrors.read_repository_snapshot([repository]):
            mirror = mirrors.open_repository(repository)
            if mirror is None:
                skipped = _candidate_transition_count(ordered)
                result.skipped["mirror_absent"] += skipped
                logger.debug("mirror_absent", extra={"repo": repo, "skipped": skipped})
                continue
            pending_failed: CIOutcome | None = None
            for outcome in ordered:
                if outcome.result not in {CIResult.PASSED, CIResult.FAILED}:
                    continue  # non-verdict evidence isn't a transition boundary
                if outcome.result == CIResult.FAILED:
                    # Last red before the green wins: overwrite, never accumulate.
                    pending_failed = outcome
                    continue
                # outcome.result == CIResult.PASSED
                if pending_failed is None:
                    continue
                sample, skipped, diff_line_count = _try_pair(
                    mirror,
                    repository,
                    org_id,
                    repo,
                    branch,
                    pending_failed,
                    outcome,
                    completions_by_commit,
                    policy,
                    provenance,
                )
                if sample is not None:
                    sample = replace(
                        sample,
                        failed_attribution_evidence=attribution_evidence_by_commit.get(
                            CommitKey(repository, sample.failed_commit_sha), ()
                        ),
                        fixed_attribution_evidence=attribution_evidence_by_commit.get(
                            CommitKey(repository, sample.fixed_commit_sha), ()
                        ),
                    )
                # The FAILED is spent on the next PASSED regardless of whether the
                # candidate pair survives the ancestry/size gates. The next
                # PASSED is the only candidate fix; later PASSEDs are not searched.
                pending_failed = None
                if sample is not None:
                    result.pairs.append(sample)
                    if diff_line_count is not None:
                        result.kept_diff_line_counts.append(diff_line_count)
                elif skipped is not None:
                    result.skipped[skipped] += 1
                    if skipped == "diff_oversized" and diff_line_count is not None:
                        result.dropped_diff_line_counts.append(diff_line_count)
    return result


def _candidate_transition_count(ordered: list[CIOutcome]) -> int:
    """Count FAILED->next-PASSED candidate transitions in one ordered lineage.

    Used when a mirror is absent: the real pairing gates cannot run, but the
    report still needs one ``mirror_absent`` skip per candidate transition
    that would otherwise have been evaluated.
    """
    count = 0
    pending_failed = False
    for outcome in ordered:
        if outcome.result not in {CIResult.PASSED, CIResult.FAILED}:
            continue
        if outcome.result == CIResult.FAILED:
            pending_failed = True
            continue
        if pending_failed:
            count += 1
            pending_failed = False
    return count


def _try_pair(
    mirror: RepoMirror,
    repository: RepositoryKey,
    org_id: str,
    repo: str,
    branch: str,
    failed: CIOutcome,
    fixed: CIOutcome,
    completions_by_commit: dict[CommitKey, list[str]],
    policy: RecoveryPolicy,
    provenance: Provenance,
) -> tuple[RecoverySample | None, str | None, int | None]:
    """One candidate (FAILED, PASSED) pair through the ancestry guard, the
    diff extraction, and the size cap. Returns the sample or skip reason plus
    the changed-line count when the diff reached the size gate — never raises
    out of the derivation for an ordinary skip."""
    failed_sha, fixed_sha = failed.commit_sha, fixed.commit_sha
    if failed_sha == fixed_sha:
        # The same workflow re-run to green on the identical commit (a flaky
        # rerun) — no diff exists to recover from, so this is not a recovery
        # pair, and spending the FAILED on it is correct: the rerun proved
        # the commit was never really red.
        logger.debug(
            "recovery_pair_same_commit",
            extra={"repo": repo, "branch": branch, "commit": failed_sha},
        )
        return None, "same_commit", None
    try:
        is_ancestor = mirror.is_ancestor(failed_sha, fixed_sha)
    except MirrorError as exc:
        logger.warning(
            "recovery_ancestry_check_failed",
            extra={
                "repo": repo,
                "branch": branch,
                "failed_commit": failed_sha,
                "fixed_commit": fixed_sha,
                "error": str(exc),
            },
        )
        return None, "ancestry_check_failed", None
    if not is_ancestor:
        # Force-push, reordered webhook delivery, or branch-name reuse: the
        # "fixed" commit doesn't actually descend from the "failed" one.
        logger.debug(
            "recovery_pair_not_ancestor",
            extra={
                "repo": repo,
                "branch": branch,
                "failed_commit": failed_sha,
                "fixed_commit": fixed_sha,
            },
        )
        return None, "not_ancestor", None

    try:
        diff = mirror.diff_range(failed_sha, fixed_sha)
    except MirrorError as exc:
        logger.warning(
            "recovery_diff_unavailable",
            extra={
                "repo": repo,
                "branch": branch,
                "failed_commit": failed_sha,
                "fixed_commit": fixed_sha,
                "error": str(exc),
            },
        )
        return None, "diff_unavailable", None

    line_count = _diff_line_count(diff)
    if line_count > policy.max_recovery_diff_lines:
        logger.debug(
            "recovery_diff_oversized",
            extra={
                "repo": repo,
                "branch": branch,
                "failed_commit": failed_sha,
                "fixed_commit": fixed_sha,
                "lines": line_count,
                "max_lines": policy.max_recovery_diff_lines,
            },
        )
        return None, "diff_oversized", line_count

    return (
        RecoverySample(
            org_id=org_id,
            repo=repo,
            branch=branch,
            workflow_name=failed.workflow_name,
            workflow_path=failed.workflow_path,
            failed_commit_sha=failed_sha,
            fixed_commit_sha=fixed_sha,
            failed_outcome_id=failed.outcome_id,
            fixed_outcome_id=fixed.outcome_id,
            recovery_diff=diff,
            failed_inference_call_ids=list(
                completions_by_commit.get(CommitKey(repository, failed_sha), [])
            ),
            fixed_inference_call_ids=list(
                completions_by_commit.get(CommitKey(repository, fixed_sha), [])
            ),
            provenance=provenance,
            repository_identity=getattr(repository, "identity", None),
        ),
        None,
        line_count,
    )


def _diff_line_count(diff: str) -> int:
    """Changed lines in a unified diff: every ``+``/``-`` line inside a hunk
    — added-plus-removed, the same count ``git diff --shortstat`` reports as
    "insertions(+), deletions(-)". Hunk-aware rather than a bare prefix
    filter: the ``+++``/``---`` file headers only appear *outside* hunks, so
    removed content that itself begins ``--`` (an SQL comment) renders as
    ``---…`` inside a hunk and must still count."""
    count = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith(("+", "-")):
            count += 1
    return count


def _attribution_evidence_by_commit(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: AttributionPolicy,
    *,
    policy_digest: str | None,
    repository_context: RepositoryContext,
    skipped: Counter[str],
) -> dict[CommitKey, tuple[RecoveryAttributionEvidence, ...]]:
    """Enrich within the pair's qualified commit namespace and exact boundary."""
    observations = store.read_session_commit_observations(
        org_id, as_of=repository_context.as_of
    )
    binding_result = bind_session_commit_keys_result(
        observations,
        org_id,
        as_of=repository_context.as_of,
        repository_context=repository_context,
    )
    skipped.update(binding_result.skipped)
    bindings = binding_result.bindings
    grouped: dict[CommitKey, dict[tuple[str, str], set[AttributionSource]]] = {}
    for item in derive_attributions(
        store,
        mirrors,
        org_id,
        policy,
        policy_digest=policy_digest,
        repository_context=repository_context,
        as_of=repository_context.as_of,
    ):
        key = repository_context.commit_key(
            org_id,
            item.repo,
            item.commit_sha,
            repository_identity=item.repository_identity,
        )
        if key is None:
            continue  # Attribution already reports its declined source.
        grouped.setdefault(key, {}).setdefault(
            (item.inference_call_id, item.session_id), set()
        ).add(item.attribution_source)
    return {
        key: tuple(
            RecoveryAttributionEvidence(
                inference_call_id=call_id,
                session_id=session_id,
                attribution_sources=tuple(
                    sorted(sources, key=lambda value: value.value)
                ),
                session_commit_observation_ids=tuple(
                    sorted(
                        {
                            item.observation_id
                            for item in bindings.get((key, session_id), ())
                        }
                    )
                ),
            )
            for (call_id, session_id), sources in sorted(grouped[key].items())
        )
        for key in sorted(grouped, key=commit_sort_key)
    }

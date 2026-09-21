# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure attempt-aware resolution of immutable CI outcome facts."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC
from typing import Literal

from sediment_core import (
    BranchName,
    CIOutcome,
    CIProvider,
    CIResult,
    CommitSha,
    NonEmptyId,
    OrgId,
    RepoSlug,
    WorkflowName,
)
from sediment_core.store import CIOutcomeProjection

from .attachment import index_ci_outcomes_by_commit_key_result
from .provenance import Provenance
from .repository_identity import (
    REPOSITORY_IDENTITY_SKIP_REASONS,
    CommitKey,
    IdentifiedRepositoryKey,
    RepositoryContext,
    RepositoryIdentity,
    RepositoryIdentitySkipReason,
    commit_sort_key,
)

_VERDICTS = frozenset({CIResult.PASSED, CIResult.FAILED})

CIResolutionSkipReason = (
    Literal[
        "conflicting_run_identity",
        "ambiguous_workflow_verdicts",
    ]
    | RepositoryIdentitySkipReason
)
CI_RESOLUTION_SKIP_REASONS: tuple[CIResolutionSkipReason, ...] = (
    "conflicting_run_identity",
    "ambiguous_workflow_verdicts",
    *sorted(REPOSITORY_IDENTITY_SKIP_REASONS),
)


@dataclass(frozen=True)
class CIResolutionPolicy:
    """Versioned reliability policy for CI attempt resolution."""

    clean_reliability: float = 1.0
    suspected_flake_reliability: float = 0.0
    non_verdict_reliability: float = 1.0
    policy_version: str = "2"

    def __post_init__(self) -> None:
        for name in (
            "clean_reliability",
            "suspected_flake_reliability",
            "non_verdict_reliability",
        ):
            value = getattr(self, name)
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise ValueError(f"CIResolutionPolicy.{name} must be a number")
            if not 0.0 <= value <= 1.0:
                raise ValueError(
                    f"CIResolutionPolicy.{name} must be in [0.0, 1.0] (got {value})"
                )
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise ValueError("CIResolutionPolicy.policy_version must be non-empty")

    @property
    def digest(self) -> str:
        """Return a stable digest of every resolved policy value."""

        encoded = json.dumps(
            asdict(self), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CIWorkflowResolution:
    """One provider run lineage after ordering its observable attempts."""

    provider: CIProvider
    run_id: NonEmptyId
    workflow_id: NonEmptyId | None
    workflow_name: WorkflowName
    workflow_path: WorkflowName | None
    branch: BranchName
    verdict: CIResult | None
    reliability: float | None
    suspected_flake: bool
    source_outcome_ids: tuple[NonEmptyId, ...]
    verdict_outcome_id: NonEmptyId | None
    non_verdict_outcome_ids: tuple[NonEmptyId, ...]


@dataclass(frozen=True)
class CIResolution:
    """One commit's aggregate CI verdict, reliability, and exact evidence ids."""

    org_id: OrgId
    repo: RepoSlug
    commit_sha: CommitSha
    verdict: CIResult | None
    reliability: float | None
    suspected_flake: bool
    workflow_resolutions: tuple[CIWorkflowResolution, ...]
    source_outcome_ids: tuple[NonEmptyId, ...]
    verdict_outcome_ids: tuple[NonEmptyId, ...]
    non_verdict_outcome_ids: tuple[NonEmptyId, ...]
    provenance: Provenance
    repository_identity: RepositoryIdentity | None = None


@dataclass
class CIResolutionResult:
    """Commit resolutions and closed losses.

    Repository reasons count source outcomes, conflicting identities count runs,
    and ambiguous verdicts count commits. These units must not be added together.
    """

    resolutions: list[CIResolution] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    conflicting_commit_keys: frozenset[CommitKey] = field(default_factory=frozenset)
    outcomes_by_commit: dict[CommitKey, tuple[CIOutcome | CIOutcomeProjection, ...]] = (
        field(default_factory=dict)
    )


def derive_ci_resolutions(
    outcomes: Iterable[CIOutcome | CIOutcomeProjection],
    policy: CIResolutionPolicy | None = None,
    *,
    quarantine_revision: int = 0,
    repository_context: RepositoryContext | None = None,
) -> list[CIResolution]:
    """Resolve CI outcomes and return only the commit resolutions."""

    return derive_ci_resolution_result(
        outcomes,
        policy,
        quarantine_revision=quarantine_revision,
        repository_context=repository_context,
    ).resolutions


def derive_ci_resolution_result(
    outcomes: Iterable[CIOutcome | CIOutcomeProjection],
    policy: CIResolutionPolicy | None = None,
    *,
    quarantine_revision: int = 0,
    repository_context: RepositoryContext | None = None,
) -> CIResolutionResult:
    """Resolve attempts within captured repository identity and forge instance.

    Without a repository context, only legacy islands can resolve. A declined
    source prevents its run from becoming a falsely clean partial lineage.
    """

    policy = policy or CIResolutionPolicy()
    provenance = Provenance(
        policy_version=policy.policy_version,
        quarantine_revision=quarantine_revision,
        policy_digest=policy.digest,
    )
    result = CIResolutionResult()

    outcomes = tuple(outcomes)
    if repository_context is not None:
        outcomes = tuple(
            outcome
            for outcome in outcomes
            if outcome.org_id == repository_context.org_id
            and outcome.captured_at.astimezone(UTC) <= repository_context.as_of
        )
    indexed = index_ci_outcomes_by_commit_key_result(
        outcomes,
        repository_context=repository_context,
    )
    result.skipped.update(indexed.skipped)
    keys_by_source = defaultdict(set)
    for commit, sources in indexed.outcomes_by_commit.items():
        for outcome in sources:
            keys_by_source[(outcome.org_id, outcome.outcome_id)].add(commit)
    by_run = defaultdict(list)
    incomplete_runs = set()
    for outcome in outcomes:
        run_key = _run_key(outcome)
        by_run[run_key].append(outcome)
        source = (outcome.org_id, outcome.outcome_id)
        if source in indexed.declined_sources or not keys_by_source[source]:
            incomplete_runs.add(run_key)

    by_commit: dict[CommitKey, list[CIWorkflowResolution]] = defaultdict(list)
    for run_key in sorted(by_run):
        if run_key in incomplete_runs:
            continue
        run_outcomes = by_run[run_key]
        keys = set().union(
            *(keys_by_source[(item.org_id, item.outcome_id)] for item in run_outcomes)
        )
        identity = _run_identity(run_outcomes)
        if identity is None or len(keys) != 1:
            result.skipped["conflicting_run_identity"] += 1
            result.conflicting_commit_keys |= keys
            continue
        commit = next(iter(keys))
        # Preserve original Facts from complete consistent runs before any
        # consumer narrows by commit. A non-verdict or flake remains evidence.
        result.outcomes_by_commit[commit] = tuple(
            sorted(
                (*result.outcomes_by_commit.get(commit, ()), *run_outcomes),
                key=lambda row: (row.captured_at.astimezone(UTC), row.outcome_id),
            )
        )
        branch, workflow_id, workflow_name, workflow_path = identity
        resolution = _resolve_workflow(
            run_outcomes,
            policy,
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            workflow_path=workflow_path,
            branch=branch,
        )
        by_commit[commit].append(resolution)

    for commit in sorted(by_commit, key=commit_sort_key):
        repository = commit.repository
        repository_identity = None
        if isinstance(repository, IdentifiedRepositoryKey):
            repository_identity = repository.identity
            repo = repository_context.repo_for(repository)
        else:
            repo = repository.repo
        workflows = tuple(sorted(by_commit[commit], key=_workflow_key))
        verdicts = {item.verdict for item in workflows if item.verdict is not None}
        if len(verdicts) > 1:
            verdict = None
            reliability = None
            result.skipped["ambiguous_workflow_verdicts"] += 1
        elif verdicts:
            verdict = next(iter(verdicts))
            reliability = min(
                item.reliability for item in workflows if item.reliability is not None
            )
        else:
            verdict = None
            reliability = None

        result.resolutions.append(
            CIResolution(
                org_id=repository.org_id,
                repo=repo,
                commit_sha=commit.commit_sha,
                verdict=verdict,
                reliability=reliability,
                suspected_flake=any(item.suspected_flake for item in workflows),
                workflow_resolutions=workflows,
                source_outcome_ids=tuple(
                    sorted(
                        outcome_id
                        for item in workflows
                        for outcome_id in item.source_outcome_ids
                    )
                ),
                verdict_outcome_ids=tuple(
                    sorted(
                        item.verdict_outcome_id
                        for item in workflows
                        if item.verdict_outcome_id is not None
                    )
                ),
                non_verdict_outcome_ids=tuple(
                    sorted(
                        outcome_id
                        for item in workflows
                        for outcome_id in item.non_verdict_outcome_ids
                    )
                ),
                provenance=provenance,
                repository_identity=repository_identity,
            )
        )
    return result


def _run_identity(
    outcomes: list[CIOutcome | CIOutcomeProjection],
) -> (
    tuple[
        BranchName,
        NonEmptyId | None,
        WorkflowName,
        WorkflowName | None,
    ]
    | None
):
    branches = {outcome.branch for outcome in outcomes}
    workflow_ids = {outcome.workflow_id for outcome in outcomes if outcome.workflow_id}
    workflow_names = {
        outcome.workflow_name for outcome in outcomes if outcome.workflow_name
    }
    workflow_paths = {
        outcome.workflow_path for outcome in outcomes if outcome.workflow_path
    }
    if (
        len(branches) != 1
        or len(workflow_ids) > 1
        or len(workflow_names) > 1
        or len(workflow_paths) > 1
    ):
        return None
    by_attempt = defaultdict(set)
    for outcome in outcomes:
        by_attempt[outcome.run_attempt or 0].add(outcome.result)
    if any(len(results) > 1 for results in by_attempt.values()):
        return None
    return (
        next(iter(branches)),
        next(iter(workflow_ids), None),
        next(iter(workflow_names), ""),
        next(iter(workflow_paths), None),
    )


def _run_key(outcome: CIOutcome | CIOutcomeProjection) -> tuple[str, ...]:
    # CI provider and repository provider are distinct captured namespaces. ID
    # stays outside this key: one run cannot claim two repository lifetimes.
    if outcome.repository_provider is not None:
        return (
            outcome.org_id,
            outcome.provider.value,
            "identified",
            str(outcome.repository_provider),
            outcome.repository_host or "",
            outcome.run_id,
        )
    return (outcome.org_id, outcome.provider.value, "legacy", "", "", outcome.run_id)


def _resolve_workflow(
    outcomes: list[CIOutcome | CIOutcomeProjection],
    policy: CIResolutionPolicy,
    *,
    workflow_id: NonEmptyId | None,
    workflow_name: WorkflowName,
    workflow_path: WorkflowName | None,
    branch: BranchName,
) -> CIWorkflowResolution:
    # A same-attempt contradiction was declined above. The ID only selects a
    # stable evidence representative among equivalent copies of that attempt.
    ordered = sorted(
        outcomes, key=lambda outcome: (outcome.run_attempt or 0, outcome.outcome_id)
    )
    verdict_outcomes = [outcome for outcome in ordered if outcome.result in _VERDICTS]
    verdict = verdict_outcomes[-1].result if verdict_outcomes else None
    observed_verdicts = {outcome.result for outcome in verdict_outcomes}
    suspected_flake = len(observed_verdicts) > 1
    non_verdicts = [outcome for outcome in ordered if outcome.result not in _VERDICTS]
    reliability: float | None = None
    if verdict is not None:
        reliability = (
            policy.suspected_flake_reliability
            if suspected_flake
            else policy.clean_reliability
        )
        if non_verdicts:
            reliability = min(reliability, policy.non_verdict_reliability)
    elif non_verdicts:
        reliability = policy.non_verdict_reliability
    first = outcomes[0]
    return CIWorkflowResolution(
        provider=first.provider,
        run_id=first.run_id,
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        branch=branch,
        verdict=verdict,
        reliability=reliability,
        suspected_flake=suspected_flake,
        source_outcome_ids=tuple(sorted(outcome.outcome_id for outcome in outcomes)),
        verdict_outcome_id=(
            verdict_outcomes[-1].outcome_id if verdict_outcomes else None
        ),
        non_verdict_outcome_ids=tuple(
            sorted(outcome.outcome_id for outcome in non_verdicts)
        ),
    )


def _workflow_key(resolution: CIWorkflowResolution) -> tuple:
    return (
        resolution.provider.value,
        resolution.workflow_id or "",
        resolution.workflow_path or "",
        resolution.workflow_name,
        resolution.run_id,
    )

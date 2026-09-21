# SPDX-License-Identifier: AGPL-3.0-or-later
"""Coverage-aware operational lifecycle report for accepted agent work."""

from __future__ import annotations

from sediment_derive.session_commit import bind_session_commit_keys_result
from sediment_derive.attachment import index_ci_outcomes_by_commit_key_result
from sediment_derive.repository_identity import (
    CommitKey,
    RepositoryContext,
    RepositoryIdentity,
    RepositoryIdentitySkipReason,
    IdentifiedRepositoryKey,
    repository_read_key,
    repository_sort_key,
)
from sediment_derive.repository_context import read_repository_context
from datetime import UTC, datetime

from collections import Counter, defaultdict
from dataclasses import dataclass, field, fields
from datetime import timedelta
from typing import Annotated, Literal, get_args

from pydantic import Field

from sediment_core import (
    AgentHarness,
    CIResult,
    FactStore,
    OrgId,
    RepoSlug,
)
from sediment_derive import (
    AbandonmentPolicy,
    AttributionPolicy,
    CIResolutionPolicy,
    FatePolicy,
    MergeRetentionPolicy,
    MirrorManager,
    Provenance,
    derive_abandonment,
    derive_attribution_result,
    derive_ci_resolution_result,
    derive_fate_result,
    derive_merge_retention_result,
    join_decisions_by_call_id_result,
    DecisionAttachmentSkipReason,
    DiffSkipReason,
)
from sediment_derive.attribution import tokenize
from sediment_derive.inference_call import render_scoring_text
from sediment_derive.rollout import CommitRef
from sediment_derive.survival_scoring import four_gram_containment

from .merge_retention_report import build_merge_retention_report
from .operational_scope import OperationalReportScope

_SUPPORTING_FACT_LIMIT = 50_000

CoverageQualification = Literal["partial_pull_request_history"]

StratumDimension = Literal["model", "repository", "workflow", "agent_harness"]
StratumSkipReason = Literal[
    "missing_model",
    "multiple_agent_harnesses",
    "missing_repository",
    "multiple_repositories",
    "missing_workflow",
    "multiple_workflows",
]
ProgressionSkipReason = Literal[
    DecisionAttachmentSkipReason,
    "session_commit_unobserved",
    "attribution_unavailable",
    "pull_request_membership_unavailable",
    "ci_missing",
    "ci_non_verdict",
    "ambiguous_ci_verdict",
]
FateSkipReason = Literal["scorer_error", "invalid_score"]
AbandonmentSkipReason = Literal[
    RepositoryIdentitySkipReason,
    "session_commit_unobserved",
    "no_accepted_decision",
    "reached_a_commit",
    "within_grace_horizon",
    "attribution_unavailable",
]
MergeMembershipReason = Literal[
    "attribution_without_merge",
    "ambiguous_pr_membership",
    "ancestry_check_failed",
]
MergeScoringSkipReason = Literal[
    RepositoryIdentitySkipReason,
    "session_commit_unobserved",
    DiffSkipReason,
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
]
ReworkName = Literal[
    "explicit_rejects",
    "retry_linkages",
    "modified_or_deleted_edits",
    "external_line_changes",
    "abandoned_sessions",
    "failed_ci_resolutions",
]
ReworkGrain = Literal[
    "developer_decision",
    "retry_linkage",
    "edit_observation",
    "edit_observation_with_known_external_counts",
    "accepted_session",
    "ci_resolution",
]
ExamplePopulation = Literal[
    "accepted",
    "attributed",
    "pull_request_membership",
    "ci_failed",
    "unmodified",
    "partially_modified",
    "deleted",
    "joined",
    "scored",
    "committed",
    "abandoned",
    "in_flight",
    "attribution_unavailable",
]
CoverageMissingReason = Literal[
    RepositoryIdentitySkipReason,
    DiffSkipReason,
    "decision_org_mismatch",
    "decision_session_mismatch",
    "session_commit_unobserved",
    "missing_decision_call_id",
    "unmatched_decision_call_id",
    "fate_unavailable",
    "attribution_unavailable",
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
]
CoverageUnsupportedReason = Literal[
    "cursor_human_explicit_inference_capture",
    "cursor_edit_observations",
]
CoverageAmbiguousReason = Literal[
    "ambiguous_decision_call_id",
    "ambiguous_ci_verdict",
    "ambiguous_pr_membership",
]
NonNegativeInt = Annotated[int, Field(ge=0)]
UnitRate = Annotated[float, Field(ge=0.0, le=1.0)]
PositiveInt = Annotated[int, Field(ge=1)]


def _validate_metrics(value: object) -> None:
    """Validate count/rate fields on frozen serialized metric objects."""

    for item in fields(value):
        field_value = getattr(value, item.name)
        if item.name in {
            "rate",
            "initial_rate",
            "fate_rate",
            "membership_rate",
            "scoring_rate",
            "mean",
            "median",
            "p10",
            "p90",
            "threshold",
        }:
            if field_value is not None and not 0.0 <= field_value <= 1.0:
                raise ValueError(f"{item.name} must be in [0, 1]")
        elif isinstance(field_value, int) and not isinstance(field_value, bool):
            if field_value < 0:
                raise ValueError(f"{item.name} must be non-negative")
        elif item.name == "repository_skipped":
            for counts in field_value.values():
                _validate_keys(
                    counts, RepositoryIdentitySkipReason, "repository skip reason"
                )
                if any(
                    type(count) is not int or count < 0 for count in counts.values()
                ):
                    raise ValueError(
                        "repository skip counts must be non-negative integers"
                    )
        elif isinstance(field_value, dict):
            if any(
                type(count) is not int or count < 0 for count in field_value.values()
            ):
                raise ValueError(f"{item.name} counts must be non-negative integers")


def _validate_keys(values: dict[str, int], vocabulary: object, name: str) -> None:
    unknown = set(values) - set(get_args(vocabulary))
    if unknown:
        raise ValueError(f"unknown {name}: {sorted(unknown)}")


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


@dataclass(frozen=True)
class LifecycleReportPolicy:
    """Presentation policy for the lifecycle artifact."""

    example_session_limit: PositiveInt = 10
    policy_version: str = "3"

    def __post_init__(self) -> None:
        if (
            type(self.example_session_limit) is not int
            or self.example_session_limit < 1
        ):
            raise ValueError("example_session_limit must be a positive integer")
        if not self.policy_version.strip():
            raise ValueError("policy_version must be non-empty")


@dataclass(frozen=True)
class LifecycleStage:
    """One count with its immediate and initial denominators."""

    count: NonNegativeInt
    denominator: NonNegativeInt
    rate: UnitRate | None
    initial_denominator: NonNegativeInt
    initial_rate: UnitRate | None

    def __post_init__(self) -> None:
        _validate_metrics(self)


@dataclass(frozen=True)
class LifecycleCoverage:
    """Observed, missing, unsupported, and ambiguous evidence populations."""

    observed: NonNegativeInt
    missing: dict[CoverageMissingReason, NonNegativeInt] = field(default_factory=dict)
    unsupported_by_integration: dict[CoverageUnsupportedReason, NonNegativeInt] = field(
        default_factory=dict
    )
    ambiguous: dict[CoverageAmbiguousReason, NonNegativeInt] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        _validate_metrics(self)
        _validate_keys(self.missing, CoverageMissingReason, "coverage missing reason")
        _validate_keys(
            self.unsupported_by_integration,
            CoverageUnsupportedReason,
            "coverage unsupported reason",
        )
        _validate_keys(
            self.ambiguous, CoverageAmbiguousReason, "coverage ambiguous reason"
        )


@dataclass(frozen=True)
class LifecycleRetentionDistribution:
    count: NonNegativeInt
    mean: UnitRate | None
    median: UnitRate | None
    p10: UnitRate | None
    p90: UnitRate | None

    def __post_init__(self) -> None:
        _validate_metrics(self)


@dataclass(frozen=True)
class LifecycleRetentionThreshold:
    threshold: UnitRate
    below: NonNegativeInt
    total: NonNegativeInt
    rate: UnitRate | None

    def __post_init__(self) -> None:
        _validate_metrics(self)


@dataclass(frozen=True)
class LifecycleRetentionScoreSummary:
    distribution: LifecycleRetentionDistribution
    thresholds: tuple[LifecycleRetentionThreshold, ...]


@dataclass(frozen=True)
class SessionExamples:
    """Deterministically bounded Session identifiers for one population."""

    population: ExamplePopulation
    session_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.population not in get_args(ExamplePopulation):
            raise ValueError("unknown example population")
        if tuple(sorted(set(self.session_ids))) != self.session_ids:
            raise ValueError("Session examples must be sorted and unique")


@dataclass(frozen=True)
class AcceptedWorkProgression:
    accepted_calls: NonNegativeInt
    attributed: LifecycleStage
    pull_request_membership: LifecycleStage
    ci_linked: LifecycleStage
    ci_verdict: LifecycleStage
    ci_passed: NonNegativeInt
    ci_failed: NonNegativeInt
    skips: dict[ProgressionSkipReason, NonNegativeInt]
    coverage: LifecycleCoverage
    examples: tuple[SessionExamples, ...]

    def __post_init__(self) -> None:
        _validate_metrics(self)
        _validate_keys(self.skips, ProgressionSkipReason, "progression skip reason")


@dataclass(frozen=True)
class EditRetentionPanel:
    observations: NonNegativeInt
    eligible: NonNegativeInt
    derived_fates: NonNegativeInt
    unmodified: NonNegativeInt
    partially_modified: NonNegativeInt
    deleted: NonNegativeInt
    known_external_line_counts: NonNegativeInt
    external_lines_added: NonNegativeInt
    external_lines_removed: NonNegativeInt
    fate_rate: UnitRate | None
    skips: dict[FateSkipReason, NonNegativeInt]
    coverage: LifecycleCoverage
    examples: tuple[SessionExamples, ...]

    def __post_init__(self) -> None:
        _validate_metrics(self)
        _validate_keys(self.skips, FateSkipReason, "Fate skip reason")


@dataclass(frozen=True)
class MergeDurabilityPanel:
    coverage_qualification: CoverageQualification
    attributed_file_candidates: NonNegativeInt
    unique_pull_request_membership: NonNegativeInt
    scored_rows: NonNegativeInt
    membership_rate: UnitRate | None
    scoring_rate: UnitRate | None
    membership: dict[MergeMembershipReason, NonNegativeInt]
    scoring_skips: dict[MergeScoringSkipReason, NonNegativeInt]
    head: LifecycleRetentionScoreSummary
    merge: LifecycleRetentionScoreSummary
    coverage: LifecycleCoverage
    examples: tuple[SessionExamples, ...]

    def __post_init__(self) -> None:
        _validate_metrics(self)
        if self.coverage_qualification != "partial_pull_request_history":
            raise ValueError("unknown merge-durability coverage qualification")
        _validate_keys(self.membership, MergeMembershipReason, "membership reason")
        _validate_keys(self.scoring_skips, MergeScoringSkipReason, "scoring reason")


@dataclass(frozen=True)
class SessionAttritionPanel:
    eligible_sessions: NonNegativeInt
    committed: NonNegativeInt
    abandoned: NonNegativeInt
    in_flight: NonNegativeInt
    attribution_unavailable: NonNegativeInt
    skips: dict[AbandonmentSkipReason, NonNegativeInt]
    coverage: LifecycleCoverage
    examples: tuple[SessionExamples, ...]

    def __post_init__(self) -> None:
        _validate_metrics(self)
        _validate_keys(self.skips, AbandonmentSkipReason, "abandonment skip reason")


@dataclass(frozen=True)
class ReworkComponent:
    name: ReworkName
    grain: ReworkGrain
    count: NonNegativeInt
    denominator: NonNegativeInt
    rate: UnitRate | None

    def __post_init__(self) -> None:
        _validate_metrics(self)
        if self.name not in get_args(ReworkName) or self.grain not in get_args(
            ReworkGrain
        ):
            raise ValueError("unknown rework component contract value")


@dataclass(frozen=True)
class LifecycleStratum:
    """A supported unique identity and its accepted-call count."""

    dimension: StratumDimension
    value: str
    accepted_calls: NonNegativeInt
    repo: RepoSlug | None = None
    repository_identity: RepositoryIdentity | None = None

    def __post_init__(self) -> None:
        _validate_metrics(self)
        if self.dimension not in get_args(StratumDimension):
            raise ValueError("unknown lifecycle stratum dimension")


@dataclass(frozen=True)
class LifecycleProvenance:
    lifecycle: Provenance
    attribution: Provenance
    abandonment: Provenance
    fate: Provenance
    ci_resolution: Provenance
    merge_retention: Provenance


@dataclass(frozen=True)
class AcceptedWorkLifecycleReport:
    org_id: OrgId
    accepted_work: AcceptedWorkProgression
    edit_retention: EditRetentionPanel
    merge_durability: MergeDurabilityPanel
    session_attrition: SessionAttritionPanel
    rework: tuple[ReworkComponent, ...]
    strata: tuple[LifecycleStratum, ...]
    stratum_skips: dict[StratumSkipReason, NonNegativeInt]
    policy: LifecycleReportPolicy
    provenance: LifecycleProvenance
    repository_skipped: dict[
        str, dict[RepositoryIdentitySkipReason, NonNegativeInt]
    ] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_metrics(self)
        _validate_keys(self.stratum_skips, StratumSkipReason, "stratum skip reason")


def _stage(count: int, denominator: int, initial: int) -> LifecycleStage:
    return LifecycleStage(
        count, denominator, _rate(count, denominator), initial, _rate(count, initial)
    )


def _examples(
    name: ExamplePopulation, sessions: set[str], limit: int
) -> SessionExamples:
    return SessionExamples(name, tuple(sorted(sessions)[:limit]))


def _call_ci_status(
    attributed_commit_keys: set[CommitKey],
    ci_evidence_keys: set[CommitKey],
    conflicting_run_keys: set[CommitKey],
    resolutions: dict[CommitKey, object],
) -> Literal["missing", "non_verdict", "ambiguous", "passed", "failed"]:
    """Resolve one call conservatively while preserving raw CI linkage."""

    if not attributed_commit_keys & ci_evidence_keys:
        return "missing"
    if attributed_commit_keys & conflicting_run_keys:
        return "ambiguous"
    linked = [resolutions[key] for key in attributed_commit_keys if key in resolutions]
    if any(
        len(
            {
                workflow.verdict
                for workflow in resolution.workflow_resolutions
                if workflow.verdict is not None
            }
        )
        > 1
        for resolution in linked
    ):
        return "ambiguous"
    verdicts = [resolution.verdict for resolution in linked]
    if any(value == CIResult.FAILED for value in verdicts):
        return "failed"
    if any(value is not None for value in verdicts) and all(
        value in (None, CIResult.PASSED) for value in verdicts
    ):
        return "passed"
    return "non_verdict"


def _retention_summary(summary: object) -> LifecycleRetentionScoreSummary:
    distribution = summary.distribution
    return LifecycleRetentionScoreSummary(
        distribution=LifecycleRetentionDistribution(
            count=distribution.count,
            mean=distribution.mean,
            median=distribution.median,
            p10=distribution.p10,
            p90=distribution.p90,
        ),
        thresholds=tuple(
            LifecycleRetentionThreshold(
                threshold=item.threshold,
                below=item.below,
                total=item.total,
                rate=item.rate,
            )
            for item in summary.thresholds
        ),
    )


def generate_accepted_work_lifecycle_report(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    *,
    policy: LifecycleReportPolicy | None = None,
    attribution_policy: AttributionPolicy | None = None,
    abandonment_policy: AbandonmentPolicy | None = None,
    fate_policy: FatePolicy | None = None,
    ci_policy: CIResolutionPolicy | None = None,
    merge_retention_policy: MergeRetentionPolicy | None = None,
    scope: OperationalReportScope | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> AcceptedWorkLifecycleReport:
    """Assemble one report from one stable Fact and mirror snapshot."""

    policy = policy or LifecycleReportPolicy()
    attribution_policy = attribution_policy or AttributionPolicy()
    abandonment_policy = abandonment_policy or AbandonmentPolicy()
    fate_policy = fate_policy or FatePolicy()
    ci_policy = ci_policy or CIResolutionPolicy()
    merge_retention_policy = merge_retention_policy or MergeRetentionPolicy()

    boundary = (
        scope.as_of
        if scope is not None
        else as_of
        or (
            repository_context.as_of
            if repository_context is not None
            else datetime.now(UTC)
        )
    )
    if boundary.utcoffset() is None or (as_of is not None and as_of != boundary):
        raise ValueError("lifecycle boundary must be aware and match scope")
    boundary = boundary.astimezone(UTC)
    with store.read_snapshot() as snapshot:
        repository_context = repository_context or read_repository_context(
            snapshot, org_id, as_of=boundary
        )
        if repository_context.org_id != org_id or repository_context.as_of != boundary:
            raise ValueError(
                "repository context must match report organization and boundary"
            )
        # Run consistency must see sibling outcomes outside the selected commit cohort.
        ci_population = snapshot.read_ci_outcome_projections(
            org_id, captured_through=boundary, limit=_SUPPORTING_FACT_LIMIT
        )
        if scope is None:
            pushes = snapshot.read_pushes(org_id, captured_through=boundary)
            merges = snapshot.read_pull_request_merges(
                org_id, captured_through=boundary
            )
            calls = snapshot.read_report_inference_calls(
                org_id, observed_through=boundary
            )
            decisions = snapshot.read_decisions(org_id, captured_through=boundary)
            observations = snapshot.read_edit_observations(
                org_id, captured_through=boundary
            )
            retries = snapshot.read_retry_linkages(org_id, captured_through=boundary)
            ci_outcomes = snapshot.read_ci_outcomes(org_id, captured_through=boundary)
            revisions = None
            session_commits = None
            attribution_candidates = None
            note_sessions_by_commit = None
            observations_by_commit = snapshot.read_session_commit_observations(
                org_id, as_of=boundary
            )
        else:
            summaries = snapshot.read_inference_call_summaries(
                org_id,
                observed_between=(scope.cohort_start, scope.cohort_end),
                limit=scope.max_inference_calls,
            )
            call_ids = {item.inference_call_id for item in summaries}
            calls = snapshot.read_report_inference_calls(
                org_id, inference_call_ids=call_ids
            )
            session_ids = {item.session_id for item in summaries}
            decisions = snapshot.read_decisions(
                org_id,
                captured_through=scope.as_of,
                session_ids=session_ids,
                limit=_SUPPORTING_FACT_LIMIT,
            )
            observations = snapshot.read_edit_observations(
                org_id,
                captured_through=scope.as_of,
                session_ids=session_ids,
                limit=_SUPPORTING_FACT_LIMIT,
            )
            retries = snapshot.read_retry_linkages(
                org_id,
                captured_through=scope.as_of,
                session_ids=session_ids,
                limit=_SUPPORTING_FACT_LIMIT,
            )
            pushes = snapshot.read_pushes(
                org_id,
                captured_between=(
                    scope.cohort_start
                    - timedelta(
                        minutes=attribution_policy.post_push_grace_period_minutes
                    ),
                    scope.as_of + timedelta(microseconds=1),
                ),
                limit=_SUPPORTING_FACT_LIMIT,
            )
            observations_by_commit = snapshot.read_session_commit_observations(
                org_id,
                as_of=scope.as_of,
                session_ids=session_ids,
                limit=_SUPPORTING_FACT_LIMIT,
            )
            bindings = bind_session_commit_keys_result(
                observations_by_commit,
                org_id,
                as_of=boundary,
                repository_context=repository_context,
            )
            note_sessions = defaultdict(set)
            session_commit_sets: dict[str, set[CommitRef]] = defaultdict(set)
            for commit, session_id in bindings.bindings:
                note_sessions[commit].add(session_id)
                session_commit_sets[session_id].add(
                    CommitRef(
                        repo=repository_context.repo_for(commit.repository),
                        commit_sha=commit.commit_sha,
                        repository_identity=commit.repository.identity
                        if isinstance(commit.repository, IdentifiedRepositoryKey)
                        else None,
                    )
                )
            note_sessions_by_commit = {
                key: frozenset(value) for key, value in note_sessions.items()
            }
            session_commits = dict(session_commit_sets)
            attribution_candidates = [
                (call, tokenize(render_scoring_text(call))) for call in calls
            ]
            merges = snapshot.read_pull_request_merges(
                org_id,
                captured_through=scope.as_of,
                merged_through=scope.as_of,
                limit=_SUPPORTING_FACT_LIMIT,
            )
        decision_identities = (
            calls
            if scope is None
            else snapshot.read_inference_call_identities(
                org_id, observed_through=scope.as_of, limit=_SUPPORTING_FACT_LIMIT
            )
            if any(decision.call_id is not None for decision in decisions)
            else []
        )
        observation_boundary = boundary
        bindings = bind_session_commit_keys_result(
            observations_by_commit,
            org_id,
            as_of=boundary,
            repository_context=repository_context,
        )
        observed_bindings = bindings.bindings
        attachments = join_decisions_by_call_id_result(decision_identities, decisions)
        with mirrors.read_repository_snapshot(repository_context.repository_keys()):
            attribution = derive_attribution_result(
                snapshot,
                mirrors,
                org_id,
                attribution_policy,
                pushes=pushes if scope is not None else None,
                candidates=attribution_candidates,
                note_session_ids_by_commit=note_sessions_by_commit,
                repository_context=repository_context,
                as_of=boundary,
            )
            if scope is not None:
                attribution_keys = {
                    repository_context.commit_key(
                        item.org_id,
                        item.repo,
                        item.commit_sha,
                        repository_identity=item.repository_identity,
                    )
                    for item in attribution.attributions
                }
                attribution_repos = {
                    key.repository for key in attribution_keys if key is not None
                }
                merges = [
                    item
                    for item in merges
                    if repository_context.resolve_fact(item).key in attribution_repos
                ]
                repository_prs = {
                    (
                        repository_read_key(repository_context.resolve_fact(item).key),
                        item.pr_number,
                    )
                    for item in merges
                }
                revisions = snapshot.read_pull_request_revisions(
                    org_id,
                    captured_through=boundary,
                    repository_prs=repository_prs,
                    limit=_SUPPORTING_FACT_LIMIT,
                )
                ci_outcomes = snapshot.read_ci_outcomes(
                    org_id,
                    captured_through=boundary,
                    repository_commits={
                        (repository_read_key(key.repository), key.commit_sha)
                        for key in attribution_keys
                        if key is not None
                    },
                    limit=_SUPPORTING_FACT_LIMIT,
                )
            abandonment = derive_abandonment(
                snapshot,
                mirrors,
                org_id,
                abandonment_policy,
                attributions=attribution.attributions,
                decisions=decisions if scope is not None else None,
                pushes=pushes if scope is not None else None,
                completions=calls if scope is not None else None,
                as_of=boundary,
                repository_context=repository_context,
                session_commits=session_commits,
                session_commit_observations=observations_by_commit,
            )
            fate = derive_fate_result(
                observations,
                four_gram_containment,
                fate_policy,
                quarantine_revision=snapshot.quarantine_revision(org_id),
            )
            ci = derive_ci_resolution_result(
                ci_population,
                ci_policy,
                repository_context=repository_context,
                quarantine_revision=snapshot.quarantine_revision(org_id),
            )
            retention = derive_merge_retention_result(
                snapshot,
                mirrors,
                org_id,
                merge_retention_policy,
                attributions=attribution.attributions,
                merges=merges if scope is not None else None,
                revisions=revisions,
                ci_outcomes=ci_outcomes if scope is not None else None,
                session_commit_observations=observations_by_commit,
                as_of=observation_boundary,
                repository_context=repository_context,
            )
    call_by_id = {call.inference_call_id: call for call in calls}
    accepted_ids = {
        call_id
        for call_id, attached in attachments.decisions_by_completion.items()
        if call_id in call_by_id
        and any(item.accepted and item.explicit for item in attached)
    }

    def commit_key(item):
        return repository_context.commit_key(
            item.org_id,
            item.repo,
            item.commit_sha,
            repository_identity=item.repository_identity,
        )

    attrs_by_call: dict[str, list] = defaultdict(list)
    for item in attribution.attributions:
        if (commit_key(item), item.session_id) in observed_bindings:
            attrs_by_call[item.inference_call_id].append(item)
    attributed_ids = accepted_ids & attrs_by_call.keys()
    joined_ids = {
        item.inference_call_id
        for item in retention.membership_outcomes
        if item.status == "joined" and item.inference_call_id in accepted_ids
    }
    # Only the selected CI cohort contributes report counters; resolution above
    # still evaluates complete run lineages through the shared boundary.
    ci_index = index_ci_outcomes_by_commit_key_result(
        ci_outcomes, repository_context=repository_context
    )
    ci_evidence_keys = set(ci_index.outcomes_by_commit)
    resolutions = {
        commit_key(item): item
        for item in ci.resolutions
        if commit_key(item) in ci_evidence_keys
    }
    conflicting_run_keys = ci.conflicting_commit_keys & ci_evidence_keys
    linked_ids: set[str] = set()
    passed_ids: set[str] = set()
    failed_ids: set[str] = set()
    verdict_ambiguous: set[str] = set()
    non_verdict_ids: set[str] = set()
    for call_id in joined_ids:
        attributed_commit_keys = {commit_key(item) for item in attrs_by_call[call_id]}
        status = _call_ci_status(
            attributed_commit_keys,
            ci_evidence_keys,
            conflicting_run_keys,
            resolutions,
        )
        if status != "missing":
            linked_ids.add(call_id)
        if status == "ambiguous":
            verdict_ambiguous.add(call_id)
        elif status == "failed":
            failed_ids.add(call_id)
        elif status == "passed":
            passed_ids.add(call_id)
        elif status == "non_verdict":
            non_verdict_ids.add(call_id)
    verdict_ids = passed_ids | failed_ids

    session_for = {call_id: call_by_id[call_id].session_id for call_id in accepted_ids}
    cursor_decisions = [d for d in decisions if d.agent_harness == AgentHarness.CURSOR]
    progression_skips = Counter(attachments.skipped)
    unobserved_edges = {
        (commit_key(item), item.session_id)
        for item in attribution.attributions
        if item.inference_call_id in accepted_ids
        and (commit_key(item), item.session_id) not in observed_bindings
    }
    if unobserved_edges:
        progression_skips["session_commit_unobserved"] += len(unobserved_edges)
    progression_skips["attribution_unavailable"] += len(accepted_ids - attributed_ids)
    progression_skips["pull_request_membership_unavailable"] += len(
        attributed_ids - joined_ids
    )
    progression_skips["ci_missing"] += len(joined_ids - linked_ids)
    progression_skips["ci_non_verdict"] += len(non_verdict_ids)
    progression_skips["ambiguous_ci_verdict"] += len(verdict_ambiguous)
    accepted_panel = AcceptedWorkProgression(
        accepted_calls=len(accepted_ids),
        attributed=_stage(len(attributed_ids), len(accepted_ids), len(accepted_ids)),
        pull_request_membership=_stage(
            len(joined_ids), len(attributed_ids), len(accepted_ids)
        ),
        ci_linked=_stage(len(linked_ids), len(joined_ids), len(accepted_ids)),
        ci_verdict=_stage(len(verdict_ids), len(linked_ids), len(accepted_ids)),
        ci_passed=len(passed_ids),
        ci_failed=len(failed_ids),
        skips=dict(sorted((k, v) for k, v in progression_skips.items() if v)),
        coverage=LifecycleCoverage(
            observed=len(accepted_ids),
            missing={
                k: attachments.skipped[k]
                for k in get_args(CoverageMissingReason)
                if attachments.skipped[k]
            },
            unsupported_by_integration={
                "cursor_human_explicit_inference_capture": len(
                    {item.session_id for item in cursor_decisions}
                )
            }
            if cursor_decisions
            else {},
            ambiguous={
                k: v for k, v in progression_skips.items() if "ambiguous" in k and v
            },
        ),
        examples=tuple(
            _examples(
                name, {session_for[item] for item in ids}, policy.example_session_limit
            )
            for name, ids in (
                ("accepted", accepted_ids),
                ("attributed", set(attributed_ids)),
                ("pull_request_membership", joined_ids),
                ("ci_failed", failed_ids),
            )
        ),
    )

    fate_counts = Counter(item.fate.value for item in fate.fates)
    known_external = [
        item
        for item in observations
        if item.external_lines_added is not None
        and item.external_lines_removed is not None
    ]
    edit_panel = EditRetentionPanel(
        observations=len(observations),
        eligible=len(observations),
        derived_fates=len(fate.fates),
        unmodified=fate_counts["unmodified"],
        partially_modified=fate_counts["partially_modified"],
        deleted=fate_counts["deleted"],
        known_external_line_counts=len(known_external),
        external_lines_added=sum(
            item.external_lines_added or 0 for item in known_external
        ),
        external_lines_removed=sum(
            item.external_lines_removed or 0 for item in known_external
        ),
        fate_rate=_rate(len(fate.fates), len(observations)),
        skips=dict(sorted(fate.skipped.items())),
        coverage=LifecycleCoverage(
            observed=len(observations),
            missing={"fate_unavailable": sum(fate.skipped.values())}
            if fate.skipped
            else {},
            unsupported_by_integration={
                "cursor_edit_observations": len(
                    {d.session_id for d in cursor_decisions}
                )
            }
            if cursor_decisions
            else {},
        ),
        examples=tuple(
            _examples(
                name,
                {item.session_id for item in fate.fates if item.fate.value == name},
                policy.example_session_limit,
            )
            for name in ("unmodified", "partially_modified", "deleted")
        ),
    )

    retention_report = build_merge_retention_report(
        org_id,
        retention,
        explicit_accepted_inference_call_ids=accepted_ids,
        attribution_skipped=attribution.skipped,
        decision_attachment_skipped=attachments.skipped,
        attribution_provenance=Provenance(
            attribution_policy.policy_version, retention.provenance.quarantine_revision
        ),
        repository_context=repository_context,
    )
    merge_panel = MergeDurabilityPanel(
        coverage_qualification="partial_pull_request_history",
        attributed_file_candidates=retention_report.attributed_file_candidates,
        unique_pull_request_membership=retention_report.candidates_joined_to_merge,
        scored_rows=retention_report.scored_rows,
        membership_rate=_rate(
            retention_report.candidates_joined_to_merge,
            retention_report.attributed_file_candidates,
        ),
        scoring_rate=_rate(
            retention_report.scored_rows, retention_report.candidates_joined_to_merge
        ),
        membership=dict(sorted(retention.membership.items())),
        scoring_skips=retention_report.scoring_skips,
        head=_retention_summary(retention_report.head),
        merge=_retention_summary(retention_report.merge),
        coverage=LifecycleCoverage(
            observed=len(retention.rows),
            missing=retention_report.scoring_skips,
            ambiguous={
                "ambiguous_pr_membership": retention.membership[
                    "ambiguous_pr_membership"
                ]
            }
            if retention.membership["ambiguous_pr_membership"]
            else {},
        ),
        examples=(
            _examples(
                "joined",
                {
                    item.session_id
                    for item in retention.membership_outcomes
                    if item.status == "joined"
                },
                policy.example_session_limit,
            ),
            _examples(
                "scored",
                {item.session_id for item in retention.rows},
                policy.example_session_limit,
            ),
        ),
    )

    outcome_counts = Counter(item.status for item in abandonment.outcomes)
    attrition_panel = SessionAttritionPanel(
        eligible_sessions=len(abandonment.outcomes),
        committed=outcome_counts["committed"],
        abandoned=outcome_counts["abandoned"],
        in_flight=outcome_counts["in_flight"],
        attribution_unavailable=outcome_counts["attribution_unavailable"],
        skips=dict(sorted(abandonment.skipped.items())),
        coverage=LifecycleCoverage(
            observed=len(abandonment.outcomes)
            - outcome_counts["attribution_unavailable"],
            missing={
                "attribution_unavailable": outcome_counts["attribution_unavailable"]
            }
            if outcome_counts["attribution_unavailable"]
            else {},
        ),
        examples=tuple(
            _examples(
                name,
                {
                    item.session_id
                    for item in abandonment.outcomes
                    if item.status == name
                },
                policy.example_session_limit,
            )
            for name in (
                "committed",
                "abandoned",
                "in_flight",
                "attribution_unavailable",
            )
        ),
    )

    explicit_rejects = sum(
        1 for item in decisions if not item.accepted and item.explicit
    )
    external_changes = sum(
        1
        for item in known_external
        if (item.external_lines_added or 0) + (item.external_lines_removed or 0) > 0
    )
    rework = (
        ReworkComponent(
            "explicit_rejects",
            "developer_decision",
            explicit_rejects,
            len(decisions),
            _rate(explicit_rejects, len(decisions)),
        ),
        ReworkComponent(
            "retry_linkages",
            "retry_linkage",
            len(retries),
            len(retries),
            _rate(len(retries), len(retries)),
        ),
        ReworkComponent(
            "modified_or_deleted_edits",
            "edit_observation",
            fate_counts["partially_modified"] + fate_counts["deleted"],
            len(fate.fates),
            _rate(
                fate_counts["partially_modified"] + fate_counts["deleted"],
                len(fate.fates),
            ),
        ),
        ReworkComponent(
            "external_line_changes",
            "edit_observation_with_known_external_counts",
            external_changes,
            len(known_external),
            _rate(external_changes, len(known_external)),
        ),
        ReworkComponent(
            "abandoned_sessions",
            "accepted_session",
            outcome_counts["abandoned"],
            sum(outcome_counts[status] for status in ("committed", "abandoned")),
            _rate(
                outcome_counts["abandoned"],
                sum(outcome_counts[status] for status in ("committed", "abandoned")),
            ),
        ),
        ReworkComponent(
            "failed_ci_resolutions",
            "ci_resolution",
            sum(1 for item in resolutions.values() if item.verdict == CIResult.FAILED),
            len(resolutions),
            _rate(
                sum(
                    1
                    for item in resolutions.values()
                    if item.verdict == CIResult.FAILED
                ),
                len(resolutions),
            ),
        ),
    )
    strata_counts: Counter[tuple] = Counter()
    stratum_skips: Counter[StratumSkipReason] = Counter()
    for call_id in accepted_ids:
        call = call_by_id[call_id]
        if call.model:
            strata_counts[("model", None, call.model)] += 1
        else:
            stratum_skips["missing_model"] += 1
        harnesses = {
            item.agent_harness.value
            for item in attachments.decisions_by_completion[call_id]
        }
        if len(harnesses) == 1:
            strata_counts[("agent_harness", None, next(iter(harnesses)))] += 1
        elif len(harnesses) > 1:
            stratum_skips["multiple_agent_harnesses"] += 1
        repositories = {commit_key(item).repository for item in attrs_by_call[call_id]}
        if len(repositories) == 1:
            strata_counts[("repository", next(iter(repositories)), "")] += 1
        elif repositories:
            stratum_skips["multiple_repositories"] += 1
        else:
            stratum_skips["missing_repository"] += 1
        workflows = {
            (
                commit_key(item).repository,
                "|".join(
                    (
                        workflow.provider.value,
                        workflow.workflow_id or "",
                        workflow.workflow_path or "",
                        workflow.workflow_name,
                    )
                ),
            )
            for item in attrs_by_call[call_id]
            if commit_key(item) in resolutions
            for workflow in resolutions[commit_key(item)].workflow_resolutions
        }
        if len(workflows) == 1:
            repository, workflow = next(iter(workflows))
            strata_counts[("workflow", repository, workflow)] += 1
        elif workflows:
            stratum_skips["multiple_workflows"] += 1
        else:
            stratum_skips["missing_workflow"] += 1
    strata = tuple(
        LifecycleStratum(
            dimension,
            repository_context.repo_for(repository)
            if dimension == "repository"
            else value,
            count,
            repo=repository_context.repo_for(repository) if repository else None,
            repository_identity=repository.identity
            if isinstance(repository, IdentifiedRepositoryKey)
            else None,
        )
        for (dimension, repository, value), count in sorted(
            strata_counts.items(),
            key=lambda pair: (
                pair[0][0],
                repository_sort_key(pair[0][1]) if pair[0][1] else (),
                pair[0][2],
            ),
        )
    )
    repository_skipped = {}
    if bindings.skipped:
        repository_skipped["session_observations"] = dict(
            sorted(bindings.skipped.items())
        )
    for unit, skipped in (
        ("attribution_sources", attribution.skipped),
        ("ci_population_outcomes", ci.skipped),
    ):
        reasons = {
            reason: count
            for reason, count in sorted(skipped.items())
            if reason in get_args(RepositoryIdentitySkipReason)
        }
        if reasons:
            repository_skipped[unit] = reasons

    quarantine_revision = retention.provenance.quarantine_revision
    lifecycle_provenance = Provenance(policy.policy_version, quarantine_revision)
    return AcceptedWorkLifecycleReport(
        org_id=org_id,
        repository_skipped=repository_skipped,
        accepted_work=accepted_panel,
        edit_retention=edit_panel,
        merge_durability=merge_panel,
        session_attrition=attrition_panel,
        rework=rework,
        strata=strata,
        stratum_skips=dict(sorted(stratum_skips.items())),
        policy=policy,
        provenance=LifecycleProvenance(
            lifecycle=lifecycle_provenance,
            attribution=Provenance(
                attribution_policy.policy_version, quarantine_revision
            ),
            abandonment=abandonment.provenance
            or Provenance(abandonment_policy.policy_version, quarantine_revision),
            fate=fate.provenance,
            ci_resolution=ci.resolutions[0].provenance
            if ci.resolutions
            else Provenance(
                ci_policy.policy_version, quarantine_revision, ci_policy.digest
            ),
            merge_retention=retention.provenance,
        ),
    )

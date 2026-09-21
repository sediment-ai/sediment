# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Per-model outcome report — attribution, CI, and acceptance, by model.

Per ADR 0005 this ships before any export-destination polish: it is the
first value a deployment sees and the renewal question ("did the fine-tune
work?") made answerable. **No router is built** — LiteLLM does weighted
routing natively; model-call facts arrive stamped with the serving model.
Sediment only measures what already happened.

``build_model_report`` aggregates canonical Attributed completions for Attribution
and qualified CI outcomes. Direct Developer decisions use the shared
unique-or-drop attachment over the caller's Inference-call population, independently
of commit evidence (ADR 0014). Artifact-only callers retain their supplied
assembled decisions. ``generate_model_report`` is the
one-call orchestration (``store`` + ``mirrors`` in, rows out) that
``sediment report model`` wraps, mirroring ``export_rlvr``'s shape.

The report row (``ModelOutcomeReport``) is a derived report row, not a fact
(ADR 0001): recomputed fresh on every run, never persisted.
"""

from __future__ import annotations

from sediment_core.store import InferenceCallIdentity, CIOutcomeProjection
from sediment_derive.session_commit import bind_session_commit_keys_result
from sediment_derive.repository_identity import (
    CommitKey,
    IdentifiedRepositoryKey,
    LegacyRepositoryKey,
    RepositoryContext,
    RepositoryIdentity,
    RepositoryKey,
    REPOSITORY_IDENTITY_SKIP_REASONS,
    build_repository_context,
    commit_sort_key,
    repository_identity_evidence_of,
    repository_sort_key,
)
from sediment_derive.attachment import index_ci_outcomes_by_commit_key_result

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import logging
from itertools import combinations
from math import ceil, sqrt
from statistics import NormalDist, median

from sediment_core import (
    CIOutcome,
    CIResult,
    DeveloperDecision,
    FactStore,
    Push,
    NonEmptyId,
    FactTable,
    OrgId,
)
from sediment_derive import (
    AttributionShareAlert,
    AttributionSharePolicy,
    CIResolutionPolicy,
    CIResolution,
    CIResolutionResult,
    derive_ci_resolution_result,
    FatePolicy,
    FateResult,
    MIN_DRIFT_CASES,
    MirrorManager,
    InferenceCall,
    RecoveryPolicy,
    RecoveryResult,
    RepoAttributionShare,
    check_attribution_share_alerts,
    derive_attribution_share,
    derive_fate_result,
    Provenance,
    derive_recovery_result,
    inference_fact_id,
    inference_model,
    inference_observed_at,
    four_gram_containment,
    join_decisions_by_call_id_result,
    read_repository_context,
)

from .attributed_completions import (
    AbandonmentSummary,
    AttributedCompletion,
    AttributedCompletionPolicy,
    AttributedCompletionAssemblyResult,
    assemble_attributed_completions_result,
    build_abandonment_summary,
)
from .operational_scope import OperationalReportScope

_STANDARD_NORMAL = NormalDist()
_DIFF_SIZE_BUCKET_LIMITS = (0, 5, 10, 25, 50, 100, 200, 500, 1000)
logger = logging.getLogger(__name__)


def _report_now(now: datetime | None, context: RepositoryContext | None) -> datetime:
    boundary = (
        now if now is not None else (context.as_of if context else datetime.now(UTC))
    )
    if boundary.tzinfo is None or boundary.utcoffset() is None:
        raise ValueError("report boundary must be timezone-aware")
    boundary = boundary.astimezone(UTC)
    if context is not None and context.as_of != boundary:
        raise ValueError("repository context boundary differs from report boundary")
    return boundary


class CIGrain(StrEnum):
    """The unit of analysis for ``ci_linked``/``ci_passed``.

    ``AttributedCompletion`` is per ``(repo, commit_sha, file_path, inference_call_id)``, but
    ``CIOutcome`` is keyed per ``(repo, commit_sha)`` — attributed-completion assembly
    attaches the *same* outcome list to every attributed completion of a commit, so
    counting attributed completions pseudo-replicates one CI run once per file it touched
    (and again per completion attributed to that commit). This enum picks
    the aggregation's unit of analysis explicitly, as a policy knob
    (``OutcomeReportPolicy.ci_grain``), rather than silently baking one
    choice into the aggregation.
    """

    #: One count per distinct ``commit_sha`` with >=1 attached CI outcome —
    #: the correct grain, and the default. A commit touching k files
    #: contributes exactly one trial, matching what actually ran in CI.
    COMMIT = "commit"
    #: One count per attributed completion with >=1 attached CI outcome — the
    #: superseded behavior, preserved for explicit opt-in/comparison only.
    #: **Not recommended**: a commit touching k files contributes k
    #: identical trials, inflating the sample size fed into any inference
    #: over these counts (e.g. a two-proportion z-test) and,
    #: since fan-out is plausibly model-attributed, can manufacture a
    #: directional "significant" result that isn't real. Kept only because
    #: it remains a real, previously-shipped code path that some
    #: caller may need to reproduce or compare against explicitly.
    ATTRIBUTED_COMPLETION = "attributed_completion"


class StratificationMetric(StrEnum):
    """Outcome-report metrics that can be checked for repo stratification."""

    CI_PASS_RATE = "ci_pass_rate"
    ATTRIBUTION_RATE = "attribution_rate"


class StratificationStatus(StrEnum):
    """Machine-readable outcome of a repo-stratification check."""

    REVERSAL_DETECTED = "reversal_detected"
    NO_REVERSAL_DETECTED = "no_reversal_detected"
    NOT_ENOUGH_REPOS = "not_enough_repos"
    NOT_ENOUGH_MODELS = "not_enough_models"
    NOT_CHECKABLE = "not_checkable"
    NO_COMPARABLE_DATA = "no_comparable_data"


class TrendMetric(StrEnum):
    """Outcome-report rates that can be checked for temporal drift."""

    ATTRIBUTION_RATE = "attribution_rate"
    CI_PASS_RATE = "ci_pass_rate"


class TrendStatus(StrEnum):
    """Machine-readable Mann-Kendall trend-test outcome."""

    SIGNIFICANT_INCREASE = "significant_increase"
    SIGNIFICANT_DECREASE = "significant_decrease"
    NO_SIGNIFICANT_TREND = "no_significant_trend"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class OutcomeReportPolicy:
    """The tunable outcome-report aggregation semantics.

    ``policy_version`` stamps provenance the same way ``AttributedCompletionPolicy``/
    ``RolloutPolicy`` do; bump it when tuning any knob so derived reports
    stay distinguishable (ADR 0001)."""

    #: Unit of analysis for ``ci_linked``/``ci_passed``. Defaults to the
    #: correct, non-inflating grain (``CIGrain.COMMIT``) — see ``CIGrain``.
    ci_grain: CIGrain = CIGrain.COMMIT
    ci_resolution: CIResolutionPolicy = field(default_factory=CIResolutionPolicy)
    policy_version: str = "4"


@dataclass(frozen=True)
class ModelOutcomeReport:
    """One model's outcome row — attribution, CI, and acceptance.

    ``attribution_rate`` is a **lower bound**: JACCARD attribution
    matches on token overlap, so a completion that shipped after substantial
    edits can fall below the attribution threshold and never become an
    attributed completion. Compare it across models under the same conditions;
    do not interpret it as an absolute retention number.
    ``docs/agents/statistics.md`` carries the comparison caveats.
    """

    model: str
    # The ``since_days`` this row was computed under; ``None`` means all
    # history. Carried on the row so a JSON consumer knows the window
    # without re-reading the CLI invocation.
    since_days: int | None
    completions: int
    attributed_inference_calls: int
    attribution_rate: float
    attribution_rate_ci: tuple[float, float]
    ci_linked: int
    ci_passed: int
    ci_pass_rate: float
    ci_pass_rate_ci: tuple[float, float]
    explicit_accepts: int
    explicit_rejects: int
    mean_similarity: float
    provenance: Provenance
    grain: CIGrain
    ci_failures_by_workflow: dict[str, int] = field(default_factory=dict)
    explicit_rejects_by_agent_harness: dict[str, int] = field(default_factory=dict)
    fates: dict[str, int] = field(default_factory=dict)
    explicit_accept_fates: dict[str, int] = field(default_factory=dict)
    fates_with_external_changes: dict[str, int] = field(default_factory=dict)
    session_commit_unobserved: int = 0
    session_commit_observation_ids: tuple[NonEmptyId, ...] = ()


@dataclass(frozen=True)
class SignalFunnelReport:
    """Per-model completion attrition from capture to training-row eligibility.

    Counts are per completion, not per attributed completion or per commit: a completion with
    several file-grained attributed completions still contributes one count to each stage it reaches.
    ``has_decision`` means an explicit developer decision, matching
    ``CONTEXT.md``'s "Reward signal" definition for training-row eligibility.
    CI stages require observed Session edges. ``training_row_eligible`` counts
    that observed outcome population plus direct explicit decisions; it is not
    the complete yield of version-1 recipes that also permit inferred CI links.
    """

    model: str
    since_days: int | None
    completions_total: int
    attributed: int
    attribution_rate: float
    ci_linked: int
    ci_linked_retention_rate: float
    has_decision: int
    has_decision_retention_rate: float
    training_row_eligible: int
    training_row_eligible_retention_rate: float
    session_commit_unobserved: int = 0


@dataclass(frozen=True)
class StratifiedRate:
    """One numerator/denominator/rate cell in a stratification comparison."""

    model: str
    numerator: int
    denominator: int
    rate: float


@dataclass(frozen=True)
class ShrunkStratifiedRate:
    """Empirical-Bayes companion to one raw stratified rate.

    ``shrunk_rate`` is the posterior mean from a beta-binomial partial-pooling
    approximation: ``w * raw_rate + (1 - w) * grand_mean`` where
    ``w = denominator / (denominator + pooling_strength)``. The raw
    numerator/denominator/rate stay visible so operators can see the
    diagnostic pull toward the pooled mean without losing the observed cell.
    """

    model: str
    numerator: int
    denominator: int
    raw_rate: float
    shrunk_rate: float
    grand_mean: float
    shrinkage_weight: float
    pooling_strength: float


@dataclass(frozen=True)
class StratificationReversal:
    """A repo where the per-repo direction opposes the aggregate direction."""

    repo: str
    left: StratifiedRate
    right: StratifiedRate
    aggregate_left: StratifiedRate
    aggregate_right: StratifiedRate
    aggregate_delta: float
    repo_delta: float
    org_id: OrgId | None = None
    repository_identity: RepositoryIdentity | None = None


@dataclass(frozen=True)
class RepositoryShrunkRates:
    """Model rate cells for one repository lifetime, independent of its label."""

    org_id: OrgId
    repo: str
    repository_identity: RepositoryIdentity | None
    model_rates: dict[str, ShrunkStratifiedRate]


@dataclass(frozen=True)
class MantelHaenszelRiskDifference:
    """Repo-stratified Mantel-Haenszel pooled risk-difference estimate.

    ``naive_delta`` is the unadjusted aggregate risk difference
    (``left.rate - right.rate``). ``adjusted_delta`` is the
    Mantel-Haenszel pooled risk difference over comparable repo strata,
    weighted by ``n_left * n_right / (n_left + n_right)`` per stratum:

    ``sum(weight_i * (left_rate_i - right_rate_i)) / sum(weight_i)``.

    This intentionally ships the hand-verifiable point estimate only. The
    Mantel-Haenszel CI/test variance for risk difference is follow-up work;
    this row does not expose p-values or confidence intervals.
    """

    left_model: str
    right_model: str
    aggregate_left: StratifiedRate
    aggregate_right: StratifiedRate
    naive_delta: float
    adjusted_delta: float
    compared_repos: int
    skipped_repos: int
    weight_sum: float


@dataclass(frozen=True)
class StratificationComparison:
    """One pairwise model comparison for one metric."""

    left_model: str
    right_model: str
    aggregate_left: StratifiedRate
    aggregate_right: StratifiedRate
    aggregate_delta: float
    compared_repos: int
    skipped_repos: int
    reversals: list[StratificationReversal]


@dataclass(frozen=True)
class StratificationCheck:
    """Repo-stratification result for one metric."""

    metric: StratificationMetric
    status: StratificationStatus
    checked_repos: int
    comparisons: list[StratificationComparison]
    reason: str
    session_commit_unobserved: int = 0
    adjusted_comparisons: list[MantelHaenszelRiskDifference] = field(
        default_factory=list
    )
    shrunk_rates: list[RepositoryShrunkRates] = field(default_factory=list)


@dataclass(frozen=True)
class MannKendallTrendTest:
    """Mann-Kendall monotonic trend test over ordered window rates.

    Chosen over a linear-regression slope test because the outcome report's
    inputs are bounded rates with uneven denominators across windows; the
    Mann-Kendall test only asks whether later rates tend to be higher or
    lower than earlier rates, without assuming normally distributed residuals.
    The implementation is stdlib-only: standard S statistic, tie-corrected
    variance, continuity-corrected normal approximation, and two-sided
    p-value via ``statistics.NormalDist``.
    """

    sample_size: int
    s_statistic: int
    variance: float
    z_statistic: float
    p_value: float | None
    alpha: float
    status: TrendStatus
    reason: str


@dataclass(frozen=True)
class TemporalTrendWindow:
    """One captured-at bucket used as an input to a temporal trend test."""

    window_index: int
    window_start: datetime
    window_end: datetime
    numerator: int
    denominator: int
    rate: float


@dataclass(frozen=True)
class ModelTemporalTrend:
    """One model/metric temporal-drift result over rolling report windows."""

    model: str
    metric: TrendMetric
    window_days: int
    windows: list[TemporalTrendWindow]
    test: MannKendallTrendTest
    session_commit_unobserved: int = 0


@dataclass(frozen=True)
class ModelOutcomeReportResult:
    """The model report plus auxiliary pure checks for the same inputs."""

    rows: list[ModelOutcomeReport]
    stratification: list[StratificationCheck]
    attribution_share: list[RepoAttributionShare] = field(default_factory=list)
    attribution_alerts: list[AttributionShareAlert] = field(default_factory=list)
    signal_funnel: list[SignalFunnelReport] = field(default_factory=list)
    trends: list[ModelTemporalTrend] = field(default_factory=list)
    abandonment: AbandonmentSummary = field(default_factory=AbandonmentSummary)
    fate_skipped: dict[str, int] = field(default_factory=dict)
    fate_provenance: Provenance | None = None
    repository_skipped: dict[str, dict[str, int]] = field(default_factory=dict)
    ci_skipped: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelReportInputsResult:
    """Selected inputs and separate source/edge repository loss units."""

    completions: list[InferenceCall]
    attributed_completions: list[AttributedCompletion]
    repository_skipped: dict[str, dict[str, int]]


def scope_model_report_evidence(
    completions: Iterable[InferenceCall],
    attributed_completions: Iterable[AttributedCompletion],
    scope: OperationalReportScope,
) -> tuple[list[InferenceCall], list[AttributedCompletion]]:
    """Select one Inference-call cohort and its evidence through ``as_of``."""
    cohort_start = scope.cohort_start.astimezone(UTC)
    cohort_end = scope.cohort_end.astimezone(UTC)
    as_of = scope.as_of.astimezone(UTC)
    scoped_completions = [
        call
        for call in completions
        if cohort_start <= inference_observed_at(call).astimezone(UTC) < cohort_end
    ]
    cohort_ids = {inference_fact_id(call) for call in scoped_completions}
    scoped_rows = []
    for row in attributed_completions:
        if row.inference_call_id not in cohort_ids:
            continue
        decisions = [
            decision
            for decision in row.decisions
            if decision.captured_at.astimezone(UTC) <= as_of
        ]
        if row.abandonment is not None and (
            row.abandonment.as_of.astimezone(UTC) > as_of
            or not any(
                decision.accepted and decision.explicit for decision in decisions
            )
        ):
            continue
        scoped_rows.append(
            replace(
                row,
                decisions=decisions,
                ci_outcomes=[
                    outcome
                    for outcome in row.ci_outcomes
                    if outcome.captured_at.astimezone(UTC) <= as_of
                ],
            )
        )
    return scoped_completions, scoped_rows


@dataclass(frozen=True)
class ReportCITrial:
    """One qualified report trial, shared with temporal diagnostics."""

    commit: CommitKey
    resolution: CIResolution
    first_captured_at: datetime
    inference_call_id: NonEmptyId | None = None
    file_path: str | None = None


@dataclass(frozen=True)
class _ReportCIPopulation:
    outcomes: tuple[CIOutcome | CIOutcomeProjection, ...]
    result: CIResolutionResult
    resolutions: dict[CommitKey, CIResolution]
    policy: CIResolutionPolicy
    first_captured_at: dict[CommitKey, datetime]


def prepare_report_ci(population, rows, context, boundary, policy=None):
    """Resolve and cache one CI population for reuse across a report's stages."""
    if isinstance(population, _ReportCIPopulation):
        if policy is None or population.policy == policy:
            return population
        population = population.outcomes
    policy = policy or CIResolutionPolicy()
    outcomes = tuple(
        _unique_report_facts(
            (
                fact
                for fact in (
                    population
                    if population is not None
                    else (fact for row in rows for fact in row.ci_outcomes)
                )
                if (context is None or fact.org_id == context.org_id)
                and (boundary is None or fact.captured_at.astimezone(UTC) <= boundary)
            ),
            "outcome_id",
        )
    )
    result = derive_ci_resolution_result(outcomes, policy, repository_context=context)
    resolutions = {}
    for resolution in result.resolutions:
        key = (
            context.commit_key(
                resolution.org_id,
                resolution.repo,
                resolution.commit_sha,
                repository_identity=resolution.repository_identity,
            )
            if context
            else CommitKey(
                LegacyRepositoryKey(resolution.org_id, resolution.repo),
                resolution.commit_sha,
            )
        )
        if key is not None:
            resolutions[key] = resolution
    return _ReportCIPopulation(
        outcomes,
        result,
        resolutions,
        policy,
        {
            key: min(item.captured_at.astimezone(UTC) for item in sources)
            for key, sources in result.outcomes_by_commit.items()
        },
    )


def _report_ci_losses(population: _ReportCIPopulation) -> dict[str, dict[str, int]]:
    units = {}
    for reason, count in sorted(population.result.skipped.items()):
        unit = (
            "runs"
            if reason == "conflicting_run_identity"
            else "commits"
            if reason == "ambiguous_workflow_verdicts"
            else "source_outcomes"
        )
        units.setdefault(unit, {})[reason] = count
    return units


def model_ci_trials(
    rows: Iterable[AttributedCompletion],
    grain: CIGrain = CIGrain.COMMIT,
    policy: CIResolutionPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
    ci_population: Iterable[CIOutcome | CIOutcomeProjection] | None = None,
) -> list[ReportCITrial]:
    """Resolve the factual rows returned by model_report_inputs at one trial grain.

    Union exact CI evidence before resolving a commit. Artifact fan-out cannot
    select a last-seen verdict or split one lifetime across captured names.
    Without ci_population, the supplied rows declare their carried CI to be the
    whole known population; repository_context does not certify CI completeness.
    """
    rows = list(rows)
    ci_population = prepare_report_ci(
        ci_population,
        rows,
        repository_context,
        repository_context.as_of if repository_context else None,
        policy,
    )
    grouped = defaultdict(list)
    for index, row in enumerate(rows):
        commit = _report_commit_key(row, repository_context)
        if commit is None or not row.session_commit_observations:
            continue
        identity = (
            (commit, row.inference_call_id, row.file_path, index)
            if grain == CIGrain.ATTRIBUTED_COMPLETION
            else (commit, "", "", 0)
        )
        grouped[identity].append(row)
    trials = []
    for (commit, call_id, file_path, _), members in sorted(
        grouped.items(), key=lambda pair: (commit_sort_key(pair[0][0]), *pair[0][1:])
    ):
        resolution = ci_population.resolutions.get(commit)
        if resolution is None or resolution.verdict is None:
            continue
        trials.append(
            ReportCITrial(
                commit,
                resolution,
                ci_population.first_captured_at[commit],
                call_id or None,
                file_path or None,
            )
        )
    return sorted(
        trials,
        key=lambda trial: (
            trial.first_captured_at,
            commit_sort_key(trial.commit),
            trial.inference_call_id or "",
            trial.file_path or "",
            trial.resolution.source_outcome_ids,
            trial.resolution.verdict,
        ),
    )


def wilson_score_interval(
    successes: int, trials: int, *, confidence: float = 0.95
) -> tuple[float, float]:
    """Return the Wilson score interval for a binomial proportion.

    Formula (Wilson, 1927): with ``p = successes / trials`` and
    ``z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)``,
    ``center = (p + z^2 / (2n)) / (1 + z^2 / n)`` and
    ``half_width = z * sqrt((p(1-p) + z^2 / (4n)) / n) / (1 + z^2 / n)``.
    The interval is ``center +/- half_width``, clipped to ``[0.0, 1.0]`` for
    floating-point guardrails. When ``trials == 0``, returns the documented
    report sentinel ``(0.0, 0.0)``.
    """
    if trials < 0:
        raise ValueError("trials must be non-negative")
    if successes < 0 or successes > trials:
        raise ValueError("successes must be between 0 and trials")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between 0.0 and 1.0")
    if trials == 0:
        return (0.0, 0.0)

    proportion = successes / trials
    z = _STANDARD_NORMAL.inv_cdf(1 - (1 - confidence) / 2)
    z_squared = z * z
    denominator = 1 + z_squared / trials
    center = (proportion + z_squared / (2 * trials)) / denominator
    half_width = (
        z
        * sqrt((proportion * (1 - proportion) + z_squared / (4 * trials)) / trials)
        / denominator
    )
    return (max(0.0, center - half_width), min(1.0, center + half_width))


def _ci_counts(
    model_attributed_completions: list[AttributedCompletion],
    grain: CIGrain,
    policy: CIResolutionPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
    ci_population: Iterable[CIOutcome | CIOutcomeProjection] | None = None,
) -> tuple[int, int]:
    trials = model_ci_trials(
        model_attributed_completions,
        grain,
        policy,
        repository_context=repository_context,
        ci_population=ci_population,
    )
    return len(trials), sum(
        trial.resolution.verdict == CIResult.PASSED for trial in trials
    )


@dataclass(frozen=True)
class DiffSizeStats:
    """Distribution stats for candidate recovery diffs in changed lines."""

    count: int
    min: int | None
    median: float | None
    p90: int | None
    max: int | None
    histogram: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryDiffSizeDistribution:
    """Kept-vs-dropped candidate diff-size distribution at the active cap."""

    max_recovery_diff_lines: int
    kept: DiffSizeStats
    dropped: DiffSizeStats


def _empty_diff_size_stats() -> DiffSizeStats:
    return DiffSizeStats(
        count=0,
        min=None,
        median=None,
        p90=None,
        max=None,
        histogram={},
    )


def _default_diff_size_distribution() -> RecoveryDiffSizeDistribution:
    return RecoveryDiffSizeDistribution(
        max_recovery_diff_lines=RecoveryPolicy().max_recovery_diff_lines,
        kept=_empty_diff_size_stats(),
        dropped=_empty_diff_size_stats(),
    )


@dataclass(frozen=True)
class RecoveryYieldReport:
    """Recovery-pair yield over CI-outcome facts and the recovery derivation.

    ``total_failed_ci_runs`` is the denominator operators care about: every
    stored FAILED CI outcome for the org/window the caller passed in. A
    failed run can be absent from ``skipped`` when it never forms a
    FAILED->PASSED candidate transition at all (for example, still red at the
    end of history). ``skipped`` is the derivation's structured gate tally,
    reason -> count, mirroring the projection skip-counter pattern.
    ``diff_size_distribution`` summarizes actual changed-line counts for
    candidates that reached the recovery diff-size gate, split into kept
    and dropped at the active cap.
    """

    total_failed_ci_runs: int
    recovery_pairs: int
    recovery_yield_rate: float
    skipped: Counter[str] = field(default_factory=Counter)
    diff_size_distribution: RecoveryDiffSizeDistribution = field(
        default_factory=_default_diff_size_distribution
    )


def _diff_size_bucket_label(lower: int, upper: int) -> str:
    if lower == upper:
        return str(upper)
    return f"{lower}-{upper}"


def _diff_size_histogram(
    line_counts: Iterable[int],
    max_recovery_diff_lines: int,
) -> dict[str, int]:
    boundaries = sorted({*_DIFF_SIZE_BUCKET_LIMITS, max(0, max_recovery_diff_lines)})
    histogram: Counter[str] = Counter()
    for line_count in line_counts:
        previous = -1
        for boundary in boundaries:
            lower = previous + 1
            if line_count <= boundary:
                histogram[_diff_size_bucket_label(lower, boundary)] += 1
                break
            previous = boundary
        else:
            histogram[f">{boundaries[-1]}"] += 1
    return dict(histogram)


def _diff_size_stats(
    line_counts: Iterable[int],
    max_recovery_diff_lines: int,
) -> DiffSizeStats:
    ordered = sorted(line_counts)
    if not ordered:
        return _empty_diff_size_stats()
    return DiffSizeStats(
        count=len(ordered),
        min=ordered[0],
        median=median(ordered),
        p90=ordered[ceil(len(ordered) * 0.9) - 1],
        max=ordered[-1],
        histogram=_diff_size_histogram(ordered, max_recovery_diff_lines),
    )


def _recovery_diff_size_distribution(
    recovery: RecoveryResult,
) -> RecoveryDiffSizeDistribution:
    return RecoveryDiffSizeDistribution(
        max_recovery_diff_lines=recovery.max_recovery_diff_lines,
        kept=_diff_size_stats(
            recovery.kept_diff_line_counts,
            recovery.max_recovery_diff_lines,
        ),
        dropped=_diff_size_stats(
            recovery.dropped_diff_line_counts,
            recovery.max_recovery_diff_lines,
        ),
    )


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _rate(model: str, numerator: int, denominator: int) -> StratifiedRate:
    return StratifiedRate(
        model=model,
        numerator=numerator,
        denominator=denominator,
        rate=_ratio(numerator, denominator),
    )


def shrink_stratified_rates(rates: list[StratifiedRate]) -> list[ShrunkStratifiedRate]:
    """Shrink raw stratified rates toward their denominator-weighted mean.

    This uses a method-of-moments beta-binomial empirical-Bayes estimate:
    observed between-cell variance is reduced by expected binomial sampling
    variance, then converted to a prior sample size ``k`` where
    ``Var(theta) = grand_mean * (1 - grand_mean) / (k + 1)``. Cells are
    shrunk as ``(x + k * grand_mean) / (n + k)``.

    Tiny stratification tables often have non-positive between-cell variance
    after subtracting sampling noise. In that numerically fragile case, the
    function uses a documented conservative finite pooling strength equal to
    the total denominator: small cells move substantially toward the grand
    mean, while large cells keep most of their raw signal.
    """
    total_denominator = sum(rate.denominator for rate in rates)
    if total_denominator <= 0:
        return [
            ShrunkStratifiedRate(
                model=rate.model,
                numerator=rate.numerator,
                denominator=rate.denominator,
                raw_rate=rate.rate,
                shrunk_rate=rate.rate,
                grand_mean=0.0,
                shrinkage_weight=0.0,
                pooling_strength=0.0,
            )
            for rate in rates
        ]

    grand_mean = sum(rate.numerator for rate in rates) / total_denominator
    binomial_variance = grand_mean * (1 - grand_mean)
    observed_variance = (
        sum(
            rate.denominator * ((rate.rate - grand_mean) ** 2)
            for rate in rates
            if rate.denominator
        )
        / total_denominator
    )
    expected_sampling_variance = (
        sum(
            rate.denominator * (binomial_variance / rate.denominator)
            for rate in rates
            if rate.denominator
        )
        / total_denominator
    )
    between_variance = observed_variance - expected_sampling_variance
    if between_variance > 0 and binomial_variance > 0:
        pooling_strength = max((binomial_variance / between_variance) - 1, 0.0)
    else:
        pooling_strength = float(total_denominator)

    shrunk: list[ShrunkStratifiedRate] = []
    for rate in rates:
        if rate.denominator <= 0:
            weight = 0.0
            shrunk_rate = grand_mean
        else:
            weight = rate.denominator / (rate.denominator + pooling_strength)
            shrunk_rate = (weight * rate.rate) + ((1 - weight) * grand_mean)
        shrunk.append(
            ShrunkStratifiedRate(
                model=rate.model,
                numerator=rate.numerator,
                denominator=rate.denominator,
                raw_rate=rate.rate,
                shrunk_rate=shrunk_rate,
                grand_mean=grand_mean,
                shrinkage_weight=weight,
                pooling_strength=pooling_strength,
            )
        )
    return shrunk


def _direction(delta: float) -> int:
    if delta > 0:
        return 1
    if delta < 0:
        return -1
    return 0


def _mantel_haenszel_risk_difference(
    left_model: str,
    right_model: str,
    aggregate_left: StratifiedRate,
    aggregate_right: StratifiedRate,
    strata: Iterable[tuple[StratifiedRate, StratifiedRate]],
    *,
    skipped_repos: int,
) -> MantelHaenszelRiskDifference | None:
    """Pool repo-level risk differences with Mantel-Haenszel stratum weights."""
    numerator = 0.0
    weight_sum = 0.0
    compared_repos = 0
    for left, right in strata:
        if left.denominator == 0 or right.denominator == 0:
            continue
        stratum_size = left.denominator + right.denominator
        weight = (left.denominator * right.denominator) / stratum_size
        numerator += weight * (left.rate - right.rate)
        weight_sum += weight
        compared_repos += 1

    if weight_sum == 0:
        return None

    return MantelHaenszelRiskDifference(
        left_model=left_model,
        right_model=right_model,
        aggregate_left=aggregate_left,
        aggregate_right=aggregate_right,
        naive_delta=aggregate_left.rate - aggregate_right.rate,
        adjusted_delta=numerator / weight_sum,
        compared_repos=compared_repos,
        skipped_repos=skipped_repos,
        weight_sum=weight_sum,
    )


def model_report_inputs(
    completions: Iterable[InferenceCall],
    attributed_completions: Iterable[AttributedCompletion],
    since_days: int | None,
    now: datetime,
    *,
    repository_context: RepositoryContext | None = None,
    ci_population: Iterable[CIOutcome | CIOutcomeProjection] | None = None,
) -> tuple[list[InferenceCall], list[AttributedCompletion]]:
    """Select report inputs and qualify CI by captured Session observations."""
    result = model_report_inputs_result(
        completions,
        attributed_completions,
        since_days,
        now,
        repository_context=repository_context,
        ci_population=ci_population,
    )
    return result.completions, result.attributed_completions


def model_report_inputs_result(
    completions: Iterable[InferenceCall],
    attributed_completions: Iterable[AttributedCompletion],
    since_days: int | None,
    now: datetime,
    *,
    repository_context: RepositoryContext | None = None,
    ci_population: Iterable[CIOutcome | CIOutcomeProjection] | None = None,
) -> ModelReportInputsResult:
    """Use complete repository context and the declared CI population.

    If ci_population is omitted, all supplied artifacts declare their carried CI
    as the whole available population. Store-backed consumers pass complete
    metadata through the same boundary before selecting a report cohort.
    """
    if now.utcoffset() is None:
        raise ValueError("now must be an aware datetime")
    if repository_context is not None and repository_context.as_of != now.astimezone(
        UTC
    ):
        raise ValueError("repository context must match report boundary")
    cutoff = now - timedelta(days=since_days) if since_days is not None else None
    now = now.astimezone(UTC)
    cutoff = cutoff.astimezone(UTC) if cutoff is not None else None
    all_completions = [
        call
        for call in completions
        if repository_context is None or call.org_id == repository_context.org_id
    ]
    all_rows = list(attributed_completions)
    ci_population = prepare_report_ci(ci_population, all_rows, repository_context, now)
    completions_by_id = {inference_fact_id(c): c for c in all_completions}

    def _in_window(captured_at: datetime) -> bool:
        captured_at = captured_at.astimezone(UTC)
        return captured_at <= now and (cutoff is None or captured_at >= cutoff)

    windowed_completions = [
        completion
        for completion in all_completions
        if _in_window(inference_observed_at(completion))
    ]
    windowed_attributed_completions = [
        attributed_completion
        for attributed_completion in all_rows
        if (
            completion := completions_by_id.get(attributed_completion.inference_call_id)
        )
        is not None
        and (completion.org_id, completion.session_id)
        == (attributed_completion.org_id, attributed_completion.session_id)
        and _in_window(inference_observed_at(completion))
    ]
    # Canonical evidence remains available to training. The report projects only
    # CI outcomes whose Session edge has a matching observation at this boundary.
    observations = _unique_report_facts(
        (fact for row in all_rows for fact in row.session_commit_observations),
        "observation_id",
    )
    outcomes = _unique_report_facts(
        (fact for row in all_rows for fact in row.ci_outcomes), "outcome_id"
    )
    contexts = (
        {repository_context.org_id: repository_context}
        if repository_context is not None
        else {
            org_id: build_repository_context(
                (
                    repository_identity_evidence_of(fact)
                    for fact in (*observations, *outcomes)
                    if fact.org_id == org_id
                ),
                (),
                org_id,
                as_of=now,
            )
            for org_id in {row.org_id for row in all_rows}
        }
    )
    observations = _unique_report_facts(
        (
            fact
            for row in windowed_attributed_completions
            for fact in row.session_commit_observations
        ),
        "observation_id",
    )
    outcomes = _unique_report_facts(
        (fact for row in windowed_attributed_completions for fact in row.ci_outcomes),
        "outcome_id",
    )
    bindings = {}
    losses = {
        "attribution_edges": Counter(),
        "session_observations": Counter(),
        "ci_outcomes": Counter(),
    }
    ci_by_commit = {}
    for org_id, context in contexts.items():
        bound = bind_session_commit_keys_result(
            observations, org_id, as_of=now, repository_context=context
        )
        losses["session_observations"].update(bound.skipped)
        bindings.update(bound.bindings)
        indexed = index_ci_outcomes_by_commit_key_result(
            outcomes, repository_context=context
        )
        losses["ci_outcomes"].update(indexed.skipped)
        ci_by_commit.update(indexed.outcomes_by_commit)
    valid_ci_sources = {
        (item.org_id, item.outcome_id)
        for sources in ci_population.result.outcomes_by_commit.values()
        for item in sources
    }
    factual_rows = []
    declined_edges = set()
    for row in windowed_attributed_completions:
        context = contexts.get(row.org_id)
        commit = _report_commit_key(row, context)
        reason = None
        if row.abandonment is None:
            if commit is None or (
                repository_context is None and row.repository_identity is not None
            ):
                reason = "repository_identity_unresolved"
            elif row.repository_identity is not None:
                source = context.resolve_source(FactTable.PUSHES, row.source_push_id)
                if source.key is None:
                    reason = source.reason
                elif source.key != commit.repository:
                    reason = "repository_identity_conflict"
        if reason is not None:
            edge = _report_edge_key(row)
            if edge not in declined_edges:
                losses["attribution_edges"][reason] += 1
                declined_edges.add(edge)
            commit = None
        exact_observations = tuple(
            fact
            for fact in bindings.get((commit, row.session_id), ())
            if fact in row.session_commit_observations
        )
        eligible_outcomes = ci_by_commit.get(commit, ()) if exact_observations else ()
        factual_rows.append(
            replace(
                row,
                session_commit_observations=exact_observations,
                ci_outcomes=[
                    item
                    for item in eligible_outcomes
                    if item in row.ci_outcomes
                    and (item.org_id, item.outcome_id) in valid_ci_sources
                ],
            )
        )
    for unit, reasons in losses.items():
        for reason, count in sorted(reasons.items()):
            logger.warning(
                "Model report repository loss unit=%s reason=%s count=%d",
                unit,
                reason,
                count,
            )
    return ModelReportInputsResult(
        windowed_completions,
        factual_rows,
        {
            unit: dict(sorted(reasons.items()))
            for unit, reasons in losses.items()
            if reasons
        },
    )


def _unique_report_facts(facts: Iterable, identity_field: str) -> list:
    """Remove artifact fan-out while retaining contradictory copies for resolution."""
    grouped = defaultdict(list)
    for fact in facts:
        copies = grouped[(fact.org_id, getattr(fact, identity_field))]
        if fact not in copies:
            copies.append(fact)
    return [fact for key in sorted(grouped) for fact in grouped[key]]


def _declared_repository_key(row) -> RepositoryKey:
    return (
        IdentifiedRepositoryKey(row.org_id, row.repository_identity)
        if row.repository_identity is not None
        else LegacyRepositoryKey(row.org_id, row.repo)
    )


def _report_repository_label(
    key: RepositoryKey, context: RepositoryContext | None
) -> str:
    if context is not None:
        return context.repo_for(key)
    if isinstance(key, LegacyRepositoryKey):
        return key.repo
    raise ValueError("identified repository label requires complete context")


def _report_commit_key(
    row: AttributedCompletion, context: RepositoryContext | None
) -> CommitKey | None:
    if row.abandonment is not None:
        return None
    if context is not None:
        return context.commit_key(
            row.org_id,
            row.repo,
            row.commit_sha,
            repository_identity=row.repository_identity,
        )
    if row.repository_identity is None:
        return CommitKey(_declared_repository_key(row), row.commit_sha)
    return None


def _report_edge_key(row: AttributedCompletion) -> tuple:
    return (_declared_repository_key(row), row.commit_sha, row.session_id)


def _unobserved_edges(rows: Iterable[AttributedCompletion]) -> int:
    return len(
        {
            _report_edge_key(row)
            for row in rows
            if row.abandonment is None and not row.session_commit_observations
        }
    )


def _has_explicit_decision(
    attributed_completions: Iterable[AttributedCompletion],
) -> bool:
    return any(
        d.explicit
        for attributed_completion in attributed_completions
        for d in attributed_completion.decisions
    )


def _has_reward_signal(
    attributed_completions: Iterable[AttributedCompletion],
    *,
    repository_context: RepositoryContext | None = None,
    ci_population: Iterable[CIOutcome | CIOutcomeProjection] | None = None,
) -> bool:
    return bool(
        model_ci_trials(
            attributed_completions,
            repository_context=repository_context,
            ci_population=ci_population,
        )
    )


def mann_kendall_trend_test(
    values: Sequence[float],
    *,
    alpha: float = 0.05,
) -> MannKendallTrendTest:
    """Return a stdlib-only Mann-Kendall monotonic trend test.

    ``values`` must already be ordered by time. At least three windows are
    required; one or two windows return ``INSUFFICIENT_DATA`` instead of
    crashing or fabricating significance. Tied rates use the standard
    tie-corrected variance term, so flat sequences report
    ``NO_SIGNIFICANT_TREND`` with ``p_value=1.0``.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be between 0.0 and 1.0")

    n = len(values)
    s_statistic = sum(
        _direction(later - earlier) for earlier, later in combinations(values, 2)
    )
    if n < 3:
        return MannKendallTrendTest(
            sample_size=n,
            s_statistic=s_statistic,
            variance=0.0,
            z_statistic=0.0,
            p_value=None,
            alpha=alpha,
            status=TrendStatus.INSUFFICIENT_DATA,
            reason="need at least three non-empty windows to test a trend",
        )

    ties = Counter(values)
    tie_term = sum(
        count * (count - 1) * (2 * count + 5) for count in ties.values() if count > 1
    )
    variance = (n * (n - 1) * (2 * n + 5) - tie_term) / 18
    if variance <= 0:
        return MannKendallTrendTest(
            sample_size=n,
            s_statistic=s_statistic,
            variance=0.0,
            z_statistic=0.0,
            p_value=1.0,
            alpha=alpha,
            status=TrendStatus.NO_SIGNIFICANT_TREND,
            reason="all window rates are tied",
        )

    if s_statistic > 0:
        z_statistic = (s_statistic - 1) / sqrt(variance)
    elif s_statistic < 0:
        z_statistic = (s_statistic + 1) / sqrt(variance)
    else:
        z_statistic = 0.0
    p_value = 2 * (1 - _STANDARD_NORMAL.cdf(abs(z_statistic)))

    if p_value <= alpha and s_statistic > 0:
        status = TrendStatus.SIGNIFICANT_INCREASE
        reason = "window rates show a significant increasing monotonic trend"
    elif p_value <= alpha and s_statistic < 0:
        status = TrendStatus.SIGNIFICANT_DECREASE
        reason = "window rates show a significant decreasing monotonic trend"
    else:
        status = TrendStatus.NO_SIGNIFICANT_TREND
        reason = "window rates do not clear the configured significance threshold"

    return MannKendallTrendTest(
        sample_size=n,
        s_statistic=s_statistic,
        variance=variance,
        z_statistic=z_statistic,
        p_value=p_value,
        alpha=alpha,
        status=status,
        reason=reason,
    )


def _floor_utc_day(value: datetime) -> datetime:
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _window_index(captured_at: datetime, anchor: datetime, window_days: int) -> int:
    captured_at = (
        captured_at.replace(tzinfo=UTC)
        if captured_at.tzinfo is None
        else captured_at.astimezone(UTC)
    )
    seconds = window_days * 24 * 60 * 60
    return int((captured_at - anchor).total_seconds() // seconds)


def _trend_window(
    *,
    window_index: int,
    anchor: datetime,
    window_days: int,
    numerator: int,
    denominator: int,
) -> TemporalTrendWindow:
    start = anchor + timedelta(days=window_index * window_days)
    return TemporalTrendWindow(
        window_index=window_index,
        window_start=start,
        window_end=start + timedelta(days=window_days),
        numerator=numerator,
        denominator=denominator,
        rate=_ratio(numerator, denominator),
    )


def _trend_from_windows(
    model: str,
    metric: TrendMetric,
    window_days: int,
    windows: list[TemporalTrendWindow],
    alpha: float,
) -> ModelTemporalTrend:
    return ModelTemporalTrend(
        model=model,
        metric=metric,
        window_days=window_days,
        windows=windows,
        test=mann_kendall_trend_test([window.rate for window in windows], alpha=alpha),
    )


def build_signal_funnel_report(
    completions: Iterable[InferenceCall],
    attributed_completions: Iterable[AttributedCompletion],
    since_days: int | None = None,
    *,
    now: datetime | None = None,
    repository_context: RepositoryContext | None = None,
    ci_population: Iterable[CIOutcome | CIOutcomeProjection] | None = None,
) -> list[SignalFunnelReport]:
    """Aggregate per-model completion attrition through the signal funnel.

    The windowing and model attribution match ``build_model_report`` exactly:
    ``since_days`` filters by model-call observation time and attributed completions whose
    completion is missing or outside the window are omitted rather than
    guessed at. Rates are stage-retention rates over the prior meaningful
    denominator: ``attributed / completions_total`` for the first stage, then
    every signal-bearing stage over ``attributed`` because CI and decisions
    are parallel signals attached only after attribution. Zero denominators
    yield ``0.0``.
    """
    now = _report_now(now, repository_context)
    attributed_completions = list(attributed_completions)
    ci_population = prepare_report_ci(
        ci_population, attributed_completions, repository_context, now
    )
    windowed_completions, windowed_attributed_completions = model_report_inputs(
        completions,
        attributed_completions,
        since_days,
        now,
        repository_context=repository_context,
        ci_population=ci_population,
    )
    inference_call_ids = {
        inference_fact_id(completion) for completion in windowed_completions
    }

    completions_by_model: dict[str, list[InferenceCall]] = defaultdict(list)
    for completion in windowed_completions:
        if (model := inference_model(completion)) is not None:
            completions_by_model[model].append(completion)

    attributed_completions_by_completion: dict[str, list[AttributedCompletion]] = (
        defaultdict(list)
    )
    for attributed_completion in windowed_attributed_completions:
        if (
            attributed_completion.abandonment is None
            and attributed_completion.inference_call_id in inference_call_ids
        ):
            attributed_completions_by_completion[
                attributed_completion.inference_call_id
            ].append(attributed_completion)

    rows: list[SignalFunnelReport] = []
    for model in sorted(completions_by_model):
        model_inference_call_ids = {
            inference_fact_id(completion) for completion in completions_by_model[model]
        }
        attributed_ids = {
            inference_call_id
            for inference_call_id in model_inference_call_ids
            if attributed_completions_by_completion.get(inference_call_id)
        }
        ci_linked_ids = {
            inference_call_id
            for inference_call_id in attributed_ids
            if any(
                attributed_completion.ci_outcomes
                for attributed_completion in attributed_completions_by_completion[
                    inference_call_id
                ]
            )
        }
        decision_ids = {
            inference_call_id
            for inference_call_id in attributed_ids
            if _has_explicit_decision(
                attributed_completions_by_completion[inference_call_id]
            )
        }
        eligible_ids = {
            inference_call_id
            for inference_call_id in attributed_ids
            if inference_call_id in decision_ids
            or _has_reward_signal(
                attributed_completions_by_completion[inference_call_id],
                repository_context=repository_context,
                ci_population=ci_population,
            )
        }

        completions_total = len(model_inference_call_ids)
        attributed = len(attributed_ids)
        ci_linked = len(ci_linked_ids)
        has_decision = len(decision_ids)
        training_row_eligible = len(eligible_ids)
        rows.append(
            SignalFunnelReport(
                model=model,
                since_days=since_days,
                completions_total=completions_total,
                attributed=attributed,
                attribution_rate=_ratio(attributed, completions_total),
                ci_linked=ci_linked,
                ci_linked_retention_rate=_ratio(ci_linked, attributed),
                has_decision=has_decision,
                has_decision_retention_rate=_ratio(has_decision, attributed),
                training_row_eligible=training_row_eligible,
                training_row_eligible_retention_rate=_ratio(
                    training_row_eligible, attributed
                ),
                session_commit_unobserved=_unobserved_edges(
                    item
                    for item in windowed_attributed_completions
                    if item.inference_call_id in model_inference_call_ids
                ),
            )
        )
    return rows


def build_temporal_trend_report(
    completions: Iterable[InferenceCall],
    attributed_completions: Iterable[AttributedCompletion],
    since_days: int | None = None,
    *,
    now: datetime | None = None,
    policy: OutcomeReportPolicy | None = None,
    window_days: int = 7,
    alpha: float = 0.05,
    cohort_start: datetime | None = None,
    repository_context: RepositoryContext | None = None,
    ci_population: Iterable[CIOutcome | CIOutcomeProjection] | None = None,
) -> list[ModelTemporalTrend]:
    """Bucket per-model outcome rates into captured-at windows and test drift.

    The report uses non-overlapping ``window_days`` buckets anchored to the
    report window start (``now - since_days`` when supplied, otherwise the
    earliest completion's UTC day). An explicit ``cohort_start`` anchors an
    already selected cohort independently of the ``now`` evidence cutoff.
    It computes Mann-Kendall trend tests for
    the same two proportion metrics that support outcome-report inference:
    ``attribution_rate`` (distinct attributed completions / completions) and
    ``ci_pass_rate`` (CI-passed trials / CI-linked trials, using
    ``OutcomeReportPolicy.ci_grain``). Empty-denominator buckets are omitted
    from the test rather than treated as zero-rate evidence.
    """
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be between 0.0 and 1.0")

    now = _report_now(now, repository_context)
    policy = policy if policy is not None else OutcomeReportPolicy()
    completions_list = list(completions)
    attributed_completions_list = list(attributed_completions)
    ci_population = prepare_report_ci(
        ci_population, attributed_completions_list, repository_context, now
    )
    windowed_completions, windowed_attributed_completions = model_report_inputs(
        completions_list,
        attributed_completions_list,
        since_days,
        now,
        repository_context=repository_context,
        ci_population=ci_population,
    )
    if not windowed_completions:
        return []

    anchor = (
        cohort_start.astimezone(UTC)
        if cohort_start is not None
        else _floor_utc_day(now - timedelta(days=since_days))
        if since_days is not None
        else min(_floor_utc_day(inference_observed_at(c)) for c in windowed_completions)
    )
    completions_by_id = {inference_fact_id(c): c for c in windowed_completions}

    attribution_denominators: dict[str, Counter[int]] = defaultdict(Counter)
    attribution_successes: dict[str, dict[int, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    attributed_completions_by_model_window: dict[
        str, dict[int, list[AttributedCompletion]]
    ] = defaultdict(lambda: defaultdict(list))

    for completion in windowed_completions:
        model = inference_model(completion)
        if model is None:
            continue
        index = _window_index(inference_observed_at(completion), anchor, window_days)
        attribution_denominators[model][index] += 1

    for attributed_completion in windowed_attributed_completions:
        completion = completions_by_id.get(attributed_completion.inference_call_id)
        if completion is None:
            continue
        model = inference_model(completion)
        if model is None:
            continue
        index = _window_index(inference_observed_at(completion), anchor, window_days)
        if attributed_completion.abandonment is None:
            attribution_successes[model][index].add(
                attributed_completion.inference_call_id
            )
        attributed_completions_by_model_window[model][index].append(
            attributed_completion
        )

    trends: list[ModelTemporalTrend] = []
    for model in sorted(attribution_denominators):
        attribution_windows = [
            _trend_window(
                window_index=index,
                anchor=anchor,
                window_days=window_days,
                numerator=len(attribution_successes[model].get(index, set())),
                denominator=denominator,
            )
            for index, denominator in sorted(attribution_denominators[model].items())
            if denominator
        ]
        trends.append(
            _trend_from_windows(
                model,
                TrendMetric.ATTRIBUTION_RATE,
                window_days,
                attribution_windows,
                alpha,
            )
        )

        ci_windows: list[TemporalTrendWindow] = []
        for index, model_attributed_completions in sorted(
            attributed_completions_by_model_window[model].items()
        ):
            ci_linked, ci_passed = _ci_counts(
                model_attributed_completions,
                policy.ci_grain,
                policy.ci_resolution,
                repository_context=repository_context,
                ci_population=ci_population,
            )
            if ci_linked:
                ci_windows.append(
                    _trend_window(
                        window_index=index,
                        anchor=anchor,
                        window_days=window_days,
                        numerator=ci_passed,
                        denominator=ci_linked,
                    )
                )
        trends.append(
            _trend_from_windows(
                model,
                TrendMetric.CI_PASS_RATE,
                window_days,
                ci_windows,
                alpha,
            )
        )
    return [
        replace(
            item,
            session_commit_unobserved=_unobserved_edges(
                row
                for row in windowed_attributed_completions
                if inference_model(completions_by_id[row.inference_call_id])
                == item.model
            ),
        )
        for item in trends
    ]


def _ci_rates_by_repo(
    completions: Iterable[InferenceCall],
    attributed_completions: Iterable[AttributedCompletion],
    since_days: int | None,
    now: datetime,
    grain: CIGrain,
    ci_resolution_policy: CIResolutionPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
    ci_population: Iterable[CIOutcome | CIOutcomeProjection] | None = None,
) -> dict[str, dict[RepositoryKey, StratifiedRate]]:
    attributed_completions = list(attributed_completions)
    ci_population = prepare_report_ci(
        ci_population, attributed_completions, repository_context, now
    )
    windowed_completions, windowed_attributed_completions = model_report_inputs(
        completions,
        attributed_completions,
        since_days,
        now,
        repository_context=repository_context,
        ci_population=ci_population,
    )
    completions_by_id = {inference_fact_id(c): c for c in windowed_completions}
    attributed_completions_by_model_repo: dict[
        str, dict[RepositoryKey, list[AttributedCompletion]]
    ] = defaultdict(lambda: defaultdict(list))
    for attributed_completion in windowed_attributed_completions:
        completion = completions_by_id.get(attributed_completion.inference_call_id)
        if completion is None:
            continue
        model = inference_model(completion)
        if model is None:
            continue
        commit = _report_commit_key(attributed_completion, repository_context)
        if commit is not None:
            attributed_completions_by_model_repo[model][commit.repository].append(
                attributed_completion
            )

    rates: dict[str, dict[RepositoryKey, StratifiedRate]] = {}
    for (
        model,
        attributed_completions_by_repo,
    ) in attributed_completions_by_model_repo.items():
        rates[model] = {}
        for repo, repo_attributed_completions in attributed_completions_by_repo.items():
            ci_linked, ci_passed = _ci_counts(
                repo_attributed_completions,
                grain,
                ci_resolution_policy,
                repository_context=repository_context,
                ci_population=ci_population,
            )
            rates[model][repo] = _rate(model, ci_passed, ci_linked)
    return rates


def _aggregate_rate(
    row: ModelOutcomeReport, metric: StratificationMetric
) -> StratifiedRate:
    if metric == StratificationMetric.CI_PASS_RATE:
        return _rate(row.model, row.ci_passed, row.ci_linked)
    return _rate(row.model, row.attributed_inference_calls, row.completions)


def _build_pairwise_stratification_check(
    rows: list[ModelOutcomeReport],
    rates_by_model_repo: dict[str, dict[RepositoryKey, StratifiedRate]],
    metric: StratificationMetric,
    *,
    repository_context: RepositoryContext | None = None,
) -> StratificationCheck:
    models = [row.model for row in rows]
    if len(models) < 2:
        return StratificationCheck(
            metric=metric,
            status=StratificationStatus.NOT_ENOUGH_MODELS,
            checked_repos=0,
            comparisons=[],
            reason="need at least two models to compare aggregate direction",
        )

    repos = sorted(
        {
            repo
            for rates_by_repo in rates_by_model_repo.values()
            for repo, rate in rates_by_repo.items()
            if rate.denominator
        },
        key=repository_sort_key,
    )

    rate_cells = [
        (model, repo, rate)
        for model in sorted(rates_by_model_repo)
        for repo, rate in sorted(
            rates_by_model_repo[model].items(),
            key=lambda pair: repository_sort_key(pair[0]),
        )
        if rate.denominator
    ]
    shrunk_by_repo: dict[RepositoryKey, dict[str, ShrunkStratifiedRate]] = defaultdict(
        dict
    )
    for (model, repo, _), shrunk in zip(
        rate_cells,
        shrink_stratified_rates([rate for _, _, rate in rate_cells]),
        strict=True,
    ):
        shrunk_by_repo[repo][model] = shrunk
    shrunk_rates = [
        RepositoryShrunkRates(
            org_id=repo.org_id,
            repo=_report_repository_label(repo, repository_context),
            repository_identity=repo.identity
            if isinstance(repo, IdentifiedRepositoryKey)
            else None,
            model_rates=dict(sorted(rates.items())),
        )
        for repo, rates in sorted(
            shrunk_by_repo.items(), key=lambda pair: repository_sort_key(pair[0])
        )
    ]

    rows_by_model = {row.model: row for row in rows}
    comparisons_out: list[StratificationComparison] = []
    adjusted_out: list[MantelHaenszelRiskDifference] = []
    for left_model, right_model in combinations(models, 2):
        left_aggregate = _aggregate_rate(rows_by_model[left_model], metric)
        right_aggregate = _aggregate_rate(rows_by_model[right_model], metric)
        if left_aggregate.denominator == 0 or right_aggregate.denominator == 0:
            continue

        aggregate_delta = left_aggregate.rate - right_aggregate.rate
        aggregate_direction = _direction(aggregate_delta)

        reversals: list[StratificationReversal] = []
        comparable_strata: list[tuple[StratifiedRate, StratifiedRate]] = []
        compared_repos = 0
        skipped_repos = 0
        for repo in repos:
            left = rates_by_model_repo.get(left_model, {}).get(repo)
            right = rates_by_model_repo.get(right_model, {}).get(repo)
            if (
                left is None
                or right is None
                or left.denominator == 0
                or right.denominator == 0
            ):
                skipped_repos += 1
                continue

            compared_repos += 1
            comparable_strata.append((left, right))
            repo_delta = left.rate - right.rate
            if aggregate_direction and _direction(repo_delta) == -aggregate_direction:
                reversals.append(
                    StratificationReversal(
                        repo=_report_repository_label(repo, repository_context),
                        org_id=repo.org_id,
                        repository_identity=repo.identity
                        if isinstance(repo, IdentifiedRepositoryKey)
                        else None,
                        left=left,
                        right=right,
                        aggregate_left=left_aggregate,
                        aggregate_right=right_aggregate,
                        aggregate_delta=aggregate_delta,
                        repo_delta=repo_delta,
                    )
                )

        adjusted = _mantel_haenszel_risk_difference(
            left_model,
            right_model,
            left_aggregate,
            right_aggregate,
            comparable_strata,
            skipped_repos=skipped_repos,
        )
        if adjusted is not None:
            adjusted_out.append(adjusted)

        if aggregate_direction == 0:
            continue
        if compared_repos < 2:
            continue

        comparisons_out.append(
            StratificationComparison(
                left_model=left_model,
                right_model=right_model,
                aggregate_left=left_aggregate,
                aggregate_right=right_aggregate,
                aggregate_delta=aggregate_delta,
                compared_repos=compared_repos,
                skipped_repos=skipped_repos,
                reversals=reversals,
            )
        )

    if len(repos) < 2:
        return StratificationCheck(
            metric=metric,
            status=StratificationStatus.NOT_ENOUGH_REPOS,
            checked_repos=len(repos),
            comparisons=[],
            reason="need at least two repos with comparable metric data",
            adjusted_comparisons=adjusted_out,
            shrunk_rates=shrunk_rates,
        )

    if not comparisons_out:
        return StratificationCheck(
            metric=metric,
            status=StratificationStatus.NO_COMPARABLE_DATA,
            checked_repos=len(repos),
            comparisons=[],
            reason=(
                "no model pair had non-zero aggregate denominators, a non-tied "
                "aggregate direction, and at least two comparable repos"
            ),
            adjusted_comparisons=adjusted_out,
            shrunk_rates=shrunk_rates,
        )

    reversal_count = sum(len(comparison.reversals) for comparison in comparisons_out)
    if reversal_count:
        return StratificationCheck(
            metric=metric,
            status=StratificationStatus.REVERSAL_DETECTED,
            checked_repos=len(repos),
            comparisons=comparisons_out,
            reason=f"{reversal_count} repo-level reversal(s) detected",
            adjusted_comparisons=adjusted_out,
            shrunk_rates=shrunk_rates,
        )

    return StratificationCheck(
        metric=metric,
        status=StratificationStatus.NO_REVERSAL_DETECTED,
        checked_repos=len(repos),
        comparisons=comparisons_out,
        reason="aggregate direction matched every comparable repo",
        adjusted_comparisons=adjusted_out,
        shrunk_rates=shrunk_rates,
    )


def build_stratification_checks(
    completions: Iterable[InferenceCall],
    attributed_completions: Iterable[AttributedCompletion],
    rows: Iterable[ModelOutcomeReport] | None = None,
    since_days: int | None = None,
    *,
    now: datetime | None = None,
    policy: OutcomeReportPolicy | None = None,
    repository_context: RepositoryContext | None = None,
    ci_population: Iterable[CIOutcome | CIOutcomeProjection] | None = None,
) -> list[StratificationCheck]:
    """Check model outcome comparisons for repo-level direction reversals.

    ``ci_pass_rate`` is checkable because both aggregate and per-repo
    denominators are reward-bearing commits/attributed completions already carried by
    ``AttributedCompletion.repo``. The check uses the same ``OutcomeReportPolicy.ci_grain``
    as the aggregate report, so the aggregate and per-repo counts have the
    same unit of analysis.

    ``attribution_rate`` is reported explicitly as not checkable today:
    A model-call fact has no canonical repo field, and ``AttributedCompletion.repo`` exists
    only after a completion has an attribution. Using it for the
    attribution denominator would silently drop unattributed completions, exactly
    the population the metric is meant to count.
    """
    now = _report_now(now, repository_context)
    policy = policy if policy is not None else OutcomeReportPolicy()
    completions_list = list(completions)
    attributed_completions_list = list(attributed_completions)
    ci_population = prepare_report_ci(
        ci_population, attributed_completions_list, repository_context, now
    )
    # Recompute aggregates from source artifacts even when a caller supplies
    # legacy precomputed rows. Those counters carry no qualified Fact evidence.
    rows_list = build_model_report(
        completions_list,
        attributed_completions_list,
        since_days,
        now=now,
        policy=policy,
        repository_context=repository_context,
        ci_population=ci_population,
    )

    ci_rates = _ci_rates_by_repo(
        completions_list,
        attributed_completions_list,
        since_days,
        now,
        policy.ci_grain,
        policy.ci_resolution,
        repository_context=repository_context,
        ci_population=ci_population,
    )
    ci_check = _build_pairwise_stratification_check(
        rows_list,
        ci_rates,
        StratificationMetric.CI_PASS_RATE,
        repository_context=repository_context,
    )
    attribution_check = StratificationCheck(
        metric=StratificationMetric.ATTRIBUTION_RATE,
        status=StratificationStatus.NOT_CHECKABLE,
        checked_repos=0,
        comparisons=[],
        reason=(
            "InferenceCall has no canonical repo field; AttributedCompletion.repo only exists "
            "for attributed completions, so per-repo attribution denominators "
            "would exclude unattributed inference calls"
        ),
    )
    _, source_rows = model_report_inputs(
        completions_list,
        attributed_completions_list,
        since_days,
        now,
        repository_context=repository_context,
        ci_population=ci_population,
    )
    return [
        replace(ci_check, session_commit_unobserved=_unobserved_edges(source_rows)),
        attribution_check,
    ]


def build_model_report(
    completions: Iterable[InferenceCall],
    attributed_completions: Iterable[AttributedCompletion],
    since_days: int | None = None,
    *,
    scope: OperationalReportScope | None = None,
    decisions: Iterable[DeveloperDecision] | None = None,
    decision_identities: Iterable[InferenceCallIdentity] | None = None,
    now: datetime | None = None,
    policy: OutcomeReportPolicy | None = None,
    quarantine_revision: int = 0,
    policy_digest: str | None = None,
    fate_result: FateResult | None = None,
    repository_context: RepositoryContext | None = None,
    ci_population: Iterable[CIOutcome | CIOutcomeProjection] | None = None,
) -> list[ModelOutcomeReport]:
    """Aggregate completions + attributed completions into one ``ModelOutcomeReport`` per
    model, sorted by ``model`` ascending — a pure function of the given
    facts/derived attributed_completions, ``since_days``, and ``policy`` alone (ADR 0001),
    so re-running it, or running it over the same inputs read in a different
    order, reproduces an identical report.

    When ``decisions`` is supplied, ``decision_identities`` defines its attachment population,
    including when empty; omission uses all supplied completions before cohort
    filtering. Identity witnesses never enter metric denominators.

    **The window applies to both sides at once.** ``since_days``, when
    given, filters on the inference call's ``observed_at``: a call outside
    the window is dropped from the denominator (``completions``), and any
    attributed_completion whose completion falls outside the window is dropped from every
    numerator derived from it — never one without the other, or the rates
    would compare two different populations. ``now`` defaults to
    ``datetime.now(UTC)``; pass a fixed value to make the window boundary
    deterministic in tests.

    **Model attribution for a attributed completion** is read off its own completion
    (``completions_by_id[attributed_completion.inference_call_id].model``) — the attributed-completion layer
    does not carry ``model`` directly (it is a completion field, not one of
    the attributed completion's own ADR 0004 fields). The lookup uses the artifact's
    ``inference_call_id`` — never a re-derivation of the attribution itself. A
    attributed completion whose ``inference_call_id`` cannot be found among the
    inference calls
    (a caller passing a mismatched pair) contributes nothing, rather than
    guessed at.

    **CI counting is a pluggable grain, commit by default.**
    ``ci_linked``/``ci_passed`` are computed by ``_ci_counts``, dispatched on
    ``policy.ci_grain`` (``OutcomeReportPolicy``, default-constructed —
    i.e. ``CIGrain.COMMIT`` — when ``policy`` is not given). See ``CIGrain``
    for what each branch counts and why ``COMMIT`` is the default.

    **``ci_failures_by_workflow`` dedups by ``outcome_id``, each failed run
    counted once.** Assembly attaches the same ``ci_outcomes`` list to
    *every* attributed completion of a commit — the same pseudo-replication
    ``ci_linked``/``ci_passed`` guard against above — so counting FAILED
    outcomes per attributed completion would read a 10-file commit failing Lint once as
    ``Lint=10``, a 10:1 "failure mode" that is 1:1 in actual CI runs. Every
    ``CIOutcome`` has a unique ``outcome_id`` regardless of how many attributed completions
    it got attached to, so keying a dict on it collapses the duplicates
    before ``workflow_name`` frequencies are counted.
    ``explicit_rejects_by_agent_harness`` counts explicit
    rejected decisions keyed by ``DeveloperDecision.agent_harness``.

    **Direct decisions retain their own population.** When ``decisions`` is
    supplied, it is authoritative for acceptance and Fate keys. The shared
    attachment checks uniqueness over supplied identity evidence (or every supplied
    Inference call) before cohort filtering, then requires organization and Session agreement.
    Only decisions captured through ``now`` (or ``scope.as_of``) qualify.
    Inference calls or identity projections must retain response tool-call IDs;
    metadata summaries cannot serve as attachment candidates. When ``decisions`` is omitted, the report retains
    the supplied canonical artifacts' assembled decision population.
    Counts deduplicate by ``decision_id`` so artifact fan-out or repeated input
    cannot multiply a captured decision. ``fate_result`` must already reflect
    the evidence time boundary; Fate records carry no capture timestamp.

    Every rate is ``0.0`` on a zero denominator — never a crash, never NaN.
    """
    if scope is not None and since_days is not None:
        raise ValueError("scope and since_days are mutually exclusive")
    now = scope.as_of if scope is not None else _report_now(now, repository_context)
    policy = policy if policy is not None else OutcomeReportPolicy()
    provenance = Provenance(
        policy_version=policy.policy_version,
        quarantine_revision=quarantine_revision,
        policy_digest=policy_digest,
    )

    all_completions = list(completions)
    all_attributed_completions = list(attributed_completions)
    ci_population = prepare_report_ci(
        ci_population, all_attributed_completions, repository_context, now
    )
    decisions_by_completion = (
        join_decisions_by_call_id_result(
            all_completions if decision_identities is None else decision_identities,
            (
                decision
                for decision in decisions
                if decision.captured_at.astimezone(UTC) <= now.astimezone(UTC)
            ),
        ).decisions_by_completion
        if decisions is not None
        else None
    )
    if scope is not None:
        scoped_completions, scoped_artifacts = scope_model_report_evidence(
            all_completions, all_attributed_completions, scope
        )
        windowed_completions, windowed_attributed_completions = model_report_inputs(
            scoped_completions,
            scoped_artifacts,
            None,
            now,
            repository_context=repository_context,
            ci_population=ci_population,
        )
        since_days = max(
            1, ceil((scope.cohort_end - scope.cohort_start).total_seconds() / 86_400)
        )
    else:
        windowed_completions, windowed_attributed_completions = model_report_inputs(
            all_completions,
            all_attributed_completions,
            since_days,
            now,
            repository_context=repository_context,
            ci_population=ci_population,
        )
    completions_by_id = {inference_fact_id(c): c for c in windowed_completions}

    completions_by_model: dict[str, list[InferenceCall]] = defaultdict(list)
    for completion in windowed_completions:
        if (model := inference_model(completion)) is not None:
            completions_by_model[model].append(completion)

    attributed_completions_by_model: dict[str, list[AttributedCompletion]] = (
        defaultdict(list)
    )
    for attributed_completion in windowed_attributed_completions:
        completion = completions_by_id.get(attributed_completion.inference_call_id)
        if completion is None:
            continue
        model = inference_model(completion)
        if model is not None:
            attributed_completions_by_model[model].append(attributed_completion)

    fates = fate_result.fates if fate_result is not None else []

    rows: list[ModelOutcomeReport] = []
    for model in sorted(completions_by_model):
        model_attributed_completions = attributed_completions_by_model.get(model, [])
        attribution_rows = [
            item for item in model_attributed_completions if item.abandonment is None
        ]

        total = len(completions_by_model[model])
        attributed = len({t.inference_call_id for t in attribution_rows})
        attribution_rate = _ratio(attributed, total)
        attribution_rate_ci = wilson_score_interval(attributed, total)

        ci_linked, ci_passed = _ci_counts(
            model_attributed_completions,
            policy.ci_grain,
            policy.ci_resolution,
            repository_context=repository_context,
            ci_population=ci_population,
        )
        ci_pass_rate = _ratio(ci_passed, ci_linked)
        ci_pass_rate_ci = wilson_score_interval(ci_passed, ci_linked)

        # Count only resolved failing workflow lineages. Earlier failed
        # attempts in a passing retry remain evidence, not report failures.
        failed_workflows: dict[tuple, str] = {}
        for trial in model_ci_trials(
            model_attributed_completions,
            CIGrain.COMMIT,
            policy.ci_resolution,
            repository_context=repository_context,
            ci_population=ci_population,
        ):
            resolution = trial.resolution
            if resolution.verdict != CIResult.FAILED:
                continue
            for workflow in resolution.workflow_resolutions:
                if workflow.verdict != CIResult.FAILED:
                    continue
                workflow_key = (
                    trial.commit,
                    workflow.provider,
                    workflow.workflow_id,
                    workflow.workflow_path,
                    workflow.workflow_name,
                )
                failed_workflows[workflow_key] = workflow.workflow_name
        ci_failures_by_workflow = Counter(failed_workflows.values())

        model_decisions = (
            [
                decision
                for call in completions_by_model[model]
                for decision in decisions_by_completion.get(inference_fact_id(call), ())
            ]
            if decisions_by_completion is not None
            else [
                decision
                for item in model_attributed_completions
                for decision in item.decisions
            ]
        )
        explicit = {d.decision_id: d for d in model_decisions if d.explicit}
        explicit_accepts = sum(1 for d in explicit.values() if d.accepted)
        explicit_rejects = sum(1 for d in explicit.values() if not d.accepted)
        explicit_rejects_by_agent_harness = Counter(
            str(d.agent_harness) for d in explicit.values() if not d.accepted
        )

        decision_keys = {
            (d.org_id, d.agent_harness, d.session_id, d.call_id)
            for d in model_decisions
            if d.call_id is not None
        }
        explicit_accept_keys = {
            (d.org_id, d.agent_harness, d.session_id, d.call_id)
            for d in model_decisions
            if d.call_id is not None and d.explicit and d.accepted
        }
        model_fates: Counter[str] = Counter()
        model_explicit_accept_fates: Counter[str] = Counter()
        model_external_change_fates: Counter[str] = Counter()
        seen_observations: set[str] = set()
        for fate in fates:
            key = (
                fate.org_id,
                fate.agent_harness,
                fate.session_id,
                fate.call_id,
            )
            if key not in decision_keys or fate.observation_id in seen_observations:
                continue
            seen_observations.add(fate.observation_id)
            label = fate.fate.value
            model_fates[label] += 1
            if key in explicit_accept_keys:
                model_explicit_accept_fates[label] += 1
            if (fate.external_lines_added or 0) > 0 or (
                fate.external_lines_removed or 0
            ) > 0:
                model_external_change_fates[label] += 1

        scores = [t.similarity_score for t in attribution_rows]
        mean_similarity = sum(scores) / len(scores) if scores else 0.0

        rows.append(
            ModelOutcomeReport(
                model=model,
                since_days=since_days,
                session_commit_unobserved=len(
                    {
                        _report_edge_key(item)
                        for item in attribution_rows
                        if not item.session_commit_observations
                    }
                ),
                session_commit_observation_ids=tuple(
                    sorted(
                        {
                            fact.observation_id
                            for item in attribution_rows
                            for fact in item.session_commit_observations
                        }
                    )
                ),
                completions=total,
                attributed_inference_calls=attributed,
                attribution_rate=attribution_rate,
                attribution_rate_ci=attribution_rate_ci,
                ci_linked=ci_linked,
                ci_passed=ci_passed,
                ci_pass_rate=ci_pass_rate,
                ci_pass_rate_ci=ci_pass_rate_ci,
                explicit_accepts=explicit_accepts,
                explicit_rejects=explicit_rejects,
                mean_similarity=mean_similarity,
                provenance=provenance,
                grain=policy.ci_grain,
                ci_failures_by_workflow=dict(sorted(ci_failures_by_workflow.items())),
                explicit_rejects_by_agent_harness=dict(
                    sorted(explicit_rejects_by_agent_harness.items())
                ),
                fates=dict(sorted(model_fates.items())),
                explicit_accept_fates=dict(sorted(model_explicit_accept_fates.items())),
                fates_with_external_changes=dict(
                    sorted(model_external_change_fates.items())
                ),
            )
        )
    return rows


def build_recovery_yield_report(
    ci_outcomes: Iterable[CIOutcome],
    recovery: RecoveryResult,
) -> RecoveryYieldReport:
    """Aggregate CI outcomes + recovery result into one yield summary row.

    Pure over the given facts and derived result: callers decide the org and
    quarantine/window read policy before passing values in. Rates are ``0.0``
    on a zero denominator.
    """
    total_failed = sum(
        1 for outcome in ci_outcomes if outcome.result == CIResult.FAILED
    )
    recovery_pairs = len(recovery.pairs)
    return RecoveryYieldReport(
        total_failed_ci_runs=total_failed,
        recovery_pairs=recovery_pairs,
        recovery_yield_rate=recovery_pairs / total_failed if total_failed else 0.0,
        skipped=Counter(recovery.skipped),
        diff_size_distribution=_recovery_diff_size_distribution(recovery),
    )


def build_model_report_result(
    completions: Iterable[InferenceCall],
    attributed_completions: Iterable[AttributedCompletion],
    since_days: int | None = None,
    *,
    scope: OperationalReportScope | None = None,
    decisions: Iterable[DeveloperDecision] | None = None,
    decision_identities: Iterable[InferenceCallIdentity] | None = None,
    now: datetime | None = None,
    policy: OutcomeReportPolicy | None = None,
    include_trends: bool = False,
    attribution_share: list[RepoAttributionShare] | None = None,
    attribution_alerts: list[AttributionShareAlert] | None = None,
    abandonment: AbandonmentSummary | None = None,
    abandonment_is_scoped: bool = False,
    quarantine_revision: int = 0,
    policy_digest: str | None = None,
    fate_result: FateResult | None = None,
    repository_context: RepositoryContext | None = None,
    ci_population: Iterable[CIOutcome | CIOutcomeProjection] | None = None,
) -> ModelOutcomeReportResult:
    """Build the model report and repo-stratification checks together."""
    if scope is not None and since_days is not None:
        raise ValueError("scope and since_days are mutually exclusive")
    if scope is not None and abandonment is not None and not abandonment_is_scoped:
        raise ValueError("scope requires a boundary-native scoped abandonment summary")
    now = scope.as_of if scope is not None else _report_now(now, repository_context)
    policy = policy if policy is not None else OutcomeReportPolicy()
    completions_list = list(completions)
    attributed_completions_list = list(attributed_completions)
    ci_population = prepare_report_ci(
        ci_population, attributed_completions_list, repository_context, now
    )
    rows = build_model_report(
        completions_list,
        attributed_completions_list,
        since_days,
        scope=scope,
        decisions=decisions,
        decision_identities=decision_identities,
        now=now,
        policy=policy,
        quarantine_revision=quarantine_revision,
        policy_digest=policy_digest,
        fate_result=fate_result,
        repository_context=repository_context,
        ci_population=ci_population,
    )
    if scope is not None:
        completions_list, attributed_completions_list = scope_model_report_evidence(
            completions_list, attributed_completions_list, scope
        )
        now = scope.as_of
        # The cohort is selected. A trailing window relative to as_of would
        # move its lower bound when supporting evidence arrives later.
    stratification = build_stratification_checks(
        completions_list,
        attributed_completions_list,
        rows,
        since_days,
        now=now,
        policy=policy,
        repository_context=repository_context,
        ci_population=ci_population,
    )
    signal_funnel = build_signal_funnel_report(
        completions_list,
        attributed_completions_list,
        since_days,
        now=now,
        repository_context=repository_context,
        ci_population=ci_population,
    )
    if scope is not None:
        cohort_days = max(
            1, ceil((scope.cohort_end - scope.cohort_start).total_seconds() / 86_400)
        )
        signal_funnel = [replace(row, since_days=cohort_days) for row in signal_funnel]
    trends = (
        build_temporal_trend_report(
            completions_list,
            attributed_completions_list,
            since_days,
            now=now,
            policy=policy,
            cohort_start=scope.cohort_start if scope is not None else None,
            repository_context=repository_context,
            ci_population=ci_population,
        )
        if include_trends
        else []
    )
    return ModelOutcomeReportResult(
        rows=rows,
        stratification=stratification,
        attribution_share=attribution_share or [],
        attribution_alerts=attribution_alerts or [],
        signal_funnel=signal_funnel,
        trends=trends,
        abandonment=abandonment or AbandonmentSummary(),
        fate_skipped=(
            dict(sorted(fate_result.skipped.items())) if fate_result is not None else {}
        ),
        fate_provenance=fate_result.provenance if fate_result is not None else None,
        ci_skipped=_report_ci_losses(ci_population),
        repository_skipped=model_report_inputs_result(
            completions_list,
            attributed_completions_list,
            since_days,
            now,
            repository_context=repository_context,
            ci_population=ci_population,
        ).repository_skipped,
    )


def with_model_assembly_diagnostics(
    result: ModelOutcomeReportResult,
    assembly: AttributedCompletionAssemblyResult,
) -> ModelOutcomeReportResult:
    """Retain assembly losses even when no canonical row can carry its source.

    This evaluated assembly population is separate from preload observations and
    report edges. These diagnostics are not additive across population units.
    """
    reasons = {
        reason: count
        for reason, count in sorted(assembly.skipped.items())
        if reason in REPOSITORY_IDENTITY_SKIP_REASONS
    }
    if not reasons:
        return result
    return replace(
        result,
        repository_skipped={**result.repository_skipped, "assembly_sources": reasons},
    )


def derive_model_report_attribution_share(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    since_days: int | None = None,
    *,
    now: datetime | None = None,
    pushes: list[Push] | None = None,
    candidate_limit: int | None = None,
    note_sessions_by_commit: dict[tuple[str, str], frozenset[str]] | None = None,
    note_session_ids_by_commit: dict[CommitKey, frozenset[str]] | None = None,
    repository_context: RepositoryContext | None = None,
) -> tuple[list[RepoAttributionShare], list[AttributionShareAlert]]:
    """Derive attribution share and alerts over the model report window.

    A bounded report uses the same lower time boundary for push facts that
    ``build_model_report`` uses for inference calls. Its alert baseline is the
    preceding ``AttributionSharePolicy.baseline_window_days`` period. An
    all-history report includes every push and has no preceding baseline.
    """
    now = _report_now(now, repository_context)
    if pushes is None:
        with store.read_snapshot() as snapshot:
            context = repository_context or read_repository_context(
                snapshot, org_id, as_of=now
            )
            return derive_model_report_attribution_share(
                snapshot,
                mirrors,
                org_id,
                since_days,
                now=now,
                pushes=snapshot.read_pushes(org_id, captured_through=now),
                candidate_limit=candidate_limit,
                note_sessions_by_commit=note_sessions_by_commit,
                note_session_ids_by_commit=note_session_ids_by_commit,
                repository_context=context,
            )
    pushes = [
        push for push in pushes if push.org_id == org_id and push.captured_at <= now
    ]

    def key_of(push: Push) -> RepositoryKey | None:
        if repository_context is not None:
            return repository_context.resolve_fact(push).key
        if push.repository_id is None:
            return LegacyRepositoryKey(push.org_id, push.repo)
        return None

    def row_key(row: RepoAttributionShare) -> RepositoryKey:
        return (
            IdentifiedRepositoryKey(row.org_id, row.repository_identity)
            if row.repository_identity is not None
            else LegacyRepositoryKey(row.org_id, row.repo)
        )

    policy = AttributionSharePolicy(min_cases_for_decline_verdict=MIN_DRIFT_CASES)
    if since_days is None:
        current_pushes = pushes
        if pushes:
            first = min(push.captured_at for push in pushes)
            last = max(push.captured_at for push in pushes)
            window_days = max(1, ceil((last - first).total_seconds() / 86_400))
        else:
            window_days = 1
        baseline_pushes: list[Push] = []
        baseline_start = None
        cutoff = None
    else:
        cutoff = now - timedelta(days=since_days)
        current_pushes = [push for push in pushes if push.captured_at >= cutoff]
        window_days = since_days
        baseline_start = cutoff - timedelta(days=policy.baseline_window_days)
        baseline_pushes = [
            push for push in pushes if baseline_start <= push.captured_at < cutoff
        ]

    policy = replace(policy, window_days=window_days)
    current = derive_attribution_share(
        store,
        mirrors,
        org_id,
        policy,
        pushes=current_pushes,
        candidate_limit=candidate_limit,
        note_sessions_by_commit=note_sessions_by_commit,
        note_session_ids_by_commit=note_session_ids_by_commit,
        repository_context=repository_context,
        as_of=now,
    )
    if cutoff is None:
        bounds: dict[RepositoryKey, tuple[datetime, datetime]] = {}
        for push in current_pushes:
            key = key_of(push)
            if key is None:
                continue
            first, last = bounds.get(key, (push.captured_at, push.captured_at))
            bounds[key] = (
                min(first, push.captured_at),
                max(last, push.captured_at),
            )
        current = [
            replace(
                row,
                window_start=bounds[row_key(row)][0],
                window_end=bounds[row_key(row)][1],
            )
            for row in current
        ]
    else:
        current = [replace(row, window_start=cutoff, window_end=now) for row in current]
    if not current or not baseline_pushes:
        return current, check_attribution_share_alerts(current, [], policy)

    current_repos = {row_key(row) for row in current}
    baseline = derive_attribution_share(
        store,
        mirrors,
        org_id,
        replace(policy, window_days=policy.baseline_window_days),
        pushes=[push for push in baseline_pushes if key_of(push) in current_repos],
        candidate_limit=candidate_limit,
        note_sessions_by_commit=note_sessions_by_commit,
        note_session_ids_by_commit=note_session_ids_by_commit,
        repository_context=repository_context,
        as_of=now,
    )
    assert baseline_start is not None and cutoff is not None
    baseline = [
        replace(row, window_start=baseline_start, window_end=cutoff) for row in baseline
    ]
    return current, check_attribution_share_alerts(current, baseline, policy)


def generate_model_report(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    since_days: int | None = None,
    policy: AttributedCompletionPolicy | None = None,
    report_policy: OutcomeReportPolicy | None = None,
    fate_policy: FatePolicy | None = None,
    *,
    now: datetime | None = None,
    repository_context: RepositoryContext | None = None,
) -> list[ModelOutcomeReport]:
    """Read canonical Attribution and independent direct decision populations.

    ``policy`` is the attributed-completion-assembly policy
    (``AttributedCompletionPolicy``); ``report_policy`` is this report's own
    aggregation policy
    (``OutcomeReportPolicy``, default-constructed — i.e. ``CIGrain.COMMIT``
    — when not given). Two separate parameters, not one shared ``policy``,
    since they tune two different derivations that happen to compose in this
    one orchestration call.

    Inference calls are read without a store-side ``since`` filter on purpose:
    ``build_model_report`` applies ``since_days`` to inference calls and attributed completions
    together from the same full read, so the two populations are always
    windowed identically. Pre-filtering inference calls here (via
    ``FactStore.read_inference_calls(..., since=...)``) would desync the
    denominator from the attributed completion lookup, since a attributed completion assembled from a
    now-excluded inference call would have nothing to resolve its model against.
    """
    return generate_model_report_result(
        store,
        mirrors,
        org_id,
        since_days,
        policy,
        report_policy,
        fate_policy=fate_policy,
        now=now,
        repository_context=repository_context,
    ).rows


def generate_recovery_yield_report(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: RecoveryPolicy | None = None,
    *,
    now: datetime | None = None,
    repository_context: RepositoryContext | None = None,
) -> RecoveryYieldReport:
    """The whole recovery-yield report as one call: read CI outcomes, derive
    recovery pairs with skip reasons, and aggregate the operator-facing
    summary. ``sediment report recovery-yield`` wraps exactly this.
    """
    now = _report_now(now, repository_context)
    with store.read_snapshot() as snapshot:
        repository_context = repository_context or read_repository_context(
            snapshot, org_id, as_of=now
        )
        ci_outcomes = snapshot.read_ci_outcome_projections(org_id, captured_through=now)
        recovery = derive_recovery_result(
            snapshot,
            mirrors,
            org_id,
            policy,
            repository_context=repository_context,
            as_of=now,
        )
        return build_recovery_yield_report(ci_outcomes, recovery)


def generate_model_report_result(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    since_days: int | None = None,
    policy: AttributedCompletionPolicy | None = None,
    report_policy: OutcomeReportPolicy | None = None,
    include_trends: bool = False,
    fate_policy: FatePolicy | None = None,
    *,
    now: datetime | None = None,
    repository_context: RepositoryContext | None = None,
) -> ModelOutcomeReportResult:
    """Generate the model report plus repo-stratification checks in one pass."""
    now = _report_now(now, repository_context)
    with store.read_snapshot() as snapshot:
        repository_context = repository_context or read_repository_context(
            snapshot, org_id, as_of=now
        )
        ci_population = snapshot.read_ci_outcome_projections(
            org_id, captured_through=now, limit=50_000
        )
        completions = snapshot.read_report_inference_calls(org_id)
        assembly = assemble_attributed_completions_result(
            snapshot,
            mirrors,
            org_id,
            policy,
            ci_population=ci_population,
            completions=completions,
            repository_context=repository_context,
            as_of=now,
        )
        attribution_share, attribution_alerts = derive_model_report_attribution_share(
            snapshot,
            mirrors,
            org_id,
            since_days,
            now=now,
            repository_context=repository_context,
        )
        quarantine_revision = snapshot.quarantine_revision(org_id)
        fate_result = derive_fate_result(
            [
                observation
                for observation in snapshot.read_edit_observation_projections(org_id)
                if observation.captured_at <= now
            ],
            four_gram_containment,
            fate_policy,
            quarantine_revision=quarantine_revision,
        )
        result = build_model_report_result(
            completions,
            assembly.rows,
            since_days=since_days,
            now=now,
            decisions=snapshot.read_decision_projections(org_id),
            policy=report_policy,
            include_trends=include_trends,
            attribution_share=attribution_share,
            attribution_alerts=attribution_alerts,
            abandonment=build_abandonment_summary(assembly),
            quarantine_revision=quarantine_revision,
            fate_result=fate_result,
            repository_context=repository_context,
            ci_population=ci_population,
        )

        return with_model_assembly_diagnostics(result, assembly)

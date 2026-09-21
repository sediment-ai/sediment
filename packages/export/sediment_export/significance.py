# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two-proportion significance testing for model-vs-model comparison.

The first outcome report shipped descriptive counts only, with an explicit
"no significance testing" caveat — right for v1, but the renewal question the
report exists to answer ("did the fine-tune work?") eventually needs more
than two rates side by side. This module adds exactly that: a two-proportion
z-test over **CI pass rate** and **attribution rate**, the two rates in
``ModelOutcomeReport`` that are themselves proportions (a count over a
count), and therefore the only two a two-proportion test applies to. It also
reports the comparison's current minimum detectable effect (MDE): the
smallest true difference in proportions this sample size could detect at a
target alpha and power.

**Stdlib-only** — ``math`` and ``statistics.NormalDist`` (stdlib since
Python 3.8), no scipy/numpy. This rules out an exact (e.g. Fisher's) test as
the default path: a correct
two-tailed Fisher's exact test needs a poisson-binomial-style enumeration
over the hypergeometric distribution, not a one-line ``math.comb`` call, and
getting the two-tailed convention subtly wrong is an easy way to ship a
worse-than-honest number. So this module keeps the z-test at every n and
flags ``small_n`` (see below) rather than switching tests underfoot at an
arbitrary threshold.

**Pooled vs. unpooled standard error** — the two textbook choices disagree
because they answer different questions:

- The **z-statistic** (and its p-value) tests the null hypothesis "these two
  groups have the same true proportion." Under that null, pooling the two
  samples' successes into one shared proportion estimate is the standard,
  more powerful choice — that shared estimate is what "no difference" means.
- The **confidence interval** on the difference is not conditioned on that
  null; it is asking "how large could the true difference plausibly be,"
  which requires each group's own proportion, not a shared one. Pooling here
  would narrow the interval using an assumption (equal true proportions) the
  interval itself is supposed to be agnostic to. Unpooled SE is standard
  practice for the CI for exactly this reason.

So: pooled SE for ``z_stat``/``p_value``, unpooled SE for ``ci_low``/
``ci_high``. Both are documented at the call site below, not just here.
Non-inferiority testing reuses that same unpooled normal-approximation CI
machinery for ``p_b - p_a``; see ``non_inferiority_test`` for the one-sided
convention.

This module also includes a Bayesian A/B companion: each arm's unknown
rate is modeled as ``Beta(successes + prior_alpha, failures + prior_beta)``,
then ``P(rate_a > rate_b)`` and a credible interval for ``rate_a - rate_b``
are estimated by Monte Carlo sampling from those posteriors. The sampler uses
``random.Random(seed).betavariate(...)`` with a fixed explicit default seed,
never module-global random state, so identical inputs produce identical
diagnostic output. The default 100,000 paired draws bound the worst-case Monte
Carlo standard error for the improvement probability to about
``sqrt(0.25 / 100000) == 0.0016`` (0.16 percentage points), enough precision
for this operator-facing companion without adding scipy/numpy.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from itertools import combinations
from statistics import NormalDist
from typing import Literal

from .outcome_report import (
    ModelOutcomeReport,
    MannKendallTrendTest,
    TrendStatus,
    mann_kendall_trend_test,
)

_STANDARD_NORMAL = NormalDist()
DEFAULT_ALPHA = 0.05
COMPARE_TEST_FAMILY_SIZE = 2
DEFAULT_BAYESIAN_PRIOR_ALPHA = 1.0
DEFAULT_BAYESIAN_PRIOR_BETA = 1.0
DEFAULT_BAYESIAN_SEED = 179
DEFAULT_BAYESIAN_SAMPLES = 100_000
DEFAULT_CREDIBLE_LEVEL = 0.95
DEFAULT_BOOTSTRAP_ITERATIONS = 2_000
DEFAULT_BOOTSTRAP_SEED = 151
DEFAULT_MIN_CI_OVERLAP_FRACTION = 0.25

#: Below this many observations *per group*, the normal approximation
#: underlying a z-test (large-sample CLT) is shakier — the usual rule of
#: thumb wants n*p and n*(1-p) both comfortably above 5, and this is a coarse
#: stand-in for that check that does not require peeking at the proportion
#: itself. 30 per group is the conventional line; a result below it is not
#: thrown away, just flagged via ``small_n``.
SMALL_N_THRESHOLD = 30

#: Disjoint sequential blocks used to test for effect decay. Must be
#: independent (non-overlapping) samples for
#: Mann-Kendall's assumptions to hold -- unlike the displayed checkpoints,
#: which are cumulative prefixes and therefore share almost all their data
#: with each other. 10 balances trend-test power (fewer blocks lose the
#: ability to detect a real decay concentrated early in the series) against
#: each block still holding enough observations to be a meaningful sample.
DECAY_TREND_BLOCKS = 10

#: Placeholder numeric fields on an ``insufficient_data`` result. Chosen to
#: match ``build_model_report``'s own convention ("every rate is 0.0 on a
#: zero denominator — never a crash, never NaN") rather than inventing a
#: second silent-failure value for the same situation.
_INSUFFICIENT_PLACEHOLDER = 0.0


@dataclass(frozen=True)
class ProportionComparison:
    """One metric's two-proportion z-test between model A and model B.

    ``difference`` is ``proportion_a - proportion_b`` (positive means A's
    rate is higher). ``cohens_h`` is the standardized two-proportion effect
    size ``2*asin(sqrt(proportion_a)) - 2*asin(sqrt(proportion_b))``; Cohen's
    *Statistical Power Analysis for the Behavioral Sciences* (1988) gives
    conventional absolute-value benchmarks around 0.2/0.5/0.8 for
    small/medium/large effects. ``ci_low``/``ci_high`` bound the raw
    difference at the requested confidence level (95% default) using the
    **unpooled** SE; ``z_stat``/``p_value`` use the **pooled** SE (see module
    docstring for why the two differ).

    ``p_value`` is the raw two-tailed p-value. ``significant_at_alpha`` says
    whether it clears the nominal ``alpha`` threshold. ``bonferroni_alpha`` is
    ``alpha / comparison_family_size``; ``significant_after_bonferroni`` says
    whether the same raw p-value also clears that corrected threshold. A
    ``--compare`` call uses a family size of 2 because it tests both CI pass
    rate and attribution rate.

    ``benjamini_hochberg_*`` fields are populated only by
    ``compare_all_models``. The threshold is the final BH rejection cutoff for
    the whole tested family; ``significant_after_benjamini_hochberg`` says
    whether this metric is in that FDR-controlled rejection set.

    ``minimum_detectable_effect`` is the current sample's two-sided
    normal-approximation MDE at alpha=0.05 and power=0.80, using the pooled
    observed proportion as the baseline estimate. It is reported in raw
    proportion points (``0.05`` means five percentage points). When
    ``degenerate_se`` is set this field is the ``0.0`` placeholder, not a
    computed MDE — see ``degenerate_se`` below.

    ``sequential_boundary`` is populated only when the caller provides an
    information fraction (``0 < t <= 1``): it keeps the naive p-value and
    alpha calls above intact, then adds the O'Brien-Fleming peeking-corrected
    critical value and alpha spent for the same z-statistic, built from
    ``bonferroni_alpha`` rather than the nominal ``alpha`` so it never
    disagrees with ``significant_after_bonferroni`` on the same family.

    ``insufficient_data`` is ``True`` when either group has ``n == 0`` — no
    proportion, let alone a test or MDE, is computable, and every numeric
    field above is a ``0.0`` placeholder (never a crash, never a silently
    printed NaN). ``small_n`` is ``True`` when both groups have ``n > 0`` but
    at least one is below ``SMALL_N_THRESHOLD``: the fields are real,
    computed values — not placeholders — just less trustworthy than a
    large-sample z-test assumes.

    ``degenerate_se`` is ``True`` when both groups share a boundary outcome
    (every observation on both sides the same — all-pass/all-pass,
    all-fail/all-fail, or any other mix that pools to ``p_pool`` of exactly 0
    or 1), so the pooled standard error collapses to 0. The z-test's
    ``z_stat=0.0``/``p_value=1.0`` ("no evidence of a difference") is still
    the correct read under that null, but the normal-approximation power
    formula has no meaningful value at ``p*(1-p)=0``: the formula would emit
    ``MDE=0.0`` and ``required_n=0`` as degenerate arithmetic artifacts rather
    than honest power-planning answers. Under ``degenerate_se``,
    ``minimum_detectable_effect`` is the ``0.0`` placeholder (not a computed
    MDE) and callers should treat MDE/required-n as unavailable rather than
    print the degenerate values. ``insufficient_data`` and ``small_n`` are
    independent of this flag: a large all-pass/all-pass comparison has
    ``insufficient_data=False`` and ``small_n=False`` but
    ``degenerate_se=True``.
    """

    proportion_a: float
    proportion_b: float
    difference: float
    cohens_h: float
    ci_low: float
    ci_high: float
    z_stat: float
    p_value: float
    alpha: float
    bonferroni_alpha: float
    significant_at_alpha: bool
    significant_after_bonferroni: bool
    baseline_p: float
    minimum_detectable_effect: float
    sequential_boundary: SequentialBoundary | None
    n_a: int
    n_b: int
    insufficient_data: bool
    small_n: bool
    degenerate_se: bool
    benjamini_hochberg_rank: int | None = None
    benjamini_hochberg_alpha: float | None = None
    significant_after_benjamini_hochberg: bool | None = None


@dataclass(frozen=True)
class BayesianProportionComparison:
    """One metric's Beta-Binomial posterior comparison.

    ``posterior_mean_a``/``posterior_mean_b`` are the posterior means of each
    arm's rate under the supplied Beta prior. ``mean_difference`` is their
    difference. ``probability_a_gt_b`` is the posterior probability that A's
    true rate is higher than B's. ``credible_interval_low``/
    ``credible_interval_high`` are the central credible interval bounds for
    ``rate_a - rate_b`` from the same paired posterior draws.

    ``insufficient_data`` mirrors ``ProportionComparison``: if either
    denominator is zero, every numeric result field is a ``0.0`` placeholder
    rather than a fabricated posterior comparison.
    """

    posterior_mean_a: float
    posterior_mean_b: float
    mean_difference: float
    probability_a_gt_b: float
    credible_interval_low: float
    credible_interval_high: float
    credible_level: float
    prior_alpha: float
    prior_beta: float
    posterior_alpha_a: float
    posterior_beta_a: float
    posterior_alpha_b: float
    posterior_beta_b: float
    sample_count: int
    seed: int
    n_a: int
    n_b: int
    insufficient_data: bool
    small_n: bool


@dataclass(frozen=True)
class BootstrapIntervalDiagnostic:
    """Bootstrap diagnostic for one two-proportion comparison.

    ``analytic`` is the ordinary z-test result for the same raw counts.
    ``bootstrap_ci_low``/``bootstrap_ci_high`` are a fixed-seed nonparametric
    bootstrap percentile-method confidence interval over the same difference
    in proportions. The bootstrap resamples each group's empirical binary
    outcomes with replacement ``bootstrap_iterations`` times using a local
    ``random.Random(seed)`` instance, so repeated calls with the same inputs
    are deterministic and do not touch global RNG state.

    ``substantial_disagreement`` is a diagnostic flag, not a new statistical
    test. It is true when the bootstrap CI excludes the analytic point
    estimate, when the analytic and bootstrap CIs do not overlap, or when
    they overlap by less than ``min_ci_overlap_fraction`` of the narrower CI.
    ``disagreement_reasons`` is closed to those vocabulary strings.
    """

    analytic: ProportionComparison
    bootstrap_ci_low: float
    bootstrap_ci_high: float
    bootstrap_iterations: int
    seed: int
    confidence: float
    min_ci_overlap_fraction: float
    analytic_point_in_bootstrap_ci: bool
    ci_overlap_fraction: float
    substantial_disagreement: bool
    disagreement_reasons: tuple[str, ...]


@dataclass(frozen=True)
class SequentialBoundary:
    """O'Brien-Fleming peeking-corrected read for one z-statistic.

    ``critical_z`` is the two-sided absolute z boundary at the current
    ``information_fraction``. ``alpha_spent`` is the equivalent cumulative
    alpha threshold at that same look. ``alpha`` is the family-adjusted
    (Bonferroni) alpha the boundary was built from — the same value as
    ``ProportionComparison.bonferroni_alpha`` — not the nominal per-test
    alpha, so this boundary and ``significant_after_bonferroni`` never give
    contradictory reads for the same comparison family.
    ``significant_after_sequential_boundary`` says whether this result
    crosses the boundary; compare it to ``ProportionComparison.
    significant_after_bonferroni`` for the non-sequential family-adjusted
    read, or ``significant_at_alpha`` for the naive fixed-sample,
    single-test read.
    """

    information_fraction: float
    alpha: float
    alpha_spent: float
    critical_z: float
    significant_after_sequential_boundary: bool


@dataclass(frozen=True)
class ModelComparison:
    """The full ``--compare model_a model_b`` result: one
    ``ProportionComparison`` per metric the report can meaningfully test —
    CI pass rate and attribution rate, the two proportions
    ``ModelOutcomeReport`` carries. Acceptance (accepts/rejects) and mean
    similarity are intentionally not compared here: the former is not a rate
    over a fixed denominator in the same sense (an explicit decision is
    itself optional per completion) and the latter is not a proportion at
    all — a two-proportion test does not apply to either.
    """

    model_a: str
    model_b: str
    alpha: float
    bonferroni_alpha: float
    comparison_family_size: int
    information_fraction: float | None
    ci_pass_rate: ProportionComparison
    attribution_rate: ProportionComparison
    bayesian_ci_pass_rate: BayesianProportionComparison
    bayesian_attribution_rate: BayesianProportionComparison


@dataclass(frozen=True)
class PowerSimulationResult:
    """Fixed-seed Monte Carlo estimate of two-proportion z-test power.

    ``empirical_power`` is the fraction of ``simulations`` synthetic trials
    that reject at ``alpha``. It is a validation aid for the analytic MDE
    formula, not a derivation artifact: callers provide the seed explicitly,
    and the result is pure and reproducible for the same inputs.
    """

    n_a: int
    n_b: int
    baseline_p: float
    true_effect: float
    alpha: float
    simulations: int
    seed: int
    significant_trials: int
    empirical_power: float


@dataclass(frozen=True)
class MultiModelComparison:
    """All pairwise model comparisons with BH FDR control over one family.

    ``comparison_family_size`` is the intended family size: two metrics times
    every unordered model pair. ``tested_family_size`` is the count of
    computable p-values that entered the BH procedure; insufficient-data
    metrics are still reported on their pair, but they are not treated as fake
    zero p-values.
    """

    models: tuple[str, ...]
    alpha: float
    comparison_family_size: int
    tested_family_size: int
    comparisons: tuple[ModelComparison, ...]


_MetricName = Literal["ci_pass_rate", "attribution_rate"]


@dataclass(frozen=True)
class _RankedPValue:
    comparison_index: int
    metric: _MetricName
    p_value: float
    original_index: int


@dataclass(frozen=True)
class NonInferiorityResult:
    """One lower-margin non-inferiority test over two proportions.

    ``point_estimate`` is ``p_b - p_a``: positive means B's rate is higher than
    A's, negative means B is worse. ``ci_lower_bound`` is the lower bound used
    for the non-inferiority decision. ``margin`` is the caller's tolerated
    downside in raw proportion points (``0.05`` means five percentage points).

    ``is_non_inferior`` is ``True`` exactly when ``ci_lower_bound > -margin``.
    The comparison is strict, so the boundary case reads as inferior.

    ``insufficient_data`` mirrors ``ProportionComparison``: either arm with
    ``n <= 0`` returns explicit placeholders and never crashes. ``small_n`` is
    flagged, not rejected, because this is still the same large-sample normal
    approximation as the superiority comparison.
    """

    proportion_a: float
    proportion_b: float
    point_estimate: float
    ci_lower_bound: float
    margin: float
    alpha: float
    equivalent_two_sided_confidence: float
    is_non_inferior: bool
    n_a: int
    n_b: int
    insufficient_data: bool
    small_n: bool


@dataclass(frozen=True)
class EffectDecayCheckpoint:
    """One prefix checkpoint in an effect-decay diagnostic.

    ``fraction`` is the requested prefix fraction. ``n_a``/``n_b`` are the
    actual prefix sizes used for each arm; for non-empty lists this is
    ``ceil(len(outcomes) * fraction)`` clamped to at least one observation,
    so tiny samples may repeat the same prefix across adjacent checkpoints
    rather than crash or silently drop a requested row. ``comparison`` is the
    regular two-proportion result for that prefix, and ``cohens_h`` is copied
    from it so callers can render the effect-size series directly.
    """

    fraction: float
    n_a: int
    n_b: int
    successes_a: int
    successes_b: int
    proportion_a: float
    proportion_b: float
    cohens_h: float
    abs_cohens_h: float
    comparison: ProportionComparison


@dataclass(frozen=True)
class EffectDecayReport:
    """Effect-size trajectory across accumulating sample prefixes.

    ``checkpoints`` is purely descriptive: Cohen's h at the requested
    cumulative prefixes (25/50/75/100% by default), for display.
    ``decay_trend`` is the Mann-Kendall test that ``decay_detected`` is
    actually computed from — see ``effect_decay_check`` for why the two are
    not the same series.
    """

    checkpoints: list[EffectDecayCheckpoint]
    decay_trend: MannKendallTrendTest
    decay_detected: bool
    start_abs_effect: float
    end_abs_effect: float
    total_abs_drop: float
    insufficient_data: bool
    small_n: bool


@dataclass(frozen=True)
class RegretAlternative:
    """Expected regret of shipping the apparent-best model vs. one alternative.

    ``rate_difference`` is ``alternative_rate - apparent_best_rate``. Because
    the selected model is the observed best, this is normally non-positive;
    ``expected_regret`` is still positive when uncertainty leaves probability
    mass on the alternative actually being better.
    """

    model: str
    successes: int
    n: int
    observed_rate: float
    rate_difference: float
    standard_error: float
    expected_regret: float
    insufficient_data: bool
    small_n: bool


@dataclass(frozen=True)
class RegretReport:
    """Expected regret for choosing the apparent-best observed model.

    ``expected_regret`` values are raw proportion points: ``0.01`` means one
    percentage point of expected loss if the named alternative is actually the
    better arm.
    """

    metric_name: str
    apparent_best_model: str | None
    apparent_best_successes: int
    apparent_best_n: int
    apparent_best_rate: float
    alternatives: tuple[RegretAlternative, ...]
    insufficient_data: bool


def cohens_h(proportion_a: float, proportion_b: float) -> float:
    """Cohen's h standardized effect size for two proportions."""
    if not 0 <= proportion_a <= 1:
        raise ValueError("proportion_a must be between 0 and 1")
    if not 0 <= proportion_b <= 1:
        raise ValueError("proportion_b must be between 0 and 1")
    return 2 * math.asin(math.sqrt(proportion_a)) - 2 * math.asin(
        math.sqrt(proportion_b)
    )


def obrien_fleming_alpha_spent(
    information_fraction: float, alpha: float = DEFAULT_ALPHA
) -> float:
    """Lan-DeMets O'Brien-Fleming cumulative alpha spent at information time t.

    ``alpha_spent(t) = 2 - 2 * Phi(z_(1-alpha/2) / sqrt(t))`` for
    ``0 < t <= 1``. The function is two-sided: with ``alpha=0.05`` and
    ``t=1.0``, it returns ``0.05`` up to floating-point precision.
    """
    _validate_information_fraction(information_fraction)
    _validate_alpha(alpha)
    return 2 - 2 * _STANDARD_NORMAL.cdf(
        _STANDARD_NORMAL.inv_cdf(1 - alpha / 2) / math.sqrt(information_fraction)
    )


def obrien_fleming_boundary(
    information_fraction: float, alpha: float = DEFAULT_ALPHA
) -> float:
    """Two-sided absolute z boundary for an O'Brien-Fleming peek correction.

    At the final look (``information_fraction == 1``), this reduces to the
    ordinary fixed-sample critical value ``NormalDist().inv_cdf(1-alpha/2)``.
    Earlier looks require a larger ``|z|``.
    """
    _validate_information_fraction(information_fraction)
    _validate_alpha(alpha)
    return _STANDARD_NORMAL.inv_cdf(1 - alpha / 2) / math.sqrt(information_fraction)


def _disjoint_blocks(outcomes: list[bool], blocks: int) -> list[list[bool]]:
    n = len(outcomes)
    return [outcomes[i * n // blocks : (i + 1) * n // blocks] for i in range(blocks)]


def _decay_trend_test(
    outcomes_a: list[bool],
    outcomes_b: list[bool],
    *,
    blocks: int = DECAY_TREND_BLOCKS,
    alpha: float = DEFAULT_ALPHA,
) -> MannKendallTrendTest:
    """Mann-Kendall trend test over Cohen's h across disjoint blocks.

    These blocks partition each arm's arrival-ordered outcomes into
    ``blocks`` non-overlapping segments — the independent samples
    ``mann_kendall_trend_test`` assumes, unlike the displayed cumulative
    checkpoints. A block with zero observations on either side (small
    inputs, many blocks) is skipped rather than fabricating a rate.
    """
    block_effects = [
        abs(cohens_h(sum(block_a) / len(block_a), sum(block_b) / len(block_b)))
        for block_a, block_b in zip(
            _disjoint_blocks(outcomes_a, blocks), _disjoint_blocks(outcomes_b, blocks)
        )
        if block_a and block_b
    ]
    return mann_kendall_trend_test(block_effects, alpha=alpha)


def effect_decay_check(
    outcomes_a: list[bool],
    outcomes_b: list[bool],
    checkpoint_fractions: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0),
    *,
    trend_blocks: int = DECAY_TREND_BLOCKS,
    alpha: float = DEFAULT_ALPHA,
) -> EffectDecayReport:
    """Check whether an observed two-proportion effect shrinks as data arrives.

    The input lists are consumed in the order supplied by the caller. No
    shuffling or resampling is performed. ``checkpoints`` displays Cohen's h
    at cumulative prefixes (``checkpoint_fractions``, e.g. 25/50/75/100%) for
    readability: checkpoint ``0.25`` means the first quarter of each arm's
    observed outcomes, checkpoint ``1.0`` means all of them. Prefix sizes use
    ``ceil(len(outcomes) * fraction)`` and clamp non-empty arms to at least
    one observation, so fewer observations than checkpoint rows is valid and
    may produce repeated prefix sizes.

    ``decay_detected`` is **not** computed from that checkpoint series —
    adjacent checkpoints are nested prefixes sharing almost all their data,
    so any monotonicity/threshold test run directly on them (fixed-tolerance
    or Mann-Kendall alike) inherits the early checkpoints' large sampling
    noise and false-positives on a plainly constant real effect (simulation
    over a constant effect flagged 26-30% of trials as decaying). Instead,
    ``decay_trend`` runs Mann-Kendall over Cohen's h computed on
    ``trend_blocks`` disjoint sequential blocks of the same outcomes — see
    ``_decay_trend_test``. ``decay_detected`` is true only when
    ``decay_trend.status == TrendStatus.SIGNIFICANT_DECREASE`` and the final
    checkpoint has enough data to avoid the existing small-n warning; tiny
    samples still get checkpoint rows but do not raise the decay flag.
    """
    _validate_checkpoint_fractions(checkpoint_fractions)

    checkpoints: list[EffectDecayCheckpoint] = []
    for fraction in checkpoint_fractions:
        n_a = _checkpoint_n(len(outcomes_a), fraction)
        n_b = _checkpoint_n(len(outcomes_b), fraction)
        successes_a = sum(outcomes_a[:n_a])
        successes_b = sum(outcomes_b[:n_b])
        comparison = two_proportion_z_test(successes_a, n_a, successes_b, n_b)
        checkpoints.append(
            EffectDecayCheckpoint(
                fraction=fraction,
                n_a=n_a,
                n_b=n_b,
                successes_a=successes_a,
                successes_b=successes_b,
                proportion_a=comparison.proportion_a,
                proportion_b=comparison.proportion_b,
                cohens_h=comparison.cohens_h,
                abs_cohens_h=abs(comparison.cohens_h),
                comparison=comparison,
            )
        )

    abs_effects = [checkpoint.abs_cohens_h for checkpoint in checkpoints]
    start_abs_effect = abs_effects[0]
    end_abs_effect = abs_effects[-1]
    total_abs_drop = start_abs_effect - end_abs_effect
    decay_trend = _decay_trend_test(
        outcomes_a, outcomes_b, blocks=trend_blocks, alpha=alpha
    )
    insufficient_data = any(
        checkpoint.comparison.insufficient_data for checkpoint in checkpoints
    )
    small_n = any(checkpoint.comparison.small_n for checkpoint in checkpoints)
    final = checkpoints[-1].comparison
    final_sample_large_enough = (
        not final.insufficient_data
        and final.n_a >= SMALL_N_THRESHOLD
        and final.n_b >= SMALL_N_THRESHOLD
    )
    decay_detected = (
        decay_trend.status == TrendStatus.SIGNIFICANT_DECREASE
        and final_sample_large_enough
    )

    return EffectDecayReport(
        checkpoints=checkpoints,
        decay_trend=decay_trend,
        decay_detected=decay_detected,
        start_abs_effect=start_abs_effect,
        end_abs_effect=end_abs_effect,
        total_abs_drop=total_abs_drop,
        insufficient_data=insufficient_data,
        small_n=small_n,
    )


def _checkpoint_n(outcome_count: int, fraction: float) -> int:
    if outcome_count == 0:
        return 0
    return min(outcome_count, max(1, math.ceil(outcome_count * fraction)))


def _validate_checkpoint_fractions(fractions: tuple[float, ...]) -> None:
    if not fractions:
        raise ValueError("checkpoint_fractions must not be empty")
    previous = 0.0
    for fraction in fractions:
        if not 0 < fraction <= 1:
            raise ValueError("checkpoint_fractions must be in (0, 1]")
        if fraction <= previous:
            raise ValueError("checkpoint_fractions must be strictly increasing")
        previous = fraction


def two_proportion_z_test(
    successes_a: int,
    n_a: int,
    successes_b: int,
    n_b: int,
    *,
    confidence: float = 0.95,
    alpha: float = DEFAULT_ALPHA,
    comparison_family_size: int = 1,
    small_n_threshold: int = SMALL_N_THRESHOLD,
    information_fraction: float | None = None,
) -> ProportionComparison:
    """The two-proportion z-test itself, over raw counts.

    Formulas (``p_a = successes_a / n_a``, ``p_b`` likewise):

    - pooled proportion: ``p_pool = (successes_a + successes_b) / (n_a + n_b)``
    - pooled SE (for the z-stat): ``sqrt(p_pool * (1 - p_pool) * (1/n_a + 1/n_b))``
    - z-statistic: ``(p_a - p_b) / se_pooled``
    - two-tailed p-value: ``2 * (1 - Phi(|z|))`` via
      ``statistics.NormalDist().cdf``
    - unpooled SE (for the CI): ``sqrt(p_a*(1-p_a)/n_a + p_b*(1-p_b)/n_b)``
    - ``100*confidence%`` CI on the difference:
      ``(p_a - p_b) ± z_crit * se_unpooled``, where ``z_crit`` is the
      two-tailed critical value ``NormalDist().inv_cdf(1 - (1-confidence)/2)``
      (``≈1.95996`` at the 95% default)

    ``alpha`` is the nominal threshold for ``significant_at_alpha``.
    ``comparison_family_size`` controls the Bonferroni-adjusted threshold
    (``alpha / comparison_family_size``) used for
    ``significant_after_bonferroni``; standalone calls default to 1, while
    ``compare_models`` passes 2 for the CI-pass/attribution-rate test family.

    ``information_fraction`` optionally adds an O'Brien-Fleming sequential
    boundary read for repeated peeking. It does not replace or hide the naive
    fixed-sample p-value; it populates ``sequential_boundary`` with the
    stricter critical value, alpha spent, and corrected significance call.
    The boundary is built from ``bonferroni_alpha`` (the family-adjusted
    alpha), not the nominal ``alpha`` — so a comparison run through
    ``compare_models`` (family size 2) never prints a sequential-boundary
    verdict that contradicts its own ``significant_after_bonferroni`` call at
    ``information_fraction=1.0``. Standalone calls default
    ``comparison_family_size=1``, where the two alphas coincide.

    ``n_a == 0`` or ``n_b == 0`` returns ``insufficient_data=True`` with
    every numeric field a ``0.0`` placeholder — never a ``ZeroDivisionError``,
    never a silently printed NaN.

    A **degenerate pooled SE of exactly 0** (every observation on both sides
    the same outcome — all-pass/all-pass, all-fail/all-fail, or any mix that
    happens to pool to ``p_pool`` of 0 or 1) forces ``p_a == p_b`` too, so
    ``z_stat=0.0``/``p_value=1.0`` ("no evidence of a difference") is the
    correct answer, not a division by zero — handled explicitly rather than
    left to float division to raise. The same ``p*(1-p)=0`` collapse also
    makes the MDE formula degenerate, so that condition is surfaced as
    ``degenerate_se=True`` and ``minimum_detectable_effect`` carries the
    ``0.0`` placeholder under it (see ``ProportionComparison.degenerate_se``).
    """
    _validate_alpha(alpha)
    if comparison_family_size <= 0:
        raise ValueError("comparison_family_size must be positive")
    if information_fraction is not None:
        _validate_information_fraction(information_fraction)
    bonferroni_alpha = alpha / comparison_family_size

    if n_a <= 0 or n_b <= 0:
        return ProportionComparison(
            proportion_a=_INSUFFICIENT_PLACEHOLDER,
            proportion_b=_INSUFFICIENT_PLACEHOLDER,
            difference=_INSUFFICIENT_PLACEHOLDER,
            cohens_h=_INSUFFICIENT_PLACEHOLDER,
            ci_low=_INSUFFICIENT_PLACEHOLDER,
            ci_high=_INSUFFICIENT_PLACEHOLDER,
            z_stat=_INSUFFICIENT_PLACEHOLDER,
            p_value=_INSUFFICIENT_PLACEHOLDER,
            alpha=alpha,
            bonferroni_alpha=bonferroni_alpha,
            significant_at_alpha=False,
            significant_after_bonferroni=False,
            baseline_p=_INSUFFICIENT_PLACEHOLDER,
            minimum_detectable_effect=_INSUFFICIENT_PLACEHOLDER,
            sequential_boundary=None,
            n_a=n_a,
            n_b=n_b,
            insufficient_data=True,
            small_n=False,
            degenerate_se=False,
        )

    p_a = successes_a / n_a
    p_b = successes_b / n_b
    difference = p_a - p_b
    effect_size = cohens_h(p_a, p_b)

    pooled_p = (successes_a + successes_b) / (n_a + n_b)
    se_pooled = math.sqrt(pooled_p * (1 - pooled_p) * (1 / n_a + 1 / n_b))
    # p_pool in {0, 1} collapses both the z-test SE and the MDE variance term
    # p*(1-p) to 0. The z-test's z_stat=0.0/p_value=1.0 is the correct "no
    # difference" read, but the normal-approximation power formula is
    # undefined at p*(1-p)=0: it would emit MDE=0.0 and required-n=0 as
    # degenerate arithmetic, not honest power-planning answers. Flag the
    # condition and carry the MDE placeholder so callers treat MDE as
    # unavailable; keep the exact formula for every interior pooled_p.
    degenerate_se = se_pooled == 0
    if degenerate_se:
        z_stat = 0.0
        p_value = 1.0
        detectable_effect = _INSUFFICIENT_PLACEHOLDER
    else:
        z_stat = difference / se_pooled
        p_value = 2 * (1 - _STANDARD_NORMAL.cdf(abs(z_stat)))
        detectable_effect = minimum_detectable_effect(n_a, n_b, pooled_p)

    ci_low, ci_high = _normal_approximation_difference_ci(
        p_a,
        n_a,
        p_b,
        n_b,
        difference,
        confidence,
    )
    significant_at_alpha = p_value < alpha
    sequential_boundary = (
        _sequential_boundary(z_stat, information_fraction, bonferroni_alpha)
        if information_fraction is not None
        else None
    )

    return ProportionComparison(
        proportion_a=p_a,
        proportion_b=p_b,
        difference=difference,
        cohens_h=effect_size,
        ci_low=ci_low,
        ci_high=ci_high,
        z_stat=z_stat,
        p_value=p_value,
        alpha=alpha,
        bonferroni_alpha=bonferroni_alpha,
        significant_at_alpha=significant_at_alpha,
        significant_after_bonferroni=p_value < bonferroni_alpha,
        baseline_p=pooled_p,
        minimum_detectable_effect=detectable_effect,
        sequential_boundary=sequential_boundary,
        n_a=n_a,
        n_b=n_b,
        insufficient_data=False,
        small_n=(n_a < small_n_threshold or n_b < small_n_threshold),
        degenerate_se=degenerate_se,
    )


def beta_binomial_probability_of_improvement(
    successes_a: int,
    n_a: int,
    successes_b: int,
    n_b: int,
    *,
    prior_alpha: float = DEFAULT_BAYESIAN_PRIOR_ALPHA,
    prior_beta: float = DEFAULT_BAYESIAN_PRIOR_BETA,
    sample_count: int = DEFAULT_BAYESIAN_SAMPLES,
    seed: int = DEFAULT_BAYESIAN_SEED,
    credible_level: float = DEFAULT_CREDIBLE_LEVEL,
    small_n_threshold: int = SMALL_N_THRESHOLD,
) -> BayesianProportionComparison:
    """Beta-Binomial posterior probability that A's rate exceeds B's.

    Each arm uses the conjugate posterior
    ``Beta(successes + prior_alpha, failures + prior_beta)``. The posterior
    probability and central credible interval are estimated from
    ``sample_count`` paired draws using a local, explicitly seeded
    ``random.Random`` instance, which makes this diagnostic deterministic for
    repeated calls with the same inputs.
    """
    _validate_beta_binomial_inputs(
        successes_a,
        n_a,
        successes_b,
        n_b,
        prior_alpha,
        prior_beta,
        sample_count,
        credible_level,
    )

    if n_a <= 0 or n_b <= 0:
        return BayesianProportionComparison(
            posterior_mean_a=_INSUFFICIENT_PLACEHOLDER,
            posterior_mean_b=_INSUFFICIENT_PLACEHOLDER,
            mean_difference=_INSUFFICIENT_PLACEHOLDER,
            probability_a_gt_b=_INSUFFICIENT_PLACEHOLDER,
            credible_interval_low=_INSUFFICIENT_PLACEHOLDER,
            credible_interval_high=_INSUFFICIENT_PLACEHOLDER,
            credible_level=credible_level,
            prior_alpha=prior_alpha,
            prior_beta=prior_beta,
            posterior_alpha_a=_INSUFFICIENT_PLACEHOLDER,
            posterior_beta_a=_INSUFFICIENT_PLACEHOLDER,
            posterior_alpha_b=_INSUFFICIENT_PLACEHOLDER,
            posterior_beta_b=_INSUFFICIENT_PLACEHOLDER,
            sample_count=sample_count,
            seed=seed,
            n_a=n_a,
            n_b=n_b,
            insufficient_data=True,
            small_n=False,
        )

    failures_a = n_a - successes_a
    failures_b = n_b - successes_b
    posterior_alpha_a = successes_a + prior_alpha
    posterior_beta_a = failures_a + prior_beta
    posterior_alpha_b = successes_b + prior_alpha
    posterior_beta_b = failures_b + prior_beta
    posterior_mean_a = posterior_alpha_a / (posterior_alpha_a + posterior_beta_a)
    posterior_mean_b = posterior_alpha_b / (posterior_alpha_b + posterior_beta_b)

    rng = random.Random(seed)
    better_count = 0
    differences: list[float] = []
    for _ in range(sample_count):
        rate_a = rng.betavariate(posterior_alpha_a, posterior_beta_a)
        rate_b = rng.betavariate(posterior_alpha_b, posterior_beta_b)
        if rate_a > rate_b:
            better_count += 1
        differences.append(rate_a - rate_b)

    differences.sort()
    tail = (1 - credible_level) / 2
    return BayesianProportionComparison(
        posterior_mean_a=posterior_mean_a,
        posterior_mean_b=posterior_mean_b,
        mean_difference=posterior_mean_a - posterior_mean_b,
        probability_a_gt_b=better_count / sample_count,
        credible_interval_low=_percentile(differences, tail),
        credible_interval_high=_percentile(differences, 1 - tail),
        credible_level=credible_level,
        prior_alpha=prior_alpha,
        prior_beta=prior_beta,
        posterior_alpha_a=posterior_alpha_a,
        posterior_beta_a=posterior_beta_a,
        posterior_alpha_b=posterior_alpha_b,
        posterior_beta_b=posterior_beta_b,
        sample_count=sample_count,
        seed=seed,
        n_a=n_a,
        n_b=n_b,
        insufficient_data=False,
        small_n=(n_a < small_n_threshold or n_b < small_n_threshold),
    )


def non_inferiority_test(
    successes_a: int,
    n_a: int,
    successes_b: int,
    n_b: int,
    margin: float,
    alpha: float = DEFAULT_ALPHA,
    small_n_threshold: int = SMALL_N_THRESHOLD,
) -> NonInferiorityResult:
    """Test whether B is non-inferior to A for a binary success rate.

    This implements the lower-margin one-sided test from the TOST framing:

    - estimate ``delta = p_b - p_a`` (B minus A; A is the baseline/control)
    - null hypothesis: ``delta <= -margin`` (B is worse than A by at least the
      tolerated margin)
    - alternative: ``delta > -margin`` (B is not meaningfully worse)
    - decision rule at one-sided ``alpha``: compute the lower confidence bound
      with ``z_(1-alpha)`` and declare non-inferiority iff
      ``lower_bound > -margin``

    Equivalently, for ``alpha=0.05`` this is the lower bound of a two-sided
    90% normal-approximation CI, not a 95% two-sided CI. That is the common
    off-by-factor-of-two trap in non-inferiority literature: TOST spends
    ``alpha`` on each one-sided component, so the matching two-sided interval
    has confidence ``1 - 2*alpha``. Non-inferiority only needs the lower-margin
    component because B being much better than A is not a failure.

    The CI itself reuses the same unpooled normal-approximation difference
    formula used by ``two_proportion_z_test``'s reported CI; this module does
    not introduce a third interval formula.
    """
    _validate_non_inferiority_inputs(margin, alpha)
    equivalent_two_sided_confidence = 1 - 2 * alpha

    if n_a <= 0 or n_b <= 0:
        return NonInferiorityResult(
            proportion_a=_INSUFFICIENT_PLACEHOLDER,
            proportion_b=_INSUFFICIENT_PLACEHOLDER,
            point_estimate=_INSUFFICIENT_PLACEHOLDER,
            ci_lower_bound=_INSUFFICIENT_PLACEHOLDER,
            margin=margin,
            alpha=alpha,
            equivalent_two_sided_confidence=equivalent_two_sided_confidence,
            is_non_inferior=False,
            n_a=n_a,
            n_b=n_b,
            insufficient_data=True,
            small_n=False,
        )

    p_a = successes_a / n_a
    p_b = successes_b / n_b
    point_estimate = p_b - p_a
    ci_lower_bound, _ = _normal_approximation_difference_ci(
        p_b,
        n_b,
        p_a,
        n_a,
        point_estimate,
        equivalent_two_sided_confidence,
    )

    return NonInferiorityResult(
        proportion_a=p_a,
        proportion_b=p_b,
        point_estimate=point_estimate,
        ci_lower_bound=ci_lower_bound,
        margin=margin,
        alpha=alpha,
        equivalent_two_sided_confidence=equivalent_two_sided_confidence,
        is_non_inferior=ci_lower_bound > -margin,
        n_a=n_a,
        n_b=n_b,
        insufficient_data=False,
        small_n=(n_a < small_n_threshold or n_b < small_n_threshold),
    )


def bootstrap_two_proportion_interval_diagnostic(
    successes_a: int,
    n_a: int,
    successes_b: int,
    n_b: int,
    *,
    confidence: float = 0.95,
    alpha: float = DEFAULT_ALPHA,
    comparison_family_size: int = 1,
    small_n_threshold: int = SMALL_N_THRESHOLD,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    min_ci_overlap_fraction: float = DEFAULT_MIN_CI_OVERLAP_FRACTION,
) -> BootstrapIntervalDiagnostic:
    """Diagnose analytic-interval behavior with a fixed-seed bootstrap interval.

    This is a diagnostic/validation helper for the two-proportion z-test, not
    a persisted derivation. Given the same raw counts as
    ``two_proportion_z_test``, it builds the analytic result, then resamples
    each group's empirical binary outcomes with replacement ``B`` times and
    returns the percentile-method bootstrap CI for ``p_a - p_b`` alongside
    disagreement flags.
    """
    _validate_confidence(confidence)
    if bootstrap_iterations < 2:
        raise ValueError("bootstrap_iterations must be at least 2")
    if not 0 <= min_ci_overlap_fraction <= 1:
        raise ValueError("min_ci_overlap_fraction must be between 0 and 1")

    analytic = two_proportion_z_test(
        successes_a,
        n_a,
        successes_b,
        n_b,
        confidence=confidence,
        alpha=alpha,
        comparison_family_size=comparison_family_size,
        small_n_threshold=small_n_threshold,
    )
    if analytic.insufficient_data:
        return BootstrapIntervalDiagnostic(
            analytic=analytic,
            bootstrap_ci_low=_INSUFFICIENT_PLACEHOLDER,
            bootstrap_ci_high=_INSUFFICIENT_PLACEHOLDER,
            bootstrap_iterations=bootstrap_iterations,
            seed=seed,
            confidence=confidence,
            min_ci_overlap_fraction=min_ci_overlap_fraction,
            analytic_point_in_bootstrap_ci=False,
            ci_overlap_fraction=0.0,
            substantial_disagreement=False,
            disagreement_reasons=(),
        )

    rng = random.Random(seed)
    p_a = successes_a / n_a
    p_b = successes_b / n_b
    differences = sorted(
        _resampled_proportion(rng, n_a, p_a) - _resampled_proportion(rng, n_b, p_b)
        for _ in range(bootstrap_iterations)
    )
    tail = (1 - confidence) / 2
    bootstrap_ci_low = _percentile(differences, tail)
    bootstrap_ci_high = _percentile(differences, 1 - tail)

    point_in_bootstrap = bootstrap_ci_low <= analytic.difference <= bootstrap_ci_high
    overlap_fraction = _ci_overlap_fraction(
        analytic.ci_low,
        analytic.ci_high,
        bootstrap_ci_low,
        bootstrap_ci_high,
    )
    reasons: list[str] = []
    if not point_in_bootstrap:
        reasons.append("bootstrap_ci_excludes_analytic_difference")
    if overlap_fraction == 0.0:
        reasons.append("confidence_intervals_do_not_overlap")
    elif overlap_fraction < min_ci_overlap_fraction:
        reasons.append("confidence_intervals_barely_overlap")

    return BootstrapIntervalDiagnostic(
        analytic=analytic,
        bootstrap_ci_low=bootstrap_ci_low,
        bootstrap_ci_high=bootstrap_ci_high,
        bootstrap_iterations=bootstrap_iterations,
        seed=seed,
        confidence=confidence,
        min_ci_overlap_fraction=min_ci_overlap_fraction,
        analytic_point_in_bootstrap_ci=point_in_bootstrap,
        ci_overlap_fraction=overlap_fraction,
        substantial_disagreement=bool(reasons),
        disagreement_reasons=tuple(reasons),
    )


def _resampled_proportion(rng: random.Random, n: int, empirical_p: float) -> float:
    return _draw_binomial(rng, n, empirical_p) / n


def _percentile(sorted_values: list[float], quantile: float) -> float:
    if not sorted_values:
        raise ValueError("sorted_values must not be empty")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")

    position = quantile * (len(sorted_values) - 1)
    lower_idx = math.floor(position)
    upper_idx = math.ceil(position)
    if lower_idx == upper_idx:
        return sorted_values[lower_idx]
    fraction = position - lower_idx
    return (
        sorted_values[lower_idx] * (1 - fraction) + sorted_values[upper_idx] * fraction
    )


def _validate_beta_binomial_inputs(
    successes_a: int,
    n_a: int,
    successes_b: int,
    n_b: int,
    prior_alpha: float,
    prior_beta: float,
    sample_count: int,
    credible_level: float,
) -> None:
    _validate_success_counts(successes_a, n_a, "a")
    _validate_success_counts(successes_b, n_b, "b")
    if prior_alpha <= 0:
        raise ValueError("prior_alpha must be positive")
    if prior_beta <= 0:
        raise ValueError("prior_beta must be positive")
    if sample_count <= 0:
        raise ValueError("sample_count must be positive")
    if not 0 < credible_level < 1:
        raise ValueError("credible_level must be between 0 and 1")


def _validate_success_counts(successes: int, n: int, label: str) -> None:
    if n < 0:
        raise ValueError(f"n_{label} must be non-negative")
    if successes < 0:
        raise ValueError(f"successes_{label} must be non-negative")
    if successes > n:
        raise ValueError(f"successes_{label} must be <= n_{label}")


def _ci_overlap_fraction(
    ci_a_low: float,
    ci_a_high: float,
    ci_b_low: float,
    ci_b_high: float,
) -> float:
    overlap_width = max(0.0, min(ci_a_high, ci_b_high) - max(ci_a_low, ci_b_low))
    ci_a_width = ci_a_high - ci_a_low
    ci_b_width = ci_b_high - ci_b_low
    narrower_width = min(ci_a_width, ci_b_width)
    if narrower_width > 0:
        return overlap_width / narrower_width
    return 1.0 if ci_a_low <= ci_b_high and ci_b_low <= ci_a_high else 0.0


def minimum_detectable_effect(
    n_a: int,
    n_b: int,
    baseline_p: float,
    alpha: float = 0.05,
    power: float = 0.80,
) -> float:
    """Approximate two-sided MDE for comparing two independent proportions.

    Uses the standard normal-approximation power formula for two independent
    proportions: ``d = (z_(1-alpha/2) + z_power) *
    sqrt(p * (1-p) * (1/n_a + 1/n_b))``. This is the same rearranged
    sample-size relationship documented for binary-outcome superiority trials
    and field trials comparing proportions; see NCBI Bookshelf, *Practical
    help for specifying the target difference in sample size calculations for
    RCTs: the DELTA2 five-stage study* (Equation 1,
    https://www.ncbi.nlm.nih.gov/books/NBK549166/), and *Field Trials of
    Health Interventions*, section 5.4.1
    (https://www.ncbi.nlm.nih.gov/books/NBK305517/).

    ``baseline_p`` is the observed pooled/baseline proportion estimate. The
    result is raw proportion points: ``0.05`` means a five percentage-point
    difference. ``n_a <= 0`` or ``n_b <= 0`` mirrors
    ``two_proportion_z_test``'s insufficient-data convention and returns the
    same ``0.0`` placeholder.

    At ``baseline_p in {0, 1}`` the variance term ``p*(1-p)`` collapses to 0,
    so the formula's output is the degenerate ``0.0``, not a real "smallest
    detectable difference." This function keeps that exact interior formula
    (it is harmless at the boundary — the output is already the ``0.0``
    placeholder); the boundary is surfaced as ``ProportionComparison.
    degenerate_se`` by ``two_proportion_z_test``, which carries the ``0.0``
    MDE placeholder under that flag so callers treat MDE as unavailable rather
    than print the degenerate value. The inverse
    :func:`required_n_per_arm_for_mde` guards the same boundary explicitly
    and returns ``None``.
    """
    if n_a <= 0 or n_b <= 0:
        return _INSUFFICIENT_PLACEHOLDER
    _validate_power_inputs(baseline_p, alpha, power)

    z_alpha = _STANDARD_NORMAL.inv_cdf(1 - alpha / 2)
    z_beta = _STANDARD_NORMAL.inv_cdf(power)
    variance = baseline_p * (1 - baseline_p) * (1 / n_a + 1 / n_b)
    return (z_alpha + z_beta) * math.sqrt(variance)


def required_n_per_arm_for_mde(
    target_mde: float,
    baseline_p: float,
    alpha: float = 0.05,
    power: float = 0.80,
) -> int | None:
    """Equal-allocation sample size per arm needed to detect ``target_mde``.

    Solves ``minimum_detectable_effect``'s two-proportion normal-approximation
    formula for equal group sizes (``n_a == n_b``):
    ``n = ceil(2 * p * (1-p) * (z_(1-alpha/2) + z_power)^2 / target_mde^2)``.

    Returns ``None`` at ``baseline_p in {0, 1}``: the same ``p*(1-p)=0``
    collapse that makes the forward MDE degenerate forces the inverse formula
    to ``ceil(0 / target_mde^2) == 0`` — "plan for n=0 per arm" — which is an
    arithmetic artifact, not an honest sample size. ``None`` lets callers
    omit the planning line rather than recommend a degenerate n.
    """
    if target_mde <= 0:
        raise ValueError("target_mde must be positive")
    _validate_power_inputs(baseline_p, alpha, power)
    if baseline_p <= 0.0 or baseline_p >= 1.0:
        return None

    z_alpha = _STANDARD_NORMAL.inv_cdf(1 - alpha / 2)
    z_beta = _STANDARD_NORMAL.inv_cdf(power)
    n = 2 * baseline_p * (1 - baseline_p) * (z_alpha + z_beta) ** 2
    return math.ceil(n / target_mde**2)


def required_days_for_mde(
    target_mde: float,
    baseline_p: float,
    completions_per_day_a: float,
    completions_per_day_b: float,
    alpha: float = 0.05,
    power: float = 0.80,
) -> float | None:
    """Calendar days needed to reach the target MDE at per-arm traffic rates.

    The underlying sample-size calculation is the same equal-allocation
    ``required_n_per_arm_for_mde`` formula, so this function takes observed or
    planned completions/day separately for arm A and arm B. That keeps
    weighted LiteLLM splits explicit: if model A sees 20 completions/day and
    model B sees 80 completions/day, the duration is governed by the slower
    arm's time to reach the required per-arm ``n``.

    Returns ``None`` when the planning line should be omitted rather than
    print an infinite or fabricated duration: non-positive traffic in either
    arm, or ``baseline_p in {0, 1}`` (where ``required_n_per_arm_for_mde``
    is itself undefined and returns ``None``).
    """
    if completions_per_day_a <= 0 or completions_per_day_b <= 0:
        return None

    required_n = required_n_per_arm_for_mde(
        target_mde,
        baseline_p,
        alpha=alpha,
        power=power,
    )
    if required_n is None:
        return None
    return max(required_n / completions_per_day_a, required_n / completions_per_day_b)


def simulate_two_proportion_power(
    n_a: int,
    n_b: int,
    baseline_p: float,
    true_effect: float,
    *,
    simulations: int = 2000,
    seed: int,
    alpha: float = DEFAULT_ALPHA,
) -> PowerSimulationResult:
    """Estimate z-test power by deterministic Monte Carlo simulation.

    Each synthetic trial draws arm B from ``baseline_p`` and arm A from
    ``baseline_p + true_effect``, then runs ``two_proportion_z_test`` over the
    simulated success counts. The same inputs and seed produce the same
    ``PowerSimulationResult`` exactly.
    """
    if n_a <= 0 or n_b <= 0:
        raise ValueError("n_a and n_b must be positive")
    if not 0 <= baseline_p <= 1:
        raise ValueError("baseline_p must be between 0 and 1")
    _validate_alpha(alpha)
    true_p_a = baseline_p + true_effect
    if not 0 <= true_p_a <= 1:
        raise ValueError("baseline_p + true_effect must be between 0 and 1")
    if simulations <= 0:
        raise ValueError("simulations must be positive")

    rng = random.Random(seed)
    significant_trials = 0
    for _ in range(simulations):
        successes_a = _draw_binomial(rng, n_a, true_p_a)
        successes_b = _draw_binomial(rng, n_b, baseline_p)
        comparison = two_proportion_z_test(
            successes_a,
            n_a,
            successes_b,
            n_b,
            alpha=alpha,
        )
        if comparison.significant_at_alpha:
            significant_trials += 1

    return PowerSimulationResult(
        n_a=n_a,
        n_b=n_b,
        baseline_p=baseline_p,
        true_effect=true_effect,
        alpha=alpha,
        simulations=simulations,
        seed=seed,
        significant_trials=significant_trials,
        empirical_power=significant_trials / simulations,
    )


def _draw_binomial(rng: random.Random, n: int, probability: float) -> int:
    return sum(rng.random() < probability for _ in range(n))


def expected_regret(
    model_rates: dict[str, tuple[int, int]],
    metric_name: str,
) -> RegretReport:
    """Estimate regret for shipping the apparent-best observed model.

    For each alternative, let ``D = p_alt - p_best``. The observed proportions
    give ``D ~ Normal(mu, sigma^2)`` under the same unpooled normal
    approximation used for the confidence interval in ``two_proportion_z_test``.
    Regret is the positive part ``E[max(0, D)]``.

    Closed form: for ``X ~ Normal(mu, sigma^2)``,
    ``E[max(0, X)] = sigma * phi(mu / sigma) + mu * Phi(mu / sigma)``.
    This is the normal first-order loss / partial-expectation formula (same
    derivation as
    https://stats.stackexchange.com/questions/399429/how-to-compute-the-loss-normal-function-not-standard-normal-distribution).

    ``sigma`` uses the Agresti-Coull "plus-four" smoothed rate
    (``(successes + 2) / (n + 4)``) in place of the raw observed rate,
    **only** inside the variance term — ``mu``/``rate_difference``/
    ``observed_rate`` still report the raw observed rate, the actual
    finding. Without this, a boundary rate
    (``p in {0, 1}``, e.g. a small all-pass/all-fail sample) forces
    ``p*(1-p) = 0`` and therefore ``sigma = 0``, printing exactly ``0.0``
    regret — the opposite of the intended "identical rates, tiny n →
    larger regret" behavior a tiny boundary sample should produce.

    ``model_rates`` maps model name to ``(successes, n)`` for one metric.
    Models with ``n == 0`` cannot be the apparent best; they remain visible in
    the alternatives with ``insufficient_data=True`` and a ``0.0`` placeholder
    regret, matching this module's no-NaN convention.
    """
    if not metric_name:
        raise ValueError("metric_name must be non-empty")
    _validate_model_rates(model_rates)

    computable = [
        (model, successes, n, successes / n)
        for model, (successes, n) in model_rates.items()
        if n > 0
    ]
    if not computable:
        return RegretReport(
            metric_name=metric_name,
            apparent_best_model=None,
            apparent_best_successes=0,
            apparent_best_n=0,
            apparent_best_rate=_INSUFFICIENT_PLACEHOLDER,
            alternatives=tuple(
                _insufficient_alternative(model, successes, n)
                for model, (successes, n) in sorted(model_rates.items())
            ),
            insufficient_data=True,
        )

    best_model, best_successes, best_n, best_rate = min(
        computable, key=lambda item: (-item[3], item[0])
    )
    alternatives: list[RegretAlternative] = []
    for model, (successes, n) in sorted(model_rates.items()):
        if model == best_model:
            continue
        if n <= 0:
            alternatives.append(_insufficient_alternative(model, successes, n))
            continue

        alternative_rate = successes / n
        rate_difference = alternative_rate - best_rate
        se = math.sqrt(
            _plus_four_variance(successes, n)
            + _plus_four_variance(best_successes, best_n)
        )
        alternatives.append(
            RegretAlternative(
                model=model,
                successes=successes,
                n=n,
                observed_rate=alternative_rate,
                rate_difference=rate_difference,
                standard_error=se,
                expected_regret=_positive_normal_partial_expectation(
                    rate_difference, se
                ),
                insufficient_data=False,
                small_n=(n < SMALL_N_THRESHOLD or best_n < SMALL_N_THRESHOLD),
            )
        )

    return RegretReport(
        metric_name=metric_name,
        apparent_best_model=best_model,
        apparent_best_successes=best_successes,
        apparent_best_n=best_n,
        apparent_best_rate=best_rate,
        alternatives=tuple(alternatives),
        insufficient_data=False,
    )


def _insufficient_alternative(model: str, successes: int, n: int) -> RegretAlternative:
    return RegretAlternative(
        model=model,
        successes=successes,
        n=n,
        observed_rate=_INSUFFICIENT_PLACEHOLDER,
        rate_difference=_INSUFFICIENT_PLACEHOLDER,
        standard_error=_INSUFFICIENT_PLACEHOLDER,
        expected_regret=_INSUFFICIENT_PLACEHOLDER,
        insufficient_data=True,
        small_n=False,
    )


def _plus_four_variance(successes: int, n: int) -> float:
    """Agresti-Coull "plus-four" variance term for one arm's SE contribution.

    ``p_tilde = (successes + 2) / (n + 4)`` never lands exactly on 0 or 1
    even when the raw observed rate does, so this arm's contribution to the
    regret standard error stays a real, positive number instead of
    collapsing to 0 at a boundary rate. Standard adjusted-Wald construction
    (Agresti & Coull, 1998); used here for the SE only, never for
    ``observed_rate``/``rate_difference``, which stay the raw observed rate.
    """
    n_tilde = n + 4
    p_tilde = (successes + 2) / n_tilde
    return p_tilde * (1 - p_tilde) / n_tilde


def _positive_normal_partial_expectation(mu: float, sigma: float) -> float:
    if sigma < 0:
        raise ValueError("sigma must be non-negative")
    if sigma == 0:
        return max(0.0, mu)

    z = mu / sigma
    pdf = math.exp(-(z**2) / 2) / math.sqrt(2 * math.pi)
    return sigma * pdf + mu * _STANDARD_NORMAL.cdf(z)


def _validate_model_rates(model_rates: dict[str, tuple[int, int]]) -> None:
    if len(model_rates) < 2:
        raise ValueError("expected_regret requires at least two models")
    if len(set(model_rates)) != len(model_rates):
        raise ValueError("expected_regret requires unique model names")
    for model, (successes, n) in model_rates.items():
        if not model:
            raise ValueError("model names must be non-empty")
        if successes < 0:
            raise ValueError("successes must be non-negative")
        if n < 0:
            raise ValueError("n must be non-negative")
        if successes > n:
            raise ValueError("successes cannot exceed n")


def _validate_power_inputs(baseline_p: float, alpha: float, power: float) -> None:
    if not 0 <= baseline_p <= 1:
        raise ValueError("baseline_p must be between 0 and 1")
    _validate_alpha(alpha)
    if not 0 < power < 1:
        raise ValueError("power must be between 0 and 1")


def _validate_alpha(alpha: float) -> None:
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between 0 and 1")


def _validate_confidence(confidence: float) -> None:
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")


def _validate_non_inferiority_inputs(margin: float, alpha: float) -> None:
    if not 0 < margin <= 1:
        raise ValueError("margin must be > 0 and <= 1")
    _validate_alpha(alpha)
    if alpha >= 0.5:
        raise ValueError("alpha must be below 0.5 for non-inferiority testing")


def _normal_approximation_difference_ci(
    proportion_left: float,
    n_left: int,
    proportion_right: float,
    n_right: int,
    difference: float,
    confidence: float,
) -> tuple[float, float]:
    se_unpooled = math.sqrt(
        proportion_left * (1 - proportion_left) / n_left
        + proportion_right * (1 - proportion_right) / n_right
    )
    z_crit = _STANDARD_NORMAL.inv_cdf(1 - (1 - confidence) / 2)
    return difference - z_crit * se_unpooled, difference + z_crit * se_unpooled


def _validate_information_fraction(information_fraction: float) -> None:
    if not 0 < information_fraction <= 1:
        raise ValueError("information_fraction must be > 0 and <= 1")


def _sequential_boundary(
    z_stat: float, information_fraction: float, alpha: float
) -> SequentialBoundary:
    critical_z = obrien_fleming_boundary(information_fraction, alpha)
    return SequentialBoundary(
        information_fraction=information_fraction,
        alpha=alpha,
        alpha_spent=obrien_fleming_alpha_spent(information_fraction, alpha),
        critical_z=critical_z,
        significant_after_sequential_boundary=abs(z_stat) >= critical_z,
    )


def compare_models(
    report_a: ModelOutcomeReport,
    report_b: ModelOutcomeReport,
    *,
    confidence: float = 0.95,
    alpha: float = DEFAULT_ALPHA,
    comparison_family_size: int = COMPARE_TEST_FAMILY_SIZE,
    prior_alpha: float = DEFAULT_BAYESIAN_PRIOR_ALPHA,
    prior_beta: float = DEFAULT_BAYESIAN_PRIOR_BETA,
    bayesian_sample_count: int = DEFAULT_BAYESIAN_SAMPLES,
    bayesian_seed: int = DEFAULT_BAYESIAN_SEED,
    information_fraction: float | None = None,
) -> ModelComparison:
    """Build the full A/B significance comparison from two
    ``ModelOutcomeReport`` rows (same org, same window — the caller's job to
    ensure that, same as reading the two rows off one ``build_model_report``
    call already guarantees).

    CI pass rate's counts are ``(ci_passed, ci_linked)`` — the same
    numerator/denominator the descriptive ``ci_pass_rate`` column already
    uses. Attribution rate's counts are ``(attributed_inference_calls,
    completions)`` likewise. Nothing here re-derives a rate the report row
    does not already carry.
    """
    ci_pass_rate = two_proportion_z_test(
        report_a.ci_passed,
        report_a.ci_linked,
        report_b.ci_passed,
        report_b.ci_linked,
        confidence=confidence,
        alpha=alpha,
        comparison_family_size=comparison_family_size,
        information_fraction=information_fraction,
    )
    attribution_rate = two_proportion_z_test(
        report_a.attributed_inference_calls,
        report_a.completions,
        report_b.attributed_inference_calls,
        report_b.completions,
        confidence=confidence,
        alpha=alpha,
        comparison_family_size=comparison_family_size,
        information_fraction=information_fraction,
    )
    bayesian_ci_pass_rate = beta_binomial_probability_of_improvement(
        report_a.ci_passed,
        report_a.ci_linked,
        report_b.ci_passed,
        report_b.ci_linked,
        prior_alpha=prior_alpha,
        prior_beta=prior_beta,
        sample_count=bayesian_sample_count,
        seed=bayesian_seed,
    )
    bayesian_attribution_rate = beta_binomial_probability_of_improvement(
        report_a.attributed_inference_calls,
        report_a.completions,
        report_b.attributed_inference_calls,
        report_b.completions,
        prior_alpha=prior_alpha,
        prior_beta=prior_beta,
        sample_count=bayesian_sample_count,
        seed=bayesian_seed,
    )
    return ModelComparison(
        model_a=report_a.model,
        model_b=report_b.model,
        alpha=alpha,
        bonferroni_alpha=alpha / comparison_family_size,
        comparison_family_size=comparison_family_size,
        information_fraction=information_fraction,
        ci_pass_rate=ci_pass_rate,
        attribution_rate=attribution_rate,
        bayesian_ci_pass_rate=bayesian_ci_pass_rate,
        bayesian_attribution_rate=bayesian_attribution_rate,
    )


def compare_all_models(
    reports: list[ModelOutcomeReport],
    *,
    confidence: float = 0.95,
    alpha: float = DEFAULT_ALPHA,
) -> MultiModelComparison:
    """Compare every unordered model pair and apply BH FDR control.

    The BH family is the full round-robin hypothesis family: CI pass rate and
    attribution rate for every unordered pair of the N input models. Each pair is
    still computed through ``compare_models`` so the underlying z-test and
    Bonferroni plumbing remain single-sourced.
    """
    _validate_alpha(alpha)
    if len(reports) < 2:
        raise ValueError("compare_all_models requires at least two models")

    models = tuple(report.model for report in reports)
    if len(set(models)) != len(models):
        raise ValueError("compare_all_models requires unique model names")

    comparison_family_size = 2 * math.comb(len(reports), 2)
    comparisons_by_pair = tuple(
        compare_models(
            report_a,
            report_b,
            confidence=confidence,
            alpha=alpha,
            comparison_family_size=comparison_family_size,
        )
        for report_a, report_b in combinations(reports, 2)
    )

    ranked_p_values: list[_RankedPValue] = []
    for comparison_index, comparison in enumerate(comparisons_by_pair):
        for metric in ("ci_pass_rate", "attribution_rate"):
            pc = getattr(comparison, metric)
            if pc.insufficient_data:
                continue
            ranked_p_values.append(
                _RankedPValue(
                    comparison_index=comparison_index,
                    metric=metric,
                    p_value=pc.p_value,
                    original_index=len(ranked_p_values),
                )
            )

    fdr_by_metric: dict[tuple[int, _MetricName], ProportionComparison] = {}
    if ranked_p_values:
        sorted_p_values = sorted(
            ranked_p_values, key=lambda item: (item.p_value, item.original_index)
        )
        m = len(sorted_p_values)
        rejected_count = 0
        ranks_by_metric: dict[tuple[int, _MetricName], int] = {}
        for rank, item in enumerate(sorted_p_values, start=1):
            ranks_by_metric[(item.comparison_index, item.metric)] = rank
            if item.p_value <= (rank / m) * alpha:
                rejected_count = rank

        bh_alpha = (rejected_count / m) * alpha if rejected_count else 0.0
        rejected = {
            (item.comparison_index, item.metric)
            for item in sorted_p_values[:rejected_count]
        }
        for item in ranked_p_values:
            key = (item.comparison_index, item.metric)
            comparison = comparisons_by_pair[item.comparison_index]
            pc = getattr(comparison, item.metric)
            fdr_by_metric[key] = replace(
                pc,
                benjamini_hochberg_rank=ranks_by_metric[key],
                benjamini_hochberg_alpha=bh_alpha,
                significant_after_benjamini_hochberg=key in rejected,
            )

    adjusted_comparisons: list[ModelComparison] = []
    for comparison_index, comparison in enumerate(comparisons_by_pair):
        ci_pass_rate = fdr_by_metric.get(
            (comparison_index, "ci_pass_rate"),
            replace(
                comparison.ci_pass_rate,
                benjamini_hochberg_alpha=0.0,
                significant_after_benjamini_hochberg=False,
            ),
        )
        attribution_rate = fdr_by_metric.get(
            (comparison_index, "attribution_rate"),
            replace(
                comparison.attribution_rate,
                benjamini_hochberg_alpha=0.0,
                significant_after_benjamini_hochberg=False,
            ),
        )
        adjusted_comparisons.append(
            replace(
                comparison,
                ci_pass_rate=ci_pass_rate,
                attribution_rate=attribution_rate,
            )
        )

    return MultiModelComparison(
        models=models,
        alpha=alpha,
        comparison_family_size=comparison_family_size,
        tested_family_size=len(ranked_p_values),
        comparisons=tuple(adjusted_comparisons),
    )

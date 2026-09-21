# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two-proportion significance testing tests.

``two_proportion_z_test`` is a pure function over raw counts — the reference
values below (80/100 vs. 50/100, and the 50/100 vs. 50/100 null case) are
hand-computed independently (pooled/unpooled SE by the textbook formulas,
critical value from ``statistics.NormalDist().inv_cdf(0.975)``) rather than
merely asserted against whatever the implementation happens to produce.
``compare_models`` is then exercised as thin plumbing over two
``ModelOutcomeReport`` rows into that same function.
"""

from __future__ import annotations

import math
import random
from statistics import NormalDist

import pytest
from sediment_derive import Provenance
import sediment_export.significance as significance_module

from sediment_export import (
    CIGrain,
    ModelOutcomeReport,
    TrendStatus,
    compare_all_models,
    compare_models,
    expected_regret,
)
from sediment_export.significance import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    SMALL_N_THRESHOLD,
    BayesianProportionComparison,
    beta_binomial_probability_of_improvement,
    bootstrap_two_proportion_interval_diagnostic,
    cohens_h,
    effect_decay_check,
    minimum_detectable_effect,
    non_inferiority_test,
    obrien_fleming_alpha_spent,
    obrien_fleming_boundary,
    required_days_for_mde,
    required_n_per_arm_for_mde,
    simulate_two_proportion_power,
    two_proportion_z_test,
)

ORG_NOW_SINCE_DAYS = None  # rows below don't need a real window to compare
POWER_SIMULATIONS = 2000
POWER_SIMULATION_SEED = 152
POWER_SIMULATION_N_PER_ARM = 200
POWER_SIMULATION_BASELINE = 0.5
POWER_SIMULATION_TARGET = 0.80
POWER_SIMULATION_ALPHA = 0.05


def test_bootstrap_interval_diagnostic_replaces_cross_validation_public_name() -> None:
    assert hasattr(significance_module, "bootstrap_two_proportion_interval_diagnostic")
    assert not hasattr(significance_module, "bootstrap_two_proportion_cross_validation")


POWER_SIMULATION_EFFECT_MULTIPLIERS = (0.75, 1.0, 1.25)
POWER_SIMULATION_NORMAL = NormalDist()


def _report(
    model: str,
    *,
    completions: int,
    attributed: int,
    ci_linked: int,
    ci_passed: int,
) -> ModelOutcomeReport:
    attribution_rate = attributed / completions if completions else 0.0
    ci_pass_rate = ci_passed / ci_linked if ci_linked else 0.0
    return ModelOutcomeReport(
        model=model,
        since_days=ORG_NOW_SINCE_DAYS,
        completions=completions,
        attributed_inference_calls=attributed,
        attribution_rate=attribution_rate,
        attribution_rate_ci=(0.0, 0.0),
        ci_linked=ci_linked,
        ci_passed=ci_passed,
        ci_pass_rate=ci_pass_rate,
        ci_pass_rate_ci=(0.0, 0.0),
        explicit_accepts=0,
        explicit_rejects=0,
        mean_similarity=0.0,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        grain=CIGrain.COMMIT,
    )


# ── real-difference case: hand-verified against independently computed
# reference values ──────────────────────────────────────────────────────────


def test_real_difference_80_of_100_vs_50_of_100_matches_hand_computed_reference() -> (
    None
):
    result = two_proportion_z_test(80, 100, 50, 100)

    assert result.insufficient_data is False
    assert result.small_n is False
    assert result.proportion_a == 0.8
    assert result.proportion_b == 0.5
    assert math.isclose(result.difference, 0.3, rel_tol=1e-9)

    # Hand-computed reference (pooled p = 130/200 = 0.65):
    #   se_pooled = sqrt(0.65 * 0.35 * (1/100 + 1/100)) = 0.06745368781616021
    #   z = 0.3 / se_pooled = 4.447495899966608
    #   p = 2 * (1 - Phi(z)) = 8.687711676946819e-06
    #   se_unpooled = sqrt(0.8*0.2/100 + 0.5*0.5/100) = 0.06403124237432849
    #   z_crit(95%) = 1.9599639845400534
    #   ci = 0.3 +/- z_crit * se_unpooled = [0.17450107..., 0.42549892...]
    #   Cohen's h = 2*asin(sqrt(0.8)) - 2*asin(sqrt(0.5))
    #             = 0.643501108793284
    assert math.isclose(result.z_stat, 4.447495899966608, rel_tol=1e-9)
    assert math.isclose(result.p_value, 8.687711676946819e-06, rel_tol=1e-6)
    assert math.isclose(result.cohens_h, 0.643501108793284, rel_tol=1e-12)
    assert math.isclose(result.ci_low, 0.17450107106096127, rel_tol=1e-9)
    assert math.isclose(result.ci_high, 0.42549892893903885, rel_tol=1e-9)
    # MDE uses the pooled observed proportion as its baseline:
    #   p_pool = 0.65
    #   (z_alpha/2 + z_beta) * sqrt(0.65 * 0.35 * (1/100 + 1/100))
    #   = 0.18897725469296123
    assert math.isclose(result.baseline_p, 0.65, rel_tol=1e-9)
    assert math.isclose(
        result.minimum_detectable_effect,
        0.18897725469296123,
        rel_tol=1e-9,
    )

    # A real gap should read as small-p, and the CI should exclude zero.
    assert result.p_value < 0.001
    assert result.ci_low > 0


def test_cohens_h_matches_hand_computed_reference() -> None:
    assert math.isclose(cohens_h(0.8, 0.5), 0.643501108793284, rel_tol=1e-12)


# ── O'Brien-Fleming peeking correction ─────────────────────────────────────


def test_obrien_fleming_boundary_at_final_look_matches_fixed_sample_z() -> None:
    result = obrien_fleming_boundary(1.0, alpha=0.05)

    assert math.isclose(result, 1.9599639845400534, rel_tol=1e-12)


def test_obrien_fleming_boundary_is_more_conservative_at_half_information() -> None:
    halfway = obrien_fleming_boundary(0.5, alpha=0.05)
    final = obrien_fleming_boundary(1.0, alpha=0.05)

    assert halfway > final
    assert math.isclose(halfway, 2.7718076486993546, rel_tol=1e-12)
    assert math.isclose(
        obrien_fleming_alpha_spent(0.5, alpha=0.05),
        0.005574596680784527,
        rel_tol=1e-12,
    )


def test_obrien_fleming_alpha_spent_matches_published_reference_table() -> None:
    # Published Lan-DeMets O'Brien-Fleming teaching tables report cumulative
    # alpha 0.0025 at t=0.55 for one-sided alpha=0.025. The formula carries
    # more precision; rounding to four decimals gives the table value.
    result = obrien_fleming_alpha_spent(0.55, alpha=0.025)

    assert math.isclose(result, 0.002508561402993603, rel_tol=1e-12)
    assert math.isclose(round(result, 4), 0.0025, rel_tol=1e-12)


def test_naive_significant_result_can_fail_sequential_boundary_at_early_peek() -> None:
    result = two_proportion_z_test(
        33,
        100,
        20,
        100,
        alpha=0.05,
        information_fraction=0.3,
    )

    assert result.significant_at_alpha is True
    assert math.isclose(result.z_stat, 2.0828680009133076, rel_tol=1e-12)
    assert math.isclose(result.p_value, 0.03726325708596967, rel_tol=1e-12)
    assert result.sequential_boundary is not None
    assert math.isclose(
        result.sequential_boundary.critical_z,
        3.578388287434313,
        rel_tol=1e-12,
    )
    assert math.isclose(
        result.sequential_boundary.alpha_spent,
        0.000345719580169046,
        rel_tol=1e-12,
    )
    assert result.sequential_boundary.significant_after_sequential_boundary is False


def test_sequential_boundary_uses_bonferroni_alpha_not_raw_alpha() -> None:
    # Same fixture as the naive/early-peek test above, but at t=1.0 with a
    # two-test family (comparison_family_size=2, as compare_models passes).
    # Before the fix, the boundary used raw alpha=0.05 (critical_z=1.95996),
    # so this exact z_stat (2.0829) would cross it while
    # significant_after_bonferroni (raw_alpha/2=0.025) stayed False --
    # a same-row contradiction between "sig=no" and "seq_sig=yes".
    result = two_proportion_z_test(
        33,
        100,
        20,
        100,
        alpha=0.05,
        comparison_family_size=2,
        information_fraction=1.0,
    )

    assert result.significant_after_bonferroni is False
    assert result.sequential_boundary is not None
    assert result.sequential_boundary.alpha == result.bonferroni_alpha
    assert math.isclose(
        result.sequential_boundary.critical_z,
        2.2414027276049464,
        rel_tol=1e-12,
    )
    # At t=1.0 the sequential boundary must agree with the non-sequential
    # Bonferroni call for the same alpha and z-statistic -- never contradict.
    assert (
        result.sequential_boundary.significant_after_sequential_boundary
        == result.significant_after_bonferroni
    )


def test_information_fraction_must_be_in_unit_interval() -> None:
    for invalid in (0.0, -0.1, 1.1):
        with pytest.raises(ValueError, match="information_fraction"):
            obrien_fleming_boundary(invalid)


# ── no-difference case: large p-value, CI spans zero ────────────────────────


def test_no_difference_50_of_100_vs_50_of_100_yields_large_p_and_ci_spans_zero() -> (
    None
):
    result = two_proportion_z_test(50, 100, 50, 100)

    assert result.insufficient_data is False
    assert result.difference == 0.0
    assert result.z_stat == 0.0
    assert result.p_value == 1.0
    # Hand-computed: se_unpooled = sqrt(0.25/100 + 0.25/100) = 0.07071067811865475
    # ci = 0 +/- 1.9599639845400534 * se_unpooled = +/-0.13859038243496774
    assert math.isclose(result.ci_low, -0.13859038243496774, rel_tol=1e-9)
    assert math.isclose(result.ci_high, 0.13859038243496774, rel_tol=1e-9)
    assert result.ci_low < 0 < result.ci_high
    assert result.p_value > 0.9


# ── small-n edge case: valid but flagged, never a crash ─────────────────────


def test_small_n_two_vs_three_is_valid_but_flagged_not_a_crash() -> None:
    result = two_proportion_z_test(1, 2, 2, 3)

    assert result.insufficient_data is False
    assert result.small_n is True
    assert result.n_a == 2
    assert result.n_b == 3
    # A real (if unreliable) computation, not a placeholder.
    assert result.proportion_a == 0.5
    assert math.isclose(result.proportion_b, 2 / 3, rel_tol=1e-9)


def test_small_n_threshold_boundary() -> None:
    # Exactly at the threshold on both sides: not flagged.
    at_threshold = two_proportion_z_test(15, SMALL_N_THRESHOLD, 15, SMALL_N_THRESHOLD)
    assert at_threshold.small_n is False

    # One group just under the threshold: flagged.
    below_threshold = two_proportion_z_test(
        15, SMALL_N_THRESHOLD - 1, 15, SMALL_N_THRESHOLD
    )
    assert below_threshold.small_n is True


# ── zero-denominator: insufficient_data, never a ZeroDivisionError ──────────


def test_zero_n_a_is_insufficient_data_not_a_crash() -> None:
    result = two_proportion_z_test(0, 0, 10, 20)
    assert result.insufficient_data is True
    assert result.proportion_a == 0.0
    assert result.proportion_b == 0.0
    assert result.difference == 0.0
    assert result.ci_low == 0.0
    assert result.ci_high == 0.0
    assert result.z_stat == 0.0
    assert result.p_value == 0.0
    assert result.baseline_p == 0.0
    assert result.minimum_detectable_effect == 0.0
    assert result.small_n is False
    assert result.n_a == 0
    assert result.n_b == 20


def test_zero_n_b_is_insufficient_data_not_a_crash() -> None:
    result = two_proportion_z_test(10, 20, 0, 0)
    assert result.insufficient_data is True


def test_zero_n_on_both_sides_is_insufficient_data_not_a_crash() -> None:
    result = two_proportion_z_test(0, 0, 0, 0)
    assert result.insufficient_data is True


# ── degenerate pooled SE (all-same-outcome) never divides by zero ──────────


def test_all_pass_both_groups_is_zero_difference_not_a_crash() -> None:
    result = two_proportion_z_test(50, 50, 40, 40)
    assert result.insufficient_data is False
    assert result.small_n is False
    assert result.proportion_a == 1.0
    assert result.proportion_b == 1.0
    assert result.difference == 0.0
    assert result.z_stat == 0.0
    assert result.p_value == 1.0
    assert result.baseline_p == 1.0
    assert result.degenerate_se is True
    # The normal-approximation power formula is undefined at a pooled rate of
    # exactly 1: the MDE field is the 0.0 placeholder, flagged via
    # degenerate_se, not a real "smallest detectable difference" of 0.0.
    assert result.minimum_detectable_effect == 0.0


def test_all_fail_both_groups_is_zero_difference_not_a_crash() -> None:
    result = two_proportion_z_test(0, 50, 0, 40)
    assert result.insufficient_data is False
    assert result.small_n is False
    assert result.proportion_a == 0.0
    assert result.proportion_b == 0.0
    assert result.difference == 0.0
    assert result.z_stat == 0.0
    assert result.p_value == 1.0
    assert result.baseline_p == 0.0
    assert result.degenerate_se is True
    assert result.minimum_detectable_effect == 0.0


def test_mixed_arm_boundary_is_not_degenerate_keeps_positive_mde() -> None:
    # One arm all-pass, the other not: pooled_p lands strictly inside (0, 1),
    # so the SE is positive and MDE is a real (positive) value. The guard
    # must be boundary-only, never over-broad.
    result = two_proportion_z_test(50, 50, 40, 50)
    assert result.proportion_a == 1.0
    assert result.proportion_b == 0.8
    assert result.baseline_p == 0.9
    assert result.degenerate_se is False
    assert result.minimum_detectable_effect > 0.0


def test_insufficient_data_is_not_degenerate_se() -> None:
    # The n=0 path is its own unavailable condition (its own CLI message); it
    # must not also raise degenerate_se, which would be a contradictory flag.
    result = two_proportion_z_test(0, 0, 10, 20)
    assert result.insufficient_data is True
    assert result.degenerate_se is False


# ── power analysis: MDE and inverse sample-size calculation ────────────────


def test_minimum_detectable_effect_matches_hand_computed_reference() -> None:
    result = minimum_detectable_effect(100, 100, 0.5)

    # Hand-computed reference:
    #   z_alpha/2 = NormalDist().inv_cdf(0.975) = 1.9599639845400534
    #   z_beta = NormalDist().inv_cdf(0.80) = 0.8416212335729144
    #   sqrt(0.5 * 0.5 * (1/100 + 1/100)) = 0.07071067811865475
    #   MDE = (1.9599639845400534 + 0.8416212335729144) * 0.07071067811865475
    assert math.isclose(result, 0.19810199057996725, rel_tol=1e-9)


def test_minimum_detectable_effect_zero_n_returns_insufficient_placeholder() -> None:
    assert minimum_detectable_effect(0, 100, 0.5) == 0.0
    assert minimum_detectable_effect(100, 0, 0.5) == 0.0


def test_required_n_per_arm_for_mde_matches_hand_computed_reference() -> None:
    result = required_n_per_arm_for_mde(0.05, 0.5)

    # Hand-computed from the inverse equal-arm formula:
    #   ceil(2 * 0.5 * 0.5 * (1.9599639845400534 + 0.8416212335729144)^2 / 0.05^2)
    #   = ceil(1569.775...) = 1570
    assert result == 1570


def _analytic_power_prediction(true_effect: float) -> float:
    """Same normal approximation the MDE formula rearranges.

    At 80% power the binomial standard error of a Monte Carlo estimate with
    B=2000 is sqrt(0.8 * 0.2 / 2000) ~= 0.009. A 5-SE tolerance is about
    4.5 percentage points, so it covers fixed-seed simulation noise without
    allowing the old sqrt(2)-inflated MDE formula's ~99% at-threshold power.
    """
    se = math.sqrt(
        POWER_SIMULATION_BASELINE
        * (1 - POWER_SIMULATION_BASELINE)
        * (1 / POWER_SIMULATION_N_PER_ARM + 1 / POWER_SIMULATION_N_PER_ARM)
    )
    z_crit = POWER_SIMULATION_NORMAL.inv_cdf(1 - POWER_SIMULATION_ALPHA / 2)
    mean_z_under_alternative = true_effect / se
    return (
        1
        - POWER_SIMULATION_NORMAL.cdf(z_crit - mean_z_under_alternative)
        + POWER_SIMULATION_NORMAL.cdf(-z_crit - mean_z_under_alternative)
    )


def test_mde_power_simulation_tracks_analytic_prediction_monotonically() -> None:
    predicted_mde = minimum_detectable_effect(
        POWER_SIMULATION_N_PER_ARM,
        POWER_SIMULATION_N_PER_ARM,
        POWER_SIMULATION_BASELINE,
        alpha=POWER_SIMULATION_ALPHA,
        power=POWER_SIMULATION_TARGET,
    )
    results = [
        simulate_two_proportion_power(
            POWER_SIMULATION_N_PER_ARM,
            POWER_SIMULATION_N_PER_ARM,
            POWER_SIMULATION_BASELINE,
            predicted_mde * multiplier,
            simulations=POWER_SIMULATIONS,
            seed=POWER_SIMULATION_SEED,
            alpha=POWER_SIMULATION_ALPHA,
        )
        for multiplier in POWER_SIMULATION_EFFECT_MULTIPLIERS
    ]

    empirical_powers = [result.empirical_power for result in results]
    assert empirical_powers[0] < empirical_powers[1] < empirical_powers[2]

    for result in results:
        analytic_power = _analytic_power_prediction(result.true_effect)
        tolerance = 5 * math.sqrt(
            analytic_power * (1 - analytic_power) / POWER_SIMULATIONS
        )
        assert math.isclose(
            result.empirical_power,
            analytic_power,
            abs_tol=tolerance,
        )

    at_mde = results[1]
    at_mde_tolerance = 5 * math.sqrt(
        POWER_SIMULATION_TARGET * (1 - POWER_SIMULATION_TARGET) / POWER_SIMULATIONS
    )
    assert math.isclose(
        at_mde.empirical_power,
        POWER_SIMULATION_TARGET,
        abs_tol=at_mde_tolerance,
    )


def test_mde_power_simulation_is_deterministic_for_same_seed() -> None:
    predicted_mde = minimum_detectable_effect(
        POWER_SIMULATION_N_PER_ARM,
        POWER_SIMULATION_N_PER_ARM,
        POWER_SIMULATION_BASELINE,
    )

    first = simulate_two_proportion_power(
        POWER_SIMULATION_N_PER_ARM,
        POWER_SIMULATION_N_PER_ARM,
        POWER_SIMULATION_BASELINE,
        predicted_mde,
        simulations=POWER_SIMULATIONS,
        seed=POWER_SIMULATION_SEED,
    )
    second = simulate_two_proportion_power(
        POWER_SIMULATION_N_PER_ARM,
        POWER_SIMULATION_N_PER_ARM,
        POWER_SIMULATION_BASELINE,
        predicted_mde,
        simulations=POWER_SIMULATIONS,
        seed=POWER_SIMULATION_SEED,
    )

    assert first.significant_trials == second.significant_trials
    assert math.isclose(
        first.empirical_power,
        second.empirical_power,
        rel_tol=0.0,
        abs_tol=0.0,
    )


def test_required_days_for_mde_matches_hand_computed_reference() -> None:
    result = required_days_for_mde(0.05, 0.5, 100, 80)

    # Hand-computed from the required-n reference above:
    #   n=1570 per arm; arm A at 100/day takes 15.7 days; arm B at 80/day
    #   takes 19.625 days, so the slower arm governs the equal-allocation plan.
    assert result == 19.625


def test_required_days_for_mde_omits_non_positive_traffic() -> None:
    assert required_days_for_mde(0.05, 0.5, 0, 80) is None
    assert required_days_for_mde(0.05, 0.5, 100, 0) is None


def test_required_n_per_arm_rejects_non_positive_target_mde() -> None:
    with pytest.raises(ValueError, match="target_mde"):
        required_n_per_arm_for_mde(0.0, 0.5)


@pytest.mark.parametrize("boundary_p", [0.0, 1.0])
def test_required_n_per_arm_for_mde_boundary_returns_none(boundary_p: float) -> None:
    # p*(1-p)=0 forced the old formula to ceil(0 / target_mde^2) == 0,
    # i.e. "plan for n=0 per arm". The boundary must surface an explicit
    # unavailable sentinel instead.
    assert required_n_per_arm_for_mde(0.05, boundary_p) is None


@pytest.mark.parametrize("boundary_p", [0.0, 1.0])
def test_required_days_for_mde_boundary_returns_none(boundary_p: float) -> None:
    # required_days_for_mde forwards the unavailable required-n rather than
    # printing a fabricated 0.0-day duration.
    assert required_days_for_mde(0.05, boundary_p, 100, 80) is None


# ── non-inferiority: one-sided lower-margin test over p_b - p_a ─────────────


def test_non_inferiority_equal_large_samples_with_reasonable_margin_passes() -> None:
    result = non_inferiority_test(800, 1000, 800, 1000, margin=0.05)

    assert result.insufficient_data is False
    assert result.small_n is False
    assert result.proportion_a == 0.8
    assert result.proportion_b == 0.8
    assert result.point_estimate == 0.0
    assert result.ci_lower_bound > -0.05
    assert result.is_non_inferior is True
    assert result.equivalent_two_sided_confidence == 0.9


def test_non_inferiority_b_worse_than_margin_fails() -> None:
    result = non_inferiority_test(800, 1000, 700, 1000, margin=0.05)

    assert math.isclose(result.point_estimate, -0.1, rel_tol=1e-12)
    assert result.ci_lower_bound < -0.05
    assert result.is_non_inferior is False


def test_non_inferiority_boundary_case_matches_hand_computed_lower_bound() -> None:
    result = non_inferiority_test(800, 1000, 770, 1000, margin=0.05)

    # Hand-computed lower bound using the one-sided alpha=0.05 convention:
    #   delta = p_b - p_a = 0.77 - 0.80 = -0.03
    #   se_unpooled = sqrt(0.77*0.23/1000 + 0.80*0.20/1000)
    #               = 0.018360283222216372
    #   z_(1-alpha) = NormalDist().inv_cdf(0.95) = 1.6448536269514715
    #   lower = delta - z_(1-alpha) * se = -0.06019997844991885
    # This is also the lower bound of the two-sided 90% CI, not a 95% CI.
    assert math.isclose(result.point_estimate, -0.03, rel_tol=1e-12)
    assert math.isclose(result.ci_lower_bound, -0.06019997844991885, rel_tol=1e-12)
    assert result.is_non_inferior is False


def test_non_inferiority_tiny_samples_do_not_confidently_pass() -> None:
    result = non_inferiority_test(3, 5, 3, 5, margin=0.05)

    assert result.insufficient_data is False
    assert result.small_n is True
    assert result.ci_lower_bound < -0.05
    assert result.is_non_inferior is False


def test_non_inferiority_zero_denominator_is_insufficient_not_a_crash() -> None:
    result = non_inferiority_test(0, 0, 10, 20, margin=0.05)

    assert result.insufficient_data is True
    assert result.point_estimate == 0.0
    assert result.ci_lower_bound == 0.0
    assert result.is_non_inferior is False


def test_non_inferiority_rejects_invalid_margin() -> None:
    with pytest.raises(ValueError, match="margin"):
        non_inferiority_test(10, 20, 10, 20, margin=0.0)


# ── multiple comparisons: raw vs Bonferroni-adjusted significance ──────────


def test_raw_significance_can_fail_bonferroni_adjusted_threshold() -> None:
    result = two_proportion_z_test(
        33,
        100,
        20,
        100,
        alpha=0.05,
        comparison_family_size=2,
    )

    # Hand-computed reference:
    #   p_a=0.33, p_b=0.20, pooled p=0.265
    #   z = 2.0828680009133076
    #   raw p = 0.03726325708596967
    assert math.isclose(result.p_value, 0.03726325708596967, rel_tol=1e-12)
    assert result.alpha == 0.05
    assert result.bonferroni_alpha == 0.025
    assert result.significant_at_alpha is True
    assert result.significant_after_bonferroni is False


# ── bootstrap interval diagnostic: fixed-seed resampling diagnostic ───────────


def test_bootstrap_interval_diagnostic_large_n_agrees_with_analytic_ci() -> None:
    result = bootstrap_two_proportion_interval_diagnostic(620, 1000, 580, 1000)

    assert result.analytic.insufficient_data is False
    assert result.analytic.small_n is False
    assert result.bootstrap_iterations == DEFAULT_BOOTSTRAP_ITERATIONS
    assert result.seed == DEFAULT_BOOTSTRAP_SEED
    assert result.analytic_point_in_bootstrap_ci is True
    assert result.substantial_disagreement is False
    assert result.disagreement_reasons == ()
    # Golden values from the pre-rename fixed-seed procedure. These pin the
    # resampling calculation itself, not only its proximity to the analytic
    # interval.
    assert math.isclose(
        result.bootstrap_ci_low,
        -0.0030249999999999587,
        rel_tol=0.0,
        abs_tol=1e-15,
    )
    assert math.isclose(
        result.bootstrap_ci_high,
        0.08299999999999996,
        rel_tol=0.0,
        abs_tol=1e-15,
    )
    assert result.ci_overlap_fraction == 1.0


def test_bootstrap_interval_diagnostic_small_n_computes_and_documents_divergence() -> (
    None
):
    result = bootstrap_two_proportion_interval_diagnostic(7, 12, 3, 10)

    assert result.analytic.insufficient_data is False
    assert result.analytic.small_n is True
    assert math.isfinite(result.bootstrap_ci_low)
    assert math.isfinite(result.bootstrap_ci_high)
    assert result.bootstrap_ci_low < result.bootstrap_ci_high
    # This fixed-seed small-n example differs by roughly three percentage
    # points at both ends of the interval, illustrating why the bootstrap is
    # a diagnostic rather than a replacement policy.
    assert abs(result.bootstrap_ci_low - result.analytic.ci_low) > 0.02
    assert abs(result.bootstrap_ci_high - result.analytic.ci_high) > 0.02


def test_bootstrap_interval_diagnostic_is_deterministic_for_the_same_seed() -> None:
    first = bootstrap_two_proportion_interval_diagnostic(80, 100, 50, 100, seed=8675309)
    second = bootstrap_two_proportion_interval_diagnostic(
        80, 100, 50, 100, seed=8675309
    )

    assert math.isclose(
        first.bootstrap_ci_low,
        second.bootstrap_ci_low,
        rel_tol=0.0,
        abs_tol=1e-15,
    )
    assert math.isclose(
        first.bootstrap_ci_high,
        second.bootstrap_ci_high,
        rel_tol=0.0,
        abs_tol=1e-15,
    )
    assert math.isclose(
        first.ci_overlap_fraction,
        second.ci_overlap_fraction,
        rel_tol=0.0,
        abs_tol=1e-15,
    )
    assert first.analytic_point_in_bootstrap_ci is second.analytic_point_in_bootstrap_ci
    assert first.substantial_disagreement is second.substantial_disagreement
    assert first.disagreement_reasons == second.disagreement_reasons


def test_bootstrap_interval_diagnostic_rejects_too_few_iterations() -> None:
    with pytest.raises(ValueError, match="bootstrap_iterations"):
        bootstrap_two_proportion_interval_diagnostic(
            1, 10, 2, 10, bootstrap_iterations=1
        )


# ── compare_models: thin plumbing over two ModelOutcomeReport rows ─────────


def test_compare_models_maps_ci_pass_rate_and_attribution_rate_from_report_fields() -> (
    None
):
    report_a = _report(
        "model-a", completions=100, attributed=80, ci_linked=100, ci_passed=80
    )
    report_b = _report(
        "model-b", completions=100, attributed=50, ci_linked=100, ci_passed=50
    )

    comparison = compare_models(report_a, report_b)

    assert comparison.model_a == "model-a"
    assert comparison.model_b == "model-b"
    assert comparison.alpha == 0.05
    assert comparison.bonferroni_alpha == 0.025
    assert comparison.comparison_family_size == 2
    assert comparison.ci_pass_rate.proportion_a == 0.8
    assert comparison.ci_pass_rate.proportion_b == 0.5
    assert comparison.attribution_rate.proportion_a == 0.8
    assert comparison.attribution_rate.proportion_b == 0.5
    assert comparison.ci_pass_rate.p_value < 0.001
    assert comparison.attribution_rate.p_value < 0.001
    assert comparison.ci_pass_rate.significant_after_bonferroni is True
    assert comparison.attribution_rate.significant_after_bonferroni is True


def test_compare_models_threads_information_fraction_into_both_metrics() -> None:
    report_a = _report(
        "model-a", completions=100, attributed=33, ci_linked=100, ci_passed=33
    )
    report_b = _report(
        "model-b", completions=100, attributed=20, ci_linked=100, ci_passed=20
    )

    comparison = compare_models(report_a, report_b, information_fraction=0.3)

    assert comparison.information_fraction == 0.3
    assert comparison.ci_pass_rate.sequential_boundary is not None
    assert comparison.attribution_rate.sequential_boundary is not None
    ci_boundary = comparison.ci_pass_rate.sequential_boundary
    attribution_boundary = comparison.attribution_rate.sequential_boundary
    assert ci_boundary.significant_after_sequential_boundary is False
    assert attribution_boundary.significant_after_sequential_boundary is False


def test_compare_models_reports_insufficient_data_when_a_model_has_no_completions() -> (
    None
):
    report_a = _report("model-a", completions=0, attributed=0, ci_linked=0, ci_passed=0)
    report_b = _report(
        "model-b", completions=100, attributed=50, ci_linked=80, ci_passed=40
    )

    comparison = compare_models(report_a, report_b)

    assert comparison.attribution_rate.insufficient_data is True
    assert comparison.ci_pass_rate.insufficient_data is True
    assert comparison.bayesian_attribution_rate.insufficient_data is True
    assert comparison.bayesian_ci_pass_rate.insufficient_data is True


# ── Bayesian Beta-Binomial companion ───────────────────────────────────────


def _beta_pdf(x: float, alpha: float, beta: float) -> float:
    if x <= 0.0 or x >= 1.0:
        return 0.0
    log_normalizer = math.lgamma(alpha) + math.lgamma(beta) - math.lgamma(alpha + beta)
    return math.exp(
        (alpha - 1) * math.log(x) + (beta - 1) * math.log1p(-x) - log_normalizer
    )


def _simpson(values: list[float], step: float) -> float:
    total = values[0] + values[-1]
    total += 4 * sum(values[1:-1:2])
    total += 2 * sum(values[2:-1:2])
    return total * step / 3


def _quadrature_probability_a_gt_b(
    alpha_a: float,
    beta_a: float,
    alpha_b: float,
    beta_b: float,
    *,
    grid_points: int = 20_000,
) -> float:
    """Independent check: integrate Beta-A PDF times Beta-B CDF.

    This is deliberately separate from the production Monte Carlo method so
    the pinned posterior probability below has a second numerical reference.
    """
    step = 1 / grid_points
    pdf_a = [_beta_pdf(idx * step, alpha_a, beta_a) for idx in range(grid_points + 1)]
    pdf_b = [_beta_pdf(idx * step, alpha_b, beta_b) for idx in range(grid_points + 1)]
    cdf_b = [0.0] * (grid_points + 1)
    running = 0.0
    for idx in range(1, grid_points + 1):
        running += (pdf_b[idx - 1] + pdf_b[idx]) * step / 2
        cdf_b[idx] = running
    return _simpson(
        [pdf_a[idx] * cdf_b[idx] for idx in range(grid_points + 1)],
        step,
    )


def test_beta_binomial_probability_matches_independent_quadrature_reference() -> None:
    result = beta_binomial_probability_of_improvement(
        80,
        99,
        50,
        99,
        sample_count=1_000_000,
    )
    quadrature_reference = _quadrature_probability_a_gt_b(81, 20, 51, 50)

    # Flat-prior counts produce
    # Beta(81, 20) for A and Beta(51, 50) for B. The production method is
    # fixed-seed Monte Carlo; this deterministic quadrature reference was
    # computed independently by integrating f_A(x) * F_B(x).
    assert math.isclose(
        quadrature_reference,
        0.9999968493190731,
        rel_tol=1e-12,
    )
    # abs_tol, not exact equality: probability_a_gt_b is an integer draw
    # count over 1,000,000 samples, and betavariate's output depends on
    # libm transcendental functions that can differ by 1 ULP across
    # platforms -- enough, in the rare case a draw pair lands exactly on
    # the boundary, to flip one comparison and shift this value by
    # +/-0.000001. A cross-platform CI flake of this exact shape has
    # already bitten the Wilson-interval test.
    assert math.isclose(result.probability_a_gt_b, 0.999997, abs_tol=1e-5)
    assert math.isclose(
        result.probability_a_gt_b,
        quadrature_reference,
        abs_tol=0.0001,
    )
    assert math.isclose(result.posterior_mean_a, 81 / 101, rel_tol=1e-12)
    assert math.isclose(result.posterior_mean_b, 51 / 101, rel_tol=1e-12)
    assert math.isclose(result.mean_difference, 30 / 101, rel_tol=1e-12)
    assert math.isclose(
        result.credible_interval_low,
        0.17086414347829412,
        rel_tol=1e-12,
    )
    assert math.isclose(
        result.credible_interval_high,
        0.4188574386455925,
        rel_tol=1e-12,
    )


def test_beta_binomial_comparison_is_deterministic_for_identical_inputs() -> None:
    first = beta_binomial_probability_of_improvement(33, 100, 20, 100)
    second = beta_binomial_probability_of_improvement(33, 100, 20, 100)

    assert first == second


def test_beta_binomial_tied_posteriors_are_close_to_even_odds() -> None:
    result = beta_binomial_probability_of_improvement(50, 100, 50, 100)

    assert result.insufficient_data is False
    assert result.posterior_mean_a == result.posterior_mean_b
    assert result.mean_difference == 0.0
    assert math.isclose(result.probability_a_gt_b, 0.5, abs_tol=0.01)
    assert result.credible_interval_low < 0 < result.credible_interval_high


def test_beta_binomial_zero_n_reuses_insufficient_data_convention() -> None:
    result = beta_binomial_probability_of_improvement(0, 0, 50, 100)

    assert result == BayesianProportionComparison(
        posterior_mean_a=0.0,
        posterior_mean_b=0.0,
        mean_difference=0.0,
        probability_a_gt_b=0.0,
        credible_interval_low=0.0,
        credible_interval_high=0.0,
        credible_level=0.95,
        prior_alpha=1.0,
        prior_beta=1.0,
        posterior_alpha_a=0.0,
        posterior_beta_a=0.0,
        posterior_alpha_b=0.0,
        posterior_beta_b=0.0,
        sample_count=100_000,
        seed=179,
        n_a=0,
        n_b=100,
        insufficient_data=True,
        small_n=False,
    )


# ── compare_all_models: Benjamini-Hochberg FDR over all pairs and metrics ──


def test_compare_all_models_applies_hand_verified_bh_rejection_set() -> None:
    report_a = _report(
        "model-a", completions=100, attributed=80, ci_linked=100, ci_passed=80
    )
    report_b = _report(
        "model-b", completions=100, attributed=50, ci_linked=100, ci_passed=50
    )
    report_c = _report(
        "model-c", completions=100, attributed=60, ci_linked=100, ci_passed=60
    )

    comparison = compare_all_models([report_a, report_b, report_c])

    # Hand-verified BH reference:
    #   m = 2 * C(3, 2) = 6 tests
    #   AB p = 8.687711676946819e-06 for each metric
    #   AC p = 0.0020282311484520754 for each metric
    #   BC p = 0.1552184896846842 for each metric
    #   sorted thresholds at alpha=0.05:
    #     rank 1=0.008333..., 2=0.016666..., 3=0.025,
    #     4=0.033333..., 5=0.041666..., 6=0.05
    #   largest passing rank is k=4, so AB and AC survive; BC does not.
    assert comparison.comparison_family_size == 6
    assert comparison.tested_family_size == 6
    assert math.isclose(
        comparison.comparisons[0].ci_pass_rate.p_value,
        8.687711676946819e-06,
        rel_tol=1e-6,
    )
    assert math.isclose(
        comparison.comparisons[1].ci_pass_rate.p_value,
        0.0020282311484520754,
        rel_tol=1e-12,
    )
    assert math.isclose(
        comparison.comparisons[2].ci_pass_rate.p_value,
        0.1552184896846842,
        rel_tol=1e-12,
    )
    expected_rejections = {
        ("model-a", "model-b", "ci_pass_rate"),
        ("model-a", "model-b", "attribution_rate"),
        ("model-a", "model-c", "ci_pass_rate"),
        ("model-a", "model-c", "attribution_rate"),
    }
    actual_rejections = {
        (pair.model_a, pair.model_b, metric)
        for pair in comparison.comparisons
        for metric, pc in (
            ("ci_pass_rate", pair.ci_pass_rate),
            ("attribution_rate", pair.attribution_rate),
        )
        if pc.significant_after_benjamini_hochberg
    }
    assert actual_rejections == expected_rejections
    for pair in comparison.comparisons:
        for pc in (pair.ci_pass_rate, pair.attribution_rate):
            assert math.isclose(
                pc.benjamini_hochberg_alpha,
                4 / 6 * 0.05,
                rel_tol=1e-12,
            )


def test_compare_all_models_gets_more_conservative_as_family_grows() -> None:
    report_a = _report(
        "model-a", completions=100, attributed=33, ci_linked=100, ci_passed=33
    )
    report_b = _report(
        "model-b", completions=100, attributed=20, ci_linked=100, ci_passed=20
    )
    extra_reports = [
        _report(
            f"model-{name}",
            completions=100,
            attributed=20,
            ci_linked=100,
            ci_passed=20,
        )
        for name in ("c", "d", "e")
    ]

    two_model = compare_all_models([report_a, report_b])
    five_model = compare_all_models([report_a, report_b, *extra_reports])

    assert two_model.comparison_family_size == 2
    assert five_model.comparison_family_size == 20
    assert math.isclose(
        two_model.comparisons[0].ci_pass_rate.p_value,
        five_model.comparisons[0].ci_pass_rate.p_value,
        rel_tol=1e-12,
    )
    assert two_model.comparisons[0].ci_pass_rate.significant_after_benjamini_hochberg
    assert two_model.comparisons[
        0
    ].attribution_rate.significant_after_benjamini_hochberg
    assert (
        five_model.comparisons[0].ci_pass_rate.significant_after_benjamini_hochberg
        is False
    )
    assert (
        five_model.comparisons[0].attribution_rate.significant_after_benjamini_hochberg
        is False
    )


def test_compare_all_models_n_equals_two_reuses_compare_models_numbers() -> None:
    report_a = _report(
        "model-a", completions=100, attributed=80, ci_linked=100, ci_passed=75
    )
    report_b = _report(
        "model-b", completions=100, attributed=50, ci_linked=100, ci_passed=55
    )

    direct = compare_models(report_a, report_b)
    [round_robin] = compare_all_models([report_a, report_b]).comparisons

    assert round_robin.model_a == direct.model_a
    assert round_robin.model_b == direct.model_b
    assert round_robin.comparison_family_size == direct.comparison_family_size
    assert round_robin.bonferroni_alpha == direct.bonferroni_alpha
    assert math.isclose(
        round_robin.ci_pass_rate.p_value,
        direct.ci_pass_rate.p_value,
        rel_tol=1e-12,
    )
    assert math.isclose(
        round_robin.attribution_rate.p_value,
        direct.attribution_rate.p_value,
        rel_tol=1e-12,
    )


def test_compare_all_models_stamps_shared_alpha_on_sufficient_and_zero_on_insufficient() -> (
    None
):
    # model-a is listed first with ci_linked=0 -> both ci_pass_rate slots that
    # involve it are insufficient data; model-b/model-c have full data and
    # produce BH rejections on the attribution_rate slots. This is the
    # invariant the report's `_bh_threshold_for_display` helper relies on:
    # every sufficient-data slot carries the same global bh_alpha, and every
    # insufficient-data slot carries a 0.0 placeholder (never None).
    a = _report("model-a", completions=100, attributed=80, ci_linked=0, ci_passed=0)
    b = _report("model-b", completions=100, attributed=50, ci_linked=100, ci_passed=50)
    c = _report("model-c", completions=100, attributed=60, ci_linked=100, ci_passed=60)
    comparison = compare_all_models([a, b, c])

    assert comparison.tested_family_size == 4

    sufficient_alphas: set[float] = set()
    for pair in comparison.comparisons:
        for pc in (pair.ci_pass_rate, pair.attribution_rate):
            if pc.insufficient_data:
                assert pc.benjamini_hochberg_alpha == 0.0
                assert pc.significant_after_benjamini_hochberg is False
            else:
                sufficient_alphas.add(pc.benjamini_hochberg_alpha)

    # One global BH cutoff shared by every sufficient slot: rejected_count=2,
    # m=4, alpha=0.05 -> 2/4 * 0.05 == 0.025.
    assert sufficient_alphas == {0.025}


# ── effect-decay diagnostic: prefix Cohen's h trajectory ───────────────────


def test_effect_decay_flags_mixed_distribution_shrinking_toward_zero() -> None:
    # First 25%: a large early gap, A all-pass and B all-fail.
    # Remaining 75%: no effect, both arms have the same 30/60 pass rate.
    outcomes_a = [True] * 20 + [True] * 30 + [False] * 30
    outcomes_b = [False] * 20 + [True] * 30 + [False] * 30

    report = effect_decay_check(outcomes_a, outcomes_b)

    assert report.decay_detected is True
    assert report.decay_trend.status == TrendStatus.SIGNIFICANT_DECREASE
    assert report.start_abs_effect > report.end_abs_effect
    assert [checkpoint.n_a for checkpoint in report.checkpoints] == [
        20,
        40,
        60,
        80,
    ]
    assert [checkpoint.n_b for checkpoint in report.checkpoints] == [
        20,
        40,
        60,
        80,
    ]
    assert [round(checkpoint.abs_cohens_h, 3) for checkpoint in report.checkpoints] == [
        3.142,
        1.571,
        0.73,
        0.505,
    ]


def test_effect_decay_does_not_flag_constant_real_effect() -> None:
    # Every block of 20 preserves the same real effect: A=15/20, B=10/20.
    # Each default checkpoint lands on a block boundary, so h is flat.
    outcomes_a = ([True] * 15 + [False] * 5) * 4
    outcomes_b = ([True] * 10 + [False] * 10) * 4

    report = effect_decay_check(outcomes_a, outcomes_b)

    assert report.decay_detected is False
    assert report.decay_trend.status == TrendStatus.NO_SIGNIFICANT_TREND
    assert math.isclose(report.total_abs_drop, 0.0, abs_tol=1e-12)
    assert {round(checkpoint.cohens_h, 12) for checkpoint in report.checkpoints} == {
        round(cohens_h(0.75, 0.5), 12)
    }


def test_effect_decay_tiny_samples_repeat_prefixes_without_crashing() -> None:
    report = effect_decay_check([True, False], [False])

    assert report.decay_detected is False
    assert report.small_n is True
    assert report.insufficient_data is False
    assert [(c.fraction, c.n_a, c.n_b) for c in report.checkpoints] == [
        (0.25, 1, 1),
        (0.5, 1, 1),
        (0.75, 2, 1),
        (1.0, 2, 1),
    ]


def test_effect_decay_checkpoint_h_matches_direct_cohens_h_calls() -> None:
    outcomes_a = [True, True, False, True, False, False, True, True]
    outcomes_b = [False, True, False, False, True, False, False, True]

    report = effect_decay_check(outcomes_a, outcomes_b)

    for checkpoint in report.checkpoints:
        expected = cohens_h(
            sum(outcomes_a[: checkpoint.n_a]) / checkpoint.n_a,
            sum(outcomes_b[: checkpoint.n_b]) / checkpoint.n_b,
        )
        assert checkpoint.cohens_h == expected
        assert checkpoint.cohens_h == checkpoint.comparison.cohens_h


def test_effect_decay_false_positive_rate_is_controlled_for_constant_real_effect() -> (
    None
):
    # The superseded fixed-tolerance design flagged decay_detected on
    # 26-30% of trials with a perfectly constant real effect (p_a=0.75,
    # p_b=0.5) -- nowhere near the intended "a real effect must not be
    # reported as decaying" guarantee. Rerun that same simulation shape
    # against the Mann-Kendall-over-disjoint-blocks design: the
    # false-positive rate must land near the nominal alpha
    # (0.05), not at the old 26-30%.
    rng = random.Random(207)
    trials = 500
    false_positives = 0
    for _ in range(trials):
        outcomes_a = [rng.random() < 0.75 for _ in range(100)]
        outcomes_b = [rng.random() < 0.5 for _ in range(100)]
        rng.shuffle(outcomes_a)
        rng.shuffle(outcomes_b)
        if effect_decay_check(outcomes_a, outcomes_b).decay_detected:
            false_positives += 1

    assert false_positives / trials < 0.10


def test_effect_decay_trend_blocks_can_be_overridden() -> None:
    # A smaller trend_blocks count still runs (fewer, larger disjoint
    # blocks) and is threaded through to decay_trend.sample_size.
    outcomes_a = [True] * 20 + [True] * 30 + [False] * 30
    outcomes_b = [False] * 20 + [True] * 30 + [False] * 30

    report = effect_decay_check(outcomes_a, outcomes_b, trend_blocks=4)

    assert report.decay_trend.sample_size == 4


# ── expected_regret: normal partial expectation over apparent-best arm ─────


def test_expected_regret_tie_is_larger_with_tiny_n_than_large_n() -> None:
    large_n = expected_regret(
        {"model-a": (5_000, 10_000), "model-b": (5_000, 10_000)},
        "attribution_rate",
    )
    tiny_n = expected_regret(
        {"model-a": (1, 2), "model-b": (1, 2)},
        "attribution_rate",
    )

    [large_alt] = large_n.alternatives
    [tiny_alt] = tiny_n.alternatives

    assert large_n.apparent_best_model == "model-a"
    assert tiny_n.apparent_best_model == "model-a"
    assert tiny_alt.expected_regret > large_alt.expected_regret
    assert tiny_alt.small_n is True
    assert large_alt.small_n is False


def test_expected_regret_dramatic_tight_gap_is_near_zero() -> None:
    result = expected_regret(
        {"model-a": (900, 1_000), "model-b": (100, 1_000)},
        "ci_pass_rate",
    )

    [alternative] = result.alternatives

    assert result.apparent_best_model == "model-a"
    assert alternative.model == "model-b"
    assert alternative.expected_regret < 1e-300


def test_expected_regret_matches_hand_verified_partial_expectation_reference() -> None:
    result = expected_regret(
        {"model-a": (50, 100), "model-b": (50, 100)},
        "attribution_rate",
    )

    [alternative] = result.alternatives

    # Hand-verified closed-form reference for D = p_b - p_a, using the
    # Agresti-Coull "plus-four" smoothed rate inside the SE only
    # (mu/rate_difference still use the raw observed rate, which is 0 here
    # since both arms tie at 50/100):
    #   p_tilde = (50 + 2) / (100 + 4) = 52/104 = 0.5
    #   sigma = sqrt(0.5*0.5/104 + 0.5*0.5/104) = 0.06933752452815364
    #   mu = 0
    #   E[max(0, D)] = sigma * phi(0) + 0 * Phi(0)
    #                 = 0.06933752452815364 * 0.3989422804014327
    #                 = 0.027661670152651887
    assert math.isclose(
        alternative.expected_regret,
        0.027661670152651887,
        rel_tol=1e-12,
    )
    assert math.isclose(
        alternative.standard_error,
        0.06933752452815364,
        rel_tol=1e-12,
    )


def test_expected_regret_boundary_rate_is_not_forced_to_exactly_zero() -> None:
    # A small all-pass/all-pass tie (raw rates identical at the p=1
    # boundary) used to force sigma=0 in the raw Wald formula, printing
    # expected_regret=0.0 -- the opposite of the intended "identical rates,
    # tiny n -> regret should be larger".
    # The plus-four-smoothed SE keeps sigma, and therefore regret, a real
    # positive number even though both raw observed rates are exactly 1.0.
    result = expected_regret(
        {"model-a": (5, 5), "model-b": (5, 5)},
        "ci_pass_rate",
    )

    [alternative] = result.alternatives

    assert alternative.observed_rate == 1.0
    assert alternative.rate_difference == 0.0
    assert alternative.standard_error > 0.0
    assert alternative.expected_regret > 0.0


def test_expected_regret_reports_zero_n_alternative_as_insufficient() -> None:
    result = expected_regret(
        {"model-a": (50, 100), "missing-model": (0, 0)},
        "attribution_rate",
    )

    [alternative] = result.alternatives

    assert result.insufficient_data is False
    assert result.apparent_best_model == "model-a"
    assert alternative.model == "missing-model"
    assert alternative.insufficient_data is True
    assert alternative.expected_regret == 0.0

# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI coverage for the illustrative attribution precision harness fixture."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import sediment_derive.precision_harness as precision_harness_module
from sediment_derive.precision_harness import (
    LabeledCase,
    evaluate_scorer,
    load_labelled_cases,
    plan_labelled_corpus_size,
    required_labelled_examples_for_proportion_margin,
    sweep_thresholds,
    threshold_drift_report,
)
from sediment_derive.scoring import JaccardScorer


FIXTURE = (
    Path(__file__).parent / "fixtures" / "precision_harness" / "illustrative_cases.json"
)


def test_labeled_case_replaces_british_spelling_in_public_api() -> None:
    assert hasattr(precision_harness_module, "LabeledCase")
    assert not hasattr(precision_harness_module, "LabelledCase")


class AlwaysPositiveScorer:
    version = "always-positive"

    def score(self, completion_tokens: set[str], diff_tokens: set[str]) -> float:
        return 1.0


class AlwaysZeroScorer:
    version = "always-zero"

    def score(self, completion_tokens: set[str], diff_tokens: set[str]) -> float:
        return 0.0


def test_jaccard_baseline_on_illustrative_labelled_set() -> None:
    # This synthetic fixture set is only an executable harness example, not a
    # real-world performance claim.
    cases = load_labelled_cases(FIXTURE)

    result = evaluate_scorer(JaccardScorer(), cases, threshold=0.7)

    assert result.scorer_version == "jaccard-v1"
    assert result.threshold == 0.7
    assert result.true_positives == 2
    assert result.false_positives == 1
    assert result.false_negatives == 1
    assert result.true_negatives == 1
    assert result.precision == pytest.approx(2 / 3)
    assert result.precision_ci == pytest.approx(
        (0.20765960080204776, 0.9385080552796037)
    )
    assert result.recall == pytest.approx(2 / 3)
    assert result.recall_ci == pytest.approx((0.20765960080204776, 0.9385080552796037))


def test_precision_interval_matches_hand_checked_reference_case() -> None:
    cases = [
        LabeledCase(
            completion_text=f"completion {index}",
            diff_added_lines=f"diff {index}",
            should_match=index < 8,
        )
        for index in range(10)
    ]

    result = evaluate_scorer(AlwaysPositiveScorer(), cases, threshold=0.7)

    assert result.true_positives == 8
    assert result.false_positives == 2
    assert result.precision == 0.8
    assert math.isclose(result.precision_ci[0], 0.4901624715366419, rel_tol=1e-9)
    assert math.isclose(result.precision_ci[1], 0.9433178485456246, rel_tol=1e-9)


def test_precision_interval_zero_denominator_returns_documented_sentinel() -> None:
    cases = [
        LabeledCase(
            completion_text="matching completion",
            diff_added_lines="matching diff",
            should_match=True,
        ),
        LabeledCase(
            completion_text="nonmatch completion",
            diff_added_lines="nonmatch diff",
            should_match=False,
        ),
    ]

    result = evaluate_scorer(AlwaysZeroScorer(), cases, threshold=0.7)

    assert result.true_positives == 0
    assert result.false_positives == 0
    assert result.precision == 0.0
    assert result.precision_ci == (0.0, 0.0)


def test_threshold_sweep_shows_precision_recall_tradeoff() -> None:
    # The fixture is intentionally tiny, so assert the broad tradeoff shape
    # instead of treating every adjacent precision point as a recommendation.
    cases = load_labelled_cases(FIXTURE)
    thresholds = [round(i / 10, 1) for i in range(1, 10)]

    results = sweep_thresholds(JaccardScorer(), cases, thresholds)

    assert [result.threshold for result in results] == thresholds
    assert all(result.scorer_version == "jaccard-v1" for result in results)
    assert all(lower <= upper for lower, upper in (r.precision_ci for r in results))
    assert all(lower <= upper for lower, upper in (r.recall_ci for r in results))
    assert all(left.recall >= right.recall for left, right in zip(results, results[1:]))
    assert results[-1].precision > results[0].precision
    assert results[-1].recall < results[0].recall


def test_corpus_sizing_reference_case_for_assumed_rate() -> None:
    # Hand check: z=1.9599639845, z^2 * 0.8 * 0.2 / 0.1^2 = 61.463..., ceil=62.
    assert (
        required_labelled_examples_for_proportion_margin(
            assumed_rate=0.8,
            target_margin=0.1,
        )
        == 62
    )


def test_corpus_sizing_reports_worst_case_alongside_assumption() -> None:
    estimate = plan_labelled_corpus_size(target_margin=0.1, assumed_rate=0.8)

    assert estimate.assumed_rate == 0.8
    assert estimate.assumed_rate_required_n == 62
    assert estimate.worst_case_rate == 0.5
    assert estimate.worst_case_required_n == 97


def test_worst_case_rate_is_monotonic_upper_bound_for_planning() -> None:
    margin = 0.1
    worst_case_n = required_labelled_examples_for_proportion_margin(0.5, margin)
    other_rates = [0.7, 0.9]

    assert all(
        worst_case_n >= required_labelled_examples_for_proportion_margin(rate, margin)
        for rate in other_rates
    )


def test_corpus_sizing_rejects_non_positive_margin() -> None:
    with pytest.raises(ValueError, match="target_margin must be positive"):
        required_labelled_examples_for_proportion_margin(0.8, 0)

    with pytest.raises(ValueError, match="target_margin must be positive"):
        required_labelled_examples_for_proportion_margin(0.8, -0.1)


def test_threshold_drift_not_flagged_when_historical_threshold_is_still_optimal() -> (
    None
):
    cases = [
        _case_with_score("tp_high", shared=9, completion_unique=1, should_match=True),
        _case_with_score("tp_mid", shared=4, completion_unique=1, should_match=True),
        _case_with_score("fp_low", shared=3, completion_unique=2, should_match=False),
        _case_with_score("tn_low", shared=2, completion_unique=3, should_match=False),
    ]

    report = threshold_drift_report(0.7, cases, min_cases=2)

    assert report.status == "ok"
    assert report.material is False
    assert report.optimal_threshold == 0.7
    assert report.historical_result is not None
    assert report.optimal_result is not None
    assert report.historical_result.f1 == pytest.approx(1.0)
    assert report.optimal_result.f1 == pytest.approx(1.0)
    assert report.threshold_delta == pytest.approx(0.0)
    assert report.f1_delta == pytest.approx(0.0)
    # Historical and optimal are the identical result here, so neither
    # metric can be outside the optimal's own CI (guards the
    # _CI_BOUNDARY_TOLERANCE floating-point edge case).
    assert report.precision_outside_ci is False
    assert report.recall_outside_ci is False


def test_threshold_drift_flagged_when_optimal_threshold_moves_lower() -> None:
    cases = [
        _case_with_score("tp_one", shared=11, completion_unique=9, should_match=True),
        _case_with_score("tp_two", shared=11, completion_unique=9, should_match=True),
        _case_with_score("fp_one", shared=1, completion_unique=1, should_match=False),
        _case_with_score("fp_two", shared=1, completion_unique=1, should_match=False),
    ]

    report = threshold_drift_report(0.7, cases, min_cases=2)

    assert report.status == "ok"
    assert report.material is True
    assert report.optimal_threshold == 0.55
    assert report.historical_result is not None
    assert report.optimal_result is not None
    assert report.historical_result.f1 == pytest.approx(0.0)
    assert report.optimal_result.f1 == pytest.approx(1.0)
    assert report.threshold_delta == pytest.approx(0.15)
    assert report.f1_delta == pytest.approx(1.0)
    # A total miss (f1=0.0) vs a perfect optimum (f1=1.0) is trivially
    # outside any CI, which is what makes this material.
    assert report.precision_outside_ci is True
    assert report.recall_outside_ci is True


def test_threshold_drift_reports_insufficient_data_without_drift_verdict() -> None:
    report = threshold_drift_report(0.7, [])

    assert report.status == "insufficient_data"
    assert report.reason == "too_few_cases"
    assert report.material is False
    assert report.optimal_threshold is None
    assert report.historical_result is None
    assert report.optimal_result is None


def test_threshold_drift_reports_no_positive_cases() -> None:
    cases = [
        _case_with_score(f"n{i}", shared=2, completion_unique=8, should_match=False)
        for i in range(3)
    ]

    report = threshold_drift_report(0.7, cases, min_cases=2)

    assert report.status == "insufficient_data"
    assert report.reason == "no_positive_cases"
    assert report.material is False
    assert report.optimal_threshold is None


def test_threshold_drift_reports_no_negative_cases() -> None:
    cases = [
        _case_with_score(f"p{i}", shared=8, completion_unique=2, should_match=True)
        for i in range(3)
    ]

    report = threshold_drift_report(0.7, cases, min_cases=2)

    assert report.status == "insufficient_data"
    assert report.reason == "no_negative_cases"
    assert report.material is False
    assert report.optimal_threshold is None


def test_threshold_drift_default_min_cases_uses_corpus_sizing_floor() -> None:
    # MIN_DRIFT_CASES is derived from the corpus-sizing planner (currently
    # 97), not a hand-picked literal -- a handful of cases is not enough
    # signal to trust a Wilson-CI comparison, so the default gate must still
    # say so.
    cases = [
        _case_with_score(f"p{i}", shared=8, completion_unique=2, should_match=True)
        for i in range(5)
    ] + [
        _case_with_score(f"n{i}", shared=2, completion_unique=8, should_match=False)
        for i in range(5)
    ]

    report = threshold_drift_report(0.7, cases)

    assert report.status == "insufficient_data"
    assert report.reason == "too_few_cases"


def test_threshold_drift_not_material_when_f1_gap_is_within_sampling_noise() -> None:
    # A fixed 0.01 F1-drop literal fires on sampling noise at realistic corpus
    # sizes. Here the optimal threshold (0.66) beats the historical threshold
    # (0.7) by ~2.6 F1 points -- more than that fixed drop, so drop-based logic
    # would flag this as material -- but the historical result's
    # precision/recall both sit comfortably inside the optimal result's own
    # Wilson interval, so it is not statistically distinguishable from noise at
    # this sample size.
    positives = [
        # Three borderline positives score just under the 0.7 historical
        # threshold (2/3 ~= 0.667) but clear the lower optimal threshold.
        _case_with_score(f"p_low{i}", shared=2, completion_unique=1, should_match=True)
        for i in range(3)
    ] + [
        _case_with_score(f"p_high{i}", shared=8, completion_unique=2, should_match=True)
        for i in range(57)
    ]
    negatives = [
        _case_with_score(f"n{i}", shared=2, completion_unique=8, should_match=False)
        for i in range(60)
    ]

    report = threshold_drift_report(0.7, positives + negatives, min_cases=2)

    assert report.status == "ok"
    assert report.threshold_delta < 0.05
    assert report.f1_delta > 0.01  # would have tripped the old fixed-drop gate
    assert report.precision_outside_ci is False
    assert report.recall_outside_ci is False
    assert report.material is False


def test_threshold_drift_fixture_threshold_delta_alone_is_not_material() -> None:
    # On the bundled 5-case fixture with the floor overridden, the argmax
    # lands far from the historical threshold purely by sampling luck. The
    # removed `threshold_delta > 0.05` OR-arm turned that into material=true
    # with both CI arms false — a false alarm. Materiality now requires CI
    # evidence.
    report = threshold_drift_report(0.7, load_labelled_cases(FIXTURE), min_cases=5)

    assert report.status == "ok"
    assert report.threshold_delta > 0.05  # would have tripped the removed arm
    assert report.precision_outside_ci is False
    assert report.recall_outside_ci is False
    assert report.material is False


def _case_with_score(
    prefix: str,
    *,
    shared: int,
    completion_unique: int,
    should_match: bool,
) -> LabeledCase:
    shared_tokens = [f"{prefix}_shared_{i}" for i in range(shared)]
    completion_tokens = shared_tokens + [
        f"{prefix}_completion_{i}" for i in range(completion_unique)
    ]
    return LabeledCase(
        completion_text=" ".join(completion_tokens),
        diff_added_lines=" ".join(shared_tokens),
        should_match=should_match,
    )

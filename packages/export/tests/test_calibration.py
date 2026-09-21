# SPDX-License-Identifier: AGPL-3.0-or-later
"""Calibration metric tests.

Reference cases are hand-computed in comments before the assertions, not
derived from the implementation under test.
"""

from __future__ import annotations

import math

import pytest

from sediment_export import (
    auroc,
    brier_score,
    bucket_inversions,
    expected_calibration_error,
    reliability_diagram_data,
    compare_calibration_policies,
)
from sediment_export.calibration import CalibrationRecord, stratify_calibration


def test_perfect_deterministic_calibration_has_zero_brier_and_zero_ece() -> None:
    pairs = [(0.0, False), (0.0, False), (1.0, True), (1.0, True)]

    # Hand-computed Brier:
    #   ((0 - 0)^2 + (0 - 0)^2 + (1 - 1)^2 + (1 - 1)^2) / 4 = 0.0
    assert brier_score(pairs) == 0.0
    assert expected_calibration_error(pairs, n_bins=2) == 0.0
    assert reliability_diagram_data(pairs, n_bins=2) == [
        (0.0, 0.0, 2),
        (1.0, 1.0, 2),
    ]


def test_overconfident_predictions_match_hand_computed_brier_and_ece() -> None:
    pairs = [(0.9, True)] * 5 + [(0.9, False)] * 5

    # Hand-computed Brier:
    #   true rows:  5 * (0.9 - 1)^2 = 5 * 0.01 = 0.05
    #   false rows: 5 * (0.9 - 0)^2 = 5 * 0.81 = 4.05
    #   mean: (0.05 + 4.05) / 10 = 0.41
    assert math.isclose(brier_score(pairs), 0.41, rel_tol=1e-12)

    # Hand-computed ECE:
    #   one non-empty bucket, mean confidence 0.9, empirical accuracy 5/10 = 0.5
    #   weighted contribution: (10/10) * abs(0.5 - 0.9) = 0.4
    assert math.isclose(
        expected_calibration_error(pairs, n_bins=10), 0.4, rel_tol=1e-12
    )
    [(mean_predicted, empirical_accuracy, count)] = reliability_diagram_data(
        pairs, n_bins=10
    )
    assert math.isclose(mean_predicted, 0.9, rel_tol=1e-12)
    assert empirical_accuracy == 0.5
    assert count == 10


def test_auroc_perfect_separation_is_one() -> None:
    pairs = [(0.8, True), (0.9, True), (0.1, False), (0.2, False)]

    # Hand-computed AUROC:
    #   positives beat every negative: 4 wins / 4 positive-negative pairs = 1.0
    assert auroc(pairs) == 1.0


def test_auroc_perfect_anti_separation_is_zero() -> None:
    pairs = [(0.1, True), (0.2, True), (0.8, False), (0.9, False)]

    # Hand-computed AUROC:
    #   positives beat no negatives: 0 wins / 4 positive-negative pairs = 0.0
    assert auroc(pairs) == 0.0


def test_auroc_no_discriminative_power_is_half() -> None:
    pairs = [(0.2, True), (0.8, True), (0.3, False), (0.7, False)]

    # Hand-computed AUROC:
    #   positive 0.2 beats 0 negatives; positive 0.8 beats both negatives.
    #   2 wins / 4 positive-negative pairs = 0.5
    assert auroc(pairs) == 0.5


def test_auroc_tied_predictions_get_half_credit() -> None:
    pairs = [(0.5, True), (0.5, True), (0.5, False), (0.5, False)]

    # Hand-computed AUROC:
    #   every positive-negative comparison ties.
    #   4 ties * 0.5 credit / 4 positive-negative pairs = 0.5
    assert auroc(pairs) == 0.5


def test_auroc_mixed_ties_match_hand_computed_value() -> None:
    pairs = [(0.5, True), (0.9, True), (0.5, False), (0.1, False)]

    # Hand-computed AUROC:
    #   positive 0.5 ties negative 0.5 and beats negative 0.1: 1.5
    #   positive 0.9 beats both negatives: 2.0
    #   (1.5 + 2.0) / 4 positive-negative pairs = 0.875
    assert auroc(pairs) == 0.875


def test_auroc_without_both_classes_uses_documented_zero_sentinel() -> None:
    assert auroc([]) == 0.0
    assert auroc([(0.9, True)]) == 0.0
    assert auroc([(0.1, False)]) == 0.0


def test_bucket_inversions_report_higher_bucket_lower_empirical_rate() -> None:
    pairs = [(0.1, True), (0.5, True), (0.5, False), (0.9, False)]

    # Hand-computed bucket accuracies:
    #   bucket 1: 1/1 = 1.0
    #   bucket 5: 1/2 = 0.5
    #   bucket 9: 0/1 = 0.0
    # Pairwise higher-vs-lower inversions:
    #   5 < 1, 9 < 1, 9 < 5 -> 3 inversions.
    inversions = bucket_inversions(pairs, n_bins=10)
    assert len(inversions) == 3
    assert [
        (inversion.lower_bucket, inversion.higher_bucket) for inversion in inversions
    ] == [(1, 5), (1, 9), (5, 9)]
    assert inversions[0].lower_empirical_accuracy == 1.0
    assert inversions[0].higher_empirical_accuracy == 0.5
    assert inversions[0].lower_count == 1
    assert inversions[0].higher_count == 2


def test_bucket_inversions_empty_when_empirical_rates_are_monotonic() -> None:
    pairs = [(0.1, False), (0.5, False), (0.5, True), (0.9, True)]

    # Bucket accuracies in order are 0.0, 0.5, 1.0.
    assert bucket_inversions(pairs, n_bins=10) == []


def test_empty_input_uses_documented_zero_sentinel() -> None:
    assert brier_score([]) == 0.0
    assert expected_calibration_error([], n_bins=10) == 0.0
    assert reliability_diagram_data([], n_bins=10) == []


def test_all_identical_predictions_compute_without_divide_by_zero() -> None:
    pairs = [(0.5, True), (0.5, False), (0.5, True), (0.5, False)]

    # Hand-computed Brier:
    #   four rows each have squared error 0.25, so mean = 1.0 / 4 = 0.25
    assert brier_score(pairs) == 0.25

    # Same bucket has mean confidence 0.5 and empirical accuracy 2/4 = 0.5.
    assert expected_calibration_error(pairs, n_bins=10) == 0.0
    assert reliability_diagram_data(pairs, n_bins=10) == [(0.5, 0.5, 4)]


def test_empty_buckets_are_skipped_and_do_not_contribute_to_ece() -> None:
    pairs = [(0.1, False), (0.9, True)]

    # With ten bins, only [0.1, 0.2) and [0.9, 1.0) are non-empty.
    #   bucket 1: mean 0.1, accuracy 0.0 -> contribution (1/2) * 0.1 = 0.05
    #   bucket 9: mean 0.9, accuracy 1.0 -> contribution (1/2) * 0.1 = 0.05
    # Empty buckets are skipped; no zero-count division occurs.
    assert math.isclose(
        expected_calibration_error(pairs, n_bins=10), 0.1, rel_tol=1e-12
    )
    assert reliability_diagram_data(pairs, n_bins=10) == [
        (0.1, 0.0, 1),
        (0.9, 1.0, 1),
    ]


def test_predictions_outside_probability_range_are_rejected() -> None:
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        brier_score([(1.1, True)])


def test_non_positive_bin_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        expected_calibration_error([(0.5, True)], n_bins=0)


def test_calibration_comparison_reports_policy_v4_against_v3() -> None:
    current = [(0.0, False), (1.0, True)]
    policy_v3 = [(0.8, False), (1.0, True)]

    comparison = compare_calibration_policies(current, policy_v3, n_bins=2)

    assert comparison.current.policy_version == "4"
    assert comparison.prior.policy_version == "3"
    assert comparison.current.brier_score == 0.0
    assert comparison.prior.brier_score == pytest.approx(0.32)
    assert comparison.brier_score_delta == pytest.approx(-0.32)


def test_calibration_stratifies_recipe_and_source_without_pooling() -> None:
    rows = [
        CalibrationRecord(
            predicted_confidence=0.9,
            actual_outcome=True,
            recipe_id="sft_curated",
            recipe_version=1,
            eligibility_source="explicit_accept",
        ),
        CalibrationRecord(
            predicted_confidence=0.8,
            actual_outcome=False,
            recipe_id="sft_verified",
            recipe_version=1,
            eligibility_source="resolved_ci_pass",
        ),
    ]

    strata = stratify_calibration(rows, n_bins=2)

    assert [
        (
            row.recipe_id,
            row.recipe_version,
            row.label_source,
            row.eligibility_source,
            row.metrics.policy_version,
        )
        for row in strata
    ] == [
        ("sft_curated", 1, None, "explicit_accept", "4"),
        ("sft_verified", 1, None, "resolved_ci_pass", "4"),
    ]
    assert [row.metrics.brier_score for row in strata] == pytest.approx([0.01, 0.64])


def test_dpo_calibration_preserves_both_label_sources() -> None:
    [stratum] = stratify_calibration(
        [
            CalibrationRecord(
                predicted_confidence=0.9,
                actual_outcome=True,
                recipe_id="dpo_outcome",
                recipe_version=1,
                chosen_label_source="resolved_ci_pass",
                rejected_label_source="resolved_ci_fail",
            )
        ]
    )

    assert stratum.chosen_label_source == "resolved_ci_pass"
    assert stratum.rejected_label_source == "resolved_ci_fail"
    assert stratum.label_source is None
    assert stratum.eligibility_source is None

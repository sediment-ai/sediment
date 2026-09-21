# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cohen's kappa reference tests for inter-rater reliability.

The expected values below are hand-computed from the textbook formula:
``kappa = (p_o - p_e) / (1 - p_e)``. They intentionally cover perfect
agreement, agreement exactly at chance, a partial-agreement case with
explicit marginals, and zero-variance rater labels.
"""

from __future__ import annotations

import math

import pytest

from sediment_export import cohens_kappa, percent_agreement


def test_perfect_agreement_yields_kappa_one() -> None:
    result = cohens_kappa(
        ["accept", "reject", "accept"], ["accept", "reject", "accept"]
    )

    assert result.n_items == 3
    assert result.observed_agreement == 1.0
    # Marginals: both raters use accept 2/3 and reject 1/3.
    # p_e = (2/3 * 2/3) + (1/3 * 1/3) = 5/9.
    assert math.isclose(result.expected_agreement, 5 / 9, rel_tol=1e-12)
    assert result.kappa == 1.0
    assert result.degenerate is False


def test_agreement_exactly_at_chance_yields_kappa_zero() -> None:
    result = cohens_kappa(
        ["yes", "yes", "no", "no"],
        ["yes", "no", "yes", "no"],
    )

    assert result.observed_agreement == 0.5
    # Both raters use yes 2/4 and no 2/4, so p_e = 0.5.
    assert result.expected_agreement == 0.5
    assert result.kappa == 0.0


def test_partial_agreement_matches_hand_computed_expected_kappa() -> None:
    result = cohens_kappa(
        ["yes", "yes", "yes", "yes", "yes", "no", "no", "no", "no", "no"],
        ["yes", "yes", "yes", "yes", "no", "yes", "no", "no", "no", "no"],
    )

    # Confusion matrix:
    #   yes/yes = 4, yes/no = 1, no/yes = 1, no/no = 4
    # Observed agreement = (4 + 4) / 10 = 0.8.
    # Both marginal distributions are yes 5/10 and no 5/10:
    #   p_e = (0.5 * 0.5) + (0.5 * 0.5) = 0.5
    #   kappa = (0.8 - 0.5) / (1 - 0.5) = 0.6
    assert result.observed_agreement == 0.8
    assert result.expected_agreement == 0.5
    assert math.isclose(result.kappa, 0.6, rel_tol=1e-12)
    assert result.degenerate is False


def test_zero_variance_one_rater_does_not_divide_by_zero() -> None:
    result = cohens_kappa(
        ["yes", "yes", "yes", "yes"],
        ["yes", "no", "yes", "no"],
    )

    # Rater A is all yes; rater B is yes 2/4 and no 2/4.
    # Observed agreement = 2/4 = 0.5.
    # Expected agreement = (1.0 * 0.5) + (0.0 * 0.5) = 0.5.
    # kappa = (0.5 - 0.5) / (1 - 0.5) = 0.0.
    assert result.observed_agreement == 0.5
    assert result.expected_agreement == 0.5
    assert result.kappa == 0.0
    assert result.degenerate is False


def test_both_raters_zero_variance_same_class_is_perfect_degenerate() -> None:
    result = cohens_kappa(["yes", "yes", "yes"], ["yes", "yes", "yes"])

    assert result.observed_agreement == 1.0
    assert result.expected_agreement == 1.0
    assert result.kappa == 1.0
    assert result.degenerate is True


def test_empty_input_returns_no_data_sentinel() -> None:
    result = cohens_kappa([], [])

    assert result.n_items == 0
    assert result.observed_agreement == 0.0
    assert result.expected_agreement == 0.0
    assert result.kappa == 0.0
    assert result.degenerate is True


def test_percent_agreement_is_raw_agreement_wrapper() -> None:
    assert percent_agreement(["a", "b", "c"], ["a", "x", "c"]) == 2 / 3


def test_mismatched_lengths_raise() -> None:
    with pytest.raises(ValueError, match="same length"):
        cohens_kappa(["yes"], ["yes", "no"])

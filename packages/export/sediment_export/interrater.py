# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inter-rater agreement metrics for future multi-labeler review.

This module is deliberately standalone: it consumes two parallel lists of
binary or categorical judgments over the same items, with no dependency on
the label-confidence-inspection CLI that will eventually produce human judgment
columns. It reports both raw percent agreement and Cohen's kappa because
kappa alone can be misleading with skewed class distributions: high raw
agreement can still produce a low kappa when chance agreement is high
(Cohen's kappa paradox).

Degenerate convention: when both raters assign one identical class to every
item, chance agreement is mathematically 1.0 and the usual denominator
``1 - p_e`` is zero. Because the observed agreement is also perfect, this
module returns ``kappa=1.0`` and flags ``degenerate=True`` rather than
raising or emitting NaN. Empty input returns all scalar metrics as ``0.0``
with ``degenerate=True`` to match the export package's no-data-is-valid
operator-output convention.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TypeAlias

Label: TypeAlias = str | int | bool


@dataclass(frozen=True)
class InterRaterAgreement:
    """Cohen's kappa plus raw percent agreement for two raters.

    ``observed_agreement`` is raw percent agreement as a fraction in ``[0, 1]``.
    ``expected_agreement`` is the chance agreement implied by each rater's
    marginal label distribution. ``kappa`` is
    ``(observed_agreement - expected_agreement) / (1 - expected_agreement)``,
    except for documented degenerate cases where the denominator would be
    zero.
    """

    n_items: int
    observed_agreement: float
    expected_agreement: float
    kappa: float
    degenerate: bool


def cohens_kappa(rater_a: list[Label], rater_b: list[Label]) -> InterRaterAgreement:
    """Return raw agreement and Cohen's kappa for two parallel label lists."""
    _validate_parallel_labels(rater_a, rater_b)

    n_items = len(rater_a)
    if n_items == 0:
        return InterRaterAgreement(
            n_items=0,
            observed_agreement=0.0,
            expected_agreement=0.0,
            kappa=0.0,
            degenerate=True,
        )

    observed = sum(a == b for a, b in zip(rater_a, rater_b, strict=True)) / n_items
    expected = _expected_agreement(rater_a, rater_b)
    denominator = 1.0 - expected
    if denominator == 0.0:
        return InterRaterAgreement(
            n_items=n_items,
            observed_agreement=observed,
            expected_agreement=expected,
            kappa=1.0 if observed == 1.0 else 0.0,
            degenerate=True,
        )

    return InterRaterAgreement(
        n_items=n_items,
        observed_agreement=observed,
        expected_agreement=expected,
        kappa=(observed - expected) / denominator,
        degenerate=False,
    )


def percent_agreement(rater_a: list[Label], rater_b: list[Label]) -> float:
    """Return raw percent agreement as a fraction in ``[0, 1]``."""
    return cohens_kappa(rater_a, rater_b).observed_agreement


def _expected_agreement(rater_a: list[Label], rater_b: list[Label]) -> float:
    counts_a = Counter(rater_a)
    counts_b = Counter(rater_b)
    n_items = len(rater_a)
    labels = counts_a.keys() | counts_b.keys()
    return sum(
        (counts_a[label] / n_items) * (counts_b[label] / n_items) for label in labels
    )


def _validate_parallel_labels(rater_a: list[Label], rater_b: list[Label]) -> None:
    if len(rater_a) != len(rater_b):
        raise ValueError("rater label lists must have the same length")

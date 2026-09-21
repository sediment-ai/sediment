# SPDX-License-Identifier: AGPL-3.0-or-later
"""Calibration and discrimination metrics for confidence labels.

The scalar metric functions consume plain
``(predicted_confidence, actual_outcome)`` pairs, where
``predicted_confidence`` is the probability-like confidence that
``resolve_confidence``/``resolve_confidence_breakdown`` assigns to a attributed completion
and ``actual_outcome`` is the future human-judgment label from reward
inspection's currently-empty review column: ``True`` when a human judges the
confidence as right/trustworthy for that row, ``False`` otherwise.
``stratify_calibration`` instead requires recipe and source metadata on every
record. It never pools DPO label-source pairs, single label sources, or SFT
eligibility sources.

Empty input returns ``0.0`` for scalar metrics and an empty reliability data
list. That sentinel matches the rest of the export package's "no data is a
valid answer" convention and avoids NaN in operator-facing output.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

type CalibrationPair = tuple[float, bool]
type ReliabilityBucket = tuple[float, float, int]
EvidenceSource = Literal[
    "explicit_accept",
    "explicit_reject",
    "edit_retention",
    "resolved_ci_pass",
    "resolved_ci_fail",
    "abandonment",
]

_EVIDENCE_SOURCES = {
    "explicit_accept",
    "explicit_reject",
    "edit_retention",
    "resolved_ci_pass",
    "resolved_ci_fail",
    "abandonment",
}


@dataclass(frozen=True)
class CalibrationRecord:
    """One human judgment with the recipe stratum that produced its label."""

    predicted_confidence: float
    actual_outcome: bool
    recipe_id: str
    recipe_version: int
    chosen_label_source: EvidenceSource | None = None
    rejected_label_source: EvidenceSource | None = None
    label_source: EvidenceSource | None = None
    eligibility_source: EvidenceSource | None = None

    def __post_init__(self) -> None:
        if self.recipe_version < 1:
            raise ValueError("recipe_version must be a positive integer")
        pair_supplied = (
            self.chosen_label_source is not None
            and self.rejected_label_source is not None
        )
        pair_partial = (self.chosen_label_source is None) != (
            self.rejected_label_source is None
        )
        if pair_partial:
            raise ValueError(
                "chosen_label_source and rejected_label_source must be supplied together"
            )
        source_modes = (
            pair_supplied,
            self.label_source is not None,
            self.eligibility_source is not None,
        )
        if sum(source_modes) != 1:
            raise ValueError(
                "exactly one label-source pair, label_source, or "
                "eligibility_source is required"
            )
        for source in (
            self.chosen_label_source,
            self.rejected_label_source,
            self.label_source,
            self.eligibility_source,
        ):
            if source is not None and source not in _EVIDENCE_SOURCES:
                raise ValueError(f"unsupported evidence source {source!r}")


@dataclass(frozen=True)
class CalibrationStratum:
    """Calibration results for one recipe, version, and closed source."""

    recipe_id: str
    recipe_version: int
    chosen_label_source: EvidenceSource | None
    rejected_label_source: EvidenceSource | None
    label_source: EvidenceSource | None
    eligibility_source: EvidenceSource | None
    metrics: CalibrationMetrics
    reliability_buckets: list[ReliabilityBucket]
    bucket_inversions: list[BucketInversion]


@dataclass(frozen=True)
class BucketInversion:
    """A higher-confidence bucket with lower empirical accuracy."""

    lower_bucket: int
    higher_bucket: int
    lower_empirical_accuracy: float
    higher_empirical_accuracy: float
    lower_count: int
    higher_count: int


@dataclass(frozen=True)
class CalibrationMetrics:
    """Calibration metrics stamped with the label-confidence policy version."""

    policy_version: str
    brier_score: float
    expected_calibration_error: float
    auroc: float


@dataclass(frozen=True)
class CalibrationPolicyComparison:
    """Policy-version-4 calibration beside the prior version-3 baseline."""

    current: CalibrationMetrics
    prior: CalibrationMetrics
    brier_score_delta: float
    expected_calibration_error_delta: float
    auroc_delta: float


def stratify_calibration(
    records: list[CalibrationRecord], n_bins: int = 10
) -> list[CalibrationStratum]:
    """Compute metrics separately for every recipe and evidence source."""

    grouped: dict[
        tuple[
            str,
            int,
            EvidenceSource | None,
            EvidenceSource | None,
            EvidenceSource | None,
            EvidenceSource | None,
        ],
        list[CalibrationPair],
    ] = defaultdict(list)
    for record in records:
        key = (
            record.recipe_id,
            record.recipe_version,
            record.chosen_label_source,
            record.rejected_label_source,
            record.label_source,
            record.eligibility_source,
        )
        grouped[key].append((record.predicted_confidence, record.actual_outcome))

    rows: list[CalibrationStratum] = []
    for (
        recipe_id,
        recipe_version,
        chosen_label_source,
        rejected_label_source,
        label_source,
        eligibility_source,
    ), pairs in sorted(
        grouped.items(),
        key=lambda item: (
            item[0][0],
            item[0][1],
            item[0][2] or "",
            item[0][3] or "",
            item[0][4] or "",
            item[0][5] or "",
        ),
    ):
        metrics = CalibrationMetrics(
            policy_version="4",
            brier_score=brier_score(pairs),
            expected_calibration_error=expected_calibration_error(pairs, n_bins=n_bins),
            auroc=auroc(pairs),
        )
        rows.append(
            CalibrationStratum(
                recipe_id=recipe_id,
                recipe_version=recipe_version,
                chosen_label_source=chosen_label_source,
                rejected_label_source=rejected_label_source,
                label_source=label_source,
                eligibility_source=eligibility_source,
                metrics=metrics,
                reliability_buckets=reliability_diagram_data(pairs, n_bins=n_bins),
                bucket_inversions=bucket_inversions(pairs, n_bins=n_bins),
            )
        )
    return rows


def compare_calibration_policies(
    current_pairs: list[CalibrationPair],
    policy_v3_pairs: list[CalibrationPair],
    n_bins: int = 10,
) -> CalibrationPolicyComparison:
    """Compare policy version 4 with the same judgments scored by version 3."""

    current = CalibrationMetrics(
        policy_version="4",
        brier_score=brier_score(current_pairs),
        expected_calibration_error=expected_calibration_error(
            current_pairs, n_bins=n_bins
        ),
        auroc=auroc(current_pairs),
    )
    prior = CalibrationMetrics(
        policy_version="3",
        brier_score=brier_score(policy_v3_pairs),
        expected_calibration_error=expected_calibration_error(
            policy_v3_pairs, n_bins=n_bins
        ),
        auroc=auroc(policy_v3_pairs),
    )
    return CalibrationPolicyComparison(
        current=current,
        prior=prior,
        brier_score_delta=current.brier_score - prior.brier_score,
        expected_calibration_error_delta=(
            current.expected_calibration_error - prior.expected_calibration_error
        ),
        auroc_delta=current.auroc - prior.auroc,
    )


def brier_score(pairs: list[CalibrationPair]) -> float:
    """Return the Brier score: mean squared error between probability and label.

    Formula: ``(1 / n) * sum((predicted_confidence - actual_outcome)^2)`` with
    ``actual_outcome`` interpreted as ``1.0`` for ``True`` and ``0.0`` for
    ``False``. Empty input returns ``0.0``.
    """
    _validate_pairs(pairs)
    if not pairs:
        return 0.0
    return sum((predicted - float(actual)) ** 2 for predicted, actual in pairs) / len(
        pairs
    )


def auroc(pairs: list[CalibrationPair]) -> float:
    """Return AUROC via the rank-based Mann-Whitney U equivalence.

    ``actual_outcome=True`` is the positive class. The result is the
    probability that a randomly chosen positive row has a higher predicted
    confidence than a randomly chosen negative row, with tied predictions
    counting as 0.5. Empty input or input without both classes returns the
    package's scalar ``0.0`` sentinel instead of NaN.
    """
    _validate_pairs(pairs)
    n_pos = sum(1 for _, actual in pairs if actual)
    n_neg = len(pairs) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.0

    rank_sum_pos = 0.0
    ordered = sorted(pairs, key=lambda pair: pair[0])
    index = 0
    while index < len(ordered):
        tied_until = index + 1
        while tied_until < len(ordered) and ordered[tied_until][0] == ordered[index][0]:
            tied_until += 1
        average_rank = (index + 1 + tied_until) / 2
        rank_sum_pos += average_rank * sum(
            1 for _, actual in ordered[index:tied_until] if actual
        )
        index = tied_until

    u_statistic = rank_sum_pos - (n_pos * (n_pos + 1) / 2)
    return u_statistic / (n_pos * n_neg)


def expected_calibration_error(pairs: list[CalibrationPair], n_bins: int = 10) -> float:
    """Return standard binned expected calibration error (ECE).

    Predictions are split into ``n_bins`` equal-width buckets over ``[0, 1]``:
    ``[0, 1/n_bins)``, ``[1/n_bins, 2/n_bins)``, ... with the final bucket
    including ``1.0``. For non-empty buckets ``B_m``, the standard binned-ECE
    formula is:

    ``ECE = sum_m (|B_m| / n) * |acc(B_m) - conf(B_m)|``

    where ``acc(B_m)`` is the empirical fraction of ``True`` outcomes in the
    bucket and ``conf(B_m)`` is the mean predicted confidence. This is the
    formula popularized for neural-network reliability diagrams by Guo et al.,
    "On Calibration of Modern Neural Networks" (ICML 2017). Empty buckets are
    skipped, so they do not divide by zero or contribute to the weighted sum.
    Empty input returns ``0.0``.
    """
    if not pairs:
        _validate_n_bins(n_bins)
        return 0.0
    buckets = reliability_diagram_data(pairs, n_bins=n_bins)
    return sum(
        (count / len(pairs)) * abs(empirical_accuracy - mean_predicted)
        for mean_predicted, empirical_accuracy, count in buckets
    )


def reliability_diagram_data(
    pairs: list[CalibrationPair], n_bins: int = 10
) -> list[ReliabilityBucket]:
    """Return non-empty reliability-diagram buckets in bucket order.

    Each tuple is ``(mean_predicted, empirical_accuracy, count)`` for one
    non-empty equal-width bucket over ``[0, 1]``. Empty buckets are omitted
    because their empirical accuracy and mean prediction are undefined; callers
    that need display ranges can derive them from ``n_bins`` and the original
    predictions, as ``scripts/calibration_check.py`` does.
    """
    totals, positives, counts = _bucket_tallies(pairs, n_bins)
    return [
        (totals[index] / count, positives[index] / count, count)
        for index, count in enumerate(counts)
        if count
    ]


def bucket_inversions(
    pairs: list[CalibrationPair], n_bins: int = 10
) -> list[BucketInversion]:
    """Return empirical-accuracy inversions across confidence buckets.

    Buckets use the same equal-width assignment as ``reliability_diagram_data``.
    Each returned row means a higher-confidence non-empty bucket has a lower
    empirical positive rate than an earlier lower-confidence non-empty bucket.
    """
    buckets = _bucket_summaries(pairs, n_bins)
    inversions: list[BucketInversion] = []
    for lower_position, lower in enumerate(buckets):
        lower_index, lower_accuracy, lower_count = lower
        for higher_index, higher_accuracy, higher_count in buckets[
            lower_position + 1 :
        ]:
            if higher_accuracy < lower_accuracy:
                inversions.append(
                    BucketInversion(
                        lower_bucket=lower_index,
                        higher_bucket=higher_index,
                        lower_empirical_accuracy=lower_accuracy,
                        higher_empirical_accuracy=higher_accuracy,
                        lower_count=lower_count,
                        higher_count=higher_count,
                    )
                )
    return inversions


def _bucket_summaries(
    pairs: list[CalibrationPair], n_bins: int
) -> list[tuple[int, float, int]]:
    _, positives, counts = _bucket_tallies(pairs, n_bins)
    return [
        (index, positives[index] / count, count)
        for index, count in enumerate(counts)
        if count
    ]


def _bucket_tallies(
    pairs: list[CalibrationPair], n_bins: int
) -> tuple[list[float], list[int], list[int]]:
    """Per-bucket (summed prediction, positive count, row count) tallies."""
    _validate_n_bins(n_bins)
    _validate_pairs(pairs)

    totals = [0.0] * n_bins
    positives = [0] * n_bins
    counts = [0] * n_bins
    for predicted, actual in pairs:
        bucket = _bucket_index(predicted, n_bins)
        totals[bucket] += predicted
        positives[bucket] += int(actual)
        counts[bucket] += 1
    return totals, positives, counts


def _bucket_index(predicted: float, n_bins: int) -> int:
    if predicted == 1.0:
        return n_bins - 1
    return int(predicted * n_bins)


def _validate_n_bins(n_bins: int) -> None:
    if n_bins <= 0:
        raise ValueError("n_bins must be a positive integer")


def _validate_pairs(pairs: list[CalibrationPair]) -> None:
    for predicted, actual in pairs:
        if predicted < 0.0 or predicted > 1.0:
            raise ValueError("predicted_confidence values must be between 0.0 and 1.0")
        if not isinstance(actual, bool):
            raise TypeError("actual_outcome values must be bool")

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Precision/recall harness for attribution scorers.

The bundled fixture set under ``packages/derive/tests/fixtures`` is
illustrative and synthetic, not the real labelled corpus. Real labelling is
future human work; this harness records the mechanism and the current Jaccard
baseline so future scorer swaps have a CI regression guard.

Recorded illustrative baseline at threshold 0.7:
``JaccardScorer(version="jaccard-v1")`` yields precision 0.6667 and recall
0.6667 over the bundled synthetic fixture set. These numbers are not a claim
about real-world attribution performance.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from math import sqrt
from pathlib import Path
from statistics import NormalDist
from typing import Any

from .scoring import JaccardScorer, Scorer
from .similarity import tokenize

_STANDARD_NORMAL = NormalDist()
DEFAULT_CORPUS_SIZING_CONFIDENCE = 0.95
WORST_CASE_ASSUMED_RATE = 0.5


DEFAULT_THRESHOLD_STEP = 0.01
#: The margin used to derive MIN_DRIFT_CASES below: a fixed literal floor is
#: an arbitrary number free of any statistical grounding, so this is instead
#: the corpus size required_labelled_examples_for_proportion_margin says is
#: needed for a ten-percentage-point-margin Wilson interval at worst-case
#: p=0.5.
MIN_DRIFT_MATERIALITY_MARGIN = 0.1


@dataclass(frozen=True)
class LabeledCase:
    """One human-labeled scorer example.

    ``completion_text`` and ``diff_added_lines`` are raw text; the harness
    tokenizes them before scoring. ``should_match`` is the human label.
    """

    completion_text: str
    diff_added_lines: str
    should_match: bool


@dataclass(frozen=True)
class PrecisionRecallResult:
    """Confusion matrix plus precision/recall for one scorer version."""

    scorer_version: str
    threshold: float
    precision: float
    precision_ci: tuple[float, float]
    recall: float
    recall_ci: tuple[float, float]
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int

    @property
    def f1(self) -> float:
        """F1 score derived from this row's precision and recall."""
        denominator = self.precision + self.recall
        return 2 * self.precision * self.recall / denominator if denominator else 0.0


@dataclass(frozen=True)
class ThresholdDriftReport:
    """Threshold-drift verdict for the current labelled corpus.

    ``status`` is ``"ok"`` only when the labelled cases contain enough signal
    to compare thresholds: at least ``min_cases`` total cases (see
    ``MIN_DRIFT_CASES`` — derived from the corpus-sizing planner, not a
    hand-picked literal), one positive label, and one negative label.

    A material drift verdict means the historical threshold's precision or
    recall falls **outside** the optimal threshold's own Wilson confidence
    interval for that same metric
    (``precision_outside_ci``/``recall_outside_ci``) — i.e. the historical
    threshold is statistically distinguishable from the optimum at this
    sample size, not just numerically different by some fixed amount that a
    single lucky grid point can clear on noise alone. ``threshold_delta``
    and ``f1_delta`` are still reported for visibility but do not gate
    materiality: argmax *position* on a small corpus is sampling luck, so only
    the CI comparison decides.
    """

    scorer_version: str
    case_count: int
    positive_count: int
    negative_count: int
    historical_threshold: float
    optimal_threshold: float | None
    historical_result: PrecisionRecallResult | None
    optimal_result: PrecisionRecallResult | None
    threshold_delta: float | None
    f1_delta: float | None
    precision_outside_ci: bool | None
    recall_outside_ci: bool | None
    material: bool
    status: str
    reason: str | None


def _wilson_score_interval(
    successes: int, trials: int, *, confidence: float = 0.95
) -> tuple[float, float]:
    """Return a Wilson score interval for a binomial proportion.

    This is a local stdlib copy rather than an import from
    ``packages/export``: ``packages/derive`` must remain lower-level than
    export, and the formula is short enough that adding a shared dependency
    surface would be heavier than the duplication. When ``trials == 0``,
    returns the documented precision-harness sentinel ``(0.0, 0.0)``.
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


@dataclass(frozen=True)
class CorpusSizeEstimate:
    """Required labelled-case counts for a target one-proportion CI margin."""

    target_margin: float
    confidence: float
    worst_case_rate: float
    worst_case_required_n: int
    assumed_rate: float | None
    assumed_rate_required_n: int | None


def load_labelled_cases(path: str | Path) -> list[LabeledCase]:
    """Load labeled cases from a JSON file.

    The function name retains its original spelling as a compatibility-bound
    public API.

    The JSON may be either a list of case objects or an object with a
    ``cases`` list. Each case must carry ``completion_text``,
    ``diff_added_lines``, and ``should_match``.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    payload = raw["cases"] if isinstance(raw, dict) and "cases" in raw else raw
    if not isinstance(payload, list):
        raise ValueError("labelled fixture must be a list or contain a cases list")
    return [_case_from_json(item, i) for i, item in enumerate(payload)]


def evaluate_scorer(
    scorer: Scorer,
    cases: list[LabeledCase],
    threshold: float,
) -> PrecisionRecallResult:
    """Evaluate one scorer against labeled cases at the given threshold."""
    if threshold < 0.0 or threshold > 1.0:
        raise ValueError("threshold must be between 0.0 and 1.0")

    true_positives = 0
    false_positives = 0
    false_negatives = 0
    true_negatives = 0

    for case in cases:
        score = scorer.score(
            tokenize(case.completion_text), tokenize(case.diff_added_lines)
        )
        predicted_match = score > 0.0 and score >= threshold
        if predicted_match and case.should_match:
            true_positives += 1
        elif predicted_match and not case.should_match:
            false_positives += 1
        elif not predicted_match and case.should_match:
            false_negatives += 1
        else:
            true_negatives += 1

    precision_denominator = true_positives + false_positives
    recall_denominator = true_positives + false_negatives
    precision = true_positives / precision_denominator if precision_denominator else 0.0
    recall = true_positives / recall_denominator if recall_denominator else 0.0
    return PrecisionRecallResult(
        scorer_version=scorer.version,
        threshold=threshold,
        precision=precision,
        precision_ci=_wilson_score_interval(true_positives, precision_denominator),
        recall=recall,
        recall_ci=_wilson_score_interval(true_positives, recall_denominator),
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        true_negatives=true_negatives,
    )


def sweep_thresholds(
    scorer: Scorer,
    cases: list[LabeledCase],
    thresholds: Iterable[float],
) -> list[PrecisionRecallResult]:
    """Evaluate one scorer across threshold values, preserving input order."""
    return [evaluate_scorer(scorer, cases, threshold) for threshold in thresholds]


def required_labelled_examples_for_proportion_margin(
    assumed_rate: float,
    target_margin: float,
    *,
    confidence: float = DEFAULT_CORPUS_SIZING_CONFIDENCE,
) -> int:
    """Plan labelled examples for a target CI half-width around one proportion.

    Uses the standard Wald-based sample-size planning relationship
    ``n = z^2 * p * (1-p) / margin^2``, where ``z`` is the two-tailed critical
    value for ``confidence`` (``NormalDist().inv_cdf(0.975)`` at the 95%
    default). This is intentionally a conservative planning formula for
    deciding how many examples to label before the corpus exists. Once real
    labelled precision/recall counts exist, report their uncertainty with the
    Wilson interval instead; this function is not the reporting interval.

    ``target_margin`` is in raw proportion points, so ``0.1`` means a
    ten-percentage-point half-width. A zero or negative margin raises
    ``ValueError`` instead of producing a divide-by-zero crash or silent
    garbage.
    """
    if not 0 <= assumed_rate <= 1:
        raise ValueError("assumed_rate must be between 0 and 1")
    if target_margin <= 0:
        raise ValueError("target_margin must be positive")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")

    z_crit = _STANDARD_NORMAL.inv_cdf(1 - (1 - confidence) / 2)
    n = z_crit**2 * assumed_rate * (1 - assumed_rate)
    return math.ceil(n / target_margin**2)


#: Corpus-size floor before threshold_drift_report treats its Wilson-CI
#: comparison as meaningful -- computed, not a hand-picked literal, from the
#: same worst-case planning formula operators use to size a labelled corpus.
MIN_DRIFT_CASES = required_labelled_examples_for_proportion_margin(
    WORST_CASE_ASSUMED_RATE, MIN_DRIFT_MATERIALITY_MARGIN
)


def plan_labelled_corpus_size(
    target_margin: float,
    *,
    assumed_rate: float | None = None,
    confidence: float = DEFAULT_CORPUS_SIZING_CONFIDENCE,
) -> CorpusSizeEstimate:
    """Plan corpus size under both worst-case and optional assumed-rate inputs.

    When the true precision or recall is unknown, ``p=0.5`` is the safe
    conservative assumption because it maximizes ``p * (1-p)`` and therefore
    the required labelled-case count. If ``assumed_rate`` is provided, the
    returned estimate includes both that assumption-specific answer and the
    ``p=0.5`` worst-case answer for comparison.
    """
    worst_case_required_n = required_labelled_examples_for_proportion_margin(
        WORST_CASE_ASSUMED_RATE,
        target_margin,
        confidence=confidence,
    )
    assumed_rate_required_n = (
        required_labelled_examples_for_proportion_margin(
            assumed_rate,
            target_margin,
            confidence=confidence,
        )
        if assumed_rate is not None
        else None
    )
    return CorpusSizeEstimate(
        target_margin=target_margin,
        confidence=confidence,
        worst_case_rate=WORST_CASE_ASSUMED_RATE,
        worst_case_required_n=worst_case_required_n,
        assumed_rate=assumed_rate,
        assumed_rate_required_n=assumed_rate_required_n,
    )


#: Wilson-interval bounds are clamped to [0, 1] via min()/max() (see
#: _wilson_score_interval), which can leave a bound a few ULPs shy of the
#: mathematical value (e.g. 0.9999999999999998 instead of 1.0). Without this
#: tolerance, a historical result identical to the optimal result (same
#: threshold, same counts) could spuriously read as outside its own CI.
_CI_BOUNDARY_TOLERANCE = 1e-9


def _outside_interval(value: float, interval: tuple[float, float]) -> bool:
    low, high = interval
    return value < low - _CI_BOUNDARY_TOLERANCE or value > high + _CI_BOUNDARY_TOLERANCE


def threshold_drift_report(
    historical_threshold: float,
    cases: list[LabeledCase],
    *,
    scorer: Scorer | None = None,
    thresholds: Iterable[float] | None = None,
    min_cases: int = MIN_DRIFT_CASES,
) -> ThresholdDriftReport:
    """Report whether the historical threshold has drifted materially.

    The current optimum is selected from ``sweep_thresholds`` output by maximum
    F1. If several thresholds tie, the threshold nearest ``historical_threshold``
    wins, which avoids reporting drift when the old threshold is still on an
    equally optimal plateau. Remaining ties choose the lower threshold for a
    stable, deterministic result.

    Materiality (see ``ThresholdDriftReport`` for the full rule) checks the
    historical threshold's precision/recall against the optimal threshold's
    own Wilson interval, not a fixed F1-drop or threshold-move literal: at
    small corpus sizes a fixed drop like ``0.01`` is well within sampling
    noise and fires constantly, while a CI-outside verdict only fires when
    the difference is unlikely to be noise at this sample size.
    ``min_cases`` gates entry into that comparison at all (see
    ``MIN_DRIFT_CASES``).
    """
    if historical_threshold < 0.0 or historical_threshold > 1.0:
        raise ValueError("historical_threshold must be between 0.0 and 1.0")
    if min_cases < 0:
        raise ValueError("min_cases must be non-negative")

    active_scorer = scorer or JaccardScorer()
    positive_count = sum(1 for case in cases if case.should_match)
    negative_count = len(cases) - positive_count
    insufficient_reason = _insufficient_reason(
        len(cases), positive_count, negative_count, min_cases
    )
    if insufficient_reason is not None:
        return ThresholdDriftReport(
            scorer_version=active_scorer.version,
            case_count=len(cases),
            positive_count=positive_count,
            negative_count=negative_count,
            historical_threshold=historical_threshold,
            optimal_threshold=None,
            historical_result=None,
            optimal_result=None,
            threshold_delta=None,
            f1_delta=None,
            precision_outside_ci=None,
            recall_outside_ci=None,
            material=False,
            status="insufficient_data",
            reason=insufficient_reason,
        )

    sweep = sweep_thresholds(
        active_scorer,
        cases,
        _thresholds_including_historical(thresholds, historical_threshold),
    )
    historical_result = next(
        result for result in sweep if result.threshold == historical_threshold
    )
    optimal_result = min(
        sweep,
        key=lambda result: (
            -result.f1,
            abs(result.threshold - historical_threshold),
            result.threshold,
        ),
    )
    threshold_delta = abs(optimal_result.threshold - historical_threshold)
    f1_delta = optimal_result.f1 - historical_result.f1
    precision_outside_ci = _outside_interval(
        historical_result.precision, optimal_result.precision_ci
    )
    recall_outside_ci = _outside_interval(
        historical_result.recall, optimal_result.recall_ci
    )
    # threshold_delta is reported as a diagnostic but never gates materiality:
    # on a small corpus the argmax lands wherever noise puts it, so only the
    # Wilson-CI arms carry evidence.
    material = precision_outside_ci or recall_outside_ci
    return ThresholdDriftReport(
        scorer_version=active_scorer.version,
        case_count=len(cases),
        positive_count=positive_count,
        negative_count=negative_count,
        historical_threshold=historical_threshold,
        optimal_threshold=optimal_result.threshold,
        historical_result=historical_result,
        optimal_result=optimal_result,
        threshold_delta=threshold_delta,
        f1_delta=f1_delta,
        precision_outside_ci=precision_outside_ci,
        recall_outside_ci=recall_outside_ci,
        material=material,
        status="ok",
        reason=None,
    )


def _case_from_json(item: Any, index: int) -> LabeledCase:
    if not isinstance(item, dict):
        raise ValueError(f"labelled case {index} must be an object")
    try:
        completion_text = item["completion_text"]
        diff_added_lines = item["diff_added_lines"]
        should_match = item["should_match"]
    except KeyError as exc:
        raise ValueError(f"labelled case {index} missing {exc.args[0]}") from exc
    if not isinstance(completion_text, str):
        raise ValueError(f"labelled case {index} completion_text must be a string")
    if not isinstance(diff_added_lines, str):
        raise ValueError(f"labelled case {index} diff_added_lines must be a string")
    if not isinstance(should_match, bool):
        raise ValueError(f"labelled case {index} should_match must be a boolean")
    return LabeledCase(
        completion_text=completion_text,
        diff_added_lines=diff_added_lines,
        should_match=should_match,
    )


def _default_thresholds() -> list[float]:
    steps = round(1.0 / DEFAULT_THRESHOLD_STEP)
    return [round(i * DEFAULT_THRESHOLD_STEP, 2) for i in range(steps + 1)]


def _thresholds_including_historical(
    thresholds: Iterable[float] | None, historical_threshold: float
) -> list[float]:
    values = list(_default_thresholds() if thresholds is None else thresholds)
    values.append(historical_threshold)
    unique = sorted(set(values))
    for threshold in unique:
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError("thresholds must be between 0.0 and 1.0")
    return unique


def _insufficient_reason(
    case_count: int, positive_count: int, negative_count: int, min_cases: int
) -> str | None:
    if case_count < min_cases:
        return "too_few_cases"
    if positive_count == 0:
        return "no_positive_cases"
    if negative_count == 0:
        return "no_negative_cases"
    return None

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ground-truth precision report for derived attributions.

The manifest schema is JSONL, one object per labelled completion/linkage:
``scenario``, ``inference_call_id``, ``expected_commit``, ``expected_file``,
``expected_attribution`` (``"git_notes"``, ``"jaccard"``, or null/absent),
``expected_reward_min``, and ``notes``. Rows with null/absent attribution are
negative controls: the inference call must not produce an attribution.

This module is the stats slice of the synthetic ground-truth harness.
The bundled illustrative manifest proves the mechanism and is not a real-world
precision claim; real hand-labelling at scale is future work.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .attribution import AttributionSource, Attribution, derive_attributions
from .mirror import MirrorManager
from .precision_harness import LabeledCase, PrecisionRecallResult, sweep_thresholds
from .scoring import Scorer
from .repository_identity import (
    IdentifiedRepositoryKey,
    LegacyRepositoryKey,
    RepositoryContext,
    RepositoryIdentity,
    RepositoryKey,
    repository_sort_key,
)

if TYPE_CHECKING:
    from sediment_core import FactStore

DEFAULT_THRESHOLD = 0.7
DEFAULT_SWEEP_THRESHOLDS = tuple(round(i / 10, 1) for i in range(1, 10))

_SOURCE_ORDER = (AttributionSource.GIT_NOTES, AttributionSource.JACCARD)
_PredictionKey = tuple[RepositoryKey, str, str, str]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GroundTruthRow:
    """One labelled expected linkage, or one negative completion row."""

    scenario: str
    inference_call_id: str
    expected_commit: str | None
    expected_file: str | None
    expected_attribution: AttributionSource | None
    expected_reward_min: float | None
    notes: str
    expected_repository: RepositoryKey | None = None

    def __post_init__(self) -> None:
        if self.expected_repository is not None:
            if not isinstance(
                self.expected_repository, (IdentifiedRepositoryKey, LegacyRepositoryKey)
            ):
                raise ValueError(
                    "expected_repository must be a validated repository key"
                )
            if self.expected_attribution is None:
                raise ValueError(
                    "negative ground truth cannot name expected_repository"
                )


@dataclass(frozen=True)
class AttributionSourcePrecisionReport:
    """Precision/recall for one attribution source and its threshold sweep."""

    attribution_source: AttributionSource
    default: PrecisionRecallResult
    sweep: list[PrecisionRecallResult]
    skipped_unlabelled_predictions: int
    skipped_repository_ground_truth: int = 0
    skipped_repository_predictions: int = 0


@dataclass(frozen=True)
class AttributionPrecisionReport:
    """Git-notes and jaccard precision reports, evaluated independently."""

    threshold: float
    by_source: dict[AttributionSource, AttributionSourcePrecisionReport]


def load_ground_truth_manifest(path: str | Path) -> list[GroundTruthRow]:
    """Load a JSONL ground-truth manifest.

    Each non-empty line must be one row object. Positive rows require
    ``expected_commit`` and ``expected_file``; negative rows require those
    fields to be null when present.
    """
    rows: list[GroundTruthRow] = []
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"ground-truth manifest line {line_number} is not valid JSON"
            ) from exc
        rows.append(_row_from_json(raw, line_number))
    return rows


def evaluate_attribution_precision(
    attributions: Iterable[Attribution],
    manifest: list[GroundTruthRow],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    sweep_threshold_values: Iterable[float] = DEFAULT_SWEEP_THRESHOLDS,
) -> AttributionPrecisionReport:
    """Compare derived attributions with the labelled manifest.

    A match is exact on repository key and ``(inference_call_id, commit_sha, file_path)`` within the
    attribution source being evaluated. Predictions for completions absent
    from the manifest are skipped and counted, because the labelled set makes
    no claim about them.
    """
    if threshold < 0.0 or threshold > 1.0:
        raise ValueError("threshold must be between 0.0 and 1.0")

    attributions = tuple(attributions)
    labelled_inference_call_ids = {row.inference_call_id for row in manifest}
    by_source, skipped = _attributions_by_source(
        attributions, labelled_inference_call_ids
    )
    manifest, blocked_calls, skipped_truth = _qualify_ground_truth(
        attributions, manifest
    )
    reports: dict[AttributionSource, AttributionSourcePrecisionReport] = {}
    for source in _SOURCE_ORDER:
        scorer = _AttributionPredictionScorer(source.value)
        predictions = [
            row
            for row in by_source[source]
            if row.inference_call_id not in blocked_calls
        ]
        skipped_predictions = len(by_source[source]) - len(predictions)
        cases = _labelled_cases_for_source(source, manifest, predictions, scorer)
        default = sweep_thresholds(scorer, cases, [threshold])[0]
        sweep = sweep_thresholds(scorer, cases, sweep_threshold_values)
        reports[source] = AttributionSourcePrecisionReport(
            attribution_source=source,
            default=default,
            sweep=sweep,
            skipped_unlabelled_predictions=skipped[source],
            skipped_repository_ground_truth=skipped_truth,
            skipped_repository_predictions=skipped_predictions,
        )
    return AttributionPrecisionReport(threshold=threshold, by_source=reports)


def generate_precision_report_for_org(
    store: "FactStore",
    mirrors: MirrorManager,
    org_id: str,
    manifest: list[GroundTruthRow],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    sweep_threshold_values: Iterable[float] = DEFAULT_SWEEP_THRESHOLDS,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> AttributionPrecisionReport:
    """Derive an org's attributions and score them against the manifest."""
    attributions = derive_attributions(
        store, mirrors, org_id, repository_context=repository_context, as_of=as_of
    )
    return evaluate_attribution_precision(
        attributions,
        manifest,
        threshold=threshold,
        sweep_threshold_values=sweep_threshold_values,
    )


def _row_from_json(item: Any, line_number: int) -> GroundTruthRow:
    if not isinstance(item, dict):
        raise ValueError(f"ground-truth manifest line {line_number} must be an object")
    scenario = _required_str(item, "scenario", line_number)
    inference_call_id = _required_str(item, "inference_call_id", line_number)
    expected_attribution = _expected_attribution(item, line_number)
    expected_commit = _optional_str(item, "expected_commit", line_number)
    expected_file = _optional_str(item, "expected_file", line_number)
    expected_reward_min = _expected_reward_min(item, line_number)
    notes = _required_str(item, "notes", line_number)
    expected_repository = _expected_repository(
        item.get("expected_repository"), line_number
    )

    if expected_attribution is None:
        if (
            expected_commit is not None
            or expected_file is not None
            or expected_repository is not None
        ):
            raise ValueError(
                "ground-truth manifest line "
                f"{line_number} negative row must not carry expected commit/file"
            )
    elif expected_commit is None or expected_file is None:
        raise ValueError(
            "ground-truth manifest line "
            f"{line_number} positive row requires expected_commit and expected_file"
        )
    return GroundTruthRow(
        scenario=scenario,
        inference_call_id=inference_call_id,
        expected_commit=expected_commit,
        expected_file=expected_file,
        expected_attribution=expected_attribution,
        expected_reward_min=expected_reward_min,
        notes=notes,
        expected_repository=expected_repository,
    )


def _required_str(item: dict[str, Any], field: str, line_number: int) -> str:
    try:
        value = item[field]
    except KeyError as exc:
        raise ValueError(
            f"ground-truth manifest line {line_number} missing {field}"
        ) from exc
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"ground-truth manifest line {line_number} {field} must be a string"
        )
    return value


def _expected_repository(value: Any, line_number: int) -> RepositoryKey | None:
    if value is None:
        return None
    try:
        if isinstance(value, dict) and set(value) == {"org_id", "repo"}:
            return LegacyRepositoryKey(value["org_id"], value["repo"])
        if isinstance(value, dict) and set(value) == {"org_id", "identity"}:
            identity = value["identity"]
            if isinstance(identity, dict) and set(identity) == {
                "provider",
                "host",
                "repository_id",
            }:
                return IdentifiedRepositoryKey(
                    value["org_id"], RepositoryIdentity(**identity)
                )
    except (TypeError, ValueError):
        pass
    raise ValueError(
        f"ground-truth manifest line {line_number} expected_repository must be a validated repository key"
    )


def _optional_str(item: dict[str, Any], field: str, line_number: int) -> str | None:
    value = item.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"ground-truth manifest line {line_number} {field} must be a string"
        )
    return value


def _expected_attribution(
    item: dict[str, Any], line_number: int
) -> AttributionSource | None:
    value = item.get("expected_attribution")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            "ground-truth manifest line "
            f"{line_number} expected_attribution must be git_notes, jaccard, or null"
        )
    try:
        return AttributionSource(value)
    except ValueError as exc:
        raise ValueError(
            "ground-truth manifest line "
            f"{line_number} expected_attribution must be git_notes, jaccard, or null"
        ) from exc


def _expected_reward_min(item: dict[str, Any], line_number: int) -> float | None:
    try:
        value = item["expected_reward_min"]
    except KeyError as exc:
        raise ValueError(
            f"ground-truth manifest line {line_number} missing expected_reward_min"
        ) from exc
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(
            "ground-truth manifest line "
            f"{line_number} expected_reward_min must be a number or null"
        )
    return float(value)


def _attributions_by_source(
    attributions: Iterable[Attribution], labelled_inference_call_ids: set[str]
) -> tuple[dict[AttributionSource, list[Attribution]], dict[AttributionSource, int]]:
    """Split predictions by attribution source, counting the ones the manifest
    makes no claim about (their completion is unlabelled) instead of scoring
    them."""
    by_source: dict[AttributionSource, list[Attribution]] = {
        s: [] for s in _SOURCE_ORDER
    }
    skipped: dict[AttributionSource, int] = {s: 0 for s in _SOURCE_ORDER}
    for attribution in attributions:
        source = AttributionSource(attribution.attribution_source)
        if attribution.inference_call_id not in labelled_inference_call_ids:
            skipped[source] += 1
            continue
        by_source[source].append(attribution)
    return by_source, skipped


def _qualify_ground_truth(
    attributions: Iterable[Attribution], manifest: list[GroundTruthRow]
) -> tuple[list[GroundTruthRow], set[str], int]:
    """Old positive labels require exactly one legacy repository in the population.

    An unresolved positive prevents judging other predictions for that call:
    a partially interpreted label set cannot establish a false positive. Negative
    controls remain repository-independent because they prohibit every linkage.
    Counts name manifest rows and predictions separately, per source report.
    """
    repositories = {_attribution_repository(row) for row in attributions}
    repositories.update(
        row.expected_repository
        for row in manifest
        if row.expected_repository is not None
    )
    legacy = next(iter(repositories)) if len(repositories) == 1 else None
    if not isinstance(legacy, LegacyRepositoryKey):
        legacy = None
    blocked = {
        row.inference_call_id
        for row in manifest
        if row.expected_attribution is not None
        and row.expected_repository is None
        and legacy is None
    }
    skipped = sum(row.inference_call_id in blocked for row in manifest)
    if skipped:
        logger.warning(
            "Precision ground truth declined reason=repository_identity_unresolved count=%d",
            skipped,
        )
    return (
        [
            replace(row, expected_repository=legacy)
            if row.expected_attribution is not None and row.expected_repository is None
            else row
            for row in manifest
            if row.inference_call_id not in blocked
        ],
        blocked,
        skipped,
    )


def _labelled_cases_for_source(
    source: AttributionSource,
    manifest: list[GroundTruthRow],
    attributions: list[Attribution],
    scorer: "_AttributionPredictionScorer",
) -> list[LabeledCase]:
    expected_keys = {
        _expected_key(row)
        for row in manifest
        if row.expected_attribution == source
        and row.expected_commit is not None
        and row.expected_file is not None
    }
    predicted_keys = {_prediction_key(attribution) for attribution in attributions}
    cases: list[LabeledCase] = []

    for attribution in sorted(
        attributions,
        key=lambda item: (
            item.inference_call_id,
            item.commit_sha,
            item.file_path,
            repository_sort_key(_attribution_repository(item)),
            item.similarity_score,
        ),
    ):
        cases.append(
            scorer.case(
                score=attribution.similarity_score,
                should_match=_prediction_key(attribution) in expected_keys,
            )
        )

    # Expected linkages nothing predicted: false negatives, one per missing key.
    for _ in expected_keys - predicted_keys:
        cases.append(scorer.case(score=0.0, should_match=True))

    predicted_inference_call_ids = {
        attribution.inference_call_id for attribution in attributions
    }
    for row in manifest:
        if row.expected_attribution == source:
            continue
        if row.inference_call_id in predicted_inference_call_ids:
            continue
        cases.append(scorer.case(score=0.0, should_match=False))
    return cases


def _expected_key(row: GroundTruthRow) -> _PredictionKey:
    if (
        row.expected_commit is None
        or row.expected_file is None
        or row.expected_repository is None
    ):
        raise ValueError("expected key requires repository, commit and file")
    return (
        row.expected_repository,
        row.inference_call_id,
        row.expected_commit,
        row.expected_file,
    )


def _prediction_key(attribution: Attribution) -> _PredictionKey:
    return (
        _attribution_repository(attribution),
        attribution.inference_call_id,
        attribution.commit_sha,
        attribution.file_path,
    )


def _attribution_repository(attribution: Attribution) -> RepositoryKey:
    return (
        IdentifiedRepositoryKey(attribution.org_id, attribution.repository_identity)
        if attribution.repository_identity is not None
        else LegacyRepositoryKey(attribution.org_id, attribution.repo)
    )


class _AttributionPredictionScorer(Scorer):
    """Replay precomputed attribution scores through ``sweep_thresholds``."""

    def __init__(self, attribution_source: str) -> None:
        self.version = f"attribution-{attribution_source}"
        self._scores: dict[str, float] = {}
        self._next_case = 0

    def case(self, *, score: float, should_match: bool) -> LabeledCase:
        case_id = f"groundtruthcase_{self._next_case}"
        self._next_case += 1
        self._scores[case_id] = score
        return LabeledCase(
            completion_text=case_id,
            diff_added_lines=case_id,
            should_match=should_match,
        )

    def score(self, completion_tokens: set[str], diff_tokens: set[str]) -> float:
        del diff_tokens
        for token in completion_tokens:
            if token in self._scores:
                return self._scores[token]
        return 0.0

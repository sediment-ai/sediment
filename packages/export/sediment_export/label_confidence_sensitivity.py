# SPDX-License-Identifier: AGPL-3.0-or-later
"""Label-confidence policy hyperparameter sensitivity.

This module sweeps one :class:`~sediment_export.label_confidence.LabelConfidencePolicy` knob at
a time over a fixed list of already-assembled attributed completions. It is a pure
diagnostic derivation: no facts are persisted, no attributions are re-joined,
and every row is recomputable from ``(attributed_completions, policy grid)``.
Every statistic stays within one selected SFT recipe and eligibility-source
stratum. Ineligible inputs use a null source instead of disappearing.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal, get_args

from sediment_core import FactStore
from sediment_derive import MirrorManager, RepositoryContext

from .label_confidence import (
    LabelConfidencePolicy,
    ci_failed,
    ci_passed,
    decision_branch,
    resolve_ci_resolution,
    resolve_confidence,
    resolve_policy_v3_confidence,
)
from .sft import (
    SFT_RECIPE_VERSION,
    SFTEligibilitySource,
    SFTPolicy,
    _eligibility_source,
)
from .attributed_completions import (
    AttributedCompletion,
    AttributedCompletionPolicy,
    assemble_attributed_completions_result,
)

LabelConfidencePolicyKnob = Literal[
    "explicit_accept_confidence",
    "explicit_reject_confidence",
    "baseline_confidence",
    "implicit_accept_multiplier",
    "implicit_reject_multiplier",
    "ci_pass_multiplier",
    "ci_fail_multiplier",
    "ci_resolution.clean_reliability",
    "ci_resolution.suspected_flake_reliability",
    "ci_resolution.non_verdict_reliability",
]

LABEL_CONFIDENCE_POLICY_KNOBS: tuple[LabelConfidencePolicyKnob, ...] = get_args(
    LabelConfidencePolicyKnob
)

DEFAULT_SWEEP_GRIDS: Mapping[LabelConfidencePolicyKnob, tuple[float, ...]] = {
    "explicit_accept_confidence": (0.8, 0.9, 1.0),
    "explicit_reject_confidence": (0.0, 0.1, 0.2),
    "baseline_confidence": (0.4, 0.5, 0.6, 0.7, 0.8),
    "implicit_accept_multiplier": (1.0, 1.1, 1.2, 1.3, 1.4, 1.5),
    "implicit_reject_multiplier": (0.5, 0.7, 0.9, 1.0, 1.1),
    "ci_pass_multiplier": (1.0, 1.1, 1.2, 1.3),
    "ci_fail_multiplier": (0.5, 0.7, 0.9, 1.0),
    "ci_resolution.clean_reliability": (0.8, 0.9, 1.0),
    "ci_resolution.suspected_flake_reliability": (0.0, 0.25, 0.5, 0.75, 1.0),
    "ci_resolution.non_verdict_reliability": (0.5, 0.75, 1.0),
}


@dataclass(frozen=True)
class ConfidenceStats:
    """Basic distribution for resolved confidence values."""

    count: int
    mean: float | None
    median: float | None


@dataclass(frozen=True)
class LabelConfidenceSensitivityRow:
    """One point in a one-knob label-confidence-policy sweep."""

    recipe_id: str
    recipe_version: int
    eligibility_source: SFTEligibilitySource | None
    knob: LabelConfidencePolicyKnob
    value: float
    affected_attributed_completions: int
    sft_eligible_count: int
    sft_eligible_delta: int
    all_confidences: ConfidenceStats
    all_mean_delta: float | None
    all_median_delta: float | None
    sft_confidences: ConfidenceStats
    sft_mean_delta: float | None
    sft_median_delta: float | None
    policy_v3_all_confidences: ConfidenceStats
    all_mean_delta_from_policy_v3: float | None
    policy_v3_sft_confidences: ConfidenceStats
    policy_v3_sft_eligible_count: int
    sft_eligible_delta_from_policy_v3: int


def _stats(values: Sequence[float]) -> ConfidenceStats:
    if not values:
        return ConfidenceStats(count=0, mean=None, median=None)
    return ConfidenceStats(
        count=len(values),
        mean=statistics.mean(values),
        median=statistics.median(values),
    )


def _delta(value: float | None, baseline: float | None) -> float | None:
    if value is None or baseline is None:
        return None
    return value - baseline


def _sft_confidences_by_source(
    attributed_completions: Iterable[AttributedCompletion],
    policy: SFTPolicy,
    *,
    repository_context: RepositoryContext | None = None,
) -> dict[SFTEligibilitySource, list[float]]:
    confidences: dict[SFTEligibilitySource, list[float]] = {}
    for attributed_completion in attributed_completions:
        if attributed_completion.abandonment is not None:
            continue
        source, _ = _eligibility_source(
            attributed_completion, policy, repository_context=repository_context
        )
        if source is None:
            continue
        confidence = resolve_confidence(
            attributed_completion,
            policy.label_confidence,
            repository_context=repository_context,
        )
        if confidence is not None and confidence >= policy.min_confidence:
            confidences.setdefault(source, []).append(confidence)
    return confidences


def _all_confidences_by_source(
    attributed_completions: Iterable[AttributedCompletion],
    sft_policy: SFTPolicy,
    label_confidence_policy: LabelConfidencePolicy,
    *,
    repository_context: RepositoryContext | None = None,
) -> dict[SFTEligibilitySource | None, list[float]]:
    confidences: dict[SFTEligibilitySource | None, list[float]] = {}
    for attributed_completion in attributed_completions:
        source, _ = _eligibility_source(
            attributed_completion, sft_policy, repository_context=repository_context
        )
        confidence = resolve_confidence(
            attributed_completion,
            label_confidence_policy,
            repository_context=repository_context,
        )
        if confidence is not None:
            confidences.setdefault(source, []).append(confidence)
    return confidences


def _policy_v3_all_confidences_by_source(
    attributed_completions: Iterable[AttributedCompletion],
    sft_policy: SFTPolicy,
    label_confidence_policy: LabelConfidencePolicy,
    *,
    repository_context: RepositoryContext | None = None,
) -> dict[SFTEligibilitySource | None, list[float]]:
    confidences: dict[SFTEligibilitySource | None, list[float]] = {}
    for attributed_completion in attributed_completions:
        source, _ = _eligibility_source(
            attributed_completion, sft_policy, repository_context=repository_context
        )
        confidence = resolve_policy_v3_confidence(
            attributed_completion, label_confidence_policy
        )
        if confidence is not None:
            confidences.setdefault(source, []).append(confidence)
    return confidences


def _resolved_confidences(
    attributed_completions: Iterable[AttributedCompletion],
    label_confidence_policy: LabelConfidencePolicy,
    *,
    repository_context: RepositoryContext | None = None,
) -> list[float]:
    return [
        confidence
        for attributed_completion in attributed_completions
        if (
            confidence := resolve_confidence(
                attributed_completion,
                label_confidence_policy,
                repository_context=repository_context,
            )
        )
        is not None
    ]


def _policy_v3_confidences(
    attributed_completions: Iterable[AttributedCompletion],
    policy: LabelConfidencePolicy,
) -> list[float]:
    return [
        confidence
        for attributed_completion in attributed_completions
        if (confidence := resolve_policy_v3_confidence(attributed_completion, policy))
        is not None
    ]


def _policy_v3_sft_confidences_by_source(
    attributed_completions: Iterable[AttributedCompletion],
    policy: SFTPolicy,
    *,
    repository_context: RepositoryContext | None = None,
) -> dict[SFTEligibilitySource, list[float]]:
    confidences: dict[SFTEligibilitySource, list[float]] = {}
    for attributed_completion in attributed_completions:
        if attributed_completion.abandonment is not None:
            continue
        source, _ = _eligibility_source(
            attributed_completion, policy, repository_context=repository_context
        )
        if source is None:
            continue
        confidence = resolve_policy_v3_confidence(
            attributed_completion, policy.label_confidence
        )
        if confidence is not None and confidence >= policy.min_confidence:
            confidences.setdefault(source, []).append(confidence)
    return confidences


def _affected_by_knob(
    attributed_completion: AttributedCompletion,
    knob: LabelConfidencePolicyKnob,
    *,
    repository_context: RepositoryContext | None = None,
) -> bool:
    branch = decision_branch(attributed_completion)
    if knob == "explicit_accept_confidence":
        return branch == "explicit_accept"
    if knob == "explicit_reject_confidence":
        return branch in {"abandoned", "explicit_reject"}
    if knob == "baseline_confidence":
        return branch in {
            "no_decision",
            "implicit_reject",
            "implicit_accept_codex_neutral",
            "implicit_accept",
        }
    if knob == "implicit_accept_multiplier":
        return branch == "implicit_accept"
    if knob == "implicit_reject_multiplier":
        return branch in {"implicit_reject", "implicit_accept"}
    if knob == "ci_pass_multiplier":
        return ci_passed(attributed_completion, repository_context=repository_context)
    if knob == "ci_fail_multiplier":
        return ci_failed(attributed_completion, repository_context=repository_context)
    resolution = resolve_ci_resolution(
        attributed_completion, repository_context=repository_context
    )
    if resolution is None or resolution.verdict is None:
        return False
    if knob == "ci_resolution.clean_reliability":
        return not resolution.suspected_flake
    if knob == "ci_resolution.suspected_flake_reliability":
        return resolution.suspected_flake
    if knob == "ci_resolution.non_verdict_reliability":
        return bool(resolution.non_verdict_outcome_ids)
    raise ValueError(f"unknown LabelConfidencePolicy knob {knob!r}")


def _validate_knob(knob: str) -> LabelConfidencePolicyKnob:
    if knob not in LABEL_CONFIDENCE_POLICY_KNOBS:
        raise ValueError(f"unknown LabelConfidencePolicy knob {knob!r}")
    return knob  # type: ignore[return-value]


def _replace_knob(
    policy: LabelConfidencePolicy,
    knob: LabelConfidencePolicyKnob,
    value: float,
) -> LabelConfidencePolicy:
    prefix = "ci_resolution."
    if knob.startswith(prefix):
        ci_resolution = replace(
            policy.ci_resolution, **{knob.removeprefix(prefix): value}
        )
        return replace(policy, ci_resolution=ci_resolution)
    return replace(policy, **{knob: value})


def build_label_confidence_sensitivity(
    attributed_completions: Iterable[AttributedCompletion],
    *,
    label_confidence_policy: LabelConfidencePolicy | None = None,
    sft_min_confidence: float = SFTPolicy.min_confidence,
    sft_policy: SFTPolicy | None = None,
    grids: Mapping[str, Sequence[float]] | None = None,
    repository_context: RepositoryContext | None = None,
) -> list[LabelConfidenceSensitivityRow]:
    """Sweep one ``LabelConfidencePolicy`` knob at a time over a fixed attributed-completion set.

    ``sft_eligible_count`` counts attributed completions that clear the same SFT eligibility
    gates and confidence floor as ``sft.py``. It deliberately does not apply
    completion lookup or one-row-per-completion deduplication because this
    report is specified over a fixed set of attributed completions.
    Identified CI evidence requires the caller's complete repository context.
    """

    attributed_completions_list = list(attributed_completions)
    if not attributed_completions_list:
        return []

    base_label_confidence = label_confidence_policy or (
        sft_policy.label_confidence if sft_policy else LabelConfidencePolicy()
    )
    base_sft = replace(
        sft_policy or SFTPolicy(),
        label_confidence=base_label_confidence,
        min_confidence=sft_min_confidence,
    )
    base_all_by_source = _all_confidences_by_source(
        attributed_completions_list,
        base_sft,
        base_label_confidence,
        repository_context=repository_context,
    )
    base_sft_by_source = _sft_confidences_by_source(
        attributed_completions_list, base_sft, repository_context=repository_context
    )
    policy_v3_all_by_source = _policy_v3_all_confidences_by_source(
        attributed_completions_list,
        base_sft,
        base_label_confidence,
        repository_context=repository_context,
    )
    policy_v3_sft_by_source = _policy_v3_sft_confidences_by_source(
        attributed_completions_list, base_sft, repository_context=repository_context
    )

    configured_grids: Mapping[str, Sequence[float]] = grids or DEFAULT_SWEEP_GRIDS
    rows: list[LabelConfidenceSensitivityRow] = []
    for knob_name, values in configured_grids.items():
        knob = _validate_knob(knob_name)
        for value in values:
            swept_label_confidence = _replace_knob(base_label_confidence, knob, value)
            swept_sft = replace(
                base_sft,
                label_confidence=swept_label_confidence,
            )
            swept_all_by_source = _all_confidences_by_source(
                attributed_completions_list,
                swept_sft,
                swept_label_confidence,
                repository_context=repository_context,
            )
            swept_by_source = _sft_confidences_by_source(
                attributed_completions_list,
                swept_sft,
                repository_context=repository_context,
            )
            sources = sorted(
                set(base_sft_by_source)
                | set(policy_v3_sft_by_source)
                | set(swept_by_source)
                | set(base_all_by_source)
                | set(policy_v3_all_by_source)
                | set(swept_all_by_source),
                key=lambda source: source or "",
            )
            for source in sources:
                all_stats = _stats(swept_all_by_source.get(source, []))
                base_all = _stats(base_all_by_source.get(source, []))
                policy_v3_all = _stats(policy_v3_all_by_source.get(source, []))
                sft_stats = _stats(swept_by_source.get(source, []))
                base_sft_stats = _stats(base_sft_by_source.get(source, []))
                policy_v3_sft = _stats(policy_v3_sft_by_source.get(source, []))
                affected_attributed_completions = sum(
                    _affected_by_knob(t, knob, repository_context=repository_context)
                    and _eligibility_source(
                        t, base_sft, repository_context=repository_context
                    )[0]
                    == source
                    for t in attributed_completions_list
                )
                rows.append(
                    LabelConfidenceSensitivityRow(
                        recipe_id=base_sft.recipe_id,
                        recipe_version=SFT_RECIPE_VERSION,
                        eligibility_source=source,
                        knob=knob,
                        value=value,
                        affected_attributed_completions=(
                            affected_attributed_completions
                        ),
                        sft_eligible_count=sft_stats.count,
                        sft_eligible_delta=sft_stats.count - base_sft_stats.count,
                        all_confidences=all_stats,
                        all_mean_delta=_delta(all_stats.mean, base_all.mean),
                        all_median_delta=_delta(all_stats.median, base_all.median),
                        sft_confidences=sft_stats,
                        sft_mean_delta=_delta(sft_stats.mean, base_sft_stats.mean),
                        sft_median_delta=_delta(
                            sft_stats.median, base_sft_stats.median
                        ),
                        policy_v3_all_confidences=policy_v3_all,
                        all_mean_delta_from_policy_v3=_delta(
                            all_stats.mean, policy_v3_all.mean
                        ),
                        policy_v3_sft_confidences=policy_v3_sft,
                        policy_v3_sft_eligible_count=policy_v3_sft.count,
                        sft_eligible_delta_from_policy_v3=(
                            sft_stats.count - policy_v3_sft.count
                        ),
                    )
                )
    return rows


def generate_label_confidence_sensitivity(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    *,
    label_confidence_policy: LabelConfidencePolicy | None = None,
    sft_min_confidence: float = SFTPolicy.min_confidence,
    sft_policy: SFTPolicy | None = None,
    attributed_completion_policy: AttributedCompletionPolicy | None = None,
    grids: Mapping[str, Sequence[float]] | None = None,
) -> list[LabelConfidenceSensitivityRow]:
    """Assemble the org's attributed completions once, then run the pure sensitivity sweep."""

    with store.read_snapshot() as snapshot:
        assembled = assemble_attributed_completions_result(
            snapshot, mirrors, org_id, attributed_completion_policy
        )
        return build_label_confidence_sensitivity(
            assembled.rows,
            label_confidence_policy=label_confidence_policy,
            sft_min_confidence=sft_min_confidence,
            sft_policy=sft_policy,
            grids=grids,
            repository_context=assembled.repository_context,
        )

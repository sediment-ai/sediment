# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SFT projection — a thin projection over attributed completions (ADR 0004) into
``SFTSample`` rows under one selected evidence recipe.

``sft_curated`` version 1 is the default. It requires either a human-explicit
accept or an accepted edit whose retention score meets the versioned strong
retention threshold (0.8). A human-explicit reject, abandonment, or
any resolved workflow failure vetoes eligibility. A resolved CI pass can affect
Confidence and CI reliability but cannot create curated eligibility.

``sft_verified`` version 1 is explicit opt-in. It requires a clean resolved CI
pass and excludes ambiguous verdicts, non-verdict-only evidence, and suspected
flakes. Both recipes also require:
- **Confidence floor**: the resolved confidence (``label_confidence.py``)
  clears ``SFTPolicy.min_confidence`` (default ``0.6`` — chosen to admit a
  bare no-decision-but-CI-passed attributed_completion under the default
  ``LabelConfidencePolicy`` (``baseline_confidence(0.6) * ci_pass_multiplier(1.1) =
  0.66``) while excluding a CI-passed-but-implicit-reject attributed_completion
  (``0.6*0.9*1.1=0.594``) — a floor with real, documented teeth against the
  default label-confidence policy's own numbers, not an arbitrary round figure).
- **Not explicit-rejected**: an explicit reject disqualifies an attributed
  completion independent of the other gates.
- **Not abandoned**: abandonment evidence disqualifies a attributed completion
  before every other gate, including a configured zero confidence floor.

**One sample per completion.** A completion can appear in several labeled
completions — each changed file of a commit is its own attribution, so a
notes-attributed session touching N files yields N attributed completions for one
completion, and an SFT sample carries no per-file field: emitting one row per
attributed completion would write N near-identical (often byte-identical) copies
of the same prompt/completion text, silently over-weighting multi-file
completions. Among a completion's *eligible* attributed completions, the
highest-confidence one is kept. The repository-qualified ``_evidence_sort_key``
breaks ties. Extras count as ``duplicate_completion`` skips. Conflicting payloads
for one complete evidence identity decline the whole Inference call once under
``conflicting_evidence``, even when another sibling ranks higher.

Diff-shaped SFT (``diff_sft.py``) uses this module's selected recipe and
``_eligibility_source`` for the exact retained patch target.
"""

from __future__ import annotations

import logging
import math
from sediment_derive.repository_identity import RepositoryContext, RepositoryIdentity

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Literal, Never

from pydantic import BaseModel
from sediment_core import CIResult
from sediment_core import NonEmptyId

from sediment_derive import (
    AttributionSource,
    CI_RESOLUTION_SKIP_REASONS,
    CIResolutionSkipReason,
    InferenceCall,
    Provenance,
    Split,
    inference_model,
)

from .jsonl import ExportRow
from .schema_identity import SFT_SAMPLE_SCHEMA_ID, SFT_SAMPLE_SCHEMA_VERSION
from .label_confidence import (
    LabelConfidencePolicy,
    resolve_confidence,
    resolve_ci_resolution,
    resolve_ci_resolution_result,
)
from .trainer import (
    TRAINER_SKIP_REASONS,
    TrainerAssistantMessage,
    TrainerMappingError,
    TrainerMessage,
    TrainerSkipReason,
    map_inference_call,
    validate_training_representation,
)
from .attributed_completions import (
    AttributedCompletion,
    _evidence_sort_key,
    session_commit_observation_ids,
)

logger = logging.getLogger("sediment.export.sft")

SFTRecipeId = Literal["sft_curated", "sft_verified"]
SFTRecipeVersion = Literal[1]
SFTEligibilitySource = Literal[
    "explicit_accept",
    "edit_retention",
    "resolved_ci_pass",
]
SFT_RECIPE_VERSION: SFTRecipeVersion = 1
SFT_CURATED_V1_STRONG_RETENTION_THRESHOLD = 0.8
type SFTSkipReason = (
    CIResolutionSkipReason
    | TrainerSkipReason
    | Literal[
        "abandoned",
        "explicit_reject",
        "resolved_ci_failure",
        "no_eligibility_source",
        "unreliable_ci_resolution",
        "no_reward_signal",
        "below_confidence_floor",
        "inference_call_not_found",
        "model_absent",
        "duplicate_completion",
        "conflicting_evidence",
    ]
)
SFT_SKIP_REASONS: tuple[SFTSkipReason, ...] = (
    *CI_RESOLUTION_SKIP_REASONS,
    *TRAINER_SKIP_REASONS,
    "abandoned",
    "explicit_reject",
    "resolved_ci_failure",
    "no_eligibility_source",
    "unreliable_ci_resolution",
    "no_reward_signal",
    "below_confidence_floor",
    "inference_call_not_found",
    "model_absent",
    "duplicate_completion",
    "conflicting_evidence",
)


@dataclass(frozen=True)
class SFTMetadata:
    """Evidence kept outside the trainer's prompt and completion inputs."""

    org_id: str
    source_model: str
    completion_id: str
    recipe_id: SFTRecipeId
    recipe_version: SFTRecipeVersion
    eligibility_source: SFTEligibilitySource
    label_confidence: float
    ci_reliability: float | None
    provenance: Provenance
    split: Split
    repository_identity: RepositoryIdentity | None = None
    attribution_source: AttributionSource | None = None
    session_commit_observation_ids: tuple[NonEmptyId, ...] = ()
    schema_id: Literal[SFT_SAMPLE_SCHEMA_ID] = SFT_SAMPLE_SCHEMA_ID
    schema_version: Literal[3] = SFT_SAMPLE_SCHEMA_VERSION


@dataclass(frozen=True)
class SFTSample:
    """One SFT training row admitted by the selected evidence recipe."""

    prompt: list[TrainerMessage]
    completion: list[TrainerAssistantMessage]
    tools: list[Never]
    metadata: SFTMetadata


@dataclass(frozen=True)
class SFTPolicy:
    """SFT projection policy.

    ``label_confidence`` drives confidence resolution (``label_confidence.py``,
    reused not reforked). ``min_confidence`` is the floor; see the module
    docstring for its rationale. Eligibility thresholds belong to the selected
    versioned recipe rather than this caller-configurable policy.
    """

    label_confidence: LabelConfidencePolicy = field(
        default_factory=LabelConfidencePolicy
    )
    min_confidence: float = 0.6
    recipe_id: SFTRecipeId = "sft_curated"

    def __post_init__(self) -> None:
        if self.recipe_id not in {"sft_curated", "sft_verified"}:
            raise ValueError(f"unsupported SFT recipe {self.recipe_id!r}")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError(
                f"SFTPolicy.min_confidence must be in [0.0, 1.0] "
                f"(got {self.min_confidence})"
            )


@dataclass
class Projection:
    """Mirrors ``rlvr.Projection``: the rows to write plus the skip tally
    (reason -> count) for the structured summary log line."""

    rows: list[SFTSample] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)


def _explicit_accept(attributed_completion: AttributedCompletion) -> bool:
    return any(d.explicit and d.accepted for d in attributed_completion.decisions)


def _explicit_reject(attributed_completion: AttributedCompletion) -> bool:
    return any(d.explicit and not d.accepted for d in attributed_completion.decisions)


def _eligibility_source(
    attributed_completion: AttributedCompletion,
    policy: SFTPolicy,
    *,
    repository_context: RepositoryContext | None = None,
) -> tuple[SFTEligibilitySource | None, str | None]:
    """Resolve one SFT recipe source or its closed ineligibility reason."""

    if attributed_completion.abandonment is not None:
        return None, "abandoned"
    if _explicit_reject(attributed_completion):
        return None, "explicit_reject"

    resolution = resolve_ci_resolution(
        attributed_completion,
        policy.label_confidence.ci_resolution,
        repository_context=repository_context,
    )
    if resolution is not None and any(
        workflow.verdict == CIResult.FAILED
        for workflow in resolution.workflow_resolutions
    ):
        return None, "resolved_ci_failure"

    if policy.recipe_id == "sft_verified":
        if resolution is None or resolution.verdict != CIResult.PASSED:
            return None, "no_eligibility_source"
        if resolution.suspected_flake:
            return None, "unreliable_ci_resolution"
        return "resolved_ci_pass", None

    if _explicit_accept(attributed_completion):
        return "explicit_accept", None
    if any(
        decision.accepted
        and decision.edit_retention_score is not None
        and decision.edit_retention_score >= SFT_CURATED_V1_STRONG_RETENTION_THRESHOLD
        for decision in attributed_completion.decisions
    ):
        return "edit_retention", None
    return None, "no_eligibility_source"


def _same_evidence_payload(left: object, right: object) -> bool:
    """Compare canonical payloads, including reflexive non-finite categories."""
    if type(left) is not type(right):
        return False
    if isinstance(left, BaseModel):
        return _same_evidence_payload(
            left.model_dump(mode="python"), right.model_dump(mode="python")
        )
    if is_dataclass(left) and not isinstance(left, type):
        return all(
            _same_evidence_payload(getattr(left, item.name), getattr(right, item.name))
            for item in fields(left)
        )
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_evidence_payload(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(
            _same_evidence_payload(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, float):
        return (math.isnan(left) and math.isnan(right)) or left.hex() == right.hex()
    return left == right


def project_sft(
    attributed_completions: Iterable[AttributedCompletion],
    inference_calls: Mapping[str, InferenceCall],
    policy: SFTPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> Projection:
    """Project ``attributed_completions`` into SFT samples, per the module docstring's
    eligibility rule. ``inference_calls`` maps a attributed-completion id to its
    inference-call fact; ``AttributedCompletion`` itself carries no prompt,
    completion text, or model. Inputs require canonical assembly or a validated
    bundle; this low-level API cannot verify an omitted identity population."""
    policy = policy or SFTPolicy()
    out = Projection()

    attributed_completions = list(attributed_completions)
    identities: dict[tuple, AttributedCompletion] = {}
    conflicting_calls: set[str] = set()
    for evidence in attributed_completions:
        identity = (evidence.org_id, _evidence_sort_key(evidence))
        held = identities.get(identity)
        if held is None:
            identities[identity] = evidence
        elif not _same_evidence_payload(held, evidence):
            conflicting_calls.add(evidence.inference_call_id)
    if conflicting_calls:
        out.skipped["conflicting_evidence"] = len(conflicting_calls)

    # Check identity conflicts before selection: a stronger sibling can't hide
    # inconsistent copies. Identical duplicates retain the normal skip count.
    # Completion ids determine row order.
    best: dict[str, tuple[tuple, SFTSample]] = {}

    for attributed_completion in attributed_completions:
        if attributed_completion.inference_call_id in conflicting_calls:
            continue
        if attributed_completion.abandonment is not None:
            out.skipped["abandoned"] += 1
            continue
        if _explicit_reject(attributed_completion):
            out.skipped["explicit_reject"] += 1
            continue

        out.skipped.update(
            resolve_ci_resolution_result(
                attributed_completion,
                policy.label_confidence.ci_resolution,
                repository_context=repository_context,
            ).skipped
        )
        eligibility_source, skip_reason = _eligibility_source(
            attributed_completion, policy, repository_context=repository_context
        )
        if eligibility_source is None:
            assert skip_reason is not None
            out.skipped[skip_reason] += 1
            continue

        confidence = resolve_confidence(
            attributed_completion,
            policy.label_confidence,
            repository_context=repository_context,
        )
        if confidence is None:
            # Unreachable in practice: _eligibility_source already required
            # evidence that produces Confidence. Keep a fail-soft guard so a
            # recipe change skips and counts instead of crashing an export.
            out.skipped["no_reward_signal"] += 1
            continue
        if confidence < policy.min_confidence:
            out.skipped["below_confidence_floor"] += 1
            continue

        inference_call = inference_calls.get(attributed_completion.inference_call_id)
        if inference_call is None:
            out.skipped["inference_call_not_found"] += 1
            continue
        model = inference_model(inference_call)
        if model is None:
            out.skipped["model_absent"] += 1
            continue
        try:
            conversation = map_inference_call(inference_call)
        except TrainerMappingError as exc:
            out.skipped[exc.reason] += 1
            continue

        sample = SFTSample(
            prompt=conversation.prompt,
            completion=conversation.completion,
            tools=conversation.tools,
            metadata=SFTMetadata(
                org_id=attributed_completion.org_id,
                source_model=model,
                completion_id=attributed_completion.inference_call_id,
                recipe_id=policy.recipe_id,
                recipe_version=SFT_RECIPE_VERSION,
                eligibility_source=eligibility_source,
                label_confidence=confidence,
                ci_reliability=(
                    resolution.reliability
                    if (
                        resolution := resolve_ci_resolution(
                            attributed_completion,
                            policy.label_confidence.ci_resolution,
                            repository_context=repository_context,
                        )
                    )
                    is not None
                    else None
                ),
                provenance=attributed_completion.provenance,
                split=attributed_completion.split,
                repository_identity=attributed_completion.repository_identity,
                attribution_source=attributed_completion.attribution_source,
                session_commit_observation_ids=session_commit_observation_ids(
                    attributed_completion, repository_context=repository_context
                ),
            ),
        )
        try:
            validate_training_representation(sample)
        except TrainerMappingError as exc:
            out.skipped[exc.reason] += 1
            continue
        rank = (
            confidence,
            _evidence_sort_key(attributed_completion),
        )
        held = best.get(attributed_completion.inference_call_id)
        if held is None:
            best[attributed_completion.inference_call_id] = (rank, sample)
            continue
        out.skipped["duplicate_completion"] += 1
        if rank > held[0]:
            best[attributed_completion.inference_call_id] = (rank, sample)

    out.rows = [best[inference_call_id][1] for inference_call_id in sorted(best)]

    logger.info(
        "sft_projected", extra={"rows": len(out.rows), "skipped": dict(out.skipped)}
    )
    return out


def to_export_rows(rows: Iterable[SFTSample]) -> list[ExportRow]:
    """Adapt ``SFTSample`` rows to ``jsonl.py``'s ``ExportRow`` destination
    shape — reused, not reforked (ADR 0004): the write path (atomic write,
    the empty-input no-truncate guard, split-aware naming) lives exactly
    once in ``jsonl.py``, for every projection."""
    return [ExportRow(split=row.metadata.split, body=asdict(row)) for row in rows]

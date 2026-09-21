# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DPO projection — a thin projection over attributed completions into ``DPOPair``
rows, following the pairing semantics ratified in ADR 0004:

- Pairs form only within **identical-prompt buckets**: the same message
  history, structurally, for the **same model**. Bucketing here is **direct
  equality** of the inference call's native message structure — not
  ``rollout.py``'s prefix-comparison ``_canonical_history`` (that helper
  answers "does completion B's history provably extend completion A's",
  which is a *continuation* question for stitching one session's turns into
  segments; DPO bucketing asks "are these two already-complete prompts the
  same", a *membership* question with no continuation to detect). Nor does
  it reuse ``_canonical_history``'s ``cache_control``-stripping: that marker
  varies between transport calls for the same logical prompt, so in
  principle two calls against the same real prompt could differ only in it —
  but bucketing is the more conservative side to be wrong on (a missed pair
  is a lost training signal; a *wrong* pair is a poisoned one), and this is
  the one place ADR 0004 says to pick the fewer-cleaner-pairs reading when
  genuinely ambiguous. So: raw structural equality, documented, not imported
  from a module-private helper answering a different question.
- **Self-pair guard**: a completion never pairs against itself — guaranteed
  by construction, see ``_classify`` below (a completion resolves to exactly
  one of ``chosen``/``rejected``/neither per bucket, so the two pools are
  always disjoint).
- **Promptless skip**: a completion with no messages never enters a bucket.
- **One evidence recipe per pair**: ``dpo_human`` version 2 requires a
  human-explicit accept on the chosen member and a human-explicit reject on
  the rejected member. It is the default and doesn't claim that the developer
  compared the two members directly. ``dpo_outcome`` version 2 is explicit
  opt-in and requires a clean resolved CI pass against a clean resolved CI
  failure. It excludes ambiguity, non-verdict-only evidence, and suspected
  flakes. Human and CI label sources never mix in one pair.
- **Response contrast**: after complete-row representation validation, identical
  mapped response lists decline once as ``identical_responses``. Strict JSON
  comparison ignores object key order and preserves all response values.
- **Per-bucket cap**: ``DPOPolicy.max_pairs_per_bucket`` (default 3 — a
  prolific bucket, e.g. many retries against one prompt, is capped so it
  can't dominate the emitted set and bias training toward one prompt over
  the rest of the dataset; 3 keeps a handful of contrastive pairs per prompt
  without one bucket swamping a small export). The cap bounds evaluated pairs,
  not emitted rows. Declined pairs consume a slot without backfill. Every capped
  bucket is
  **logged** (a ``bucket_capped`` warning naming the bucket and the excess)
  and counted in ``Projection.skipped``, never silently truncated.
- **Dedup-per-completion within pools**: a completion can appear in several
  attributed completions (e.g. touching several files in one commit — each a separate
  attribution). Before pairing, each bucket keeps **at most one attributed completion per
  inference_call_id** — first preferring evidence that the selected recipe can
  label, then the highest resolved confidence (ties broken on
  ``(commit_sha, file_path)`` for determinism) — so a completion
  can occupy at most one pairing slot per bucket, in at most one of the two
  pools. Deliberately conservative (fewer, cleaner pairs: one
  proliferation-prone completion cannot flood a bucket's chosen or rejected
  pool with near-duplicate entries of itself), per ADR 0004's stated
  tie-break for this one qualitative rule.

Row shape: ``prompt``, ``chosen``, and ``rejected`` are trainer-facing message
lists. ``tools`` carries available tool definitions. ``metadata`` carries all
Sediment evidence. ``metadata.label_confidence`` is the weaker member's
confidence. ``metadata.confidence_margin`` is
``chosen_confidence - rejected_confidence``. Since each member confidence is
capped to ``[0.0, 1.0]``, the margin is bounded to
``[-1.0, 1.0]``. In practice the selected recipe and default label-confidence policy usually
emits non-negative margins for ordinary CI-pass-vs-CI-fail and
explicit-accept-vs-explicit-reject pairs, but the pairing rules do not prove
that invariant: attribution discounts and configurable ``LabelConfidencePolicy`` knobs
can make a chosen member's resolved confidence lower than a rejected member's.
``metadata.split`` follows ``split.py``'s decided multi-session rule: an attributed completion's split
is a pure function of its ``session_id``, so two members can only disagree
when they come from different sessions — a legitimate pairing (two sessions,
same prompt, same model) that straddles the holdout. **Eval wins**: the pair
lands in eval whenever either member's session is eval, so a train-side
completion can never leak train-adjacent context into the eval set through a
cross-session pairing (the seam note ``split.py`` left for exactly this
artifact).
"""

from __future__ import annotations

import json
import logging
from sediment_derive.repository_identity import RepositoryContext, RepositoryIdentity

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from itertools import islice
from typing import Literal, Never

from sediment_core import NonEmptyId

from sediment_derive import (
    AttributionSource,
    CI_RESOLUTION_SKIP_REASONS,
    CIResolutionSkipReason,
    CIResolutionPolicy,
    EVAL,
    InferenceCall,
    Provenance,
    Split,
    inference_model,
    inference_prompt,
    inference_prompt_key,
)

from .jsonl import ExportRow
from .schema_identity import DPO_PAIR_SCHEMA_ID, DPO_PAIR_SCHEMA_VERSION
from .label_confidence import (
    LabelConfidencePolicy,
    ci_failed,
    ci_passed,
    decision_branch,
    resolve_confidence,
    resolve_ci_resolution,
    resolve_ci_resolution_result,
)
from .attributed_completions import (
    AttributedCompletion,
    _evidence_sort_key,
    session_commit_observation_ids,
)
from .trainer import (
    TRAINER_SKIP_REASONS,
    TRAINING_REPRESENTATION_SKIP_REASONS,
    TrainerAssistantMessage,
    TrainerConversation,
    TrainerMappingError,
    TrainerMessage,
    TrainerSkipReason,
    map_inference_call,
    validate_training_representation,
)

logger = logging.getLogger("sediment.export.dpo")

DPORecipeId = Literal["dpo_human", "dpo_outcome"]
DPORecipeVersion = Literal[2]
DPOChosenLabelSource = Literal["explicit_accept", "resolved_ci_pass"]
DPORejectedLabelSource = Literal["explicit_reject", "resolved_ci_fail"]
type DPOLabelSource = DPOChosenLabelSource | DPORejectedLabelSource
DPO_RECIPE_VERSION: DPORecipeVersion = 2
type DPOSkipReason = (
    CIResolutionSkipReason
    | TrainerSkipReason
    | Literal[
        "inference_call_not_found",
        "promptless",
        "model_absent",
        "no_label_source",
        "unreliable_ci_resolution",
        "bucket_capped",
        "identical_responses",
    ]
)
DPO_SKIP_REASONS: tuple[DPOSkipReason, ...] = (
    *CI_RESOLUTION_SKIP_REASONS,
    *TRAINER_SKIP_REASONS,
    "inference_call_not_found",
    "promptless",
    "model_absent",
    "no_label_source",
    "unreliable_ci_resolution",
    "bucket_capped",
    "identical_responses",
)


@dataclass(frozen=True)
class DPOProvenance:
    """The independent provenance of both preference members."""

    chosen: Provenance
    rejected: Provenance


@dataclass(frozen=True)
class DPOMetadata:
    """Evidence kept outside the trainer's preference inputs."""

    org_id: str
    source_model: str
    chosen_completion_id: str
    rejected_completion_id: str
    recipe_id: DPORecipeId
    recipe_version: DPORecipeVersion
    chosen_label_source: DPOChosenLabelSource
    rejected_label_source: DPORejectedLabelSource
    label_confidence: float
    ci_reliability: float | None
    confidence_margin: float
    provenance: DPOProvenance
    split: Split
    chosen_repository_identity: RepositoryIdentity | None = None
    rejected_repository_identity: RepositoryIdentity | None = None
    chosen_attribution_source: AttributionSource | None = None
    rejected_attribution_source: AttributionSource | None = None
    chosen_session_commit_observation_ids: tuple[NonEmptyId, ...] = ()
    rejected_session_commit_observation_ids: tuple[NonEmptyId, ...] = ()
    schema_id: Literal[DPO_PAIR_SCHEMA_ID] = DPO_PAIR_SCHEMA_ID
    schema_version: Literal[4] = DPO_PAIR_SCHEMA_VERSION


@dataclass(frozen=True)
class DPOPair:
    """One DPO training row: a ``chosen`` completion set against a
    ``rejected`` one for the identical prompt and model."""

    prompt: list[TrainerMessage]
    chosen: list[TrainerAssistantMessage]
    rejected: list[TrainerAssistantMessage]
    tools: list[Never]
    metadata: DPOMetadata


@dataclass(frozen=True)
class DPOPolicy:
    """The tunable DPO pairing semantics. ``label_confidence`` drives confidence
    resolution (``label_confidence.py``, reused not reforked); ``max_pairs_per_bucket``
    is the per-bucket candidate cap — see the module docstring for the
    default's rationale."""

    label_confidence: LabelConfidencePolicy = field(
        default_factory=LabelConfidencePolicy
    )
    max_pairs_per_bucket: int = 3
    recipe_id: DPORecipeId = "dpo_human"

    def __post_init__(self) -> None:
        if self.recipe_id not in {"dpo_human", "dpo_outcome"}:
            raise ValueError(f"unsupported DPO recipe {self.recipe_id!r}")
        if self.max_pairs_per_bucket < 1:
            raise ValueError(
                "DPOPolicy.max_pairs_per_bucket must be >= 1 "
                f"(got {self.max_pairs_per_bucket})"
            )


def _classify(
    attributed_completion: AttributedCompletion,
    ci_resolution_policy: CIResolutionPolicy | None = None,
    recipe_id: DPORecipeId = "dpo_human",
    *,
    repository_context: RepositoryContext | None = None,
) -> str | None:
    """Return one directional label under the selected evidence recipe."""

    label, _, _ = _classify_with_source(
        attributed_completion,
        ci_resolution_policy,
        recipe_id,
        repository_context=repository_context,
    )
    return label


def _classify_with_source(
    attributed_completion: AttributedCompletion,
    ci_resolution_policy: CIResolutionPolicy | None,
    recipe_id: DPORecipeId,
    *,
    repository_context: RepositoryContext | None = None,
) -> tuple[str | None, DPOLabelSource | None, str | None]:
    """Return label, closed source, and an optional recipe-specific skip."""

    branch = decision_branch(attributed_completion)
    if recipe_id == "dpo_human":
        if branch == "explicit_accept":
            return "chosen", "explicit_accept", None
        if branch == "explicit_reject":
            return "rejected", "explicit_reject", None
        return None, None, "no_label_source"

    resolution = resolve_ci_resolution(
        attributed_completion,
        ci_resolution_policy,
        repository_context=repository_context,
    )
    if resolution is None or resolution.verdict is None:
        return None, None, "no_label_source"
    if resolution.suspected_flake:
        return None, None, "unreliable_ci_resolution"
    if ci_passed(
        attributed_completion,
        ci_resolution_policy,
        repository_context=repository_context,
    ):
        return "chosen", "resolved_ci_pass", None
    if ci_failed(
        attributed_completion,
        ci_resolution_policy,
        repository_context=repository_context,
    ):
        return "rejected", "resolved_ci_fail", None
    return None, None, "no_label_source"


@dataclass
class Projection:
    """Mirrors ``rlvr.Projection``: the rows to write plus the skip tally
    (reason -> count) for the structured summary log line."""

    rows: list[DPOPair] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)


def _dedup_by_completion(
    attributed_completions: list[AttributedCompletion],
    label_confidence: LabelConfidencePolicy,
    recipe_id: DPORecipeId = "dpo_human",
    *,
    repository_context: RepositoryContext | None = None,
) -> dict[str, AttributedCompletion]:
    """One attributed completion per ``inference_call_id``: the highest-confidence one under the
    *operative* label-confidence policy (the same one pair confidence is resolved with
    — ranking under a different policy could retain a different attributed_completion than
    the one the emitted confidence describes), ties broken on
    ``(commit_sha, file_path)`` for determinism (never on read/ingest order,
    ADR 0001). A signal-less attributed completion (``resolve_confidence`` is ``None``)
    ranks below every signal-bearing one — otherwise a no-signal attributed completion
    could win the slot on the sha/path tie-break against an explicit-reject
    attributed_completion whose real confidence is 0.0, and the completion would silently
    drop out of the selected recipe instead of landing in the rejected pool."""
    best: dict[str, tuple[tuple, AttributedCompletion]] = {}
    for t in attributed_completions:
        label = _classify(
            t,
            label_confidence.ci_resolution,
            recipe_id,
            repository_context=repository_context,
        )
        confidence = resolve_confidence(
            t, label_confidence, repository_context=repository_context
        )
        key = (
            label is not None,
            confidence is not None,
            confidence or 0.0,
            _evidence_sort_key(t),
        )
        held = best.get(t.inference_call_id)
        if held is None or key > held[0]:
            best[t.inference_call_id] = (key, t)
    return {inference_call_id: t for inference_call_id, (_, t) in best.items()}


def _bucket_candidates(
    attributed_completions: Iterable[AttributedCompletion],
    inference_calls: Mapping[str, InferenceCall],
    skipped: Counter[str],
) -> tuple[
    dict[tuple, list[AttributedCompletion]],
    dict[str, TrainerConversation],
    dict[str, TrainerSkipReason],
]:
    """Apply DPO membership once for projection and pre-pairing diagnostics.

    Structural failures exclude a member. Representation failures retain it
    until pair selection, so bucket sizes and caps use the same population.
    """
    buckets: dict[tuple, list[AttributedCompletion]] = {}
    conversations: dict[str, TrainerConversation] = {}
    representation_skips: dict[str, TrainerSkipReason] = {}
    for attributed_completion in attributed_completions:
        inference_call = inference_calls.get(attributed_completion.inference_call_id)
        if inference_call is None:
            skipped["inference_call_not_found"] += 1
            continue
        prompt = inference_prompt(inference_call)
        if not prompt:
            skipped["promptless"] += 1
            continue
        model = inference_model(inference_call)
        if model is None:
            skipped["model_absent"] += 1
            continue
        try:
            conversation = map_inference_call(inference_call)
        except TrainerMappingError as exc:
            if exc.reason not in TRAINING_REPRESENTATION_SKIP_REASONS:
                skipped[exc.reason] += 1
                continue
            # Representation eligibility belongs to the pair, including when
            # both members fail. Retain this member until a pair is selected.
            representation_skips[attributed_completion.inference_call_id] = exc.reason
        else:
            conversations[attributed_completion.inference_call_id] = conversation
        key = (
            attributed_completion.org_id,
            model,
            inference_prompt_key(inference_call),
        )
        buckets.setdefault(key, []).append(attributed_completion)

    return buckets, conversations, representation_skips


def _response_key(response: list[TrainerAssistantMessage]) -> str:
    """Compare complete representable responses without collapsing scalar types."""
    return json.dumps(
        response,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def project_dpo(
    attributed_completions: Iterable[AttributedCompletion],
    inference_calls: Mapping[str, InferenceCall],
    policy: DPOPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> Projection:
    """Project ``attributed_completions`` into DPO pairs, per the module docstring's pairing
    semantics. ``inference_calls`` maps a attributed-completion id to its
    inference-call fact; ``AttributedCompletion`` itself carries no message history
    or model. Inputs require canonical assembly or a validated bundle; this
    low-level API cannot verify an omitted identity population."""
    policy = policy or DPOPolicy()
    out = Projection()

    buckets, conversations, representation_skips = _bucket_candidates(
        attributed_completions, inference_calls, out.skipped
    )

    for key, bucket_attributed_completions in sorted(
        buckets.items(), key=lambda kv: kv[0]
    ):
        deduped = _dedup_by_completion(
            bucket_attributed_completions,
            policy.label_confidence,
            policy.recipe_id,
            repository_context=repository_context,
        )

        chosen_ids: list[str] = []
        rejected_ids: list[str] = []
        label_sources: dict[str, DPOLabelSource] = {}
        for inference_call_id, attributed_completion in deduped.items():
            out.skipped.update(
                resolve_ci_resolution_result(
                    attributed_completion,
                    policy.label_confidence.ci_resolution,
                    repository_context=repository_context,
                ).skipped
            )
            label, label_source, skip_reason = _classify_with_source(
                attributed_completion,
                policy.label_confidence.ci_resolution,
                policy.recipe_id,
                repository_context=repository_context,
            )
            if label == "chosen":
                chosen_ids.append(inference_call_id)
                assert label_source is not None
                label_sources[inference_call_id] = label_source
            elif label == "rejected":
                rejected_ids.append(inference_call_id)
                assert label_source is not None
                label_sources[inference_call_id] = label_source
            else:
                assert skip_reason is not None
                out.skipped[skip_reason] += 1

        if not chosen_ids or not rejected_ids:
            continue

        chosen_ids.sort()
        rejected_ids.sort()

        # Lazy: a prolific bucket is exactly what the cap exists to contain, so
        # the cross product is never materialized — only the capped prefix is
        # consumed. Representation eligibility can decline a selected pair.
        rows_before_bucket = len(out.rows)
        possible_pairs = len(chosen_ids) * len(rejected_ids)
        pairs = ((c, r) for c in chosen_ids for r in rejected_ids)
        for chosen_id, rejected_id in islice(pairs, policy.max_pairs_per_bucket):
            chosen = deduped[chosen_id]
            rejected = deduped[rejected_id]
            # A cross-session pair straddling the holdout: eval wins
            # (split.py's decided multi-session rule) — the pair lands in eval
            # whenever either member's session is eval, never skipped and never
            # resolved train-ward.
            pair_split: Split = chosen.split if chosen.split == rejected.split else EVAL
            chosen_confidence = resolve_confidence(
                chosen, policy.label_confidence, repository_context=repository_context
            )
            rejected_confidence = resolve_confidence(
                rejected, policy.label_confidence, repository_context=repository_context
            )
            # Both members classified via an explicit decision or CI, so both
            # always resolve a real confidence (never None) here.
            assert chosen_confidence is not None
            assert rejected_confidence is not None
            label_confidence = min(chosen_confidence, rejected_confidence)
            resolutions = [
                resolution
                for member in (chosen, rejected)
                if (
                    resolution := resolve_ci_resolution(
                        member,
                        policy.label_confidence.ci_resolution,
                        repository_context=repository_context,
                    )
                )
                is not None
                and resolution.reliability is not None
            ]
            ci_reliability = (
                min(resolution.reliability for resolution in resolutions)
                if resolutions
                else None
            )

            representation_reason = representation_skips.get(
                chosen_id
            ) or representation_skips.get(rejected_id)
            if representation_reason is not None:
                out.skipped[representation_reason] += 1
                continue

            chosen_inference_call = inference_calls[chosen_id]
            chosen_conversation = conversations[chosen_id]
            rejected_conversation = conversations[rejected_id]
            chosen_label_source = label_sources[chosen_id]
            rejected_label_source = label_sources[rejected_id]
            assert chosen_label_source in {"explicit_accept", "resolved_ci_pass"}
            assert rejected_label_source in {"explicit_reject", "resolved_ci_fail"}
            pair = DPOPair(
                prompt=chosen_conversation.prompt,
                chosen=chosen_conversation.completion,
                rejected=rejected_conversation.completion,
                tools=chosen_conversation.tools,
                metadata=DPOMetadata(
                    org_id=chosen.org_id,
                    source_model=inference_model(chosen_inference_call),
                    chosen_completion_id=chosen_id,
                    rejected_completion_id=rejected_id,
                    recipe_id=policy.recipe_id,
                    recipe_version=DPO_RECIPE_VERSION,
                    chosen_label_source=chosen_label_source,
                    rejected_label_source=rejected_label_source,
                    label_confidence=label_confidence,
                    ci_reliability=ci_reliability,
                    confidence_margin=chosen_confidence - rejected_confidence,
                    provenance=DPOProvenance(
                        chosen=chosen.provenance,
                        rejected=rejected.provenance,
                    ),
                    split=pair_split,
                    chosen_repository_identity=chosen.repository_identity,
                    rejected_repository_identity=rejected.repository_identity,
                    chosen_attribution_source=chosen.attribution_source,
                    rejected_attribution_source=rejected.attribution_source,
                    chosen_session_commit_observation_ids=session_commit_observation_ids(
                        chosen, repository_context=repository_context
                    ),
                    rejected_session_commit_observation_ids=session_commit_observation_ids(
                        rejected, repository_context=repository_context
                    ),
                ),
            )
            try:
                validate_training_representation(pair)
            except TrainerMappingError as exc:
                out.skipped[exc.reason] += 1
                continue
            if _response_key(chosen_conversation.completion) == _response_key(
                rejected_conversation.completion
            ):
                out.skipped["identical_responses"] += 1
                continue
            out.rows.append(pair)

        if possible_pairs > policy.max_pairs_per_bucket:
            logger.warning(
                "dpo_bucket_capped",
                extra={
                    "org_id": key[0],
                    "model": key[1],
                    "chosen_pool": len(chosen_ids),
                    "rejected_pool": len(rejected_ids),
                    "possible_pairs": possible_pairs,
                    "emitted": len(out.rows) - rows_before_bucket,
                    "cap": policy.max_pairs_per_bucket,
                },
            )
            out.skipped["bucket_capped"] += 1

    logger.info(
        "dpo_projected", extra={"rows": len(out.rows), "skipped": dict(out.skipped)}
    )
    return out


def to_export_rows(rows: Iterable[DPOPair]) -> list[ExportRow]:
    """Adapt ``DPOPair`` rows to ``jsonl.py``'s ``ExportRow`` destination
    shape — reused, not reforked (ADR 0004): the write path (atomic write,
    the empty-input no-truncate guard, split-aware naming) lives exactly
    once in ``jsonl.py``, for every projection."""
    return [ExportRow(split=row.metadata.split, body=asdict(row)) for row in rows]

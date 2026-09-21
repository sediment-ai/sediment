# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Recovery row projection — ``RecoverySample`` -> ``RecoveryRow``, the
JSONL training-row shape for red-to-green CI-transition recovery pairs (ADR
0004). Unlike DPO/SFT/diff-SFT, this is not a
projection over attributed_completions: ``RecoverySample`` (``sediment_derive.recovery``) is
itself the fully-vetted training-row candidate — every ancestry check, the
same-commit guard, and the diff size cap already ran at derivation time — so
this module's only job is the destination-shape adaptation every other
format gets: stamp the eval split and hand rows to ``jsonl.py``.
Every row names ``recovery_ci`` version 1. The projection doesn't reinterpret
CI facts; the recovery derivation supplies only clean red-to-green lineages.

**Split lives here, not on ``RecoverySample``.** Every other training-row
format inherits its split from a canonical artifact that already carries one
(a ``AttributedCompletion``'s or ``Rollout``'s ``split``, stamped once at assembly time,
ADR 0004). A recovery pair has no such upstream: it is derived directly from
``CIOutcome`` facts and the mirror, and ``RecoverySample`` (packages/derive,
outside this package's scope) carries no split field. This module stamps one
at projection time using the exact same primitive every other split
assignment uses (``sediment_derive.split.split_of``) — never a second hash,
never a fabricated field on the derive-owned dataclass.

**Split key: the attributed inference calls' sessions on BOTH sides, eval
wins.** A recovery pair's ``failed_inference_call_ids`` are the inference calls
notes/jaccard attribution attaches to the *failed* commit, and its
``fixed_inference_call_ids`` the inference calls attached to the *fixed* commit
(best-effort — ``recovery.py``'s own module docstring: absence is a fine,
expected outcome). The fix is as much session evidence as the mistake: a
captured session that authored the fixed commit must not see its work in
the train half of the holdout. Mirroring the multi-session rule
``split.py`` hands to any multi-constituent artifact (``dpo.py``'s
cross-session pair, "eval wins"), a recovery row is eval when ANY
resolvable inference call's session on EITHER side is eval, else train. An
``inference_call_id`` absent from the ``inference_calls`` lookup contributes
nothing to that computation — but it is no longer silent: every
unresolvable id on either side is counted in the projection's
``skipped["inference_call_not_found"]`` tally, mirroring ``dpo.py`` (rows are
never dropped over it; the tally exists so degradation is visible). A pair
with no resolvable inference calls has no session evidence either way
and defaults to train, matching the split-disabled default: no knowable
eval membership is never fabricated into one (house rule, "absent, never
guessed at").

**Known ceiling.** Fixes authored outside any captured session carry no
session evidence and fall to the train default — the holdout guarantee
covers captured sessions only (``_pair_split``'s ``ponytail:`` comment).

**Representation eligibility.** Each pair declines once under
``non_finite_number`` or ``unrepresentable_unicode`` if an emitted value cannot
be represented for training. Omitted source-call content has no effect.
``inference_call_not_found`` counts unresolved source IDs, and
``attribution_evidence_absent`` counts missing source records per side and call.
Neither source gap excludes an otherwise eligible row.
"""

from __future__ import annotations

from sediment_derive.repository_identity import RepositoryIdentity

import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Literal

from sediment_derive import (
    EVAL,
    TRAIN,
    InferenceCall,
    Provenance,
    RecoverySample,
    Split,
    split_of,
)

from sediment_derive.recovery import RecoveryAttributionEvidence


from .trainer import (
    TRAINING_REPRESENTATION_SKIP_REASONS,
    TrainingRepresentationSkipReason,
    TrainerMappingError,
    validate_training_representation,
)
from .jsonl import ExportRow
from .schema_identity import RECOVERY_ROW_SCHEMA_ID, RECOVERY_ROW_SCHEMA_VERSION

logger = logging.getLogger("sediment.export.recovery")

RecoveryRecipeId = Literal["recovery_ci"]
RecoveryRecipeVersion = Literal[1]
RecoveryProjectionSkipReason = (
    TrainingRepresentationSkipReason
    | Literal["inference_call_not_found", "attribution_evidence_absent"]
)
RECOVERY_RECIPE_ID: RecoveryRecipeId = "recovery_ci"
RECOVERY_RECIPE_VERSION: RecoveryRecipeVersion = 1
RECOVERY_PROJECTION_SKIP_REASONS: tuple[RecoveryProjectionSkipReason, ...] = (
    *TRAINING_REPRESENTATION_SKIP_REASONS,
    "inference_call_not_found",
    "attribution_evidence_absent",
)


@dataclass(frozen=True)
class RecoveryRow:
    """One recovery-pair training row: ``RecoverySample``'s fields plus the
    eval-split stamp — see the module docstring for why the split is
    computed here rather than carried on ``RecoverySample`` itself."""

    org_id: str
    recipe_id: RecoveryRecipeId
    recipe_version: RecoveryRecipeVersion
    repo: str
    branch: str
    workflow_name: str
    workflow_path: str | None
    failed_commit_sha: str
    fixed_commit_sha: str
    failed_outcome_id: str
    fixed_outcome_id: str
    recovery_diff: str
    failed_inference_call_ids: list[str]
    fixed_inference_call_ids: list[str]
    provenance: Provenance
    split: Split
    repository_identity: RepositoryIdentity | None = None
    failed_attribution_evidence: tuple[RecoveryAttributionEvidence, ...] = ()
    fixed_attribution_evidence: tuple[RecoveryAttributionEvidence, ...] = ()
    schema_id: Literal[RECOVERY_ROW_SCHEMA_ID] = RECOVERY_ROW_SCHEMA_ID
    schema_version: Literal[3] = RECOVERY_ROW_SCHEMA_VERSION


@dataclass
class Projection:
    """Mirrors ``dpo.Projection``/``sft.Projection``/``diff_sft.Projection``:
    the rows to write plus the skip tally (reason -> count) for the
    structured summary log line. Representation failures count once per pair;
    the tally's ``inference_call_not_found`` entry counts attributed inference-call
    ids — either side — absent from the ``inference_calls`` lookup, so the
    best-effort degradation is visible rather than silent."""

    rows: list[RecoveryRow] = field(default_factory=list)
    skipped: Counter[RecoveryProjectionSkipReason] = field(default_factory=Counter)


def _pair_split(
    sample: RecoverySample,
    inference_calls: Mapping[str, InferenceCall],
    eval_fraction: float,
) -> Split:
    # ponytail: fixes authored outside any captured session carry no session
    # evidence and fall to the train default — the holdout guarantee covers
    # captured sessions only. Upgrade path: none in-pipeline; closing it
    # needs a non-session authorship signal (e.g. forge actor identity),
    # which is capture-side work, not a projection knob.
    sessions = [
        inference_calls[cid].session_id
        for cid in (*sample.failed_inference_call_ids, *sample.fixed_inference_call_ids)
        if cid in inference_calls
    ]
    if any(split_of(session_id, eval_fraction) == EVAL for session_id in sessions):
        return EVAL
    return TRAIN


def project_recovery(
    pairs: Iterable[RecoverySample],
    inference_calls: Mapping[str, InferenceCall],
    eval_fraction: float = 0.1,
) -> Projection:
    """Project ``RecoverySample`` pairs into ``RecoveryRow`` training rows.

    ``inference_calls`` maps an id to its inference-call fact, the same
    contract ``dpo.project_dpo`` and ``sft.project_sft`` take — used here only to
    resolve each attributed inference call's ``session_id`` for the split
    computation (module docstring), never to gate emission. ``eval_fraction``
    comes from the canonical derivation policy. The caller threads it through,
    matching ``AttributedCompletionPolicy.eval_fraction``'s contract.
    """
    if not 0.0 <= eval_fraction <= 0.5:
        raise ValueError(
            f"eval_fraction must be between 0.0 and 0.5 (got {eval_fraction})."
        )
    out = Projection()
    for sample in pairs:
        missing_sources = sum(
            len(set(ids) - {item.inference_call_id for item in evidence})
            for ids, evidence in (
                (sample.failed_inference_call_ids, sample.failed_attribution_evidence),
                (sample.fixed_inference_call_ids, sample.fixed_attribution_evidence),
            )
        )
        if missing_sources:
            out.skipped["attribution_evidence_absent"] += missing_sources
        unresolvable = sum(
            1
            for cid in (
                *sample.failed_inference_call_ids,
                *sample.fixed_inference_call_ids,
            )
            if cid not in inference_calls
        )
        if unresolvable:
            # Counter += 0 would still materialize a zero entry; only tally a
            # real degradation.
            out.skipped["inference_call_not_found"] += unresolvable
        row = RecoveryRow(
            org_id=sample.org_id,
            recipe_id=RECOVERY_RECIPE_ID,
            recipe_version=RECOVERY_RECIPE_VERSION,
            repo=sample.repo,
            repository_identity=sample.repository_identity,
            branch=sample.branch,
            workflow_name=sample.workflow_name,
            workflow_path=sample.workflow_path,
            failed_commit_sha=sample.failed_commit_sha,
            fixed_commit_sha=sample.fixed_commit_sha,
            failed_outcome_id=sample.failed_outcome_id,
            fixed_outcome_id=sample.fixed_outcome_id,
            recovery_diff=sample.recovery_diff,
            failed_inference_call_ids=list(sample.failed_inference_call_ids),
            fixed_inference_call_ids=list(sample.fixed_inference_call_ids),
            provenance=sample.provenance,
            split=_pair_split(sample, inference_calls, eval_fraction),
            failed_attribution_evidence=sample.failed_attribution_evidence,
            fixed_attribution_evidence=sample.fixed_attribution_evidence,
        )
        try:
            validate_training_representation(row)
        except TrainerMappingError as exc:
            out.skipped[exc.reason] += 1
            continue
        out.rows.append(row)
    logger.info(
        "recovery_projected",
        extra={"rows": len(out.rows), "skipped": dict(out.skipped)},
    )
    return out


def to_export_rows(rows: Iterable[RecoveryRow]) -> list[ExportRow]:
    """Adapt ``RecoveryRow`` to ``jsonl.py``'s ``ExportRow`` destination
    shape — reused, not reforked (ADR 0004): the write path lives exactly
    once in ``jsonl.py``, for every projection."""
    return [ExportRow(split=row.split, body=asdict(row)) for row in rows]

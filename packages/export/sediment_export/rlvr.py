# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explicit Sediment, SWE-bench, and NeMo Gym RLVR projections.

Each target is a thin, stateless projection over canonical rollouts (ADR 0004).
Shared helpers resolve only evidence that all targets consume. Target-specific
row shapes, eligibility rules, and skip vocabularies remain separate because no
universal RLVR interchange schema exists.
"""

from __future__ import annotations

import logging
import os
import tempfile
from sediment_derive.repository_identity import (
    RepositoryContext,
    RepositoryIdentity,
    IdentifiedRepositoryKey,
    LegacyRepositoryKey,
    CommitKey,
    repository_identity_of,
)

from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .derived_bundle import DerivedBundle

from pydantic import BaseModel
from sediment_core import (
    CIOutcome,
    CIResult,
    AgentHarness,
    CommitSha,
    FactStore,
    InferenceMessage,
    InferenceMessagePart,
    InteractionMode,
    NonEmptyId,
    OrgId,
    RepoSlug,
    TextPart,
    ToolCallPart,
)
from sediment_derive import (
    AttributionSource,
    CI_RESOLUTION_SKIP_REASONS,
    CIResolution,
    CIResolutionPolicy,
    CIResolutionResult,
    CIResolutionSkipReason,
    MirrorError,
    MirrorManager,
    Provenance,
    Rollout,
    Split,
    Turn,
    derive_ci_resolution_result,
)

from .environment_manifest import (
    NemoGymRuntimeSettings,
    OpenEnvRuntimeSettings,
    build_manifest,
    write_manifest,
)
from .trainer import (
    TRAINING_REPRESENTATION_SKIP_REASONS,
    TrainingRepresentationSkipReason,
    TrainerMappingError,
    validate_training_representation,
)
from .jsonl import ExportRow, write_jsonl
from .staged_rows import ExportRowStore
from .schema_identity import (
    NEMO_GYM_ROLLOUT_ROW_SCHEMA_ID,
    NEMO_GYM_ROLLOUT_ROW_SCHEMA_VERSION,
    SEDIMENT_ROLLOUT_ROW_SCHEMA_ID,
    SEDIMENT_ROLLOUT_ROW_SCHEMA_VERSION,
    SEDIMENT_TASK_ROW_SCHEMA_ID,
    SEDIMENT_TASK_ROW_SCHEMA_VERSION,
    SWE_BENCH_TASK_ROW_SCHEMA_ID,
    SWE_BENCH_TASK_ROW_SCHEMA_VERSION,
)
from .verifier_commands import VerifierCommands, VerifierCommandsSettings

logger = logging.getLogger("sediment.export.rlvr")

# Stands in for an explicit non-text part when a typed message is rendered to
# a task prompt. TextPart content remains verbatim. The marker keeps the
# flattening *visibly* lossy — a consumer sees that something was there — rather
# than silently dropping structure that changes the instruction's meaning.
_OMITTED_MARKER = "[non-text content omitted]"

RLVRTarget = Literal["sediment", "swe-bench", "nemo-gym"]
RLVR_TARGETS: tuple[RLVRTarget, ...] = ("sediment", "swe-bench", "nemo-gym")
RLVREcipeId = Literal["rlvr_ci"]
RLVRRecipeVersion = Literal[1]
RLVRRewardSource = Literal["resolved_ci_pass", "resolved_ci_fail"]
RLVR_RECIPE_ID: RLVREcipeId = "rlvr_ci"
RLVR_RECIPE_VERSION: RLVRRecipeVersion = 1

_TARGET_MARKER_FILENAME = ".sediment-rlvr-target"
_EXPORT_LOCK_DIRECTORY = ".sediment-rlvr-export.lock"
_RLVR_ARTIFACT_FILENAMES = (
    "tasks.jsonl",
    "tasks.train.jsonl",
    "tasks.eval.jsonl",
    "rollouts.jsonl",
    "rollouts.train.jsonl",
    "rollouts.eval.jsonl",
    "environment.yaml",
)

PatchSkipReason = Literal[
    "mirror_absent",
    "no_commit_in_verifier_repo",
    "degenerate_commit_range",
    "root_commit_no_base",
    "reference_patch_unavailable",
]

SedimentTaskSkipReason = (
    CIResolutionSkipReason
    | TrainingRepresentationSkipReason
    | Literal[
        "no_attributed_commit",
        "missing_verifier_evidence",
        "mirror_absent",
        "no_commit_in_verifier_repo",
        "degenerate_commit_range",
        "root_commit_no_base",
        "reference_patch_unavailable",
    ]
)

SWEBenchSkipReason = (
    CIResolutionSkipReason
    | TrainingRepresentationSkipReason
    | Literal[
        "no_attributed_commit",
        "missing_verifier_evidence",
        "failed_reference_patch",
        "mirror_absent",
        "no_commit_in_verifier_repo",
        "degenerate_commit_range",
        "root_commit_no_base",
        "reference_patch_unavailable",
    ]
)

RolloutSkipReason = (
    CIResolutionSkipReason | TrainingRepresentationSkipReason | Literal["no_segments"]
)

SEDIMENT_TASK_SKIP_REASONS: tuple[SedimentTaskSkipReason, ...] = (
    *CI_RESOLUTION_SKIP_REASONS,
    *TRAINING_REPRESENTATION_SKIP_REASONS,
    "no_attributed_commit",
    "missing_verifier_evidence",
    "mirror_absent",
    "no_commit_in_verifier_repo",
    "degenerate_commit_range",
    "root_commit_no_base",
    "reference_patch_unavailable",
)
SWE_BENCH_SKIP_REASONS: tuple[SWEBenchSkipReason, ...] = (
    *CI_RESOLUTION_SKIP_REASONS,
    *TRAINING_REPRESENTATION_SKIP_REASONS,
    "no_attributed_commit",
    "missing_verifier_evidence",
    "failed_reference_patch",
    "mirror_absent",
    "no_commit_in_verifier_repo",
    "degenerate_commit_range",
    "root_commit_no_base",
    "reference_patch_unavailable",
)
ROLLOUT_SKIP_REASONS: tuple[RolloutSkipReason, ...] = (
    *CI_RESOLUTION_SKIP_REASONS,
    *TRAINING_REPRESENTATION_SKIP_REASONS,
    "no_segments",
)


@dataclass(frozen=True)
class Verification:
    """Operator configuration for running a verifier again."""

    verification_command: str


@dataclass(frozen=True)
class RLVRDecisionRow:
    """One developer decision attached to an RLVR turn."""

    accepted: bool
    explicit: bool
    agent_harness: AgentHarness
    interaction_mode: InteractionMode
    file_path: str


@dataclass(frozen=True)
class RLVRInferenceMessageRow:
    """One structured message serialized for an RLVR trajectory."""

    role: str
    parts: list[InferenceMessagePart]
    finish_reason: str | None = field(metadata={"omit_none": True})


@dataclass(frozen=True)
class RLVRTurnRow:
    """One closed trajectory turn in an RLVR rollout row."""

    inference_call_id: NonEmptyId
    new_messages: list[RLVRInferenceMessageRow]
    completion: str
    tool_calls: list[ToolCallPart]
    decisions: list[RLVRDecisionRow]


@dataclass(frozen=True)
class SedimentTaskRow:
    """One lossless Sediment audit task."""

    instance_id: NonEmptyId
    recipe_id: RLVREcipeId
    recipe_version: RLVRRecipeVersion
    reward_source: RLVRRewardSource
    repo: RepoSlug
    base_commit: CommitSha
    problem_statement: str
    reference_patch: str
    verification: Verification | None = field(metadata={"omit_none": True})
    verifier_results: list[CIOutcome]
    ci_resolution: CIResolution
    attribution_source: AttributionSource
    split: Split
    provenance: Provenance
    session_commit_observation_ids: tuple[NonEmptyId, ...] = ()
    repository_identity: RepositoryIdentity | None = None
    schema_id: Literal[SEDIMENT_TASK_ROW_SCHEMA_ID] = SEDIMENT_TASK_ROW_SCHEMA_ID
    schema_version: Literal[3] = SEDIMENT_TASK_ROW_SCHEMA_VERSION


@dataclass(frozen=True)
class SedimentRolloutRow:
    """One lossless Sediment rollout segment."""

    instance_id: NonEmptyId
    recipe_id: RLVREcipeId
    recipe_version: RLVRRecipeVersion
    reward_source: RLVRRewardSource | None = field(metadata={"omit_none": True})
    ci_resolution: CIResolution | None = field(metadata={"omit_none": True})
    org_id: OrgId
    session_id: NonEmptyId
    segment_index: int
    turns: list[RLVRTurnRow]
    verifier_results: list[CIOutcome]
    ci_resolutions: list[CIResolution]
    attribution_source: AttributionSource
    split: Split
    provenance: Provenance
    session_commit_observation_ids: tuple[NonEmptyId, ...] = ()
    repository_identity: RepositoryIdentity | None = None
    schema_id: Literal[SEDIMENT_ROLLOUT_ROW_SCHEMA_ID] = SEDIMENT_ROLLOUT_ROW_SCHEMA_ID
    schema_version: Literal[4] = SEDIMENT_ROLLOUT_ROW_SCHEMA_VERSION


@dataclass(frozen=True)
class SWEBenchMetadata:
    """Sediment evidence kept outside SWE-bench trainer inputs."""

    recipe_id: RLVREcipeId
    recipe_version: RLVRRecipeVersion
    reward_source: Literal["resolved_ci_pass"]
    verification: Verification | None = field(metadata={"omit_none": True})
    verifier_results: list[CIOutcome]
    ci_resolution: CIResolution
    attribution_source: AttributionSource
    split: Split
    provenance: Provenance
    session_commit_observation_ids: tuple[NonEmptyId, ...] = ()
    repository_identity: RepositoryIdentity | None = None
    schema_id: Literal[SWE_BENCH_TASK_ROW_SCHEMA_ID] = SWE_BENCH_TASK_ROW_SCHEMA_ID
    schema_version: Literal[3] = SWE_BENCH_TASK_ROW_SCHEMA_VERSION


@dataclass(frozen=True)
class SWEBenchTaskRow:
    """One passing historical patch in the SWE-bench task shape."""

    instance_id: NonEmptyId
    repo: RepoSlug
    base_commit: CommitSha
    problem_statement: str
    patch: str
    metadata: SWEBenchMetadata


@dataclass(frozen=True)
class NemoGymMetadata:
    """Sediment evidence kept beside one NeMo Gym rollout mapping."""

    instance_id: NonEmptyId
    recipe_id: RLVREcipeId
    recipe_version: RLVRRecipeVersion
    reward_source: RLVRRewardSource | None = field(metadata={"omit_none": True})
    org_id: OrgId
    session_id: NonEmptyId
    segment_index: int
    verification: Verification | None = field(metadata={"omit_none": True})
    verifier_results: list[CIOutcome]
    ci_resolution: CIResolution | None = field(metadata={"omit_none": True})
    attribution_source: AttributionSource
    split: Split
    provenance: Provenance
    session_commit_observation_ids: tuple[NonEmptyId, ...] = ()
    repository_identity: RepositoryIdentity | None = None
    schema_id: Literal[NEMO_GYM_ROLLOUT_ROW_SCHEMA_ID] = NEMO_GYM_ROLLOUT_ROW_SCHEMA_ID
    schema_version: Literal[4] = NEMO_GYM_ROLLOUT_ROW_SCHEMA_VERSION


@dataclass(frozen=True)
class NemoGymResponsesCreateParams:
    """The NeMo Gym request parameters captured for one segment."""

    input: list[RLVRInferenceMessageRow]


@dataclass(frozen=True)
class NemoGymResponse:
    """The NeMo Gym response trajectory for one segment."""

    turns: list[RLVRTurnRow]


@dataclass(frozen=True)
class NemoGymRolloutRow:
    """One captured segment in the NeMo Gym rollout boundary shape."""

    responses_create_params: NemoGymResponsesCreateParams
    response: NemoGymResponse
    reward: float | None = field(metadata={"omit_none": True})
    metadata: NemoGymMetadata


@dataclass(frozen=True)
class _PatchEvidence:
    repo: RepoSlug
    first_commit: CommitSha
    base_commit: CommitSha
    reference_patch: str


@dataclass
class Projection:
    """The result of a projection: the rows to write, plus the skip tally
    (reason -> count) for the structured summary log line. ``rows`` is already
    in deterministic organization-and-session order."""

    rows: list[ExportRow] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)


def _first_user_message(rollout: Rollout) -> str:
    """The first user message, retaining each canonical TextPart verbatim.
    Scans segments then turns then messages in trajectory order; ``""``
    if the session has none (e.g. a tool-only or system-only opener)."""
    for segment in rollout.segments:
        for turn in segment:
            for message in turn.new_messages:
                if message.role == "user":
                    return "\n".join(
                        part.content if isinstance(part, TextPart) else _OMITTED_MARKER
                        for part in message.parts
                    )
    return ""


def _qualified_commit(org_id, record, repository_context):
    if repository_context is not None:
        return repository_context.commit_key(
            org_id,
            record.repo,
            record.commit_sha,
            repository_identity=record.repository_identity,
        )
    key = (
        LegacyRepositoryKey(org_id, record.repo)
        if record.repository_identity is None
        else IdentifiedRepositoryKey(org_id, record.repository_identity)
    )
    return CommitKey(key, record.commit_sha)


def _reward_resolution(
    rollout: Rollout,
    policy: CIResolutionPolicy,
    *,
    repository_context: RepositoryContext | None = None,
) -> tuple[CIResolutionResult, CIResolution | None]:
    """Return the last commit's attempt-aware CI resolution."""

    result = derive_ci_resolution_result(
        rollout.terminal_outcomes,
        policy,
        quarantine_revision=rollout.provenance.quarantine_revision,
        repository_context=repository_context,
    )
    positions = {
        _qualified_commit(rollout.org_id, commit, repository_context): index
        for index, commit in enumerate(rollout.commits)
    }
    candidates = [
        resolution
        for resolution in result.resolutions
        if _qualified_commit(rollout.org_id, resolution, repository_context) is not None
        and _qualified_commit(rollout.org_id, resolution, repository_context)
        in positions
        and resolution.verdict is not None
    ]
    if not candidates:
        return result, None
    return result, max(
        candidates,
        key=lambda resolution: positions[
            _qualified_commit(rollout.org_id, resolution, repository_context)
        ],
    )


def _resolution_evidence(rollout: Rollout, resolution: CIResolution) -> list[CIOutcome]:
    source_ids = set(resolution.source_outcome_ids)
    return sorted(
        (
            outcome
            for outcome in rollout.terminal_outcomes
            if outcome.outcome_id in source_ids
        ),
        key=lambda outcome: outcome.outcome_id,
    )


def _reward_source(resolution: CIResolution | None) -> RLVRRewardSource | None:
    if resolution is None or resolution.verdict is None:
        return None
    if resolution.verdict == CIResult.PASSED:
        return "resolved_ci_pass"
    return "resolved_ci_fail"


def _json_value(value: object) -> object:
    """Serialize one declared RLVR contract without changing its semantics."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        body: dict[str, object] = {}
        for declared_field in fields(value):
            field_value = getattr(value, declared_field.name)
            if declared_field.metadata.get("omit_none") and field_value is None:
                continue
            body[declared_field.name] = _json_value(field_value)
        return body
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_json_value(item) for item in value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _body(value: object) -> dict:
    """Serialize a closed target row while omitting declared absent fields."""
    body = _json_value(value)
    assert isinstance(body, dict)
    return body


def _verification(repo: RepoSlug, commands: VerifierCommands) -> Verification | None:
    command = commands.for_repo(repo)
    return Verification(command) if command is not None else None


def _resolve_reference_patch(
    rollout: Rollout,
    mirrors: MirrorManager,
    repo: RepoSlug,
    verifier_commit: CommitSha,
    *,
    repository_context: RepositoryContext | None = None,
    repository_identity: RepositoryIdentity | None = None,
) -> tuple[_PatchEvidence | None, PatchSkipReason | None]:
    """Resolve one observed historical patch without target eligibility rules."""
    key = (
        LegacyRepositoryKey(rollout.org_id, repo)
        if repository_identity is None
        else IdentifiedRepositoryKey(rollout.org_id, repository_identity)
    )
    if (
        repository_context is not None
        and repository_context.resolve_reference(
            rollout.org_id, repo, repository_identity=repository_identity
        ).key
        != key
    ):
        return None, "no_commit_in_verifier_repo"
    mirror = mirrors.open_repository(key)
    if mirror is None:
        return None, "mirror_absent"

    in_repo = [
        commit.commit_sha
        for commit in rollout.commits
        if _qualified_commit(rollout.org_id, commit, repository_context)
        == CommitKey(key, commit.commit_sha)
        and mirror.commit_exists(commit.commit_sha)
    ]
    if not in_repo:
        return None, "no_commit_in_verifier_repo"
    if verifier_commit not in in_repo:
        return None, "no_commit_in_verifier_repo"
    first, last = in_repo[0], verifier_commit

    try:
        linear = mirror.is_ancestor(first, last)
    except MirrorError as exc:
        logger.warning(
            "reference_patch_range_uncheckable",
            extra={"repo": repo, "first": first, "last": last, "error": str(exc)},
        )
        return None, "reference_patch_unavailable"
    if not linear:
        return None, "degenerate_commit_range"

    try:
        base = mirror.parent_commit(first)
    except MirrorError as exc:
        logger.warning(
            "reference_patch_base_unavailable",
            extra={"repo": repo, "commit": first, "error": str(exc)},
        )
        return None, "reference_patch_unavailable"
    if base is None:
        return None, "root_commit_no_base"

    try:
        patch = mirror.diff_range(base, last)
    except MirrorError as exc:
        logger.warning(
            "reference_patch_unavailable",
            extra={"repo": repo, "base": base, "head": last, "error": str(exc)},
        )
        return None, "reference_patch_unavailable"
    return _PatchEvidence(repo, first, base, patch), None


def _ordered_rollouts(rollouts: Iterable[Rollout]) -> list[Rollout]:
    return sorted(rollouts, key=lambda rollout: (rollout.org_id, rollout.session_id))


def _validate_or_create_target_marker(out: Path, target: RLVRTarget) -> None:
    marker = out / _TARGET_MARKER_FILENAME
    if marker.exists():
        try:
            claimed = marker.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"cannot read RLVR target claim {marker}: {exc}") from exc
        if claimed != target:
            raise ValueError(
                f"output directory already belongs to RLVR target {claimed or 'unknown'}; "
                f"use a different directory for {target}"
            )
        existing = [name for name in _RLVR_ARTIFACT_FILENAMES if (out / name).exists()]
        if existing:
            raise ValueError(
                f"output directory already contains {target} artifacts; "
                "use a new directory for each export"
            )
        return

    stale = [name for name in _RLVR_ARTIFACT_FILENAMES if (out / name).exists()]
    if stale:
        raise ValueError(
            "output directory contains RLVR artifacts without a target claim; "
            "use a new directory"
        )

    fd, tmp_name = tempfile.mkstemp(
        dir=out, prefix=_TARGET_MARKER_FILENAME, suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{target}\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, marker)
        except FileExistsError:
            _validate_or_create_target_marker(out, target)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


@contextmanager
def _claim_output_target(out: Path, target: RLVRTarget) -> Iterator[None]:
    """Hold one fail-closed output-generation claim for the complete export."""
    lock = out / _EXPORT_LOCK_DIRECTORY
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise ValueError(
            f"RLVR export already in progress for output directory {out}"
        ) from exc
    try:
        _validate_or_create_target_marker(out, target)
        yield
    finally:
        try:
            lock.rmdir()
        except OSError as exc:
            logger.warning(
                "rlvr_export_lock_cleanup_failed",
                extra={"path": str(lock), "error": str(exc)},
            )


def project_sediment_tasks(
    rollouts: Iterable[Rollout],
    mirrors: MirrorManager,
    verifier_commands: VerifierCommands,
    ci_resolution_policy: CIResolutionPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> Projection:
    """Project training-representable Sediment tasks for passes and failures.

    Requires trusted canonical assembly or a validated bundle; source identity
    completeness cannot be established from Rollouts alone.
    """
    out = Projection()
    ci_resolution_policy = ci_resolution_policy or CIResolutionPolicy()

    for rollout in _ordered_rollouts(rollouts):
        if not rollout.commits:
            out.skipped["no_attributed_commit"] += 1
            continue
        ci_result, resolution = _reward_resolution(
            rollout, ci_resolution_policy, repository_context=repository_context
        )
        out.skipped.update(ci_result.skipped)
        if resolution is None or resolution.verdict is None:
            out.skipped["missing_verifier_evidence"] += 1
            continue
        evidence, reason = _resolve_reference_patch(
            rollout,
            mirrors,
            resolution.repo,
            resolution.commit_sha,
            repository_identity=resolution.repository_identity,
            repository_context=repository_context,
        )
        if evidence is None:
            assert reason is not None
            out.skipped[reason] += 1
            continue
        reward_source = _reward_source(resolution)
        assert reward_source is not None
        row = SedimentTaskRow(
            instance_id=(
                f"{rollout.org_id}-{rollout.session_id}-{evidence.first_commit[:7]}"
            ),
            recipe_id=RLVR_RECIPE_ID,
            recipe_version=RLVR_RECIPE_VERSION,
            reward_source=reward_source,
            repo=evidence.repo,
            base_commit=evidence.base_commit,
            problem_statement=_first_user_message(rollout),
            reference_patch=evidence.reference_patch,
            verification=_verification(evidence.repo, verifier_commands),
            verifier_results=_resolution_evidence(rollout, resolution),
            ci_resolution=resolution,
            repository_identity=resolution.repository_identity
            if resolution is not None
            else None,
            attribution_source=rollout.attribution_source,
            session_commit_observation_ids=_rollout_observation_ids(
                rollout,
                [resolution] if resolution is not None else [],
                repository_context=repository_context,
            ),
            split=rollout.split,
            provenance=rollout.provenance,
        )
        try:
            validate_training_representation(row)
        except TrainerMappingError as exc:
            out.skipped[exc.reason] += 1
            continue
        out.rows.append(ExportRow(split=rollout.split, body=_body(row)))
    logger.info(
        "sediment_tasks_projected",
        extra={"rows": len(out.rows), "skipped": dict(out.skipped)},
    )
    return out


def project_swe_bench_tasks(
    rollouts: Iterable[Rollout],
    mirrors: MirrorManager,
    verifier_commands: VerifierCommands,
    ci_resolution_policy: CIResolutionPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
    row_sink: Callable[[ExportRow], None] | None = None,
) -> Projection:
    """Project SWE-bench tasks only from recorded terminal passing patches.

    Requires trusted canonical assembly or a validated bundle; source identity
    completeness cannot be established from Rollouts alone. A row sink stages
    each admitted row instead of retaining it in the result. The caller must
    discard staged rows if projection fails; sink errors propagate unchanged.
    """
    out = Projection()
    emitted = 0
    ci_resolution_policy = ci_resolution_policy or CIResolutionPolicy()
    for rollout in _ordered_rollouts(rollouts):
        if not rollout.commits:
            out.skipped["no_attributed_commit"] += 1
            continue
        ci_result, resolution = _reward_resolution(
            rollout, ci_resolution_policy, repository_context=repository_context
        )
        out.skipped.update(ci_result.skipped)
        if resolution is None or resolution.verdict is None:
            out.skipped["missing_verifier_evidence"] += 1
            continue
        if resolution.verdict == CIResult.FAILED:
            out.skipped["failed_reference_patch"] += 1
            continue
        evidence, reason = _resolve_reference_patch(
            rollout,
            mirrors,
            resolution.repo,
            resolution.commit_sha,
            repository_identity=resolution.repository_identity,
            repository_context=repository_context,
        )
        if evidence is None:
            assert reason is not None
            out.skipped[reason] += 1
            continue
        reward_source = _reward_source(resolution)
        assert reward_source == "resolved_ci_pass"
        metadata = SWEBenchMetadata(
            recipe_id=RLVR_RECIPE_ID,
            recipe_version=RLVR_RECIPE_VERSION,
            reward_source=reward_source,
            verification=_verification(evidence.repo, verifier_commands),
            verifier_results=_resolution_evidence(rollout, resolution),
            ci_resolution=resolution,
            repository_identity=resolution.repository_identity
            if resolution is not None
            else None,
            attribution_source=rollout.attribution_source,
            session_commit_observation_ids=_rollout_observation_ids(
                rollout,
                [resolution] if resolution is not None else [],
                repository_context=repository_context,
            ),
            split=rollout.split,
            provenance=rollout.provenance,
        )
        row = SWEBenchTaskRow(
            instance_id=(
                f"{rollout.org_id}-{rollout.session_id}-{evidence.first_commit[:7]}"
            ),
            repo=evidence.repo,
            base_commit=evidence.base_commit,
            problem_statement=_first_user_message(rollout),
            patch=evidence.reference_patch,
            metadata=metadata,
        )
        try:
            validate_training_representation(row)
        except TrainerMappingError as exc:
            out.skipped[exc.reason] += 1
            continue
        export_row = ExportRow(split=rollout.split, body=_body(row))
        if row_sink is None:
            out.rows.append(export_row)
        else:
            row_sink(export_row)
        emitted += 1
    logger.info(
        "swe_bench_tasks_projected",
        extra={"rows": emitted, "skipped": dict(out.skipped)},
    )
    return out


def _message_row(message: InferenceMessage) -> RLVRInferenceMessageRow:
    return RLVRInferenceMessageRow(
        role=message.role,
        parts=list(message.parts),
        finish_reason=message.finish_reason,
    )


def _turn_row(turn: Turn) -> RLVRTurnRow:
    """One turn with native messages, scoring text, and response tool calls."""
    return RLVRTurnRow(
        inference_call_id=turn.inference_call_id,
        new_messages=[_message_row(message) for message in turn.new_messages],
        completion=turn.completion,
        tool_calls=list(turn.tool_calls),
        decisions=[
            RLVRDecisionRow(
                accepted=decision.accepted,
                explicit=decision.explicit,
                agent_harness=decision.agent_harness,
                interaction_mode=decision.interaction_mode,
                file_path=decision.file_path,
            )
            for decision in turn.decisions
        ],
    )


def _all_verifier_results(rollout: Rollout) -> list[CIOutcome]:
    """Return every exact CI fact in deterministic fact-id order."""

    return sorted(
        rollout.terminal_outcomes,
        key=lambda outcome: outcome.outcome_id,
    )


def project_sediment_rollouts(
    rollouts: Iterable[Rollout],
    ci_resolution_policy: CIResolutionPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> Projection:
    """Project eligible Sediment rollout rows, one per contiguous segment.

    Requires trusted canonical assembly or a validated bundle; source identity
    completeness cannot be established from Rollouts alone.
    """
    out = Projection()
    ci_resolution_policy = ci_resolution_policy or CIResolutionPolicy()
    for rollout in _ordered_rollouts(rollouts):
        if not rollout.segments:
            out.skipped["no_segments"] += 1
            continue
        ci_result, resolution = _reward_resolution(
            rollout, ci_resolution_policy, repository_context=repository_context
        )
        out.skipped.update(ci_result.skipped)
        verifier_results = _all_verifier_results(rollout)
        for index, segment in enumerate(rollout.segments):
            row = SedimentRolloutRow(
                instance_id=f"{rollout.org_id}-{rollout.session_id}-seg{index}",
                recipe_id=RLVR_RECIPE_ID,
                recipe_version=RLVR_RECIPE_VERSION,
                reward_source=_reward_source(resolution),
                ci_resolution=resolution,
                repository_identity=resolution.repository_identity
                if resolution is not None
                else None,
                org_id=rollout.org_id,
                session_id=rollout.session_id,
                segment_index=index,
                turns=[_turn_row(turn) for turn in segment],
                verifier_results=verifier_results,
                ci_resolutions=list(ci_result.resolutions),
                attribution_source=rollout.attribution_source,
                session_commit_observation_ids=_rollout_observation_ids(
                    rollout,
                    ci_result.resolutions,
                    repository_context=repository_context,
                ),
                split=rollout.split,
                provenance=rollout.provenance,
            )
            try:
                validate_training_representation(row)
            except TrainerMappingError as exc:
                out.skipped[exc.reason] += 1
                continue
            out.rows.append(ExportRow(split=rollout.split, body=_body(row)))

    logger.info(
        "sediment_rollouts_projected",
        extra={"rows": len(out.rows), "skipped": dict(out.skipped)},
    )
    return out


def _nemo_verification(
    rollout: Rollout,
    commands: VerifierCommands,
    resolution: CIResolution | None,
) -> Verification | None:
    if resolution is not None:
        return _verification(resolution.repo, commands)
    repos = {result.repo for result in rollout.terminal_outcomes}
    if not repos:
        repos = {commit.repo for commit in rollout.commits}
    if len(repos) == 1:
        return _verification(repos.pop(), commands)
    return None


def project_nemo_gym_rollouts(
    rollouts: Iterable[Rollout],
    verifier_commands: VerifierCommands,
    ci_resolution_policy: CIResolutionPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
    row_sink: Callable[[ExportRow], None] | None = None,
) -> Projection:
    """Map captured segments onto the NeMo Gym rollout boundary fields.

    Requires trusted canonical assembly or a validated bundle; source identity
    completeness cannot be established from Rollouts alone. A row sink stages
    each admitted row instead of retaining it in the result. The caller must
    discard staged rows if projection fails; sink errors propagate unchanged.
    """
    out = Projection()
    emitted = 0
    ci_resolution_policy = ci_resolution_policy or CIResolutionPolicy()
    for rollout in _ordered_rollouts(rollouts):
        if not rollout.segments:
            out.skipped["no_segments"] += 1
            continue
        ci_result, resolution = _reward_resolution(
            rollout, ci_resolution_policy, repository_context=repository_context
        )
        out.skipped.update(ci_result.skipped)
        reward = None
        if resolution is not None and resolution.verdict is not None:
            reward = 1.0 if resolution.verdict == CIResult.PASSED else 0.0
        verifier_results = _all_verifier_results(rollout)
        verification = _nemo_verification(rollout, verifier_commands, resolution)
        for index, segment in enumerate(rollout.segments):
            first_messages = (
                [_message_row(message) for message in segment[0].new_messages]
                if segment
                else []
            )
            instance_id = f"{rollout.org_id}-{rollout.session_id}-seg{index}"
            metadata = NemoGymMetadata(
                instance_id=instance_id,
                recipe_id=RLVR_RECIPE_ID,
                recipe_version=RLVR_RECIPE_VERSION,
                reward_source=_reward_source(resolution),
                org_id=rollout.org_id,
                session_id=rollout.session_id,
                segment_index=index,
                verification=verification,
                verifier_results=verifier_results,
                ci_resolution=resolution,
                repository_identity=resolution.repository_identity
                if resolution is not None
                else None,
                attribution_source=rollout.attribution_source,
                session_commit_observation_ids=_rollout_observation_ids(
                    rollout,
                    [resolution] if resolution is not None else [],
                    repository_context=repository_context,
                ),
                split=rollout.split,
                provenance=rollout.provenance,
            )
            row = NemoGymRolloutRow(
                responses_create_params=NemoGymResponsesCreateParams(
                    input=first_messages
                ),
                response=NemoGymResponse(turns=[_turn_row(turn) for turn in segment]),
                reward=reward,
                metadata=metadata,
            )
            try:
                validate_training_representation(row)
            except TrainerMappingError as exc:
                out.skipped[exc.reason] += 1
                continue
            export_row = ExportRow(split=rollout.split, body=_body(row))
            if row_sink is None:
                out.rows.append(export_row)
            else:
                row_sink(export_row)
            emitted += 1

    logger.info(
        "nemo_gym_rollouts_projected",
        extra={"rows": emitted, "skipped": dict(out.skipped)},
    )
    return out


def export_rlvr(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: OrgId,
    out_dir: str | Path,
    *,
    target: RLVRTarget,
) -> dict:
    """Derive canonical rollouts and write one explicit RLVR target."""
    from .derived_bundle import DerivationPolicy, build_derived_bundle_context

    with build_derived_bundle_context(
        store,
        mirrors,
        org_id,
        policy=DerivationPolicy(),
    ) as bundle:
        return export_rlvr_from_bundle(bundle, mirrors, out_dir, target=target)


def export_rlvr_from_bundle(
    bundle: DerivedBundle,
    mirrors: MirrorManager | None,
    out_dir: str | Path,
    *,
    target: RLVRTarget,
) -> dict:
    """Validate a complete declared bundle before projecting or publishing RLVR."""
    from .derived_bundle import validate_derived_bundle

    repository_context = validate_derived_bundle(bundle)
    summary = export_rlvr_from_rollouts(
        bundle.rollouts,
        mirrors,
        out_dir,
        target=target,
        split_enabled=bundle.policy.eval_fraction > 0,
        repository_context=repository_context,
    )
    summary["fragmented"] = dict(bundle.fragmented)
    return summary


def export_rlvr_from_rollouts(
    rollouts: Sequence[Rollout],
    mirrors: MirrorManager | None,
    out_dir: str | Path,
    *,
    target: RLVRTarget,
    split_enabled: bool,
    repository_context: RepositoryContext | None = None,
) -> dict:
    """Project and write one RLVR target from trusted canonical Rollouts.

    Requires canonical assembly or a validated bundle. This low-level API has
    no source identity population; use ``export_rlvr_from_bundle`` for bundles.

    ``mirrors`` resolves reference patches for the ``sediment`` and ``swe-bench``
    targets, which read the bare git mirror even from a frozen bundle because raw
    git diffs are not carried in a derived bundle. It may be ``None`` only for
    ``target="nemo-gym"``, whose projection is mirror-free: it projects purely
    from captured Rollout Segments. Passing ``None`` for any target that
    resolves reference patches is a programming error.

    A repeatable file-backed Sequence keeps only the active complete Rollout
    resident. Every selected artifact is prepared privately before publication;
    filesystem replacements remain atomic per file, not across all files.
    """

    if target not in RLVR_TARGETS:
        raise ValueError(f"unsupported RLVR target: {target}")
    if mirrors is None and target != "nemo-gym":
        raise ValueError(
            f"mirrors must be supplied for target={target!r}; "
            "only 'nemo-gym' projects without a mirror"
        )
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (
        _claim_output_target(out, target),
        ExportRowStore(temporary_parent=out) as stage,
    ):
        commands = VerifierCommandsSettings().resolve()
        runtime_settings = OpenEnvRuntimeSettings() if target == "sediment" else None
        nemo_settings = NemoGymRuntimeSettings() if target == "sediment" else None
        tasks = stage.records("tasks")
        trajectories = stage.records("rollouts")
        task_skipped, trajectory_skipped = Counter(), Counter()
        # Stable ordinal ties preserve duplicate Rollouts without retaining them.
        ordered = []
        for index in range(len(rollouts)):
            row = rollouts[index]
            ordered.append((row.org_id, row.session_id, index))
            del row
        ordered.sort()
        for _, _, index in ordered:
            rollout = rollouts[index]
            if target == "sediment":
                projection = project_sediment_tasks(
                    (rollout,), mirrors, commands, repository_context=repository_context
                )
                tasks.extend(projection.rows)
                task_skipped.update(projection.skipped)
                del projection
                projection = project_sediment_rollouts(
                    (rollout,), repository_context=repository_context
                )
                trajectories.extend(projection.rows)
                trajectory_skipped.update(projection.skipped)
            elif target == "swe-bench":
                projection = project_swe_bench_tasks(
                    (rollout,), mirrors, commands, repository_context=repository_context
                )
                tasks.extend(projection.rows)
                task_skipped.update(projection.skipped)
            else:
                projection = project_nemo_gym_rollouts(
                    (rollout,), commands, repository_context=repository_context
                )
                trajectories.extend(projection.rows)
                trajectory_skipped.update(projection.skipped)
            del rollout, projection
        tasks.seal()
        trajectories.seal()

        prepared = stage.directory / "prepared"
        prepared.mkdir(mode=0o700)
        remaining = (
            stage.limits.max_staging_bytes
            - tasks.encoded_bytes
            - trajectories.encoded_bytes
        )
        prepared_files: dict[str, int] = {}
        if target != "nemo-gym":
            prepared_files.update(
                write_jsonl(
                    tasks,
                    prepared / "tasks.jsonl",
                    split_enabled=split_enabled,
                    max_bytes=remaining,
                ).written
            )
            remaining -= sum(Path(path).stat().st_size for path in prepared_files)
        if target != "swe-bench":
            trajectory_files = write_jsonl(
                trajectories,
                prepared / "rollouts.jsonl",
                split_enabled=split_enabled,
                max_bytes=remaining,
            ).written
            prepared_files.update(trajectory_files)
            remaining -= sum(Path(path).stat().st_size for path in trajectory_files)
        prepared_manifest = None
        if target == "sediment":
            manifest = build_manifest(
                tasks,
                runtime_settings,
                split_enabled=split_enabled,
                nemo_gym_settings=nemo_settings,
            )
            # Bound the small taskset metadata before writing its prepared file.
            if manifest is not None:
                import yaml
                from sediment_core import OperationalReportLimitExceeded

                manifest_bytes = len(
                    yaml.safe_dump(
                        manifest, sort_keys=False, default_flow_style=False
                    ).encode("utf-8")
                )
                if manifest_bytes > remaining:
                    raise OperationalReportLimitExceeded(
                        "RLVR manifest exceeds remaining encoded byte budget"
                    )
            prepared_manifest = write_manifest(manifest, prepared)

        written: dict[str, int] = {}
        for source, count in prepared_files.items():
            destination = out / Path(source).name
            os.replace(source, destination)
            written[str(destination)] = count
        manifest_path = None
        if prepared_manifest is not None:
            manifest_path = out / prepared_manifest.name
            os.replace(prepared_manifest, manifest_path)
        return {
            "target": target,
            "rollouts": len(rollouts),
            "task_rows": len(tasks),
            "task_skipped": dict(task_skipped),
            "rollout_rows": len(trajectories),
            "rollout_skipped": dict(trajectory_skipped),
            "written": written,
            "environment_manifest": str(manifest_path) if manifest_path else None,
        }


def _rollout_observation_ids(
    rollout: Rollout,
    resolutions: Iterable[CIResolution],
    *,
    repository_context: RepositoryContext | None = None,
) -> tuple[str, ...]:
    """Keep original observation sources on the selected qualified verifier edges."""
    commits = {
        _qualified_commit(rollout.org_id, item, repository_context)
        for item in resolutions
    }
    commits.intersection_update(
        _qualified_commit(rollout.org_id, item, repository_context)
        for item in rollout.commits
    )
    commits.discard(None)
    ids = set()
    for item in rollout.session_commit_observations:
        if (item.org_id, item.session_id) != (rollout.org_id, rollout.session_id):
            continue
        if repository_context is not None:
            key = repository_context.resolve_fact(item).key
        else:
            identity = repository_identity_of(item)
            key = (
                LegacyRepositoryKey(item.org_id, item.repo)
                if identity is None
                else IdentifiedRepositoryKey(item.org_id, identity)
            )
        if key is not None and CommitKey(key, item.commit_sha) in commits:
            ids.add(item.observation_id)
    return tuple(sorted(ids))

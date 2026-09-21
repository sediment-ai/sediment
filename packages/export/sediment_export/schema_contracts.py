# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical serialized-contract registry for schema publication."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sediment_core import (
    CIOutcome,
    DeveloperDecision,
    EditObservation,
    InferenceCall,
    InferenceMessage,
    PullRequestMerge,
    PullRequestRevision,
    Push,
    QuarantineRecord,
    ReasoningPart,
    RejectedEdit,
    RepositoryRename,
    RepositoryIdentityEvidence,
    RetryLinkage,
    SessionCommitObservation,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)
from sediment_derive import (
    AcceptedSessionOutcome,
    Attribution,
    CIResolution,
    CIWorkflowResolution,
    CommitRef,
    Fate,
    MergeMembershipOutcome,
    MergeRetention,
    Provenance,
    RecoverySample,
    Rollout,
    SessionAbandonment,
    Turn,
)

from sediment_derive.recovery import RecoveryAttributionEvidence
from sediment_derive.repository_identity import RepositoryIdentity
from sediment_core.store import InferenceCallIdentity

from .attributed_completions import AbandonmentSummary, AttributedCompletion
from .accepted_work_lifecycle import AcceptedWorkLifecycleReport
from .derived_bundle import BundleFileMetadata, BundleManifest, BundleRecord
from .diff_sft import DiffSFTMetadata, DiffSFTSample, SourceIds
from .dpo import DPOMetadata, DPOPair, DPOProvenance
from .price_manifest import PriceManifest
from .recovery import RecoveryRow
from .rlvr import (
    NemoGymMetadata,
    NemoGymResponse,
    NemoGymResponsesCreateParams,
    NemoGymRolloutRow,
    RLVRDecisionRow,
    RLVRInferenceMessageRow,
    RLVRTurnRow,
    SWEBenchMetadata,
    SWEBenchTaskRow,
    SedimentRolloutRow,
    SedimentTaskRow,
    Verification,
)
from .sft import SFTMetadata, SFTSample
from .schema_identity import canonical_schema_id
from .trainer import (
    TrainerAssistantContentMessage,
    TrainerAssistantThinkingMessage,
    TrainerAssistantToolCallMessage,
    TrainerFunctionCall,
    TrainerTextMessage,
    TrainerToolCall,
    TrainerToolMessage,
)

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


@dataclass(frozen=True)
class SchemaContract:
    """One published Python wire contract and its stable schema identity."""

    python_type_object: type
    artifact_family: str
    slug: str
    purpose: str
    version: int = 1

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version < 1:
            raise ValueError("schema contract version must be a positive integer")

    @property
    def python_type(self) -> str:
        return (
            f"{self.python_type_object.__module__}."
            f"{self.python_type_object.__qualname__}"
        )

    @property
    def schema_id(self) -> str:
        return canonical_schema_id(self.artifact_family, self.slug, self.version)

    @property
    def output_path(self) -> Path:
        return Path(
            "schemas",
            self.artifact_family,
            self.slug,
            f"v{self.version}.json",
        )


def _contract(
    python_type: type,
    family: str,
    slug: str,
    purpose: str,
    version: int = 1,
) -> SchemaContract:
    return SchemaContract(python_type, family, slug, purpose, version)


CONTRACT_GROUPS: tuple[
    tuple[str, str, tuple[SchemaContract, ...]],
    ...,
] = (
    (
        "Facts",
        "Immutable records of things that happened, and the only persisted "
        "state (ADR 0001).",
        (
            _contract(
                InferenceCall, "facts", "inference-call", "One model call.", version=2
            ),
            _contract(
                InferenceMessage,
                "facts",
                "inference-message",
                "One ordered message in an inference call.",
                version=2,
            ),
            _contract(TextPart, "facts", "text-part", "One plain-text part."),
            _contract(
                ReasoningPart,
                "facts",
                "reasoning-part",
                "One readable model-reasoning part.",
            ),
            _contract(
                ToolCallPart,
                "facts",
                "tool-call-part",
                "One tool invocation requested by the model.",
                version=2,
            ),
            _contract(
                ToolCallResponsePart,
                "facts",
                "tool-call-response-part",
                "One result supplied for an earlier tool invocation.",
                version=2,
            ),
            _contract(
                DeveloperDecision,
                "facts",
                "developer-decision",
                "One developer accept or reject decision.",
                version=3,
            ),
            _contract(
                EditObservation,
                "facts",
                "edit-observation",
                "One applied edit and later file observation.",
                version=3,
            ),
            _contract(
                RejectedEdit,
                "facts",
                "rejected-edit",
                "One refused AI edit.",
                version=3,
            ),
            _contract(
                RetryLinkage,
                "facts",
                "retry-linkage",
                "One human-directed correction retry after a refused AI edit.",
                version=3,
            ),
            _contract(
                CIOutcome, "facts", "ci-outcome", "One CI run attempt.", version=3
            ),
            _contract(Push, "facts", "push", "One forge push receipt.", version=3),
            _contract(
                PullRequestMerge,
                "facts",
                "pull-request-merge",
                "One pull request merge boundary.",
                version=3,
            ),
            _contract(
                PullRequestRevision,
                "facts",
                "pull-request-revision",
                "One observed pull request head revision.",
                version=3,
            ),
            _contract(
                SessionCommitObservation,
                "facts",
                "session-commit-observation",
                "The first observed Git-note Session-to-commit edge.",
                version=3,
            ),
            _contract(
                QuarantineRecord,
                "facts",
                "quarantine-record",
                "One append-only fact quarantine action.",
                version=7,
            ),
            _contract(
                RepositoryRename,
                "facts",
                "repository-rename",
                "One provider repository rename receipt.",
            ),
        ),
    ),
    (
        "External inputs",
        "Versioned operator-supplied inputs used by Derivations but never stored as Facts.",
        (
            _contract(
                PriceManifest,
                "external-inputs",
                "price-manifest",
                "One immutable model price policy for cost analysis.",
            ),
        ),
    ),
    (
        "Derived artifacts",
        "Pure, recomputable artifacts derived from facts and policy.",
        (
            _contract(
                RepositoryIdentity,
                "derived-artifacts",
                "repository-identity",
                "One immutable provider repository identity.",
            ),
            _contract(
                AcceptedWorkLifecycleReport,
                "derived-artifacts",
                "accepted-work-lifecycle",
                "One coverage-aware operational lifecycle report.",
                version=3,
            ),
            _contract(
                Attribution,
                "derived-artifacts",
                "attribution",
                "One completion-to-commit attribution.",
                version=2,
            ),
            _contract(
                AttributedCompletion,
                "derived-artifacts",
                "attributed-completion",
                "One canonical attributed completion.",
                version=4,
            ),
            _contract(
                Rollout,
                "derived-artifacts",
                "rollout",
                "One canonical session trajectory.",
                version=4,
            ),
            _contract(
                CommitRef,
                "derived-artifacts",
                "commit-ref",
                "One repository-qualified commit.",
                version=2,
            ),
            _contract(
                Turn,
                "derived-artifacts",
                "turn",
                "One rollout turn.",
                version=3,
            ),
            _contract(
                CIResolution,
                "derived-artifacts",
                "ci-resolution",
                "One attempt-aware commit CI resolution.",
                version=3,
            ),
            _contract(
                CIWorkflowResolution,
                "derived-artifacts",
                "ci-workflow-resolution",
                "One attempt-aware workflow resolution.",
                version=3,
            ),
            _contract(
                Provenance,
                "derived-artifacts",
                "provenance",
                "One derivation provenance stamp.",
            ),
            _contract(
                RecoveryAttributionEvidence,
                "derived-artifacts",
                "recovery-attribution-evidence",
                "Source Attribution metadata for one Recovery enrichment call.",
            ),
            _contract(
                RecoverySample,
                "derived-artifacts",
                "recovery-sample",
                "One red-to-green recovery sample.",
                version=3,
            ),
            _contract(
                AcceptedSessionOutcome,
                "derived-artifacts",
                "accepted-session-outcome",
                "One accepted Session's terminal abandonment classification.",
                version=2,
            ),
            _contract(
                SessionAbandonment,
                "derived-artifacts",
                "session-abandonment",
                "One session whose accepted edits reached no commit.",
            ),
            _contract(
                Fate,
                "derived-artifacts",
                "fate",
                "One Edit observation's derived final Fate.",
                version=3,
            ),
            _contract(
                MergeMembershipOutcome,
                "derived-artifacts",
                "merge-membership-outcome",
                "One Attribution's pull-request membership classification.",
                version=3,
            ),
            _contract(
                MergeRetention,
                "derived-artifacts",
                "merge-retention",
                "One attributed file measured at pull request merge.",
                version=3,
            ),
            _contract(
                AbandonmentSummary,
                "derived-artifacts",
                "abandonment-summary",
                "One abandonment coverage summary.",
            ),
        ),
    ),
    (
        "Trainer message objects",
        "Closed nested message contracts used by training rows.",
        (
            _contract(
                TrainerFunctionCall,
                "trainer-messages",
                "function-call",
                "One structured function-call payload.",
            ),
            _contract(
                TrainerToolCall,
                "trainer-messages",
                "tool-call",
                "One assistant-requested function call.",
            ),
            _contract(
                TrainerTextMessage,
                "trainer-messages",
                "text-message",
                "One developer, system, or user message.",
            ),
            _contract(
                TrainerAssistantContentMessage,
                "trainer-messages",
                "assistant-content-message",
                "One assistant message with visible text.",
            ),
            _contract(
                TrainerAssistantThinkingMessage,
                "trainer-messages",
                "assistant-thinking-message",
                "One assistant message with readable reasoning.",
            ),
            _contract(
                TrainerAssistantToolCallMessage,
                "trainer-messages",
                "assistant-tool-call-message",
                "One assistant message with structured tool calls.",
            ),
            _contract(
                TrainerToolMessage,
                "trainer-messages",
                "tool-message",
                "One string tool result.",
            ),
        ),
    ),
    (
        "Training rows",
        "Canonical rows written to training JSONL files.",
        (
            _contract(DPOPair, "training-rows", "dpo-pair", "One DPO pair.", version=4),
            _contract(
                DPOMetadata,
                "training-rows",
                "dpo-metadata",
                "Sediment evidence for one DPO pair.",
                version=4,
            ),
            _contract(
                DPOProvenance,
                "training-rows",
                "dpo-provenance",
                "Provenance for both DPO members.",
            ),
            _contract(
                SFTSample, "training-rows", "sft-sample", "One SFT sample.", version=3
            ),
            _contract(
                SFTMetadata,
                "training-rows",
                "sft-metadata",
                "Sediment evidence for one SFT sample.",
                version=3,
            ),
            _contract(
                DiffSFTSample,
                "training-rows",
                "diff-sft-sample",
                "One diff-SFT sample.",
                version=3,
            ),
            _contract(
                DiffSFTMetadata,
                "training-rows",
                "diff-sft-metadata",
                "Sediment evidence for one diff-SFT sample.",
                version=3,
            ),
            _contract(
                SourceIds,
                "training-rows",
                "diff-sft-source-ids",
                "Source fact ids for one diff-SFT sample.",
                version=2,
            ),
            _contract(
                RecoveryRow,
                "training-rows",
                "recovery-row",
                "One recovery training row.",
                version=3,
            ),
            _contract(
                Verification,
                "training-rows",
                "verification",
                "Operator verifier configuration.",
            ),
            _contract(
                RLVRDecisionRow,
                "training-rows",
                "rlvr-decision",
                "One decision attached to an RLVR turn.",
                version=2,
            ),
            _contract(
                RLVRInferenceMessageRow,
                "training-rows",
                "rlvr-inference-message",
                "One RLVR trajectory message.",
                version=2,
            ),
            _contract(
                RLVRTurnRow,
                "training-rows",
                "rlvr-turn",
                "One RLVR trajectory turn.",
                version=3,
            ),
            _contract(
                NemoGymResponsesCreateParams,
                "training-rows",
                "nemo-gym-responses-create-params",
                "One NeMo Gym segment input.",
                version=2,
            ),
            _contract(
                NemoGymResponse,
                "training-rows",
                "nemo-gym-response",
                "One NeMo Gym response trajectory.",
                version=3,
            ),
            _contract(
                SedimentTaskRow,
                "training-rows",
                "sediment-rlvr-task",
                "One Sediment RLVR task.",
                version=3,
            ),
            _contract(
                SedimentRolloutRow,
                "training-rows",
                "sediment-rlvr-rollout",
                "One Sediment RLVR rollout segment.",
                version=4,
            ),
            _contract(
                SWEBenchTaskRow,
                "training-rows",
                "swe-bench-task",
                "One SWE-bench task.",
                version=3,
            ),
            _contract(
                SWEBenchMetadata,
                "training-rows",
                "swe-bench-metadata",
                "Sediment evidence for one SWE-bench task.",
                version=3,
            ),
            _contract(
                NemoGymRolloutRow,
                "training-rows",
                "nemo-gym-rollout",
                "One NeMo Gym rollout mapping.",
                version=4,
            ),
            _contract(
                NemoGymMetadata,
                "training-rows",
                "nemo-gym-metadata",
                "Sediment evidence for one NeMo Gym rollout.",
                version=4,
            ),
        ),
    ),
    (
        "Derived bundle",
        "The manifest and integrity metadata beside canonical artifact JSONL files.",
        (
            _contract(
                BundleManifest,
                "derived-bundle",
                "manifest",
                "The top-level derived-bundle manifest.",
                version=4,
            ),
            _contract(
                InferenceCallIdentity,
                "derived-bundle",
                "inference-call-identity",
                "One declared source identity and its distinct attachment aliases.",
            ),
            _contract(
                RepositoryIdentityEvidence,
                "derived-bundle",
                "repository-identity-evidence",
                "One captured repository role and its exact source Fact.",
            ),
            _contract(
                BundleRecord,
                "derived-bundle",
                "record",
                "Strict JSON envelope for one lossless canonical record.",
            ),
            _contract(
                BundleFileMetadata,
                "derived-bundle",
                "file-metadata",
                "Integrity metadata for one bundle JSONL file.",
            ),
        ),
    ),
)

CONTRACTS: tuple[SchemaContract, ...] = tuple(
    contract for _title, _description, group in CONTRACT_GROUPS for contract in group
)

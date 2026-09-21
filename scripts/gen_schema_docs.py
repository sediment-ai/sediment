# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate canonical JSON Schemas, their catalog, and the schema reference.

Third sibling of `gen_cli_docs.py` and `gen_api_docs.py`, and the same
bargain: structure comes from the code, prose comes from a map here, and an
unmapped entry fails the run so a new field cannot ship undocumented. CI
runs `--check`.

Two sources, because the code cannot carry the whole contract:

- **The classes themselves** — field names, order, types, nullability, and
  which shapes nest inside which. Read through ``typing.get_type_hints`` so
  a Pydantic model and a frozen dataclass render identically, and so the
  ``from __future__ import annotations`` string forms resolve.
- **`COMMON` and `FIELDS` here** — what each field means to someone reading
  a row. Pydantic fields carry no ``description=`` and dataclass fields
  cannot carry one at all; the meanings live in source comments written for
  maintainers, in a voice and at a depth this page should not inherit.

The registry in ``sediment_export.schema_contracts`` owns membership, schema
identity, version, artifact family, output path, and section purpose.
``COMMON`` is keyed by bare field name and ``FIELDS`` by ``Class.field``, which
wins. That split is the point rather than a saving: ``org_id`` means one
thing everywhere, and a vocabulary that drifts between shapes is the defect
CONTEXT.md exists to prevent. Reach for `FIELDS` only where a name genuinely
carries a different sense in that shape.

Types render as JSON, not Python: this page is read beside a `.jsonl` file.
Every training-row projection defines a frozen dataclass. A dataclass field is
a JSON key, subject only to the documented omission of unknown optional RLVR
fields.

Determinism: a pure function of the imported classes. Nothing reads the
environment or the clock, and a test pins that.
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import json
import re
import subprocess
import sys
import types
import typing
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Literal,
    Never,
    NotRequired,
    Required,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
)

from pydantic import AwareDatetime, BaseModel, WithJsonSchema
from sediment_export.price_manifest import PRICE_DECIMAL_PATTERN
from sediment_export.schema_contracts import (
    CONTRACTS,
    CONTRACT_GROUPS,
    SCHEMA_DIALECT,
    SchemaContract,
)
from sediment_core.models import (
    CIProvider,
    CIResult,
    AgentHarness,
    FactTable,
    InteractionMode,
    ForgeProvider,
    GatewayProvider,
    QuarantineAction,
)
from sediment_derive.attribution import AttributionSource
from sediment_derive.survival import EditFate

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = REPO_ROOT / "docs" / "reference" / "schema.md"
CATALOG_PATH = REPO_ROOT / "schemas" / "catalog.json"

# Grouped in the order a reader meets them: what is stored, what is computed
# from it, what lands in the files they feed to a trainer. Each entry is the
# one-line purpose for the section heading — deliberately not the class
# docstring, which is written for someone changing the class.
GROUPS: list[tuple[str, str, list[tuple[type, str]]]] = [
    (
        title,
        description,
        [(contract.python_type_object, contract.purpose) for contract in contracts],
    )
    for title, description, contracts in CONTRACT_GROUPS
]

# Enums referenced by the fields above, rendered as value tables.
ENUMS: list[tuple[type, str]] = [
    (GatewayProvider, "Which LLM gateway captured an inference call."),
    (AgentHarness, "Which coding agent harness emitted a developer-side fact."),
    (InteractionMode, "Whether the edit interaction was agent or inline."),
    (CIProvider, "Which CI system reported an outcome."),
    (CIResult, "How a CI run ended."),
    (ForgeProvider, "Which git host a push came from."),
    (FactTable, "Which fact table owns a quarantined fact."),
    (QuarantineAction, "Whether a record quarantines or releases a fact."),
    (
        AttributionSource,
        "How an attribution was established — the provenance that gates "
        "confidence: `git_notes` is never discounted, `jaccard` multiplies "
        "confidence by the attribution score.",
    ),
    (EditFate, "The derived final Fate of an applied edit."),
]

# Field meaning by bare name: one sense per name, everywhere it appears.
COMMON: dict[str, str] = {
    "repository_skipped": "Repository evidence omissions, separated by evaluated population and closed reason; units are not additive.",
    "repository_provider": "Captured forge provider.",
    "repository_host": "Configured lowercase forge host.",
    "repository_id": "Immutable provider repository ID within its forge host.",
    "head_repository_provider": "Captured head repository provider, independent of the target identity.",
    "head_repository_host": "Captured head repository host, independent of the target identity.",
    "head_repository_id": "Captured immutable head repository ID, independent of the target identity.",
    "repository_identity": "Qualified provider, host, and repository ID; null for an unambiguous legacy repository.",
    "chosen_repository_identity": "Qualified repository identity of the chosen member; null for an unambiguous legacy repository.",
    "rejected_repository_identity": "Qualified repository identity of the rejected member; independent of the chosen member.",
    "accepted_work": "Inference-call progression for human-explicit accepts.",
    "accepted_calls": "Uniquely attached Inference calls with a human-explicit accept.",
    "attributed": "Accepted calls with at least one commit Attribution.",
    "pull_request_membership": "Attributed calls with unique pull-request membership.",
    "ci_linked": "Calls with linked continuous integration evidence.",
    "ci_verdict": "Linked calls with a resolved continuous integration verdict.",
    "ci_passed": "Resolved accepted calls whose verdict passed.",
    "ci_failed": "Resolved accepted calls whose verdict failed.",
    "coverage": "Observed and unavailable evidence populations.",
    "examples": "Deterministically bounded Session examples by population.",
    "observed": "Population with the required evidence.",
    "missing": "Closed counts for evidence that was not captured or derived.",
    "unsupported_by_integration": "Closed counts for integration capture limits.",
    "ambiguous": "Closed counts for evidence that could not join uniquely.",
    "population": "The population represented by the Session examples.",
    "session_ids": "Sorted, deduplicated example Session identifiers.",
    "count": "Number of records in this population.",
    "denominator": "Immediate eligible population for this measurement.",
    "rate": "Count divided by its denominator, or null when the denominator is zero.",
    "initial_denominator": "Initial accepted-call population.",
    "initial_rate": "Count divided by the initial population, or null when empty.",
    "edit_retention": "Edit-observation retention and final Fate evidence.",
    "observations": "Captured Edit observations.",
    "eligible": "Observations eligible for scoring.",
    "derived_fates": "Observations with a derived final Fate.",
    "unmodified": "Observations whose applied text remained unmodified.",
    "partially_modified": "Observations whose applied text was partially modified.",
    "deleted": "Observations whose applied text was deleted.",
    "known_external_line_counts": "Observations with known external line counts.",
    "external_lines_added": "Observed external lines added over known windows.",
    "external_lines_removed": "Observed external lines removed over known windows.",
    "fate_rate": "Derived Fate coverage over captured observations.",
    "merge_durability": "Attributed file contribution retention through merge.",
    "coverage_qualification": "Qualification governing interpretation of this panel.",
    "attributed_file_candidates": "Attributed commit-file candidates.",
    "unique_pull_request_membership": "Candidates with unique pull-request membership.",
    "scored_rows": "Candidates scored at both merge boundaries.",
    "membership_rate": "Unique membership coverage over attributed candidates.",
    "scoring_rate": "Boundary scoring coverage over joined candidates.",
    "membership": "Closed pull-request membership outcome counts.",
    "scoring_skips": "Closed merge-retention scoring skip counts.",
    "head": "Retention score summary at the final pull-request head.",
    "merge": "Retention score summary at the merged commit.",
    "session_attrition": "Terminal classification of accepted Sessions.",
    "eligible_sessions": "Accepted Sessions eligible for classification.",
    "committed": "Accepted Sessions that reached a commit.",
    "abandoned": "Accepted Sessions abandoned after the grace horizon.",
    "in_flight": "Accepted Sessions inside the grace horizon.",
    "attribution_unavailable": "Accepted Sessions without an observable Attribution path.",
    "skips": "Closed counts for inputs that could not produce this artifact.",
    "rework": "Independent rework evidence components with separate grains.",
    "grain": "The unit counted by this component.",
    "strata": "Supported uniquely joined identity breakdowns.",
    "stratum_skips": "Closed counts for identities that could not form a unique stratum.",
    "dimension": "The identity dimension used for this stratum.",
    "value": "The stable identity value for this stratum.",
    "policy": "Resolved lifecycle presentation policy.",
    "example_session_limit": "Maximum Session identifiers in each example population.",
    "policy_version": "Version of the policy that produced this artifact.",
    "lifecycle": "Lifecycle report policy provenance.",
    "attribution": "Commit Attribution policy provenance.",
    "abandonment": "Session abandonment policy provenance.",
    "merge_retention": "Merge-retention policy provenance.",
    "distribution": "Distribution of observed retention scores.",
    "thresholds": "Counts below fixed retention sensitivity thresholds.",
    "mean": "Arithmetic mean of observed values, or null for an empty population.",
    "median": "Median of observed values, or null for an empty population.",
    "p10": "Tenth percentile, or null for an empty population.",
    "p90": "Ninetieth percentile, or null for an empty population.",
    "threshold": "Fixed retention sensitivity threshold.",
    "below": "Observed values below the threshold.",
    "total": "Total eligible values for this measurement.",
    "action": "Whether this record quarantines or releases the fact.",
    "accepted": "Whether the developer took the change.",
    "arguments": "The exact structured arguments supplied to the function.",
    "attribution_source": "How the underlying attribution was established.",
    "branch": "The branch the run or commit belongs to.",
    "call_id": (
        "The provider's per-call id, joining this record back to the "
        "inference call that produced it. Null when the provider emits none."
    ),
    "captured_at": (
        "When Sediment stored the fact, stamped server-side. Windows and "
        "ordering anchor on this, never on a client clock."
    ),
    "ci_outcomes": "The CI runs recorded for this commit.",
    "chosen": "The preferred trainer-facing response messages.",
    "commit_sha": "Full-length commit sha, lowercased.",
    "completion": "The model's response text.",
    "completion_id": "The inference call that supplied the training target.",
    "confidence_margin": "Chosen confidence minus rejected confidence.",
    "inference_call_id": "Id of the inference call this record belongs to.",
    "failed_inference_call_ids": ("Inference calls attributed to the failing commit."),
    "content": "Plain-text message content.",
    "confidence": (
        "The value in [0, 1] from the confidence ladder: decision "
        "precedence, then CI, then the attribution discount."
    ),
    "decision_id": "Stable id for this decision.",
    "decisions": "The developer decisions recorded against this completion.",
    "explicit": "Whether a real human gesture produced this decision.",
    "fact_id": "The fact affected by this quarantine record.",
    "fact_table": "The fact table that owns the affected fact.",
    "file_path": "Repo-relative path of the file the record concerns.",
    "function": "The structured function payload for this tool call.",
    "fixed_commit_sha": "The commit whose run passed.",
    "fixed_inference_call_ids": "Inference calls attributed to the fixing commit.",
    "fixed_outcome_id": "Id of the passing CI outcome.",
    "failed_commit_sha": "The commit whose run failed.",
    "failed_outcome_id": "Id of the failing CI outcome.",
    "model": "The model that served the call, as the gateway spelled it.",
    "name": "The function name associated with this tool call or result.",
    "model_call_id": (
        "The model service's per-call id and dedup key when present. It is "
        "separate from the ids on tool-call parts."
    ),
    "agent_harness": "Which coding agent harness produced this fact.",
    "interaction_mode": "Whether the edit interaction was `agent` or `inline`.",
    "label_confidence": "Trust in the derived training label, bounded to [0, 1].",
    "ci_reliability": (
        "Trust in the resolved CI evidence, separate from the categorical label."
    ),
    "chosen_label_source": (
        "The closed evidence source that labeled the preferred member."
    ),
    "rejected_label_source": (
        "The closed evidence source that labeled the dispreferred member."
    ),
    "eligibility_source": (
        "The closed evidence source that made this supervised target eligible."
    ),
    "metadata": "Sediment evidence excluded from trainer inputs.",
    "instance_id": "Stable target-row identity derived from recorded facts.",
    "recipe_id": "The closed evidence recipe that produced this training row.",
    "recipe_version": (
        "The version of the evidence recipe; increment it when recipe semantics change."
    ),
    "retry_linkage_id": "Stable id for this retry-linkage fact.",
    "rejected_call_id": "The refused edit tool-call id that started the retry.",
    "accepted_call_id": "The later accepted edit tool-call id.",
    "reward_source": (
        "The closed source of the directional reward; absent without a verdict."
    ),
    "base_commit": "The commit that the historical reference patch applies to.",
    "problem_statement": "The first recorded user message for the task.",
    "reference_patch": "The observed historical patch, whether it passed or failed.",
    "patch": "A historical patch with a recorded passing terminal verifier result.",
    "verification": "Operator configuration for running the verifier again.",
    "verification_command": "The opaque operator-configured verifier command.",
    "verifier_results": "The exact recorded CI outcomes that support this row.",
    "ci_resolution": "The selected attempt-aware CI resolution for this row.",
    "ci_resolutions": "The attempt-aware CI resolutions visible to this row.",
    "responses_create_params": "The captured segment input in the NeMo Gym field.",
    "response": "The captured segment turns in the NeMo Gym response field.",
    "input": "The structured messages at the start of this segment.",
    "reward": "Numeric reinforcement-learning value; absent without pass/fail evidence.",
    "segment_index": "Zero-based position of this contiguous segment.",
    "turns": "The captured turns in this contiguous rollout segment.",
    "observation_id": "Stable id for this edit observation.",
    "applied_text": "The replacement text that the edit tool applied.",
    "observed_file_text": (
        "The complete file text observed at session end. Empty string means "
        "the file was deleted."
    ),
    "occurred_at": (
        "When the event happened, stamped client-side — distinct from "
        "`captured_at`, which is when Sediment stored it."
    ),
    "observed_at": ("When Sediment observed the call, stamped at capture."),
    "run_id": "The provider-issued pipeline-run identity.",
    "run_attempt": (
        "The provider-issued attempt number. Null sorts as attempt 0 only in "
        "CI resolution."
    ),
    "schema_id": "The canonical wire-shape contract for this row.",
    "schema_version": "The positive integer version of the canonical row schema.",
    "verdict": "The resolved pass or failure; null means no directional verdict.",
    "reliability": "Trust in the resolved verdict, bounded to [0, 1].",
    "suspected_flake": (
        "Whether the lineage contains both passing and failing verdict attempts."
    ),
    "source_outcome_ids": "Every CI outcome id that contributed to the resolution.",
    "verdict_outcome_id": "The final semantic pass or failure outcome id.",
    "verdict_outcome_ids": (
        "The final semantic verdict outcome id from each workflow lineage."
    ),
    "non_verdict_outcome_ids": (
        "The infrastructure, timeout, cancellation, skip, neutral, or unknown evidence."
    ),
    "workflow_resolutions": "Attempt-aware resolutions for the commit's workflows.",
    "workflow_id": "The provider-issued workflow definition identity, when supplied.",
    "org_id": "The deployment's tenant id, normalized. Bound to the server.",
    "outcome_id": "Stable id for this outcome.",
    "prompt": (
        "The message history sent to the model in canonical structured JSON shape."
    ),
    "role": "The trainer-facing message role.",
    "provenance": "The structured provenance for this artifact.",
    "provider": "Which system this fact came from.",
    "push_id": "Stable id for this push.",
    "merge_id": "Stable id for this pull request merge boundary.",
    "revision_id": "Stable id for this observed pull request revision.",
    "status": "The closed classification assigned by this derivation.",
    "pr_number": "The positive pull request number within the repository.",
    "head_repo": "The repository that supplied the pull request head.",
    "head_ref": "The normalized final pull request head branch.",
    "head_sha": "The final pull request head commit.",
    "base_ref": "The normalized target branch at merge time.",
    "base_sha": "The target branch commit recorded by the merge event.",
    "merge_commit_sha": "The provider-reported merged commit boundary.",
    "merged_at": "When the forge recorded the pull request merge.",
    "previous_head_sha": (
        "The previously observed pull request head commit, or null when absent."
    ),
    "source_event_id": "The provider delivery identity when supplied.",
    "source_push_id": "The Push that triggered this Git-note observation.",
    "raw": (
        "The source payload after Basic redaction, kept so a translator defect "
        "stays recoverable. Shape varies by provider; never join on it."
    ),
    "quarantine_id": "Stable id for this quarantine record.",
    "reason": "The recorded reason for the quarantine action.",
    "recorded_at": "When Sediment appended this quarantine record.",
    "proposed": (
        "The edit the model wrote and the developer declined, as the model "
        "wrote it. There is no `observed_file_text` counterpart: a refused "
        "edit never reached the file, so there is no file state to observe."
    ),
    "recovery_diff": "The unified diff from the failing commit to the fixing one.",
    "rejection_id": "Stable id for this rejected edit.",
    "rejected": "The dispreferred trainer-facing response messages.",
    "repo": "`owner/repo`, lowercased. Empty string when the sender omitted it.",
    "session_id": "The coding-agent session this belongs to (ADR 0002).",
    "source_model": "The model that served the source inference call.",
    "type": "The message-part discriminator.",
    "tools": (
        "Tool definitions available to the trainer. Empty for inference-call "
        "schema version 1, which carries no tool-definition field."
    ),
    "thinking": "Readable assistant reasoning kept outside visible content.",
    "tool_calls": "Structured function calls requested by the assistant.",
    "tool_call_id": "The earlier tool-call id this result answers.",
    "tool_name": "The edit tool shared by the rejected call and accepted retry.",
    "split": (
        "Which side of the deterministic eval split this row is on, hashed "
        "on `session_id` so a session never straddles both."
    ),
    "user_id": "The developer identity when known. Null when capture omitted it.",
    "workflow_name": "The CI workflow's display name.",
    "workflow_path": (
        "The workflow definition's repo-relative path — the stable "
        "identity a display name does not give you. Null when unreported."
    ),
    "fate": "The categorical final Fate assigned from the retention score.",
}

# Per-shape overrides, where a name genuinely means something else here.
FIELDS: dict[str, str] = {
    "PriceManifest.version": "The supported price-manifest contract version.",
    "PriceManifest.manifest_id": "The operator-assigned immutable price-policy identifier.",
    "PriceManifest.prices": "The model price entries in this policy.",
    "ModelPrice.model_provider": "The explicitly declared model provider.",
    "ModelPrice.model": "The provider-specific model identifier.",
    "ModelPrice.currency": "The uppercase three-letter currency code for both prices.",
    "ModelPrice.effective_from": (
        "The inclusive start of this price interval, or null when unbounded."
    ),
    "ModelPrice.effective_until": (
        "The exclusive end of this price interval, or null when unbounded."
    ),
    "ModelPrice.input_per_million_tokens": (
        "The exact input price per million tokens, serialized as a decimal string."
    ),
    "ModelPrice.output_per_million_tokens": (
        "The exact output price per million tokens, serialized as a decimal string."
    ),
    "SessionCommitObservation.schema_version": (
        "The Fact payload contract: 1 for stored legacy payloads, 2 for identity-capable payloads."
    ),
    "SessionCommitObservation.observation_id": (
        "Stable id for this Session-to-commit observation."
    ),
    "SessionCommitObservation.repo": (
        "The non-empty `owner/repo` that owns the observed commit, lowercased."
    ),
    "PullRequestRevision.head_ref": "The normalized observed pull request head branch.",
    "PullRequestRevision.head_sha": "The observed pull request head commit.",
    "PullRequestRevision.base_ref": "The normalized target branch when observed.",
    "PullRequestRevision.base_sha": "The target branch commit when observed.",
    "TrainerToolCall.id": "The tool-call identity used by the matching result.",
    "TrainerToolCall.type": "The fixed `function` tool-call discriminator.",
    "InferenceCall.schema_version": (
        "The normalized fact-contract version. Inference calls use 1."
    ),
    "InferenceCall.inference_call_id": "Stable id for this inference-call fact.",
    "InferenceCall.gateway_provider": (
        "The capture gateway that observed the call, not the model service."
    ),
    "InferenceCall.model_provider": (
        "The model service when the gateway reports it. Null when unknown."
    ),
    "InferenceCall.input_messages": (
        "The ordered request messages with typed parts preserved."
    ),
    "InferenceCall.output_messages": (
        "The ordered response messages with typed parts preserved."
    ),
    "InferenceCall.input_tokens": (
        "Input tokens reported by the gateway. Null when unreported."
    ),
    "InferenceCall.output_tokens": (
        "Output tokens reported by the gateway. Null when unreported."
    ),
    "InferenceCall.duration_ms": (
        "Call duration in milliseconds. Null when unreported."
    ),
    "InferenceMessage.role": (
        "The message author role, such as `system`, `user`, `assistant`, or `tool`."
    ),
    "InferenceMessage.parts": (
        "Ordered text, reasoning, tool-call, or tool-response parts."
    ),
    "InferenceMessage.finish_reason": (
        "The model service's finish reason when reported."
    ),
    "RLVRInferenceMessageRow.parts": (
        "Ordered text, reasoning, tool-call, or tool-response parts."
    ),
    "RLVRInferenceMessageRow.finish_reason": (
        "The model service's finish reason; omitted when unreported."
    ),
    "TextPart.content": "Plain text, without JSON encoding around it.",
    "ReasoningPart.content": (
        "Readable model reasoning. Opaque provider state remains on the fact's "
        "`raw` payload."
    ),
    "ToolCallPart.id": (
        "The agent-visible tool-call id. It is separate from `model_call_id`."
    ),
    "ToolCallPart.name": "The tool the model requested.",
    "ToolCallPart.arguments": (
        "The parsed argument object. Malformed wire arguments become `{}`; "
        "the original stays on `InferenceCall.raw`."
    ),
    "ToolCallResponsePart.id": "The tool-call id this result answers.",
    "ToolCallResponsePart.result": (
        "The structured tool result supplied to the model."
    ),
    "BundleManifest.bundle_schema_version": (
        "The derived-bundle container version. Canonical bundles use 4."
    ),
    "BundleManifest.repository_population": "The producer-declared complete organization repository evidence and rename population through inclusive `as_of`; not proof of undisclosed external Facts.",
    "BundleArtifactCounts.repository_identities": "Complete declared repository source-role projection count.",
    "BundleArtifactCounts.repository_renames": "Complete declared Repository rename Fact count.",
    "BundleFiles.repository_identities": "Integrity metadata for repository_identities.jsonl.",
    "BundleFiles.repository_renames": "Integrity metadata for repository_renames.jsonl.",
    "BundleImplementationVersions.repository_identity": "The repository identity resolver implementation version.",
    "RepositoryIdentity.host": "The lowercase configured forge host.",
    "RepositoryIdentity.provider": "The forge provider that assigns the immutable repository ID.",
    "RepositoryIdentity.repository_id": "Immutable provider repository ID within its forge host.",
    "RepositoryIdentityEvidence.source_table": "Fact family that owns this repository-role projection.",
    "RepositoryIdentityEvidence.source_fact_id": "Exact source Fact ID retained without replacement.",
    "RepositoryIdentityEvidence.role": "Target repository or independent pull-request head repository role.",
    "RepositoryRename.rename_id": "Stable receipt ID for this captured rename.",
    "RepositoryRename.old_repo": "Normalized repository label before the rename.",
    "RepositoryRename.new_repo": "Normalized repository label after the rename.",
    "RepositoryRename.occurred_at": "Provider-reported rename time; null when the provider supplies none.",
    "BundleManifest.record_encoding": "The declared lossless inner-record decoder: `sediment-record-json-v1`.",
    "BundleManifest.identity_population": "The producer-declared complete organization identity population through inclusive `as_of`; not proof of undisclosed external Facts.",
    "InferenceCallIdentity.call_ids": "Sorted distinct provider-call and output tool-call aliases belonging to this Fact.",
    "BundleManifest.fragmented": "Retained Rollout boundaries by closed reason: `prior_output_absent`, `input_history_changed`, or `prior_output_not_replayed`; nonnegative counts copied from Derivation.",
    "BundleRecord.record_json": "One ASCII-escaped canonical record serialized with the declared `NaN`, `Infinity`, and `-Infinity` extensions. The outer envelope is strict JSON.",
    "BundleManifest.scope": (
        "The inclusive `since`, exclusive `until`, and user allowlist. An "
        "absent user allowlist means every user in the configured organization."
    ),
    "BundleManifest.policy": "Every resolved attribution and split policy value.",
    "BundleManifest.policy_digest": "SHA-256 of the canonical resolved policy.",
    "BundleManifest.implementation_versions": (
        "The repository identity, abandonment, attribution, attributed-completion, and rollout "
        "implementation versions."
    ),
    "BundleManifest.quarantine_revision": (
        "The fact-store quarantine token from the bundle's read snapshot."
    ),
    "BundleManifest.as_of": (
        "The newest visible input-Fact timestamp, set before observation binding; required when artifacts carry Session observations, or null for an empty Fact set."
    ),
    "BundleManifest.mirror_revisions": (
        "The ref-to-object map for every mirror held stable during derivation, keyed by encoded qualified repository identity."
    ),
    "BundleManifest.counts": "Row count for each canonical JSONL file.",
    "BundleManifest.skipped": "Data-quality derivation skips by closed reason.",
    "BundleManifest.excluded": "Intended cohort exclusions by closed reason.",
    "BundleManifest.files": "Integrity metadata keyed by canonical artifact name.",
    "BundleScope.since": "Inclusive lower timestamp bound, or null when unbounded.",
    "BundleScope.until": "Exclusive upper timestamp bound, or null when unbounded.",
    "BundleScope.users": "Included user ids, or null when all users are included.",
    "BundleSimilarityPolicy.min_similarity": (
        "The inclusive similarity threshold, bounded to [0, 1]."
    ),
    "BundleSimilarityPolicy.lookback_window_minutes": (
        "The positive fact-anchored lookback window in minutes."
    ),
    "BundleAttributionPolicy.post_push_grace_period_minutes": (
        "The non-negative post-push attribution grace period in minutes."
    ),
    "BundleAttributionPolicy.max_commits_per_push": (
        "The positive maximum number of commits inspected per push."
    ),
    "BundleAttributionPolicy.git_notes": (
        "The resolved similarity policy for git-notes attribution."
    ),
    "BundleAttributionPolicy.jaccard": (
        "The resolved similarity policy for Jaccard attribution."
    ),
    "BundleSplitPolicy.eval_fraction": (
        "The deterministic evaluation fraction, bounded to [0, 0.5]."
    ),
    "BundlePolicy.schema_version": "The closed derivation-policy schema version.",
    "BundlePolicy.attribution": "The resolved attribution policy.",
    "BundlePolicy.split": "The resolved deterministic split policy.",
    "BundleImplementationVersions.abandonment": (
        "The abandonment derivation implementation version."
    ),
    "BundleImplementationVersions.attribution": (
        "The attribution derivation implementation version."
    ),
    "BundleImplementationVersions.attributed_completion": (
        "The attributed-completion assembly implementation version."
    ),
    "BundleImplementationVersions.rollout": (
        "The rollout derivation implementation version."
    ),
    "BundleArtifactCounts.attributed_completions": (
        "The attributed-completions JSONL row count."
    ),
    "BundleArtifactCounts.rollouts": "The rollouts JSONL row count.",
    "BundleArtifactCounts.inference_calls": ("The inference-calls JSONL row count."),
    "BundleArtifactCounts.inference_call_identities": "The declared complete identity population row count.",
    "BundleFiles.attributed_completions": (
        "Integrity metadata for attributed_completions.jsonl."
    ),
    "BundleFiles.rollouts": "Integrity metadata for rollouts.jsonl.",
    "BundleFiles.inference_calls": ("Integrity metadata for inference_calls.jsonl."),
    "BundleFiles.inference_call_identities": "Integrity metadata for inference_call_identities.jsonl.",
    "BundleFileMetadata.path": "The fixed bundle-relative JSONL path.",
    "BundleFileMetadata.rows": "The JSONL row count.",
    "BundleFileMetadata.bytes": "The exact file size in bytes.",
    "BundleFileMetadata.sha256": "SHA-256 of the complete file bytes.",
    "BundleAttributedCompletionsFile.path": (
        "The fixed attributed_completions.jsonl path."
    ),
    "BundleAttributedCompletionsFile.rows": "The JSONL row count.",
    "BundleAttributedCompletionsFile.bytes": "The exact file size in bytes.",
    "BundleAttributedCompletionsFile.sha256": ("SHA-256 of the complete file bytes."),
    "BundleRolloutsFile.path": "The fixed rollouts.jsonl path.",
    "BundleRolloutsFile.rows": "The JSONL row count.",
    "BundleRolloutsFile.bytes": "The exact file size in bytes.",
    "BundleRolloutsFile.sha256": "SHA-256 of the complete file bytes.",
    "BundleInferenceCallsFile.path": "The fixed inference_calls.jsonl path.",
    "BundleInferenceCallsFile.rows": "The JSONL row count.",
    "BundleInferenceCallsFile.bytes": "The exact file size in bytes.",
    "BundleInferenceCallsFile.sha256": "SHA-256 of the complete file bytes.",
    "BundleInferenceCallIdentitiesFile.path": "The fixed inference_call_identities.jsonl path.",
    "BundleInferenceCallIdentitiesFile.rows": "The JSONL row count.",
    "BundleInferenceCallIdentitiesFile.bytes": "The exact file size in bytes.",
    "BundleInferenceCallIdentitiesFile.sha256": "SHA-256 of the complete file bytes.",
    "BundleRepositoryIdentitiesFile.path": "The fixed repository_identities.jsonl path.",
    "BundleRepositoryIdentitiesFile.rows": "The JSONL row count.",
    "BundleRepositoryIdentitiesFile.bytes": "The exact file size in bytes.",
    "BundleRepositoryIdentitiesFile.sha256": "SHA-256 of the complete file bytes.",
    "BundleRepositoryRenamesFile.path": "The fixed repository_renames.jsonl path.",
    "BundleRepositoryRenamesFile.rows": "The JSONL row count.",
    "BundleRepositoryRenamesFile.bytes": "The exact file size in bytes.",
    "BundleRepositoryRenamesFile.sha256": "SHA-256 of the complete file bytes.",
    "SessionAbandonment.accepted_decisions": (
        "How many accepted edits this session had that never reached a commit."
    ),
    "SessionAbandonment.explicit_accepted_decisions": (
        "How many of those accepts came from a real human gesture. Only a "
        "session with at least one explicit accept can emit a negative "
        "attributed completion."
    ),
    "SessionAbandonment.last_decision_at": (
        "The session's newest decision time — the anchor the grace horizon "
        "is measured back from."
    ),
    "SessionAbandonment.as_of": (
        "The newest timestamp anywhere in the facts this derivation read. "
        "The horizon is measured against this, never the wall clock, so the "
        "same facts always yield the same verdict."
    ),
    "AcceptedSessionOutcome.accepted_decisions": (
        "How many accepted Developer decisions this Session contains."
    ),
    "AcceptedSessionOutcome.explicit_accepted_decisions": (
        "How many accepted decisions came from an explicit developer gesture."
    ),
    "AcceptedSessionOutcome.last_decision_at": (
        "The Session's newest decision time, which anchors the grace horizon."
    ),
    "AcceptedSessionOutcome.as_of": (
        "The newest timestamp in the Facts read by the Derivation."
    ),
    "AcceptedSessionOutcome.status": (
        "The Session's closed terminal classification: committed, abandoned, "
        "in_flight, or attribution_unavailable."
    ),
    "DeveloperDecision.explicit": (
        "True for a real human gesture; false for auto-applied or inferred. "
        "Implicit decisions move confidence but never classify a row."
    ),
    "DeveloperDecision.file_path": (
        "The file edited, or empty string when the client emits none — "
        "common on rejects."
    ),
    "DeveloperDecision.commit_sha": "The commit the client attributed, when it knew one.",
    "DeveloperDecision.edit_retention_score": (
        "Graded preference strength in [0, 1]: how much applied edit text "
        "remains in a later file observation. Null unless capture or "
        "derivation supplies it."
    ),
    "DeveloperDecision.observation_delay_ms": (
        "The delay before the edit retention score was observed. Null unless "
        "the harness reports one."
    ),
    "DeveloperDecision.occurred_at": (
        "When the developer acted, stamped client-side. Required — it is "
        "what makes a redelivery collapse, and is never backfilled."
    ),
    "EditObservation.occurred_at": "When the edit was observed, stamped client-side.",
    "EditObservation.call_id": "The tool-use id of the edit this pair describes.",
    "RejectedEdit.call_id": (
        "The tool-use id of the refused call, joining this to the "
        "`DeveloperDecision` that rejected it — which is what makes it "
        "usable as a rejected side."
    ),
    "RetryLinkage.file_path": (
        "The path the harness reported for both edit attempts. It may be "
        "absolute or repo-relative."
    ),
    "EditObservation.external_lines_added": (
        "Lines added by something other than the agent's edit tools between "
        "this edit and the next observation of the file. **External, not "
        "human** — a formatter, a linter, a watcher, or the agent's own "
        "shell all land here. Null means no window covered the call, which "
        "is not the same claim as 0."
    ),
    "EditObservation.external_lines_removed": (
        "Lines removed by something other than the agent's edit tools over "
        "the same window. Same caveats as `external_lines_added`."
    ),
    "Fate.score": (
        "The scorer result after clamping to [0, 1], before threshold mapping."
    ),
    "Fate.call_id": "The edit tool-call id that joins this Fate to its decision.",
    "Fate.external_lines_added": (
        "Accumulated external lines added from this edit through Session end. "
        "Null when any window in the tail lacks coverage."
    ),
    "Fate.external_lines_removed": (
        "Accumulated external lines removed from this edit through Session end. "
        "Null when any window in the tail lacks coverage."
    ),
    "CIOutcome.schema_version": (
        "The Fact payload contract: 1 for stored legacy payloads, 2 for identity-capable payloads."
    ),
    "CIOutcome.provider": (
        "The normalized CI system selected by a capture adapter or declared "
        "by the bearer-authenticated vendor integration."
    ),
    "CIOutcome.run_id": (
        "The provider-issued pipeline-run id, unique within the deployment "
        "organization and provider namespace."
    ),
    "CIOutcome.run_attempt": (
        "The positive provider attempt number. Null when the provider omits it."
    ),
    "CIOutcome.result": "Sediment's normalized terminal result.",
    "CIOutcome.workflow_id": (
        "The provider's stable workflow or pipeline-definition identity."
    ),
    "CIOutcome.run_url": (
        "Link to the run in the CI system. Null when unreported; never run identity."
    ),
    "CIOutcome.provider_result": (
        "The exact terminal value reported by the provider before normalization."
    ),
    "CIOutcome.error_type": (
        "A structured, low-cardinality provider error type. Null when absent."
    ),
    "CIOutcome.reason": (
        "A bounded, Basic-redacted provider reason. Null when absent."
    ),
    "CIOutcome.source_event_type": (
        "The source contract and event type. Null when unreported."
    ),
    "CIOutcome.source_spec_version": (
        "The source contract version. Null when unreported."
    ),
    "CIOutcome.source_event_id": (
        "The provider or standard source event identity. Null when unreported."
    ),
    "CIOutcome.pr_number": "The pull request the run belongs to, when there is one.",
    "Push.clone_url": "Where the mirror fetches this repo from.",
    "Push.ref": "The ref that moved, e.g. `refs/heads/main`.",
    "Push.before_sha": "The ref's tip before the push.",
    "Push.after_sha": "The ref's tip after the push.",
    "Push.forced": "Whether the push rewrote history.",
    "MergeRetention.source_commit_sha": "The attributed source commit.",
    "MergeRetention.source_file_path": "The attributed source path.",
    "MergeMembershipOutcome.source_commit_sha": "The attributed source commit.",
    "MergeMembershipOutcome.source_file_path": "The attributed source path.",
    "MergeMembershipOutcome.pr_number": (
        "The uniquely joined pull request; null for an unjoined Attribution."
    ),
    "MergeMembershipOutcome.merge_id": (
        "The uniquely joined merge boundary; null for an unjoined Attribution."
    ),
    "MergeMembershipOutcome.attribution_similarity_score": (
        "The similarity score from the source Attribution."
    ),
    "MergeRetention.head_commit_sha": "The final pull request head commit.",
    "MergeRetention.head_file_path": (
        "The source path resolved at the final pull request head."
    ),
    "MergeRetention.merge_commit_sha": "The merged commit boundary.",
    "MergeRetention.merge_file_path": (
        "The source path resolved at the merged commit boundary."
    ),
    "MergeRetention.head_retention_score": (
        "Four-gram containment of the source addition at the final head."
    ),
    "MergeRetention.merge_retention_score": (
        "Four-gram containment of the source addition at the merged commit."
    ),
    "MergeRetention.attribution_similarity_score": (
        "The similarity score from the source Attribution."
    ),
    "Attribution.file_path": "The changed file this attribution is keyed on.",
    "Attribution.similarity_score": (
        "Token-overlap similarity in [0, 1]. Under `git_notes` attribution it "
        "only ranks completions within a proven session; under `jaccard` it "
        "is the evidence, and it discounts confidence."
    ),
    "AttributedCompletion.similarity_score": (
        "The attribution's similarity score, carried through so a "
        "projection can discount on it without re-deriving. Null on an "
        "abandonment-evidence row."
    ),
    "AttributedCompletion.repo": (
        "The attributed repo. Null on an abandonment-evidence row; Sediment "
        "does not guess a repo for work that reached no commit."
    ),
    "AttributedCompletion.commit_sha": (
        "The attributed commit. Null on an abandonment-evidence row."
    ),
    "AttributedCompletion.file_path": (
        "The changed file the attribution was keyed on. Null on an "
        "abandonment-evidence row."
    ),
    "AttributedCompletion.attribution_source": (
        "The attribution's `git_notes` or `jaccard` source. Null on an "
        "abandonment-evidence row."
    ),
    "AttributedCompletion.abandonment": (
        "The session-level abandonment evidence for an explicit-accept "
        "negative. Null on an attribution-evidence row. Exactly one evidence "
        "variant must be present."
    ),
    "AbandonmentSummary.abandoned_sessions": (
        "All sessions derived as abandoned, including implicit-only sessions."
    ),
    "AbandonmentSummary.grade_eligible_sessions": (
        "Abandoned sessions with at least one explicit accepted decision."
    ),
    "AbandonmentSummary.implicit_only_sessions": (
        "Abandoned sessions whose accepted decisions were all implicit."
    ),
    "AbandonmentSummary.negative_completions": (
        "Abandonment-evidence attributed completions emitted for training."
    ),
    "AbandonmentSummary.explicit_accepts_unjoined": (
        "Explicit accepted decision facts that could not join uniquely to a completion."
    ),
    "AbandonmentSummary.derivation_skipped": (
        "Candidate counts by the abandonment derivation's closed skip reasons."
    ),
    "Rollout.segments": (
        "The session's turns, split into contiguous segments. A segment "
        "break marks a gap the rollout should not pretend was continuous."
    ),
    "Rollout.commits": "Every commit this session was attributed to.",
    "CommitRef.repo": "The repository containing this commit.",
    "CommitRef.commit_sha": "The full commit identity within that repository.",
    "Provenance.policy_version": ("The derivation implementation policy version."),
    "Provenance.quarantine_revision": (
        "The integer quarantine-log high-water mark used for the derivation."
    ),
    "Provenance.policy_digest": (
        "SHA-256 of the fully resolved derivation policy when available."
    ),
    "Rollout.terminal_outcomes": (
        "The CI outcomes that supply the trajectory's terminal verifier results."
    ),
    "Turn.new_messages": (
        "Only the messages this turn added, so a trajectory does not repeat "
        "the whole history per turn."
    ),
    "RLVRTurnRow.new_messages": (
        "Only the structured messages this turn added to the trajectory."
    ),
    "Turn.tool_calls": "The tool calls this turn's response made.",
    "RecoverySample.workflow_name": "The workflow that failed and then passed.",
    "RecoveryRow.workflow_name": "The workflow that failed and then passed.",
    "DPOMetadata.chosen_completion_id": "The preferred inference call.",
    "DPOMetadata.rejected_completion_id": (
        "The dispreferred inference call from the same prompt and model."
    ),
    "DPOProvenance.chosen": "Provenance for the preferred member.",
    "DPOProvenance.rejected": "Provenance for the dispreferred member.",
    "DPOMetadata.label_confidence": (
        "The weaker member confidence — how much to trust the pair."
    ),
    "DPOMetadata.confidence_margin": (
        "Chosen confidence minus rejected. Can be negative when attribution "
        "discounts or configured confidence factors outweigh direction."
    ),
    "SFTSample.completion": "The response messages to train on.",
    "DiffSFTSample.completion": "An assistant message containing the exact patch.",
    "DPOPair.prompt": "The ordered trainer-facing request-context messages.",
    "SFTSample.prompt": "The ordered trainer-facing request-context messages.",
    "DiffSFTSample.prompt": "The ordered trainer-facing request-context messages.",
    "DiffSFTMetadata.source_ids": "The facts this row was assembled from.",
    "SourceIds.decision_ids": "Decisions that contributed to the row.",
    "SourceIds.ci_outcome_ids": "CI outcomes that contributed to the row.",
    "AttributedCompletion.session_commit_observations": "Captured Session-to-commit Facts matching this artifact, ordered by qualified edge, UTC capture time, and observation ID. Empty means no observed relationship; Attribution remains inferred.",
    "Rollout.session_commit_observations": "Captured Facts matching this Session and its repository-qualified commits at the derivation boundary. These Facts do not prove individual-call authorship.",
    "DPOMetadata.chosen_attribution_source": "The selected preferred member's inferred Attribution method, or null without Attribution.",
    "DPOMetadata.rejected_attribution_source": "The selected dispreferred member's inferred Attribution method, or null without Attribution.",
    "DPOMetadata.chosen_session_commit_observation_ids": "Sorted unique observation IDs for the preferred member's exact Session-to-commit edge; empty when absent.",
    "DPOMetadata.rejected_session_commit_observation_ids": "Sorted unique observation IDs for the dispreferred member's exact Session-to-commit edge; empty when absent.",
    "SFTMetadata.attribution_source": "The selected target's inferred Attribution method, independent of recipe eligibility.",
    "SFTMetadata.session_commit_observation_ids": "Sorted unique observation IDs for the selected target's exact Session-to-commit edge; empty when absent.",
    "DiffSFTMetadata.attribution_sources": "Sorted distinct Attribution methods of members supplying the emitted patch.",
    "SourceIds.session_commit_observation_ids": "Sorted unique observation IDs from members supplying the emitted patch.",
    "RecoveryAttributionEvidence.attribution_sources": "Sorted distinct Attribution methods for this enrichment call on this side's commit.",
    "RecoveryAttributionEvidence.session_commit_observation_ids": "Sorted unique observation IDs matching this enrichment Session and this side's qualified commit.",
    "RecoverySample.failed_attribution_evidence": "Optional failed-side enrichment source records, ordered by Inference call and Session ID.",
    "RecoverySample.fixed_attribution_evidence": "Optional fixed-side enrichment source records, ordered by Inference call and Session ID.",
    "RecoveryRow.failed_attribution_evidence": "Failed-side enrichment sources retained from the Recovery sample; empty when absent.",
    "RecoveryRow.fixed_attribution_evidence": "Fixed-side enrichment sources retained from the Recovery sample; empty when absent.",
    "SedimentTaskRow.session_commit_observation_ids": "Sorted unique observation IDs matching the emitted CI resolution and source Rollout Session.",
    "SedimentRolloutRow.session_commit_observation_ids": "Sorted unique observation IDs matching emitted CI resolutions and source Rollout Session.",
    "SWEBenchMetadata.session_commit_observation_ids": "Sorted unique observation IDs matching the emitted CI resolution and source Rollout Session.",
    "NemoGymMetadata.session_commit_observation_ids": "Sorted unique observation IDs matching the emitted CI resolution and source Rollout Session; empty without a resolution.",
    "MergeRetention.session_commit_observation_ids": "Sorted unique observations of the Session on the source commit. The selected call/file Attribution remains inferred.",
    "MergeMembershipOutcome.session_commit_observation_ids": "Sorted unique observations of the Session on the source commit; membership does not prove individual-call authorship.",
}

_HEADER = """# Schema reference

Every shape Sediment stores, derives, or writes to an export file,
generated from the classes themselves by `scripts/gen_schema_docs.py`. Do
not edit this file — change the class, then run `uv run python
scripts/gen_schema_docs.py` (CI fails on a stale page).

`schemas/catalog.json` indexes the committed JSON Schema Draft 2020-12
contract for every shape. A canonical schema id and positive integer schema
version identify wire shape and field semantics. A `recipe_id` and
`recipe_version` identify how an exporter interprets evidence for one training
objective. A compatibility-profile id and version identify a downstream
consumer adapter. An Alembic revision identifies the physical PostgreSQL
schema. These four version namespaces are independent.

Types are written as JSON, because this page is meant to be read beside a
`.jsonl` file or an API response. `string | null` means the key is present
and may be null.

The RLVR target rows omit optional `verification`, `reward`, `reward_source`,
and `ci_resolution` keys when the facts or operator configuration don't
provide them. The complete target
mappings live in [Export RLVR tasks and trajectories](../exports/rlvr-export.md).

New here? [How Sediment works](../explanation/how-sediment-works.md)
explains why facts and derivations are separate, and
[CONTEXT.md](../../CONTEXT.md) is the vocabulary these names come from.
"""

# Order matters: bool is a subclass of int, so it has to be tested first or
# every boolean field renders as an integer.
_JSON_SCALARS: dict[Any, str] = {
    bool: "boolean",
    str: "string",
    int: "integer",
    float: "number",
    dict: "object",
    type(None): "null",
}


def _shape_names() -> set[str]:
    return {cls.__name__ for _, _, members in GROUPS for cls, _ in members}


def _anchor(name: str) -> str:
    return f"[`{name}`](#{name.lower()})"


def _render_type(annotation: Any, known: set[str]) -> str:
    """A JSON type string, with nested shapes and enums linked.

    Unwraps ``Annotated`` (the validated id aliases are all
    ``Annotated[str, ...]``) and flattens unions so ``str | None`` reads
    ``string | null`` rather than leaking `Optional`.
    """
    if annotation is Never:
        return "never (must be empty)"
    if isinstance(annotation, TypeAliasType):
        return _render_type(annotation.__value__, known)
    origin = get_origin(annotation)
    if origin is Annotated:
        return _render_type(get_args(annotation)[0], known)
    if origin in (Required, NotRequired):
        return _render_type(get_args(annotation)[0], known)
    # "or", never "|": a pipe inside a cell splits a markdown table column.
    if origin in (Union, types.UnionType):
        seen = [_render_type(arg, known) for arg in get_args(annotation)]
        return " or ".join(dict.fromkeys(seen))
    if origin is Literal:
        return " or ".join(f"`{value}`" for value in get_args(annotation))
    if origin in (list, tuple):
        args = [a for a in get_args(annotation) if a is not Ellipsis]
        inner = _render_type(args[0], known) if args else "any"
        return f"array of {inner}"
    if origin is dict:
        return "object"
    if typing.is_typeddict(annotation):
        return (
            _anchor(annotation.__name__) if annotation.__name__ in known else "object"
        )
    if isinstance(annotation, type):
        if issubclass(annotation, enum.Enum):
            return _anchor(annotation.__name__)
        if annotation.__name__ in known:
            return _anchor(annotation.__name__)
        # Ahead of the scalar sweep. `AwareDatetime` is a bare marker class —
        # it does NOT subclass datetime — so identity is the only test that
        # catches it, and a subclass check alone silently renders every
        # timestamp as `object`.
        if annotation is AwareDatetime or issubclass(annotation, datetime):
            return "string (RFC 3339)"
        if annotation is Decimal:
            return "decimal string"
        for base, name in _JSON_SCALARS.items():
            if issubclass(annotation, base):
                return name
    return "object"


def _field_names(cls: type) -> list[str]:
    if typing.is_typeddict(cls):
        return list(typing.get_type_hints(cls, include_extras=True))
    if issubclass(cls, BaseModel):
        return list(cls.model_fields)
    return [f.name for f in dataclasses.fields(cls)]


def _render_field_type(cls: type, name: str, annotation: Any, known: set[str]) -> str:
    origin = get_origin(annotation)
    typed_dict_optional = typing.is_typeddict(cls) and (
        origin is NotRequired or (not cls.__total__ and origin is not Required)
    )
    omits_none = dataclasses.is_dataclass(cls) and next(
        field for field in dataclasses.fields(cls) if field.name == name
    ).metadata.get("omit_none", False)
    if omits_none and origin in (Union, types.UnionType):
        non_null = tuple(arg for arg in get_args(annotation) if arg is not type(None))
        annotation = non_null[0] if len(non_null) == 1 else Union[non_null]
    rendered = _render_type(annotation, known)
    if typed_dict_optional or omits_none:
        return f"{rendered} (optional)"
    return rendered


def _describe(cls: type, name: str) -> str:
    key = f"{cls.__name__}.{name}"
    if key in FIELDS:
        return FIELDS[key]
    if name in COMMON:
        return COMMON[name]
    raise SystemExit(
        f"error: no description for {key} — add it to COMMON (if the name "
        "means the same everywhere) or FIELDS in scripts/gen_schema_docs.py"
    )


def _table(columns: list[str], rows: list[tuple[str, ...]]) -> list[str]:
    return [
        "| " + " | ".join(columns) + " |",
        "|" + "---|" * len(columns),
        *["| " + " | ".join(row) + " |" for row in rows],
        "",
    ]


def render() -> str:
    known = _shape_names()
    contracts_by_type = {
        contract.python_type_object: contract for contract in CONTRACTS
    }
    lines = [_HEADER]

    for title, blurb, members in GROUPS:
        lines += [f"## {title}", "", blurb, ""]
        for cls, purpose in members:
            contract = contracts_by_type[cls]
            hints = typing.get_type_hints(cls, include_extras=True)
            rows = [
                (
                    f"`{name}`",
                    _render_field_type(cls, name, hints[name], known),
                    _describe(cls, name),
                )
                for name in _field_names(cls)
            ]
            lines += [
                f"### {cls.__name__}",
                "",
                purpose,
                "",
                f"Canonical schema: `{contract.schema_id}` "
                f"(version {contract.version}; `{contract.output_path.as_posix()}`).",
                "",
            ]
            lines += _table(["Field", "Type", "Meaning"], rows)

    lines += ["## Enumerations", ""]
    for enum_cls, purpose in ENUMS:
        values = ", ".join(f"`{member.value}`" for member in enum_cls)
        lines += [f"### {enum_cls.__name__}", "", purpose, "", f"Values: {values}", ""]

    return "\n".join(lines).rstrip() + "\n"


def _without_null(schema: dict[str, Any] | bool) -> dict[str, Any] | bool:
    """Remove the null branch for a key that the serializer omits."""
    if not isinstance(schema, dict):
        return schema
    branches = schema.get("anyOf")
    if not isinstance(branches, list):
        return schema
    non_null = [branch for branch in branches if branch != {"type": "null"}]
    if len(non_null) == 1:
        return non_null[0]
    return {**schema, "anyOf": non_null}


def _literal_schema(values: tuple[object, ...]) -> dict[str, Any]:
    value_types = {type(value) for value in values}
    type_names = {
        bool: "boolean",
        str: "string",
        int: "integer",
        float: "number",
        type(None): "null",
    }
    schema: dict[str, Any] = (
        {"const": values[0]} if len(values) == 1 else {"enum": list(values)}
    )
    if len(value_types) == 1 and (value_type := next(iter(value_types))) in type_names:
        schema["type"] = type_names[value_type]
    return schema


def _apply_constraints(
    schema: dict[str, Any] | bool,
    annotation: Any,
    metadata: tuple[object, ...],
) -> dict[str, Any] | bool:
    if not isinstance(schema, dict):
        return schema
    origin = get_origin(annotation)
    scalar = (
        get_args(annotation)[0] if origin in (Union, types.UnionType) else annotation
    )
    scalar_origin = get_origin(scalar)
    length_kind = "array" if scalar_origin in (list, tuple) else "string"
    constraints = list(metadata)
    for constraint in list(constraints):
        constraints.extend(getattr(constraint, "metadata", ()))
    for constraint in constraints:
        if isinstance(constraint, WithJsonSchema):
            schema.update(constraint.json_schema or {})
        for attribute, keyword in (
            ("ge", "minimum"),
            ("gt", "exclusiveMinimum"),
            ("le", "maximum"),
            ("lt", "exclusiveMaximum"),
        ):
            value = getattr(constraint, attribute, None)
            if value is not None:
                schema[keyword] = value
        pattern = getattr(constraint, "pattern", None)
        if pattern is not None:
            schema["pattern"] = pattern
        for attribute, keyword in (
            ("min_length", "minItems" if length_kind == "array" else "minLength"),
            ("max_length", "maxItems" if length_kind == "array" else "maxLength"),
        ):
            value = getattr(constraint, attribute, None)
            if value is not None:
                schema[keyword] = value
    return schema


def _object_schema(
    cls: type,
    definitions: dict[str, dict[str, Any] | bool],
) -> dict[str, Any]:
    hints = typing.get_type_hints(cls, include_extras=True)
    properties: dict[str, Any] = {}
    required: list[str] = []

    if typing.is_typeddict(cls):
        declared = {name: None for name in hints}
    elif issubclass(cls, BaseModel):
        declared = cls.model_fields
    else:
        declared = {item.name: item for item in dataclasses.fields(cls)}

    for name, declared_field in declared.items():
        annotation = hints[name]
        origin = get_origin(annotation)
        omit_none = bool(
            dataclasses.is_dataclass(cls)
            and declared_field.metadata.get("omit_none", False)
        )
        field_metadata: tuple[object, ...] = ()
        if issubclass(cls, BaseModel):
            field_metadata = tuple(declared_field.metadata)
        field_schema = _schema_for_type(
            annotation,
            definitions,
            metadata=field_metadata,
        )
        if omit_none:
            field_schema = _without_null(field_schema)
        if isinstance(field_schema, dict):
            field_schema = {**field_schema, "description": _describe(cls, name)}
        properties[name] = field_schema

        typed_dict_optional = typing.is_typeddict(cls) and (
            origin is NotRequired or (not cls.__total__ and origin is not Required)
        )
        if not typed_dict_optional and not omit_none:
            required.append(name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _schema_for_type(
    annotation: Any,
    definitions: dict[str, dict[str, Any] | bool],
    *,
    metadata: tuple[object, ...] = (),
) -> dict[str, Any] | bool:
    if annotation is Any:
        return {}
    if annotation is Never:
        return False
    if isinstance(annotation, TypeAliasType):
        return _schema_for_type(annotation.__value__, definitions, metadata=metadata)

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        return _schema_for_type(
            arguments[0],
            definitions,
            metadata=(*arguments[1:], *metadata),
        )
    if origin in (Required, NotRequired):
        return _schema_for_type(arguments[0], definitions, metadata=metadata)
    if origin in (Union, types.UnionType):
        schema = {
            "anyOf": [_schema_for_type(argument, definitions) for argument in arguments]
        }
        return _apply_constraints(schema, annotation, metadata)
    if origin is Literal:
        return _literal_schema(arguments)
    if origin is list:
        item_schema = _schema_for_type(arguments[0], definitions) if arguments else {}
        return _apply_constraints(
            {"type": "array", "items": item_schema}, annotation, metadata
        )
    if origin is tuple:
        tuple_arguments = tuple(arg for arg in arguments if arg is not Ellipsis)
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return _apply_constraints(
                {
                    "type": "array",
                    "items": _schema_for_type(arguments[0], definitions),
                },
                annotation,
                metadata,
            )
        return _apply_constraints(
            {
                "type": "array",
                "prefixItems": [
                    _schema_for_type(argument, definitions)
                    for argument in tuple_arguments
                ],
                "minItems": len(tuple_arguments),
                "maxItems": len(tuple_arguments),
            },
            annotation,
            metadata,
        )
    if origin in (dict, Mapping, typing.Mapping):
        value_schema = _schema_for_type(arguments[1], definitions) if arguments else {}
        if arguments and get_origin(arguments[0]) is Literal:
            return {
                "type": "object",
                "properties": {key: value_schema for key in get_args(arguments[0])},
                "additionalProperties": False,
            }
        return {"type": "object", "additionalProperties": value_schema}
    if typing.is_typeddict(annotation) or (
        isinstance(annotation, type)
        and (dataclasses.is_dataclass(annotation) or issubclass(annotation, BaseModel))
    ):
        name = annotation.__name__
        if name not in definitions:
            definitions[name] = {}
            definitions[name] = _object_schema(annotation, definitions)
        return {"$ref": f"#/$defs/{name}"}
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        values = tuple(member.value for member in annotation)
        return _literal_schema(values)
    if annotation is AwareDatetime or (
        isinstance(annotation, type) and issubclass(annotation, datetime)
    ):
        return _apply_constraints(
            {"type": "string", "format": "date-time"}, annotation, metadata
        )
    if annotation is Decimal:
        return {
            "type": "string",
            "format": "decimal",
            "pattern": PRICE_DECIMAL_PATTERN,
        }
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is str:
        return _apply_constraints({"type": "string"}, annotation, metadata)
    if annotation is int:
        return _apply_constraints({"type": "integer"}, annotation, metadata)
    if annotation is float:
        return _apply_constraints({"type": "number"}, annotation, metadata)
    if annotation is type(None):
        return {"type": "null"}
    raise SystemExit(f"error: no JSON Schema mapping for {annotation!r}")


def render_json_schema(contract: SchemaContract) -> dict[str, Any]:
    definitions: dict[str, dict[str, Any] | bool] = {}
    root = _object_schema(contract.python_type_object, definitions)
    definitions.pop(contract.python_type_object.__name__, None)
    schema: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "$id": contract.schema_id,
        "title": contract.python_type_object.__name__,
        "description": contract.purpose,
        **root,
    }
    if definitions:
        schema["$defs"] = definitions
    return schema


def render_catalog() -> str:
    payload = {
        "$schema": SCHEMA_DIALECT,
        "contracts": [
            {
                "schema_id": contract.schema_id,
                "schema_version": contract.version,
                "python_type": contract.python_type,
                "artifact_family": contract.artifact_family,
                "output_path": contract.output_path.as_posix(),
            }
            for contract in CONTRACTS
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _rendered_files() -> dict[Path, str]:
    files = {OUT_PATH: render(), CATALOG_PATH: render_catalog()}
    files.update(
        {
            REPO_ROOT / contract.output_path: (
                json.dumps(
                    render_json_schema(contract),
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n"
            )
            for contract in CONTRACTS
        }
    )
    return files


def _canonical_json(document: str) -> str:
    return json.dumps(json.loads(document), sort_keys=True, separators=(",", ":"))


def compatibility_errors(
    previous_schemas: Mapping[str, str],
    current_schemas: Mapping[str, str],
) -> list[str]:
    """Reject removal or mutation of an already published id/version pair.

    Sediment uses a conservative compatibility rule: a published schema file
    is immutable. A compatible extension gets a new versioned path while the
    prior contract remains available byte-semantically unchanged.
    """
    errors: list[str] = []
    for path, previous in sorted(previous_schemas.items()):
        current = current_schemas.get(path)
        if current is None:
            errors.append(f"{path} removed after publication")
            continue
        try:
            unchanged = _canonical_json(previous) == _canonical_json(current)
        except (TypeError, ValueError):
            unchanged = False
        if not unchanged:
            errors.append(f"{path} changed under its published schema id and version")
    return errors


def _published_schemas_at(revision: str) -> dict[str, str]:
    """Read every published version from Git, including inactive catalog history."""
    tree = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{revision}^{{tree}}"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", tree, "--", "schemas/"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    published: dict[str, str] = {}
    for path in listing.stdout.split("\0"):
        if not re.search(r"/v[0-9]+\.json$", path):
            continue
        result = subprocess.run(
            ["git", "show", f"{tree}:{path}"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        published[path] = result.stdout
    return published


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any committed schema output differs from generation",
    )
    parser.add_argument(
        "--compatibility-base",
        metavar="REVISION",
        help="reject mutation or removal of schemas published at REVISION",
    )
    args = parser.parse_args(argv)
    rendered_files = _rendered_files()
    if args.check:
        stale = [
            path
            for path, generated in rendered_files.items()
            if not path.exists() or path.read_text(encoding="utf-8") != generated
        ]
        if stale:
            for path in stale:
                print(
                    f"error: {_display(path)} is stale — run "
                    "`uv run python scripts/gen_schema_docs.py`",
                    file=sys.stderr,
                )
            return 1
        if args.compatibility_base is not None:
            try:
                previous = _published_schemas_at(args.compatibility_base)
            except subprocess.CalledProcessError as error:
                print(
                    f"error: cannot read schema compatibility base "
                    f"{args.compatibility_base!r}: {error.stderr.strip()}",
                    file=sys.stderr,
                )
                return 1
            current = {
                path: (REPO_ROOT / path).read_text(encoding="utf-8")
                for path in previous
                if (REPO_ROOT / path).exists()
            }
            errors = compatibility_errors(previous, current)
            if errors:
                for error in errors:
                    print(
                        f"error: incompatible canonical schema: {error}",
                        file=sys.stderr,
                    )
                return 1
        print("canonical schemas and schema reference are current")
        return 0
    for path, generated in rendered_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generated, encoding="utf-8")
        print(f"wrote {_display(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

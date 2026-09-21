# Schema reference

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

## Facts

Immutable records of things that happened, and the only persisted state (ADR 0001).

### InferenceCall

One model call.

Canonical schema: `https://sediment.so/schemas/facts/inference-call/v2.json` (version 2; `schemas/facts/inference-call/v2.json`).

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `1` | The normalized fact-contract version. Inference calls use 1. |
| `inference_call_id` | string | Stable id for this inference-call fact. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `user_id` | string or null | The developer identity when known. Null when capture omitted it. |
| `gateway_provider` | [`GatewayProvider`](#gatewayprovider) | The capture gateway that observed the call, not the model service. |
| `model_provider` | string or null | The model service when the gateway reports it. Null when unknown. |
| `model` | string or null | The model that served the call, as the gateway spelled it. |
| `input_messages` | array of [`InferenceMessage`](#inferencemessage) | The ordered request messages with typed parts preserved. |
| `output_messages` | array of [`InferenceMessage`](#inferencemessage) | The ordered response messages with typed parts preserved. |
| `input_tokens` | integer or null | Input tokens reported by the gateway. Null when unreported. |
| `output_tokens` | integer or null | Output tokens reported by the gateway. Null when unreported. |
| `duration_ms` | integer or null | Call duration in milliseconds. Null when unreported. |
| `model_call_id` | string or null | The model service's per-call id and dedup key when present. It is separate from the ids on tool-call parts. |
| `observed_at` | string (RFC 3339) | When Sediment observed the call, stamped at capture. |
| `raw` | object | The source payload after Basic redaction, kept so a translator defect stays recoverable. Shape varies by provider; never join on it. |

### InferenceMessage

One ordered message in an inference call.

Canonical schema: `https://sediment.so/schemas/facts/inference-message/v2.json` (version 2; `schemas/facts/inference-message/v2.json`).

| Field | Type | Meaning |
|---|---|---|
| `role` | string | The message author role, such as `system`, `user`, `assistant`, or `tool`. |
| `parts` | array of [`TextPart`](#textpart) or [`ReasoningPart`](#reasoningpart) or [`ToolCallPart`](#toolcallpart) or [`ToolCallResponsePart`](#toolcallresponsepart) | Ordered text, reasoning, tool-call, or tool-response parts. |
| `finish_reason` | string or null | The model service's finish reason when reported. |

### TextPart

One plain-text part.

Canonical schema: `https://sediment.so/schemas/facts/text-part/v1.json` (version 1; `schemas/facts/text-part/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `type` | `text` | The message-part discriminator. |
| `content` | string | Plain text, without JSON encoding around it. |

### ReasoningPart

One readable model-reasoning part.

Canonical schema: `https://sediment.so/schemas/facts/reasoning-part/v1.json` (version 1; `schemas/facts/reasoning-part/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `type` | `reasoning` | The message-part discriminator. |
| `content` | string | Readable model reasoning. Opaque provider state remains on the fact's `raw` payload. |

### ToolCallPart

One tool invocation requested by the model.

Canonical schema: `https://sediment.so/schemas/facts/tool-call-part/v2.json` (version 2; `schemas/facts/tool-call-part/v2.json`).

| Field | Type | Meaning |
|---|---|---|
| `type` | `tool_call` | The message-part discriminator. |
| `id` | string | The agent-visible tool-call id. It is separate from `model_call_id`. |
| `name` | string | The tool the model requested. |
| `arguments` | object | The parsed argument object. Malformed wire arguments become `{}`; the original stays on `InferenceCall.raw`. |

### ToolCallResponsePart

One result supplied for an earlier tool invocation.

Canonical schema: `https://sediment.so/schemas/facts/tool-call-response-part/v2.json` (version 2; `schemas/facts/tool-call-response-part/v2.json`).

| Field | Type | Meaning |
|---|---|---|
| `type` | `tool_call_response` | The message-part discriminator. |
| `id` | string | The tool-call id this result answers. |
| `result` | object | The structured tool result supplied to the model. |

### DeveloperDecision

One developer accept or reject decision.

Canonical schema: `https://sediment.so/schemas/facts/developer-decision/v3.json` (version 3; `schemas/facts/developer-decision/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `decision_id` | string | Stable id for this decision. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `user_id` | string or null | The developer identity when known. Null when capture omitted it. |
| `agent_harness` | [`AgentHarness`](#agentharness) | Which coding agent harness produced this fact. |
| `file_path` | string | The file edited, or empty string when the client emits none — common on rejects. |
| `accepted` | boolean | Whether the developer took the change. |
| `explicit` | boolean | True for a real human gesture; false for auto-applied or inferred. Implicit decisions move confidence but never classify a row. |
| `interaction_mode` | [`InteractionMode`](#interactionmode) | Whether the edit interaction was `agent` or `inline`. |
| `commit_sha` | string or null | The commit the client attributed, when it knew one. |
| `call_id` | string or null | The provider's per-call id, joining this record back to the inference call that produced it. Null when the provider emits none. |
| `edit_retention_score` | number or null | Graded preference strength in [0, 1]: how much applied edit text remains in a later file observation. Null unless capture or derivation supplies it. |
| `observation_delay_ms` | integer or null | The delay before the edit retention score was observed. Null unless the harness reports one. |
| `occurred_at` | string (RFC 3339) | When the developer acted, stamped client-side. Required — it is what makes a redelivery collapse, and is never backfilled. |
| `captured_at` | string (RFC 3339) | When Sediment stored the fact, stamped server-side. Windows and ordering anchor on this, never on a client clock. |
| `raw` | object | The source payload after Basic redaction, kept so a translator defect stays recoverable. Shape varies by provider; never join on it. |

### EditObservation

One applied edit and later file observation.

Canonical schema: `https://sediment.so/schemas/facts/edit-observation/v3.json` (version 3; `schemas/facts/edit-observation/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `observation_id` | string | Stable id for this edit observation. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `user_id` | string or null | The developer identity when known. Null when capture omitted it. |
| `agent_harness` | [`AgentHarness`](#agentharness) | Which coding agent harness produced this fact. |
| `file_path` | string | Repo-relative path of the file the record concerns. |
| `call_id` | string | The tool-use id of the edit this pair describes. |
| `applied_text` | string | The replacement text that the edit tool applied. |
| `observed_file_text` | string | The complete file text observed at session end. Empty string means the file was deleted. |
| `external_lines_added` | integer or null | Lines added by something other than the agent's edit tools between this edit and the next observation of the file. **External, not human** — a formatter, a linter, a watcher, or the agent's own shell all land here. Null means no window covered the call, which is not the same claim as 0. |
| `external_lines_removed` | integer or null | Lines removed by something other than the agent's edit tools over the same window. Same caveats as `external_lines_added`. |
| `occurred_at` | string (RFC 3339) | When the edit was observed, stamped client-side. |
| `captured_at` | string (RFC 3339) | When Sediment stored the fact, stamped server-side. Windows and ordering anchor on this, never on a client clock. |
| `raw` | object | The source payload after Basic redaction, kept so a translator defect stays recoverable. Shape varies by provider; never join on it. |

### RejectedEdit

One refused AI edit.

Canonical schema: `https://sediment.so/schemas/facts/rejected-edit/v3.json` (version 3; `schemas/facts/rejected-edit/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `rejection_id` | string | Stable id for this rejected edit. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `user_id` | string or null | The developer identity when known. Null when capture omitted it. |
| `agent_harness` | [`AgentHarness`](#agentharness) | Which coding agent harness produced this fact. |
| `file_path` | string | Repo-relative path of the file the record concerns. |
| `call_id` | string | The tool-use id of the refused call, joining this to the `DeveloperDecision` that rejected it — which is what makes it usable as a rejected side. |
| `proposed` | string | The edit the model wrote and the developer declined, as the model wrote it. There is no `observed_file_text` counterpart: a refused edit never reached the file, so there is no file state to observe. |
| `occurred_at` | string (RFC 3339) | When the event happened, stamped client-side — distinct from `captured_at`, which is when Sediment stored it. |
| `captured_at` | string (RFC 3339) | When Sediment stored the fact, stamped server-side. Windows and ordering anchor on this, never on a client clock. |
| `raw` | object | The source payload after Basic redaction, kept so a translator defect stays recoverable. Shape varies by provider; never join on it. |

### RetryLinkage

One human-directed correction retry after a refused AI edit.

Canonical schema: `https://sediment.so/schemas/facts/retry-linkage/v3.json` (version 3; `schemas/facts/retry-linkage/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `retry_linkage_id` | string | Stable id for this retry-linkage fact. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `user_id` | string or null | The developer identity when known. Null when capture omitted it. |
| `agent_harness` | [`AgentHarness`](#agentharness) | Which coding agent harness produced this fact. |
| `file_path` | string | The path the harness reported for both edit attempts. It may be absolute or repo-relative. |
| `tool_name` | `Edit` or `Write` | The edit tool shared by the rejected call and accepted retry. |
| `rejected_call_id` | string | The refused edit tool-call id that started the retry. |
| `accepted_call_id` | string | The later accepted edit tool-call id. |
| `occurred_at` | string (RFC 3339) | When the event happened, stamped client-side — distinct from `captured_at`, which is when Sediment stored it. |
| `captured_at` | string (RFC 3339) | When Sediment stored the fact, stamped server-side. Windows and ordering anchor on this, never on a client clock. |
| `raw` | object | The source payload after Basic redaction, kept so a translator defect stays recoverable. Shape varies by provider; never join on it. |

### CIOutcome

One CI run attempt.

Canonical schema: `https://sediment.so/schemas/facts/ci-outcome/v3.json` (version 3; `schemas/facts/ci-outcome/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `1` or `2` | The Fact payload contract: 1 for stored legacy payloads, 2 for identity-capable payloads. |
| `repository_provider` | [`ForgeProvider`](#forgeprovider) or null | Captured forge provider. |
| `repository_host` | string or null | Configured lowercase forge host. |
| `repository_id` | string or null | Immutable provider repository ID within its forge host. |
| `outcome_id` | string | Stable id for this outcome. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `provider` | [`CIProvider`](#ciprovider) | The normalized CI system selected by a capture adapter or declared by the bearer-authenticated vendor integration. |
| `run_id` | string | The provider-issued pipeline-run id, unique within the deployment organization and provider namespace. |
| `run_attempt` | integer or null | The positive provider attempt number. Null when the provider omits it. |
| `repo` | string | `owner/repo`, lowercased. Empty string when the sender omitted it. |
| `commit_sha` | string | Full-length commit sha, lowercased. |
| `branch` | string | The branch the run or commit belongs to. |
| `result` | [`CIResult`](#ciresult) | Sediment's normalized terminal result. |
| `workflow_name` | string | The CI workflow's display name. |
| `workflow_id` | string or null | The provider's stable workflow or pipeline-definition identity. |
| `workflow_path` | string or null | The workflow definition's repo-relative path — the stable identity a display name does not give you. Null when unreported. |
| `run_url` | string or null | Link to the run in the CI system. Null when unreported; never run identity. |
| `provider_result` | string or null | The exact terminal value reported by the provider before normalization. |
| `error_type` | string or null | A structured, low-cardinality provider error type. Null when absent. |
| `reason` | string or null | A bounded, Basic-redacted provider reason. Null when absent. |
| `source_event_type` | string or null | The source contract and event type. Null when unreported. |
| `source_spec_version` | string or null | The source contract version. Null when unreported. |
| `source_event_id` | string or null | The provider or standard source event identity. Null when unreported. |
| `pr_number` | integer or null | The pull request the run belongs to, when there is one. |
| `captured_at` | string (RFC 3339) | When Sediment stored the fact, stamped server-side. Windows and ordering anchor on this, never on a client clock. |
| `raw` | object | The source payload after Basic redaction, kept so a translator defect stays recoverable. Shape varies by provider; never join on it. |

### Push

One forge push receipt.

Canonical schema: `https://sediment.so/schemas/facts/push/v3.json` (version 3; `schemas/facts/push/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `1` or `2` | The positive integer version of the canonical row schema. |
| `repository_provider` | [`ForgeProvider`](#forgeprovider) or null | Captured forge provider. |
| `repository_host` | string or null | Configured lowercase forge host. |
| `repository_id` | string or null | Immutable provider repository ID within its forge host. |
| `push_id` | string | Stable id for this push. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `provider` | [`ForgeProvider`](#forgeprovider) | Which system this fact came from. |
| `repo` | string | `owner/repo`, lowercased. Empty string when the sender omitted it. |
| `clone_url` | string | Where the mirror fetches this repo from. |
| `ref` | string | The ref that moved, e.g. `refs/heads/main`. |
| `before_sha` | string | The ref's tip before the push. |
| `after_sha` | string | The ref's tip after the push. |
| `forced` | boolean | Whether the push rewrote history. |
| `captured_at` | string (RFC 3339) | When Sediment stored the fact, stamped server-side. Windows and ordering anchor on this, never on a client clock. |

### PullRequestMerge

One pull request merge boundary.

Canonical schema: `https://sediment.so/schemas/facts/pull-request-merge/v3.json` (version 3; `schemas/facts/pull-request-merge/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `1` or `2` | The positive integer version of the canonical row schema. |
| `repository_provider` | [`ForgeProvider`](#forgeprovider) or null | Captured forge provider. |
| `repository_host` | string or null | Configured lowercase forge host. |
| `repository_id` | string or null | Immutable provider repository ID within its forge host. |
| `merge_id` | string | Stable id for this pull request merge boundary. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `provider` | [`ForgeProvider`](#forgeprovider) | Which system this fact came from. |
| `repo` | string | `owner/repo`, lowercased. Empty string when the sender omitted it. |
| `pr_number` | integer | The positive pull request number within the repository. |
| `head_repo` | string | The repository that supplied the pull request head. |
| `head_repository_provider` | [`ForgeProvider`](#forgeprovider) or null | Captured head repository provider, independent of the target identity. |
| `head_repository_host` | string or null | Captured head repository host, independent of the target identity. |
| `head_repository_id` | string or null | Captured immutable head repository ID, independent of the target identity. |
| `head_ref` | string | The normalized final pull request head branch. |
| `head_sha` | string | The final pull request head commit. |
| `base_ref` | string | The normalized target branch at merge time. |
| `base_sha` | string | The target branch commit recorded by the merge event. |
| `merge_commit_sha` | string | The provider-reported merged commit boundary. |
| `merged_at` | string (RFC 3339) | When the forge recorded the pull request merge. |
| `source_event_id` | string or null | The provider delivery identity when supplied. |
| `captured_at` | string (RFC 3339) | When Sediment stored the fact, stamped server-side. Windows and ordering anchor on this, never on a client clock. |

### PullRequestRevision

One observed pull request head revision.

Canonical schema: `https://sediment.so/schemas/facts/pull-request-revision/v3.json` (version 3; `schemas/facts/pull-request-revision/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `1` or `2` | The positive integer version of the canonical row schema. |
| `repository_provider` | [`ForgeProvider`](#forgeprovider) or null | Captured forge provider. |
| `repository_host` | string or null | Configured lowercase forge host. |
| `repository_id` | string or null | Immutable provider repository ID within its forge host. |
| `revision_id` | string | Stable id for this observed pull request revision. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `provider` | [`ForgeProvider`](#forgeprovider) | Which system this fact came from. |
| `repo` | string | `owner/repo`, lowercased. Empty string when the sender omitted it. |
| `pr_number` | integer | The positive pull request number within the repository. |
| `head_repo` | string | The repository that supplied the pull request head. |
| `head_repository_provider` | [`ForgeProvider`](#forgeprovider) or null | Captured head repository provider, independent of the target identity. |
| `head_repository_host` | string or null | Captured head repository host, independent of the target identity. |
| `head_repository_id` | string or null | Captured immutable head repository ID, independent of the target identity. |
| `head_ref` | string | The normalized observed pull request head branch. |
| `head_sha` | string | The observed pull request head commit. |
| `base_ref` | string | The normalized target branch when observed. |
| `base_sha` | string | The target branch commit when observed. |
| `previous_head_sha` | string or null | The previously observed pull request head commit, or null when absent. |
| `source_event_id` | string or null | The provider delivery identity when supplied. |
| `captured_at` | string (RFC 3339) | When Sediment stored the fact, stamped server-side. Windows and ordering anchor on this, never on a client clock. |

### SessionCommitObservation

The first observed Git-note Session-to-commit edge.

Canonical schema: `https://sediment.so/schemas/facts/session-commit-observation/v3.json` (version 3; `schemas/facts/session-commit-observation/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `1` or `2` | The Fact payload contract: 1 for stored legacy payloads, 2 for identity-capable payloads. |
| `repository_provider` | [`ForgeProvider`](#forgeprovider) or null | Captured forge provider. |
| `repository_host` | string or null | Configured lowercase forge host. |
| `repository_id` | string or null | Immutable provider repository ID within its forge host. |
| `observation_id` | string | Stable id for this Session-to-commit observation. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `repo` | string | The non-empty `owner/repo` that owns the observed commit, lowercased. |
| `commit_sha` | string | Full-length commit sha, lowercased. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `source_push_id` | string | The Push that triggered this Git-note observation. |
| `captured_at` | string (RFC 3339) | When Sediment stored the fact, stamped server-side. Windows and ordering anchor on this, never on a client clock. |

### QuarantineRecord

One append-only fact quarantine action.

Canonical schema: `https://sediment.so/schemas/facts/quarantine-record/v7.json` (version 7; `schemas/facts/quarantine-record/v7.json`).

| Field | Type | Meaning |
|---|---|---|
| `quarantine_id` | string | Stable id for this quarantine record. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `fact_table` | [`FactTable`](#facttable) | The fact table that owns the affected fact. |
| `fact_id` | string | The fact affected by this quarantine record. |
| `action` | [`QuarantineAction`](#quarantineaction) | Whether this record quarantines or releases the fact. |
| `reason` | string | The recorded reason for the quarantine action. |
| `recorded_at` | string (RFC 3339) | When Sediment appended this quarantine record. |

### RepositoryRename

One provider repository rename receipt.

Canonical schema: `https://sediment.so/schemas/facts/repository-rename/v1.json` (version 1; `schemas/facts/repository-rename/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `1` | The positive integer version of the canonical row schema. |
| `rename_id` | string | Stable receipt ID for this captured rename. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `repository_provider` | [`ForgeProvider`](#forgeprovider) | Captured forge provider. |
| `repository_host` | string | Configured lowercase forge host. |
| `repository_id` | string | Immutable provider repository ID within its forge host. |
| `old_repo` | string | Normalized repository label before the rename. |
| `new_repo` | string | Normalized repository label after the rename. |
| `source_event_id` | string or null | The provider delivery identity when supplied. |
| `occurred_at` | string (RFC 3339) or null | Provider-reported rename time; null when the provider supplies none. |
| `captured_at` | string (RFC 3339) | When Sediment stored the fact, stamped server-side. Windows and ordering anchor on this, never on a client clock. |

## External inputs

Versioned operator-supplied inputs used by Derivations but never stored as Facts.

### PriceManifest

One immutable model price policy for cost analysis.

Canonical schema: `https://sediment.so/schemas/external-inputs/price-manifest/v1.json` (version 1; `schemas/external-inputs/price-manifest/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `version` | `1` | The supported price-manifest contract version. |
| `manifest_id` | string | The operator-assigned immutable price-policy identifier. |
| `prices` | array of object | The model price entries in this policy. |

## Derived artifacts

Pure, recomputable artifacts derived from facts and policy.

### RepositoryIdentity

One immutable provider repository identity.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/repository-identity/v1.json` (version 1; `schemas/derived-artifacts/repository-identity/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `provider` | [`ForgeProvider`](#forgeprovider) | The forge provider that assigns the immutable repository ID. |
| `host` | string | The lowercase configured forge host. |
| `repository_id` | string | Immutable provider repository ID within its forge host. |

### AcceptedWorkLifecycleReport

One coverage-aware operational lifecycle report.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/accepted-work-lifecycle/v3.json` (version 3; `schemas/derived-artifacts/accepted-work-lifecycle/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `accepted_work` | object | Inference-call progression for human-explicit accepts. |
| `edit_retention` | object | Edit-observation retention and final Fate evidence. |
| `merge_durability` | object | Attributed file contribution retention through merge. |
| `session_attrition` | object | Terminal classification of accepted Sessions. |
| `rework` | array of object | Independent rework evidence components with separate grains. |
| `strata` | array of object | Supported uniquely joined identity breakdowns. |
| `stratum_skips` | object | Closed counts for identities that could not form a unique stratum. |
| `policy` | object | Resolved lifecycle presentation policy. |
| `provenance` | object | The structured provenance for this artifact. |
| `repository_skipped` | object | Repository evidence omissions, separated by evaluated population and closed reason; units are not additive. |

### Attribution

One completion-to-commit attribution.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/attribution/v2.json` (version 2; `schemas/derived-artifacts/attribution/v2.json`).

| Field | Type | Meaning |
|---|---|---|
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `repo` | string | `owner/repo`, lowercased. Empty string when the sender omitted it. |
| `commit_sha` | string | Full-length commit sha, lowercased. |
| `file_path` | string | The changed file this attribution is keyed on. |
| `inference_call_id` | string | Id of the inference call this record belongs to. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `similarity_score` | number | Token-overlap similarity in [0, 1]. Under `git_notes` attribution it only ranks completions within a proven session; under `jaccard` it is the evidence, and it discounts confidence. |
| `attribution_source` | [`AttributionSource`](#attributionsource) | How the underlying attribution was established. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |
| `repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified provider, host, and repository ID; null for an unambiguous legacy repository. |
| `source_push_id` | string or null | The Push that triggered this Git-note observation. |

### AttributedCompletion

One canonical attributed completion.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/attributed-completion/v4.json` (version 4; `schemas/derived-artifacts/attributed-completion/v4.json`).

| Field | Type | Meaning |
|---|---|---|
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `inference_call_id` | string | Id of the inference call this record belongs to. |
| `repo` | string or null | The attributed repo. Null on an abandonment-evidence row; Sediment does not guess a repo for work that reached no commit. |
| `commit_sha` | string or null | The attributed commit. Null on an abandonment-evidence row. |
| `file_path` | string or null | The changed file the attribution was keyed on. Null on an abandonment-evidence row. |
| `similarity_score` | number or null | The attribution's similarity score, carried through so a projection can discount on it without re-deriving. Null on an abandonment-evidence row. |
| `attribution_source` | [`AttributionSource`](#attributionsource) or null | The attribution's `git_notes` or `jaccard` source. Null on an abandonment-evidence row. |
| `decisions` | array of [`DeveloperDecision`](#developerdecision) | The developer decisions recorded against this completion. |
| `ci_outcomes` | array of [`CIOutcome`](#cioutcome) | The CI runs recorded for this commit. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |
| `split` | `train` or `eval` | Which side of the deterministic eval split this row is on, hashed on `session_id` so a session never straddles both. |
| `abandonment` | [`SessionAbandonment`](#sessionabandonment) or null | The session-level abandonment evidence for an explicit-accept negative. Null on an attribution-evidence row. Exactly one evidence variant must be present. |
| `session_commit_observations` | array of [`SessionCommitObservation`](#sessioncommitobservation) | Captured Session-to-commit Facts matching this artifact, ordered by qualified edge, UTC capture time, and observation ID. Empty means no observed relationship; Attribution remains inferred. |
| `repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified provider, host, and repository ID; null for an unambiguous legacy repository. |
| `source_push_id` | string or null | The Push that triggered this Git-note observation. |

### Rollout

One canonical session trajectory.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/rollout/v4.json` (version 4; `schemas/derived-artifacts/rollout/v4.json`).

| Field | Type | Meaning |
|---|---|---|
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `segments` | array of array of [`Turn`](#turn) | The session's turns, split into contiguous segments. A segment break marks a gap the rollout should not pretend was continuous. |
| `commits` | array of [`CommitRef`](#commitref) | Every commit this session was attributed to. |
| `attribution_source` | [`AttributionSource`](#attributionsource) | How the underlying attribution was established. |
| `terminal_outcomes` | array of [`CIOutcome`](#cioutcome) | The CI outcomes that supply the trajectory's terminal verifier results. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |
| `split` | `train` or `eval` | Which side of the deterministic eval split this row is on, hashed on `session_id` so a session never straddles both. |
| `session_commit_observations` | array of [`SessionCommitObservation`](#sessioncommitobservation) | Captured Facts matching this Session and its repository-qualified commits at the derivation boundary. These Facts do not prove individual-call authorship. |

### CommitRef

One repository-qualified commit.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/commit-ref/v2.json` (version 2; `schemas/derived-artifacts/commit-ref/v2.json`).

| Field | Type | Meaning |
|---|---|---|
| `repo` | string | The repository containing this commit. |
| `commit_sha` | string | The full commit identity within that repository. |
| `repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified provider, host, and repository ID; null for an unambiguous legacy repository. |

### Turn

One rollout turn.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/turn/v3.json` (version 3; `schemas/derived-artifacts/turn/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `new_messages` | array of [`InferenceMessage`](#inferencemessage) | Only the messages this turn added, so a trajectory does not repeat the whole history per turn. |
| `completion` | string | The model's response text. |
| `decisions` | array of [`DeveloperDecision`](#developerdecision) | The developer decisions recorded against this completion. |
| `inference_call_id` | string | Id of the inference call this record belongs to. |
| `tool_calls` | array of [`ToolCallPart`](#toolcallpart) | The tool calls this turn's response made. |

### CIResolution

One attempt-aware commit CI resolution.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/ci-resolution/v3.json` (version 3; `schemas/derived-artifacts/ci-resolution/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `repo` | string | `owner/repo`, lowercased. Empty string when the sender omitted it. |
| `commit_sha` | string | Full-length commit sha, lowercased. |
| `verdict` | [`CIResult`](#ciresult) or null | The resolved pass or failure; null means no directional verdict. |
| `reliability` | number or null | Trust in the resolved verdict, bounded to [0, 1]. |
| `suspected_flake` | boolean | Whether the lineage contains both passing and failing verdict attempts. |
| `workflow_resolutions` | array of [`CIWorkflowResolution`](#ciworkflowresolution) | Attempt-aware resolutions for the commit's workflows. |
| `source_outcome_ids` | array of string | Every CI outcome id that contributed to the resolution. |
| `verdict_outcome_ids` | array of string | The final semantic verdict outcome id from each workflow lineage. |
| `non_verdict_outcome_ids` | array of string | The infrastructure, timeout, cancellation, skip, neutral, or unknown evidence. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |
| `repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified provider, host, and repository ID; null for an unambiguous legacy repository. |

### CIWorkflowResolution

One attempt-aware workflow resolution.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/ci-workflow-resolution/v3.json` (version 3; `schemas/derived-artifacts/ci-workflow-resolution/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `provider` | [`CIProvider`](#ciprovider) | Which system this fact came from. |
| `run_id` | string | The provider-issued pipeline-run identity. |
| `workflow_id` | string or null | The provider-issued workflow definition identity, when supplied. |
| `workflow_name` | string | The CI workflow's display name. |
| `workflow_path` | string or null | The workflow definition's repo-relative path — the stable identity a display name does not give you. Null when unreported. |
| `branch` | string | The branch the run or commit belongs to. |
| `verdict` | [`CIResult`](#ciresult) or null | The resolved pass or failure; null means no directional verdict. |
| `reliability` | number or null | Trust in the resolved verdict, bounded to [0, 1]. |
| `suspected_flake` | boolean | Whether the lineage contains both passing and failing verdict attempts. |
| `source_outcome_ids` | array of string | Every CI outcome id that contributed to the resolution. |
| `verdict_outcome_id` | string or null | The final semantic pass or failure outcome id. |
| `non_verdict_outcome_ids` | array of string | The infrastructure, timeout, cancellation, skip, neutral, or unknown evidence. |

### Provenance

One derivation provenance stamp.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/provenance/v1.json` (version 1; `schemas/derived-artifacts/provenance/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `policy_version` | string | The derivation implementation policy version. |
| `quarantine_revision` | integer | The integer quarantine-log high-water mark used for the derivation. |
| `policy_digest` | string or null | SHA-256 of the fully resolved derivation policy when available. |

### RecoveryAttributionEvidence

Source Attribution metadata for one Recovery enrichment call.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/recovery-attribution-evidence/v1.json` (version 1; `schemas/derived-artifacts/recovery-attribution-evidence/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `inference_call_id` | string | Id of the inference call this record belongs to. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `attribution_sources` | array of [`AttributionSource`](#attributionsource) | Sorted distinct Attribution methods for this enrichment call on this side's commit. |
| `session_commit_observation_ids` | array of string | Sorted unique observation IDs matching this enrichment Session and this side's qualified commit. |

### RecoverySample

One red-to-green recovery sample.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/recovery-sample/v3.json` (version 3; `schemas/derived-artifacts/recovery-sample/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `repo` | string | `owner/repo`, lowercased. Empty string when the sender omitted it. |
| `branch` | string | The branch the run or commit belongs to. |
| `workflow_name` | string | The workflow that failed and then passed. |
| `workflow_path` | string or null | The workflow definition's repo-relative path — the stable identity a display name does not give you. Null when unreported. |
| `failed_commit_sha` | string | The commit whose run failed. |
| `fixed_commit_sha` | string | The commit whose run passed. |
| `failed_outcome_id` | string | Id of the failing CI outcome. |
| `fixed_outcome_id` | string | Id of the passing CI outcome. |
| `recovery_diff` | string | The unified diff from the failing commit to the fixing one. |
| `failed_inference_call_ids` | array of string | Inference calls attributed to the failing commit. |
| `fixed_inference_call_ids` | array of string | Inference calls attributed to the fixing commit. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |
| `failed_attribution_evidence` | array of [`RecoveryAttributionEvidence`](#recoveryattributionevidence) | Optional failed-side enrichment source records, ordered by Inference call and Session ID. |
| `fixed_attribution_evidence` | array of [`RecoveryAttributionEvidence`](#recoveryattributionevidence) | Optional fixed-side enrichment source records, ordered by Inference call and Session ID. |
| `repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified provider, host, and repository ID; null for an unambiguous legacy repository. |

### AcceptedSessionOutcome

One accepted Session's terminal abandonment classification.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/accepted-session-outcome/v2.json` (version 2; `schemas/derived-artifacts/accepted-session-outcome/v2.json`).

| Field | Type | Meaning |
|---|---|---|
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `accepted_decisions` | integer | How many accepted Developer decisions this Session contains. |
| `explicit_accepted_decisions` | integer | How many accepted decisions came from an explicit developer gesture. |
| `last_decision_at` | string (RFC 3339) | The Session's newest decision time, which anchors the grace horizon. |
| `as_of` | string (RFC 3339) | The newest timestamp in the Facts read by the Derivation. |
| `status` | `committed` or `abandoned` or `in_flight` or `attribution_unavailable` | The Session's closed terminal classification: committed, abandoned, in_flight, or attribution_unavailable. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |

### SessionAbandonment

One session whose accepted edits reached no commit.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/session-abandonment/v1.json` (version 1; `schemas/derived-artifacts/session-abandonment/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `accepted_decisions` | integer | How many accepted edits this session had that never reached a commit. |
| `explicit_accepted_decisions` | integer | How many of those accepts came from a real human gesture. Only a session with at least one explicit accept can emit a negative attributed completion. |
| `last_decision_at` | string (RFC 3339) | The session's newest decision time — the anchor the grace horizon is measured back from. |
| `as_of` | string (RFC 3339) | The newest timestamp anywhere in the facts this derivation read. The horizon is measured against this, never the wall clock, so the same facts always yield the same verdict. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |

### Fate

One Edit observation's derived final Fate.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/fate/v3.json` (version 3; `schemas/derived-artifacts/fate/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `observation_id` | string | Stable id for this edit observation. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `agent_harness` | [`AgentHarness`](#agentharness) | Which coding agent harness produced this fact. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `call_id` | string | The edit tool-call id that joins this Fate to its decision. |
| `score` | number | The scorer result after clamping to [0, 1], before threshold mapping. |
| `fate` | [`EditFate`](#editfate) | The categorical final Fate assigned from the retention score. |
| `external_lines_added` | integer or null | Accumulated external lines added from this edit through Session end. Null when any window in the tail lacks coverage. |
| `external_lines_removed` | integer or null | Accumulated external lines removed from this edit through Session end. Null when any window in the tail lacks coverage. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |

### MergeMembershipOutcome

One Attribution's pull-request membership classification.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/merge-membership-outcome/v3.json` (version 3; `schemas/derived-artifacts/merge-membership-outcome/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `repo` | string | `owner/repo`, lowercased. Empty string when the sender omitted it. |
| `inference_call_id` | string | Id of the inference call this record belongs to. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `source_commit_sha` | string | The attributed source commit. |
| `source_file_path` | string | The attributed source path. |
| `attribution_source` | [`AttributionSource`](#attributionsource) | How the underlying attribution was established. |
| `attribution_similarity_score` | number | The similarity score from the source Attribution. |
| `status` | `joined` or `without_merge` or `ambiguous` or `ancestry_unresolved` | The closed classification assigned by this derivation. |
| `pr_number` | integer or null | The uniquely joined pull request; null for an unjoined Attribution. |
| `merge_id` | string or null | The uniquely joined merge boundary; null for an unjoined Attribution. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |
| `session_commit_observation_ids` | array of string | Sorted unique observations of the Session on the source commit; membership does not prove individual-call authorship. |
| `repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified provider, host, and repository ID; null for an unambiguous legacy repository. |

### MergeRetention

One attributed file measured at pull request merge.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/merge-retention/v3.json` (version 3; `schemas/derived-artifacts/merge-retention/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `repo` | string | `owner/repo`, lowercased. Empty string when the sender omitted it. |
| `pr_number` | integer | The positive pull request number within the repository. |
| `merge_id` | string | Stable id for this pull request merge boundary. |
| `inference_call_id` | string | Id of the inference call this record belongs to. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `source_commit_sha` | string | The attributed source commit. |
| `source_file_path` | string | The attributed source path. |
| `head_commit_sha` | string | The final pull request head commit. |
| `head_file_path` | string | The source path resolved at the final pull request head. |
| `merge_commit_sha` | string | The merged commit boundary. |
| `merge_file_path` | string | The source path resolved at the merged commit boundary. |
| `head_retention_score` | number | Four-gram containment of the source addition at the final head. |
| `merge_retention_score` | number | Four-gram containment of the source addition at the merged commit. |
| `attribution_source` | [`AttributionSource`](#attributionsource) | How the underlying attribution was established. |
| `attribution_similarity_score` | number | The similarity score from the source Attribution. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |
| `session_commit_observation_ids` | array of string | Sorted unique observations of the Session on the source commit. The selected call/file Attribution remains inferred. |
| `repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified provider, host, and repository ID; null for an unambiguous legacy repository. |

### AbandonmentSummary

One abandonment coverage summary.

Canonical schema: `https://sediment.so/schemas/derived-artifacts/abandonment-summary/v1.json` (version 1; `schemas/derived-artifacts/abandonment-summary/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `abandoned_sessions` | integer | All sessions derived as abandoned, including implicit-only sessions. |
| `grade_eligible_sessions` | integer | Abandoned sessions with at least one explicit accepted decision. |
| `implicit_only_sessions` | integer | Abandoned sessions whose accepted decisions were all implicit. |
| `negative_completions` | integer | Abandonment-evidence attributed completions emitted for training. |
| `explicit_accepts_unjoined` | integer | Explicit accepted decision facts that could not join uniquely to a completion. |
| `derivation_skipped` | object | Candidate counts by the abandonment derivation's closed skip reasons. |
| `provenance` | [`Provenance`](#provenance) or null | The structured provenance for this artifact. |

## Trainer message objects

Closed nested message contracts used by training rows.

### TrainerFunctionCall

One structured function-call payload.

Canonical schema: `https://sediment.so/schemas/trainer-messages/function-call/v1.json` (version 1; `schemas/trainer-messages/function-call/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `name` | string | The function name associated with this tool call or result. |
| `arguments` | object | The exact structured arguments supplied to the function. |

### TrainerToolCall

One assistant-requested function call.

Canonical schema: `https://sediment.so/schemas/trainer-messages/tool-call/v1.json` (version 1; `schemas/trainer-messages/tool-call/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `id` | string | The tool-call identity used by the matching result. |
| `type` | `function` | The fixed `function` tool-call discriminator. |
| `function` | [`TrainerFunctionCall`](#trainerfunctioncall) | The structured function payload for this tool call. |

### TrainerTextMessage

One developer, system, or user message.

Canonical schema: `https://sediment.so/schemas/trainer-messages/text-message/v1.json` (version 1; `schemas/trainer-messages/text-message/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `role` | `developer` or `system` or `user` | The trainer-facing message role. |
| `content` | string | Plain-text message content. |

### TrainerAssistantContentMessage

One assistant message with visible text.

Canonical schema: `https://sediment.so/schemas/trainer-messages/assistant-content-message/v1.json` (version 1; `schemas/trainer-messages/assistant-content-message/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `role` | `assistant` | The trainer-facing message role. |
| `content` | string | Plain-text message content. |
| `thinking` | string (optional) | Readable assistant reasoning kept outside visible content. |
| `tool_calls` | array of [`TrainerToolCall`](#trainertoolcall) (optional) | Structured function calls requested by the assistant. |

### TrainerAssistantThinkingMessage

One assistant message with readable reasoning.

Canonical schema: `https://sediment.so/schemas/trainer-messages/assistant-thinking-message/v1.json` (version 1; `schemas/trainer-messages/assistant-thinking-message/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `role` | `assistant` | The trainer-facing message role. |
| `thinking` | string | Readable assistant reasoning kept outside visible content. |
| `content` | string (optional) | Plain-text message content. |
| `tool_calls` | array of [`TrainerToolCall`](#trainertoolcall) (optional) | Structured function calls requested by the assistant. |

### TrainerAssistantToolCallMessage

One assistant message with structured tool calls.

Canonical schema: `https://sediment.so/schemas/trainer-messages/assistant-tool-call-message/v1.json` (version 1; `schemas/trainer-messages/assistant-tool-call-message/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `role` | `assistant` | The trainer-facing message role. |
| `tool_calls` | array of [`TrainerToolCall`](#trainertoolcall) | Structured function calls requested by the assistant. |
| `thinking` | string (optional) | Readable assistant reasoning kept outside visible content. |
| `content` | string (optional) | Plain-text message content. |

### TrainerToolMessage

One string tool result.

Canonical schema: `https://sediment.so/schemas/trainer-messages/tool-message/v1.json` (version 1; `schemas/trainer-messages/tool-message/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `role` | `tool` | The trainer-facing message role. |
| `name` | string | The function name associated with this tool call or result. |
| `tool_call_id` | string | The earlier tool-call id this result answers. |
| `content` | string | Plain-text message content. |

## Training rows

Canonical rows written to training JSONL files.

### DPOPair

One DPO pair.

Canonical schema: `https://sediment.so/schemas/training-rows/dpo-pair/v4.json` (version 4; `schemas/training-rows/dpo-pair/v4.json`).

| Field | Type | Meaning |
|---|---|---|
| `prompt` | array of [`TrainerTextMessage`](#trainertextmessage) or [`TrainerAssistantContentMessage`](#trainerassistantcontentmessage) or [`TrainerAssistantThinkingMessage`](#trainerassistantthinkingmessage) or [`TrainerAssistantToolCallMessage`](#trainerassistanttoolcallmessage) or [`TrainerToolMessage`](#trainertoolmessage) | The ordered trainer-facing request-context messages. |
| `chosen` | array of [`TrainerAssistantContentMessage`](#trainerassistantcontentmessage) or [`TrainerAssistantThinkingMessage`](#trainerassistantthinkingmessage) or [`TrainerAssistantToolCallMessage`](#trainerassistanttoolcallmessage) | The preferred trainer-facing response messages. |
| `rejected` | array of [`TrainerAssistantContentMessage`](#trainerassistantcontentmessage) or [`TrainerAssistantThinkingMessage`](#trainerassistantthinkingmessage) or [`TrainerAssistantToolCallMessage`](#trainerassistanttoolcallmessage) | The dispreferred trainer-facing response messages. |
| `tools` | array of never (must be empty) | Tool definitions available to the trainer. Empty for inference-call schema version 1, which carries no tool-definition field. |
| `metadata` | [`DPOMetadata`](#dpometadata) | Sediment evidence excluded from trainer inputs. |

### DPOMetadata

Sediment evidence for one DPO pair.

Canonical schema: `https://sediment.so/schemas/training-rows/dpo-metadata/v4.json` (version 4; `schemas/training-rows/dpo-metadata/v4.json`).

| Field | Type | Meaning |
|---|---|---|
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `source_model` | string | The model that served the source inference call. |
| `chosen_completion_id` | string | The preferred inference call. |
| `rejected_completion_id` | string | The dispreferred inference call from the same prompt and model. |
| `recipe_id` | `dpo_human` or `dpo_outcome` | The closed evidence recipe that produced this training row. |
| `recipe_version` | `2` | The version of the evidence recipe; increment it when recipe semantics change. |
| `chosen_label_source` | `explicit_accept` or `resolved_ci_pass` | The closed evidence source that labeled the preferred member. |
| `rejected_label_source` | `explicit_reject` or `resolved_ci_fail` | The closed evidence source that labeled the dispreferred member. |
| `label_confidence` | number | The weaker member confidence — how much to trust the pair. |
| `ci_reliability` | number or null | Trust in the resolved CI evidence, separate from the categorical label. |
| `confidence_margin` | number | Chosen confidence minus rejected. Can be negative when attribution discounts or configured confidence factors outweigh direction. |
| `provenance` | [`DPOProvenance`](#dpoprovenance) | The structured provenance for this artifact. |
| `split` | `train` or `eval` | Which side of the deterministic eval split this row is on, hashed on `session_id` so a session never straddles both. |
| `chosen_repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified repository identity of the chosen member; null for an unambiguous legacy repository. |
| `rejected_repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified repository identity of the rejected member; independent of the chosen member. |
| `chosen_attribution_source` | [`AttributionSource`](#attributionsource) or null | The selected preferred member's inferred Attribution method, or null without Attribution. |
| `rejected_attribution_source` | [`AttributionSource`](#attributionsource) or null | The selected dispreferred member's inferred Attribution method, or null without Attribution. |
| `chosen_session_commit_observation_ids` | array of string | Sorted unique observation IDs for the preferred member's exact Session-to-commit edge; empty when absent. |
| `rejected_session_commit_observation_ids` | array of string | Sorted unique observation IDs for the dispreferred member's exact Session-to-commit edge; empty when absent. |
| `schema_id` | `https://sediment.so/schemas/training-rows/dpo-pair/v4.json` | The canonical wire-shape contract for this row. |
| `schema_version` | `4` | The positive integer version of the canonical row schema. |

### DPOProvenance

Provenance for both DPO members.

Canonical schema: `https://sediment.so/schemas/training-rows/dpo-provenance/v1.json` (version 1; `schemas/training-rows/dpo-provenance/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `chosen` | [`Provenance`](#provenance) | Provenance for the preferred member. |
| `rejected` | [`Provenance`](#provenance) | Provenance for the dispreferred member. |

### SFTSample

One SFT sample.

Canonical schema: `https://sediment.so/schemas/training-rows/sft-sample/v3.json` (version 3; `schemas/training-rows/sft-sample/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `prompt` | array of [`TrainerTextMessage`](#trainertextmessage) or [`TrainerAssistantContentMessage`](#trainerassistantcontentmessage) or [`TrainerAssistantThinkingMessage`](#trainerassistantthinkingmessage) or [`TrainerAssistantToolCallMessage`](#trainerassistanttoolcallmessage) or [`TrainerToolMessage`](#trainertoolmessage) | The ordered trainer-facing request-context messages. |
| `completion` | array of [`TrainerAssistantContentMessage`](#trainerassistantcontentmessage) or [`TrainerAssistantThinkingMessage`](#trainerassistantthinkingmessage) or [`TrainerAssistantToolCallMessage`](#trainerassistanttoolcallmessage) | The response messages to train on. |
| `tools` | array of never (must be empty) | Tool definitions available to the trainer. Empty for inference-call schema version 1, which carries no tool-definition field. |
| `metadata` | [`SFTMetadata`](#sftmetadata) | Sediment evidence excluded from trainer inputs. |

### SFTMetadata

Sediment evidence for one SFT sample.

Canonical schema: `https://sediment.so/schemas/training-rows/sft-metadata/v3.json` (version 3; `schemas/training-rows/sft-metadata/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `source_model` | string | The model that served the source inference call. |
| `completion_id` | string | The inference call that supplied the training target. |
| `recipe_id` | `sft_curated` or `sft_verified` | The closed evidence recipe that produced this training row. |
| `recipe_version` | `1` | The version of the evidence recipe; increment it when recipe semantics change. |
| `eligibility_source` | `explicit_accept` or `edit_retention` or `resolved_ci_pass` | The closed evidence source that made this supervised target eligible. |
| `label_confidence` | number | Trust in the derived training label, bounded to [0, 1]. |
| `ci_reliability` | number or null | Trust in the resolved CI evidence, separate from the categorical label. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |
| `split` | `train` or `eval` | Which side of the deterministic eval split this row is on, hashed on `session_id` so a session never straddles both. |
| `repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified provider, host, and repository ID; null for an unambiguous legacy repository. |
| `attribution_source` | [`AttributionSource`](#attributionsource) or null | The selected target's inferred Attribution method, independent of recipe eligibility. |
| `session_commit_observation_ids` | array of string | Sorted unique observation IDs for the selected target's exact Session-to-commit edge; empty when absent. |
| `schema_id` | `https://sediment.so/schemas/training-rows/sft-sample/v3.json` | The canonical wire-shape contract for this row. |
| `schema_version` | `3` | The positive integer version of the canonical row schema. |

### DiffSFTSample

One diff-SFT sample.

Canonical schema: `https://sediment.so/schemas/training-rows/diff-sft-sample/v3.json` (version 3; `schemas/training-rows/diff-sft-sample/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `prompt` | array of [`TrainerTextMessage`](#trainertextmessage) or [`TrainerAssistantContentMessage`](#trainerassistantcontentmessage) or [`TrainerAssistantThinkingMessage`](#trainerassistantthinkingmessage) or [`TrainerAssistantToolCallMessage`](#trainerassistanttoolcallmessage) or [`TrainerToolMessage`](#trainertoolmessage) | The ordered trainer-facing request-context messages. |
| `completion` | array of [`TrainerAssistantContentMessage`](#trainerassistantcontentmessage) or [`TrainerAssistantThinkingMessage`](#trainerassistantthinkingmessage) or [`TrainerAssistantToolCallMessage`](#trainerassistanttoolcallmessage) | An assistant message containing the exact patch. |
| `tools` | array of never (must be empty) | Tool definitions available to the trainer. Empty for inference-call schema version 1, which carries no tool-definition field. |
| `metadata` | [`DiffSFTMetadata`](#diffsftmetadata) | Sediment evidence excluded from trainer inputs. |

### DiffSFTMetadata

Sediment evidence for one diff-SFT sample.

Canonical schema: `https://sediment.so/schemas/training-rows/diff-sft-metadata/v3.json` (version 3; `schemas/training-rows/diff-sft-metadata/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `repo` | string | `owner/repo`, lowercased. Empty string when the sender omitted it. |
| `commit_sha` | string | Full-length commit sha, lowercased. |
| `source_model` | string | The model that served the source inference call. |
| `completion_id` | string | The inference call that supplied the training target. |
| `recipe_id` | `sft_curated` or `sft_verified` | The closed evidence recipe that produced this training row. |
| `recipe_version` | `1` | The version of the evidence recipe; increment it when recipe semantics change. |
| `eligibility_source` | `explicit_accept` or `edit_retention` or `resolved_ci_pass` | The closed evidence source that made this supervised target eligible. |
| `label_confidence` | number | Trust in the derived training label, bounded to [0, 1]. |
| `ci_reliability` | number or null | Trust in the resolved CI evidence, separate from the categorical label. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |
| `split` | `train` or `eval` | Which side of the deterministic eval split this row is on, hashed on `session_id` so a session never straddles both. |
| `source_ids` | [`SourceIds`](#sourceids) | The facts this row was assembled from. |
| `repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified provider, host, and repository ID; null for an unambiguous legacy repository. |
| `attribution_sources` | array of [`AttributionSource`](#attributionsource) | Sorted distinct Attribution methods of members supplying the emitted patch. |
| `schema_id` | `https://sediment.so/schemas/training-rows/diff-sft-sample/v3.json` | The canonical wire-shape contract for this row. |
| `schema_version` | `3` | The positive integer version of the canonical row schema. |

### SourceIds

Source fact ids for one diff-SFT sample.

Canonical schema: `https://sediment.so/schemas/training-rows/diff-sft-source-ids/v2.json` (version 2; `schemas/training-rows/diff-sft-source-ids/v2.json`).

| Field | Type | Meaning |
|---|---|---|
| `inference_call_id` | string | Id of the inference call this record belongs to. |
| `decision_ids` | array of string | Decisions that contributed to the row. |
| `ci_outcome_ids` | array of string | CI outcomes that contributed to the row. |
| `session_commit_observation_ids` | array of string | Sorted unique observation IDs from members supplying the emitted patch. |

### RecoveryRow

One recovery training row.

Canonical schema: `https://sediment.so/schemas/training-rows/recovery-row/v3.json` (version 3; `schemas/training-rows/recovery-row/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `recipe_id` | `recovery_ci` | The closed evidence recipe that produced this training row. |
| `recipe_version` | `1` | The version of the evidence recipe; increment it when recipe semantics change. |
| `repo` | string | `owner/repo`, lowercased. Empty string when the sender omitted it. |
| `branch` | string | The branch the run or commit belongs to. |
| `workflow_name` | string | The workflow that failed and then passed. |
| `workflow_path` | string or null | The workflow definition's repo-relative path — the stable identity a display name does not give you. Null when unreported. |
| `failed_commit_sha` | string | The commit whose run failed. |
| `fixed_commit_sha` | string | The commit whose run passed. |
| `failed_outcome_id` | string | Id of the failing CI outcome. |
| `fixed_outcome_id` | string | Id of the passing CI outcome. |
| `recovery_diff` | string | The unified diff from the failing commit to the fixing one. |
| `failed_inference_call_ids` | array of string | Inference calls attributed to the failing commit. |
| `fixed_inference_call_ids` | array of string | Inference calls attributed to the fixing commit. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |
| `split` | `train` or `eval` | Which side of the deterministic eval split this row is on, hashed on `session_id` so a session never straddles both. |
| `repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified provider, host, and repository ID; null for an unambiguous legacy repository. |
| `failed_attribution_evidence` | array of [`RecoveryAttributionEvidence`](#recoveryattributionevidence) | Failed-side enrichment sources retained from the Recovery sample; empty when absent. |
| `fixed_attribution_evidence` | array of [`RecoveryAttributionEvidence`](#recoveryattributionevidence) | Fixed-side enrichment sources retained from the Recovery sample; empty when absent. |
| `schema_id` | `https://sediment.so/schemas/training-rows/recovery-row/v3.json` | The canonical wire-shape contract for this row. |
| `schema_version` | `3` | The positive integer version of the canonical row schema. |

### Verification

Operator verifier configuration.

Canonical schema: `https://sediment.so/schemas/training-rows/verification/v1.json` (version 1; `schemas/training-rows/verification/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `verification_command` | string | The opaque operator-configured verifier command. |

### RLVRDecisionRow

One decision attached to an RLVR turn.

Canonical schema: `https://sediment.so/schemas/training-rows/rlvr-decision/v2.json` (version 2; `schemas/training-rows/rlvr-decision/v2.json`).

| Field | Type | Meaning |
|---|---|---|
| `accepted` | boolean | Whether the developer took the change. |
| `explicit` | boolean | Whether a real human gesture produced this decision. |
| `agent_harness` | [`AgentHarness`](#agentharness) | Which coding agent harness produced this fact. |
| `interaction_mode` | [`InteractionMode`](#interactionmode) | Whether the edit interaction was `agent` or `inline`. |
| `file_path` | string | Repo-relative path of the file the record concerns. |

### RLVRInferenceMessageRow

One RLVR trajectory message.

Canonical schema: `https://sediment.so/schemas/training-rows/rlvr-inference-message/v2.json` (version 2; `schemas/training-rows/rlvr-inference-message/v2.json`).

| Field | Type | Meaning |
|---|---|---|
| `role` | string | The trainer-facing message role. |
| `parts` | array of [`TextPart`](#textpart) or [`ReasoningPart`](#reasoningpart) or [`ToolCallPart`](#toolcallpart) or [`ToolCallResponsePart`](#toolcallresponsepart) | Ordered text, reasoning, tool-call, or tool-response parts. |
| `finish_reason` | string (optional) | The model service's finish reason; omitted when unreported. |

### RLVRTurnRow

One RLVR trajectory turn.

Canonical schema: `https://sediment.so/schemas/training-rows/rlvr-turn/v3.json` (version 3; `schemas/training-rows/rlvr-turn/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `inference_call_id` | string | Id of the inference call this record belongs to. |
| `new_messages` | array of [`RLVRInferenceMessageRow`](#rlvrinferencemessagerow) | Only the structured messages this turn added to the trajectory. |
| `completion` | string | The model's response text. |
| `tool_calls` | array of [`ToolCallPart`](#toolcallpart) | Structured function calls requested by the assistant. |
| `decisions` | array of [`RLVRDecisionRow`](#rlvrdecisionrow) | The developer decisions recorded against this completion. |

### NemoGymResponsesCreateParams

One NeMo Gym segment input.

Canonical schema: `https://sediment.so/schemas/training-rows/nemo-gym-responses-create-params/v2.json` (version 2; `schemas/training-rows/nemo-gym-responses-create-params/v2.json`).

| Field | Type | Meaning |
|---|---|---|
| `input` | array of [`RLVRInferenceMessageRow`](#rlvrinferencemessagerow) | The structured messages at the start of this segment. |

### NemoGymResponse

One NeMo Gym response trajectory.

Canonical schema: `https://sediment.so/schemas/training-rows/nemo-gym-response/v3.json` (version 3; `schemas/training-rows/nemo-gym-response/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `turns` | array of [`RLVRTurnRow`](#rlvrturnrow) | The captured turns in this contiguous rollout segment. |

### SedimentTaskRow

One Sediment RLVR task.

Canonical schema: `https://sediment.so/schemas/training-rows/sediment-rlvr-task/v3.json` (version 3; `schemas/training-rows/sediment-rlvr-task/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `instance_id` | string | Stable target-row identity derived from recorded facts. |
| `recipe_id` | `rlvr_ci` | The closed evidence recipe that produced this training row. |
| `recipe_version` | `1` | The version of the evidence recipe; increment it when recipe semantics change. |
| `reward_source` | `resolved_ci_pass` or `resolved_ci_fail` | The closed source of the directional reward; absent without a verdict. |
| `repo` | string | `owner/repo`, lowercased. Empty string when the sender omitted it. |
| `base_commit` | string | The commit that the historical reference patch applies to. |
| `problem_statement` | string | The first recorded user message for the task. |
| `reference_patch` | string | The observed historical patch, whether it passed or failed. |
| `verification` | [`Verification`](#verification) (optional) | Operator configuration for running the verifier again. |
| `verifier_results` | array of [`CIOutcome`](#cioutcome) | The exact recorded CI outcomes that support this row. |
| `ci_resolution` | [`CIResolution`](#ciresolution) | The selected attempt-aware CI resolution for this row. |
| `attribution_source` | [`AttributionSource`](#attributionsource) | How the underlying attribution was established. |
| `split` | `train` or `eval` | Which side of the deterministic eval split this row is on, hashed on `session_id` so a session never straddles both. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |
| `session_commit_observation_ids` | array of string | Sorted unique observation IDs matching the emitted CI resolution and source Rollout Session. |
| `repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified provider, host, and repository ID; null for an unambiguous legacy repository. |
| `schema_id` | `https://sediment.so/schemas/training-rows/sediment-rlvr-task/v3.json` | The canonical wire-shape contract for this row. |
| `schema_version` | `3` | The positive integer version of the canonical row schema. |

### SedimentRolloutRow

One Sediment RLVR rollout segment.

Canonical schema: `https://sediment.so/schemas/training-rows/sediment-rlvr-rollout/v4.json` (version 4; `schemas/training-rows/sediment-rlvr-rollout/v4.json`).

| Field | Type | Meaning |
|---|---|---|
| `instance_id` | string | Stable target-row identity derived from recorded facts. |
| `recipe_id` | `rlvr_ci` | The closed evidence recipe that produced this training row. |
| `recipe_version` | `1` | The version of the evidence recipe; increment it when recipe semantics change. |
| `reward_source` | `resolved_ci_pass` or `resolved_ci_fail` (optional) | The closed source of the directional reward; absent without a verdict. |
| `ci_resolution` | [`CIResolution`](#ciresolution) (optional) | The selected attempt-aware CI resolution for this row. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `segment_index` | integer | Zero-based position of this contiguous segment. |
| `turns` | array of [`RLVRTurnRow`](#rlvrturnrow) | The captured turns in this contiguous rollout segment. |
| `verifier_results` | array of [`CIOutcome`](#cioutcome) | The exact recorded CI outcomes that support this row. |
| `ci_resolutions` | array of [`CIResolution`](#ciresolution) | The attempt-aware CI resolutions visible to this row. |
| `attribution_source` | [`AttributionSource`](#attributionsource) | How the underlying attribution was established. |
| `split` | `train` or `eval` | Which side of the deterministic eval split this row is on, hashed on `session_id` so a session never straddles both. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |
| `session_commit_observation_ids` | array of string | Sorted unique observation IDs matching emitted CI resolutions and source Rollout Session. |
| `repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified provider, host, and repository ID; null for an unambiguous legacy repository. |
| `schema_id` | `https://sediment.so/schemas/training-rows/sediment-rlvr-rollout/v4.json` | The canonical wire-shape contract for this row. |
| `schema_version` | `4` | The positive integer version of the canonical row schema. |

### SWEBenchTaskRow

One SWE-bench task.

Canonical schema: `https://sediment.so/schemas/training-rows/swe-bench-task/v3.json` (version 3; `schemas/training-rows/swe-bench-task/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `instance_id` | string | Stable target-row identity derived from recorded facts. |
| `repo` | string | `owner/repo`, lowercased. Empty string when the sender omitted it. |
| `base_commit` | string | The commit that the historical reference patch applies to. |
| `problem_statement` | string | The first recorded user message for the task. |
| `patch` | string | A historical patch with a recorded passing terminal verifier result. |
| `metadata` | [`SWEBenchMetadata`](#swebenchmetadata) | Sediment evidence excluded from trainer inputs. |

### SWEBenchMetadata

Sediment evidence for one SWE-bench task.

Canonical schema: `https://sediment.so/schemas/training-rows/swe-bench-metadata/v3.json` (version 3; `schemas/training-rows/swe-bench-metadata/v3.json`).

| Field | Type | Meaning |
|---|---|---|
| `recipe_id` | `rlvr_ci` | The closed evidence recipe that produced this training row. |
| `recipe_version` | `1` | The version of the evidence recipe; increment it when recipe semantics change. |
| `reward_source` | `resolved_ci_pass` | The closed source of the directional reward; absent without a verdict. |
| `verification` | [`Verification`](#verification) (optional) | Operator configuration for running the verifier again. |
| `verifier_results` | array of [`CIOutcome`](#cioutcome) | The exact recorded CI outcomes that support this row. |
| `ci_resolution` | [`CIResolution`](#ciresolution) | The selected attempt-aware CI resolution for this row. |
| `attribution_source` | [`AttributionSource`](#attributionsource) | How the underlying attribution was established. |
| `split` | `train` or `eval` | Which side of the deterministic eval split this row is on, hashed on `session_id` so a session never straddles both. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |
| `session_commit_observation_ids` | array of string | Sorted unique observation IDs matching the emitted CI resolution and source Rollout Session. |
| `repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified provider, host, and repository ID; null for an unambiguous legacy repository. |
| `schema_id` | `https://sediment.so/schemas/training-rows/swe-bench-task/v3.json` | The canonical wire-shape contract for this row. |
| `schema_version` | `3` | The positive integer version of the canonical row schema. |

### NemoGymRolloutRow

One NeMo Gym rollout mapping.

Canonical schema: `https://sediment.so/schemas/training-rows/nemo-gym-rollout/v4.json` (version 4; `schemas/training-rows/nemo-gym-rollout/v4.json`).

| Field | Type | Meaning |
|---|---|---|
| `responses_create_params` | [`NemoGymResponsesCreateParams`](#nemogymresponsescreateparams) | The captured segment input in the NeMo Gym field. |
| `response` | [`NemoGymResponse`](#nemogymresponse) | The captured segment turns in the NeMo Gym response field. |
| `reward` | number (optional) | Numeric reinforcement-learning value; absent without pass/fail evidence. |
| `metadata` | [`NemoGymMetadata`](#nemogymmetadata) | Sediment evidence excluded from trainer inputs. |

### NemoGymMetadata

Sediment evidence for one NeMo Gym rollout.

Canonical schema: `https://sediment.so/schemas/training-rows/nemo-gym-metadata/v4.json` (version 4; `schemas/training-rows/nemo-gym-metadata/v4.json`).

| Field | Type | Meaning |
|---|---|---|
| `instance_id` | string | Stable target-row identity derived from recorded facts. |
| `recipe_id` | `rlvr_ci` | The closed evidence recipe that produced this training row. |
| `recipe_version` | `1` | The version of the evidence recipe; increment it when recipe semantics change. |
| `reward_source` | `resolved_ci_pass` or `resolved_ci_fail` (optional) | The closed source of the directional reward; absent without a verdict. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `segment_index` | integer | Zero-based position of this contiguous segment. |
| `verification` | [`Verification`](#verification) (optional) | Operator configuration for running the verifier again. |
| `verifier_results` | array of [`CIOutcome`](#cioutcome) | The exact recorded CI outcomes that support this row. |
| `ci_resolution` | [`CIResolution`](#ciresolution) (optional) | The selected attempt-aware CI resolution for this row. |
| `attribution_source` | [`AttributionSource`](#attributionsource) | How the underlying attribution was established. |
| `split` | `train` or `eval` | Which side of the deterministic eval split this row is on, hashed on `session_id` so a session never straddles both. |
| `provenance` | [`Provenance`](#provenance) | The structured provenance for this artifact. |
| `session_commit_observation_ids` | array of string | Sorted unique observation IDs matching the emitted CI resolution and source Rollout Session; empty without a resolution. |
| `repository_identity` | [`RepositoryIdentity`](#repositoryidentity) or null | Qualified provider, host, and repository ID; null for an unambiguous legacy repository. |
| `schema_id` | `https://sediment.so/schemas/training-rows/nemo-gym-rollout/v4.json` | The canonical wire-shape contract for this row. |
| `schema_version` | `4` | The positive integer version of the canonical row schema. |

## Derived bundle

The manifest and integrity metadata beside canonical artifact JSONL files.

### BundleManifest

The top-level derived-bundle manifest.

Canonical schema: `https://sediment.so/schemas/derived-bundle/manifest/v4.json` (version 4; `schemas/derived-bundle/manifest/v4.json`).

| Field | Type | Meaning |
|---|---|---|
| `bundle_schema_version` | `4` | The derived-bundle container version. Canonical bundles use 4. |
| `record_encoding` | `sediment-record-json-v1` | The declared lossless inner-record decoder: `sediment-record-json-v1`. |
| `identity_population` | `organization-through-as-of-v1` | The producer-declared complete organization identity population through inclusive `as_of`; not proof of undisclosed external Facts. |
| `repository_population` | `organization-through-as-of-v1` | The producer-declared complete organization repository evidence and rename population through inclusive `as_of`; not proof of undisclosed external Facts. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `scope` | object | The inclusive `since`, exclusive `until`, and user allowlist. An absent user allowlist means every user in the configured organization. |
| `policy` | object | Every resolved attribution and split policy value. |
| `policy_digest` | string | SHA-256 of the canonical resolved policy. |
| `implementation_versions` | object | The repository identity, abandonment, attribution, attributed-completion, and rollout implementation versions. |
| `quarantine_revision` | integer | The fact-store quarantine token from the bundle's read snapshot. |
| `as_of` | string (RFC 3339) or null | The newest visible input-Fact timestamp, set before observation binding; required when artifacts carry Session observations, or null for an empty Fact set. |
| `mirror_revisions` | object | The ref-to-object map for every mirror held stable during derivation, keyed by encoded qualified repository identity. |
| `counts` | object | Row count for each canonical JSONL file. |
| `skipped` | object | Data-quality derivation skips by closed reason. |
| `excluded` | object | Intended cohort exclusions by closed reason. |
| `files` | object | Integrity metadata keyed by canonical artifact name. |
| `fragmented` | object | Retained Rollout boundaries by closed reason: `prior_output_absent`, `input_history_changed`, or `prior_output_not_replayed`; nonnegative counts copied from Derivation. |

### InferenceCallIdentity

One declared source identity and its distinct attachment aliases.

Canonical schema: `https://sediment.so/schemas/derived-bundle/inference-call-identity/v1.json` (version 1; `schemas/derived-bundle/inference-call-identity/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `inference_call_id` | string | Id of the inference call this record belongs to. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `session_id` | string | The coding-agent session this belongs to (ADR 0002). |
| `observed_at` | string (RFC 3339) | When Sediment observed the call, stamped at capture. |
| `call_ids` | array of string | Sorted distinct provider-call and output tool-call aliases belonging to this Fact. |

### RepositoryIdentityEvidence

One captured repository role and its exact source Fact.

Canonical schema: `https://sediment.so/schemas/derived-bundle/repository-identity-evidence/v1.json` (version 1; `schemas/derived-bundle/repository-identity-evidence/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `source_table` | `pushes` or `ci_outcomes` or `session_commit_observations` or `pull_request_merges` or `pull_request_revisions` | Fact family that owns this repository-role projection. |
| `source_fact_id` | string | Exact source Fact ID retained without replacement. |
| `role` | `repo` or `head_repo` | Target repository or independent pull-request head repository role. |
| `org_id` | string | The deployment's tenant id, normalized. Bound to the server. |
| `repo` | string | `owner/repo`, lowercased. Empty string when the sender omitted it. |
| `repository_provider` | [`ForgeProvider`](#forgeprovider) or null | Captured forge provider. |
| `repository_host` | string or null | Configured lowercase forge host. |
| `repository_id` | string or null | Immutable provider repository ID within its forge host. |
| `captured_at` | string (RFC 3339) | When Sediment stored the fact, stamped server-side. Windows and ordering anchor on this, never on a client clock. |
| `source_push_id` | string or null | The Push that triggered this Git-note observation. |

### BundleRecord

Strict JSON envelope for one lossless canonical record.

Canonical schema: `https://sediment.so/schemas/derived-bundle/record/v1.json` (version 1; `schemas/derived-bundle/record/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `record_json` | string | One ASCII-escaped canonical record serialized with the declared `NaN`, `Infinity`, and `-Infinity` extensions. The outer envelope is strict JSON. |

### BundleFileMetadata

Integrity metadata for one bundle JSONL file.

Canonical schema: `https://sediment.so/schemas/derived-bundle/file-metadata/v1.json` (version 1; `schemas/derived-bundle/file-metadata/v1.json`).

| Field | Type | Meaning |
|---|---|---|
| `path` | string | The fixed bundle-relative JSONL path. |
| `rows` | integer | The JSONL row count. |
| `bytes` | integer | The exact file size in bytes. |
| `sha256` | string | SHA-256 of the complete file bytes. |

## Enumerations

### GatewayProvider

Which LLM gateway captured an inference call.

Values: `litellm`, `portkey`, `helicone`, `unknown`

### AgentHarness

Which coding agent harness emitted a developer-side fact.

Values: `claude-code`, `copilot`, `codex`, `cursor`, `pi`

### InteractionMode

Whether the edit interaction was agent or inline.

Values: `agent`, `inline`

### CIProvider

Which CI system reported an outcome.

Values: `github_actions`, `jenkins`, `gitlab_ci`, `circleci`, `buildkite`, `other`

### CIResult

How a CI run ended.

Values: `passed`, `failed`, `error`, `timed_out`, `cancelled`, `skipped`, `neutral`, `unknown`

### ForgeProvider

Which git host a push came from.

Values: `github`

### FactTable

Which fact table owns a quarantined fact.

Values: `inference_calls`, `developer_decisions`, `ci_outcomes`, `pushes`, `pull_request_merges`, `pull_request_revisions`, `edit_observations`, `rejected_edits`, `retry_linkages`, `session_commit_observations`, `repository_renames`

### QuarantineAction

Whether a record quarantines or releases a fact.

Values: `quarantine`, `release`

### AttributionSource

How an attribution was established — the provenance that gates confidence: `git_notes` is never discounted, `jaccard` multiplies confidence by the attribution score.

Values: `git_notes`, `jaccard`

### EditFate

The derived final Fate of an applied edit.

Values: `deleted`, `partially_modified`, `unmodified`

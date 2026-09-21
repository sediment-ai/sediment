# Capture playbook — `packages/capture`

Values drift, so the cited file wins. Citations use `path` or `path::symbol`.
`packages/capture/sediment_capture/otlp.py`'s module docstring defines the translator contract.
This file adds per-harness mechanics and the database dedup choice.

For the reader-facing comparison of acceptance, explicitness, decision unit,
and missing signals, see [Developer decisions by agent harness](../explanation/how-capture-works.md#developer-decisions-by-agent-harness).

## Module map

| Module | Purpose |
|---|---|
| `gateway.py` | `LiteLLMAdapter.normalize()` + the `ADAPTERS` registry (LiteLLM is the only registered adapter) |
| `otlp.py` | `parse_otlp_logs` returns all four Fact types with record/container counts; reuses the four decision translators concatenated by `parse_otlp_decisions`, plus Edit observation, rejected Edit, and Retry linkage parsers |
| `session_identity.py` | `resolve_identity`: session/user identity from gateway payloads — where new identity heuristics go |
| `github.py` | Webhook HMAC plus Push, workflow-run, and pull-request merge translators |

## The translator contract

The contract itself is normative in `otlp.py`'s module docstring. Adding an agent = one translator + one `otlp.py::_TRANSLATORS` entry + frozen fixtures + a fixture README row. Exception: shim capture
(`docs/agents/capture-clients.md`) emits `sediment.tool_decision` — no
translator, only an enum member.

**Fail-soft is universal.** A malformed payload skips and logs, never raises — a
raise becomes a 500 at the route and loses every sibling Fact in the batch.
Discriminator checks precede membership or hashing; added declines log `malformed_discriminator`, `unsupported_discriminator`, or `invalid_fact` with source and record position.
Canonical validation precedes storage; database failures propagate and failed batches roll back. Numeric bounds match PostgreSQL BIGINT. Copilot retention checks finiteness only for floats; oversized integers degrade the graded fields without losing the decision, its raw record, or valid siblings.

`otlp.py::parse_otlp_logs` returns `OTLPCaptureResult` without changing the list-returning parser APIs. Each translator marks a record position only after emitting a Fact; preserve whole-batch joins, output order, and existing object-only diagnostic positions. Record counts partition supplied list entries; malformed envelope containers use a separate unit. Supporting tool-result records count as untranslated when they emit no Fact themselves. [Capture receipts](../explanation/how-capture-works.md#capture-receipts) defines the operational fields.

`cli/sediment_cli/transcript.py` packs only its independent observation, rejection, and linkage records into exact encoded requests bounded by the shared delivery limit. Native batches retain their joins. `emission_summary` counts candidates by Fact kind and prepared, queued, acknowledged, unsuccessful, oversized, and unsubmitted populations by explicit request/record units. Unsuccessful keys are `pending`, `blocked`, `declined`, `invalid_disposition`, `io_error`, and `publication_error`; oversized records count under `record_too_large`. Only complete acceptance clears snapshots. [Sender replay operations](../capture/local-capture.md#preserve-prepared-payloads-through-outages) defines per-request durability and partial-publication recovery limits.

## The keyed-index decision (read before adding an agent harness)

An agent harness's decisions enter the keyed dedup index,
`sediment_core/postgres_schema.py::uq_decisions_keyed`, **only once you have verified
its `call_id` as row-unique on the wire**. Until then it dedups under the
natural-key index.

Choose wrong and the failure arrives as *silently dropped Facts* rather than
as visible duplicates. That makes this the most consequential decision when
you add an agent. The schema comment in `store.py` is the authority, and
[Keyed / keyless fact](../../CONTEXT.md#keyed--keyless-fact) defines the
terms.

## Per-harness mechanics

### LiteLLM (inference calls — `gateway.py`)
- Capture writes `InferenceCall` schema version 1. `model_call_id` comes
  from `litellm_call_id`, then from `response["id"]`. With neither, the Fact
  is keyless. Capture metadata supplies an organization-scoped stable Fact ID and original `observed_at`; unstamped keyless envelopes retain legacy behavior.
- `input_messages` and `output_messages` preserve ordered typed parts. Text,
  readable reasoning, response-side tool calls, and tool-call responses remain
  distinct. Tool arguments use `arguments` and never duplicate into assistant text.
- Readable `thinking` and `reasoning` blocks map to `ReasoningPart`. On output
  messages, structured `thinking_blocks` take precedence over flattened
  `reasoning_content`, and reasoning precedes visible assistant text and tool
  calls. Signatures, encrypted reasoning, and `redacted_thinking` stay in
  Basic-redacted `raw`. Opaque reasoning skips under `opaque_reasoning` and logs
  at INFO when no other degradation occurs.
- Chat `message.tool_calls` and Responses `output` `function_call` items map
  to tool-call parts. Malformed arguments keep the part with `arguments={}`;
  id-less and unsupported parts skip. `inference_messages_degraded` counts all
  cases; every non-opaque case logs at WARNING. `raw` retains the Basic-redacted
  source.
- `gateway_provider` identifies LiteLLM. `custom_llm_provider` supplies
  nullable `model_provider`. Missing model, user, usage, and duration values
  remain null. `observed_at` uses callback capture time when supplied, receiver time otherwise.
- `render_scoring_text` in `packages/derive` produces Attribution text from
  visible output and tool arguments without persisting it. It excludes readable
  reasoning and tool-call responses. Do not add a flattened capture field.
- Fixtures: `litellm_slo_tool_call.json` covers the chat shape;
  `litellm_responses_standard_logging_object.json` covers Responses-format
  tool calls, results, and request-side echoes.
- Two payload shapes (real `StandardLoggingPayload` vs the callback's
  fallback); the adapter probes both. `raw` keeps the payload after the
  storage seam applies Basic redaction.
- Session id: `session_identity.py::resolve_identity` — LiteLLM-SLO-only
  heuristics. Non-string identity carriers log `gateway_identity_invalid_shape` and fall through to a valid source. The callback preserves their original shapes; arbitrary nonblank metadata strings remain valid IDs.

### Copilot (decisions — `otlp.py`)
- `call_id`: record attribute `request_id`. Discriminator: `event.name`. Session
  id: resource scope. `raw`: the log record.
- Edit-survival events are always implicit and only for `edit_source ==
  "apply_patch"` — inline survival skips to avoid double-counting `inline.done`
  (which needs a real bool). `outcome == "saved"` is not a decision. Truncated
  values (`…[N chars]`) still parse as valid JSON.

### Claude Code (decisions — `otlp.py`)
- `call_id`: `tool_use_id`, in-batch join to `claude_code.tool_result`.
  Discriminator: the namespaced name is in the record **body** (`event.name` is
  the bare `tool_decision`). Session id: record scope, resource fallback.
- `decision` is `accept|reject`; anything else skips with an
  `otlp_record_unknown_decision` log.
- Sources: explicit `user_permanent|user_temporary|user_abort|user_reject`,
  implicit `config|hook`; anything else → implicit (fail-safe) plus an
  `otlp_record_unknown_source` log. Compared raw — the Codex case-fold is
  that wire's quirk. Edit-tool allowlist `{Edit, Write, MultiEdit,
  NotebookEdit}` (NotebookEdit uses `notebook_path`).
- `file_path` recovery needs `OTEL_LOG_TOOL_DETAILS=1`; rejects emit the
  `file_path=""` sentinel — a domain value, never "fixed" to `None`
  (`attachment.py` owns the read-time collapse).
- `raw`: `{"decision": record, "result": record|None}`.

### Codex (decisions — `otlp.py`)
- `call_id`: `call_id`, in-batch join to `codex.tool_result`, then one decision
  per file from the V4A diff. The diff can arrive through native `apply_patch`
  or an `exec_command` that starts with an `apply_patch` heredoc. Other shell
  commands stay excluded. Session id: `conversation.id`. Explicit iff
  `source == "user"` (case-folded because the wire capitalizes it).
- `decision`: `approved|approved_for_session` accept, `denied|abort` reject;
  anything else (for example, `timed_out`) skips with an `otlp_record_unknown_decision`
  log.
- Wire quirks that corrupt data if dropped: `timeUnixNano == "0"` is unset
  (proto3) — real time is `observedTimeUnixNano`; V4A file markers match at
  **column 0 only**; path-less patches emit the `file_path=""` sentinel. A
  shell-tool decision needs its result because the decision doesn't prove an edit.
- `raw` scrubs `user.email` and `user.account_id` (`otlp.py::_CX_PII`), but retains patch/tool arguments. Native decision capture can therefore carry code without transcript opt-in; `log_user_prompt=false` doesn't remove it.

### Harness clients (decisions — `sediment.tool_decision`)
- The harness-neutral wire (`docs/agents/capture-clients.md`): one record
  carries everything (the shim observes the edit; no decision/result join).
  Discriminator: the record **body**. Session id: record + resource fallback.
- `agent` is mandatory, mapped to `AgentHarness` at the trust boundary —
  unregistered values skip with a trail (`otlp_record_unknown_agent`), never a
  placeholder. `explicit` must be a real `boolValue`. `file_path` inline;
  rejects may carry the `""` sentinel.
- Clients filter to edit tools. The pi extension and Cursor adapter emit this wire.
  Cursor sends successful Agent `Write` calls as implicit accepts; failures and Tab edits mark Attribution only. Native Write receipt replay uses a deterministic Fact id (the exact key is in `otlp.py`'s normative contract), enforced by the existing PostgreSQL primary key. Other shim identity behavior stays unchanged.
  Cursor preserves hook receipt time, not an inferred execution time. Historical random-ID Facts stay immutable; the first later receipt can coexist once after upgrade. `raw`: `{"decision": attrs}`; fixture: `fixtures/otlp/sediment/tool_decision.json`.

### Edit observations (`sediment.edit_observation` — `parse_otlp_edit_observations`)
- ADR 0007 maps `applied_text` and `observed_file_text` directly.
- `agent` is mandatory and maps to `AgentHarness` like decisions.
- `external_lines_added`/`external_lines_removed`: optional `intValue`
  counts; junk or negative values become absent without dropping the text pair.

### Rejected edits (`sediment.rejected_edit` — `parse_otlp_rejected_edits`)
- Self-filters by body; `agent` is mandatory with no fallback. The client
  distinguishes refusals from tool failures. The server re-enforces the 256 KiB
  cap; `proposed=""` is legal.

### Retry linkages (`sediment.retry_linkage` — `parse_otlp_retry_linkages`)

- Requires Session, agent, file, Edit/Write tool, distinct call ids, and time,
  with no defaults. Each skip logs and counts one closed reason; malformed records don't drop valid siblings or carry transcript text.

### GitHub webhooks (`github.py`)
- Workflow conclusions map without collapsing states: `success` → `passed`, `failure` →
  `failed`; the four named non-verdicts map by name and everything else to `unknown`.
- `parse_push` returns `None` for non-branch refs, falsy `after`, `deleted is
  True`, and all-zeros SHAs of any length. Repo-less Facts still store (Facts
  first; quarantine covers bad Facts) with a warning.
- `workflow_run.id` becomes required `run_id`; absent attempts and URLs stay null.
  `workflow_id`, exact result, event type, and `X-GitHub-Delivery` id remain evidence.
- `parse_pull_request_merge` accepts only a merged `closed` event. `parse_pull_request_revision` accepts `opened` and `synchronize`, records the preceding synchronize head, and rejects `after`/head disagreement as `synchronize_head_mismatch`. Both require complete repository, pull-request, ref, and SHA boundaries, preserve `X-GitHub-Delivery`, and fail soft. A deleted source fork (`head.repo: null`) leaves `head_repo` with no name to record. Both parsers decline it under a distinct logged reason (`pull_request_merge_head_repository_deleted` / `pull_request_revision_head_repository_deleted`, the latter also returned as skip reason `head_repository_deleted`) instead of the generic incomplete-boundary reason.
- Signed payloads supply repository IDs; trusted `github_host` supplies the namespace. Canonical positive integer/text IDs are accepted; malformed or absent identity logs `repository_identity_invalid` or `repository_identity_absent` and preserves an otherwise valid Fact. PR head and target identities remain independent.
- `parse_repository_rename` retains normalized old/new names and optional delivery ID; occurrence time stays absent. Closed skips add `unsupported_discriminator`, `malformed_discriminator`, `invalid_repository_rename_boundary`, and `repository_name_unchanged`. [ADR 0019](../adr/0019-repository-identity-and-renames.md) defines identity and rename boundaries.
- `verify_signature` compares **bytes** (`hmac.compare_digest` raises on
  non-ASCII str — a crafted header must be a 401, not a 500).

## Decisions metadata rules
- `occurred_at` follows [model-layer rules](fact-store.md#model-layer-rules-modelspy).
- `explicit` is harness-specific; implicit decisions only move Confidence.
- Copilot wire `survival_rate_four_gram` and `time_delay_ms` map to
  `edit_retention_score` and `observation_delay_ms`. Pairing is enforced by the
  translator, not the model.

## Fixtures

Wire captures are **frozen** — never regenerate or trim. Each source documents
what its fixtures prove (including deliberately absent captures) in a README:
`packages/capture/tests/fixtures/otlp/claude_code/README.md` and
`packages/capture/tests/fixtures/otlp/copilot/README.md` show the pattern.

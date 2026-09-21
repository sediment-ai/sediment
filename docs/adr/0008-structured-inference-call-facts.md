# ADR 0008 — Inference calls preserve structured messages

Status: accepted

## Context

Sediment needs one fact shape that preserves a model call without flattening
message parts. A role-and-text prompt plus one response string loses message-part
order, tool responses, and the distinction between a capture gateway and a model
service. It also makes persisted model output double as derived scoring material.

The [OpenTelemetry Generative AI semantic conventions](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
define input and output messages as ordered role-and-parts structures. Their part
vocabulary distinguishes text, reasoning, tool calls, and tool-call responses.
Reasoning carries readable `content`. Tool calls carry `id`, `name`, and
`arguments`. These names also align with the
[Model Context Protocol tool contract](https://modelcontextprotocol.io/specification/2025-11-25/server/tools),
which represents tool arguments as a structured object.

## Decision

`InferenceCall` is the only model-call fact. Its canonical contract uses
`schema_version = 1`. `input_messages` and `output_messages` preserve ordered
messages and ordered typed parts. The normalized part types are `text`,
`reasoning`, `tool_call`, and `tool_call_response`. `ReasoningPart` stores
readable content only. Provider signatures, encrypted reasoning, and
`redacted_thinking` remain in Basic-redacted `raw`. Structured arguments and
results remain structured.

The LiteLLM translator preserves readable `thinking` and `reasoning` blocks in
source order. For output message sibling fields, readable structured
`thinking_blocks` take precedence over flattened `reasoning_content`. Opaque
reasoning skips and counts separately from unknown unsupported content.

`gateway_provider` names the capture gateway. Nullable `model_provider` names the
model service when the gateway reports it. The remaining observability names are
`input_tokens`, `output_tokens`, `duration_ms`, and `observed_at`. Unknown user,
model, model provider, usage, and duration values stay null instead of using
sentinels.

`model_call_id` identifies the model call and supplies its optional dedup key.
Tool-call parts carry their own ids. Decision attachment searches both namespaces
under the unique-or-drop rule, but the fact contract doesn't overload one field
with both meanings.

Attribution uses a pure `render_scoring_text` view. The renderer joins output text
with string leaves from tool-call arguments in message-part order. It excludes
reasoning because a plan can restate code before the model writes it. It also
excludes tool-call responses. The renderer never persists its result.

PostgreSQL stores inference calls in `inference_calls`. Derived bundles retain
referenced Facts in `inference_calls.jsonl`. Readable reasoning is part of the
version-1 Fact shape. Existing rows remain readable; adding a supported field
does not reconstruct observations that a capture source did not record.

Basic redaction covers every content-bearing inference-call part and `raw` at the
storage seam. Credential matches propagate across parts before storage. Facts
that evade fixed patterns can be quarantined without mutation.

## Consequences

- Capture, derivation, attributed-completion assembly, derived bundles, and
  DPO/SFT/RLVR projections use one fact contract.
- DPO and SFT project ordered typed message parts into conversational trainer
  rows. Readable reasoning maps to assistant `thinking`, never visible
  `content`. Attribution scoring and rollout scalar completion fields exclude
  reasoning through `render_scoring_text`.
- Input reasoning participates in DPO prompt identity and rollout prefix
  comparison. Different reasoning contexts don't bucket or stitch together.
- A message-part addition requires an
  explicit schema-version and renderer decision. Translators skip and count
  unsupported parts instead of flattening them into an ambiguous string.
- A single canonical contract keeps capture, storage, and projection tests aligned.

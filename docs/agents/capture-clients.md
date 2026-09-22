# What a harness client sends

Implement sections 1–3 only where the harness supplies evidence. Sections 4–5
cover gateway calls. The pi shim (`shims/pi/`) is the reference client. Its independent `SEDIMENT_RETRIEVAL_ENDPOINT` / `SEDIMENT_RETRIEVAL_TOKEN` opt-in registers `sediment_retrieve_context` under [ADR 0022](../adr/0022-agent-requested-session-context.md); reads never substitute for capture.

Contributor doc. It is not published to the docs site.

ADR 0007 requires client-side transcript parsing. The client sends narrow,
purpose-built Facts. It never sends prompts, conversations, file reads, tool
results, environment variables, or raw transcripts. If the client can't know a
value, it omits the value so the server can log and count the skip.

## 1. Decisions — `sediment.tool_decision`

An edit tool writes a file. Its name differs by harness. The shim must exclude
all other tools. For each edit-tool call, send an OTLP/JSON logs batch to
`<endpoint>/v1/logs` with one log record:

- `body`: `{"stringValue": "sediment.tool_decision"}`
- `timeUnixNano`: required source event time. Native Cursor has no execution
  timestamp; its adapter records hook receipt time instead.
- record attributes:

| key | type | required | meaning |
|---|---|---|---|
| `agent` | string | yes | the harness's registered `AgentHarness` enum value (for example, `pi`). The server skips unregistered values with a log trail |
| `session.id` | string | yes | the harness Session id (record or resource scope) |
| `tool_use_id` | string | yes | the per-call id; joins the decision to inference calls and Edit observations |
| `decision` | string | yes | `accept` or `reject` |
| `explicit` | bool | yes | a real `boolValue`: `true` = a human explicitly approved or rejected the edit; `false` = the decision was automatic or inferred. A harness with no approval gesture always emits `false` |
| `tool_name` | string | no | the harness's tool name (audit provenance) |
| `file_path` | string | no | the edited path; `""` is the reject convention (a rejected edit was never observed on disk) |

- resource attributes: `user.id` when `OTEL_RESOURCE_ATTRIBUTES`
  carries it.

One record carries everything; the shim observes the edit directly.
Shim decisions remain covered by the natural-key index.
Native Cursor's implicit successful `Write` records use a deterministic Fact id;
the PostgreSQL primary key also preserves the first receipt across changing receipt times.
Other tools and verdicts retain existing behavior. Historical random-ID Facts remain;
a first later receipt can coexist once after upgrade. The exact tuple and encoding
are normative in `packages/capture/sediment_capture/otlp.py`.

## 2. Attribution — `sediment mark`

After every edit-tool call, including failed calls, run:

```text
sediment mark --tool <agent>
```

Send JSON on standard input with `session_id` (or `thread_id`) and `cwd` inside the edited repository.
For a workspace-root Session, locate the repository from an absolute edit path; otherwise use Session `cwd`.

A shim without `sediment` on PATH runs `python3 <dir>/sediment_attribution.py mark --tool <agent>`; fleet hooks use the MDM `--prefix` and pi uses `SEDIMENT_SCRIPT_DIR`.

All marker writers use short generation transactions; local notes writers share a mutex in the common Git directory.
[Concurrent marker capture](../explanation/attribution.md#concurrent-marker-capture) defines locks, cleanup, and interruption limits.
`_CAPTURE_FAILURE_REASONS` in `cli/sediment_cli/attribution.py` owns closed content-free diagnostics; doctor treats their historical log entries as informational.

## 3. Edit observations — `sediment.edit_observation` (opt-in)

At Session end, send the applied text and later observed file text per edit as
`sediment.edit_observation` log records. Extraction runs client-side (ADR
0007). The edit retention score stays a re-derivable server-side policy.
See [Edit retention and external deltas](../explanation/how-capture-works.md#edit-retention-and-external-deltas)
for the pipeline and ceilings, and [Opt in to transcript capture](../capture/local-capture.md#opt-in-to-transcript-capture)
for installation.

- `body`: `{"stringValue": "sediment.edit_observation"}`
- record attributes: `session.id`, `tool_use_id`, `file_path`,
  `tool_name`, `applied_text`, `observed_file_text`, and mandatory `agent`
  (the same semantics as decisions)
- `observed_file_text` is the file's content at Session end (`""` when the
  file is gone). Each text side caps at 256 KiB before shipping. The server
  re-enforces the cap at the trust boundary
- `external_lines_added` / `external_lines_removed` are optional
  `intValue` counts: lines changed by something other than the
  agent between this edit and the next observation of the file. Absent
  means no window covered the call — a different claim from `0`, which
  means nothing else touched it. Counts only, never text. A record whose
  counts are junk still stores its text pair, with the counts absent

The counts need a baseline the transcript can't supply, so a second
hook produces them. [External edit windows](../explanation/how-capture-works.md#external-edit-windows)
covers the measurement and its ceilings. A shim emitting this wire shape
directly may omit them. They are optional precisely so a harness without the
snapshot hook still sends a valid record.

The sanctioned client, `cli/sediment_cli/transcript.py`, runs as `sediment transcript --agent <name>`;
`scripts/sediment_transcript.py` is the checkout shim. Harness parsers register in `_PARSERS`;
the client pairs, caps, and POSTs records. JSONL framing splits on LF, preserving Unicode separators in content.
A non-Python harness may emit the wire shape directly and owns its privacy contract.
Pi resolves relative paths from header `cwd` and excludes proven inherited fork messages; [Pi source boundaries](../capture/agent-integrations.md#pi) define bounded parent reads and counted declines.
Codex Add reads `content`; Update reads complete unified-diff hunks. Shell Update patches permit the first hunk without `@@`, including move and end-of-file markers. Shell patches use explicit `workdir` (Session cwd only when absent) and anchored result headers before stdout.
Codex 0.153.4 `item_completed` / `FileChange` requires matching Session identity,
an event timestamp, native `item.id`, and `status=completed`; stdout isn't success evidence.
Codex declines `execution_directory_invalid`, `execution_directory_unknown`, `observation_unreadable`, `unsupported_patch_kind`, `malformed_patch`, `execution_status_unknown`, `execution_status_conflict`, or `execution_failed` once per affected observation.

A missing Session-end event loses its Edit observations; Attribution can still link commits.
Repeated events collapse first-write-wins on `(org, agent_harness, session, call_id)`.

### Refused edits — `sediment.rejected_edit`

The Session-end POST carries one record per Edit/Write call the developer refused.
A tool failure emits no refusal: it carries no developer preference signal.

- `body`: `{"stringValue": "sediment.rejected_edit"}`
- record attributes: `session.id`, `tool_use_id`, `file_path`, `tool_name`,
  `agent` (mandatory — no `claude-code` default, this record postdates the
  contract), and `proposed` — the denied `new_string`/Write content, capped
  at 256 KiB and re-capped server-side
- no `observed_file_text`: a refused edit never reached the file, so there is
  no file state to observe, and the shim sends none

The client must distinguish developer refusal from tool failure in the transcript.
The server trusts the event name. Pi can't distinguish these outcomes and emits
no refused-edit records.

`call_id` joins the record to the rejecting `DeveloperDecision` (which
carries `file_path=""` and no content). Dedup is first-write-wins on
`(org, agent_harness, session, call_id)`, like Edit observations.

### Correction retries — `sediment.retry_linkage`

At Session end, emit this body after a human refusal, developer correction, and a
successful retry of the same tool on the same file. Every transcript entry must carry the same explicit, nonempty `sessionId`.
The record contains Session, agent, tool, exact path, both call IDs, and later event time, but no text or label.
Failures, implicit or agent-only rewrites, regenerations, and calls that cross files or Sessions emit nothing.
PostgreSQL deduplicates the structural tuple.

## 4. Inference calls — gateway request headers

For harnesses without request metadata, including pi, LiteLLM preserves `x-*`
headers in the logging payload. `session_identity.py` maps these headers:

- `x-sediment-session` — the harness's real Session id. The pi shim's native
  `before_provider_headers` hook stamps it per request. The server
  requires a UUID shape so an invalid header can't create a fake
  Session (ADR 0002, Session aggregate root).
- `x-sediment-agent` — the static agent name from the fleet's
  `models.json` (`docs` → `user_id` `agent:docs`). The server uses this
  value only when every richer identity source leaves `user_id` blank.

Keep headers consistent across `shims/pi/lib/provider.ts::SESSION_HEADER`,
`packages/capture/sediment_capture/session_identity.py`, and the fleet's
`models.json` renderer. Missing identity produces a logged skip, never a placeholder Session.

## 5. Inference calls — gateway envelope

The gateway wire shape is a bearer-authenticated `POST /ingest/gateway`
with `{"provider": "litellm", "session_id"?, "user_id"?, "capture"?, "payload":
<SLO>}`. Envelope ids are optional: present ids win; absent ids resolve
from the payload via `session_identity.py::resolve_identity` (litellm
provider only — the heuristics are LiteLLM-SLO-specific). A
present-but-empty id is a 422 (ADR 0002). When no source yields a
Session id, the server answers 200
`{"skipped": true, "reason": "no_session"}` and logs the call id. No
placeholder Session ever reaches the store.

`capture` contains both `id` (UUID) and `observed_at` (aware datetime). The callback
prepares them once; the adapter derives an organization-scoped Fact ID and preserves
the observation instant. Legacy envelopes retain receiver-time behavior. Database
uniqueness owns dedup; duplicate receipts name the retained Fact. Contradictory
primary/natural identities return content-free 409 `inference_call_identity_conflict`.
The callback preserves `litellm_call_id`; the server uses the response ID only as
its existing fallback. Neither source present means `model_call_id` stays absent.

Privacy: The server receives a payload before it can skip a missing Session identifier.
The server rejects non-string identity carriers and tries lower-priority sources.
On the SLO-absent fallback path, the original `end_user` value and
request metadata persist in `InferenceCall.raw`.

**Upgrade order: server before callback.** A server that predates `capture` rejects
the envelope with 422. Buffered requests remain blocked until an explicit retry.
The callback and transcript client share `cli/sediment_cli/delivery.py`; pi invokes
`enqueue --fallback-direct`. Copy the implementation as `sediment_delivery.py` beside
standalone clients. See [Sender replay operations](../capture/local-capture.md#preserve-prepared-payloads-through-outages)
for limits, private storage, worker enrollment, and acknowledgment meanings.

## Environment contract

| variable | meaning |
|---|---|
| `SEDIMENT_OTLP_ENDPOINT` | explicit ingest origin or `/v1/logs` URL, generated from authenticated login. Remote hosts require HTTPS; HTTP accepts only literal `localhost`, IPv4 loopback, or `[::1]`. Credentials, query, fragment, and redirects are rejected. Unset disables endpoint-dependent capture; no generic `OTEL_EXPORTER_OTLP_ENDPOINT` fallback. pi content also requires its separate opt-in. |
| `SEDIMENT_INGEST_TOKEN` | bearer token; falls back to an `Authorization: Bearer` entry in `OTEL_EXPORTER_OTLP_HEADERS`, only ever sent to the explicit `SEDIMENT_OTLP_ENDPOINT` |
| `SEDIMENT_PI_TRANSCRIPTS` | only `1` permits pi transcript extraction. `install --transcripts` writes it; ordinary reinstall preserves a generated opt-in. `--no-env` requires manual setup. Decisions and Attribution remain independent. |
| `SEDIMENT_DELIVERY_DIR` | explicit opt-in to private prepared-payload storage; doesn't enable capture. Unset or whitespace-only selects best-effort delivery. Storage faults permit one visible direct fallback; capacity/identity declines don't. Pi and workstation transcript clients require a supervised replay worker. |
| `SEDIMENT_EXTRACT_ON_SETTLE` | a nonempty value adds pi `agent_settled` extraction when content is opted in. Use only for one-Session-per-task hosts; reload never extracts. |
| `OTEL_RESOURCE_ATTRIBUTES` | the client forwards `user.id` as resource identity |
| `SEDIMENT_PROVIDER_ID` / `SEDIMENT_PROVIDER_API` | provider and API whose native header hook receives the live Session identifier (defaults `sediment` / `anthropic-messages`; pi 0.84.1 or later) |

## Register a harness

Create a harness extension under `shims/<name>/`, or use a packaged child-process
adapter like Cursor. Implement each of sections 1 through 3 that the harness
can support with evidence, then add the harness to `AgentHarness` in
`packages/core/sediment_core/models.py`. Document unsupported evidence instead
of fabricating its fields. Cursor implements sections 1 and 2 only.

Until registration lands, the server skips the agent's records and logs each
skip instead of storing records under a placeholder. Tests before registration
expect skips, not errors. A Python extractor needs a parser in `_PARSERS` in
`cli/sediment_cli/transcript.py`. The decision payload must match
`packages/capture/tests/fixtures/otlp/sediment/tool_decision.json`.

## Out of contract

- Vendor-native GenAI *spans*: they carry no edit text pairs and can't fire
  the stamper.
- Raw transcript upload: rejected while no consumer exists that a narrow
  extraction can't serve (ADR 0007).
- Per-agent event names such as `pi.tool_decision`. Identity rides the `agent`
  attribute, and a shim must not impersonate a vendor's event name.

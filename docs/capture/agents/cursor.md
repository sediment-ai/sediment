# Capture Cursor work

Use this guide to capture commit Attribution for local Cursor desktop Agent
and Tab work. Successful Agent `Write` calls can also emit implicit-accept
Developer decision Facts.

## Evidence captured

| Evidence | Support | Source or limit |
| --- | --- | --- |
| Commit Attribution | Yes | Native user hooks mark supported Agent and Tab events. |
| Developer decisions | Partial | A successful Agent `Write` emits one implicit accept. |
| Inference calls | No | Sediment hasn't verified an identifier that links Cursor hooks to model requests. |
| Edit observations | No | Cursor exposes no supported Session-end extractor. |
| Rejected edits and Retry linkages | No | Failed writes aren't rejection evidence. |
| External line counts | No | Cursor provides no hook that snapshots a file before an edit. |

Cursor native events map to evidence as follows:

| Cursor event | Commit Attribution | Developer decision |
| --- | --- | --- |
| Successful Agent `Write` | Session marker | Implicit accept |
| Failed Agent `Write` | Session marker | None |
| Accepted Tab edit | Session marker | None |

The adapter keeps the Attribution and decision paths separate. The decision
path sends an OpenTelemetry Protocol (OTLP) HTTP/JSON log.

```text
conversation_id ─► Session marker ─► commit git note ─┐
                                                     ├─► Push evidence ─► Attribution
forge push ─────────────────────────► Push Fact ──────┘

tool_use_id + conversation_id ─► OTLP HTTP/JSON sediment.tool_decision
                              ─► Developer decision Fact (explicit=false)
```

The adapter uses `conversation_id` as the Session identifier in both paths.
Sediment hasn't verified that this identifier also appears in a Cursor gateway
request, Cursor OpenTelemetry log, or AI Code Tracking record.

## Before you begin

You need the Sediment CLI, an endpoint and ingest-only token, a git repository, and
the local Cursor desktop app installed under `~/.cursor`.

If you need an endpoint, complete the [Quickstart](../../quickstart.md). For
endpoint rules, credential storage, installer behavior, and repository git
hooks, read [Configure local capture](../local-capture.md).

## Install capture

1. Connect the CLI to the Sediment endpoint:

   ```bash
   sediment login https://sediment-api.example.com --capture
   ```

2. Install capture in the repository:

   ```bash
   sediment install --user-id alice /path/to/repo
   ```

   Replace `alice` with your developer identifier. The installer preserves
   other commands in `~/.cursor/hooks.json` and adds native user hooks for:

   - `postToolUse`, matched to successful Agent `Write` calls
   - `postToolUseFailure`, matched to failed Agent `Write` calls
   - `afterTabFileEdit`, for accepted Tab edits

   If `~/.cursor` doesn't exist, the installer skips the Cursor hooks. Install
   or start Cursor, then run `sediment install` again.

3. Restart the local Cursor desktop app after installation.

## Configure Developer decisions

The installer writes the dedicated endpoint and token from `sediment login --capture`.
Load them before starting Cursor:

```bash
. "$HOME/.sediment/env.sh"
```

If another system owns the environment and you used `--no-env`, supply:

```bash
export SEDIMENT_OTLP_ENDPOINT=https://sediment-api.example.com
export SEDIMENT_INGEST_TOKEN='<ingest-only token>'
```

The Cursor adapter doesn't infer its endpoint from
`OTEL_EXPORTER_OTLP_ENDPOINT`. Without `SEDIMENT_OTLP_ENDPOINT`, it still marks
commit Attribution but emits no Developer decision.

For each successful Agent `Write`, the adapter sends
`sediment.tool_decision` with `decision=accept` and `explicit=false`. The
translator stores the resulting Developer decision with `accepted=true`. The
value `explicit=false` records the automatic result of the tool call. It
doesn't record a human approval gesture.

A failed Agent `Write` isn't evidence of rejection, so it emits no Developer
decision. An accepted Tab edit also emits no decision because Cursor supplies
no per-call identifier for that event.

Repeated receipts of the same native successful `Write` collapse on a
deterministic Fact identity in PostgreSQL. Sediment preserves the first receipt's
timestamp and metadata. Historical random-ID Facts remain unchanged, so an
event captured before the upgrade can coexist once with its later receipt.
Cursor supplies no execution timestamp; the recorded time is hook receipt time.

## Configure inference-call capture

Cursor inference-call capture isn't supported. Sediment hasn't verified an
identifier that links local Cursor hooks to model requests. Don't treat Cursor
gateway requests as belonging to the same Session as hook events.

Commit Attribution and successful-write Developer decisions remain available
without inference-call capture.

## Configure Edit observations

Cursor Edit observations aren't supported. The adapter doesn't receive the
applied edit text and Session-end file state that the canonical Fact requires.

Commit Attribution still records successful and failed Agent writes plus
accepted Tab edits.

## Verify capture

Remote Fact and Session checks require a separate
[operator login](../local-capture.md#verify-capture). Capture enrollment alone
cannot authorize these reads.

1. Check the user hooks and repository configuration:

   ```bash
   sediment doctor --fetch /path/to/repo
   ```

2. Use local Cursor desktop Agent or Tab to edit a file.

3. Commit the change.

4. Inspect the Session note from inside the repository:

   ```bash
   git notes --ref=refs/notes/sediment show HEAD
   ```

5. After a successful Agent `Write` and telemetry delivery, copy the Session
   identifier from the note entry with `tool: "cursor"` and verify it:

   ```bash
   sediment doctor /path/to/repo --agent cursor --session-id '<Session identifier>'
   ```

The command requires a Cursor Developer decision in that Session and the exact
Session/tool entry in the local `HEAD` note. Failed writes and Tab edits add
only the marker, so they don't satisfy this decision check. Cursor rejects
`--transcripts` and `--inference-calls` requirements as unsupported.

## Limits and troubleshooting

- The integration covers local Cursor desktop Agent and Tab work through
  `~/.cursor/hooks.json` user hooks.
- It doesn't configure Cursor Cloud Agents, Cursor CLI, Enterprise
  cloud-distributed hooks, or the Sediment fleet bundle.
- It doesn't capture inference calls or Edit observations.
- The adapter omits prompts, model output, edit text, file content, tool
  output, transcript paths, and Cursor account email.
- If `conversation_id` is missing, the adapter emits no Session marker or
  Developer decision. If `tool_use_id` is missing from a successful write, the
  adapter can still mark the Session but emits no Developer decision.
- The adapter resolves an Agent `Write` from `tool_input.path` and a Tab edit
  from `file_path`. An absolute path identifies the file's repository, including
  a nested repository. Without an absolute path, the adapter uses the payload's
  `cwd` or its `workspace_roots`. It never uses the hook process's working
  directory as a fallback.
- If multiple workspace roots identify different repositories, the adapter
  skips the Session marker and logs `ambiguous repository directory`. An
  absolute edit path or an explicit `cwd` must identify the repository. A
  same-named file in one root doesn't resolve the ambiguity.
- If a path is malformed, its parent directory is unavailable, or Git can't
  identify a worktree, the adapter skips the Session marker and logs a bounded
  diagnostic. A successful Agent `Write` can still emit its Developer decision.
- Hook errors and delivery failures don't block Cursor. The adapter writes a
  size-limited diagnostic to standard error and skips the affected evidence.
- If `doctor --fetch` reports a notes-ref failure, follow
  [Repair a notes ref](../local-capture.md#repair-a-notes-ref).

[Developer decisions by agent harness](../../explanation/how-capture-works.md#developer-decisions-by-agent-harness)
defines the canonical Cursor decision unit and missing-signal behavior.

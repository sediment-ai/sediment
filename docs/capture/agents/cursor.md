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

The adapter uses `conversation_id` as the Session identifier for markers and
Developer decisions. Sediment hasn't verified a matching identifier in model
requests.

## Before you begin

You need the Sediment CLI, an endpoint and ingest-only token, a git repository, and
the local Cursor desktop app with its `cursor` command-line launcher. Start and
close Cursor once so `~/.cursor` exists.

For deployment and shared setup, see [Configure local capture](../local-capture.md).

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

3. Open the enrolled repository in Cursor:

   ```bash
   cursor /path/to/repo
   ```

   Managed Cursor hooks read `~/.sediment/env.sh` for each successful Agent
   `Write`. You don't need to source the file or restart Cursor to load capture
   settings. After upgrading Sediment, rerun `sediment install` to update the
   hook commands.

## Configure Developer decisions

The generated environment supplies the endpoint and token from
`sediment login --capture`. Managed hooks pass its absolute path through
`--env-file` and read its exports as literal data. The file owns the capture
endpoint, token, and developer identity together; inherited Cursor settings
don't override them. Sediment doesn't execute shell commands from this file.

If another system owns the environment and you used `--no-env`, supply:

```bash
export SEDIMENT_OTLP_ENDPOINT=https://sediment-api.example.com
export SEDIMENT_INGEST_TOKEN='<ingest-only token>'
```

With `--no-env`, hooks inherit Cursor's process environment and ignore the
generated file. Fully quit Cursor, then launch it from the configured shell.
Rerun `sediment install --no-env /path/to/repo` to switch an existing managed
installation to this mode.

The Cursor adapter doesn't infer its endpoint from
`OTEL_EXPORTER_OTLP_ENDPOINT`. Without `SEDIMENT_OTLP_ENDPOINT`, it still marks
commit Attribution but emits no Developer decision.

Successful Agent writes send an OpenTelemetry Protocol (OTLP) log with
`decision=accept` and `explicit=false`. This records tool success, not human
approval. Failed writes and Tab edits don't produce Developer decisions.

Repeated receipts of the same native successful `Write` collapse on a
deterministic Fact identity in PostgreSQL. Sediment preserves the first receipt's
timestamp and metadata. Historical random-ID Facts remain unchanged, so an
event captured before the upgrade can coexist once with its later receipt.
Cursor supplies no execution timestamp; the recorded time is hook receipt time.

## Configure inference-call capture

Cursor inference-call capture isn't supported. Sediment hasn't verified an
identifier that links local Cursor hooks to model requests. Don't treat Cursor
gateway requests as belonging to the same Session as hook events.

## Configure Edit observations

Cursor Edit observations aren't supported. The adapter doesn't receive the
applied edit text and Session-end file state that the canonical Fact requires.

## Verify capture

Remote checks require a separate [operator login](../local-capture.md#verify-capture).

1. Check the user hooks and repository configuration:

   ```bash
   sediment doctor --fetch /path/to/repo
   ```

2. Ask Cursor Agent to create a harmless file with its `Write` tool. Tab edits
   don't produce the Developer decision that this check requires.

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
- If the managed environment file is missing or malformed, a successful Agent
  `Write` still creates its Session marker but skips the Developer decision.
  Rerun `sediment install` to regenerate the file. `doctor` reports a failed
  configuration check when this file is unreadable, invalid, or lacks the
  endpoint or token. A passing configuration check doesn't prove delivery;
  verify the actual Session after another Agent `Write`.
- If `doctor --fetch` reports a notes-ref failure, follow
  [Repair a notes ref](../local-capture.md#repair-a-notes-ref).

[Developer decisions by agent harness](../../explanation/how-capture-works.md#developer-decisions-by-agent-harness)
defines the canonical Cursor decision unit and missing-signal behavior.

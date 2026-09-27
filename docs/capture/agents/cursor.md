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

Each Cursor event produces this evidence:

| Cursor event | Commit Attribution | Developer decision |
| --- | --- | --- |
| Successful Agent `Write` | Session marker | Implicit accept |
| Failed Agent `Write` | Session marker | None |
| Accepted Tab edit | Session marker | None |

The adapter uses Cursor's `conversation_id` as the Session identifier.

## Before you begin

Install the CLI and get a capture token with
[Configure local capture](../local-capture.md). You also need the Cursor
desktop app and its `cursor` launcher. Start and close Cursor once so that
`~/.cursor` exists.

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

3. Open the enrolled repository in Cursor:

   ```bash
   cursor /path/to/repo
   ```

   The hooks read `~/.sediment/env.sh` directly, so you don't need to load it or
   restart Cursor. After you upgrade Sediment, rerun the install command with the
same flags.

## Configure Developer decisions

Each successful Agent `Write` sends a Developer decision with
`decision=accept` and `explicit=false`. It records that the tool succeeded, not
that a person approved it. The recorded time is when the hook received the
event, because Cursor supplies no execution time.

The hooks read the endpoint, token, and developer identifier from the generated
`env.sh`, as data; they never run it as a script. If another system owns the
environment, install with `--no-env`, and set `SEDIMENT_OTLP_ENDPOINT` and
`SEDIMENT_INGEST_TOKEN` in Cursor's environment. Then fully quit Cursor, and
launch it from that shell. Without `SEDIMENT_OTLP_ENDPOINT`, the hooks still
mark commits but send no decisions.

## Inference calls and Edit observations

Cursor doesn't support either. Sediment hasn't verified an identifier that
links Cursor's hooks to its model requests, so don't treat Cursor's gateway
requests as part of the same Session. The hooks don't receive the applied text
or the file's state at Session end that an Edit observation needs.

## Verify capture

Remote checks need a separate [operator login](../local-capture.md#verify-capture).

1. Check the configuration:

   ```bash
   sediment doctor --fetch /path/to/repo
   ```

2. Ask Cursor Agent, not Tab, to create a small file with its `Write` tool, and
   commit the file.
3. Read the commit's Session note:

   ```bash
   git notes --ref=refs/notes/sediment show HEAD
   ```

4. Copy the `session_id` from the entry with `tool: "cursor"`, and check that
   Session on the server:

   ```bash
   sediment doctor /path/to/repo --agent cursor --session-id '<session_id>'
   ```

   The check needs a Cursor Developer decision in that Session and its entry in
   the note.

## Limits and troubleshooting

- The integration covers the local Cursor desktop app only. It doesn't
  configure Cursor Cloud Agents, Cursor CLI, Enterprise cloud-distributed
  hooks, or the Sediment fleet bundle.
- The hooks don't send prompts, model output, edit text, file content, tool
  output, or your Cursor account email.
- Without `conversation_id`, the hooks record nothing. Without `tool_use_id`,
  a successful write marks the Session but sends no decision.
- The hooks find the repository from the edit's absolute path, or else from
  the event's `cwd` or workspace roots. If several workspace roots are
  different repositories, the hooks skip the marker and log
  `ambiguous repository directory`.
- Hook and delivery failures never block Cursor. The hooks write a short
  diagnostic to standard error and skip that evidence.
- If `env.sh` is missing or malformed, a successful write still marks the
  Session but sends no decision, and `doctor` reports `FAIL`. Rerun
  `sediment install` to regenerate it.
- If `doctor --fetch` reports a notes-ref failure, follow
  [Repair a notes ref](../local-capture.md#repair-a-notes-ref).

[Developer decisions by agent harness](../../explanation/how-capture-works.md#developer-decisions-by-agent-harness)
defines the canonical Cursor decision unit and missing-signal behavior.

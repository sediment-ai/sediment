# Capture Claude Code work

Use this guide to capture Claude Code Developer decisions, commit Attribution,
inference calls, and optional Edit observations on one developer machine.

## Evidence captured

| Evidence | Support | Source or limit |
| --- | --- | --- |
| Commit Attribution | Yes | `PostToolUse` marks Claude Code Sessions for edits and shell work. |
| Developer decisions | Yes | Native telemetry covers `Edit`, `Write`, `MultiEdit`, and `NotebookEdit`. |
| Inference calls | Optional | The Claude Code command-line interface (CLI) can use a compatible gateway. |
| Edit observations | Optional | Transcript extraction covers successful `Edit` and `Write` calls. |
| Rejected edits and Retry linkages | Optional | Transcript extraction covers explicit refusals and later corrections. |
| External line counts | Optional | Complete `PreToolUse` snapshots show which lines changed outside Claude Code. |

[Developer decisions by agent harness](../../explanation/how-capture-works.md#developer-decisions-by-agent-harness)
defines which Claude Code decisions are explicit and which missing signals
produce pathless or no Facts.

## Before you begin

You need the Sediment CLI, an endpoint and ingest-only token, a git repository, and
Claude Code installed under `~/.claude`.

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

   Replace `alice` with your developer identifier. The installer adds a
   `PostToolUse` entry to `~/.claude/settings.json`, installs the repository git
   hooks, and writes the telemetry environment from the saved login.

   If `~/.claude` doesn't exist, the installer skips the Claude Code hook.
   Install or start Claude Code, then run `sediment install` again.

3. Load the environment, then restart Claude Code:

   ```bash
   . "$HOME/.sediment/env.sh"
   ```

## Configure Developer decisions

The environment written by `sediment install` enables Claude Code telemetry,
OpenTelemetry Protocol (OTLP) HTTP/JSON logs, tool details, and authenticated
delivery to the Sediment endpoint. Passing `--user-id` also stamps `user.id`.

If mobile device management (MDM) or an agent service owns the environment,
distribute the same values from
[Distribute decision telemetry](../managed-capture.md#distribute-decision-telemetry)
instead of relying on the installer-written shell files.

Claude Code emits accepted and rejected Developer decisions. A user approval
or refusal has `explicit=true`. A configuration or hook approval has
`explicit=false`. Missing tool details can preserve a decision with an empty
file path.

## Configure inference-call capture

If your deployment exposes a compatible large language model (LLM) gateway,
rerun the installer with its URL and client key:

```bash
sediment install \
  --gateway-url https://sediment-llm.example.com \
  --gateway-key "$SEDIMENT_GATEWAY_KEY" \
  --user-id alice \
  /path/to/repo
```

The installer writes `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and
`SEDIMENT_GATEWAY_KEY`. The upstream provider key stays on the gateway host.
[Configure inference-call capture](../managed-capture.md#configure-inference-call-capture)
sets up the server-side callback.

Claude Code prefers a saved subscription credential over
`ANTHROPIC_AUTH_TOKEN`. If this machine must use the gateway, run
`claude logout` once and restart Claude Code from the configured shell.

The Claude desktop app pins its bundled CLI to `api.anthropic.com`. Desktop
Sessions can export Developer decisions through OTLP, but they can't use this
gateway route.

## Configure Edit observations

Transcript capture sends applied edit text and Session-end file content. It is
a different privacy class from commit Attribution, so the installer leaves it
off unless you opt in.

1. Persist the transcript endpoint and token in the environment that starts
   Claude Code:

   ```bash
   export SEDIMENT_OTLP_ENDPOINT=https://sediment-api.example.com
   export SEDIMENT_INGEST_TOKEN='<ingest-only token>'
   ```

2. Install the `SessionEnd` extractor and `PreToolUse` snapshot hook:

   ```bash
   sediment install --transcripts --no-env /path/to/repo
   ```

   The extractor emits Edit observations for successful `Edit` and `Write`
   calls. It can also emit Rejected edits, Retry linkages, and external line
   counts.

3. Restart Claude Code after you install the hooks.

[Opt in to transcript capture](../local-capture.md#opt-in-to-transcript-capture)
defines the shared endpoint safety rules and payload limits.

## Verify capture

Remote checks require a separate [operator login](../local-capture.md#verify-capture).

1. Check the agent and repository configuration:

   ```bash
   sediment doctor --fetch /path/to/repo
   ```

2. Ask Claude Code to edit a file.

3. Commit the change.

4. Inspect the Session note from inside the repository:

   ```bash
   git notes --ref=refs/notes/sediment show HEAD
   ```

5. After telemetry flushes, inspect the deployment:

   ```bash
   sediment facts
   ```

`developer_decisions` grows after supported edit-tool decisions. If you opted
in to transcript capture, `edit_observations`, `rejected_edits`, and
`retry_linkages` can grow after the Session ends.

## Limits and troubleshooting

- A Session that crashes or misses `SessionEnd` can lack Edit observations
  without losing Developer decisions or commit Attribution.
- Transcript capture omits raw conversations, prompts, Read results, tool
  results, and the environment. Inference-call capture includes model inputs
  and outputs.
- Transcript survival covers `Edit` and `Write`. `NotebookEdit` and legacy
  `MultiEdit` fall back to commit Attribution.
- External line counts require a complete snapshot and observation chain for
  the file.
- If a saved Session missed extraction, follow
  [Recover transcript pairs](../local-capture.md#recover-transcript-pairs).
- If `doctor --fetch` reports a notes-ref failure, follow
  [Repair a notes ref](../local-capture.md#repair-a-notes-ref).

[Privacy boundaries and ceilings](../../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
lists the fields that each capture path sends.

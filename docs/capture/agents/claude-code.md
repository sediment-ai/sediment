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

Install the CLI and get a capture token with
[Configure local capture](../local-capture.md). Start Claude Code once so that
`~/.claude` exists.

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

3. End active Claude Code Sessions, then load the environment and start Claude
   Code in the repository:

   ```bash
   . "$HOME/.sediment/env.sh"
   cd /path/to/repo
   claude
   ```

## Configure Developer decisions

The environment that `sediment install` writes turns on Claude Code's
OpenTelemetry Protocol (OTLP) logs and sends them to Sediment. If mobile device
management (MDM) or a service owns the environment, set the variables in
[Distribute decision telemetry](../managed-capture.md#distribute-decision-telemetry)
there instead.

A user's approval or refusal has `explicit=true`. An approval from
configuration or a hook has `explicit=false`.

## Configure inference-call capture

If your deployment runs a model gateway, rerun the installer with its URL and
your client key:

```bash
sediment install \
  --gateway-url https://sediment-llm.example.com \
  --gateway-key "$SEDIMENT_GATEWAY_KEY" \
  --user-id alice \
  /path/to/repo
. "$HOME/.sediment/env.sh"
```

[Configure inference-call capture](../managed-capture.md#configure-inference-call-capture)
sets up the gateway side.

Claude Code prefers a saved subscription sign-in over `ANTHROPIC_AUTH_TOKEN`.
To use the gateway, run `claude logout` once, and restart Claude Code from the
configured shell.

The Claude desktop app pins its bundled CLI to `api.anthropic.com`. Desktop
Sessions can export Developer decisions through OTLP, but they can't use this
gateway route.

## Configure Edit observations

Transcript capture sends applied edit text and the file's content at Session
end, so it's off until you opt in. Rerun the installer with `--transcripts` and
your other flags, and restart Claude Code:

```bash
sediment install --user-id alice --transcripts /path/to/repo
. "$HOME/.sediment/env.sh"
```

The installer adds a `SessionEnd` extractor and a `PreToolUse` snapshot hook.
The extractor emits Edit observations for successful `Edit` and `Write` calls,
plus Rejected edits, Retry linkages, and external line counts.
[Opt in to transcript capture](../local-capture.md#opt-in-to-transcript-capture)
covers the `--no-env` case.

## Verify capture

Remote checks need a separate [operator login](../local-capture.md#verify-capture).

1. Check the configuration, and note the current counts:

   ```bash
   sediment doctor --fetch /path/to/repo
   sediment facts
   ```

2. Ask Claude Code to edit a file, and commit the change.
3. Read the commit's Session note:

   ```bash
   git notes --ref=refs/notes/sediment show HEAD
   ```

4. After telemetry flushes, run `sediment facts` again. `developer_decisions`
   grows. With transcript capture, `edit_observations` can also grow after the
   Session ends.

`sediment doctor --agent` doesn't support Claude Code, so compare counts
instead.

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

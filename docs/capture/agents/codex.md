# Capture Codex work

Use this guide to capture Codex Developer decisions, commit Attribution,
inference calls, and optional Edit observations on one developer machine.

## Evidence captured

| Evidence | Support | Source or limit |
| --- | --- | --- |
| Commit Attribution | Yes | Codex hooks mark the Session after tool use. |
| Developer decisions | Yes | Native telemetry covers `apply_patch` and supported `exec_command` calls. |
| Inference calls | Optional | A Codex provider can route the Responses API through a compatible gateway. |
| Edit observations | Optional | The Session-end parser supports successful single-file patches. |
| Rejected edits and Retry linkages | No | Codex transcripts don't contain enough evidence to create these Facts. |
| External line counts | No | Codex has no pre-edit snapshot hook. |

[Developer decisions by agent harness](../../explanation/how-capture-works.md#developer-decisions-by-agent-harness)
defines Codex verdicts, explicitness, per-file expansion, and missing-signal
limits.

## Before you begin

Install the CLI and get a capture token with
[Configure local capture](../local-capture.md). This guide covers Codex CLI
0.153.4; Codex Desktop isn't verified. `<Codex home>` means `CODEX_HOME`, or
`~/.codex` when it's unset.

## Install capture

1. Connect the CLI to the Sediment endpoint:

   ```bash
   sediment login https://sediment-api.example.com --capture
   ```

2. Install capture in the repository:

   ```bash
   sediment install --user-id alice --codex-profile sediment /path/to/repo
   ```

   Replace `alice` with your developer identifier. The installer adds hooks to
   `<Codex home>/hooks.json`, and creates a `sediment` Codex profile that adds
   telemetry to your existing model and settings.

3. Load the environment, then start Codex with the telemetry profile:

   ```bash
   . "$HOME/.sediment/env.sh"
   codex --profile sediment -C /path/to/repo
   ```

4. Run `/hooks`, and trust the Sediment hooks. Trust them again whenever an
   installation changes them.

## Configure Developer decisions

The installer writes an `[otel]` table to `<Codex home>/sediment.config.toml`,
with mode `0600`. Codex sends header values literally, so the file holds your
capture token itself. Keep it private. The table sets `log_user_prompt=false`,
and the installer keeps the profile's model, provider, and other settings.

Decision telemetry can include patch text and other tool arguments, even
without transcript capture. `log_user_prompt=false` doesn't remove them. Agree
on this with participants before you enable the profile.

After you rotate the capture token, rerun the install command with the same
flags to refresh the profile.

If the profile already has an `[otel]` table that Sediment didn't write, choose
another profile name, or remove that table. Generic `OTEL_EXPORTER_OTLP_*`
variables don't configure Codex.

Sediment records decisions for native `apply_patch` calls and for
`exec_command` calls that start with an `apply_patch` heredoc. It ignores other
shell commands.

## Configure inference-call capture

If your deployment runs a gateway with a Responses API route for your Codex
model, configure Codex to use it. Set up the model on the gateway first.

1. Add a provider to `<Codex home>/config.toml`:

   ```toml
   [model_providers.sediment]
   name = "Sediment gateway"
   base_url = "https://sediment-llm.example.com/v1"
   env_key = "SEDIMENT_GATEWAY_KEY"
   wire_api = "responses"
   ```

2. In `<Codex home>/gateway.config.toml`, select the provider and disable the
   unsupported image tool. Preserve other settings in that profile:

   ```toml
   model_provider = "sediment"

   [features]
   image_generation = false
   ```

3. Add native telemetry to the gateway profile:

   ```bash
   sediment install --codex-profile gateway --no-env /path/to/repo
   ```

4. Start Codex with the gateway key:

   ```bash
   SEDIMENT_GATEWAY_KEY='<gateway client key>' \
   codex --profile gateway -C /path/to/repo
   ```

Codex carries its Session identifier in `x-codex-turn-metadata`. This route
doesn't carry a user identifier.
[Configure inference-call capture](../managed-capture.md#configure-inference-call-capture)
sets up the gateway side.

## Configure Edit observations

Transcript capture sends applied patch text and the file's content at Session
end, so it's off until you opt in. Rerun the installer with `--transcripts` and
your other flags, and restart Codex:

```bash
sediment install --user-id alice --codex-profile sediment --transcripts /path/to/repo
. "$HOME/.sediment/env.sh"
```

The `SessionEnd` extractor needs a completed single-file patch. A multi-file
patch still produces Developer decisions, but no Edit observation.
[Opt in to transcript capture](../local-capture.md#opt-in-to-transcript-capture)
covers the `--no-env` case.

## Verify capture

Remote checks need a separate [operator login](../local-capture.md#verify-capture).

1. Check the configuration:

   ```bash
   sediment doctor --fetch /path/to/repo
   ```

2. Ask Codex to apply a single-file patch, end the Session, and commit the
   change.
3. Read the commit's Session note:

   ```bash
   git notes --ref=refs/notes/sediment show HEAD
   ```

4. After telemetry flushes, check that Session on the server:

   ```bash
   sediment doctor /path/to/repo --agent codex --session-id '<session_id>'
   ```

   The check needs a Codex Developer decision in that Session and its entry in
   the note. With transcript capture, add `--transcripts`. For a
   gateway-routed Session, add `--inference-calls`.

## Limits and troubleshooting

- An interactive rejection can emit no Developer decision.
- A decision without a file path, such as a rejection or a result in a later
  batch, produces one Developer decision with an empty path. A shell-tool
  decision without its result produces none.
- Multi-file patches have no Edit observations. Codex doesn't support Rejected
  edits, Retry linkages, or external line counts.
- A patch event without a matching Session, tool-call identifier, timestamp, or
  confirmed result produces no Edit observation. The extractor logs why.
- Transcript capture omits conversations, prompts, shell commands, and shell
  output. Inference-call capture includes model inputs and outputs.
- To remove the telemetry profile, see
  [Uninstall capture](../local-capture.md#uninstall-capture).
- If `doctor --fetch` reports a notes-ref failure, follow
  [Repair a notes ref](../local-capture.md#repair-a-notes-ref).

[Privacy boundaries and ceilings](../../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
lists the fields that each capture path sends.

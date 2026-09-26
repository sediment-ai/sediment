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

You need the Sediment CLI, an endpoint and ingest-only token, a git repository, and
Codex CLI installed. The profile procedure covers Codex 0.153.4; Codex Desktop
selecting that profile isn't verified. In this guide, `<Codex home>` means the
directory named by `CODEX_HOME`, or `~/.codex` when `CODEX_HOME` is unset.

For deployment and shared setup, see [Configure local capture](../local-capture.md).

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
   `<Codex home>/hooks.json`. It also installs the repository git hooks and
   writes the shared telemetry environment. The `sediment` Codex profile keeps
   your model and other settings and adds native telemetry.

3. Load the environment, then start Codex with the telemetry profile:

   ```bash
   . "$HOME/.sediment/env.sh"
   codex --profile sediment -C /path/to/repo
   ```

4. Run `/hooks`.

5. Review and trust the Sediment hooks. If an installation changes their
   definitions, review and trust them again.

## Configure Developer decisions

The installer writes a managed `[otel]` table to
`<Codex home>/sediment.config.toml`. It resolves the endpoint and ingest-only token
from `sediment login --capture` and restricts the file to mode `0600`. It sets
`log_user_prompt=false` to disable native user-prompt logging. It preserves
the profile's model, provider, comments, and unrelated settings.

Native decision telemetry can include patch text or other tool arguments in a
Developer decision's `raw` payload. This content can arrive without a transcript
hook. `log_user_prompt=false` doesn't remove it, and `--transcripts` controls
separate Edit observations rather than all code-text capture. Agree this payload
with participants before enabling the profile.

If you rotate the capture token, refresh the profile before starting another
Session:

```bash
sediment install --user-id alice --codex-profile sediment /path/to/repo
```

Repeat any gateway arguments used for the generated environment. If you need
only a profile refresh, add `--no-env`; update other agents' token environments
through their existing owner.

If the profile already has an unmanaged `[otel]` table, use another profile name
or remove that table yourself. The installer refuses malformed or conflicting
content. Profile names start with a letter or digit and contain at most 64
letters, digits, underscores, or hyphens.

Codex requires the `[otel]` table. Generic `OTEL_EXPORTER_OTLP_*` variables
alone don't configure its exporter. Codex 0.153.4 sends header values literally;
`${SEDIMENT_INGEST_TOKEN}` in TOML doesn't resolve the token. The generated
profile contains the resolved secret, so keep that file private.

Sediment recognizes native `apply_patch` decisions and `exec_command`
decisions whose command starts with an `apply_patch` heredoc. It excludes
approvals for other shell commands. An interactive rejection can emit no
decision event.

## Configure inference-call capture

If the deployment exposes a Responses API gateway that serves your selected
Codex model, do the following. Configure that model on the gateway first; the
bundled Claude-only example doesn't serve native OpenAI model requests.

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

The gateway profile disables `image_generation` because the bundled LiteLLM
bridge rejects that tool. Codex carries Session identity in
`x-codex-turn-metadata`; this route doesn't carry a user identifier.

[Configure inference-call capture](../managed-capture.md#configure-inference-call-capture)
sets up the server-side callback and gateway.

## Configure Edit observations

Transcript capture sends applied patch text and Session-end file content. It
is a different privacy class from commit Attribution, so the installer leaves
it off unless you opt in.

1. Persist the transcript endpoint and token in the environment that starts
   Codex:

   ```bash
   export SEDIMENT_OTLP_ENDPOINT=https://sediment-api.example.com
   export SEDIMENT_INGEST_TOKEN='<ingest-only token>'
   ```

2. Install the Codex `SessionEnd` extractor:

   ```bash
   sediment install --transcripts --no-env /path/to/repo
   ```

   The extractor requires a completed single-file patch with Session and tool-call
   identity. It supports native patch events and supported `apply_patch` shell
   calls. Multi-file patches can produce Developer decisions but no Edit
   observation. An unreadable file yields no observation; a confirmed missing
   file yields an empty observed value.

3. Restart Codex after you install the hook.

[Opt in to transcript capture](../local-capture.md#opt-in-to-transcript-capture)
defines the shared endpoint safety rules and payload limits.

## Verify capture

Remote checks require a separate [operator login](../local-capture.md#verify-capture).

1. Check the agent and repository configuration:

   ```bash
   sediment doctor --fetch /path/to/repo
   ```

2. Ask Codex to apply a patch.

3. Commit the change.

4. Inspect the Session note from inside the repository:

   ```bash
   git notes --ref=refs/notes/sediment show HEAD
   ```

5. After telemetry flushes, verify the Session. Use the Session identifier from
   the note:

   ```bash
   sediment doctor /path/to/repo --agent codex --session-id '<Session identifier>'
   ```

The command requires a Codex Developer decision in that Session and its Codex
Session entry in the local `HEAD` note. If you opted in to transcript capture,
end the Session and add `--transcripts` to require an Edit observation. If you
configured a gateway, add `--inference-calls` to require an Inference call in
that Session. Missing, omitted, or unreadable evidence fails verification.
Without these verification flags, `doctor` checks configuration and transport
readiness; it doesn't prove that a Session delivered evidence.

## Limits and troubleshooting

- Interactive rejections can emit no Developer decision.
- A pathless native patch, rejection, missing result, or cross-batch result
  can produce one pathless Developer decision Fact.
- A shell-tool decision without its in-batch result produces no Fact because
  the decision alone doesn't prove an edit.
- Single-file shell Update patches retain the first hunk with or without `@@`,
  including move and end-of-file markers.
- Multi-file patches have no Edit observations. Rejected edits, Retry
  linkages, and external line counts aren't supported.
- A native `FileChange` with absent or mismatched Session identity, an absent
  tool-call identifier or timestamp, or an unproved result produces no Edit
  observation. The extractor logs the reason and continues with other edits.
- Transcript capture omits raw conversations, prompts, shell commands, shell
  output, and the environment. Inference-call capture includes model inputs
  and outputs.
- `sediment uninstall --agents` removes hooks, shared environment files, and
  managed telemetry blocks from every `*.config.toml` in the active Codex home.
  It preserves unrelated settings and deletes profiles that become empty.
  If it reports a skipped profile, inspect the file and remove only the managed
  block before restarting Codex. That profile can retain its credential and
  continue sending telemetry until you resolve it.
- If `doctor --fetch` reports a notes-ref failure, follow
  [Repair a notes ref](../local-capture.md#repair-a-notes-ref).

[Privacy boundaries and ceilings](../../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
lists the fields that each capture path sends.

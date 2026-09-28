# Roll out managed capture

Use this page to connect a Sediment deployment to GitHub, a model gateway, and
a managed developer fleet. Every team needs the GitHub steps. The gateway and
fleet steps are optional. For one developer machine, use
[Configure local capture](local-capture.md).

Start with a deployment from [Deploy Sediment on EC2](../operate/deploy-ec2.md)
or [Deploy Sediment on your own host](../operate/deploy.md). Enable only the
paths that participants approve:

| Path | Captures | You need |
| --- | --- | --- |
| [GitHub webhooks](#configure-push-and-ci-capture) | Pushes, pull request revisions and merges, repository renames, and CI outcomes | Repository administrator access |
| [Repository mirrors](#configure-repository-mirrors) | Commit Attribution, with the developers' git hooks | Read-only credentials for private repositories |
| [Gateway callback](#configure-inference-call-capture) | Inference calls | A gateway, its provider credentials, a capture token, and a way to distribute client routing |
| [Fleet distribution](#distribute-decision-telemetry) | Developer decisions and commit Attribution on many machines | Mobile device management (MDM), and Python 3.12 on each macOS or Linux machine |
| [Context pruning](#prune-superseded-tool-output) | Nothing: it removes stale tool output from model requests | The gateway callback or the pruning proxy |

## Configure push and CI capture

On each captured GitHub repository, create four webhooks. Set each content type
to `application/json` and each secret to `SEDIMENT_GITHUB_WEBHOOK_SECRET` from
the server's `~/.sediment/server/server.env`:

| Payload URL | Event |
|---|---|
| `https://sediment-api.example.com/ingest/github/push` | Pushes |
| `https://sediment-api.example.com/ingest/github/ci` | Workflow runs |
| `https://sediment-api.example.com/ingest/github/pull-request` | Pull requests |
| `https://sediment-api.example.com/ingest/github/repository` | Repository changes |

In each webhook's **Recent Deliveries**, the setup ping returns `200` with a
skipped reason. A `401` means a missing or wrong secret. A redelivery returns
`"stored": false`, which is a success: the server already has that Fact. The
[API reference](../reference/api.md) lists the supported events.

GitHub doesn't retry failed deliveries. After an outage or a DNS change, check
**Recent Deliveries**, and
[redeliver the failed events](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks).

For another CI system, send normalized results to
[`POST /ingest/ci`](../reference/api.md#post-ingestci) with a capture token.
Only `passed` and `failed` count as verdicts. Sediment trusts the sender's
provider identity.

## Configure repository mirrors

Commit Attribution reads the git notes from a mirror that the server fetches
after each Push. For a private repository, give the server account read-only
Git credentials, through its credential manager or a `~/.netrc` file with mode
`0600`:

```text
machine github.com login x-access-token password <fine-grained PAT>
```

Scope the personal access token (PAT) to **Contents: read-only** on the
captured repositories.

A Push Fact alone doesn't prove that the mirror fetch worked. After a test
push, the API log shows `session_commit_observations_captured` with a nonzero
count for that repository, and the
[forge check](../operate/run-pilot.md#verify-forge-delivery) lists the Session.
If the log shows `repository_mirror_identity_unresolved`, inspect the stored
repository identities before you redeliver the Push.

## Configure inference-call capture

A gateway sends a copy of each successful model call to
[`POST /ingest/gateway`](../reference/api.md#post-ingestgateway). Sediment
doesn't serve or change model requests unless you enable
[context pruning](#prune-superseded-tool-output). The published package
doesn't include a gateway.

Sediment reads the LiteLLM callback payload. To add the callback to your own
LiteLLM proxy, follow
[Connect an existing LiteLLM gateway](../operate/rehearse-compose.md#connect-an-existing-litellm-gateway).
Another gateway needs an integration that sends the same envelope.

For each gateway:

1. Give it a capture token and the HTTPS API URL. Keep provider keys on the
   gateway.
2. Keep each developer's chosen model. Configure capture failures so they don't
   fail the model call.
3. Make sure the client carries the real Session identifier. The server skips
   calls without one and logs `gateway_ingest_skipped_no_session`.
4. Route each agent through the gateway, as described in
   [Distribute gateway routing](#distribute-gateway-routing).

A successful model response doesn't prove capture. Check a real Session, as
described in [Verify the rollout](#verify-the-rollout).
[Gateway request and capture paths](../explanation/how-capture-works.md#gateway-request-and-capture-paths)
explains how the two paths fail independently.

## Distribute gateway routing

A gateway records only the model calls that an agent sends to it. Each agent
needs the gateway URL and a client credential in the environment that starts
it. Distribute both through configuration that you manage, such as MDM or an
agent service. `sediment install` doesn't configure gateway routing, and
developers don't request a gateway credential.

Give each machine a client credential that the gateway accepts and that can't
administer the gateway. The bundled LiteLLM gateway accepts only
`LITELLM_MASTER_KEY`, its administrative key, and can't issue per-developer
keys. Keep that key on machines that you control.

Routed calls use the gateway's provider key. While a gateway credential is
active, Claude Code doesn't use the developer's claude.ai subscription.

### Claude Code

Merge the gateway route into the Claude Code managed settings file, and keep
its other keys. The file is
`/Library/Application Support/ClaudeCode/managed-settings.json` on macOS and
`/etc/claude-code/managed-settings.json` on Linux:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://sediment-llm.example.com"
  },
  "apiKeyHelper": "<credential helper command>"
}
```

`apiKeyHelper` names a command that prints the client credential. Claude Code
sends its output in the `Authorization` and `x-api-key` headers, and reruns the
command after five minutes by default. The Claude desktop app reads gateway
routing from its own configuration, not from this file.

### Codex

Codex needs a gateway with a Responses API route for the developer's model. The
bundled gateway serves only `claude-*` models. Distribute these files:

1. A provider in `<Codex home>/config.toml`:

   ```toml
   [model_providers.sediment]
   name = "Sediment gateway"
   base_url = "https://sediment-llm.example.com/v1"
   env_key = "SEDIMENT_GATEWAY_KEY"
   wire_api = "responses"
   ```

2. A `gateway` profile in `<Codex home>/gateway.config.toml` that selects the
   provider and disables the image tool, which the LiteLLM bridge rejects:

   ```toml
   model_provider = "sediment"

   [features]
   image_generation = false
   ```

3. `SEDIMENT_GATEWAY_KEY` in the environment that starts Codex.

[Capture Codex work](agents/codex.md#configure-inference-call-capture) adds
telemetry to the profile.

### pi

Merge this provider into `~/.pi/agent/models.json`, and keep the existing
providers. Replace the URL and model ID, and copy the model's capabilities,
context, and output limits from its existing definition:

```json
{
  "providers": {
    "sediment": {
      "baseUrl": "https://sediment-llm.example.com",
      "api": "anthropic-messages",
      "apiKey": "$SEDIMENT_GATEWAY_KEY",
      "models": [{"id": "<existing Anthropic model ID>"}]
    }
  }
}
```

Set `SEDIMENT_GATEWAY_KEY` in the environment that starts pi. Keep
`$SEDIMENT_GATEWAY_KEY` literally in the file; pi resolves it from the
environment. If you register a different provider name or API, also set
`SEDIMENT_PROVIDER_ID` and `SEDIMENT_PROVIDER_API` to match it.

## Prune superseded tool output

A long agent Session resends its whole conversation on every model call, and
much of that input is tool output that later tool calls have made stale.
Sediment can replace that output with one stub line before each call, through
erode, an MIT-licensed package in `contrib/erode`. The rule is deterministic
and off by default, as [ADR 0027](../adr/0027-opt-in-request-transforms.md)
requires.

A tool result is superseded when a later tool call in the same request makes it
out of date:

| Earlier result | Superseded by |
| --- | --- |
| Read of a file | A later read of the whole file or of the same range, or a later edit or write of the file |
| Run of a command | A later run of the identical command |

Sediment never changes the following parts of a request:

- System, user, and assistant text.
- Tool calls and their arguments.
- Results in the last two turns.
- Results under 512 bytes, and error results.
- Output of any tool other than pi's `read`, `edit`, `write`, and `bash`,
  Claude Code's `Read`, `Edit`, `MultiEdit`, `Write`, and `Bash`, or the Codex
  CLI `exec` calls that the erode README describes.
- Any field outside tool results, including Anthropic `cache_control`
  breakpoints.
- A request that carries Anthropic `context_management`, which hands context
  editing or compaction to the provider.

An agent's own compaction request, such as Claude Code's automatic compaction,
is an ordinary Messages request. Nothing in its wire shape identifies it, so
Sediment prunes it like any other request.

Sediment applies new stubs only when they remove at least 4 KB, so the
provider's cached prefix breaks rarely. A stub names the step that superseded
the result, and the agent can read the file again when it needs the content.

Pruning changes the model's input, so it can change the model's output. Before
you enable it for a team, compare priced tokens and task outcomes with pruning
on and off.

Choose one delivery mode:

| Mode | Use it when |
| --- | --- |
| [Bundled gateway](#enable-pruning-in-the-bundled-gateway) | You run the Compose gateway from this repository |
| [Existing LiteLLM](#enable-pruning-in-an-existing-litellm-gateway) | You run your own LiteLLM proxy with the Sediment callback |
| [Pruning proxy](#run-the-pruning-proxy) | You run another gateway, or your agents call the provider directly |

### Enable pruning in the bundled gateway

1. In `.env`, set `SEDIMENT_CONTEXT_PRUNE=supersede`.
2. Start the gateway again with the commands in
   [Enable bundled LiteLLM](../operate/rehearse-compose.md#enable-bundled-litellm).

Compose mounts the `erode` package from `contrib/erode/src/erode` next to the
capture callback and passes the variable to LiteLLM. To turn pruning off, clear
the value and start the gateway again.

### Enable pruning in an existing LiteLLM gateway

1. From a Sediment checkout, install erode into the proxy's Python environment:

   ```bash
   pip install ./contrib/erode
   ```

   Alternatively, copy the `contrib/erode/src/erode` directory into the
   directory that holds `sediment_callback.py`.
2. In the proxy's environment, set `SEDIMENT_CONTEXT_PRUNE=supersede`.
3. Restart the proxy.

The capture callback that `litellm_settings.callbacks` registers also runs the
pruning hook, so the proxy configuration doesn't change. If the proxy logs
`sediment_context_prune reason=module_unavailable`, the callback can't import
`erode`; install the package or put its directory next to `sediment_callback.py`.

Each captured Inference call keeps the request that the model received. Its
`raw` payload carries a count-only report under `sediment_context`:
`policy_version`, `stubbed_results`, and `bytes_removed`.

### Run the pruning proxy

The pruning proxy is the `erode proxy` command. It prunes
`POST /v1/chat/completions`, `POST /v1/messages`, and `POST /v1/responses`
requests and forwards every request to one upstream URL. It forwards the
agent's headers, including its credentials, unchanged. It stores nothing, adds
no retries, and streams each response as it arrives.

1. On a host with Python 3.12, install erode from a Sediment checkout:

   ```bash
   pip install ./contrib/erode
   ```

2. Start the proxy in front of your gateway or provider:

   ```bash
   erode proxy --upstream https://api.anthropic.com
   ```

   The proxy listens on `127.0.0.1:8787`. To change the address, pass `--host`
   and `--port`.
3. Point each agent at the proxy instead of the upstream. For Claude Code:

   ```bash
   ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude
   ```

   Codex CLI ignores `OPENAI_BASE_URL` and needs an explicit model provider
   and an OpenAI API key; see
   [Use erode with Codex CLI](../../contrib/erode/README.md#use-erode-with-codex-cli).

To chain the proxy in front of a gateway, pass the gateway's URL as
`--upstream`, and route agents to the proxy with the settings in
[Distribute gateway routing](#distribute-gateway-routing). Capture behind the
proxy records the pruned request, which is what the model received. The proxy
logs a count-only report for each chat request.

Running the proxy is the opt-in, so it prunes by default. To forward every
request unchanged as a pass-through baseline, pass `--mode off` or set
`ERODE_MODE=off`. The [erode README](../../contrib/erode/README.md) lists every
setting.

The proxy has no authentication of its own and relays whatever credentials an
agent sends. If you bind it to an address other than loopback, restrict who can
reach that address.

## Distribute decision telemetry

The fleet bundle installs commit Attribution hooks only. Distribute decision
telemetry separately.

For Claude Code, set this environment for each Claude Code process through
shell profiles, MDM, or the agent's service definition:

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=https://sediment-api.example.com
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <capture token>"
export OTEL_LOG_TOOL_DETAILS=1
export OTEL_RESOURCE_ATTRIBUTES='user.id=<developer>'
```

Restart Claude Code after you change its environment.
`OTEL_LOG_TOOL_DETAILS=1` lets Sediment record the file path of each edit.

Enroll the other agents on each machine with their guides:
[Codex](agents/codex.md#configure-developer-decisions),
[Cursor](agents/cursor.md), and [pi](agents/pi.md).

## Build the fleet bundle

Generate the MDM payload:

```bash
sediment install --fleet --out sediment-fleet --prefix /opt/sediment
```

The prefix is the absolute path where MDM installs the bundle. Hooks reference
that path, so they keep working if the original CLI moves.

| File | Fleet destination |
|---|---|
| `sediment_attribution.py` | `<prefix>/sediment_attribution.py` |
| `sediment_transcript.py` | `<prefix>/sediment_transcript.py`; it doesn't enable transcript capture |
| `sediment_delivery.py` | `<prefix>/sediment_delivery.py` |
| git hook template | `<prefix>/git-template/hooks/` |
| `gitconfig` fragment | System git configuration |
| Claude Code managed settings | The platform's `managed-settings.json` |
| Codex hook fragment | Each user's `~/.codex/hooks.json` |

The gitconfig fragment sets `init.templateDir` and `notes.rewriteRef`, so every
later `git clone` and `git init` gets the hooks. Codex has no system-managed
settings file, so MDM merges its fragment into each user's profile.

## Set the owner allowlist

An agent can create a clone that no installer saw. The owner allowlist lets
`sediment mark` install hooks in your organization's clones before their first
commit. Place `config.json` beside the fleet stamper:

```json
{"auto_install_remotes": ["github.com/acme-corp/"]}
```

The matcher normalizes HTTPS and SSH remotes and matches whole path segments.

The allowlist is a security boundary: the pre-push hook sends Session
identifiers to the remote, so never list third-party owners. An allowlisted
repository reinstalls its hooks after a manual uninstall, so remove the owner
from the list first.

## Distribute the bundle

1. Deploy the bundle, the system git configuration, the Claude Code managed
   settings, the Codex fragment, and the optional allowlist through MDM.
2. For existing clones, run `git init` in place to copy the template hooks, or
   run `sediment install` in each one.

To provision one machine without MDM, run
`sudo sediment install --fleet --apply`.
If `sudo` resets your `PATH`, use the CLI's absolute path. The command refuses
to overwrite an unrelated `init.templateDir` or an invalid managed-settings
file.

For an air-gapped fleet, build the bundle on a connected machine, and copy it to
the same prefix on each target. Preserve the hooks' executable modes.

Transcript capture isn't part of the bundle. Each user opts in with
[Opt in to transcript capture](local-capture.md#opt-in-to-transcript-capture).

## Verify the rollout

Check each machine and agent combination, because totals can hide one broken
client.

1. Schedule the health check on each machine through MDM:

   ```bash
   sediment doctor --fetch ~/code/repo-a ~/code/repo-b
   ```

   It exits with a failure when any installed integration is broken.

2. For each gateway and agent combination, run a short test Session. With an
   operator token, query that Session's captured calls:

   ```bash
   curl -sf \
     -H "Authorization: Bearer $SEDIMENT_OPERATOR_TOKEN" \
     "https://sediment-api.example.com/v1/facts/session/<session_id>/inference-calls"
   ```

   The response lists the test call with the expected provider and model.

3. Check the other paths: Developer decisions grow after edits, the remote has
   `refs/notes/sediment` after a push, Pushes and CI outcomes grow after forge
   events, and the API log reports `attributions_derived` after a mirror
   refresh.

| Failure | Action |
| --- | --- |
| No model response | Check gateway routing, authentication, and provider logs. |
| A model response, but no Inference call | Check the callback and API logs. |
| `gateway_ingest_skipped_no_session` | Fix how the client carries its Session identifier. |
| Callback `401` | Fix the capture token. |
| Callback `400` | Check that the gateway's provider has a Sediment adapter. |
| Callback `422` | Read the response and API log for the invalid field. |

## Remove managed capture

Remove managed capture in this order, so that the allowlist or a managed
profile doesn't reinstall something that you already removed:

1. Retire the MDM deployment policy, or switch it to removal mode. Keep the
   fleet prefix until nothing references it.
2. Delete the four GitHub webhooks. For another CI system, remove its
   `POST /ingest/ci` call and its capture token.
3. Remove each gateway's Sediment callback and capture token. From client
   machines, remove the gateway routing that you distributed: the Claude Code
   `ANTHROPIC_BASE_URL` and `apiKeyHelper` settings, the Codex provider and
   profile, the pi provider, and `SEDIMENT_GATEWAY_KEY`. Restart the agents.
4. [Revoke the retired capture tokens](../operate/maintain.md#rotate-credentials).
   Never reuse a retired token for another client.
5. Remove the distributed OpenTelemetry and pi variables from shell profiles,
   MDM, and agent services. Remove only Sediment's telemetry blocks from Codex
   profiles, and restart the agents.
6. Remove `config.json` from the fleet prefix, which turns off automatic
   installation.
7. Remove Sediment's `PostToolUse` entry from the Claude Code managed settings
   and from each user's Codex hooks file. Keep unrelated entries.
8. Check the system git values:

   ```bash
   git config --system --get init.templateDir
   git config --system --get-all notes.rewriteRef
   ```

   If `init.templateDir` points to the Sediment prefix, remove only the
   matching values. Replace `/opt/sediment` if you chose another prefix:

   ```bash
   sudo git config --system --fixed-value --unset-all \
     init.templateDir /opt/sediment/git-template
   sudo git config --system --fixed-value --unset-all \
     notes.rewriteRef refs/notes/sediment
   ```

9. On each machine that used pi or transcript capture, run
   [Uninstall capture](local-capture.md#uninstall-capture) with `--agents`.
10. Run `sediment uninstall /path/to/repo` in every other existing clone.
11. Remove the fleet prefix through MDM. When no captured private repository
    needs them, revoke the server's mirror credentials.

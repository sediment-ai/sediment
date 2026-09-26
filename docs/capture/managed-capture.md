# Roll out managed capture

Use this guide to connect a Sediment deployment to gateways, forges, private
repositories, and a managed developer fleet. For one developer machine, use
[Configure local capture](local-capture.md).

Complete [Deploy Sediment](../operate/deploy.md) before configuring capture.

## Prerequisites

Start with the deployed API. For each enabled path, obtain the following access:

| Path | Prerequisites |
| --- | --- |
| GitHub webhooks | Repository administrator access and the deployment's webhook secret |
| Private mirrors | Read-only repository credentials for the API |
| Gateway | Its configuration, provider credentials, and a named ingest-only token |
| Fleet hooks | Mobile device management (MDM) access and Python 3.12 on each macOS or Linux machine |

Native Windows capture is unsupported. The installer requires POSIX file locks
and shell hooks; it refuses Windows installation before changing configuration.

## Choose capture paths

Enable only the evidence that participants approve:

| Evidence | Setup |
| --- | --- |
| Push, Pull request revision and merge, Repository rename, and CI outcome | [Forge webhooks](#configure-push-and-ci-capture) |
| Commit Attribution | [Repository mirrors](#configure-repository-mirrors) and developer Git hooks |
| Inference call | [Gateway callback](#configure-inference-call-capture) |
| Developer decision | [Agent telemetry](#distribute-decision-telemetry) |
| Edit observation | [Per-user transcript opt-in](local-capture.md#opt-in-to-transcript-capture) |

For a pilot, [enroll each developer](../operate/run-pilot.md) before attempting
fleet distribution. Missing capture paths remain absent.

## Configure repository mirrors

The push webhook stores a Push Fact even when a mirror refresh fails. Notes
Attribution needs the mirror, so private repositories require read-only git
credentials on the API host.

Before mounting private Git credentials, reassess the default image and Git
configuration conditions in [the security procedure](../operate/security.md).
A custom credential mount falls outside the supplied deployment assurance.
Then mount a deployment-local `.netrc` through `docker-compose.override.yml`:

```yaml
services:
  api:
    volumes:
      - ~/.config/sediment/netrc:/home/sediment/.netrc:ro
```

Create the file privately with mode `0600`. Make it readable by the API
container's user without granting access to other host users:

```text
machine github.com login x-access-token password <fine-grained PAT>
```

Scope the personal access token (PAT) to **Contents: read-only** on the captured
repositories. Apply the mount with `docker compose up -d --no-deps api`. After
enrollment, push a test commit with a Session note. In
`docker compose logs --since 10m api`, require
`session_commit_observations_captured` with a nonzero stored or duplicate count
for that repository. Verify the commit with the
[forge check](../operate/run-pilot.md#verify-forge-delivery); a Push Fact alone
doesn't verify private Git access.

Identified mirrors use stable repository IDs and survive renames. Legacy mirrors
remain separate. If you see `repository_mirror_identity_unresolved`, inspect
stored identities before redelivering the Push. Known competing identities block
fetches; Git cannot detect an unobserved remote deletion or name reuse.

## Configure push and CI capture

For GitHub, create four webhooks on each captured repository. Use
`application/json` and `SEDIMENT_GITHUB_WEBHOOK_SECRET` for each secret.

| Payload URL | Selected event |
|---|---|
| `https://sediment-api.example.com/ingest/github/push` | Pushes |
| `https://sediment-api.example.com/ingest/github/ci` | Workflow runs |
| `https://sediment-api.example.com/ingest/github/pull-request` | Pull requests |
| `https://sediment-api.example.com/ingest/github/repository` | Repository changes |

Keep `SEDIMENT_GITHUB_HOST=github.com` for GitHub.com. Inspect recent deliveries
in each webhook's settings: the setup ping returns `200` with a skipped reason;
`401` means a missing or invalid signature. After enrollment, verify
[real forge deliveries](../operate/run-pilot.md#verify-forge-delivery).

Redelivery returns `"stored": false` when database uniqueness finds the same
Fact. It is a successful acknowledgment. The
[API reference](../reference/api.md) defines supported events and repository
identity fields.

If delivery fails after a DNS change, verify the hostname's public A record and
HTTPS certificate. Check **Recent Deliveries** in the repository's webhook
settings for GitHub's result; a request from your machine doesn't verify
GitHub's connection. After connectivity recovers, [redeliver the failed
events](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks).
GitHub doesn't automatically redeliver failed deliveries. Keep certificate and
webhook signature verification enabled during recovery.

For another continuous integration (CI) system, send normalized results to
[`POST /ingest/ci`](../reference/api.md#post-ingestci) with an ingest token.
Follow its required fields and identity rules. Only `passed` and `failed`
supply verdicts; other terminal results remain neutral. The sender asserts
provider identity; Sediment doesn't verify it with the provider.

## Configure inference-call capture

A gateway sends each successful model response to `POST /ingest/gateway`.
Sediment doesn't serve the model request.

### Choose a gateway path

Decision: use [bundled LiteLLM](#enable-the-bundled-litellm-gateway) for the
supplied Anthropic routes, or connect an existing LiteLLM gateway for other
models. Sediment registers only the LiteLLM adapter; an enum value alone doesn't
add support for another payload format.

Use the [gateway envelope and status codes](../reference/api.md#post-ingestgateway).
The callback must retain the complete input, output, model, available usage and
latency, and a real Session identifier. Capture failures mustn't fail successful
model calls. See [Gateway request and capture paths](../explanation/how-capture-works.md#gateway-request-and-capture-paths)
for routing effects on model behavior.

### Connect an existing LiteLLM gateway

Copy `litellm/sediment_callback.py` and
`cli/sediment_cli/delivery.py` next to the gateway configuration. Name the copied
helper `sediment_delivery.py`. Both files must be importable by the proxy. Register
the callback:

```yaml
litellm_settings:
  callbacks: sediment_callback.handler
```

Set these variables in the gateway process environment:

```bash
export SEDIMENT_INGEST_URL=https://sediment-api.example.com
export SEDIMENT_API_BEARER_TOKEN='<ingest-only token>'
```

Register the callback secret in the API's `SEDIMENT_INGEST_TOKENS` map.
The callback uses `SEDIMENT_API_BEARER_TOKEN` for ingest; don't give it an
operator token. After changing the map in `.env`, recreate the API with
`docker compose up -d --no-deps api`. Removing an entry revokes that client.

Use HTTPS for remote callback destinations. Loopback HTTP is accepted for
`localhost`, `127.0.0.0/8`, and `[::1]`. The callback rejects redirects. If the
callback and API share a trusted container network, set
`SEDIMENT_GATEWAY_LOCAL_HTTP_ORIGIN` to that one HTTP origin. The bundled Compose
profile uses `http://api:8000`. This exception matches the scheme, hostname, and
port exactly; it never authorizes OTLP delivery or another destination.

The callback uses five-second HTTP timeouts and preserves capture identity and
observation time across retries. Capture failures don't fail the model request.

If you authorize local storage of the prepared payload, set
`SEDIMENT_DELIVERY_DIR` to a private directory on persistent storage. The payload
can contain unredacted prompts, code, or credentials before server redaction.
The callback starts a replay worker for its process lifetime. Without this
setting, it reports `best_effort` and attempts direct delivery.
Unsafe, unavailable, or busy buffer storage also triggers one direct attempt
with a `best_effort` diagnostic. Repair the volume to restore durable recovery.
See [Preserve prepared payloads through outages](local-capture.md#preserve-prepared-payloads-through-outages)
for limits, permissions, and recovery commands.

If you collect raw fixtures for integration debugging, set `SEDIMENT_CAPTURE_DIR`
to an absolute private directory owned by the gateway user. This opt-in writes
unredacted prompts, responses, code, and possibly credentials. The callback
creates the directory with mode `0700` and both JSON files with mode `0600`.
It refuses permissive paths, foreign ownership, symlinks, and hardlinks.
An unsafe fixture destination logs `fixture_write_failed` without stopping
valid gateway delivery. Restrict access, use encrypted storage, and delete the
fixtures when the investigation ends. Basic redaction at the API doesn't protect
these local raw files.

Clients must carry a real Session identifier through metadata or a supported
protocol carrier. The API skips unresolved calls and logs
`gateway_ingest_skipped_no_session`. Upgrade the server before clients when
identity parsing changes.

### Enable the bundled LiteLLM gateway

The [EC2 pilot setup](../operate/deploy.md#deploy-a-pilot-on-ec2) enables the
gateway with HTTPS. Its base URL is `https://sediment.example.com/llm`, and
clients authenticate with the private `LITELLM_MASTER_KEY`. The Anthropic key
stays on the server.

Follow [Enable bundled LiteLLM](../operate/deploy.md#enable-bundled-litellm).
The supplied configuration routes `claude-*` to Anthropic without model
substitution, fallbacks, caching, guardrails, or prompt rewriting.

Give developers the exposed gateway URL and client key, keeping the upstream
provider key on the gateway. For Claude Code, follow its
[gateway setup](agents/claude-code.md#configure-inference-call-capture).

### Configure Codex for a compatible gateway

Codex uses the OpenAI Responses API and needs a participating client profile.
The gateway must serve the developer's selected model. The bundled Claude-only
configuration doesn't serve native OpenAI model requests.
[Configure inference-call capture for Codex](agents/codex.md#configure-inference-call-capture)
defines the provider, profile, environment, and unsupported tool.

## Distribute decision telemetry

The fleet bundle installs Attribution hooks. It doesn't configure native OTLP
export, install the pi shim, or enable transcript capture.

Distribute this environment to each Claude Code process through your shell
profiles, MDM, or agent service definition:

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=https://sediment-api.example.com
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <ingest-only token>"
export OTEL_LOG_TOOL_DETAILS=1
export OTEL_RESOURCE_ATTRIBUTES='user.id=<developer>'
```

Restart the agent after you change its environment. The exporter appends
`/v1/logs` to the endpoint. `OTEL_LOG_TOOL_DETAILS=1` lets Sediment recover a
file path from native edit-tool events.

For other harnesses, use their local enrollment procedures:

- [Codex telemetry](agents/codex.md#configure-developer-decisions): regenerate
  the private profile after token rotation; its header contains a resolved token.
- [pi setup](agent-integrations.md#pi): install from a source checkout and pass
  `SEDIMENT_OTLP_ENDPOINT` and `SEDIMENT_INGEST_TOKEN` to pi. Set
  `SEDIMENT_PI_TRANSCRIPTS=1` only with edit-content consent.
- [Cursor hooks](agents/cursor.md): enroll each desktop installation.

The fleet bundle includes none of these three integrations.

## Build the fleet bundle

The fleet bundle covers commit Attribution. Configure decision telemetry
separately before you distribute it.

Generate the MDM payload:

```bash
sediment install --fleet --out sediment-fleet --prefix /opt/sediment
```

The prefix is the stable absolute path where MDM installs the bundle. Hook
files reference that path, so moving or deleting a source checkout doesn't
break fleet installations.

The bundle contains:

| File | Fleet destination |
|---|---|
| `sediment_attribution.py` | `<prefix>/sediment_attribution.py` |
| `sediment_transcript.py` | `<prefix>/sediment_transcript.py`; installing the file doesn't enable content capture |
| `sediment_delivery.py` | `<prefix>/sediment_delivery.py`; the standalone shared implementation |
| git hook template | `<prefix>/git-template/hooks/` |
| `gitconfig` fragment | system git configuration |
| Claude Code managed settings | platform `managed-settings.json` |
| Codex hook fragment | each user's `~/.codex/hooks.json` |

Set `init.templateDir` and `notes.rewriteRef` from the git configuration
fragment. Every later `git clone` and `git init` starts with the Attribution
hooks.

Claude Code supports a system-managed settings file. Codex has no equivalent,
so MDM must merge its fragment into each user profile.

## Set the owner allowlist

An agent can create a clone that the per-repository installer never saw. The
owner allowlist lets `sediment mark` install hooks in organization-owned clones
before their first commit.

Place `config.json` beside the fleet stamper:

```json
{"auto_install_remotes": ["github.com/acme-corp/"]}
```

The matcher normalizes HTTPS and SSH remote forms and uses segment-aligned
prefixes. It doesn't match an allowlisted name embedded in an unrelated URL.

The allowlist is a security boundary. The pre-push hook sends Session ids to a
repository remote, so automatic installation must never cover third-party
remotes.

An allowlisted repository reinstalls its hooks after a manual uninstall. Remove
the remote from the allowlist before you uninstall it.

## Distribute the bundle

Deploy the bundle, system git configuration, managed Claude Code entry, Codex
fragment, and optional allowlist through MDM.

For existing clones, run `git init` in place to copy missing template hooks, or
run the local installer for each repository.

To provision one machine without MDM, apply the generated bundle directly:

```bash
sudo sediment install --fleet --apply
```

If `sudo` resets a per-user executable path, invoke the absolute CLI path. The
command leaves an unrelated `init.templateDir` or invalid managed-settings file
untouched and exits with a failure.

For an air-gapped rollout, generate the bundle on a connected build machine,
then transfer it to the target machines at the same prefix. Preserve executable
hook modes. Don't distribute `scripts/tests/fixtures/fleet/`; those test files
aren't a generated installation bundle.

Transcript capture isn't part of the fleet bundle. It sends edit text and file
state, so each user opts in through
[Configure local capture](local-capture.md#opt-in-to-transcript-capture).

## Verify the rollout

Verify each machine and agent combination; aggregate counts can hide a broken
client.

Schedule the machine and repository health check through MDM:

```bash
sediment doctor --fetch ~/code/repo-a ~/code/repo-b
```

Pass `--fetch` so the command can classify the remote notes ref. It prints one
finding per check and exits with a failure when any installed integration is
broken.

On the API host, inspect Fact counts and capture logs:

```bash
docker compose --profile operator run --rm operator sediment facts
docker compose logs api | grep gateway_ingest_skipped_no_session
docker compose logs api | grep attributions_derived
```

If you enabled bundled LiteLLM, verify that its container is running before the
test Session:

```bash
docker compose --profile gateway ps gateway
```

Run a short test Session through each gateway and agent combination. A
successful model response proves the request path. Set
`SEDIMENT_TEST_SESSION_ID` to the real Session identifier that the agent
carried, then query that Session's captured calls:

```bash
SEDIMENT_TEST_SESSION_ID='<real test Session id>'
SEDIMENT_TEST_TOKEN='<operator token>'
curl -sf \
  -H "Authorization: Bearer $SEDIMENT_TEST_TOKEN" \
  "https://sediment-api.example.com/v1/facts/session/$SEDIMENT_TEST_SESSION_ID/inference-calls"
```

Require the test call in `inference_calls` with the expected gateway provider
and model. The [Session API reference](../reference/api.md#get-v1factssessionsession_idinference-calls)
defines its reconciliation fields.

If you enabled bundled LiteLLM, inspect its logs after the test Session:

```bash
docker compose logs --since 10m gateway
```

The logs must show the model request without an authentication, routing, or
provider error.

Verify the other enabled paths:

- Developer decisions grow after edit-tool calls when decision telemetry is
  configured
- the remote contains `refs/notes/sediment` after a push
- Pushes and CI outcomes grow after forge events
- `attributions_derived` reports notes Attribution after mirror refresh

| Failure | Action |
| --- | --- |
| No model response | Inspect gateway routing, authentication, and provider logs. |
| Model response but no Inference call | Inspect the callback and API logs. |
| `gateway_ingest_skipped_no_session` | Repair the client's Session identity carrier. |
| Callback `401` | Correct the ingest-only token. |
| Callback `400` | Check whether the gateway provider has a registered adapter. |
| Callback `422` | Inspect the response and API log for an invalid envelope or payload. |

[How capture works](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
describes the data classes and network boundaries for rollout review.

## Remove managed capture

Remove managed capture in this order so an owner allowlist or managed profile
doesn't reinstall an integration that you already removed:

1. Retire the MDM deployment policy or change it to removal mode. Keep the
   fleet prefix in place until no configuration references it.
2. Delete the four forge webhooks. For another CI system, remove its call to
   `POST /ingest/ci` and delete the ingest-only token from that client's secret
   store.
3. If you connected a gateway that you operate, remove its Sediment success
   callback and ingest-only token. For LiteLLM, remove
   `sediment_callback.handler`, `SEDIMENT_INGEST_URL`, and
   `SEDIMENT_API_BEARER_TOKEN`.
4. If you enabled the bundled gateway, remove its routing variables and Codex
   profile from client machines. Restart those agents, then remove only the
   gateway container:

   ```bash
   docker compose --profile gateway rm --stop --force gateway
   ```

   Remove the retired client from the API's `SEDIMENT_INGEST_TOKENS` map and
   restart the API. Other named clients retain their credentials. If a retired
   client used the shared legacy `SEDIMENT_API_BEARER_TOKEN`, rotate or remove
   that compatibility secret and update every client that shared it. Never
   reuse a retired secret under another client identifier.
5. Remove the distributed OTLP and pi variables from shell profiles, MDM, and
   agent services, including `SEDIMENT_PI_TRANSCRIPTS`. Remove only the managed
   telemetry blocks from Codex profiles. The user-level uninstall command also
   removes these blocks. Restart the affected agents.
6. Remove `config.json` from the fleet prefix before you uninstall hooks in
   existing clones. This step disables owner-allowlisted auto-installation.
7. Remove the Sediment `PostToolUse` object from the Claude Code managed file
   and from each user's Codex hooks file. Preserve unrelated hook objects.
8. Inspect the system git values before you remove them:

   ```bash
   git config --system --get init.templateDir
   git config --system --get-all notes.rewriteRef
   ```

   If `init.templateDir` points to the deployed Sediment prefix, remove only
   the matching values. Replace `/opt/sediment` if you selected another prefix:

   ```bash
   sudo git config --system --fixed-value --unset-all \
     init.templateDir /opt/sediment/git-template
   sudo git config --system --fixed-value --unset-all \
     notes.rewriteRef refs/notes/sediment
   ```

9. On each machine that used pi or transcript capture, follow the Local capture
   [agent uninstall procedure](local-capture.md#uninstall-capture) once. Run it
   from the checkout that installed pi, or remove a stale pi entry manually.
10. Run `sediment uninstall /path/to/repo` for every other existing clone. The
    command removes copied hook blocks and repository-level notes configuration.
11. Remove the fleet prefix through MDM after the system and repository
    configurations no longer reference it. Revoke mirror credentials on the
    API host when no captured private repository needs them.

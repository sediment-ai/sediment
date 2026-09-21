# Roll out managed capture

Use this guide to connect a Sediment deployment to gateways, forges, private
repositories, and a managed developer fleet. For one developer machine, use
[Configure local capture](local-capture.md).

Before you configure capture, deploy the API behind a reachable endpoint and
migrate its PostgreSQL schema. The API verifies the revision at startup but
never runs migrations.

[Deploy Sediment](../operate/deploy.md) covers the host,
storage, ingress, backup, quarantine, and incident response.

## Prerequisites

The paths that you enable determine what you need:

- a deployed Sediment API and a named ingest-only token for each capture client
- the GitHub webhook secret and repository administrator access
- control of an existing gateway, or provider credentials for bundled LiteLLM
- read-only credentials for private repository mirrors
- mobile device management (MDM) or equivalent access for fleet distribution
- Python 3.12 on each managed macOS or Linux machine

Both fleet and local capture support macOS and Linux. Native Windows capture
is unsupported because the clients require POSIX file locks and shell hooks.
On Windows, `sediment install` refuses installation before changing
configuration. The fleet bundle requires an absolute POSIX install prefix.

## Choose capture paths

Each path records a different Fact. Enable the paths that your dataset needs.

| Signal | Source | Configuration |
|---|---|---|
| Inference call | Gateway success callback | Gateway and API |
| Developer decision | Agent OTLP logs or shim | Developer machines |
| Edit observation | Session-end transcript hook | Per-user opt-in |
| Commit Attribution | Agent markers and git notes | Developer machines and repositories |
| Push, Pull request revision and merge, and CI outcome | Forge webhooks or CI API | Forge and API |

Missing paths stay visibly absent. Sediment doesn't invent inference calls,
Sessions, Edit observations, Pushes, Pull request revisions and merges, or CI outcomes.

These paths do not produce a complete study context manifest or cost ledger.
An inference call preserves model, usage, duration, and provider values when
the source supplies them.

Record task identity, repository instructions, loaded skills, retrieval,
developer time, and external prices in versioned study artifacts. Mark a field
absent when its source does not expose it.

## Configure inference-call capture

Sediment stays out of the LLM request path. A gateway sends each successful
response to `POST /ingest/gateway` after it returns the response to the agent.

### Choose a gateway path

Use one of these paths:

- If you operate a gateway with a registered Sediment adapter, configure its
  success callback to send completed calls to Sediment.
- If you don't operate a gateway, enable the bundled LiteLLM profile.

The ingest envelope is gateway-neutral, but the completed-call payload isn't.
Each gateway format needs a registered adapter that converts it into an
`InferenceCall`. This repository registers only the LiteLLM adapter. If your
gateway emits another format, implement and register its adapter before you
route measured traffic through it. A provider name in the
[`GatewayProvider` schema](../reference/schema.md#gatewayprovider) doesn't mean
that its adapter exists.

A gateway integration must:

- preserve the complete model input, output, model identity, and available
  usage and latency values;
- carry a real Session identifier in the envelope, request metadata, or a
  protocol-specific identity carrier;
- authenticate to `POST /ingest/gateway` with its ingest-only token;
- send the callback after a successful model response; and
- log and drop capture failures instead of failing a successful model call.

The [gateway API reference](../reference/api.md#post-ingestgateway) defines the
envelope and status codes. [Gateway request and capture
paths](../explanation/how-capture-works.md#gateway-request-and-capture-paths)
explains how gateway configuration can affect model behavior.

### Connect an existing LiteLLM gateway

Copy `litellm/sediment_callback.py` and
`cli/sediment_cli/delivery.py` next to the gateway configuration. Name the copied
helper `sediment_delivery.py`. Both files must be importable by the proxy. Register
the callback:

```yaml
litellm_settings:
  callbacks: sediment_callback.handler
```

Set the callback environment:

```bash
SEDIMENT_INGEST_URL=https://sediment-api.example.com
SEDIMENT_API_BEARER_TOKEN=<ingest-only token>
```

Register this callback secret under a named entry in the API's
`SEDIMENT_INGEST_TOKENS` map. The callback retains its existing environment
variable name; it must never receive `SEDIMENT_OPERATOR_TOKEN`. The API's own
`SEDIMENT_API_BEARER_TOKEN` setting is optional ingest-only compatibility.
Removing a named entry and restarting the API revokes that client.

Use HTTPS for remote callback destinations. Loopback HTTP is accepted for
`localhost`, `127.0.0.0/8`, and `[::1]`. The callback rejects redirects. If the
callback and API share a trusted container network, set
`SEDIMENT_GATEWAY_LOCAL_HTTP_ORIGIN` to that one HTTP origin. The bundled Compose
profile uses `http://api:8000`. This exception matches the scheme, hostname, and
port exactly; it never authorizes OTLP delivery or another destination.

Each HTTP attempt has a five-second timeout. The callback records a capture UUID
and observation time once, then preserves them through delivery. It retains
LiteLLM's provider call ID; a capture UUID never substitutes for that ID.
Capture failures don't raise into the model request.

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

Clients must identify a real Session. LiteLLM clients can use request metadata.
Claude Code and Codex use protocol-specific `session_id` carriers that the
server resolves.

The server skips an unresolved inference call and logs
`gateway_ingest_skipped_no_session`.

Upgrade the server before the callback or client fleet when identity parsing
changes. The server owns identity resolution. Non-string identity carriers
contribute no identity; the server logs their invalid shape and tries the next
source. Explicit metadata IDs can be arbitrary nonblank strings.

### Enable the bundled LiteLLM gateway

If the deployment doesn't have a gateway, set `ANTHROPIC_API_KEY` and
`LITELLM_MASTER_KEY` in `.env`, then enable the compose profile:

```bash
docker compose --profile gateway up -d --build
```

The profile uses the pinned LiteLLM image and `litellm/config.yaml`. It
authenticates clients with `LITELLM_MASTER_KEY` and maps each requested
`claude-*` model to the matching Anthropic model. The default configuration
doesn't enable model substitution, fallbacks, caching, guardrails, or prompt
rewriting.

Expose the loopback-bound gateway through the deployment ingress. Give client
machines the public URL and the LiteLLM master key, not the upstream provider
key.

On each Claude Code machine, pass that URL and key to `sediment install` as
[Configure inference-call capture for Claude Code](agents/claude-code.md#configure-inference-call-capture)
describes.

### Configure Codex for a compatible gateway

Codex uses the OpenAI Responses API and needs a participating client profile.
The gateway must serve the developer's selected model. The bundled Claude-only
configuration doesn't serve native OpenAI model requests.
[Configure inference-call capture for Codex](agents/codex.md#configure-inference-call-capture)
defines the provider, profile, environment, and unsupported tool.

## Configure push and CI capture

For GitHub, create four webhooks on each captured repository. Use
`application/json` and `SEDIMENT_GITHUB_WEBHOOK_SECRET` for each secret.

| Payload URL | Selected event |
|---|---|
| `https://sediment-api.example.com/ingest/github/push` | Pushes |
| `https://sediment-api.example.com/ingest/github/ci` | Workflow runs |
| `https://sediment-api.example.com/ingest/github/pull-request` | Pull requests |
| `https://sediment-api.example.com/ingest/github/repository` | Repository changes |

The pull request webhook stores a Pull request revision for `opened` and
`synchronize`, and a Pull request merge for a merged `closed` event. The
repository webhook stores a Repository rename Fact, including when mirrors are
disabled. Historical Facts retain their captured repository names.

Keep `SEDIMENT_GITHUB_HOST=github.com` for GitHub.com. Sediment captures the
provider repository ID from each signed payload. A missing or invalid ID leaves
identity absent and logs the gap; Sediment doesn't reconstruct historical IDs.

GitHub's setup ping returns `200` with a skipped reason. A `401` means that the
HMAC secret doesn't match.

Redeliveries are safe. Database uniqueness collapses a repeated event and the
API returns `"stored": false` as success.

Other CI systems send a normalized result to `POST /ingest/ci` with the bearer
token. The body requires provider, provider run id, repository, commit SHA,
branch, and normalized result. It accepts the run attempt, workflow identity,
exact provider result, structured error evidence, source-event metadata, and run
URL when the sender has them.

The integration declares the normalized provider from its configuration and
maps the provider's pipeline-run identifier to `run_id`. Sediment does not infer
either value from a URL. The sender may supply the complete
`repository_provider`, `repository_host`, and `repository_id` triple. A partial
triple is invalid. Identified run IDs are scoped by deployment organization, CI
provider, and forge provider/host; repository identity must agree within a run.
Legacy run IDs retain the deployment organization and CI provider namespace. The bearer token authenticates the
integration, not the provider assertion; Sediment does not call the provider to
verify these fields. The provider run id plus optional positive attempt is the
deduplication key. The run URL is an optional location. Only `passed` and
`failed` are verdicts.
`error`, `timed_out`, `cancelled`, `skipped`, `neutral`, and `unknown` remain
visible neutral evidence. The
[API reference](../reference/api.md#post-ingestci) defines the request.

## Configure repository mirrors

The push webhook stores a Push Fact even when a mirror refresh fails. Notes
Attribution needs the mirror, so private repositories require read-only git
credentials on the API host.

Before mounting private Git credentials, reassess the default image and Git
configuration conditions in [the security procedure](../operate/security.md).
A custom credential mount falls outside the supplied deployment assurance.
Then mount a deployment-local `.netrc` through `compose.override.yml`:

```yaml
services:
  api:
    volumes:
      - ~/.config/sediment/netrc:/home/sediment/.netrc:ro
```

Create the file with mode `0600`:

```text
machine github.com login x-access-token password <fine-grained PAT>
```

Scope the token to **Contents: read-only** on the captured repositories. After
you restart the API, confirm that `mirror_refresh_failed` no longer appears for
a test push.

Identified mirrors use stable repository IDs. A rename doesn't move their
directories or change historical observation IDs. Legacy mirrors remain separate
and aren't promoted by name or clone URL. If capture logs
`repository_mirror_identity_unresolved`, inspect the stored repository identities
and requested location before redelivering the Push. A known competing identity
blocks the fetch and preserves existing refs. Git doesn't verify provider IDs;
an unobserved remote deletion or name reuse remains a capture limit.

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
export OTEL_RESOURCE_ATTRIBUTES=user.id=<developer>
```

Restart the agent after you change its environment. The exporter appends
`/v1/logs` to the endpoint. `OTEL_LOG_TOOL_DETAILS=1` lets Sediment recover a
file path from native edit-tool events.

For Codex, enroll the generated telemetry profile from
[Configure Developer decisions for Codex](agents/codex.md#configure-developer-decisions)
on each machine. Its private file contains a resolved header token. Refresh the
profile after token rotation; Codex doesn't interpolate a token variable in TOML.

The pi shim uses `SEDIMENT_OTLP_ENDPOINT` and `SEDIMENT_INGEST_TOKEN` instead.
It isn't part of the fleet bundle.

Install pi from a source checkout on each machine as
[Configure local capture](local-capture.md#install-capture)
describes. Distribute those two variables to the pi process.
If participants approve edit-content capture, also distribute
`SEDIMENT_PI_TRANSCRIPTS=1`. Endpoint/token enrollment alone sends no pi edit
content. See the [Pilot gateway procedure](../operate/run-pilot.md#add-approved-gateway-capture)
for a model-preserving pi provider entry.

The fleet bundle doesn't install Cursor user hooks. For the supported local
desktop boundary, follow [Capture Cursor work](agents/cursor.md).

## Build the fleet bundle

The fleet bundle covers commit Attribution. Configure decision telemetry
separately before you distribute it.

Generate the MDM payload:

```bash
sediment install --fleet
sediment install --fleet --out DIR --prefix /opt/sediment
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

For an air-gapped rollout, generate the complete bundle on a build machine with
the prefix that the target machines use:

```bash
SEDIMENT_FLEET_PREFIX=/opt/sediment
sediment install --fleet --out sediment-fleet --prefix "$SEDIMENT_FLEET_PREFIX"
```

Transfer `sediment-fleet/` into the air-gapped environment. Install its files
at the same prefix and destinations listed in [Build the fleet bundle](#build-the-fleet-bundle).
Preserve executable modes on the git hooks.

Don't assemble an air-gapped bundle from `scripts/tests/fixtures/fleet/`.
Those pinned test fixtures reference `/opt/sediment` and can't represent a
different selected prefix.

Transcript capture isn't part of the fleet bundle. It sends edit text and file
state, so each user opts in through
[Configure local capture](local-capture.md#opt-in-to-transcript-capture).

## Verify the rollout

Before a measured run, complete this verification for every machine and agent
harness combination. Aggregate Fact counts can hide a broken client when
another client is healthy.

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
SEDIMENT_TEST_SESSION_ID=<real test Session id>
SEDIMENT_TEST_TOKEN=<operator token>
curl -sf \
  -H "Authorization: Bearer $SEDIMENT_TEST_TOKEN" \
  "https://sediment-api.example.com/v1/facts/session/$SEDIMENT_TEST_SESSION_ID/inference-calls"
```

The `inference_calls` array must contain the test call with the expected
gateway provider and model. This Session-scoped response proves the capture
path without letting concurrent traffic mask a broken client. The
[Session inference-call API](../reference/api.md#get-v1factssessionsession_idinference-calls)
defines the returned reconciliation fields.

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

If verification fails, start with the component that owns the failed path:

- If the agent receives no model response, inspect the gateway and model
  provider. Sediment isn't on that request path.
- If the agent receives a response but the Session query returns no Inference
  call, inspect the gateway callback and Sediment API logs.
- If the API logs `gateway_ingest_skipped_no_session`, repair the client's
  Session identity carrier. Sediment doesn't create a placeholder Session.
- If the callback receives `401`, correct its ingest-only token.
- If the callback receives `400`, the selected gateway provider has no
  registered adapter.
- If the callback receives `422`, inspect the response detail and API log for
  an invalid envelope or a payload that the adapter couldn't normalize.

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

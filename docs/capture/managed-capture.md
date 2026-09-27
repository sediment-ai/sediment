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

Use the server account's Git credential manager or a private `~/.netrc` file.
Give the file mode `0600` and keep other host users from reading it:

```text
machine github.com login x-access-token password <fine-grained PAT>
```

Scope the personal access token (PAT) to **Contents: read-only** on the captured
repositories. After enrollment, push a test commit with a Session note.
In the API's supervisor logs, require
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

For another continuous integration (CI) system, send normalized results to
[`POST /ingest/ci`](../reference/api.md#post-ingestci) with an ingest token.
Follow its required fields and identity rules. Only `passed` and `failed`
supply verdicts; other terminal results remain neutral. The sender asserts
provider identity; Sediment doesn't verify it with the provider.

## Configure inference-call capture

A gateway sends each successful model response to `POST /ingest/gateway`.
Sediment doesn't serve the model request.

### Choose a gateway path

Use an independently operated gateway integration that implements the
[gateway envelope](../reference/api.md#post-ingestgateway). Sediment accepts the
LiteLLM capture payload; another provider name alone doesn't add an adapter.

The published package doesn't include a deployable gateway or its logging
callback. The capture API remains available to a separately configured client.
Don't assume that installing the CLI routes or records model traffic.

### Configure gateway delivery

Give the integration an ingest-only credential and the HTTPS API URL. Keep
provider keys on the gateway. Its payload must carry the complete supported
input and output, available usage and latency, and a real Session identifier.

Keep the developer's model unchanged. Configure capture failure handling so a
failed capture request doesn't fail a successful model call. Verify an Inference
call in the actual Session before relying on gateway reports.

If the integration buffers payloads, agree its storage, retention, retries,
and recovery behavior. A successful model response doesn't establish capture.
See [Gateway request and capture paths](../explanation/how-capture-works.md#gateway-request-and-capture-paths).

### Configure Codex for a compatible gateway

Codex requires a compatible Responses API route for its chosen model.
[Configure inference-call capture for Codex](agents/codex.md#configure-inference-call-capture)
defines its client configuration and capture limits.

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
- [pi](agent-integrations.md#pi): meet the release and runtime requirements,
  then register the packaged extension on each machine.
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
files reference that path, so moving or deleting the original CLI environment doesn't
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

From an enrolled operator terminal, inspect Fact counts:

```bash
sediment facts
```

Inspect the API's supervisor logs for `gateway_ingest_skipped_no_session` and
`attributions_derived`. Before a gateway test Session, verify that your gateway
service is running.

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

Inspect the gateway's logs after the test Session. Require the model request
without an authentication, routing, or provider error.

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
3. If you connected a gateway, remove its Sediment capture integration and
   ingest-only token. Remove its routing variables and Codex profile from
   client machines, then restart the affected agents.
4. Remove the retired client from the API's `SEDIMENT_INGEST_TOKENS` map and
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
   with the CLI installation that registered pi, or remove a stale entry manually.
10. Run `sediment uninstall /path/to/repo` for every other existing clone. The
    command removes copied hook blocks and repository-level notes configuration.
11. Remove the fleet prefix through MDM after the system and repository
    configurations no longer reference it. Revoke mirror credentials on the
    API host when no captured private repository needs them.

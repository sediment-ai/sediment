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
| [Gateway callback](#configure-inference-call-capture) | Inference calls | A gateway, its provider credentials, and a capture token |
| [Fleet distribution](#distribute-decision-telemetry) | Developer decisions and commit Attribution on many machines | Mobile device management (MDM), and Python 3.12 on each macOS or Linux machine |

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
doesn't serve model requests, and the published package doesn't include a
gateway.

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
4. Route each agent through the gateway with its guide:
   [Claude Code](agents/claude-code.md#configure-inference-call-capture),
   [Codex](agents/codex.md#configure-inference-call-capture), or
   [pi](agents/pi.md#configure-inference-call-capture).

A successful model response doesn't prove capture. Check a real Session, as
described in [Verify the rollout](#verify-the-rollout).
[Gateway request and capture paths](../explanation/how-capture-works.md#gateway-request-and-capture-paths)
explains how the two paths fail independently.

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
3. Remove each gateway's Sediment callback and capture token. Remove the
   gateway variables and Codex profiles from client machines, and restart the
   agents.
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

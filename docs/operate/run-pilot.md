# Run a Cursor, pi, and Codex pilot

Enroll macOS or Linux developers in a shared deployment. Complete the operator
handoff, install each participating harness, and verify one Session per harness.

| Owner | Tasks |
| --- | --- |
| Operator | [Prepare the deployment](#prepare-the-deployment), issue capture credentials, and run authenticated evidence checks. |
| Developer | [Install the checkout](#install-a-pinned-checkout), enroll repositories, and create the test Sessions. |

Complete the enabled [deployment validation checks](validate-deployment.md)
before expanding the pilot.

## Agree the capture scope

Record repositories, developers, harness versions, models, the pilot window,
and the questions you want to answer.

Decision: enroll Developer decisions and commit Attribution after agreeing each
harness's payload. Add Edit observations or gateway capture only for the
evidence that the participants approve.

Codex native telemetry can contain patch code even with `log_user_prompt=false`
and transcripts disabled. pi decisions are metadata-only. Transcripts add applied
and observed text; gateways add model inputs and outputs. Cursor supplies neither
Edit observations nor Inference calls. Check the
[integration comparison](../capture/agent-integrations.md#compare-integrations)
and agree the [privacy boundaries](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings).

## Prepare the deployment

1. Complete [Deploy Sediment](deploy.md) at the approved revision, including
   HTTPS ingress, private Git access, webhooks, and a tested backup.
2. [Match the installed build to its release evidence](validate-deployment.md#verify-the-installed-build).
   Keep that evidence with the deployment record; you don't need to rerun the
   release rehearsal to install an approved build.
3. Give each developer these settings through the team's credential channel:

   | Setting | Value |
   | --- | --- |
   | Source | Sediment repository URL and full approved commit hash |
   | Capture | HTTPS API root and that developer's named ingest-only token |
   | Identity | Developer identifier and local pilot repository path |
   | Consent | Approved harnesses, transcript capture, and sender buffering |
   | Gateway, if enabled | URL, client key, supported API, and the developer's existing model ID |

Keep deployment configuration and operator credentials out of harnesses. Keep
upstream provider keys on the gateway. If gateway capture is required, verify
model availability and a funded provider account before handoff. The bundled
gateway serves Anthropic `claude-*` models; other models need a separate gateway.

## Install a pinned checkout

Install Git and uv. Use a POSIX shell for these commands; uv selects Python
3.12. Install only the harnesses that the pilot includes:

| Harness | Prerequisite |
| --- | --- |
| Cursor | Install and authenticate Cursor, including its command-line launcher. |
| Codex CLI | Install and authenticate Codex 0.153.4 for this profile and transcript procedure. |
| pi | Install Node 24. The locked checkout supplies pi 0.86.1 in the pi-only step. |

Start and close Cursor or Codex once before enrollment so its configuration
directory exists. The installer skips undetected harnesses.

Replace the example values with the operator's handoff:

```bash
SEDIMENT_CHECKOUT="$HOME/.local/share/sediment-pilot"
SEDIMENT_REVISION='<approved full commit hash>'
PILOT_REPO='/absolute/path/to/pilot-repo'
PILOT_USER_ID='alice'
PILOT_API_URL='https://sediment-api.example.com'

mkdir -p "$(dirname "$SEDIMENT_CHECKOUT")"
git clone https://github.com/sediment-ai/sediment.git "$SEDIMENT_CHECKOUT" || exit 1
git -C "$SEDIMENT_CHECKOUT" checkout --detach "$SEDIMENT_REVISION" || exit 1
cd "$SEDIMENT_CHECKOUT" || exit 1
test "$(git rev-parse HEAD)" = "$SEDIMENT_REVISION" || exit 1
uv sync --locked --python 3.12 || exit 1
export PATH="$SEDIMENT_CHECKOUT/.venv/bin:$PATH"

git rev-parse HEAD
sediment --version
```

Require the first output to match the approved commit. Keep the checkout and
`.venv` at this path; hooks reference them. In later shells, export the same PATH.
Capture-only machines don't need PostgreSQL client libraries.

If you use pi, install its locked runtime and shim dependencies from this checkout:

```bash
npm ci --prefix "$SEDIMENT_CHECKOUT/shims/pi" --include=dev --no-audit --no-fund || exit 1
export PATH="$SEDIMENT_CHECKOUT/shims/pi/node_modules/.bin:$PATH"
node --version
pi --version
```

Require Node 24 and pi 0.86.1. Start pi once to create `~/.pi/agent`, authenticate
your existing model with `/login` or your credential mechanism, then close pi.
Keep this pi directory on PATH in later shells. If you use only gateway
credentials, complete [Add approved gateway capture](#add-approved-gateway-capture)
before verifying pi.

## Enroll the developer machine

Connect the CLI and install capture in the pilot repository. If you don't
enroll Codex, omit `--codex-profile sediment-pilot` from each install command
in this guide:

```bash
sediment login "$PILOT_API_URL" --capture
sediment install --user-id "$PILOT_USER_ID" \
  --codex-profile sediment-pilot "$PILOT_REPO"
. "$HOME/.sediment/env.sh"
sediment doctor "$PILOT_REPO"
```

`login --capture` prompts for the ingest-only token. Review the installation
summary: the installer adds hooks for detected harnesses, including Claude Code,
and preserves unrelated entries.
Require a valid server token and no `FAIL` rows from `doctor`. If only capture
is enrolled, `doctor` verifies the ingest token. This configuration check doesn't
require operator access or prove that an agent has delivered a Session.

The generated environment enables Cursor and pi delivery. Codex uses a private
`sediment-pilot.config.toml` under `CODEX_HOME`, or `~/.codex`, with a resolved
token. Keep that file private; TOML token placeholders don't resolve.

If participants approve Edit observations, add the opt-in and reload the
environment:

```bash
sediment install --user-id "$PILOT_USER_ID" --transcripts \
  --codex-profile sediment-pilot "$PILOT_REPO"
. "$HOME/.sediment/env.sh"
```

Reinstallation preserves a generated pi transcript opt-in. If you use `--no-env`,
set the endpoint, token, and exact `SEDIMENT_PI_TRANSCRIPTS=1` value yourself.
Leave `SEDIMENT_EXTRACT_ON_SETTLE` unset for interactive pi. See
[transcript setup](../capture/local-capture.md#opt-in-to-transcript-capture).

### Enroll bounded workstation delivery

If participants approve persistent unredacted payload storage, enable buffering
for pi Developer decisions and pi/Codex transcripts. Use private persistent
storage and an encrypted volume when required; the helper doesn't encrypt it.

After the initial install creates the credential environment, record the
operator-managed process supervisor and its start, status, restart, and stop
commands. Configure it to run as the developer, restart after failure and login
or reboot, load `$HOME/.sediment/env.sh`, and set these values:

```bash
export SEDIMENT_CHECKOUT="$HOME/.local/share/sediment-pilot"
export SEDIMENT_DELIVERY_DIR="$HOME/.local/state/sediment/delivery"
/bin/sh -c '. "$HOME/.sediment/env.sh"; exec "$SEDIMENT_CHECKOUT/.venv/bin/sediment" delivery replay --watch'
```

The supervisor must keep one worker running for that directory. In the
enrollment shell, export the same path, repeat the install command so the
generated environment preserves the opt-in, and verify the worker:

```bash
export SEDIMENT_DELIVERY_DIR="$HOME/.local/state/sediment/delivery"
sediment install --user-id "$PILOT_USER_ID" \
  --codex-profile sediment-pilot "$PILOT_REPO"
. "$HOME/.sediment/env.sh"
sediment delivery status
sediment doctor "$PILOT_REPO"
```

Require `pending: 0`, no blocked entries, and `worker_running: true` in the
delivery status. Require `ok` for the doctor delivery check. Restart the worker
through the named supervisor, then require both results again. Setting
`SEDIMENT_DELIVERY_DIR` without the supervised worker fails readiness.

Follow [sender buffer operations](../capture/local-capture.md#preserve-prepared-payloads-through-outages)
for capacity, expiry, and recovery. A stopped host can retain unredacted payloads
past 24 hours. The buffer keeps content-free terminal receipts for seven days.

If the participants don't authorize this storage, leave
`SEDIMENT_DELIVERY_DIR` unset and record workstation delivery as `best_effort`.
The outage and sender-restart recovery gates remain open. The buffer doesn't
cover Cursor hooks, native Codex telemetry, or forge webhooks. Those channels
retain separate acceptance gates.

## Verify an enrolled harness

Create test files on a dedicated branch:

```bash
git -C "$PILOT_REPO" switch -c sediment-pilot-check
```

Run only the checks for enrolled harnesses. An authorized operator must first
run `sediment login "$PILOT_API_URL"` on the machine that runs Session checks.
This read credential stays separate from capture enrollment; the installer
never distributes it to harnesses.

Wait for telemetry to flush, then require `ok` for the Session note and Developer
decision. A failed check names missing or unsupported evidence. Resolve it before
enrolling more repositories; organization-wide Fact counts don't verify a Session.

## Verify Cursor desktop

1. Fully quit Cursor, then launch it from the shell that loaded Sediment's
   environment. An already running desktop process can retain its earlier
   environment:

   ```bash
   cursor "$PILOT_REPO"
   ```
2. In a separate Agent conversation, ask Cursor to create a harmless file
   named `pilot_cursor.py`. Use an Agent write for this check; Tab doesn't
   produce a Developer decision.
3. Review and commit that file:

   ```bash
   git -C "$PILOT_REPO" add -- pilot_cursor.py
   git -C "$PILOT_REPO" commit -m 'test: verify Cursor capture'
   git -C "$PILOT_REPO" notes --ref=refs/notes/sediment show HEAD
   ```

4. Copy the actual `session_id` from the note entry with `tool: "cursor"`, then
   verify that Session:

   ```bash
   CURSOR_SESSION_ID='<Session identifier from the note>'
   sediment doctor "$PILOT_REPO" --agent cursor --session-id "$CURSOR_SESSION_ID"
   ```

Don't add transcript or Inference-call flags; Cursor doesn't supply those signals.
If the note is missing, use the
[Cursor troubleshooting guide](../capture/agents/cursor.md#limits-and-troubleshooting).

## Verify pi

1. Start the pinned pi runtime from the configured shell:

   ```bash
   cd "$PILOT_REPO"
   pi
   ```

2. In a separate Session, ask pi to create a harmless `pilot_pi.py` file with
   its `write` tool. Review the file, then end the Session. Approved transcript
   capture runs at Session shutdown.
3. Commit the file and inspect its note:

   ```bash
   git add -- pilot_pi.py
   git commit -m 'test: verify pi capture'
   git notes --ref=refs/notes/sediment show HEAD
   ```

4. Copy the `session_id` for `tool: "pi"`, then verify the Session:

   ```bash
   PI_SESSION_ID='<Session identifier from the note>'
   sediment doctor "$PILOT_REPO" --agent pi --session-id "$PI_SESSION_ID"
   ```

If you opted in to Edit observations, repeat that doctor command with `--transcripts`.
Require `ok` for the pi transcript opt-in and Edit observation as well as the
decision and note. A successful tool execution remains an implicit accept.

## Verify Codex CLI

1. Start Codex with its generated telemetry profile:

   ```bash
   codex --profile sediment-pilot -C "$PILOT_REPO"
   ```

2. Run `/hooks` and review and trust the Sediment hooks.
3. In a separate Session, ask Codex to create `pilot_codex.py` with a
   single-file patch. Review the file, then end the Session so approved
   transcript capture can run.
4. Commit the file and inspect its note:

   ```bash
   git -C "$PILOT_REPO" add -- pilot_codex.py
   git -C "$PILOT_REPO" commit -m 'test: verify Codex capture'
   git -C "$PILOT_REPO" notes --ref=refs/notes/sediment show HEAD
   ```

5. Copy the `session_id` for `tool: "codex"`, then verify the Session:

   ```bash
   CODEX_SESSION_ID='<Session identifier from the note>'
   sediment doctor "$PILOT_REPO" --agent codex --session-id "$CODEX_SESSION_ID"
   ```

If you opted in to Edit observations, repeat the command with `--transcripts` and
require an Edit observation. Native completed Add and Update events retain
their actual tool-call identifiers. Multi-file patches have no Edit observations.
The profile procedure covers Codex CLI; Codex Desktop selecting that profile
isn't verified.

## Verify workstation recovery

Run this procedure against an isolated acceptance deployment before you enroll
more repositories. Repeat it for a real pi Developer decision and for each
enabled pi or Codex transcript channel. Use a separate Session with exactly one
supported edit or write for each run. Enroll a separate test repository and
replay worker against that deployment. Set `ACCEPTANCE_API_URL` to its URL and
log in there with your separate operator credential.

1. Require `pending: 0`, `blocked: 0`, and `worker_running: true` from
   `sediment delivery status`.
2. Stop the acceptance deployment's API while leaving its database intact.
3. Run the selected harness action. If you test a transcript channel, end the
   Session so its extractor runs.
4. Require at least one pending entry and no blocked entries from
   `sediment delivery status`. A note or successful harness action isn't
   durable-enqueue evidence.
5. Stop and restart the replay worker through the named process supervisor
   while the API remains stopped. Require the worker to return and the pending
   entry to remain.
6. Start the API. Wait through the bounded retry backoff, then require
   `pending: 0`, `blocked: 0`, and `worker_running: true`.
7. Commit the edit and copy the Session identifier from its note. Keep the
   online Session doctor results separate: transcript replay doesn't recover
   the native Codex Developer decision channel.
8. Use your separate operator login to verify that the metadata-only Session
   dossier contains exactly one Fact of the expected kind. The client rejects
   redirects and requires HTTPS outside literal loopback:

   ```bash
   RECOVERY_SESSION_ID='<Session identifier from the note>'
   RECOVERY_EVENT='developer_decision'  # use edit_observation for a transcript
   export RECOVERY_SESSION_ID RECOVERY_EVENT
   SEDIMENT_URL="${ACCEPTANCE_API_URL:?Set ACCEPTANCE_API_URL}" "$SEDIMENT_CHECKOUT/.venv/bin/python" - <<'PY'
   import os
   import urllib.parse
   from sediment_cli.client import get_json

   session = urllib.parse.quote(os.environ["RECOVERY_SESSION_ID"], safe="")
   dossier = get_json(f"/query/session/{session}")
   assert dossier["found"] and dossier["omitted_events"] == 0
   matching = [
       event
       for event in dossier["timeline"]
       if event["event_type"] == os.environ["RECOVERY_EVENT"]
   ]
   assert len(matching) == 1
   print(f'recovery Fact verified: {matching[0]["fact_id"]}')
   PY
   ```

This procedure closes only the workstation channel that you run. The buffer
doesn't cover Cursor hooks, native Codex telemetry, or forge webhooks. Test the
embedded gateway's separate buffer with a real routed call. Retain those live
results as evidence for your deployment's recovery checks.

## Add approved gateway capture

Keep the developer's model unchanged. Configure the gateway to serve that same
model before routing traffic. Follow
[Configure inference-call capture](../capture/managed-capture.md#configure-inference-call-capture)
for the capture callback and
[Configure inference-call capture for Codex](../capture/agents/codex.md#configure-inference-call-capture)
for a compatible Responses API route.

For pi using an Anthropic-compatible route, merge this provider into
`~/.pi/agent/models.json`. Preserve existing providers. Replace the URL and
model placeholder with the operator's values:

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

Load `SEDIMENT_GATEWAY_KEY` from the team's credential mechanism into the pi
environment. `apiKey` uses `$SEDIMENT_GATEWAY_KEY` literally in the JSON file so
pi resolves the variable. Copy input capabilities, reasoning settings, context,
and output limits from the existing model definition when pi's custom-model
defaults differ. Omitted price fields aren't
evidence of zero cost.

The shim defaults to provider `sediment` and API `anthropic-messages`. If you
use another registered provider or API, set `SEDIMENT_PROVIDER_ID` and
`SEDIMENT_PROVIDER_API` to match it. Open `/model` to reload the model file,
then select the matching model, or start a separate Session with:

```bash
cd "$PILOT_REPO"
pi --provider sediment --model '<existing Anthropic model ID>'
```

Repeat that harness's edit and commit procedure. Add `--inference-calls` to its
Session doctor command. The `Session Inference call` check verifies capture
within that Session; Inference calls don't carry a harness field. A successful
model response alone doesn't prove that the gateway callback delivered a Fact.

## Verify forge delivery

After each harness's commit, push the branch and inspect the remote notes ref:

```bash
git -C "$PILOT_REPO" push --set-upstream origin HEAD
git -C "$PILOT_REPO" ls-remote origin refs/notes/sediment
sediment commit "$(git -C "$PILOT_REPO" rev-parse HEAD)"
```

Require the remote note ref, the server's observed Session-to-commit
relationship, and the expected CI outcome after the workflow finishes. Verify
a real, approved pull-request merge before interpreting merge retention. The
deployment must receive its signed webhook and read the repository's private
Git data.

## Use the pilot evidence

Follow [Measure agent work](measure-agent-work.md). Retain denominators, coverage,
and skip counts. Implicit accepts don't qualify as human-explicit accepted work.
Use Edit observations for Session-end retention and Git/pull-request evidence
for merge retention. Compare models only within matched scope and coverage.

[Preserve the result](measure-agent-work.md#preserve-an-operational-result)
before changing software or capture configuration.

## Update or end enrollment

Before updating the checkout, endpoint, or token, end agent Sessions, record the
commit, and back up the deployment. Stop the replay worker through its process
supervisor. Inspect `sediment delivery status`; retain pending or blocked payloads.

If marker clients change, pause hooks across the clone's linked worktrees,
reconcile pending markers, and update every helper before restarting harnesses.
Follow [Recover a pending stamp](../capture/local-capture.md#recover-a-pending-stamp).
Don't run old and generation-aware writers together.

Before changing the endpoint, load the original environment and run one bounded
replay while the supervisor stays stopped:

```bash
. "$HOME/.sediment/env.sh"
sediment delivery replay --retry-blocked
sediment delivery status
```

Require `pending: 0` and `blocked: 0`. Replay refuses to send retained payloads
to a different destination. If the original endpoint is unavailable, retain
the queue with its original configuration or obtain participant approval to
delete it under the deployment's retention procedure.

Return to `$SEDIMENT_CHECKOUT`, check out the approved successor at the same
path, and repeat the locked Python install and, if used, pi install. Rerun `sediment install` for each repository
with the same developer identifier, profile, approved flags, and gateway arguments.

After token rotation, rerun `sediment login --capture` and profile enrollment.
An environment change doesn't refresh Codex's resolved header token.
Reinstallation preserves unrelated settings and pi transcript consent.

With the worker stopped, load the updated environment and retry entries that a
rejected credential blocked:

```bash
. "$HOME/.sediment/env.sh"
sediment delivery replay --retry-blocked
sediment delivery status
```

Resolve blocked entries before starting the supervisor. Require
`worker_running: true`, reload the environment, restart harnesses, and repeat
Session checks. Review changed Codex hooks. After marker-client updates, repeat
concurrent edit/stamp checks across linked worktrees before unattended capture.

Before removing the last enrolled repository, stop and disable the replay worker.
Drain pending and blocked entries to the approved receiver, or obtain participant
approval to delete them under the retention procedure. Keep the checkout and
queue in place while the worker runs.

From the installing checkout, remove repository capture:

```bash
sediment uninstall "$PILOT_REPO"
```

If the machine stops capturing all repositories, remove user-level hooks too:

```bash
sediment uninstall "$PILOT_REPO" --agents
```

Follow [Uninstall capture](../capture/local-capture.md#uninstall-capture) for
profile and environment cleanup. User-level removal affects all repositories
using those hooks. Remove unused gateway providers and restart harnesses before
removing the checkout and supervisor configuration. Remove the delivery directory
only after its entries reach the approved terminal state.

Uninstalling doesn't erase stored Facts or historical commit notes. If removal
of captured data is required, use the operator's
[Quarantine and wholesale deletion](deploy.md#83-quarantine-and-wholesale-deletion)
procedure.

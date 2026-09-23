# Run a Cursor, pi, and Codex pilot

Enroll a macOS or Linux team from a pinned source checkout, then verify Cursor
desktop, pi, and Codex CLI capture. The operator prepares the deployment and
hands each developer the enrollment settings.

Complete the enabled checks in [Validate a deployment](validate-deployment.md)
before expanding the pilot. For one-machine evaluation, use the
[Quickstart](../quickstart.md).

## Agree the capture scope

Record the participating repositories, developer identifiers, harness versions,
model/provider settings, pilot window, and questions from
[Why Sediment?](../explanation/operational-value.md).

Decision: enroll Developer decisions and commit Attribution after agreeing each
harness's payload. Add Edit observations or gateway capture only for the
evidence that the participants approve.

| Harness | Developer decisions and commit Attribution | Optional Edit observations | Optional Inference calls |
| --- | --- | --- | --- |
| Cursor desktop | Successful Agent `Write` calls produce implicit accepts. Tab edits mark Attribution only. | Unsupported. | Unsupported. |
| pi | Successful `edit` and `write` calls produce implicit accepts. | `--transcripts` enables Session-end Edit observations through `SEDIMENT_PI_TRANSCRIPTS=1`. | A configured provider can route through a compatible gateway. |
| Codex CLI | Supported patch decisions retain native approval explicitness. Native telemetry can include patch/tool arguments. | `--transcripts` installs the Session-end extractor for successful single-file patches. | A configured Responses API provider can route through a compatible gateway. |

Codex native telemetry can contain patch code even with `log_user_prompt=false`
and transcript capture disabled. pi decisions are metadata-only. Transcript
capture adds applied and observed text; gateway capture adds model inputs and
outputs. Agree these [privacy boundaries](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
before enrollment.

## Prepare the deployment

The operator completes [Deploy Sediment](deploy.md) from the agreed checkout.
Use the durable Docker Compose deployment for a shared pilot. Keep the API and
gateway behind authenticated HTTPS ingress, and configure private-repository
credentials for the mirror.

Before handoff, provide a separate disposable PostgreSQL 17 administrative
database. Don't point this check at the pilot deployment. From a clean checkout
of the approved revision, write the revision check and installed
[release rehearsal](rehearse-release.md#run-the-no-publish-rehearsal) output to
one acceptance record:

```bash
SEDIMENT_REVISION='<approved full commit hash>'
export SEDIMENT_TEST_DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/postgres'
ACCEPTANCE_DIR="$HOME/sediment-acceptance"
ACCEPTANCE_LOG="$ACCEPTANCE_DIR/pipeline-acceptance-$SEDIMENT_REVISION.log"
ACCEPTANCE_TMP="$ACCEPTANCE_LOG.tmp"

mkdir -p "$ACCEPTANCE_DIR"
rm -f "$ACCEPTANCE_TMP"
if {
  test "${#SEDIMENT_REVISION}" -eq 40 &&
  test "$(git rev-parse HEAD)" = "$SEDIMENT_REVISION" &&
  test -z "$(git status --porcelain=v1 --untracked-files=all)" &&
  printf 'sediment_revision=%s\nworktree=clean\n' "$SEDIMENT_REVISION" &&
  uv run python scripts/release_rehearsal.py
} >"$ACCEPTANCE_TMP" 2>&1
then
  mv "$ACCEPTANCE_TMP" "$ACCEPTANCE_LOG"
else
  cat "$ACCEPTANCE_TMP"
  rm -f "$ACCEPTANCE_TMP"
  exit 1
fi
cat "$ACCEPTANCE_LOG"
```

Retain the pass message and `pipeline acceptance:` record with the revision and
`worktree=clean` lines. The rehearsal verifies synthetic installed capture,
replay, storage, reports, bundles, and exports. Verify live harness, gateway,
and forge behavior in the checks that follow.

Then [Configure push and CI capture](../capture/managed-capture.md#configure-push-and-ci-capture).
Verify real Push, pull-request, and continuous integration (CI) deliveries from
the pilot repository. A successful local health request doesn't verify the
deployment's network, private Git credentials, or webhook delivery.

Give each developer:

- the full approved Sediment commit hash and the repository URL;
- the API URL and a named ingest-only token through the team's credential channel;
- a developer identifier and the local path of a pilot repository; and
- when gateway capture is approved, its URL, client key, supported API, and
  the exact model ID already used by that developer.

Keep upstream provider credentials on the gateway. Verify provider availability
and a funded account before requiring gateway capture. The bundled gateway
maps `claude-*` to Anthropic; it doesn't serve native OpenAI models. A different
model route needs a compatible gateway configuration.

## Install a pinned checkout

Install Git, Python 3.12 through `uv`, and Node 24. Install and authenticate
Cursor and Codex using their normal setup, including Cursor's command-line
launcher. Start and close each participating harness once so its user
configuration directory exists.

Replace the example values with the operator's handoff. These commands use a
POSIX shell:

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
npm ci --prefix shims/pi --no-audit --no-fund || exit 1
export PATH="$SEDIMENT_CHECKOUT/.venv/bin:$SEDIMENT_CHECKOUT/shims/pi/node_modules/.bin:$PATH"

git rev-parse HEAD
sediment --version
node --version
pi --version
codex --version
```

The first output must match the approved commit. Install and authenticate pi
0.84.1 separately; the locked shim dependencies don't install its runtime.
This guide's Codex profile and transcript paths support Codex 0.153.4. Use
Node 24 with that pi runtime. Start `pi` once before enrollment
if `~/.pi/agent` doesn't exist. Authenticate the participant's existing model
through pi's `/login` or its existing credential mechanism, then close pi.
Don't change models to obtain credentials. If the pilot uses gateway credentials
only, complete [Add approved gateway capture](#add-approved-gateway-capture)
before the pi verification procedure.

Keep the checkout and `.venv` at this path; hooks and the pi extension reference
them. In later shells, export the same PATH. Capture-only machines don't need
PostgreSQL client libraries. Local server prerequisites are in the
[Quickstart](../quickstart.md).

## Enroll the developer machine

Connect the CLI and install capture in the pilot repository:

```bash
sediment login "$PILOT_API_URL" --capture
sediment install --user-id "$PILOT_USER_ID" \
  --codex-profile sediment-pilot "$PILOT_REPO"
. "$HOME/.sediment/env.sh"
```

`login --capture` prompts for the ingest-only token. Review the installation
summary: the installer adds hooks for detected harnesses, including Claude Code,
and preserves unrelated entries.

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

Keep the verification files on a dedicated branch in the pilot repository:

```bash
git -C "$PILOT_REPO" switch -c sediment-pilot-check
```

## Verify Cursor desktop

The following harness checks read remote Session evidence. An authorized
operator must first run `sediment login "$PILOT_API_URL"` on the machine that
runs the checks. This operator login remains separate from capture enrollment;
the installer never distributes its credential to the harnesses.

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

Require `ok` for the commit Session note and Developer decision. Unsupported
Cursor content and Inference-call requirements fail, so don't add those flags.
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

For every harness, wait for telemetry to flush before interpreting missing
evidence. A failed check identifies an absent, unsupported, incomplete, or
unreadable requirement. Diagnose it before enrolling more repositories.
Organization-wide `sediment facts` counts don't replace this Session check.

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

Use [Measure agent work](measure-agent-work.md) for reports and interpretation.
Retain denominators, coverage, and skip counts. Pilot limits include:

- Cursor supplies neither Inference calls nor Edit observations.
- pi/Cursor implicit accepts and automatic Codex approvals don't qualify as
  human-explicit accepted work.
- Session-end retention needs Edit observations; merge retention also needs Git
  and pull-request evidence.
- Model comparisons need matched scope and coverage. Sediment has no cost-report
  command and doesn't infer absent prices.

[Preserve the result](measure-agent-work.md#preserve-an-operational-result)
before changing software or capture configuration.

## Update or end enrollment

Before updating, end active agent Sessions, record the installed commit, and
back up the deployment. Stop the workstation replay worker through its process
supervisor before you change the checkout, endpoint, or token. Inspect
`sediment delivery status`; don't discard pending or blocked payloads.

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

Fetch the operator-approved successor, check it out at the same path, and repeat
the locked Python and pi installs. Rerun `sediment install` for each
participating repository with the same developer identifier, profile, and
approved flags. If you used `--gateway-url` or `--gateway-key`, repeat those
values when you rewrite the generated environment.

After rotating the capture token, rerun `sediment login --capture` and profile
enrollment.
The Codex header contains a resolved token; changing the environment alone
doesn't refresh it. Reinstallation preserves unrelated settings and an existing
pi transcript opt-in. It isn't a way to revoke consent.

With the worker stopped, load the updated environment and retry entries that a
rejected credential blocked:

```bash
. "$HOME/.sediment/env.sh"
sediment delivery replay --retry-blocked
sediment delivery status
```

If blocked entries remain, correct the reported failure and repeat the bounded
replay. Start the supervisor only after `blocked: 0`. Require
`worker_running: true`, reload the environment, restart the harnesses, review
changed Codex hooks, and repeat the Session checks.
After a marker-client update, repeat the concurrent edit/stamp check across the
clone's linked worktrees before resuming unattended capture.

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
skipped Codex profiles, manual environment settings, and pi registration.
User-level removal affects all repositories using those hooks. Remove unused
gateway providers, restart harnesses, and then remove the checkout and retired
supervisor configuration. Remove the delivery directory only after its entries
reach the approved terminal state.

Uninstalling doesn't erase stored Facts or historical commit notes. If removal
of captured data is required, use the operator's
[Quarantine and wholesale deletion](deploy.md#83-quarantine-and-wholesale-deletion)
procedure.

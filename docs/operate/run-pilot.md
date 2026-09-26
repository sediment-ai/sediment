# Run a Cursor, pi, and Codex pilot

Prepare a shared deployment, enroll developers, and verify capture before
expanding the pilot. For a one-machine evaluation, use the
[Quickstart](../quickstart.md).

## Prepare the deployment

1. Follow [Deploy Sediment](deploy.md) to start the shared server with HTTPS.
2. Configure [push and continuous integration (CI) capture](../capture/managed-capture.md#configure-push-and-ci-capture),
   including mirror access to private repositories.
3. Agree the repositories, developers, pilot window, and
   [capture scope](../capture/agent-integrations.md#compare-integrations).

Codex telemetry can contain patch code even when transcript capture is disabled.
Transcripts add applied and observed text; gateway capture adds model inputs and
outputs.

Agree the [privacy boundaries](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
before enrollment.

Give each developer the API URL, a named ingest-only token through your
credential channel, and their developer identifier. Keep operator credentials
with the people who verify and report on capture.

## Install Sediment

On each developer machine, use macOS with Homebrew, Debian, or Ubuntu. Run the
installer as your normal user; Debian and Ubuntu require `sudo`. Git and `curl`
must be on PATH.

```bash
curl -fsSL https://sediment.so/install.sh | sh
```

The installer installs the published `sediment-cli` package from PyPI. If it
prints a PATH instruction, run that instruction before continuing.

```bash
sediment --version
```

Install and authenticate each participating agent through its normal setup.
Start and close it once before enrollment so its configuration directory exists.

For pi, complete the [pi integration setup](../capture/agent-integrations.md#pi)
separately. The PyPI package doesn't include the pi extension.

## Enroll each developer

Replace these values with the deployment URL, developer identifier, and local
repository path:

If the developer doesn't use Codex, omit `--codex-profile sediment-pilot`.

```bash
PILOT_API_URL='https://sediment-api.example.com'
PILOT_USER_ID='alice'
PILOT_REPO='/absolute/path/to/pilot-repo'

sediment login "$PILOT_API_URL" --capture
sediment install --user-id "$PILOT_USER_ID" \
  --codex-profile sediment-pilot "$PILOT_REPO"
. "$HOME/.sediment/env.sh"
sediment doctor "$PILOT_REPO"
```

`login --capture` prompts for the ingest-only token. Review the installation
summary: Sediment configures detected agents, including Claude Code. Resolve
every `FAIL` from `doctor` before continuing.

If participants approve additional capture, follow the relevant setup:

| Capture | Setup |
| --- | --- |
| Edit observations | [Opt in to transcript capture](../capture/local-capture.md#opt-in-to-transcript-capture) |
| Model inputs and outputs | [Configure inference-call capture](../capture/managed-capture.md#configure-inference-call-capture); keep each developer's model unchanged |
| Buffered delivery through outages | [Preserve prepared payloads through outages](../capture/local-capture.md#preserve-prepared-payloads-through-outages); requires approved local payload storage and a supervised replay worker |

## Verify capture

On the machine running these checks, log in with a separate operator token:

```bash
sediment login "$PILOT_API_URL"
git -C "$PILOT_REPO" switch -c sediment-pilot-check
```

Repeat the following check for each participating agent, committing one agent's
file before testing the next:

1. Load `. "$HOME/.sediment/env.sh"` and start the agent:

   | Agent | Start |
   | --- | --- |
   | Cursor desktop | Fully quit Cursor, then run `cursor "$PILOT_REPO"` from this shell. |
   | Codex CLI | Run `codex --profile sediment-pilot -C "$PILOT_REPO"`. Use `/hooks` to review and trust the Sediment hooks. |
   | pi | After the separate integration setup, run `cd "$PILOT_REPO"` and `pi`. |

2. Ask the agent to create a harmless file. Use Cursor Agent rather than Tab,
   a single-file patch in Codex, or pi's `write` tool. End the Session so any
   enabled transcript capture can run.
3. Review and commit only that file, then inspect the commit note:

   ```bash
   git -C "$PILOT_REPO" add -- '<created-file>'
   git -C "$PILOT_REPO" commit -m 'test: verify agent capture'
   git -C "$PILOT_REPO" notes --ref=refs/notes/sediment show HEAD
   ```

4. Copy the `session_id` from the note entry for that agent. Replace the
   placeholders, using `cursor`, `codex`, or `pi` for the agent, and run:

   ```bash
   sediment doctor "$PILOT_REPO" --agent '<agent>' \
     --session-id '<Session identifier from the note>'
   ```

Require `ok` for the commit Session note and Developer decision. For enabled
transcript or gateway capture, add `--transcripts` or `--inference-calls`.
Cursor supports neither flag.

Allow telemetry to flush before investigating missing evidence. Use the
[agent guides](../capture/agent-integrations.md) to resolve failures.
Organization-wide Fact counts don't replace this Session check.

## Verify forge delivery

Push the verification branch and check the deployment:

```bash
git -C "$PILOT_REPO" push --set-upstream origin HEAD
git -C "$PILOT_REPO" ls-remote origin refs/notes/sediment
sediment commit "$(git -C "$PILOT_REPO" rev-parse HEAD)"
```

Require the remote notes ref, the server's Session-to-commit relationship, and
the expected CI outcome after the workflow finishes. Before interpreting merge
retention, verify a real pull-request merge and its signed webhook delivery.

## Use the pilot evidence

Use [Measure agent work](measure-agent-work.md) for reports. Keep denominators,
coverage, and skip counts with each result:

- Cursor supplies neither Inference calls nor Edit observations.
- pi/Cursor implicit accepts and automatic Codex approvals aren't human-explicit
  accepted work.
- Session-end retention needs Edit observations. Merge retention also needs Git
  and pull-request evidence.
- Model comparisons need matched scope and coverage. Missing evidence isn't a
  negative outcome.

Before expanding the pilot, complete the applicable checks in
[Validate a deployment](validate-deployment.md).

## Update or end enrollment

If you enabled buffering, stop the replay worker before an upgrade, credential
or destination change, or uninstall.

Follow [sender buffer operations](../capture/local-capture.md#preserve-prepared-payloads-through-outages)
to drain retained payloads to their original destination. Restart the worker
after reenrollment, or disable it when ending enrollment.

Before upgrading, end active Sessions, record `sediment --version`, and back up
the deployment. Rerun the curl installer, repeat enrollment with the same
identifier and approved options, and repeat the capture checks.

After rotating an ingest token, repeat `sediment login --capture` and
`sediment install`. Restart the agents; Codex's generated profile contains the
token and must be refreshed.

To remove repository capture:

```bash
sediment uninstall "$PILOT_REPO"
```

Follow [Uninstall capture](../capture/local-capture.md#uninstall-capture) when
removing machine-wide hooks. Uninstalling leaves stored Facts and historical
commit notes intact.

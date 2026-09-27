# Enroll your team

Use this page to connect developers' machines and repositories to a shared
Sediment deployment, and to verify that each agent's work arrives. Start with a
deployment from [Deploy Sediment on EC2](deploy-ec2.md) or
[Deploy Sediment on your own host](deploy.md) that passes its HTTPS health
check.

Before enrollment, agree with participants which repositories and agents
Sediment captures. [Privacy boundaries](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
lists what each capture path records.

## Create a capture token for each developer

Each developer machine gets its own named capture token. A capture token can
send data but can't read it.

1. Stop Sediment.
2. For each machine, generate a token:

   ```bash
   openssl rand -hex 32
   ```

3. In a private editor, add one `SEDIMENT_INGEST_TOKENS` line to
   `~/.sediment/server/server.env`. Its value is a JSON object that maps each
   machine's name to its token:

   ```text
   SEDIMENT_INGEST_TOKENS={"alice-laptop":"<token>","bob-laptop":"<token>"}
   ```

   The names `operator`, `legacy`, and `retrieval` are reserved. Keep the file
   mode at `0600`.

4. Start Sediment.
5. Send each developer only their own token through a private channel. Keep
   `SEDIMENT_OPERATOR_TOKEN` and the rest of `server.env` to yourself.

To add a developer later, repeat these steps, and add their entry to the
existing `SEDIMENT_INGEST_TOKENS` object.

## Connect your repositories

1. Create the [GitHub webhooks](../capture/managed-capture.md#configure-push-and-ci-capture)
   for Pushes, CI, pull requests, and repository changes.
2. For private repositories, give the server
   [read-only mirror credentials](../capture/managed-capture.md#configure-repository-mirrors).

## Install Sediment on each machine

Each developer runs these steps on a macOS or Linux machine with Git and `curl`
on `PATH`.

1. Install the version that your deployment runs. `/health` reports it:

   ```bash
   curl -fsSL https://sediment.so/install.sh | \
     sh -s -- --capture-only --version '<deployment version>'
   ```

   If the installer prints a `PATH` instruction, run it. Then check that
   `sediment --version` prints the same version.

2. Install and sign in to each agent through its normal setup. Start and close
   it once so that its configuration directory exists.
3. Enroll the machine. Replace the deployment URL, developer identifier, and
   repository path:

   ```bash
   API_URL='https://sediment.example.com'
   USER_ID='alice'
   REPO='/absolute/path/to/repo'

   sediment login "$API_URL" --capture
   sediment install --user-id "$USER_ID" --codex-profile sediment "$REPO"
   . "$HOME/.sediment/env.sh"
   sediment doctor "$REPO"
   ```

   `login --capture` prompts for the developer's capture token. `install`
   configures every agent that it detects. If the developer doesn't use Codex,
   omit `--codex-profile sediment`. Resolve every `FAIL` from `doctor`.

Optional capture needs separate consent and setup:

| Capture | Setup |
| --- | --- |
| Edit observations | [Opt in to transcript capture](../capture/local-capture.md#opt-in-to-transcript-capture) |
| Model inputs and outputs | [Configure inference-call capture](../capture/managed-capture.md#configure-inference-call-capture) |
| Delivery through server outages | [Preserve prepared payloads through outages](../capture/local-capture.md#preserve-prepared-payloads-through-outages) |

## Verify capture

A `/health` response and organization-wide Fact counts don't prove capture.
Check one real Session for each agent that the team uses.

1. On an enrolled machine, also sign in with the operator token, and create a
   scratch branch:

   ```bash
   sediment login "$API_URL"
   git -C "$REPO" switch -c sediment-check
   ```

2. Load `. "$HOME/.sediment/env.sh"`, and start the agent from that shell:

   | Agent | Start |
   | --- | --- |
   | Claude Code | Run `cd "$REPO" && claude`. |
   | Codex CLI | Run `codex --profile sediment -C "$REPO"`. Use `/hooks` to review and trust the Sediment hooks. |
   | Cursor desktop | Run `cursor "$REPO"`. |
   | pi | Run `cd "$REPO" && pi`. |

3. Ask the agent to create a small file, and end the Session. Use Cursor
   Agent rather than Tab, pi's `write` tool, and a single-file patch in Codex.
4. Commit only that file, and read the commit's Session note:

   ```bash
   git -C "$REPO" add -- '<created-file>'
   git -C "$REPO" commit -m 'test: verify agent capture'
   git -C "$REPO" notes --ref=refs/notes/sediment show HEAD
   ```

5. Copy the `session_id` from the note, and check that Session on the server.
   Set `--agent` to `codex`, `cursor`, or `pi`:

   ```bash
   sediment doctor "$REPO" --agent '<agent>' --session-id '<session_id>'
   ```

   Each check reports `ok`. If you enabled transcript or gateway capture, add
   `--transcripts` or `--inference-calls`. Cursor supports neither flag.

   For Claude Code, `doctor --agent` isn't available. Run `sediment facts`
   before and after the Session, and check that `developer_decisions` grew.

Repeat steps 2–5 for each agent, and commit each agent's file before you test
the next. Let telemetry flush before you investigate a failure. To fix one, see
the agent's page in [Agent integrations](../capture/agent-integrations.md).

## Verify forge delivery

Push the scratch branch, and check that the server linked the commit to its
Session:

```bash
git -C "$REPO" push --set-upstream origin HEAD
git -C "$REPO" ls-remote origin refs/notes/sediment
sediment commit "$(git -C "$REPO" rev-parse HEAD)"
```

The remote has `refs/notes/sediment`, and `sediment commit` lists the Session.
After the CI workflow finishes, the commit also shows its CI outcome. Merge
retention needs a real pull-request merge, so verify one before you read that
report.

## Know what each agent captures

Agents supply different evidence, so compare them with care:

- Cursor supplies neither Inference calls nor Edit observations.
- Cursor and pi accepts, and automatic Codex approvals, are implicit. Reports
  don't count them as human-explicit accepts.
- Session-end retention needs Edit observations. Merge retention also needs
  pull-request webhooks.
- Missing evidence isn't a negative outcome.

[Compare integrations](../capture/agent-integrations.md#compare-integrations)
lists each agent's signals.

Next, [measure agent work](measure-agent-work.md).

## Remove a developer

1. If the machine runs a sender replay worker, drain and stop it with
   [Preserve prepared payloads through outages](../capture/local-capture.md#preserve-prepared-payloads-through-outages).
2. On the machine, remove capture from each repository:

   ```bash
   sediment uninstall "$REPO"
   ```

   To remove the machine-wide agent hooks too, follow
   [Uninstall capture](../capture/local-capture.md#uninstall-capture).

3. On the server, [revoke the token](maintain.md#rotate-credentials).

Stored Facts and existing commit notes remain.

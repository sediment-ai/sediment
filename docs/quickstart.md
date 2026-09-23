# Quickstart

Capture your first agent Session on one machine.

Run a local server and verify synthetic capture in a scratch repository.
For a shared deployment, start at
[Deploy the API](operate/deploy.md#2-deploy-the-api).

## 0. Check the prerequisites

Use a supported macOS release with Homebrew, or a supported Debian or Ubuntu
release. Run the installer as your normal user. Debian and Ubuntu require
`sudo` access to install host packages. Git and `curl` must be on PATH.

## 1. Install the CLI

```bash
curl -fsSL https://sediment.so/install.sh | sh
```

The installer installs `sediment-cli` from PyPI in an isolated tool environment
with Python 3.12. It installs `uv` if needed, plus the maintained host libraries
that the local PostgreSQL server needs. You don't need a source checkout or
Docker.

If the installer prints an `export PATH=...` instruction, run it in this
terminal before continuing. Repeat the same instruction in each terminal where
you run `sediment`.

Verify:

```bash
sediment --help >/dev/null && echo "sediment ok"
```

```text
sediment ok
```

## 2. Start a local server

In the terminal that you used for installation, run:

```bash
sediment server
```

The command downloads PostgreSQL on first use, creates the database, and starts
the API. It keeps the database, credentials, and mirror under
`~/.sediment/server`.

Keep this terminal open. When the server reports `Application startup complete.`,
open a second terminal. If installation printed a PATH instruction, repeat it
in this terminal. Then verify the server:

```bash
curl -sf http://127.0.0.1:8000/health
```

```text
{"status":"ok","version":"0.1.0"}
```

If the health check fails, read the error in the server terminal. PostgreSQL
startup logs live in `~/.sediment/server/postgres.log`.

When you finish the tutorial, press Ctrl+C in the server terminal to stop both
the API and its database. Running `sediment server` again reuses the stored data
and credentials.

Continue in the second terminal.

## 3. Log in

```bash
sediment login http://127.0.0.1:8000
```

```text
using the token from ~/.sediment/server/server.env
✓ logged in to http://127.0.0.1:8000 (org default)
```

The loopback login verifies and stores both generated credentials: the operator
credential for reading Facts and the ingest credential for capture. `install`
puts only the ingest credential in agent configuration. For remote deployments
and named ingest identities, follow [Configure local capture](capture/local-capture.md).

`error: is the server running?` means step 2's verification never passed.

## 4. Wire a demo repo and this machine

Use a scratch repo so the quickstart leaves your real work untouched:

```bash
git init -q ~/sediment-quickstart
cd ~/sediment-quickstart
sediment install .
```

`install` adds repository git hooks, user-level hooks for detected agents, and
an environment file. pi requires a source checkout; a package-only installation
doesn't include its extension.

Verify:

```bash
sediment doctor . && echo "doctor exit 0"
```

```text
ok    claude-code hook: present in /Users/you/.claude/settings.json
info  codex hook: not detected (/Users/you/.codex does not exist)
info  cursor hooks: Cursor not detected (no ~/.cursor)
info  pi extension: pi not detected (no ~/.pi/agent)
info  fleet template: init.templateDir unset — not a fleet machine
info  attribution log: /Users/you/.sediment/attribution.log: no events recorded
ok    server[http://127.0.0.1:8000]: reachable, token valid (org default)
ok    hooks[/Users/you/sediment-quickstart]: all three current in ...
ok    notes.rewriteRef[/Users/you/sediment-quickstart]: refs/notes/sediment
info  notes ref[/Users/you/sediment-quickstart]: no origin remote; none yet locally
ok    markers[/Users/you/sediment-quickstart]: no unconsumed markers
doctor exit 0
```

Rows depend on the installed agents. `info` is informational; `FAIL` makes
`doctor` exit nonzero. Require `doctor exit 0` before continuing.

## 5. Prove the capture chain

Create a synthetic Session marker and commit it to verify the Git hooks:

```bash
printf '{"session_id":"quickstart","cwd":"%s"}\n' "$PWD" | sediment mark --tool claude-code
git -c user.name=sediment -c user.email=quickstart@example.com \
  commit -q --allow-empty -m "sediment quickstart"
git notes --ref=refs/notes/sediment show HEAD
```

```text
{"v": 1, "sessions": [{"tool": "claude-code", "session_id": "quickstart", "stamped_at": "2026-08-14T00:33:32+00:00"}]}
```

The note records the synthetic Session-to-commit relationship in Git. A mirror
refresh after a Push can store a Session-to-commit observation; Attribution is
then derived. The local note alone doesn't add a server Fact.

## 6. Prove the server side

`demo` posts one synthetic inference call and one Developer decision through
the same ingest routes that clients use. It then prints the Fact counts:

```bash
sediment demo
```

```text
posting demo session (synthetic) to http://127.0.0.1:8000
  posted 1 completion and 1 decision as session sediment-demo

table                  total  visible
sessions                   1        -
inference_calls            1        1
developer_decisions        1        1
ci_outcomes                0        0
pushes                     0        0
edit_observations          0        0
rejected_edits             0        0
retry_linkages             0        0
quarantine_revision: 0

These are synthetic facts, not your agent's. They prove the ingest path works end to end.
```

The demo verifies synthetic ingestion and storage. Repeating it retains the
same Facts through database deduplication. Verify your agent separately.

## 7. Use it on real work

```bash
sediment install /path/to/your-repo
```

Load `. "$HOME/.sediment/env.sh"`, then restart the agent. Edit a file and
commit the change.
`sediment facts` grows `developer_decisions` when the agent emits a supported
decision event. Each commit carries its own Session note.

[Agent integrations](capture/agent-integrations.md) routes you to the
agent-specific decision, inference-call, Edit observation, and verification
steps.

Each verb prints its own help (`sediment install --help`).

## Configure additional capture

Use [Agent integrations](capture/agent-integrations.md) to configure native
decisions and optional Edit observations. Connect [managed capture](capture/managed-capture.md)
for gateway Inference calls, Pushes, pull requests, and CI outcomes.

## Clean up the demo

```bash
sediment uninstall ~/sediment-quickstart && rm -rf ~/sediment-quickstart
```

That leaves the local server's data. To remove it, press Ctrl+C in the server
terminal. Then run this command, which deletes the local database, credentials,
logs, downloaded PostgreSQL binaries, and mirror:

```bash
rm -rf ~/.sediment/server
```

## Next steps

- [Agent integrations](capture/agent-integrations.md) — compare evidence and
  configure Claude Code, Codex, Cursor, pi, or Copilot Chat
- [Configure local capture](capture/local-capture.md) — connect real
  repositories and opt in to transcript capture
- [Roll out managed capture](capture/managed-capture.md) — connect a gateway,
  webhooks, private mirrors, and a developer fleet
- [How capture works](explanation/how-capture-works.md) — understand the five
  signals and their privacy boundaries
- [Architecture](explanation/architecture.md) — understand the components,
  data flow, persistence boundaries, and network boundaries
- [Deploy runbook](operate/deploy.md) — run the production server on Docker
  Compose
- [Choose a training export](exports/training-exports.md) — prepare and audit
  DPO, SFT, diff-SFT, Recovery, or RLVR rows

# Quickstart

Capture your first agent Session on one machine.

This page runs everything locally: a server, one repo, one agent. For a
hosted deployment, start at
[Deploy the API](operate/deploy.md#2-deploy-the-api).

## 0. Check the prerequisites

Use a supported macOS release with Homebrew, or a supported Debian or Ubuntu
release. Git, `curl`, and `uv` must be on PATH.

```bash
for tool in git curl uv; do command -v "$tool" >/dev/null || exit 1; done
printf 'command prerequisites found\n'
```

```text
command prerequisites found
```

This local-server tutorial also needs the maintained PostgreSQL client library,
`libpq`, on the host. Capture-only machines that send to an existing deployment
don't need it. Sediment uses [Psycopg's Python implementation](https://www.psycopg.org/psycopg3/docs/basic/install.html#pure-python-installation).

Install the host libraries for this tutorial:

```bash
case "$(uname -s)" in
  Darwin)
    brew install libpq || exit 1
    export PATH="$(brew --prefix libpq)/bin:$PATH"
    ;;
  Linux)
    sudo apt-get update || exit 1
    sudo apt-get install -y libpq5 libxml2 libzstd1 liblz4-1 zlib1g || exit 1
    ;;
  *) echo 'Use supported macOS, Debian, or Ubuntu for this tutorial.' >&2; exit 1 ;;
esac
uv run --no-project --python 3.12 --with "psycopg>=3.3.5" python -c 'from psycopg import pq; print("libpq found")'
```

```text
libpq found
```

[Homebrew's libpq](https://formulae.brew.sh/formula/libpq) is keg-only. The PATH
setting lets Psycopg find its `pg_config`; keep this shell open for the server
step. [Debian's libpq5](https://packages.debian.org/bookworm/libpq5)
receives updates through the distribution's package repositories.

## 1. Install the CLI

Install the source preview in a persistent checkout. This path installs the
workspace members and their locked dependencies without a published Python
package. Run the block in the shell that you used for the prerequisites:

```bash
SEDIMENT_CHECKOUT="$HOME/.local/share/sediment"
mkdir -p "$(dirname "$SEDIMENT_CHECKOUT")"
git clone https://github.com/sediment-ai/sediment.git "$SEDIMENT_CHECKOUT" || exit 1
SEDIMENT_REVISION="$(git -C "$SEDIMENT_CHECKOUT" rev-parse HEAD)"
git -C "$SEDIMENT_CHECKOUT" checkout --detach "$SEDIMENT_REVISION" || exit 1
cd "$SEDIMENT_CHECKOUT" || exit 1
uv sync --locked --python 3.12 --no-dev || exit 1
export PATH="$SEDIMENT_CHECKOUT/.venv/bin:$PATH"
printf 'sediment_revision=%s\n' "$SEDIMENT_REVISION"
```

Record the printed full commit hash with your results. The detached checkout
keeps that revision until you choose another one. Keep the checkout and its
`.venv` at this path; installed hooks reference them. In a later shell, export
this PATH again before running `sediment`.

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
open a second terminal. Set the installed command's PATH and verify the server:

```bash
export PATH="$HOME/.local/share/sediment/.venv/bin:$PATH"
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

`install` wires three things:

- git hooks in your repo to record which agent Sessions contributed to
  each commit
- hooks for each agent it finds on this machine (Claude Code, Codex, Cursor,
  pi) —
  these are user-level, so they apply to every repo
- the agent telemetry env, generated from your login and sourced from your
  shell profiles

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

Your exact rows depend on which agents this machine has. `info` rows are
normal — an agent you don't use isn't a failure. Only `FAIL` rows are. Any
`FAIL` makes `doctor` exit non-zero, so `doctor exit 0` is the check that
matters.

## 5. Prove the capture chain

An agent hook records a Session marker on each tool call. The post-commit
hook turns the markers into a git note. Running the marker verb
by hand does exactly what the hook does — and unlike the hook, it works
without restarting your agent:

```bash
printf '{"session_id":"quickstart","cwd":"%s"}\n' "$PWD" | sediment mark --tool claude-code
git -c user.name=sediment -c user.email=quickstart@example.com \
  commit -q --allow-empty -m "sediment quickstart"
git notes --ref=refs/notes/sediment show HEAD
```

```text
{"v": 1, "sessions": [{"tool": "claude-code", "session_id": "quickstart", "stamped_at": "2026-08-14T00:33:32+00:00"}]}
```

The note is the commit Attribution Fact: this commit came from that agent
Session. It stays in git until a push webhook ingests it, so it doesn't
show up in the server's counts yet.

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

Read that last line literally. The demo proves the chain from a client
POST to a stored Fact, on this machine, with your token. It proves nothing
about your own agent, which isn't sending anything yet — that is step 7.

The Facts are synthetic but real once stored. `demo` refuses a non-loopback
server for that reason; `--force` overrides it if you meant to seed a
shared deployment. Re-running is safe: both doors dedup, so the counts stay
at one.

## 7. Use it on real work

```bash
sediment install /path/to/your-repo
```

Restart any running agent Sessions — each agent reads hooks and environment at
startup. Then work as usual: edit files with your agent and commit.
`sediment facts` grows `developer_decisions` when the agent emits a supported
decision event. Each commit carries its own Session note.

[Agent integrations](capture/agent-integrations.md) routes you to the
agent-specific decision, inference-call, Edit observation, and verification
steps.

Each verb prints its own help (`sediment install --help`).

## What you have — and what needs a deployment

The local setup captures decisions and commit Attribution. The rest need
their own wiring:

| Signal | Captured by | Wired by |
|---|---|---|
| Developer decisions | Agent telemetry or adapter | This quickstart plus the selected [agent guide](capture/agent-integrations.md) |
| Commit Attribution | Git hooks | This quickstart |
| Edit survival and external line counts | Transcript hooks | `sediment install --transcripts` (opt-in — [Configure local capture](capture/local-capture.md#opt-in-to-transcript-capture)) |
| Inference calls (structured input + output) | LLM gateway callback | [Roll out managed capture](capture/managed-capture.md#configure-inference-call-capture) |
| Push / Pull request revisions and merge / CI outcomes | Forge webhooks | [Roll out managed capture](capture/managed-capture.md#configure-push-and-ci-capture) |

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

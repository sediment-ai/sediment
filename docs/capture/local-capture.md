# Configure local capture

Use this page to connect one macOS or Linux developer machine to a Sediment
deployment. Each agent guide in [Agent integrations](agent-integrations.md)
builds on these steps. To enroll a whole team, follow
[Enroll your team](../operate/run-pilot.md).

## Before you begin

You need the following:

- A macOS or Linux machine with Git and `curl` on `PATH`. The installer refuses
  native Windows.
- The deployment's HTTPS URL, its Sediment version, and your capture token from
  the operator. For a local trial, use the [Quickstart](../quickstart.md)
  instead.
- A git repository
- Claude Code, Codex, Cursor, or pi. Start each agent once so that its
  configuration directory exists; the installer skips an agent without one.

## Install the CLI

Install the version that the deployment runs:

```bash
curl -fsSL https://sediment.so/install.sh | \
  sh -s -- --capture-only --version '<deployment version>'
```

The installer supplies Python 3.12 and the CLI without changing host packages.
If it prints a `PATH` instruction, run it. Then check that `sediment --version`
prints the same version.

## Connect the CLI

Sign in with your capture token:

```bash
sediment login https://sediment-api.example.com --capture
```

The command prompts for the token, checks that it can send capture data, and
stores it in `~/.sediment/config.json` with mode `0600`. To sign in without a
prompt, pipe the token on standard input:

```bash
printf '%s\n' "$SEDIMENT_INGEST_TOKEN" | \
  env -u SEDIMENT_INGEST_TOKEN sediment login \
    https://sediment-api.example.com --capture --with-token
```

Use the deployment's root URL, with no credentials, query, or fragment. Plain
HTTP works only for `localhost`, `127.0.0.0/8`, and `[::1]`.

## Install capture

Run the installer for each repository that you want to capture:

```bash
sediment install --user-id '<developer>' /path/to/repo
. "$HOME/.sediment/env.sh"
```

The installer adds git hooks to the repository and hooks to each agent that it
finds. It writes the capture endpoint, token, and developer identifier to
`~/.sediment/env.sh` and a fish equivalent, and loads them from your shell
profiles. Rerun it after you install another agent, or for another repository.

Each run rewrites `env.sh` from the flags that you pass, so always pass the same
`--user-id`. Otherwise the rerun drops it. The installer changes only
Sediment's own entries in agent configuration.

Start each agent from a shell that loaded `env.sh`. Fully quit a running
desktop agent first, because it keeps its old environment. Cursor is the
exception: its hooks read `env.sh` directly.

The hooks call the CLI by its absolute path. If you move or reinstall the CLI,
rerun `sediment install`.

Sediment preserves an existing shell hook's exit status and any commands after
its marked block. Capture failures don't turn a successful repository check into
a failure. The `prepare-commit-msg` hook accepts an absent commit source when
your hook enables `set -u`. Rerun `sediment install` to update an installed block.

If another system manages the agents' environment, pass `--no-env`, and set
`SEDIMENT_OTLP_ENDPOINT` and `SEDIMENT_INGEST_TOKEN` there yourself. With
`--no-env`, Cursor's hooks also read Cursor's process environment instead of
`env.sh`. The [CLI reference](../reference/cli.md#sediment-install) lists every
flag.

## Opt in to transcript capture

Transcript capture sends the text that the agent applied and the file's content
at Session end, which produces Edit observations. Claude Code also records
external line counts, Rejected edits, and Retry linkages. Review
[Privacy boundaries and ceilings](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
before you opt in.

Rerun the installer with your usual flags plus `--transcripts`:

```bash
sediment install --user-id '<developer>' --transcripts /path/to/repo
. "$HOME/.sediment/env.sh"
```

Restart the agents. The installer adds Session-end extractors for Claude Code
and Codex, a pre-edit snapshot hook for Claude Code, and the pi opt-in. Each
agent guide lists what its extractor supports.

With `--no-env`, set `SEDIMENT_OTLP_ENDPOINT` and `SEDIMENT_INGEST_TOKEN` in the
agent's environment, and `SEDIMENT_PI_TRANSCRIPTS=1` for pi.

## Route inference calls through a gateway

If your deployment captures Inference calls, your operator routes agents through
its gateway and distributes the gateway URL and client credential, as described
in [Distribute gateway routing](managed-capture.md#distribute-gateway-routing).
`sediment install` doesn't configure gateway routing, and you don't need a
gateway credential.

## Verify capture

1. Check the machine and repository configuration:

   ```bash
   sediment doctor --fetch /path/to/repo
   ```

   `doctor` prints `ok`, `FAIL`, or `info` for each check and exits `1` on a
   failure. It checks configuration, not delivery.

2. Have an agent edit a file, commit the change, and read the commit's Session
   note:

   ```bash
   git notes --ref=refs/notes/sediment show HEAD
   ```

3. To check delivery, also sign in with an operator token. A capture token
   can't read data:

   ```bash
   sediment login https://sediment-api.example.com
   ```

   Then check the Session with the steps in your agent's guide.

After you push, `git ls-remote origin 'refs/notes/*'` lists
`refs/notes/sediment` when the remote has the note.

## What the installer changes

### Agent hooks

| Agent | Installed integration |
|---|---|
| [Claude Code](agents/claude-code.md) | A `PostToolUse` entry in `~/.claude/settings.json` |
| [Codex](agents/codex.md) | A hook entry in `~/.codex/hooks.json` |
| [Cursor](agents/cursor.md) | `postToolUse`, `postToolUseFailure`, and `afterTabFileEdit` entries in `~/.cursor/hooks.json` |
| [pi](agents/pi.md) | The bundled extension, registered in `~/.pi/agent/settings.json` |

Each supported edit marks the Session in the repository's Git directory.

### Git hooks

The installer adds three marked blocks to the repository's hooks:

- `post-commit` writes the marked Session identifiers to `refs/notes/sediment`.
- `prepare-commit-msg` carries notes through a local squash merge.
- `pre-push` reconciles and pushes the notes ref with the branch.

It appends to existing shell hooks, including husky and `core.hooksPath`
setups, and never replaces them. It leaves a non-shell hook untouched and
prints the command to add by hand. It also sets
`notes.rewriteRef=refs/notes/sediment`, so Git carries notes through
`commit --amend` and rebase.

The hooks never fail a commit or push. They log failures, without content, to
`~/.sediment/attribution.log`, so a successful commit doesn't prove capture.

## Repair or recover capture

### Repair a notes ref

If `doctor --fetch` reports a notes ref that's behind or diverged, reconcile it:

```bash
sediment repair-notes origin
```

Unlike the pre-push hook, this command fails when it can't reconcile.

### Recover a pending stamp

If `~/.sediment/attribution.log` reports `stamp_busy`, a read or write failure,
or interrupted cleanup, compare the recorded commit with the current one:

```bash
tail -n 20 ~/.sediment/attribution.log
git notes --ref=refs/notes/sediment show '<recorded commit SHA>'
git rev-parse HEAD
```

If `HEAD` is still the recorded commit, run `sediment stamp`, and read the note
again. If `HEAD` moved, don't stamp the pending markers onto the newer commit.
[Concurrent marker capture](../explanation/attribution.md#concurrent-marker-capture)
explains the recovery boundary.

Before you upgrade the CLI, pause commits in every worktree of the clone and
recover any pending stamps. Then upgrade every installed copy together, and
restart the agents. An older CLI can erase a newer one's markers.

### Install hooks in an existing clone

A clone that the installer never saw has no git hooks. Run the install command
with your usual flags for it, or use the fleet
[owner allowlist](managed-capture.md#set-the-owner-allowlist).

### Recover transcript pairs

If a Claude Code or pi Session ended without running its extractor, run the
extractor on the saved transcript. Set `--agent` to `claude-code` or `pi`:

```bash
printf '{"session_id":"%s","transcript_path":"%s"}' "$SID" "$FILE" \
  | sediment transcript --agent claude-code
```

The extractor reads the files in their current state, so later edits can lower
the retention score. Rerunning is safe; the database keeps one copy of each Fact.

### Preserve prepared payloads through outages

Without a buffer, an event that a client can't deliver during a server outage
can be lost. To keep payloads on disk and replay them, set
`SEDIMENT_DELIVERY_DIR` to an absolute path on persistent storage, in both the
sender's and the replay worker's environment. Let the sender create the
directory, with mode `0700`. It refuses a symlink, a directory that another
user owns, or a mode other than `0700`, and falls back to a direct send. The
buffer
can hold unredacted prompts, code, or credentials, so use it only with
participants' approval, and on an encrypted volume if you need encryption at
rest.

The buffer covers pi decisions, transcript extraction, and the LiteLLM gateway
callback. It doesn't cover Cursor hooks, native Codex telemetry, or forge
webhooks.

1. Run the replay worker under your process supervisor, as the same user and
   with the same environment as the sender. Skip this step for the LiteLLM
   callback, which runs its own worker:

   ```bash
   sediment delivery replay --watch
   ```

2. Check the buffer:

   ```bash
   sediment delivery status
   ```

   `sediment doctor` also checks the directory and the worker.

If the directory is unsafe, unavailable, or busy, the sender tries one direct
send and logs `best_effort`. Repair the directory, even if that send succeeds.

The worker retries transport errors, HTTP 408, HTTP 429, and server errors,
with backoff up to 60 seconds. Other client errors block an entry. To retry
blocked entries after you fix the token or upgrade the server, stop the worker,
and run:

```bash
sediment delivery replay --retry-blocked
sediment delivery status
```

Each run handles up to 32 entries, so repeat it until `status` shows none
blocked. Then restart the worker.

| Limit | Value |
| --- | --- |
| Pending entries | 2,048 |
| Pending payload bytes | 256 MiB |
| One entry | 8 MiB |
| Replay window | 24 hours from enqueue |
| Content-free receipts | 7 days, up to 2,048 |

When the buffer is full, it declines further payloads and keeps the pending
ones.
Expired content is removed only when a worker or replay command runs. Replay
sends each payload only to its original destination, so changing the endpoint
blocks older entries. Don't reuse one buffer for another deployment.

## Uninstall capture

To remove capture from one repository, run:

```bash
sediment uninstall /path/to/repo
```

To also remove the user-level agent hooks, the environment files, and the
managed Codex telemetry blocks, add `--agents`:

```bash
sediment uninstall /path/to/repo --agents
```

The command removes only Sediment's own entries, and user-level removal affects
every repository. Run it with the CLI installation that registered pi, or
remove the stale entry from `~/.pi/agent/settings.json`. If you set capture
variables yourself, remove them from your shell profiles, and restart the
agents.

If the command reports a skipped Codex profile, remove only Sediment's
telemetry block from that file by hand. Until you do, the profile keeps its
token and keeps sending telemetry. If you use several Codex homes, run
`uninstall --agents` for each one.

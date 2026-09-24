# Configure local capture

Connect one macOS or Linux developer machine to a deployed Sediment API.
If you need an endpoint, start with [Deploy Sediment](../operate/deploy.md) or
the single-machine [Quickstart](../quickstart.md).

Review [Agent integrations](agent-integrations.md) before enrollment. Codex
native decision telemetry can include patch arguments even without transcript
capture. Gateway and transcript capture require separate configuration.

## Prerequisites

You need:

- a macOS or Linux developer machine
- the approved Sediment release version, endpoint URL, and your ingest-only token
- Git and curl on `PATH`
- a git repository
- Claude Code, Codex, Cursor, or pi for agent hooks

Native Windows capture is unsupported. The installer requires POSIX file locks
and shell hooks and refuses Windows installation before changing configuration.

The installer skips an agent whose configuration directory doesn't exist. If
you install an agent later, run the installer again.

## Install the CLI

Replace the version placeholder with the operator's approved release:

```bash
curl -fsSL https://sediment.so/install.sh | \
  sh -s -- --capture-only --version '<approved release version>'
```

The installer supplies Python 3.12 and the CLI without changing host packages.
If it prints a PATH instruction, run it in this terminal and later capture shells.
Verify the installed version:

```bash
sediment --version
```

For a pilot that requires a source revision or pi, use
[Install a pinned checkout](../operate/run-pilot.md#install-a-pinned-checkout)
instead of the package installer.

## Connect the CLI

Obtain a named ingest-only token from the deployment operator, then log in:

```bash
sediment login https://sediment-api.example.com --capture
```

`login --capture` verifies ingest authority and stores the credential in
`~/.sediment/config.json` with mode `0600`. Operator login stays separate.

For unattended setup, send the capture token on standard input:

```bash
printf '%s\n' "$SEDIMENT_INGEST_TOKEN" | \
  env -u SEDIMENT_INGEST_TOKEN sediment login \
    https://sediment-api.example.com --capture --with-token
```

The installer requires verified capture enrollment; it never copies an operator
token into capture configuration. An explicit `SEDIMENT_INGEST_TOKEN` override
also requires verification.

Use an HTTPS deployment root without embedded credentials, a query string, or a
fragment. HTTP is accepted only for `localhost`, `127.0.0.0/8`, or `[::1]`.

## Install capture

Run the installer once for each repository that you want to capture:

```bash
sediment install --user-id '<developer>' /path/to/repo
```

Keep the installed CLI at its original path; hooks reference its absolute path.
For source installs, keep the checkout and Python environment in place.
Reinstall after moving them.

Reinstallation updates Sediment's marked entries and preserves unrelated
configuration. The installer refuses malformed agent JSON files.

## Load the agent environment

Load the generated environment before starting an agent:

```bash
. "$HOME/.sediment/env.sh"
```

The installer writes private shell and fish environment files and updates the
shell profiles that it finds. The environment supplies the capture endpoint,
ingest token, and `user.id` when you pass `--user-id`.

Cursor and pi require `SEDIMENT_OTLP_ENDPOINT`; they don't infer it from
`OTEL_EXPORTER_OTLP_ENDPOINT`. Codex requires its
[telemetry profile](agents/codex.md#configure-developer-decisions).

If another system owns the agent environment, pass `--no-env`. The
[CLI reference](../reference/cli.md#sediment-install) lists every install
flag.

When you rewrite the generated environment, repeat the original `--user-id`,
`--gateway-url`, and `--gateway-key` values that you still need. An ordinary
reinstall preserves an existing generated pi transcript opt-in. It doesn't
preserve omitted identity or gateway arguments.

Follow your [agent guide](agent-integrations.md) to select its telemetry profile
or trust its hooks. Then start the agent from this shell in the enrolled
repository. Fully quit an existing desktop process first; it can retain the
old environment.

[Verify capture](#verify-capture) before adding optional channels.

## Route inference calls through a gateway

If your deployment exposes a large language model (LLM) gateway, add its URL
and client key:

```bash
sediment install \
  --gateway-url https://sediment-llm.example.com \
  --gateway-key "$SEDIMENT_GATEWAY_KEY" \
  --user-id '<developer>' \
  /path/to/repo
. "$HOME/.sediment/env.sh"
```

Restart the agent from this shell before verifying gateway capture.

The installer writes `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and
`SEDIMENT_GATEWAY_KEY` into the agent environment. The upstream provider key
stays on the gateway host.

For the client-side route and its limits, follow
[Capture Claude Code work](agents/claude-code.md#configure-inference-call-capture)
or [Capture Codex work](agents/codex.md#configure-inference-call-capture). For
the server-side callback and gateway setup, follow
[Configure inference-call capture](managed-capture.md#configure-inference-call-capture).

## Opt in to transcript capture

Transcript capture sends applied edit text and Session-end file content.
Claude Code also supports refused edits and Retry linkages. Review the
[per-source payloads](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
before opting in.

Persist these variables in the shell profile or service environment that
starts the agent:

```bash
export SEDIMENT_OTLP_ENDPOINT=https://sediment-api.example.com
export SEDIMENT_INGEST_TOKEN='<ingest-only token>'
```

Use the endpoint rules from [Connect the CLI](#connect-the-cli). Capture clients
reject redirects instead of forwarding credentials.

Add the transcript hooks:

```bash
sediment install --transcripts --no-env /path/to/repo
```

`--no-env` preserves the `--user-id` and gateway settings from the earlier
install. Restart the agent after you persist the variables and install the
hooks.

For pi content capture with `--no-env`, also set
`SEDIMENT_PI_TRANSCRIPTS=1` in the environment that starts pi. Only that exact
value enables extraction. If you let the installer write the environment,
`install --transcripts` supplies the opt-in. An endpoint and token alone don't
enable pi transcript capture.

The installer adds Claude Code and Codex Session-end extractors and Claude
Code's pre-edit snapshot hook. pi extracts at `session_shutdown`.

For a one-task pi host that stays alive, set `SEDIMENT_EXTRACT_ON_SETTLE=1`.
Leave it unset for interactive pi: repeated extraction can retain an early file
state through first-write-wins deduplication.

Run `sediment doctor /path/to/repo` to check hooks. For supported observations,
see the [Claude Code](agents/claude-code.md#configure-edit-observations),
[Codex](agents/codex.md#configure-edit-observations), and
[pi](agent-integrations.md#pi) guides.

## Verify capture

On the machine that runs remote checks, log in with a separate operator token:

```bash
sediment login https://sediment-api.example.com
```

Capture enrollment doesn't authorize these reads.

Check the machine and repository configuration:

```bash
sediment doctor --fetch /path/to/repo
```

`doctor` reports `ok`, `FAIL`, or `info` and exits `1` on a failed check.
Uninstalled agents and unset endpoints report `info`. Installed but unhooked
agents and rejected endpoints report `FAIL`. These checks verify configuration;
use a Session check to verify delivery.

After an agent edit, commit the change. From the repository, inspect its note
and push:

```bash
git notes --ref=refs/notes/sediment show HEAD
git push
git ls-remote origin 'refs/notes/*'
```

The first command shows the local Session note. The final command lists
`refs/notes/sediment` when the remote has the note.

After the exporter flushes, check the deployment:

```bash
sediment facts
```

After a supported edit-tool call, `developer_decisions` grows. If you enabled
transcript capture, `edit_observations`, `rejected_edits`, and
`retry_linkages` can grow after the Session ends.

For Cursor, Codex, or pi, verify the actual Session from its local commit note:

```bash
sediment doctor /path/to/repo --agent pi --session-id '<Session identifier>'
```

Choose the matching harness. The command requires its Developer decision in
that Session and its Session/tool entry in the local `HEAD` note. Add
`--transcripts` to require an Edit observation and the relevant opt-in or hook.
Add `--inference-calls` for a gateway-routed Session. Cursor rejects both
unsupported requirements. Missing, incomplete, or unreadable evidence fails
verification; organization-wide Fact counts don't substitute for it. Follow
[Run a Cursor, pi, and Codex pilot](../operate/run-pilot.md) for the three
complete edit-to-commit checks.

## Installed hooks

### Agent hooks

The installer configures the agents that it finds on the machine:

| Agent | Installed integration |
|---|---|
| [Claude Code](agents/claude-code.md) | A `PostToolUse` entry in `~/.claude/settings.json` |
| [Codex](agents/codex.md) | A hook entry in `~/.codex/hooks.json` |
| [Cursor](agents/cursor.md) | Native `postToolUse`, `postToolUseFailure`, and `afterTabFileEdit` entries in `~/.cursor/hooks.json` |
| pi | The extension under `shims/pi/`, when you run the installer from a checkout |

Supported edits mark the Session in the repository's Git directory. For pi,
run the installer from a source checkout: the installed CLI package doesn't
contain `shims/pi/`. See [pi setup](agent-integrations.md#pi).

### Git hooks

The installer adds three marked blocks to the repository's hook directory:

- `post-commit` writes the collected Session identifiers to
  `refs/notes/sediment`.
- `prepare-commit-msg` carries notes through a local squash merge.
- `pre-push` reconciles and pushes the notes ref with the branch push.

The installer appends to existing shell hooks, including husky and
`core.hooksPath` setups. It never replaces the script. A non-shell hook is left
untouched with an instruction to wire the command by hand.

The hooks are best-effort. They can't fail a commit or push. Marker, stamp,
reconciliation, and notes-push failures write content-free diagnostics to
`~/.sediment/attribution.log`. A zero hook exit code doesn't prove capture.

The installer also sets `notes.rewriteRef=refs/notes/sediment`. Git then carries
the note through `commit --amend` and rebase.

## Repair or recover capture

### Recover a pending stamp

If a stamp reports `stamp_busy`, a read/write failure, or interrupted cleanup,
inspect its recorded SHA and the note before retrying:

```bash
tail -n 20 ~/.sediment/attribution.log
git notes --ref=refs/notes/sediment show '<recorded commit SHA>'
git rev-parse HEAD
```

If HEAD still names that commit and its pending markers belong to it, run
`sediment stamp`, then inspect the note again. If HEAD moved, don't stamp those
markers onto the later commit without determining which commit owns the evidence.
An interrupted process can leave markers that its note already contains.
Doctor reports pending generations and historical failures as `info`; live
note and hook checks determine failure status. See
[Concurrent marker capture](../explanation/attribution.md#concurrent-marker-capture)
for the recovery boundary.

Before upgrading marker clients, pause capture and commit hooks in every linked
worktree of the clone. Reconcile pending markers against their commit notes.
Update every installed and standalone helper that writes markers or notes, then
restart harness processes and run the concurrency check. Generation-free marker
files remain readable. Concurrent old and generation-aware writers aren't
supported: an old stamper can bypass the locks and erase the marker file.

### Repair a notes ref

If `doctor --fetch` reports a behind or diverged notes ref, reconcile it:

```bash
sediment repair-notes origin
```

Unlike the pre-push hook, this command returns a failure when reconciliation
doesn't complete.

### Reinstall after moving a checkout

Hooks installed from a checkout contain that checkout's absolute path. If you
move or delete it, run `sediment install` again from the current location.

For a package installation, reinstall capture if the CLI executable moves.

### Install hooks in an existing clone

A clone that the installer never saw has no git hooks. Run `sediment install`
for that clone. For organization-owned fleets, use the owner allowlist in
[Roll out managed capture](managed-capture.md#set-the-owner-allowlist).

### Preserve prepared payloads through outages

If you authorize durable local storage of prepared capture payloads, set
`SEDIMENT_DELIVERY_DIR` in both the sender and its replay worker environment.
This opt-in can store unredacted prompts, code, or credentials before server
redaction. It doesn't enable transcript capture: pi still requires
`SEDIMENT_PI_TRANSCRIPTS=1`, and transcript capture still requires an explicit
`SEDIMENT_OTLP_ENDPOINT`.

Use an absolute directory on persistent storage, owned by the sender's user.
The helper creates its directory with mode `0700` and files with mode `0600`.
It refuses symlinks, foreign ownership, and permissive existing paths. If you
require encryption at rest, provide an encrypted volume; the helper doesn't
encrypt payloads. Transport credentials aren't copied into buffer records.

If private storage is unsafe, unavailable, or busy, the callback, transcript
client, and pi Decision sender attempt one direct send of the same prepared
payload. Diagnostics name the storage reason and `best_effort` mode. This
fallback has no durable recovery guarantee; repair storage even if delivery
succeeds. Capacity, size, and identity declines don't trigger direct delivery.
Whitespace-only `SEDIMENT_DELIVERY_DIR` disables buffering.

`sediment delivery enqueue` requires durable storage unless the caller selects
`--fallback-direct`. A direct acknowledgment doesn't prove durable enqueue or
stored Fact counts.

Run the worker under your existing process supervisor, using the same user,
directory, endpoint, and active bearer-token environment as the sender:

```bash
sediment delivery replay --watch
```

Pi requests a bounded drain at startup. That request doesn't replace the
supervised worker. `sediment doctor` checks storage and worker presence when
buffering is enabled. Setting an environment variable alone doesn't enroll a
worker. The embedded LiteLLM callback owns its worker separately.

Inspect content-free counts and replay a bounded batch:

```bash
sediment delivery status
sediment delivery replay
```

One worker owns each directory. Each batch attempts at most 32 requests with a
five-second timeout. Transport errors, HTTP 408, HTTP 429, and server errors
retry with backoff capped at 60 seconds. Other client errors block retries.
`worker_busy: true` means another worker owns the directory.

After correcting the token or server compatibility, stop the supervised worker.
Use the same user, directory, endpoint, and corrected credential environment to
retry blocked entries deliberately:

```bash
sediment delivery replay --retry-blocked
sediment delivery status
```

Each replay handles at most 32 entries. If blocked entries remain, correct their
reported failure and repeat. Restart the supervised worker to resume automatic
delivery. For the embedded gateway, use the container procedure in
[Enable bundled LiteLLM](../operate/deploy.md#enable-bundled-litellm).

| Limit | Value |
| --- | --- |
| Active entries | 2,048 |
| Active payload bytes | 256 MiB |
| One entry | 8 MiB |
| Replay window | 24 hours from enqueue |
| Content-free terminal receipts | Seven days, at most 2,048 entries |

The buffer declines excess payloads without evicting pending entries. Maintenance
removes expired content only when a worker or replay command runs; a stopped host
can retain content beyond 24 hours.

Replay sends the original bytes with active credentials only to the original
configured destination. An endpoint change blocks old entries. Gateway replay
also rechecks `SEDIMENT_GATEWAY_LOCAL_HTTP_ORIGIN` before loading credentials.
Removing that exception blocks queued plaintext delivery and retains its prepared
payload until recovery or expiry. Restore an approved exact origin only on the
trusted local network, then deliberately retry blocked entries. Don't reuse a
buffer for another deployment or organization at the same URL. A gateway
acknowledgment names the retained Fact or a declared skip. An OTLP acknowledgment
proves delivery only; use receiver receipts or a Session query to prove capture.

Transcript capture splits records into requests within the 8 MiB limit. One
oversized record counts as `record_too_large`; other records can still publish.
If `emission_summary` reports `outcome: partial`, inspect unsuccessful,
unsubmitted, and oversized-record counts. Acknowledged records aren't stored
Fact counts.

Queued requests retain their original bytes. Replay doesn't reread edited files.
Capture clears line-hash snapshots after all eligible records are queued or
acknowledged, or when a readable transcript has no eligible records. Failed
reads, oversized records, and publication failures retain snapshots. Snapshots
cannot reconstruct unsubmitted observations: a crash before enqueue can lose
them, and later extraction can observe different file content.

The buffer covers enrolled pi decisions and transcript paths, with separate
gateway enrollment. Cursor hooks, native Codex telemetry, and forge webhooks
have separate recovery requirements.

### Recover transcript pairs

If a Claude Code Session ended without running the extractor, select the
Claude Code parser against the saved transcript:

```bash
printf '{"session_id":"%s","transcript_path":"%s"}' "$SID" "$FILE" \
  | sediment transcript --agent claude-code
```

For a pi Session, select the pi parser:

```bash
printf '{"session_id":"%s","transcript_path":"%s"}' "$SID" "$FILE" \
  | sediment transcript --agent pi
```

In a source checkout, `python3 scripts/sediment_transcript.py` remains a
standard-library-only compatibility entry point for all parsers and the
`snapshot` subcommand.

The extractor reads files as they exist when recovery runs. Later edits can
therefore lower the measured edit retention score. Re-running is safe because
the database deduplicates each Session and call ID.

## Uninstall capture

Remove the integration from one repository:

```bash
sediment uninstall /path/to/repo
```

Remove the repository integration and user-level agent entries:

```bash
sediment uninstall /path/to/repo --agents
```

If you installed the pi extension from a Sediment checkout, run the command
from that same checkout so the CLI can match its absolute path:

```bash
uv run sediment uninstall /path/to/repo --agents
```

If that checkout moved or no longer exists, remove its absolute `shims/pi`
entry from `~/.pi/agent/settings.json`.

If you persisted transcript or pi settings manually, remove
`SEDIMENT_OTLP_ENDPOINT`, `SEDIMENT_INGEST_TOKEN`, `SEDIMENT_PI_TRANSCRIPTS`, and
any `SEDIMENT_EXTRACT_ON_SETTLE` setting from shell profiles or service
environments. Restart the affected agents.

The command removes only Sediment-owned hook blocks, configuration values,
environment files, agent entries, and managed Codex telemetry blocks. It checks
every `*.config.toml` in the active Codex home (`CODEX_HOME`, or `~/.codex`),
preserves unrelated settings and comments, and deletes profiles that become
empty. User-level removal affects every repository using those agent hooks.

If a profile is malformed, unreadable, or a symbolic link, the command reports
the skipped file and returns a nonzero status. Inspect that file and remove
only Sediment's managed telemetry block before restarting Codex. Until you
resolve a skipped file, its credential and telemetry configuration can remain.
If you use several Codex homes, repeat `uninstall --agents` for each home.

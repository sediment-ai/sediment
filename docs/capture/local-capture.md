# Configure local capture

Use this guide to connect one developer machine and its repositories to an
existing Sediment endpoint. If you need an endpoint, complete the
[Quickstart](../quickstart.md) or [Deploy Sediment](../operate/deploy.md).

Before you connect the machine, migrate the deployment's PostgreSQL schema to
the supported revision. The API verifies the revision at startup but never
runs migrations.

This setup captures Developer decisions and commit Attribution. You can also
route inference calls through a gateway and opt in to transcript-derived edit
survival.

Codex native decision telemetry can include patch/tool-argument content even
without transcript capture. Review its
[Developer decision payload](agents/codex.md#configure-developer-decisions)
before enabling a Codex telemetry profile.

Use [Agent integrations](agent-integrations.md) to compare evidence. After you
choose an agent, follow [Capture Claude Code work](agents/claude-code.md),
[Capture Codex work](agents/codex.md), or
[Capture Cursor work](agents/cursor.md) for its setup and limits.

## Prerequisites

You need:

- a macOS or Linux developer machine
- the Sediment CLI
- the URL and an ingest-only token for a Sediment endpoint
- git and Python 3.12 on `PATH`
- a git repository
- Claude Code, Codex, Cursor, or pi for agent hooks

Native Windows capture is unsupported. The capture clients require POSIX file
locks and shell hooks. On Windows, `sediment install` refuses installation
before changing configuration. Run capture on macOS or Linux.

The installer skips an agent whose configuration directory doesn't exist. If
you install an agent later, run the installer again.

## Connect the CLI

Enroll the capture credential before you install capture. Obtain a named
ingest-only token from your deployment operator, then run:

```bash
sediment login https://sediment-api.example.com --capture
```

`login --capture` verifies ingest-only authority and stores the credential and
enrollment proof in `~/.sediment/config.json` with mode `0600`. It preserves
operator login separately. For unattended setup, send the capture token on
standard input:

```bash
printf '%s\n' "$SEDIMENT_INGEST_TOKEN" | \
  env -u SEDIMENT_INGEST_TOKEN sediment login \
    https://sediment-api.example.com --capture --with-token
```

An older profile containing one unclassified token needs capture enrollment
before installation. An explicit `SEDIMENT_INGEST_TOKEN` override must also
pass the API authority check. The installer never copies an operator credential
into capture configuration.

Use HTTPS for a remote deployment. HTTP is accepted only for `localhost`, an
address in `127.0.0.0/8`, or `[::1]`. The URL must be a deployment root with no
embedded credentials, query string, or fragment. The CLI rejects an unsafe URL
before it reads or sends the bearer token.

## Install capture

Run the installer once for each repository that you want to capture:

```bash
sediment install --user-id <developer> /path/to/repo
```

Restart active agent Sessions after installation. Agents read their hooks and
environment at startup.

The installer embeds the invoked `sediment` executable's absolute path in
hooks. If you installed from source, keep that checkout and its Python
environment in place until you uninstall capture. A different `sediment`
earlier on `PATH` doesn't replace the executable that you explicitly invoke.

Re-running the command is safe. The installer updates its own marked entries
without duplicating them, preserves unrelated configuration, and refuses to
overwrite an agent configuration file that doesn't parse as JSON.

### Agent hooks

The installer configures the agents that it finds on the machine:

| Agent | Installed integration |
|---|---|
| [Claude Code](agents/claude-code.md) | A `PostToolUse` entry in `~/.claude/settings.json` |
| [Codex](agents/codex.md) | A hook entry in `~/.codex/hooks.json` |
| [Cursor](agents/cursor.md) | Native `postToolUse`, `postToolUseFailure`, and `afterTabFileEdit` entries in `~/.cursor/hooks.json` |
| pi | The extension under `shims/pi/`, when you run the installer from a checkout |

The Claude Code and Codex hooks call `sediment mark`. Cursor hooks call the
packaged `sediment cursor-hook` adapter, which invokes the same marker. The pi
extension invokes the marker through pi's extension API. Each supported edit
adds or refreshes a Session marker generation in the repository's Git directory.

An installed CLI doesn't contain `shims/pi/`. For pi, run
`uv run sediment install /path/to/repo` from a Sediment checkout so it can
register the extension path. [Agent integrations](agent-integrations.md#pi)
covers the rest of pi setup.

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

### Agent environment

The installer derives the telemetry environment from verified capture enrollment.
It writes `~/.sediment/env.sh`, a fish `conf.d` file, and marked source lines in
the shell profiles that it finds.

The environment enables OTLP/JSON logs, points them at the Sediment endpoint,
adds the ingest-only token as both the standard header and
`SEDIMENT_INGEST_TOKEN`, and stamps `user.id` when you pass `--user-id`.

The installer also writes the explicit `SEDIMENT_OTLP_ENDPOINT` from your
verified capture enrollment. Load `~/.sediment/env.sh` before starting Cursor
or pi.
Those clients don't infer an endpoint from `OTEL_EXPORTER_OTLP_ENDPOINT`.
The dedicated endpoint and token enable decisions; pi content additionally
requires `SEDIMENT_PI_TRANSCRIPTS=1`. Follow
[Capture Cursor work](agents/cursor.md#configure-developer-decisions)
or [Agent integrations](agent-integrations.md#pi) for the agent-specific step.

If another system owns the agent environment, pass `--no-env`. The
[CLI reference](../reference/cli.md#sediment-install) lists every install
flag.

When you rewrite the generated environment, repeat the original `--user-id`,
`--gateway-url`, and `--gateway-key` values that you still need. An ordinary
reinstall preserves an existing generated pi transcript opt-in. It doesn't
preserve omitted identity or gateway arguments.

## Route inference calls through a gateway

If your deployment exposes an LLM gateway, add its URL and client key:

```bash
sediment install \
  --gateway-url https://sediment-llm.example.com \
  --gateway-key "$SEDIMENT_GATEWAY_KEY" \
  --user-id <developer> \
  /path/to/repo
```

The installer writes `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and
`SEDIMENT_GATEWAY_KEY` into the agent environment. The upstream provider key
stays on the gateway host.

For the client-side route and its limits, follow
[Capture Claude Code work](agents/claude-code.md#configure-inference-call-capture)
or [Capture Codex work](agents/codex.md#configure-inference-call-capture). For
the server-side callback and gateway setup, follow
[Configure inference-call capture](managed-capture.md#configure-inference-call-capture).

## Opt in to transcript capture

Transcript capture sends applied edit pairs, refused edits, Retry linkages, and
the Session-end file state. It is a different privacy class from Attribution
markers, so the installer leaves it off unless you opt in.

Persist these variables in the shell profile or service environment that
starts the agent:

```bash
export SEDIMENT_OTLP_ENDPOINT=https://sediment-api.example.com
export SEDIMENT_INGEST_TOKEN=<ingest-only token>
```

Use HTTPS for a remote endpoint. Plain HTTP works only with literal
`localhost`, an address in `127.0.0.0/8`, or `[::1]`. The capture clients reject
redirects rather than forwarding a bearer token. If the API is loopback-bound,
[Expose a public HTTPS endpoint](../operate/deploy.md#3-expose-a-public-https-endpoint)
shows the `cloudflared` tunnel and TLS configuration.

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

The command adds the available Claude Code and Codex `SessionEnd` extractors.
It also adds Claude Code's `PreToolUse` snapshot hook. Run
`sediment doctor /path/to/repo` to verify the hook entries. The
[Claude Code guide](agents/claude-code.md#configure-edit-observations) and
[Codex guide](agents/codex.md#configure-edit-observations) define their
supported observations and missing signals.

With its content opt-in enabled, the pi extension invokes the extractor at
`session_shutdown`. If a one-task pi host keeps the process alive, set
`SEDIMENT_EXTRACT_ON_SETTLE=1` so extraction
runs at `agent_settled`.

Leave that variable unset for interactive pi. An interactive Session settles
more than once, and first-write-wins deduplication would preserve an early file
state instead of the final one.

The pi transcript hooks also require the variables in
[Agent environment](#agent-environment). They don't fall back to
`OTEL_EXPORTER_OTLP_ENDPOINT`, which prevents export to an unrelated collector.
[Agent integrations](agent-integrations.md#pi) covers the complete pi
procedure.

[How capture works](../explanation/how-capture-works.md#edit-retention-and-external-deltas)
explains the payload, external-delta measurement, and privacy boundary.

## Verify capture

Remote Fact and Session checks require an operator credential. On the machine
that runs those checks, an authorized operator logs in separately with
`sediment login https://sediment-api.example.com`. Capture enrollment remains
separate and cannot authorize these reads.

Check the machine and repository configuration:

```bash
sediment doctor --fetch /path/to/repo
```

`doctor` reports `ok`, `FAIL`, or `info` for each agent hook, git hook, notes
configuration, remote notes state, and local Attribution error. It exits `1`
when any check fails.

An agent that isn't installed reports `info`. An installed but unhooked agent
reports `FAIL`. The pi registration is checkable only from a Sediment checkout.
An unset capture endpoint reports `info`; a rejected endpoint reports `FAIL`.
These default checks don't prove that an agent delivered evidence.

After an agent edit, commit and push from the repository:

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

Hooks written by an installed CLI invoke the executable's stable path and don't
have this failure mode.

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

The `sediment delivery enqueue` command stays strictly durable by default.
Pi passes `--fallback-direct` to select the shared fallback behavior. A returned
`acknowledged` disposition with `fallback_reason` means direct delivery, never
durable enqueue. An OTLP acknowledgment still doesn't establish Fact counts.

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

One worker owns each directory and sends at most 32 requests per batch. Each
request has a five-second timeout and refuses redirects. Transport errors,
HTTP 408, HTTP 429, and server errors retry with jittered backoff capped at
60 seconds. Other client errors block automatic retry. `worker_busy: true` means
that another worker owns the directory and this command makes no attempt.

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

The buffer accepts at most 2,048 active entries, 256 MiB of payloads, and
8 MiB per entry. Full or unwritable storage declines the incoming payload
visibly; it never evicts another pending entry to make room. The replay window
is 24 hours from enqueue. A worker or maintenance pass removes expired content;
a stopped host can't enforce a wall-clock deletion deadline. Content-free
terminal receipts remain for seven days, capped at 2,048 entries.

Replay sends the original bytes with active credentials only to the original
configured destination. An endpoint change blocks old entries. Gateway replay
also rechecks `SEDIMENT_GATEWAY_LOCAL_HTTP_ORIGIN` before loading credentials.
Removing that exception blocks queued plaintext delivery and retains its prepared
payload until recovery or expiry. Restore an approved exact origin only on the
trusted local network, then deliberately retry blocked entries. Don't reuse a
buffer for another deployment or organization at the same URL. A gateway
acknowledgment names the retained Fact or a declared skip. An OTLP acknowledgment
proves delivery only; use receiver receipts or a Session query to prove capture.

Transcript capture packs its independent records into requests that each fit
the 8 MiB entry limit. Each request has its own durable enqueue or delivery
acknowledgment. A Session can therefore publish only part of its records.
An individually oversized record counts as `record_too_large`; other records
remain eligible for delivery.

If the `emission_summary` diagnostic reports `outcome: partial`, inspect its
`unsuccessful_requests`, `unsuccessful_records`, `record_too_large`, and
`unsubmitted_requests`/`unsubmitted_records` counts. Unsuccessful requests count
by returned `pending`, `blocked`, or `declined` status, or a closed exception
reason. An exception counts the attempted request separately from the
unsubmitted suffix. `candidate_records` counts each Fact kind; queued and
acknowledged counts distinguish requests from records. Acknowledged records
aren't a stored-Fact count.

After every eligible record belongs to a queued or acknowledged request,
transcript capture clears its line-hash snapshots. Any oversized record or
publication failure retains the snapshots. A readable transcript with no
eligible observations also clears them. Failed reads retain them.
Already queued requests remain available to the worker after partial publication.
Replay sends their original bytes without reading the edited files again.
Snapshots cannot reconstruct unsubmitted observations. A crash before durable
enqueue can lose those observations; later extraction can observe different
file contents. Without buffer enrollment, delivery remains best-effort.
Cursor hooks, native Codex telemetry, and forge webhooks retain their separate
delivery and acceptance requirements.

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

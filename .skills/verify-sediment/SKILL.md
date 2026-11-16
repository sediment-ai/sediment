---
name: verify-sediment
description: Drive Sediment the way a user does — local CLI + API server — and capture proof. Use when verifying user-facing behavior, proving a change works end to end, or before claiming a quickstart/CLI/ingest path is fixed.
---

# Verify Sediment

Sediment's primary user surface is the `sediment` CLI and the local API that
`sediment server` starts. There is no product web UI. Drive the real CLI and
HTTP doors; do not call library setters as a stand-in for ingest.

Repository skills live under `.skills/`. Cursor also discovers this skill at
`.cursor/skills/verify-sediment/` (symlink). Read `features/` before choosing
what to drive.

## Launch

From the checkout root, start an isolated local server (disposable `HOME`,
free loopback port, managed PostgreSQL under that home):

```bash
eval "$(.skills/verify-sediment/bin/launch)"
```

`launch` prints `export` lines; eval them so later helpers see `SEDIMENT_VERIFY_*`.
It:

1. Creates `/tmp/sediment-verify-<run-id>/` with a private `HOME`.
2. Picks an unused `127.0.0.1` port (never assumes 8000).
3. Runs `uv run sediment server --host 127.0.0.1 --port <port>` with that
   `HOME`, stripping inherited `SEDIMENT_*` so a checkout `.env` cannot leak.
4. Waits until `GET /health` returns HTTP 200 and JSON `"status":"ok"`.
5. Writes instance state under the run directory and names an evidence
   directory that cleanup will not delete.

**Ready** means `/health` returns `{"status":"ok","version":"<semver>"}` and
`<home>/.sediment/server/server.env` exists with operator and ingest tokens.
A listening port alone is not ready: first launch downloads PostgreSQL and
provisions the database before the API serves.

**Teardown:** `.skills/verify-sediment/bin/cleanup` (see Cleanup).

**Side-by-side:** yes — each run uses its own `HOME` and port. Refuse to drive
the shared default at `~/.sediment/server` or whatever already owns port 8000
unless this run started it. `server.lock` flock blocks a second server on the
same root.

## Doctor

```bash
.skills/verify-sediment/bin/doctor
```

Read-only. Requires a launched run's env (`eval` the launch exports, or pass
`SEDIMENT_VERIFY_RUN_DIR`). Passes when:

- the recorded server PID is alive
- `GET $SEDIMENT_VERIFY_URL/health` is 200 with `"status":"ok"`
- the response `version` is non-empty
- `$SEDIMENT_VERIFY_HOME/.sediment/server/server.env` exists and is mode `0600`

Exit non-zero on any miss. Run doctor first whenever anything looks off.

## Drive

Prefer the CLI under the verify `HOME` (credentials and config stay isolated):

```bash
.skills/verify-sediment/bin/cli -- login "$SEDIMENT_VERIFY_URL"
.skills/verify-sediment/bin/cli -- demo
.skills/verify-sediment/bin/cli -- facts
```

HTTP against the same instance (tokens from `server.env`):

```bash
curl -sf "$SEDIMENT_VERIFY_URL/health"
curl -sf -H "Authorization: Bearer <operator-token>" \
  "$SEDIMENT_VERIFY_URL/v1/facts"
```

Existing repo harnesses that already exercise this path (reuse their
expectations; do not reinvent):

- `cli/tests/test_e2e_quickstart.py` — server → login → install → OTLP → facts
- `cli/tests/test_installed_wheel.py` — managed PostgreSQL server + login + demo
- `scripts/smoke.py` — HTTP ingest doors against a running deployment

Feature recipes live in `features/`. Drive one mapped feature per proof unless
the task names more. Start from Launch + Doctor, then follow that feature file.

## Evidence

Write proof under `$SEDIMENT_VERIFY_EVIDENCE` (set by `launch`; default
`/tmp/sediment-verify-evidence/<run-id>/`). Cleanup must leave this tree intact.

Capture, for every proof:

| Kind | What |
|---|---|
| Action | Exact command(s) and exit code (transcript) |
| HTTP | Request path, status, response body when relevant |
| Resulting state | Follow-up user-facing read (`facts`, `/health`, reopen) |
| Side effects | Fact counts, files under the verify `HOME`, git note content |

Proof standards:

- Exercise the real user path (CLI verbs and documented HTTP doors), not
  internal FactStore writers or test-only fixtures injected past ingest.
- Capture the action and the resulting state, not only the final line.
- Verify side effects (`sediment facts`, session fact tables, files written).
- Mocks only at real production boundaries (upstream LLM providers, remote
  forges). Local PostgreSQL and the API are in-process for verification.
- If a path is dry-run (for example `quarantine-inference-calls` without
  `--apply`, or `mirror-gc` without `--apply`), observe that nothing was
  written — do not trust the flag name alone.

## Cleanup

```bash
.skills/verify-sediment/bin/cleanup
```

Stops only the server PID recorded for this run (SIGTERM, then SIGKILL if
needed). Removes `/tmp/sediment-verify-<run-id>/` instance data. Does **not**
delete `$SEDIMENT_VERIFY_EVIDENCE`. Never kill by process name; never touch a
server this run did not start (including a developer or environment server on
port 8000).

## Helpers

All scripts are executable. Invoke from the checkout root.

| Script | Purpose |
|---|---|
| `.skills/verify-sediment/bin/launch` | Start isolated server; print `export` lines |
| `.skills/verify-sediment/bin/doctor` | Read-only health check for the active run |
| `.skills/verify-sediment/bin/cli -- <args>` | `sediment` with the run's `HOME` and cleaned env |
| `.skills/verify-sediment/bin/cleanup` | Tear down the run; keep evidence |

After Launch:

```bash
eval "$(.skills/verify-sediment/bin/launch)"
.skills/verify-sediment/bin/doctor
.skills/verify-sediment/bin/cli -- login "$SEDIMENT_VERIFY_URL"
# …drive a feature from features/…
.skills/verify-sediment/bin/cleanup
test -d "$SEDIMENT_VERIFY_EVIDENCE"
```

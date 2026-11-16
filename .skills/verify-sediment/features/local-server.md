# Local server

Local server starts a loopback API with managed PostgreSQL, writes credentials
under the server root, and answers `/health` when the instance is usable.

## Sub-features

- `server-start` boots PostgreSQL, provisions roles, migrates, and serves the API.
- `server-health` returns `status=ok` and a package version.
- `server-credentials` writes mode-`600` `server.env` with operator and ingest tokens.
- `server-stop` releases the API and managed database on SIGTERM.

## How to get to it (user POV)

- Run `sediment server` (default port 8000, root `~/.sediment/server`).
- Run `sediment server --port <port> --root <dir>` for a non-default bind or root.
- For verification, run `.skills/verify-sediment/bin/launch` (scratch `HOME` + free port).

## Driving it with verify-sediment

Preconditions:

- No other process holds the chosen port.
- `uv sync --locked` has been run in this checkout so `uv run sediment` works.
- Host libraries for managed PostgreSQL are present (`libpq5` and friends on Debian/Ubuntu).

- **Launch.** Start an isolated server. Run `eval "$(.skills/verify-sediment/bin/launch)"`. Stderr ends with `ready http://127.0.0.1:<port> (pid <n>)` and stdout emits `SEDIMENT_VERIFY_*` exports.
- **Health.** Probe the API. Run `curl -sf "$SEDIMENT_VERIFY_URL/health"`. Body is JSON with `"status":"ok"` and a non-empty `"version"`.
- **Doctor.** Confirm the instance. Run `.skills/verify-sediment/bin/doctor`. Exit code `0` and lines for pid, health, and credentials mode `600`.
- **Proof.** Save health and doctor output under `$SEDIMENT_VERIFY_EVIDENCE/local-server/`. Require the health body and doctor transcript both name `status=ok`.

## Gotchas

- First launch under a new `HOME` downloads PostgreSQL binaries; allow up to a few minutes before declaring failure.
- `/health` can stay down while migrations run. Wait for JSON `status=ok`, not only a listening socket.
- A second `sediment server` on the same root fails with `already running` (`server.lock`). Use a new verify run instead of sharing roots.
- Do not drive the environment or developer server on port 8000 unless this run started it.

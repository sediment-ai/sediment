# Facts

Facts shows per-table Fact counts for the logged-in org (remote HTTP) so an
operator can confirm capture landed.

## Sub-features

- `facts-remote` reads counts through the logged-in server.
- `facts-session` reads counts for one session id when requested by URL or follow-on tools.
- `facts-empty` shows zeros before any ingest on a fresh server.

## How to get to it (user POV)

- Run `sediment facts` after `sediment login`.
- Call `GET /v1/facts` with the operator bearer token.

## Driving it with verify-sediment

Preconditions:

- Launch, Doctor, and Login have passed for this run.

- **Empty baseline (optional).** On a fresh run before demo, run `.skills/verify-sediment/bin/cli -- facts`. Exit code `0`. Totals may be zero.
- **After ingest.** After `demo` (see [Demo ingest](./demo-ingest.md)), run `.skills/verify-sediment/bin/cli -- facts`. Exit code `0`. `inference_calls` and `developer_decisions` totals are at least `1`.
- **HTTP entry.** Read the same counts over HTTP. Load `SEDIMENT_OPERATOR_TOKEN` from `$SEDIMENT_VERIFY_SERVER_ENV` and run `curl -sf -H "Authorization: Bearer $token" "$SEDIMENT_VERIFY_URL/v1/facts"`. Status `200`; JSON includes `tables`.
- **Proof.** Save CLI and HTTP bodies under `$SEDIMENT_VERIFY_EVIDENCE/facts/`. Require matching non-zero inference and decision totals after demo.

## Gotchas

- Without login, remote `facts` fails auth; fix login before retrying.
- Direct `--database-url` mode bypasses the HTTP door — do not use it as proof of the user remote path.
- Quarantined Facts affect visible totals; a quarantine journey must state that precondition.

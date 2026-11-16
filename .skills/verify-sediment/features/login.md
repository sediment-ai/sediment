# Login

Login stores verified operator credentials for a Sediment server URL, and on
loopback also enrolls the ingest credential `sediment server` generated locally.

## Sub-features

- `login-loopback` enrolls operator and ingest tokens from local `server.env`.
- `login-with-token` reads a bearer token from stdin for unattended or remote use.
- `login-capture` enrolls an ingest credential without replacing operator login.
- `logout` removes stored credentials from the user config.

## How to get to it (user POV)

- Run `sediment login http://127.0.0.1:8000` after `sediment server`.
- Run `sediment login <url> --with-token` and pipe the token on stdin.
- Run `sediment login <url> --capture` for an ingest-only enrollment.
- Run `sediment logout` to clear stored credentials.

## Driving it with verify-sediment

Preconditions:

- Launch and Doctor have passed for this run.
- The verify `HOME` owns the server (default root under that home) so loopback
  login can read `$HOME/.sediment/server/server.env`.

- **Loopback login.** Enroll credentials. Run `.skills/verify-sediment/bin/cli -- login "$SEDIMENT_VERIFY_URL"`. Exit code `0`. Stdout includes `logged in to` and the verify URL.
- **Confirm config.** Read the stored client config. Run `test -f "$SEDIMENT_VERIFY_HOME/.sediment/config.json"` and keep a copy under `$SEDIMENT_VERIFY_EVIDENCE/login/config.json` (redact token values before sharing if needed).
- **Unattended path.** Log out, then log in with a piped operator token. Run `.skills/verify-sediment/bin/cli -- logout`, then `grep '^SEDIMENT_OPERATOR_TOKEN=' "$SEDIMENT_VERIFY_SERVER_ENV" | cut -d= -f2- | .skills/verify-sediment/bin/cli -- login --with-token "$SEDIMENT_VERIFY_URL"`. Exit code `0`.
- **Proof.** Save the login transcript under `$SEDIMENT_VERIFY_EVIDENCE/login/login.txt`. A later `demo` or `facts` call must authenticate without prompting.

## Gotchas

- Auto-pickup of `server.env` tokens searches only `$HOME/.sediment/server/server.env`. A custom `server --root` outside that path needs `--with-token`.
- Login writes `$HOME/.sediment/config.json`. Always use the verify `HOME`; never point login at a shared home while proving isolation.
- Remote (non-loopback) URLs require HTTPS and an explicit token.

# Quickstart

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
{"status":"ok","version":"0.2.0"}
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

## 4. Install capture in a scratch repository

Use an unused directory for the scratch repository:

```bash
test ! -e "$HOME/sediment-quickstart" || exit 1
git init -q "$HOME/sediment-quickstart"
cd "$HOME/sediment-quickstart"
sediment install .
```

`install` adds repository git hooks, user-level hooks for detected agents, and
an environment file. The CLI package includes the pi extension; pi requires
its own [runtime setup](capture/agent-integrations.md#pi).

Verify:

```bash
sediment doctor . && echo "doctor exit 0"
```

After the configuration checks, a successful run ends with:

```text
doctor exit 0
```

Rows depend on the installed agents. Resolve any `FAIL` result before continuing.

## 5. Verify the Git hooks

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

Require `session_id` to be `quickstart`; your timestamp differs. This local note
doesn't add a server Fact.

## 6. Verify server capture

`demo` posts one synthetic inference call and one Developer decision through
the same ingest routes that clients use. It then prints the Fact counts:

```bash
sediment demo
```

Require `inference_calls` and `developer_decisions` to each have at least one
row. A successful run ends with:

```text
These are synthetic facts, not your agent's. They prove the ingest path works end to end.
```

Repeating the demo retains the same Facts through database deduplication.

## Clean up the demo

```bash
cd "$HOME"
sediment uninstall "$HOME/sediment-quickstart" && rm -rf "$HOME/sediment-quickstart"
```

That leaves the local server's data. To remove it, press Ctrl+C in the server
terminal. Then run this command, which deletes the local database, credentials,
logs, downloaded PostgreSQL binaries, and mirror:

```bash
rm -rf ~/.sediment/server
```

## Next steps

- [Configure your agent](capture/agent-integrations.md) to capture real work.
  Follow its install, environment, restart, and Session verification steps.
- [Deploy Sediment](operate/deploy.md) to enroll a team on a shared host.
  The [PostgreSQL setup](operate/deploy.md#configure-postgresql) covers Compose's
  generated credentials, database connections, and persistent volume.

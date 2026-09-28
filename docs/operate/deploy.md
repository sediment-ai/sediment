# Deploy Sediment on your own host

Use this page to run Sediment for a team on a host where you already manage
PostgreSQL, HTTPS, and a process supervisor. It installs the published
Sediment package.

- To start from an empty cloud instance, follow
  [Deploy Sediment on EC2](deploy-ec2.md). It supplies PostgreSQL, HTTPS, and
  the supervisor.
- To try Sediment on one machine first, follow the [Quickstart](../quickstart.md).

## Before you begin

You need the following:

- A Debian or Ubuntu host, or macOS with Homebrew, with at least 4 CPU cores
  and 8 GB of memory
- Git, `curl`, and OpenSSL
- A dedicated operating-system account to run Sediment
- A dedicated PostgreSQL 17 database on a private network, and a superuser
  connection to it
- A reverse proxy that serves a stable HTTPS hostname
- Persistent storage for the account's home directory

## Install Sediment

As the account that runs Sediment, install a
[published release](https://github.com/sediment-ai/sediment/releases). Replace
the example version with the release that you want. Optional: check the
release first with [Verify a release](security.md#verify-a-release).

```bash
SEDIMENT_VERSION=0.3.0
SEDIMENT_INSTALLER="$(mktemp)" &&
  curl -fsSL https://sediment.so/install.sh -o "$SEDIMENT_INSTALLER" &&
  UV_NO_BUILD=1 sh "$SEDIMENT_INSTALLER" --method uv --version "$SEDIMENT_VERSION" &&
  rm "$SEDIMENT_INSTALLER"
```

If the installer prints an instruction to update `PATH`, follow it. Then check
the installed version:

```bash
sediment --version
```

The command prints the version that you installed.

If a coding agent helps you operate this deployment, give it the output of
`sediment guide` for guidance that matches the installed release.

## Connect PostgreSQL

Sediment needs a PostgreSQL superuser (`rolsuper=true`) connection to create
its own database roles. Managed database services that don't grant superuser
access can't provision Sediment. Don't point Sediment at a database that
another application uses.

In the server account's private environment, set the following variables.
Replace the organization, the connection, and the Git hosts that Sediment may
clone from:

```bash
export SEDIMENT_ORG_ID=acme
export SEDIMENT_BOOTSTRAP_DATABASE_URL='postgresql+psycopg://bootstrap:<password>@db.internal:5432/sediment'
export SEDIMENT_ALLOWED_CLONE_HOSTS='["github.com"]'
export SEDIMENT_DEV_MODE=false
```

Keep this environment private. Your supervisor must supply it on every start.

## Start the API

1. Start the server:

   ```bash
   sediment server --host 127.0.0.1 --port 8000
   ```

   On start, Sediment creates separate migrator, runtime, and operator
   database roles and applies migrations. The API runs with the runtime role
   only.

2. In a second terminal, check readiness:

   ```bash
   curl -fsS http://127.0.0.1:8000/health
   ```

   The response shows `"status":"ok"` and the installed version.

3. Stop the server. Configure your process supervisor to run the same command
   as the same account, with the same private environment. Set it to restart
   Sediment after a failure and after a host restart.

Sediment writes its generated API tokens and database-role passwords to
`~/.sediment/server/server.env`, and its Git mirrors to
`~/.sediment/server/mirror`. Keep `~/.sediment/server` on persistent storage.

## Expose HTTPS

1. Configure your reverse proxy to forward your HTTPS hostname to
   `http://127.0.0.1:8000`. Preserve request bodies and `Authorization`
   headers, and rate-limit authentication attempts. The API doesn't rate-limit
   them.
2. From another machine, check the public endpoint:

   ```bash
   curl -fsS https://sediment.example.com/health
   ```

   The response matches the local check.

## Set up an operator shell

Reports, Derivations, exports, and quarantine commands read PostgreSQL
directly through the `sediment_operator` role.

1. On the server, as the server account, set the following variables. Replace
   the organization and the database host:

   ```bash
   export SEDIMENT_ORG_ID=acme
   export SEDIMENT_MIRROR_PATH="$HOME/.sediment/server/mirror"
   OPERATOR_PASSWORD="$(sed -n 's/^SEDIMENT_OPERATOR_PASSWORD=//p' ~/.sediment/server/server.env)"
   export SEDIMENT_DATABASE_URL="postgresql+psycopg://sediment_operator:$OPERATOR_PASSWORD@db.internal:5432/sediment"
   unset OPERATOR_PASSWORD
   ```

2. Check the schema:

   ```bash
   sediment db status
   ```

   The output shows `at_head`.

3. Sign in to the API with the operator token:

   ```bash
   sediment login http://127.0.0.1:8000
   sediment facts
   ```

   On loopback, `sediment login` reads the operator token from
   `~/.sediment/server/server.env`. `sediment facts` prints zero counts until
   the first capture arrives.

A health check doesn't prove capture. The next page verifies it end to end.

## Next steps

1. [Enroll your team](run-pilot.md).
2. Before you rely on the data, set up backups with
   [Maintain a deployment](maintain.md#back-up-and-restore).

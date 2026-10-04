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
- A dedicated, empty PostgreSQL 17 database on a private network, and either
  an administrator connection to it or a database administrator who creates
  Sediment's roles for you. The administrator is a superuser, or a role with
  `CREATEROLE` that owns the database.
- A reverse proxy that serves a stable HTTPS hostname
- Persistent storage for the account's home directory

## Install Sediment

As the account that runs Sediment, install a
[published release](https://github.com/sediment-ai/sediment/releases). Replace
the example version with the release that you want. Optional: check the
release first with [Verify a release](security.md#verify-a-release).

```bash
SEDIMENT_VERSION=0.6.0
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

## Create the database roles

Sediment uses three PostgreSQL roles: `sediment_migrator` owns the schema and
migrates it, `sediment_runtime` serves the API, and `sediment_operator` runs
operator commands. The API can't change or delete a Fact. Don't point Sediment
at a database that another application uses.

Sediment doesn't claim support for a managed PostgreSQL service, such as Amazon
RDS, until the service passes the
[qualification in ADR 0027](../adr/0027-postgresql-without-superuser.md#qualification-before-compatibility-claims).

1. Optional: If your organization names the roles itself, export
   `SEDIMENT_MIGRATOR_ROLE`, `SEDIMENT_RUNTIME_ROLE`, and
   `SEDIMENT_OPERATOR_ROLE`. Set them in the server's environment too, and use
   those names in place of `sediment_migrator`, `sediment_runtime`, and
   `sediment_operator` in every URL on this page. Two deployments with
   different role names and databases can share one PostgreSQL instance.
2. As the server account, generate the three role passwords into a private
   file. The command refuses to overwrite an existing file, which holds the
   only copy of the passwords:

   ```bash
   (
     umask 077
     mkdir -p ~/.sediment
     set -o noclobber
     cat > ~/.sediment/database-roles.env <<EOF_ROLES
   SEDIMENT_MIGRATOR_PASSWORD=$(openssl rand -hex 32)
   SEDIMENT_RUNTIME_PASSWORD=$(openssl rand -hex 32)
   SEDIMENT_OPERATOR_PASSWORD=$(openssl rand -hex 32)
   EOF_ROLES
   )
   ```

3. Create the roles in one of two ways:

   - If you hold the administrator connection, let Sediment create the roles.
     Enter the administrator URL at the prompt, for example
     `postgresql+psycopg://admin:<password>@db.internal:5432/sediment`.
     Percent-encode any reserved URL character in the password. The URL
     reaches only this command:

     ```bash
     (
       set -a; . ~/.sediment/database-roles.env; set +a
       read -rs -p 'Administrator URL: ' SEDIMENT_BOOTSTRAP_DATABASE_URL; echo
       export SEDIMENT_BOOTSTRAP_DATABASE_URL
       sediment db provision
     )
     ```

     The command prints `database roles provisioned and schema upgraded`. If
     the administrator lacks a capability, the command names it and the
     statement that fixes it, and changes nothing.

   - If a database administrator creates the roles, print the SQL for them:

     ```bash
     sediment db provision --print-sql --database sediment > sediment-roles.sql
     ```

     The administrator runs `sediment-roles.sql` connected to the `sediment`
     database, then sets each role's password to the value in
     `~/.sediment/database-roles.env`. `sediment-roles.sql` contains no
     password.

## Configure the server

In the server account's private environment, set the following variables.
Replace the organization, the database host, the two passwords from
`~/.sediment/database-roles.env`, and the Git hosts that Sediment may clone
from. Percent-encode any reserved URL character in a password:

```bash
export SEDIMENT_ORG_ID=acme
export SEDIMENT_MIGRATOR_DATABASE_URL='postgresql+psycopg://sediment_migrator:<migrator password>@db.internal:5432/sediment'
export SEDIMENT_DATABASE_URL='postgresql+psycopg://sediment_runtime:<runtime password>@db.internal:5432/sediment'
export SEDIMENT_ALLOWED_CLONE_HOSTS='["github.com"]'
export SEDIMENT_DEV_MODE=false
```

Keep this environment private. Your supervisor must supply it on every start.
The administrator URL never belongs here. A database URL in these variables
takes only TLS options, such as `sslmode` and `sslrootcert`.

Check the database before the first start. The URL goes through the
environment, so it stays out of process listings:

```bash
SEDIMENT_DATABASE_URL="$SEDIMENT_MIGRATOR_DATABASE_URL" sediment db check
```

The check prints `passed`. Otherwise, it lists each failed check with the
statement that fixes it. Before the first start, the schema is `absent` after
the administrator's SQL, or `at_head` after `sediment db provision`.

## Start the API

1. Start the server:

   ```bash
   sediment server --host 127.0.0.1 --port 8000
   ```

   On each start, Sediment migrates the database as `sediment_migrator`,
   applies the table grants, and checks all three roles. It then removes the
   migrator credential from its environment before the API and its workers
   start, and serves with the runtime role only. Replicas that start together
   wait up to two minutes for one migration.

2. In a second terminal, check readiness:

   ```bash
   curl -fsS http://127.0.0.1:8000/health
   ```

   The response shows `"status":"ok"` and the installed version.

3. Stop the server. Configure your process supervisor to run the same command
   as the same account, with the same private environment. Set it to restart
   Sediment after a failure and after a host restart.

Sediment writes its generated API tokens to `~/.sediment/server/server.env`,
and its Git mirrors to `~/.sediment/server/mirror`. Keep `~/.sediment` on
persistent storage.

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
   OPERATOR_PASSWORD="$(sed -n 's/^SEDIMENT_OPERATOR_PASSWORD=//p' ~/.sediment/database-roles.env)"
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

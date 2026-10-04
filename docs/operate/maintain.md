# Maintain a deployment

Use this page to upgrade, back up, and repair a Sediment deployment, and to
remove data from it. It applies to both
[Deploy Sediment on EC2](deploy-ec2.md) and
[Deploy Sediment on your own host](deploy.md).

Where a step says to stop, start, or restart Sediment, use your process
supervisor. On EC2, use the commands in
[Manage the services](deploy-ec2.md#manage-the-services).

Run `sediment db`, `sediment facts`, and quarantine commands from the operator
shell. [Set up an operator shell](deploy-ec2.md#set-up-an-operator-shell)
shows it for EC2, and [the same section](deploy.md#set-up-an-operator-shell)
for your own host.

## Upgrade Sediment

Upgrade the server before developer machines.

1. Read the release's entry in the [changelog](../../CHANGELOG.md).
   Optional: check the release with [Verify a release](security.md#verify-a-release).
2. [Back up the database](#back-up-and-restore). If the release changes the
   schema, test the restore, and time the migration on the restored copy.
   Index builds can block reads and writes, so schedule a maintenance window
   for a large database.
3. Ask developers to end active agent Sessions.
4. Stop Sediment.
5. Rerun the install command from your deploy page with the target version.
   Keep `~/.sediment/server`.
6. If the server's environment sets `SEDIMENT_BOOTSTRAP_DATABASE_URL`,
   [move off the bootstrap URL](#move-off-the-bootstrap-url) first. Then start
   Sediment. On start, it applies any migrations. After a migration, don't
   start an older version against the database.
7. Check `/health`, and check that `sediment db status` shows `at_head` and
   `sediment facts` shows the earlier counts.
8. On each developer machine, stop any sender replay worker. Then rerun the
   capture install command from
   [Enroll your team](run-pilot.md#install-sediment-on-each-machine) with the
   same version, rerun `sediment install` with the same options, and restart
   the worker.

If a `sediment` command warns that the server version differs from the client,
run the `uv tool install` command in the warning. It installs the server's exact
release, which upgrades or downgrades the CLI as needed. A release candidate's
command also pins the other five Sediment distributions. If you installed with
the installer's `pipx` or `pip` method, rerun the installer with `--method pipx`
or `--method pip` and the server's version. The `pipx` method forces a reinstall
so that an existing CLI upgrades or downgrades to the requested version.

### Move off the bootstrap URL

Releases before ADR 0027 provisioned the database on every start with
`SEDIMENT_BOOTSTRAP_DATABASE_URL`. The server now refuses that variable, so
that the administrator credential never reaches it. It needs the migrator and
runtime URLs instead. With Sediment stopped and the new version installed:

1. Copy the three role passwords that the server generated into a private
   file. On EC2, run this from `~/sediment-deploy`; on your own host, from
   `~/.sediment`:

   ```bash
   (
     umask 077
     set -o noclobber
     grep -E '^SEDIMENT_(MIGRATOR|RUNTIME|OPERATOR)_PASSWORD=' \
       ~/.sediment/server/server.env > database-roles.env
   )
   ```

2. Provision once with the administrator. On EC2, run step 5 of
   [Configure PostgreSQL and Traefik](deploy-ec2.md#configure-postgresql-and-traefik).
   On your own host, run step 3 of
   [Create the database roles](deploy.md#create-the-database-roles).
3. In the server's private environment, replace `SEDIMENT_BOOTSTRAP_DATABASE_URL`
   with `SEDIMENT_MIGRATOR_DATABASE_URL` and `SEDIMENT_DATABASE_URL`. On EC2,
   run this from `~/sediment-deploy`:

   ```bash
   (
     set -eu
     umask 077
     . ./database-roles.env
     grep -v '^SEDIMENT_BOOTSTRAP_DATABASE_URL=' server.env > server.env.new
     cat >> server.env.new <<EOF_URLS
   SEDIMENT_MIGRATOR_DATABASE_URL=postgresql+psycopg://sediment_migrator:$SEDIMENT_MIGRATOR_PASSWORD@127.0.0.1:5432/sediment
   SEDIMENT_DATABASE_URL=postgresql+psycopg://sediment_runtime:$SEDIMENT_RUNTIME_PASSWORD@127.0.0.1:5432/sediment
   EOF_URLS
     mv server.env.new server.env
   )
   ```

   On your own host, set the two variables as in
   [Configure the server](deploy.md#configure-the-server).

The operator shell reads the operator password from `database-roles.env`.

## Rotate credentials

`~/.sediment/server/server.env` holds the generated API credentials, and the
server's private environment holds the database URLs. Stop Sediment before you
edit either, keep `server.env` at mode `0600`, and start Sediment after you
save it.

| Credential | To rotate it |
| --- | --- |
| A developer's capture token | Replace that entry in `SEDIMENT_INGEST_TOKENS`. The developer reruns `sediment login --capture` and `sediment install`, and restarts the agents. |
| `SEDIMENT_OPERATOR_TOKEN` | Replace the value. Rerun `sediment login` for each operator. |

If `server.env` contains `SEDIMENT_API_BEARER_TOKEN`, the server generated it
as a shared capture token before any named tokens existed. After every client
uses a named token, remove that line.

To revoke a developer, remove their entry from `SEDIMENT_INGEST_TOKENS`.

To rotate a database role password:

1. Stop Sediment.
2. Change the password in one of two ways:
   - If you hold the administrator connection, replace the value in
     `database-roles.env`. Then rerun the provisioning command: step 5 of
     [Configure PostgreSQL and Traefik](deploy-ec2.md#configure-postgresql-and-traefik)
     on EC2, or step 3 of
     [Create the database roles](deploy.md#create-the-database-roles) on your
     own host.
   - If you don't, connect with psql as that role, and run `\password`. Each
     role can change its own password. Record the new value in
     `database-roles.env`, so that a later provisioning keeps it.
3. Update `SEDIMENT_MIGRATOR_DATABASE_URL` or `SEDIMENT_DATABASE_URL` in the
   server's environment, and your operator shell.
4. Start Sediment.

## Back up and restore

A backup has two parts: a `pg_dump` of the database, and a copy of the
files that hold Sediment's credentials, configuration, and Git mirrors. A copy
of a live PostgreSQL data directory isn't a consistent backup.

On EC2, encrypt both parts with [age](https://github.com/FiloSottile/age):

1. On a separate machine, generate an age identity. Keep its private key off
   the server.
2. On the server, install `age`, and set `BACKUP_RECIPIENT` to the identity's
   public recipient.
3. From `~/sediment-deploy`, run the backup:

   ```bash
   (
     set -euo pipefail
     umask 077
     install -d -m 700 "$HOME/sediment-backups"
     stamp="$(date -u +%Y%m%dT%H%M%SZ)"
     dump="$HOME/sediment-backups/sediment-$stamp.dump.age"
     files="$HOME/sediment-backups/sediment-$stamp.files.tar.age"
     set -o noclobber
     docker compose exec -T postgres sh -ec \
       'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump -U sediment -d sediment --format=custom' \
       | age --recipient "${BACKUP_RECIPIENT:?Set the recovery public recipient}" > "$dump"
     tar -C "$HOME" --exclude=sediment-deploy/certificates \
       -cf - .sediment/server sediment-deploy \
       | age --recipient "$BACKUP_RECIPIENT" > "$files"
     test -s "$dump" && test -s "$files"
     ls -l "$dump" "$files"
   )
   ```

   The files archive holds `~/.sediment/server` and `~/sediment-deploy`.
   Traefik obtains a new certificate on its own, so the archive skips
   `certificates/`.

4. Copy both encrypted files off the server. If the command fails, delete the
   partial files and rerun it. `tar` fails when a mirror changes while it
   reads it.

On other hosts, use your database's backup procedure, such as a managed
service's snapshots, and back up `~/.sediment` and the server's private
environment with it. For a logical backup, run `pg_dump --format=custom` as
`sediment_migrator`, which can read every Sediment object. The operator role
can't dump the database.

To rebuild on a new host, extract the files archive into the operator
account's home directory. Create the empty database and apply the output of
`sediment db provision --print-sql` as its administrator, with the role
passwords from `database-roles.env`. `sediment db provision` can't prepare a
restore target, because it also creates the schema. Then restore the dump as
`sediment_migrator` with the `pg_restore` options in the following restore
test, start Sediment, and point your hostname at the new host. Sediment reuses
the same tokens, role passwords, and webhook secret, so developers and webhooks
don't need to enroll again.

Test a restore before you rely on the data, and after each upgrade that
changes the schema. On EC2, run `psql` and `pg_restore` inside the database
container, with `docker compose exec -it postgres`, and copy the dump in with
`docker compose cp`.

1. As the database administrator, create a separate, empty database. Don't
   touch the original.
2. Give the copy its own roles, so that the live roles gain no access to it.
   Set `SEDIMENT_MIGRATOR_ROLE`, `SEDIMENT_RUNTIME_ROLE`, and
   `SEDIMENT_OPERATOR_ROLE` to test names, such as `restore_migrator`, run
   `sediment db provision --print-sql --database <copy>`, and apply the output
   as the administrator. Set a password for each test role.
3. As the test migrator, restore the dump with
   [`pg_restore`](https://www.postgresql.org/docs/17/app-pgrestore.html)
   `--exit-on-error --no-owner --no-privileges`. The migrator owns every
   restored object.
4. With the same Sediment version and the test role names, set
   `SEDIMENT_MIGRATOR_DATABASE_URL` to the copy as the test migrator and run
   `sediment db upgrade`. It prints `grants applied and roles validated`.
5. Point an operator shell at the copy as the test operator. Check that
   `sediment db status` shows `at_head`, and compare `sediment facts` and
   `sediment quarantine-log` with the original.
6. Record how long recovery took. Then drop the test database and the test
   roles.

## Monitor the deployment

Facts and mirrors never expire. Watch these signals:

- Service restarts and API errors in the supervisor log
- Free space for PostgreSQL and `~/.sediment/server`. Alert before storage
  fills. The mirror worker needs 1 GiB free, but doesn't enforce a quota.
- Backup results
- Fact growth and model outcomes: `sediment facts` and `sediment report model`

## Quarantine captured data

Quarantine hides Facts from Derivations, reports, and exports without deleting
them. An append-only log records each quarantine and release. Quarantine any
Fact that contains a leaked credential, and rotate that credential. Basic
redaction catches common credential shapes, not every secret.

1. Preview the Inference calls to quarantine:

   ```bash
   sediment quarantine-inference-calls --session-id '<Session identifier>' \
     --reason '<reason>'
   ```

2. Review the selection. Then run the same command with `--apply`.
3. To review the audit trail, run `sediment quarantine-log`.

To quarantine or release one Fact of any kind, use `sediment quarantine` and
`sediment release`. See the [CLI reference](../reference/cli.md#sediment-quarantine).

If you quarantine only some Edit observations for a file in a Session, leave
that file and Session out of external-change comparisons. Recomputing doesn't
restore the missing windows. See
[External edit windows](../explanation/how-capture-works.md#external-edit-windows).

## Troubleshoot

| Symptom | Action |
| --- | --- |
| Installation fails | Check access to the package index and the host-library prerequisites. |
| The API doesn't start | Read the supervisor log. Check database reachability, then run `SEDIMENT_DATABASE_URL="$SEDIMENT_MIGRATOR_DATABASE_URL" sediment db check`, which names each failed role or grant check and its fix. |
| Ingest returns `503 database_unavailable` | Restore database access. Clients with a sender buffer replay their payloads. Sediment can't recover an event that a client never retained. |
| `/health` works, but no capture arrives | Run the agent's Session check in [Verify capture](run-pilot.md#verify-capture). Check webhook deliveries and the API log for mirror errors. |

For problems on a developer machine, see
[Repair or recover capture](../capture/local-capture.md#repair-or-recover-capture).

## Tear down the deployment

**Warning:** Deleting the database permanently removes its Facts and quarantine
history. Keep any backups and exports that you need first.

1. On each developer machine,
   [uninstall capture](../capture/local-capture.md#uninstall-capture).
2. [Remove managed capture](../capture/managed-capture.md#remove-managed-capture):
   webhooks, gateway callbacks, and fleet hooks.
3. Stop and disable Sediment in its supervisor, and remove its HTTPS route.
4. Delete the database. On EC2, run `docker compose down --volumes` from
   `~/sediment-deploy`.
5. Delete `~/.sediment`, exports, and any backups that your retention
   policy doesn't require. On your own host, also drop the three database
   roles.

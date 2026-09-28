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
6. Start Sediment. On start, it applies any migrations. After a migration,
   don't start an older version against the database.
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
the installer's `pipx` or `pip` method, rerun that method with the server's
version instead.

## Rotate credentials

`~/.sediment/server/server.env` holds every generated credential. Stop Sediment
before you edit it, keep its mode at `0600`, and start Sediment after you save
it.

| Credential | To rotate it |
| --- | --- |
| A developer's capture token | Replace that entry in `SEDIMENT_INGEST_TOKENS`. The developer reruns `sediment login --capture` and `sediment install`, and restarts the agents. |
| `SEDIMENT_OPERATOR_TOKEN` | Replace the value. Rerun `sediment login` for each operator. |
| A database role password | Replace `SEDIMENT_MIGRATOR_PASSWORD`, `SEDIMENT_RUNTIME_PASSWORD`, or `SEDIMENT_OPERATOR_PASSWORD`. Startup applies it. Update your operator shell. |

If `server.env` contains `SEDIMENT_API_BEARER_TOKEN`, the server generated it
as a shared capture token before any named tokens existed. After every client
uses a named token, remove that line.

To revoke a developer, remove their entry from `SEDIMENT_INGEST_TOKENS`.

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

On other hosts, use your database's backup procedure, and back up
`~/.sediment/server` and the server's private environment with it.

To rebuild on a new host, extract the files archive into the operator
account's home directory, and restore the dump into the empty database with the
`pg_restore` options in the following restore test. Then start Sediment, and
point your hostname at the new host. Sediment reuses the same tokens, role
passwords, and webhook secret, so developers and webhooks don't need to enroll
again.

Test a restore before you rely on the data, and after each upgrade that
changes the schema:

1. Create a separate, empty database. Don't touch the original.
2. Restore the dump with
   [`pg_restore`](https://www.postgresql.org/docs/17/app-pgrestore.html)
   `--exit-on-error --no-owner --no-privileges`.
3. With the same Sediment version, set `SEDIMENT_BOOTSTRAP_DATABASE_URL` to the
   restored database and the three role passwords from `server.env`. Then run
   `sediment db provision`.
4. Point an operator shell at the restored database. Check that
   `sediment db status` shows `at_head`, and compare `sediment facts` and
   `sediment quarantine-log` with the original.
5. Record how long recovery took, then drop the test database.

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
| The API doesn't start | Read the supervisor log. Check database reachability and the superuser bootstrap connection. |
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
5. Delete `~/.sediment/server`, exports, and any backups that your retention
   policy doesn't require.

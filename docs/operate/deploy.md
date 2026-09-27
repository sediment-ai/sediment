# Deploy Sediment

Use this page to run Sediment for a team on a host where you already manage
PostgreSQL and HTTPS. It installs the published Sediment package. You provide
the database, the HTTPS endpoint, and the process supervisor. The sections on
enrollment, capture, verification, upgrades, and data handling apply to every
deployment.

- To deploy on one Amazon Elastic Compute Cloud (EC2) instance with PostgreSQL
  and Traefik, follow [Deploy Sediment on EC2 with Traefik](deploy-ec2.md).
- For a one-machine evaluation, follow the [Quickstart](../quickstart.md).

## 1. Prerequisites

You need the following:

- A supported Debian or Ubuntu host, or macOS with Homebrew
- Git, `curl`, OpenSSL, and permission to install the host libraries
- A dedicated PostgreSQL 17 instance and database, with a PostgreSQL superuser
  connection for role provisioning and migrations
- A stable HTTPS endpoint, private persistent storage, and encrypted backups

Use a dedicated operating-system account for Sediment. Keep the database on a
private network. Start with at least 4 CPU cores and 8 GB of memory. Then check
capacity for your workload with [Validate a deployment](validate-deployment.md).

## 2. Deploy the API

As the account that runs Sediment, install the approved
[published release](https://github.com/sediment-ai/sediment/releases). Replace
the example version with the release that you selected:

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

### Configure PostgreSQL

1. Create a dedicated PostgreSQL 17 instance and database. Don't point Sediment
   at a database that another application uses.
2. Make the bootstrap connection a PostgreSQL superuser (`rolsuper=true`). Role
   provisioning rejects non-superuser administrators, including managed database
   services that don't provide superuser access.
3. In the server account's private environment, set the following variables.
   Replace the example organization, database connection, and permitted Git
   hosts:

   ```bash
   export SEDIMENT_ORG_ID=acme
   export SEDIMENT_BOOTSTRAP_DATABASE_URL='postgresql+psycopg://bootstrap:<password>@db.internal:5432/sediment'
   export SEDIMENT_ALLOWED_CLONE_HOSTS='["github.com"]'
   export SEDIMENT_DEV_MODE=false
   ```

   Use the connection security settings that your database operator requires.

Keep this environment private. Supply it again on every server restart.

### Start the API

1. Start the installed server:

   ```bash
   sediment server --host 127.0.0.1 --port 8000
   ```

2. In a second terminal, check readiness:

   ```bash
   curl -fsS http://127.0.0.1:8000/health
   ```

   Confirm that the response shows `status: "ok"` and the installed release
   version.

3. Run the same server command under your process supervisor with the same
   account, data directory, and private environment.
4. Configure the supervisor to restart Sediment after a failure and after a
   host restart.
5. Record the supervisor's start, stop, restart, and log commands.
6. Before enrollment, apply process memory limits and storage quotas.

When you set `SEDIMENT_BOOTSTRAP_DATABASE_URL`, Sediment uses that database. It
provisions separate migrator, runtime, and operator roles. Then it starts the
API with runtime authority. The API doesn't receive the bootstrap credential.

Sediment stores generated API tokens and database-role passwords in the private
file `~/.sediment/server/server.env`. Its Git mirrors live under
`~/.sediment/server/mirror`. Keep `~/.sediment/server` on persistent storage.

An external PostgreSQL service has its own lifecycle. Stopping Sediment doesn't
stop that database.

### Enroll named capture clients

1. Stop Sediment through the supervisor.
2. Generate a separate token for each developer machine or gateway:

   ```bash
   openssl rand -hex 32
   ```

3. In a private editor, add `SEDIMENT_INGEST_TOKENS` to
   `~/.sediment/server/server.env`. Its value is a JSON object that maps client
   names to tokens. Give each client a unique name, such as `alice-laptop`. The
   names `operator`, `legacy`, and `retrieval` are reserved. Keep the file mode
   at `0600`.
4. Restart Sediment.
5. Through your credential channel, give each client only its own token.

Reserve `SEDIMENT_OPERATOR_TOKEN` for queries and reports. Don't share the
configuration file or any database credential with agents.

### Run a second local deployment

For another shared deployment, use a separate database instance,
operating-system account, and API port. For an isolated local evaluation, select
a separate data root and port:
`sediment server --root /absolute/private/path --port 8001`. Don't reuse another
deployment's credentials or organization configuration.

## 3. Expose a public HTTPS endpoint

1. Configure your reverse proxy to forward the HTTPS API hostname to
   `http://127.0.0.1:8000`.
2. Configure the proxy to rate-limit authentication attempts and to preserve
   webhook request bodies. Set request-size and timeout limits that permit the
   enabled capture paths.
3. Keep PostgreSQL private, and restrict administrative access.
4. From a developer machine, check the public endpoint:

   ```bash
   curl -fsS https://sediment-api.example.com/health
   ```

   Confirm that the response shows the same status and version as the local
   check.

A health response alone doesn't verify credentials, Git access, or webhook
delivery.

## 4. Configure capture

1. Enroll developers with
   [Run a Cursor, pi, and Codex pilot](run-pilot.md) or
   [Configure local capture](../capture/local-capture.md).
2. For private repository mirrors and signed forge webhooks, follow
   [Roll out managed capture](../capture/managed-capture.md).
3. Before you expand the deployment, verify a Session from each participating
   agent and a real push.

### Connect a gateway

The published package doesn't include a deployable LiteLLM gateway. Run your
gateway separately. Connect an integration that sends the capture envelope that
[`POST /ingest/gateway`](../reference/api.md#post-ingestgateway) accepts.

Keep provider credentials at the gateway. Give developers the gateway URL and a
client credential. Don't override the model that each developer chooses. Before
you rely on gateway reports, verify a captured Inference call.

### Enable agent-requested retrieval

If your client implements the retrieval API, grant it access to specific
Sessions:

1. In the server's private process environment, set `SEDIMENT_RETRIEVAL_TOKEN`
   to a separate secret.
2. Set exactly one source setting: `SEDIMENT_RETRIEVAL_SESSION_ID` or
   `SEDIMENT_RETRIEVAL_SESSION_IDS`. The plural setting takes a JSON array of
   1–32 specific Session identifiers and must fit in 16 KiB. Empty values are
   invalid.
3. Restart Sediment.

After you change the grant, rotate its token, and then restart Sediment. To
revoke access, remove the token and the source setting.

The grant includes future Facts in the authorized Sessions. Keep operator and
database credentials out of the agent environment. For pi, meet the release and
runtime requirements in the
[pi section of Agent integrations](../capture/agent-integrations.md#pi). Then
follow [Enable agent-requested retrieval](resume-with-evidence.md#enable-agent-requested-retrieval).
Operators can also prepare a private evidence packet with the installed CLI.

## 5. Verify the deployment

1. From the server account's terminal, sign in and list Facts:

   ```bash
   sediment login http://127.0.0.1:8000
   sediment facts
   ```

   A loopback `sediment login` reads the default server's private credential
   file. Remote operators use the HTTPS URL and their operator token. Capture
   clients enroll separately with `sediment login <url> --capture`.

2. For database-local reports, Derivations, exports, and quarantine operations,
   configure a separate operator shell. Set `SEDIMENT_ORG_ID`,
   `SEDIMENT_MIRROR_PATH`, and a `SEDIMENT_DATABASE_URL` for the
   `sediment_operator` role. The server's private credential file holds that
   role's password.
3. In the operator shell, check the schema status:

   ```bash
   sediment db status
   ```

   Confirm that the output shows `at_head`.

4. Run the pilot capture checks in
   [Run a Cursor, pi, and Codex pilot](run-pilot.md).
5. Restart the service through its supervisor. Then verify health, retained
   Facts, and capture again.
6. Before you increase scope, complete
   [Validate a deployment](validate-deployment.md).

## 6. Upgrade the deployment

Upgrade the API before capture clients and gateway integrations.

1. Review the release notes and the release's evidence with
   [Check release and deployment security](security.md).
2. Before a schema-changing upgrade, back up the database and test restoration.
3. End active capture Sessions.
4. Stop the API through its supervisor.
5. As the same server account, install the selected published version:

   ```bash
   SEDIMENT_VERSION='<target release version>'
   SEDIMENT_INSTALLER="$(mktemp)" &&
     curl -fsSL https://sediment.so/install.sh -o "$SEDIMENT_INSTALLER" &&
     UV_NO_BUILD=1 sh "$SEDIMENT_INSTALLER" --method uv --version "$SEDIMENT_VERSION" &&
     rm "$SEDIMENT_INSTALLER"
   ```

   Preserve the server data directory and credentials.

6. Restart Sediment through the supervisor. Startup provisions roles and
   applies migrations to the configured database. After the migration, don't
   run an older server against the upgraded schema.
7. Before you resume clients, verify health, `sediment db status`, and retained
   capture.
8. On each client, install the approved release. Repeat capture enrollment with
   the same identifier and approved options.

To rotate an ingest token:

1. Stop the API.
2. Replace that client's entry in `SEDIMENT_INGEST_TOKENS`.
3. Restart the API.
4. Enroll the client again.

After all legacy clients move to named tokens, remove `SEDIMENT_API_BEARER_TOKEN`
from the saved credential file and the process environment.

Rotate `SEDIMENT_OPERATOR_TOKEN` separately from ingest tokens. Then run the
operator `sediment login` again. Coordinate database-role password changes with
the database operator and every process that uses those credentials. Preserve
the existing database and mirror.

## 7. Monitor the deployment

In the operator shell, inspect Fact growth and reports:

```bash
sediment facts
sediment report model
```

Monitor service restarts, capture failures, database availability, storage use,
and backup results. Apply release and host security updates within your
maintenance deadlines. Record the exact installed versions with each report.

### Back up and restore

Use your database operator's encrypted backup procedure. Include a consistent
PostgreSQL backup, the private server configuration, and the mirrors that
historical analysis needs. Keep decryption keys off the deployment host. A copy
of a live PostgreSQL data directory isn't a consistent backup.

Before enrollment and after schema changes, test a restore:

1. Restore into a separate, empty database. Keep the original deployment intact.
2. Install the same Sediment release, and provision roles.
3. Verify schema status, Fact counts, quarantine history, and a representative
   export.
4. Record the backup timestamp, the restore result, and the recovery duration.

## 8. Privacy and data handling

### 8.1 What Sediment stores

Facts can contain model inputs and outputs, patch arguments, applied text, and
observed file content. Mirrors contain Git history. Git notes contain Session
identifiers and timestamps. Before enrollment, agree on the capture boundaries
in [Privacy boundaries and ceilings](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings).

Basic redaction isn't comprehensive secret detection. If Sediment captures a
credential, quarantine the Facts that contain it, and rotate the credential.
Raw buffers, mirrors, and backups remain sensitive.

### 8.2 Where data lives

| Location | Contents |
| --- | --- |
| Configured PostgreSQL database | Facts, schema, and quarantine audit history |
| `~/.sediment/server/server.env` | Private server credentials |
| `~/.sediment/server/mirror` | Git mirrors |
| Operator-selected output and staging directories | Exports and temporary Derivation data |
| Opted-in sender delivery directories | Prepared capture payloads awaiting delivery |

Facts and mirrors don't expire automatically. Set storage quotas and retention
procedures. Configure an alert that fires before storage fills. The mirror
worker's free-space check doesn't enforce a quota. Restrict administrative
access to the host and the database.

### 8.3 Quarantine and wholesale deletion

Quarantine excludes Facts from Derivations and exports without changing their
rows. To quarantine Inference calls from the operator shell:

1. Preview the quarantine:

   ```bash
   sediment quarantine-inference-calls --session-id '<Session identifier>' \
     --reason '<reason>'
   ```

2. Review the selection. Then run the same command with `--apply`.
3. To inspect the audit trail, run `sediment quarantine-log`.

For single-Fact quarantine and release commands, see the
[CLI reference](../reference/cli.md).

If you quarantine only some Edit observations for a file and Session, exclude
the external-change diagnostics for that file and Session from comparisons.
Recomputing doesn't restore missing windows. For details, see
[External edit windows](../explanation/how-capture-works.md#external-edit-windows).

**Warning:** Deleting the database permanently removes its Facts and audit
history. To delete it:

1. Stop capture and the server.
2. Retain any required backups.
3. Follow your database operator's deletion procedure.
4. Remove mirrors, exports, staging directories, and sender buffers separately,
   under their retention policies.

### 8.4 Network exposure

| Direction | Connection |
| --- | --- |
| Inbound | HTTPS ingress forwards to the loopback API on port 8000. |
| API to database | Private PostgreSQL connection with runtime credentials. |
| API to Git hosts | Fetches to permitted hosts for repository mirrors. |
| Gateway to API | Authenticated capture requests from your gateway integration. |

Capture requires ingest authority or a valid webhook signature. Operator reads
require an operator credential. Retrieval grants permit only their configured
Sessions. `/health` requires no authentication and returns no captured content.

Unless you qualify more capacity, run one API process. The API allows 2 active
evidence or report reads, 2 mirror workers, and 16 waiting mirror jobs. A third
concurrent read gets HTTP 503. The read deadline is 30 seconds. The mirror
deadline is 120 seconds. Configure host resource limits separately.

The installer downloads packages and maintained runtime dependencies. Repository
mirroring contacts the permitted Git hosts. Your model endpoint determines where
inference content goes. Sediment doesn't add analytics or crash reporting.

## 9. Troubleshooting

| Symptom | Action |
| --- | --- |
| Installation fails | Check access to the package index and the installer's host-library prerequisites. |
| API startup fails | Inspect the supervisor logs and database reachability. Verify the private environment and the PostgreSQL superuser bootstrap connection. |
| Ingest returns `503 database_unavailable` | Restore database access, and then retry retained payloads. Database deduplication doesn't recover unsent events. |
| Health succeeds, but capture is absent | Run the agent's Session check. Inspect webhook delivery and mirror errors. Health doesn't verify capture. |

For client failures, see [Repair or recover capture](../capture/local-capture.md#repair-or-recover-capture).

## 10. Tear down the deployment

1. [Uninstall capture](../capture/local-capture.md#uninstall-capture) on each
   client.
2. [Remove managed capture](../capture/managed-capture.md#remove-managed-capture).
3. Retain the required backups and exports.
4. Stop and disable the Sediment service in its supervisor.
5. Remove the server's ingress route.
6. If you must remove the data, follow the deletion procedure in
   [Quarantine and wholesale deletion](#83-quarantine-and-wholesale-deletion).

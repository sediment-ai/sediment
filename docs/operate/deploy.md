# Deploy Sediment

Install the published package on a shared host, connect PostgreSQL, and expose
the API through HTTPS. For a one-machine evaluation with managed PostgreSQL,
use the [Quickstart](../quickstart.md).

## 1. Prerequisites

You need:

- a supported Debian or Ubuntu host, or macOS with Homebrew;
- Git, curl, OpenSSL, and permission to install the host libraries;
- a dedicated PostgreSQL 17 instance and database, with an administrative
  connection for role provisioning and migrations;
- a stable HTTPS endpoint, private persistent storage, and encrypted backups.

Use a dedicated operating-system account. Keep the database on a private network.
Start with at least four CPU cores and 8 GB of memory, then
[validate capacity](validate-deployment.md) for your workload.

## 2. Deploy the API

Install the approved [published release](https://github.com/sediment-ai/sediment/releases)
as the account that runs Sediment. Replace the version with the release you selected:

```bash
curl -fsSL https://sediment.so/install.sh | \
  sh -s -- --version '<approved release version>'
```

If the installer prints a PATH instruction, apply it. Verify the version:

```bash
sediment --version
```

### Configure PostgreSQL

Create a dedicated database instance through your PostgreSQL operator or service. Give
Sediment an administrative connection that can provision database roles and
migrate that database. Don't point it at a database used by another application.

Set these variables in the server account's private environment. Replace the
example organization, database connection, and permitted Git hosts:

```bash
export SEDIMENT_ORG_ID=acme
export SEDIMENT_BOOTSTRAP_DATABASE_URL='postgresql+psycopg://bootstrap:<password>@db.internal:5432/sediment'
export SEDIMENT_ALLOWED_CLONE_HOSTS='["github.com"]'
export SEDIMENT_DEV_MODE=false
```

Use the connection security settings required by your database operator. Keep
this environment private and supply it again on every server restart.

### Start the API

Start the installed server:

```bash
sediment server --host 127.0.0.1 --port 8000
```

With `SEDIMENT_BOOTSTRAP_DATABASE_URL` set, Sediment uses that database. It
provisions separate migrator, runtime, and operator roles, then starts the API
with runtime authority. The API doesn't receive the bootstrap credential.

Sediment stores generated API tokens and database-role passwords in the private
`~/.sediment/server/server.env` file. Its Git mirror lives under
`~/.sediment/server/mirror`. Keep this directory on persistent storage.

In a second terminal, verify readiness:

```bash
curl -fsS http://127.0.0.1:8000/health
```

Require `status: "ok"` and the installed release version. Run the same server
command under your process supervisor with the same account, data directory,
and private environment. Configure restart after failure and host restart.

Record the supervisor's start, stop, restart, and log commands. Apply process
memory limits and storage quotas before enrollment. An external PostgreSQL
service has its own lifecycle; stopping Sediment doesn't stop that database.

### Enroll named capture clients

Before distributing credentials, stop Sediment through the supervisor. Generate
a separate token for each developer machine or gateway:

```bash
openssl rand -hex 32
```

In a private editor, add `SEDIMENT_INGEST_TOKENS` to
`~/.sediment/server/server.env` as a JSON object mapping client names to tokens.
Use unique names such as `alice-laptop`. `operator`, `legacy`, and `retrieval`
are reserved. Keep the file mode at `0600`.

Restart Sediment and distribute only each client's token through your credential
channel. Reserve `SEDIMENT_OPERATOR_TOKEN` for queries and reports. Don't
share the configuration file or any database credential with agents.

### Run a second local deployment

Use a separate database instance, operating-system account, and API port for another
shared deployment. For an isolated local evaluation, select a separate data root
and port with `sediment server --root /absolute/private/path --port 8001`.
Don't reuse another deployment's credentials or organization configuration.

## 3. Expose a public HTTPS endpoint

Configure your reverse proxy to forward the HTTPS API hostname to
`http://127.0.0.1:8000`. Keep PostgreSQL private. Restrict administrative access,
rate-limit authentication attempts, and preserve webhook request bodies.

Set request-size and timeout limits that permit the enabled capture paths.
Verify the public endpoint from a developer machine:

```bash
curl -fsS https://sediment-api.example.com/health
```

Require the same status and version as the local check. A health response alone
doesn't verify credentials, Git access, or webhook delivery.

## 4. Configure capture

1. [Enroll pilot developers](run-pilot.md) or [configure local capture](../capture/local-capture.md).
2. Configure [managed capture](../capture/managed-capture.md) for private
   repository mirrors and signed forge webhooks.
3. Verify each participating agent's Session and a real push before expanding
   the deployment.

### Connect a gateway

The published package doesn't include a deployable LiteLLM gateway. Operate your
chosen gateway separately and connect an integration that sends Sediment's
[gateway capture envelope](../reference/api.md#post-ingestgateway).

Keep provider credentials at the gateway. Give developers its URL and client
credential. Preserve their chosen model and verify a captured Inference call
before relying on gateway reports.

### Enable agent-requested retrieval

If your client implements the retrieval API, set `SEDIMENT_RETRIEVAL_TOKEN` in
the server's private process environment. Use a separate secret and exactly one
source setting: `SEDIMENT_RETRIEVAL_SESSION_ID` or `SEDIMENT_RETRIEVAL_SESSION_IDS`.

The plural setting is a JSON array of 1–32 actual Session identifiers and must
fit 16 KiB. Empty values are invalid. Restart Sediment after changing the grant,
and rotate its token. Remove the token and source settings to revoke access.

The grant includes future Facts in the authorized Sessions. Keep operator and
database credentials out of the agent environment. For pi, meet the
[release and runtime requirements](../capture/agent-integrations.md#pi), then
[configure retrieval](resume-with-evidence.md#enable-agent-requested-retrieval).
Operators can also prepare a private evidence packet with the installed CLI.

## 5. Verify the deployment

Log in from the server account's terminal, then inspect Facts:

```bash
sediment login http://127.0.0.1:8000
sediment facts
```

Loopback login reads the default server's private credential file. Remote
operators use the HTTPS URL and their operator token. Capture clients use a
separate `sediment login <url> --capture` enrollment.

For database-local reports, Derivations, exports, and quarantine operations,
configure a separate operator shell with `SEDIMENT_ORG_ID`,
`SEDIMENT_MIRROR_PATH`, and a `SEDIMENT_DATABASE_URL` for the `sediment_operator`
role. Its password is in the server's private credential file.

```bash
sediment db status
```

Require `at_head`. Run the [pilot capture checks](run-pilot.md), then restart the
service through its supervisor. Verify health, retained Facts, and capture again.
Complete [Validate a deployment](validate-deployment.md) before increasing scope.

## 6. Upgrade the deployment

Review the release notes and [security evidence](security.md). Back up the
database and test restoration before a schema-changing upgrade. End active
capture Sessions and stop the API through its supervisor.

Install the selected published version as the same server account:

```bash
curl -fsSL https://sediment.so/install.sh | \
  sh -s -- --version '<target release version>'
```

Preserve the server data directory and credentials. Restart through the
supervisor; startup provisions roles and applies migrations to the configured
database. Verify health, `sediment db status`, and retained capture before
resuming clients. Don't run an older server against an upgraded schema.

Upgrade the API before capture clients and gateway integrations. On each client,
install the approved release and repeat capture enrollment with the same
identifier and approved options.

To rotate an ingest token, stop the API, replace that client's entry in
`SEDIMENT_INGEST_TOKENS`, restart, and reenroll the client. After all legacy
clients migrate to named tokens, remove `SEDIMENT_API_BEARER_TOKEN` from the
saved credential file and process environment.

Rotate `SEDIMENT_OPERATOR_TOKEN` independently and repeat operator login.
Coordinate database-role password changes with the database operator and every
process that uses those credentials. Preserve the existing database and mirror.

## 7. Operating cadence

In the operator shell, inspect Fact growth and reports:

```bash
sediment facts
sediment report model
```

Monitor service restarts, capture failures, database availability, storage use,
and backup results. Apply release and host security updates within your
maintenance deadlines. Retain the exact installed versions with each report.

### Back up and restore

Use your database operator's encrypted backup procedure. Include a consistent
PostgreSQL backup, the private server configuration, and the mirrors needed for
historical analysis. Keep decryption keys off the deployment host.

Before enrollment and after schema changes, restore into a separate, empty
database. Install the same Sediment release, provision roles, and verify schema
status, Fact counts, quarantine history, and a representative export.

Record the backup timestamp, restore result, and recovery duration. Keep the
original deployment intact during recovery testing. A copied live PostgreSQL
data directory isn't a substitute for a consistent backup.

## 8. Privacy and data handling

### 8.1 What Sediment stores

Facts can contain model inputs and outputs, patch arguments, applied text, and
observed file content. Mirrors contain Git history. Notes contain Session
identifiers and timestamps. Agree the
[capture boundaries](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
before enrollment.

Basic redaction isn't comprehensive secret detection. Quarantine captured
credentials and rotate them. Raw buffers, mirrors, and backups remain sensitive.

### 8.2 Where data lives

| Location | Contents |
| --- | --- |
| Configured PostgreSQL database | Facts, schema, and quarantine audit history |
| `~/.sediment/server/server.env` | Private server credentials |
| `~/.sediment/server/mirror` | Git mirrors |
| Operator-selected output and staging directories | Exports and temporary Derivation data |
| Opted-in sender delivery directories | Prepared capture payloads awaiting delivery |

Facts and mirrors have no automatic expiry. Set storage quotas and retention
procedures. Alert before storage fills; the mirror worker's free-space check
doesn't enforce a quota. Restrict host and database administrative access.

### 8.3 Quarantine and wholesale deletion

Quarantine excludes Facts from Derivations and exports without changing their
rows. In the operator shell, preview an inference-call quarantine:

```bash
sediment quarantine-inference-calls --session-id '<Session identifier>' \
  --reason '<reason>'
```

Review the selection before repeating with `--apply`. Use `sediment quarantine-log`
to inspect the audit trail. See the [CLI reference](../reference/cli.md) for
single-Fact quarantine and release commands.

If you quarantine only some Edit observations for a file and Session, exclude
its external-change diagnostics from comparisons. Recomputing doesn't restore
missing windows. See [External edit windows](../explanation/how-capture-works.md#external-edit-windows).

**Deleting the database permanently removes its Facts and audit history.**
Before removal, stop capture and the server, retain any required backups, and
use the database operator's deletion procedure. Remove mirrors, exports, staging,
and sender buffers separately under their retention policies.

### 8.4 Network exposure

| Direction | Connection |
| --- | --- |
| Inbound | HTTPS ingress forwards to the loopback API on port 8000. |
| API to database | Private PostgreSQL connection with runtime credentials. |
| API to Git hosts | Fetches to permitted hosts for repository mirrors. |
| Gateway to API | Authenticated capture requests from your gateway integration. |

Capture requires ingest authority or a valid webhook signature. Operator reads
require an operator credential. Retrieval grants permit only their configured
Sessions. `/health` is unauthenticated and returns no captured content.

Keep one API process unless you qualify additional capacity. The API allows two
active evidence/report reads, two mirror workers, and sixteen waiting mirror
jobs. A third active read returns 503. Read and mirror deadlines are 30 and
120 seconds. Configure host resource limits separately.

The installer downloads packages and maintained runtime dependencies. Repository
mirroring contacts the permitted Git hosts. Your model endpoint determines where
inference content goes. Sediment doesn't add analytics or crash reporting.

## 9. Troubleshooting

- **Installation fails:** check access to the package index and the installer's
  stated host-library prerequisites.
- **API startup fails:** inspect supervisor logs and database reachability.
  Verify the private environment and administrative provisioning connection.
- **Ingest returns `503 database_unavailable`:** restore database access, then
  retry retained payloads. Database deduplication doesn't recover unsent events.
- **Health succeeds but capture is absent:** run the agent's Session check and
  inspect webhook delivery and mirror errors. Health doesn't verify capture.

For client failures, use [Repair or recover capture](../capture/local-capture.md#repair-or-recover-capture).

## 10. Teardown

1. [Uninstall client capture](../capture/local-capture.md#uninstall-capture).
2. [Remove managed capture](../capture/managed-capture.md#remove-managed-capture).
3. Retain the required backups and exports.
4. Stop and disable the server's supervisor and remove its ingress route.
5. If data removal is required, follow the deletion procedure in
   [Quarantine and wholesale deletion](#83-quarantine-and-wholesale-deletion).

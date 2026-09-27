# Deploy Sediment

Deploy PostgreSQL, Sediment, LiteLLM, and Traefik HTTPS on one Amazon Elastic
Compute Cloud (EC2) instance. Follow [Deploy on EC2 with Traefik](#deploy-on-ec2-with-traefik)
for that complete stack.

If you manage PostgreSQL and HTTPS separately, follow
[Install the published package](#install-the-published-package).
For a one-machine evaluation with managed PostgreSQL, use the
[Quickstart](../quickstart.md).

## Deploy on EC2 with Traefik

Use this path for one EC2 instance and one public hostname. The setup generates
Sediment credentials and configures the service connections. You supply the
hostname, a certificate contact email, and an Anthropic API key. The bundled
gateway supports Anthropic models.

Before deploying, review [which components the release scanner checks](security.md#review-the-supplied-evidence).

1. Launch a maintained Ubuntu 24.04 instance with at least 4 vCPUs and 8 GB of
   memory. Use encrypted persistent storage with room for image builds, Facts,
   mirrors, and backups. Configure the storage and backup controls in
   [Privacy and data handling](../../docker/README.md#8-privacy-and-data-handling).
2. Install Git, Python 3.12, [Docker Engine and its Compose plugin](https://docs.docker.com/engine/install/ubuntu/).
   Complete Docker's [non-root access setup](https://docs.docker.com/engine/install/linux-postinstall/)
   for the operator account, then sign in again. Verify `docker info` and
   `docker compose version`. Docker access gives this account root-level control
   of the host.
3. Associate an [Elastic IP address](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/elastic-ip-addresses-eip.html).
   Create one DNS A record, such as `sediment.example.com`, pointing to that
   address. If you publish an AAAA record, IPv6 must also reach the host.
4. Allow inbound TCP ports 80 and 443 in the instance security group and host
   firewall. Restrict SSH to the operator's address. Don't open ports 5432,
   8000, or 4000. Keep port 80 available for certificate renewal.
5. Clone the repository and check out the commit you want to deploy. Replace
   the commit placeholder with its full hash and the example hostname with yours:

   ```bash
   SEDIMENT_REVISION='<full commit hash>'
   git clone https://github.com/sediment-ai/sediment.git sediment || exit 1
   cd sediment || exit 1
   git checkout --detach "$SEDIMENT_REVISION" || exit 1
   test "$(git rev-parse HEAD)" = "$SEDIMENT_REVISION" || exit 1
   chmod go-w .
   python3 scripts/create_deploy_env.py --domain sediment.example.com
   ```

   Enter the certificate contact email and provider key at the prompts. The
   provider-key prompt hides input. The generator creates `.env` with mode 0600
   and refuses to overwrite it. It generates distinct internal credentials,
   records the commit and source fingerprint, and sets `COMPOSE_PROFILES=https`.
   You don't need uv or Sediment packages on the host.
   The directory permission command removes shared write access; Ubuntu can
   otherwise create the checkout with group write permission, which setup rejects.
6. Start the complete deployment:

   ```bash
   docker compose up -d --build --wait
   docker compose ps -a
   ```

   PostgreSQL, the API, LiteLLM, and the proxy report `healthy`. The one-shot
   migration service exits with code 0. If startup fails, inspect
   `docker compose logs --tail 100 postgres migrate api gateway proxy`.
7. Verify the public certificate and both routes:

   ```bash
   curl -fsS https://sediment.example.com/health
   curl -fsS https://sediment.example.com/llm/health/liveliness
   ```

   The API returns `status: "ok"` and the tested build's version. The gateway
   liveness request succeeds. Don't bypass certificate verification. Container health
   doesn't prove that DNS resolves or that certificate issuance succeeds.
   If the proxy starts before public DNS resolves, correct DNS, then run
   `docker compose restart proxy` to retry certificate issuance. Repeat both
   public checks after the restart.

Give developers `https://sediment.example.com` as the Sediment endpoint and
`https://sediment.example.com/llm` as the gateway base URL.
To generate named capture credentials during setup, add `--ingest-client NAME`
once per developer machine. The gateway uses the
private `.env` value `LITELLM_MASTER_KEY` for client authentication. The provider
key stays on the server. Follow [Configure capture](../../docker/README.md#4-configure-capture) for
client enrollment, Session identity, and forge webhooks. A liveness check alone
doesn't verify provider access or stored Inference calls.

Traefik obtains and renews the certificate, redirects HTTP to HTTPS, and removes
`/llm` before forwarding gateway requests. The `sediment-certificates` volume
preserves certificate state across restarts. The proxy runs without root,
capabilities, Docker-socket access, or application credentials. Each router
limits requests from a direct peer to an average of 100 per second with a burst
of 200; excess requests return 429. Clients behind one outbound address share
that allowance. Client-supplied forwarded-address headers don't select it.

To stop and start this deployment, run `docker compose down` and
`docker compose up -d --wait`. Preserve `.env` and the volumes. Follow
[Upgrade the deployment](../../docker/README.md#6-upgrade-the-deployment) when source or credentials
change. Certificate renewal requires outbound certificate-authority access and
inbound port 80 even after initial setup.

For container administration and API-only Compose setups, use the
[Compose runbook](../../docker/README.md). A cloud load balancer doesn't remove
this single instance's availability limit.

## Install the published package

Use this procedure to install the API on a shared host with an existing
PostgreSQL instance and HTTPS endpoint. These commands manage the package
installation. Use the [Compose runbook](../../docker/README.md) to operate the
EC2 stack.

### 1. Prerequisites

You need:

- a supported Debian or Ubuntu host, or macOS with Homebrew;
- Git, curl, OpenSSL, and permission to install the host libraries;
- a dedicated PostgreSQL 17 instance and database, with a PostgreSQL superuser
  connection for role provisioning and migrations;
- a stable HTTPS endpoint, private persistent storage, and encrypted backups.

Use a dedicated operating-system account. Keep the database on a private network.
Start with at least four CPU cores and 8 GB of memory, then
[validate capacity](validate-deployment.md) for your workload.

### 2. Deploy the API

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

#### Configure PostgreSQL

Create a dedicated PostgreSQL 17 instance and database. Set the bootstrap
connection to a PostgreSQL superuser (`rolsuper=true`). Role provisioning rejects
non-superuser administrators, including managed database services that don't
provide superuser access. Don't point Sediment at a database used by another
application.

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

#### Start the API

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

#### Enroll named capture clients

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

#### Run a second local deployment

Use a separate database instance, operating-system account, and API port for another
shared deployment. For an isolated local evaluation, select a separate data root
and port with `sediment server --root /absolute/private/path --port 8001`.
Don't reuse another deployment's credentials or organization configuration.

### 3. Expose a public HTTPS endpoint

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

### 4. Configure capture

1. [Enroll pilot developers](run-pilot.md) or [configure local capture](../capture/local-capture.md).
2. Configure [managed capture](../capture/managed-capture.md) for private
   repository mirrors and signed forge webhooks.
3. Verify each participating agent's Session and a real push before expanding
   the deployment.

#### Connect a gateway

The published package doesn't include a deployable LiteLLM gateway. Operate your
chosen gateway separately and connect an integration that sends Sediment's
[gateway capture envelope](../reference/api.md#post-ingestgateway).

Keep provider credentials at the gateway. Give developers its URL and client
credential. Preserve their chosen model and verify a captured Inference call
before relying on gateway reports.

#### Enable agent-requested retrieval

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

### 5. Verify the deployment

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

### 6. Upgrade the deployment

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

### 7. Operating cadence

In the operator shell, inspect Fact growth and reports:

```bash
sediment facts
sediment report model
```

Monitor service restarts, capture failures, database availability, storage use,
and backup results. Apply release and host security updates within your
maintenance deadlines. Retain the exact installed versions with each report.

#### Back up and restore

Use your database operator's encrypted backup procedure. Include a consistent
PostgreSQL backup, the private server configuration, and the mirrors needed for
historical analysis. Keep decryption keys off the deployment host.

Before enrollment and after schema changes, restore into a separate, empty
database. Install the same Sediment release, provision roles, and verify schema
status, Fact counts, quarantine history, and a representative export.

Record the backup timestamp, restore result, and recovery duration. Keep the
original deployment intact during recovery testing. A copied live PostgreSQL
data directory isn't a substitute for a consistent backup.

### 8. Privacy and data handling

#### 8.1 What Sediment stores

Facts can contain model inputs and outputs, patch arguments, applied text, and
observed file content. Mirrors contain Git history. Notes contain Session
identifiers and timestamps. Agree the
[capture boundaries](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
before enrollment.

Basic redaction isn't comprehensive secret detection. Quarantine captured
credentials and rotate them. Raw buffers, mirrors, and backups remain sensitive.

#### 8.2 Where data lives

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

#### 8.3 Quarantine and wholesale deletion

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

#### 8.4 Network exposure

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

### 9. Troubleshooting

- **Installation fails:** check access to the package index and the installer's
  stated host-library prerequisites.
- **API startup fails:** inspect supervisor logs and database reachability.
  Verify the private environment and the PostgreSQL superuser bootstrap connection.
- **Ingest returns `503 database_unavailable`:** restore database access, then
  retry retained payloads. Database deduplication doesn't recover unsent events.
- **Health succeeds but capture is absent:** run the agent's Session check and
  inspect webhook delivery and mirror errors. Health doesn't verify capture.

For client failures, use [Repair or recover capture](../capture/local-capture.md#repair-or-recover-capture).

### 10. Teardown

1. [Uninstall client capture](../capture/local-capture.md#uninstall-capture).
2. [Remove managed capture](../capture/managed-capture.md#remove-managed-capture).
3. Retain the required backups and exports.
4. Stop and disable the server's supervisor and remove its ingress route.
5. If data removal is required, follow the deletion procedure in
   [Quarantine and wholesale deletion](#83-quarantine-and-wholesale-deletion).

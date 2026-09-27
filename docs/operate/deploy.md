# Deploy Sediment

Install a published Sediment version on one Amazon Elastic Compute Cloud (EC2)
instance with PostgreSQL and Traefik HTTPS. Follow
[Deploy on EC2 with Traefik](#deploy-on-ec2-with-traefik) for that setup.

If you manage PostgreSQL and HTTPS separately, follow
[Install the published package](#install-the-published-package).
For a one-machine evaluation, use the [Quickstart](../quickstart.md).

## Deploy on EC2 with Traefik

Use this procedure for one Ubuntu instance and one public hostname. Install
Sediment from PyPI and pull the published PostgreSQL and Traefik images. You
select a Sediment release version; no Sediment checkout or image build is needed.
The API runs as your non-root account under systemd. Docker Compose runs the
database and proxy.

This setup captures agent and Git activity. If you also need a model gateway,
follow [Connect a gateway](#connect-a-gateway). The Python release doesn't
include a deployable LiteLLM gateway.

### Prepare the instance

1. Launch Ubuntu 24.04 on a 64-bit Intel/AMD or Arm instance with at least
   4 vCPUs and 8 GB of memory. Use encrypted persistent storage for the database,
   credentials, mirrors, and backups. Use a dedicated non-root operator account
   with `sudo` access. Run the following commands from that account's SSH session.
2. Install `curl`, OpenSSL, and [Docker Engine with its Compose plugin](https://docs.docker.com/engine/install/ubuntu/).
   Complete Docker's [non-root access setup](https://docs.docker.com/engine/install/linux-postinstall/)
   and sign in again. Verify `docker info` and `docker compose version`.
   Docker access gives this account root-level control of the host.
3. Associate an [Elastic IP address](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/elastic-ip-addresses-eip.html).
   Point your hostname's DNS A record at that address. If you publish an AAAA
   record, IPv6 must also reach the host.
4. Allow inbound TCP ports 80 and 443 in the instance security group and host
   firewall. Restrict SSH to the operator's address. Keep port 5432 and 8000
   private. Certificate renewal requires inbound port 80 and outbound
   certificate-authority access.

### Install a Sediment version

Select a [published release](https://github.com/sediment-ai/sediment/releases)
and review its [security evidence](security.md). This example installs 0.2.0.
Run the installer as the operator account:

```bash
SEDIMENT_VERSION=0.2.0
SEDIMENT_INSTALLER="$(mktemp)" || exit 1
curl -fsSL https://sediment.so/install.sh -o "$SEDIMENT_INSTALLER" || exit 1
UV_NO_BUILD=1 UV_TOOL_BIN_DIR="$HOME/.local/bin" \
  sh "$SEDIMENT_INSTALLER" --method uv --version "$SEDIMENT_VERSION" || exit 1
rm "$SEDIMENT_INSTALLER"
export PATH="$HOME/.local/bin:$PATH"
sediment --version
```

Require the selected version. `UV_NO_BUILD=1` makes installation fail if a
required wheel is unavailable. The installer installs Python 3.12 and the host
libraries through package managers.

### Configure PostgreSQL and Traefik

Create a private deployment directory. These commands refuse to reuse an
existing directory. Replace the hostname, certificate contact email, and
organization before running them:

```bash
umask 077
mkdir -m 700 "$HOME/sediment-deploy" || exit 1
cd "$HOME/sediment-deploy" || exit 1
POSTGRES_PASSWORD="$(openssl rand -hex 32)"
cat > .env <<EOF_ENV
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
SEDIMENT_DOMAIN=sediment.example.com
SEDIMENT_ACME_EMAIL=operator@example.com
EOF_ENV
cat > server.env <<EOF_SERVER
SEDIMENT_ORG_ID=acme
SEDIMENT_BOOTSTRAP_DATABASE_URL=postgresql+psycopg://sediment:$POSTGRES_PASSWORD@127.0.0.1:5432/sediment
SEDIMENT_ALLOWED_CLONE_HOSTS=["github.com"]
SEDIMENT_DEV_MODE=false
EOF_SERVER
unset POSTGRES_PASSWORD
sudo install -d -m 700 -o root -g root certificates
```

Keep `.env`, `server.env`, and `certificates/` private. Don't source these files
or share them with developers.

Save the following as `compose.yaml` in that directory:

```yaml
name: sediment-deployment
services:
  postgres:
    image: postgres:17.11-bookworm
    restart: unless-stopped
    environment:
      POSTGRES_DB: sediment
      POSTGRES_USER: sediment
      POSTGRES_PASSWORD: "${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}"
      POSTGRES_INITDB_ARGS: --auth-host=scram-sha-256 --auth-local=scram-sha-256
    ports: ["127.0.0.1:5432:5432"]
    volumes: ["postgres-data:/var/lib/postgresql/data"]
    mem_limit: 1g
    pids_limit: 256
    logging:
      driver: json-file
      options: {max-size: 10m, max-file: "3"}
    healthcheck:
      test: [CMD-SHELL, pg_isready -h 127.0.0.1 -U sediment -d sediment]
      interval: 5s
      timeout: 5s
      retries: 12
  proxy:
    image: traefik:v3.7.13
    restart: unless-stopped
    network_mode: host
    read_only: true
    cap_drop: [ALL]
    cap_add: [NET_BIND_SERVICE]
    security_opt: [no-new-privileges:true]
    mem_limit: 256m
    pids_limit: 64
    environment:
      SEDIMENT_DOMAIN: "${SEDIMENT_DOMAIN:?Set SEDIMENT_DOMAIN in .env}"
      TRAEFIK_GLOBAL_CHECKNEWVERSION: "false"
      TRAEFIK_GLOBAL_SENDANONYMOUSUSAGE: "false"
      TRAEFIK_ENTRYPOINTS_WEB_ADDRESS: :80
      TRAEFIK_ENTRYPOINTS_WEB_HTTP_REDIRECTIONS_ENTRYPOINT_TO: websecure
      TRAEFIK_ENTRYPOINTS_WEB_HTTP_REDIRECTIONS_ENTRYPOINT_SCHEME: https
      TRAEFIK_ENTRYPOINTS_WEBSECURE_ADDRESS: :443
      TRAEFIK_PROVIDERS_FILE_FILENAME: /etc/traefik/routes.yml
      TRAEFIK_CERTIFICATESRESOLVERS_LETSENCRYPT_ACME_EMAIL: "${SEDIMENT_ACME_EMAIL:?Set SEDIMENT_ACME_EMAIL in .env}"
      TRAEFIK_CERTIFICATESRESOLVERS_LETSENCRYPT_ACME_STORAGE: /certificates/acme.json
      TRAEFIK_CERTIFICATESRESOLVERS_LETSENCRYPT_ACME_HTTPCHALLENGE_ENTRYPOINT: web
    volumes:
      - ./routes.yml:/etc/traefik/routes.yml:ro
      - ./certificates:/certificates
    logging:
      driver: json-file
      options: {max-size: 10m, max-file: "3"}
volumes:
  postgres-data:
```

On Linux, host networking lets Traefik reach the API on loopback. The upstream
proxy runs as container root with only the capability to bind ports 80 and 443.
It writes certificates to the root-owned private directory. It receives no
Docker socket, database password, or Sediment credentials.
Review and maintain these [PostgreSQL](https://hub.docker.com/_/postgres) and
[Traefik](https://doc.traefik.io/traefik/) releases separately from the Sediment
package. Sediment's image scan reports describe its contributor images, not
these upstream images.

Save the following as `routes.yml` beside `compose.yaml`:

```yaml
http:
  routers:
    sediment:
      rule: 'Host(`{{ env "SEDIMENT_DOMAIN" }}`)'
      entryPoints: [websecure]
      middlewares: [request-limit]
      service: sediment
      tls:
        certResolver: letsencrypt
  middlewares:
    request-limit:
      rateLimit:
        average: 100
        burst: 200
  services:
    sediment:
      loadBalancer:
        servers:
          - url: http://127.0.0.1:8000
```

Allow the proxy to read its non-secret routing configuration, then start the
database:

```bash
chmod 644 routes.yml
docker compose pull
docker compose up -d --wait postgres
```

Require PostgreSQL to report `healthy`. If it fails, inspect
`docker compose logs --tail 100 postgres` before continuing.

### Run the installed package as a service

Create `~/.config/systemd/user/` with `mkdir -p ~/.config/systemd/user`.
Save this unit as `~/.config/systemd/user/sediment.service`:

```ini
[Unit]
Description=Sediment API

[Service]
ExecStart=%h/.local/bin/sediment server --host 127.0.0.1 --port 8000
EnvironmentFile=%h/sediment-deploy/server.env
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=5
TimeoutStopSec=120
UMask=0077
NoNewPrivileges=yes
MemoryMax=2G
TasksMax=256

[Install]
WantedBy=default.target
```

Enable startup at boot and after logout, then start Sediment:

```bash
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now sediment.service
systemctl --user status sediment.service
curl -fsS http://127.0.0.1:8000/health
```

Require `status: "ok"` and your selected Sediment version. Startup provisions
separate database roles and applies migrations. The API receives only runtime
database authority. Generated API credentials and database-role passwords live
in `~/.sediment/server/server.env`; mirrors live in `~/.sediment/server/mirror`.
Keep both directories on persistent storage. If startup fails, inspect
`journalctl --user -u sediment.service -n 100 --no-pager`.

### Enable and verify HTTPS

From `~/sediment-deploy`, start Traefik:

```bash
docker compose up -d proxy
docker compose logs --tail 100 proxy
curl -fsS https://sediment.example.com/health
```

Require `status: "ok"` and the installed version. Don't bypass certificate
verification. Traefik obtains and renews the certificate and redirects HTTP to
HTTPS. If issuance fails, check public DNS, ports 80 and 443, and the proxy log.
After correcting DNS, run `docker compose restart proxy` and repeat the public
health check. Preserve `certificates/acme.json` across restarts.

The request limit uses the direct peer's address, with an average of 100 requests
per second and a burst of 200. Clients behind one outbound address share that
allowance. Give developers `https://sediment.example.com` as the endpoint.
[Enroll named capture clients](#enroll-named-capture-clients), then follow
[Configure capture](#4-configure-capture) and [Verify the deployment](#5-verify-the-deployment).
Use `systemctl --user restart sediment.service` after credential changes.
A health check doesn't prove capture or provider access.

### Operate and upgrade this installation

- Stop or restart the API with `systemctl --user stop sediment.service` or
  `systemctl --user restart sediment.service`. PostgreSQL runs independently.
- From `~/sediment-deploy`, use `docker compose stop` and `docker compose up -d`
  to stop and start the database and proxy. Stop the API before the database.
- Before an upgrade, follow [Back up and restore](#back-up-and-restore). Retain
  the `postgres-data` volume, `~/sediment-deploy`, and `~/.sediment/server`.
  Never add `--volumes` to `docker compose down` unless you intend to delete
  the database.
- To upgrade Sediment, follow [Upgrade the deployment](#6-upgrade-the-deployment)
  with the target package version. Stop and restart `sediment.service` around
  installation. Upgrade PostgreSQL and Traefik separately using their release
  instructions; don't change the PostgreSQL major version on an existing volume.

A single instance remains a single point of failure. Set storage quotas,
monitor service restarts, and test encrypted backup restoration before enrollment.

## Install the published package

Use this procedure to install the API on a shared host with an existing
PostgreSQL instance and HTTPS endpoint. These commands manage the package
installation. For the complete EC2 procedure, follow
[Deploy on EC2 with Traefik](#deploy-on-ec2-with-traefik).

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
SEDIMENT_VERSION=0.2.0
SEDIMENT_INSTALLER="$(mktemp)" || exit 1
curl -fsSL https://sediment.so/install.sh -o "$SEDIMENT_INSTALLER" || exit 1
UV_NO_BUILD=1 sh "$SEDIMENT_INSTALLER" --method uv --version "$SEDIMENT_VERSION" || exit 1
rm "$SEDIMENT_INSTALLER"
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
SEDIMENT_VERSION='<target release version>'
SEDIMENT_INSTALLER="$(mktemp)" || exit 1
curl -fsSL https://sediment.so/install.sh -o "$SEDIMENT_INSTALLER" || exit 1
UV_NO_BUILD=1 sh "$SEDIMENT_INSTALLER" --method uv --version "$SEDIMENT_VERSION" || exit 1
rm "$SEDIMENT_INSTALLER"
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

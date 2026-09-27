# Deploy Sediment

This page is for operators who run Sediment for a team. It installs a published
Sediment version on one Amazon Elastic Compute Cloud (EC2) instance with
PostgreSQL and Traefik HTTPS, or on a host where you already run PostgreSQL and
HTTPS.

- To deploy on one EC2 instance, follow
  [Deploy on EC2 with Traefik](#deploy-on-ec2-with-traefik).
- If you manage PostgreSQL and HTTPS separately, follow
  [Install the published package](#install-the-published-package).
- For a one-machine evaluation, follow the [Quickstart](../quickstart.md).

## Deploy on EC2 with Traefik

This procedure deploys Sediment on one Ubuntu instance with one public hostname.
You install Sediment from the Python Package Index (PyPI) by release version and
pull the published PostgreSQL and Traefik images. You don't need a Sediment
checkout or an image build. The API runs under systemd as your non-root account.
Docker Compose runs the database and the proxy.

This setup captures agent and Git activity. The published package doesn't
include a deployable LiteLLM gateway. If you also need a model gateway, follow
[Connect a gateway](#connect-a-gateway).

### Prepare the instance

1. Launch an Ubuntu 24.04 instance on 64-bit Intel, AMD, or Arm hardware with at
   least 4 virtual CPUs (vCPUs) and 8 GB of memory. Give it encrypted persistent
   storage for the database, credentials, mirrors, and backups.
2. Sign in over SSH as a dedicated non-root operator account that has `sudo`
   access. Run the remaining commands in this procedure from that session.
3. Install `curl`, OpenSSL, and Docker Engine with its Compose plugin. For
   Docker, follow [Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/).
4. To run Docker as a non-root user, follow
   [Linux post-installation steps for Docker Engine](https://docs.docker.com/engine/install/linux-postinstall/),
   and then sign in again. Docker access gives this account root-level control
   of the host.
5. Run `docker info` and `docker compose version` without `sudo`. Both commands
   succeed.
6. Associate an [Elastic IP address](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/elastic-ip-addresses-eip.html)
   with the instance.
7. Point your hostname's DNS A record at the Elastic IP address. If you also
   publish an AAAA record, make the host reachable over IPv6.
8. Configure the instance security group and the host firewall:
   - Allow inbound TCP ports 80 and 443. Certificate renewal requires inbound
     port 80 and outbound access to the certificate authority.
   - Restrict SSH to the operator's IP address.
   - Keep ports 5432 and 8000 private.

### Install a Sediment version

1. Select a [published release](https://github.com/sediment-ai/sediment/releases).
2. Review the release's evidence with
   [Check release and deployment security](security.md).
3. As the operator account, run the installer. This example installs version
   0.2.0:

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

   Confirm that the output shows the version that you selected.

If a required wheel is unavailable, `UV_NO_BUILD=1` makes the installation fail
instead of building from source. The installer also installs Python 3.12 and
the host libraries through package managers.

### Configure PostgreSQL and Traefik

1. Create a private deployment directory and its environment files. Before you
   run the following commands, replace the example hostname, certificate contact
   email, and organization. The commands stop if the directory already exists.

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

   Keep `.env`, `server.env`, and `certificates/` private. Don't source these
   files or share them with developers.

2. Save the following as `compose.yaml` in `~/sediment-deploy`:

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

3. Save the following as `routes.yml` beside `compose.yaml`:

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

4. Make the routing file readable by the proxy, and then start the database:

   ```bash
   chmod 644 routes.yml
   docker compose pull
   docker compose up -d --wait postgres
   ```

   Confirm that PostgreSQL reports `healthy`. If it doesn't, inspect
   `docker compose logs --tail 100 postgres` before you continue.

On Linux, host networking lets Traefik reach the API on loopback. The Traefik
container runs as root with only the capability to bind ports 80 and 443. It
writes certificates to the root-owned private `certificates/` directory. It
gets no Docker socket, database password, or Sediment credential.

Sediment's image scan reports cover its contributor images, not these upstream
images. Track PostgreSQL and Traefik releases separately from the Sediment
package. For release details, see the
[postgres official image](https://hub.docker.com/_/postgres) and the
[Traefik Proxy documentation](https://doc.traefik.io/traefik/).

### Run the installed package as a service

1. Create the systemd user directory:
   `mkdir -p ~/.config/systemd/user`.
2. Save the following unit as `~/.config/systemd/user/sediment.service`:

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

3. Enable startup at boot and after logout, and then start Sediment:

   ```bash
   sudo loginctl enable-linger "$USER"
   systemctl --user daemon-reload
   systemctl --user enable --now sediment.service
   systemctl --user status sediment.service
   curl -fsS http://127.0.0.1:8000/health
   ```

   Confirm that the health response shows `status: "ok"` and the version that
   you selected. If startup fails, inspect
   `journalctl --user -u sediment.service -n 100 --no-pager`.

Startup provisions separate database roles and applies migrations. The API
receives only runtime database authority. `~/.sediment/server/server.env` holds
the generated API credentials and database-role passwords.
`~/.sediment/server/mirror` holds the mirrors. Keep `~/.sediment/server` on
persistent storage.

### Enable and verify HTTPS

1. From `~/sediment-deploy`, start Traefik and check the public endpoint:

   ```bash
   docker compose up -d proxy
   docker compose logs --tail 100 proxy
   curl -fsS https://sediment.example.com/health
   ```

   Confirm that the health response shows `status: "ok"` and the installed
   version. Don't bypass certificate verification.

2. [Enroll named capture clients](#enroll-named-capture-clients). After you
   change credentials, restart Sediment with
   `systemctl --user restart sediment.service`.
3. Give developers `https://sediment.example.com` as the endpoint.
4. [Configure capture](#4-configure-capture).
5. [Verify the deployment](#5-verify-the-deployment). A health check doesn't
   prove capture or provider access.

Traefik obtains and renews the certificate. It also redirects HTTP to HTTPS. If
certificate issuance fails, check public DNS, ports 80 and 443, and the proxy
log. After you correct DNS, run `docker compose restart proxy` and repeat the
public health check. Preserve `certificates/acme.json` across restarts.

The request limit applies to each direct peer address: an average of 100
requests per second, with bursts of 200. Clients behind one outbound address
share that limit.

### Operate and upgrade this installation

| Task | Command |
| --- | --- |
| Stop the API | `systemctl --user stop sediment.service` |
| Restart the API | `systemctl --user restart sediment.service` |
| Stop the database and proxy | `docker compose stop`, from `~/sediment-deploy` |
| Start the database and proxy | `docker compose up -d`, from `~/sediment-deploy` |

PostgreSQL runs independently of the API. Stop the API before you stop the
database.

**Warning:** `docker compose down --volumes` permanently deletes the database.
Use `--volumes` only when you intend to delete it.

- Before an upgrade, follow [Back up and restore](#back-up-and-restore). Retain
  the `postgres-data` volume, `~/sediment-deploy`, and `~/.sediment/server`.
- To upgrade Sediment, follow [Upgrade the deployment](#6-upgrade-the-deployment)
  with the target package version. Stop `sediment.service` before you install
  the package, and restart it afterward.
- To upgrade PostgreSQL or Traefik, follow its own release instructions. Don't
  change the PostgreSQL major version on an existing volume.

A single instance is a single point of failure. Before you enroll developers,
set storage quotas, monitor service restarts, and test restoring an encrypted
backup.

## Install the published package

If you already run PostgreSQL and an HTTPS endpoint, use this procedure to
install the API on a shared host. This procedure installs only the Sediment
package. You provide the database, the HTTPS endpoint, and the process
supervisor. For the complete EC2 procedure, follow
[Deploy on EC2 with Traefik](#deploy-on-ec2-with-traefik).

### 1. Prerequisites

You need the following:

- A supported Debian or Ubuntu host, or macOS with Homebrew
- Git, `curl`, OpenSSL, and permission to install the host libraries
- A dedicated PostgreSQL 17 instance and database, with a PostgreSQL superuser
  connection for role provisioning and migrations
- A stable HTTPS endpoint, private persistent storage, and encrypted backups

Use a dedicated operating-system account for Sediment. Keep the database on a
private network. Start with at least 4 CPU cores and 8 GB of memory. Then check
capacity for your workload with [Validate a deployment](validate-deployment.md).

### 2. Deploy the API

As the account that runs Sediment, install the approved
[published release](https://github.com/sediment-ai/sediment/releases). Replace
the example version with the release that you selected:

```bash
SEDIMENT_VERSION=0.2.0
SEDIMENT_INSTALLER="$(mktemp)" || exit 1
curl -fsSL https://sediment.so/install.sh -o "$SEDIMENT_INSTALLER" || exit 1
UV_NO_BUILD=1 sh "$SEDIMENT_INSTALLER" --method uv --version "$SEDIMENT_VERSION" || exit 1
rm "$SEDIMENT_INSTALLER"
```

If the installer prints an instruction to update `PATH`, follow it. Then check
the installed version:

```bash
sediment --version
```

The command prints the version that you installed.

#### Configure PostgreSQL

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

#### Start the API

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

When `SEDIMENT_BOOTSTRAP_DATABASE_URL` is set, Sediment uses that database. It
provisions separate migrator, runtime, and operator roles. Then it starts the
API with runtime authority. The API doesn't receive the bootstrap credential.

Sediment stores generated API tokens and database-role passwords in the private
file `~/.sediment/server/server.env`. Its Git mirrors live under
`~/.sediment/server/mirror`. Keep `~/.sediment/server` on persistent storage.

An external PostgreSQL service has its own lifecycle. Stopping Sediment doesn't
stop that database.

#### Enroll named capture clients

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

#### Run a second local deployment

For another shared deployment, use a separate database instance,
operating-system account, and API port. For an isolated local evaluation, select
a separate data root and port:
`sediment server --root /absolute/private/path --port 8001`. Don't reuse another
deployment's credentials or organization configuration.

### 3. Expose a public HTTPS endpoint

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

### 4. Configure capture

1. Enroll developers with
   [Run a Cursor, pi, and Codex pilot](run-pilot.md) or
   [Configure local capture](../capture/local-capture.md).
2. For private repository mirrors and signed forge webhooks, follow
   [Roll out managed capture](../capture/managed-capture.md).
3. Before you expand the deployment, verify a Session from each participating
   agent and a real push.

#### Connect a gateway

The published package doesn't include a deployable LiteLLM gateway. Run your
gateway separately. Connect an integration that sends the capture envelope that
[`POST /ingest/gateway`](../reference/api.md#post-ingestgateway) accepts.

Keep provider credentials at the gateway. Give developers the gateway URL and a
client credential. Don't override the model that each developer chooses. Before
you rely on gateway reports, verify a captured Inference call.

#### Enable agent-requested retrieval

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

### 5. Verify the deployment

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

### 6. Upgrade the deployment

Upgrade the API before capture clients and gateway integrations.

1. Review the release notes and the release's evidence with
   [Check release and deployment security](security.md).
2. Before a schema-changing upgrade, back up the database and test restoration.
3. End active capture Sessions.
4. Stop the API through its supervisor.
5. As the same server account, install the selected published version:

   ```bash
   SEDIMENT_VERSION='<target release version>'
   SEDIMENT_INSTALLER="$(mktemp)" || exit 1
   curl -fsSL https://sediment.so/install.sh -o "$SEDIMENT_INSTALLER" || exit 1
   UV_NO_BUILD=1 sh "$SEDIMENT_INSTALLER" --method uv --version "$SEDIMENT_VERSION" || exit 1
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

### 7. Monitor the deployment

In the operator shell, inspect Fact growth and reports:

```bash
sediment facts
sediment report model
```

Monitor service restarts, capture failures, database availability, storage use,
and backup results. Apply release and host security updates within your
maintenance deadlines. Record the exact installed versions with each report.

#### Back up and restore

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

### 8. Privacy and data handling

#### 8.1 What Sediment stores

Facts can contain model inputs and outputs, patch arguments, applied text, and
observed file content. Mirrors contain Git history. Git notes contain Session
identifiers and timestamps. Before enrollment, agree on the capture boundaries
in [Privacy boundaries and ceilings](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings).

Basic redaction isn't comprehensive secret detection. If Sediment captures a
credential, quarantine the Facts that contain it, and rotate the credential.
Raw buffers, mirrors, and backups remain sensitive.

#### 8.2 Where data lives

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

#### 8.3 Quarantine and wholesale deletion

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

#### 8.4 Network exposure

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

### 9. Troubleshooting

| Symptom | Action |
| --- | --- |
| Installation fails | Check access to the package index and the installer's host-library prerequisites. |
| API startup fails | Inspect the supervisor logs and database reachability. Verify the private environment and the PostgreSQL superuser bootstrap connection. |
| Ingest returns `503 database_unavailable` | Restore database access, and then retry retained payloads. Database deduplication doesn't recover unsent events. |
| Health succeeds, but capture is absent | Run the agent's Session check. Inspect webhook delivery and mirror errors. Health doesn't verify capture. |

For client failures, see [Repair or recover capture](../capture/local-capture.md#repair-or-recover-capture).

### 10. Tear down the deployment

1. [Uninstall capture](../capture/local-capture.md#uninstall-capture) on each
   client.
2. [Remove managed capture](../capture/managed-capture.md#remove-managed-capture).
3. Retain the required backups and exports.
4. Stop and disable the Sediment service in its supervisor.
5. Remove the server's ingress route.
6. If you must remove the data, follow the deletion procedure in
   [Quarantine and wholesale deletion](#83-quarantine-and-wholesale-deletion).

# Deploy Sediment on EC2 with Traefik

Use this page to run Sediment for a team on one Amazon Elastic Compute Cloud
(EC2) instance with one public hostname. You install a published Sediment
version from the Python Package Index (PyPI) and pull the published PostgreSQL
and Traefik images. You don't need a Sediment checkout or an image build.

The API runs under systemd as your non-root operator account. Docker Compose
runs PostgreSQL and the Traefik proxy. Traefik serves HTTPS for the hostname.

This setup captures agent and Git activity. The published package doesn't
include a deployable LiteLLM gateway. If you also need a model gateway, follow
[Connect a gateway](deploy.md#connect-a-gateway). If you already run PostgreSQL
and HTTPS, follow [Deploy Sediment](deploy.md) instead.

## Prepare the instance

1. Launch an Ubuntu 24.04 instance on 64-bit Intel, AMD, or Arm hardware. Give
   it at least 4 virtual CPUs (vCPUs), 8 GB of memory, and encrypted persistent
   storage for the database, credentials, mirrors, and backups.
2. Sign in over SSH as a dedicated non-root operator account that has `sudo`
   access. Run the remaining commands on this page from that session.
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

## Install a Sediment version

1. Select a [published release](https://github.com/sediment-ai/sediment/releases).
2. Review the release's evidence with
   [Check release and deployment security](security.md).
3. As the operator account, run the installer. This example installs version
   0.3.0:

   ```bash
   SEDIMENT_VERSION=0.3.0
   SEDIMENT_INSTALLER="$(mktemp)" &&
     curl -fsSL https://sediment.so/install.sh -o "$SEDIMENT_INSTALLER" &&
     UV_NO_BUILD=1 UV_TOOL_BIN_DIR="$HOME/.local/bin" \
       sh "$SEDIMENT_INSTALLER" --method uv --version "$SEDIMENT_VERSION" &&
     rm "$SEDIMENT_INSTALLER" &&
     export PATH="$HOME/.local/bin:$PATH" &&
     sediment --version
   ```

   Confirm that the output shows the version that you selected.

If a required wheel is unavailable, `UV_NO_BUILD=1` makes the installation fail
instead of building from source. The installer also installs Python 3.12 and
the host libraries through package managers.

## Configure PostgreSQL and Traefik

1. Create a private deployment directory and its environment files. Before you
   run the following commands, replace the example hostname, certificate contact
   email, and organization. The commands stop if the directory already exists.

   ```bash
   umask 077
   if mkdir -m 700 "$HOME/sediment-deploy" && cd "$HOME/sediment-deploy"; then
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
   fi
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

## Run Sediment as a service

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

## Enable and verify HTTPS

- From `~/sediment-deploy`, start Traefik and check the public endpoint:

  ```bash
  docker compose up -d proxy
  docker compose logs --tail 100 proxy
  curl -fsS https://sediment.example.com/health
  ```

  Confirm that the health response shows `status: "ok"` and the installed
  version. Don't bypass certificate verification.

Traefik obtains and renews the certificate. It also redirects HTTP to HTTPS. If
certificate issuance fails, check public DNS, ports 80 and 443, and the proxy
log. After you correct DNS, run `docker compose restart proxy` and repeat the
public health check. Preserve `certificates/acme.json` across restarts.

The request limit applies to each direct peer address: an average of 100
requests per second, with bursts of 200. Clients behind one outbound address
share that limit.

## Enroll your team

A single instance is a single point of failure. Before you enroll developers,
set storage quotas, monitor service restarts, and test a restore with
[Back up and restore](deploy.md#back-up-and-restore).

Where the following procedures tell you to stop, restart, or inspect Sediment
through its supervisor, use the `sediment.service` commands in
[Operate and upgrade this installation](#operate-and-upgrade-this-installation).

1. [Enroll named capture clients](deploy.md#enroll-named-capture-clients).
2. Give developers `https://sediment.example.com` as the endpoint.
3. [Configure capture](deploy.md#4-configure-capture).
4. [Verify the deployment](deploy.md#5-verify-the-deployment). A health check
   doesn't prove capture or provider access.

## Operate and upgrade this installation

| Task | Command |
| --- | --- |
| Stop the API | `systemctl --user stop sediment.service` |
| Restart the API | `systemctl --user restart sediment.service` |
| Inspect the API log | `journalctl --user -u sediment.service -n 100 --no-pager` |
| Stop the database and proxy | `docker compose stop`, from `~/sediment-deploy` |
| Start the database and proxy | `docker compose up -d`, from `~/sediment-deploy` |

PostgreSQL runs independently of the API. Stop the API before you stop the
database.

**Warning:** `docker compose down --volumes` permanently deletes the database.
Use `--volumes` only when you intend to delete it.

- Before an upgrade, follow [Back up and restore](deploy.md#back-up-and-restore).
  Retain the `postgres-data` volume, `~/sediment-deploy`, and
  `~/.sediment/server`.
- To upgrade Sediment, follow
  [Upgrade the deployment](deploy.md#6-upgrade-the-deployment). For its install
  step, run the installer command from
  [Install a Sediment version](#install-a-sediment-version) with the target
  version. That command installs to `~/.local/bin`, where `sediment.service`
  runs the package.
- To upgrade PostgreSQL or Traefik, follow its own release instructions. Don't
  change the PostgreSQL major version on an existing volume.

For monitoring, data handling, troubleshooting, and teardown, see
[Deploy Sediment](deploy.md).

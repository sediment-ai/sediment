# Rehearse the single-host Compose deployment

This runbook is for contributors. It builds the repository's PostgreSQL, API,
LiteLLM, and Traefik containers from source with Docker Compose. To run
Sediment for a team, install the published package with
[Deploy Sediment on EC2](deploy-ec2.md) or
[Deploy Sediment on your own host](deploy.md) instead.

## Prerequisites

You need:

- a maintained host with at least 4 vCPUs and 8 GB of memory; allocate these
  resources to Docker Desktop's virtual machine when using it
- Docker Desktop or Docker Engine with Docker Compose
- Git, curl, uv, and the full hash of the Sediment commit you want to deploy

Start Docker, then check the host tools:

```bash
docker info >/dev/null && docker compose version
git --version && curl --version && uv --version
```

Before a shared pilot, prepare dedicated storage with an enforced quota, a stable
HTTPS endpoint, and encrypted backups with a tested restoration procedure.
The backup procedure uses age and an off-host recovery identity.

Developer machines connect outbound to the deployment. Compose binds the API
and optional gateway to loopback. Your ingress is the only off-host path.

## Deploy the API

Before building, review [release security evidence](security.md). On the
deployment host, check out the commit you want to deploy:

```bash
SEDIMENT_REVISION='<full commit hash>'
git clone https://github.com/sediment-ai/sediment.git sediment &&
  cd sediment &&
  git checkout --detach "$SEDIMENT_REVISION" &&
  test "$(git rev-parse HEAD)" = "$SEDIMENT_REVISION"
```

Run the remaining host commands from this directory. If another Sediment stack
exists on this host, [choose a separate project and ports](#run-a-second-local-deployment)
before starting this one.

If `sediment server` is running on this host, stop it with Ctrl+C before using
Compose's default API port, `8000`. To keep both deployments running, choose a
different Compose API port. Compose creates its own PostgreSQL volume; it
doesn't reuse or import the local database under `~/.sediment/server`.

Create `.env` with separate generated credentials and owner-only permissions
before any secret reaches the file:

```bash
uv run --python 3.12.14 --no-project python scripts/create_deploy_env.py \
  --ingest-client alice-laptop --ingest-client bob-laptop
```

Replace the client names with your participants; repeat `--ingest-client` for
each machine. The generator writes a distinct ingest-only token for each client
and a separate gateway token. It refuses existing files and directories writable
by another user.
Keep `.env` private. Don't source it or distribute it to developers.

Edit these deployment settings in `.env`:

| Setting | Action |
| --- | --- |
| `SEDIMENT_ORG_ID` | Set the deployment's organization identifier. |
| `SEDIMENT_ALLOWED_CLONE_HOSTS` | Set the permitted Git hosts; the default is `["github.com"]`. |
| `SEDIMENT_DEV_MODE` | Keep `false`. |

Open `.env` in a private editor to retrieve each client's entry from
`SEDIMENT_INGEST_TOKENS`. Distribute only that entry's token through your
credential channel. Reserve `SEDIMENT_OPERATOR_TOKEN` for queries and reports.
The generator never prints secrets. To add a client after installation, follow
[credential rotation](#upgrade-the-deployment).

### Configure PostgreSQL

Keep the four generated database passwords in `.env`. The supplied
[`docker-compose.yml`](../../docker-compose.yml) uses them as follows:

| Setting | Database role | Used by |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | `sediment` | PostgreSQL initialization and the migration service's bootstrap connection. |
| `SEDIMENT_MIGRATOR_PASSWORD` | `sediment_migrator` | The migration service to own and migrate the schema. |
| `SEDIMENT_RUNTIME_PASSWORD` | `sediment_runtime` | The API to store and read Facts. |
| `SEDIMENT_OPERATOR_PASSWORD` | `sediment_operator` | The operator profile to run database checks, Derivations, exports, and quarantine operations. |

Compose sets the database name to `sediment` and connects services to
`postgres:5432` on the internal `database` network. You don't need to install
PostgreSQL on the host or publish port `5432`.

Compose builds `SEDIMENT_BOOTSTRAP_DATABASE_URL` for `migrate` and a separate
`SEDIMENT_DATABASE_URL` for each of `api` and `operator` from those passwords.
You don't need to add a connection URL to `.env`. Keep the generated hexadecimal
passwords; they are safe to include in these URLs. For an existing deployment,
follow [credential rotation](#upgrade-the-deployment) instead of replacing
passwords before a restart.

The `postgres` service stores data in the `sediment-postgres` named volume at
`/var/lib/postgresql/data`. Keep this volume when restarting or rebuilding.
LiteLLM sends captured Inference calls to `http://api:8000`; the API writes
them to PostgreSQL. The gateway doesn't receive database credentials.

### Start the API and database

Build and start the deployment with its source identity:

```bash
set -e
SEDIMENT_SOURCE_REVISION="$(git rev-parse HEAD)"
SEDIMENT_SOURCE_DIGEST="$(uv run --python 3.12.14 --no-project python scripts/security_image_assurance.py source-digest)"
export SEDIMENT_SOURCE_REVISION SEDIMENT_SOURCE_DIGEST
docker compose up --build --wait --wait-timeout 120
```

Wait for the API to become ready:

```bash
curl --retry 30 --retry-connrefused --retry-delay 2 --max-time 5 \
  -fsS http://127.0.0.1:8000/health
```

```text
{"status":"ok","version":"0.4.0"}
```

Compose waits for PostgreSQL to pass its health check, then runs `migrate` to
provision the database roles and apply migrations. It starts the API only after
`migrate` exits successfully. The start command returns success after PostgreSQL
and the API pass health checks.
The 120-second readiness limit starts after image building. If startup fails,
inspect the containers and logs before retrying the same start command:

```bash
docker compose ps -a
docker compose logs postgres migrate api
```

Database and mirror volumes survive image rebuilds and `docker compose down`.

### Run a second local deployment

Before its first start, set these values in the second checkout's private `.env`:

```dotenv
COMPOSE_PROJECT_NAME=sediment-evaluation
SEDIMENT_API_PORT=18080
SEDIMENT_GATEWAY_PORT=14000
```

The project name separates containers, volumes, networks, and image tags. Keep it
unchanged when restarting. Use `http://127.0.0.1:18080` for this stack's API checks
and login. Ports remain bound to loopback. The defaults are `sediment`, `8000`,
and `4000`; volume names in this guide assume those defaults.

## Expose a public HTTPS endpoint

If you operate external ingress, leave the `https` profile disabled.

Route your HTTPS API hostname to `http://127.0.0.1:8000` through a reverse
proxy or tunnel on the host. Keep the Compose ports bound to loopback. Preserve
request bodies and authorization headers, and rate-limit authentication attempts
at ingress; the API doesn't provide that limit.

If you use Cloudflare, follow its
[local tunnel setup](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/)
and [service installation](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/as-a-service/linux/).
Cloudflare carries requests through its network. If your data must stay within
your own network, use an internal proxy.

Remote clients use the HTTPS deployment root, such as
`https://sediment-api.example.com`. `sediment login` rejects embedded credentials,
query strings, fragments, and non-root paths. HTTP is allowed only on literal
loopback hosts.

## Configure capture

1. For private repositories, [configure read-only mirror credentials](#configure-private-mirrors).
2. [Configure GitHub webhooks](../capture/managed-capture.md#configure-push-and-ci-capture)
   for Pushes, pull requests, repository changes, and continuous integration (CI)
   outcomes.
3. Optional: [Enable bundled LiteLLM](#enable-bundled-litellm) or
   [connect an existing gateway](#connect-an-existing-litellm-gateway)
   for Inference calls. Keep the developer's selected model unchanged.

### Configure private mirrors

Before mounting private Git credentials, review the image and Git configuration
requirements in [Secure a deployment](security.md).
The supplied release evidence doesn't cover custom credential mounts.
Then mount a deployment-local `.netrc` through `docker-compose.override.yml`:

```yaml
services:
  api:
    volumes:
      - ~/.config/sediment/netrc:/home/sediment/.netrc:ro
```

Create the file privately with mode `0600`. Make it readable by the API
container's user without granting access to other host users:

```text
machine github.com login x-access-token password <fine-grained PAT>
```

Scope the personal access token (PAT) to **Contents: read-only** on the captured
repositories. Apply the mount with `docker compose up -d --no-deps api`. After
enrollment, push a test commit with a Session note. In
`docker compose logs --since 10m api`, require
`session_commit_observations_captured` with a nonzero stored or duplicate count
for that repository. Verify the commit with the
[forge check](run-pilot.md#verify-forge-delivery); a Push Fact alone
doesn't verify private Git access.

### Connect an existing LiteLLM gateway

Copy `litellm/sediment_callback.py` and
`cli/sediment_cli/delivery.py` next to the gateway configuration. Name the copied
helper `sediment_delivery.py`. Both files must be importable by the proxy. Register
the callback:

```yaml
litellm_settings:
  callbacks: sediment_callback.handler
```

Set these variables in the gateway process environment:

```bash
export SEDIMENT_INGEST_URL=https://sediment-api.example.com
export SEDIMENT_API_BEARER_TOKEN='<ingest-only token>'
```

Register the callback secret in the API's `SEDIMENT_INGEST_TOKENS` map.
The callback uses `SEDIMENT_API_BEARER_TOKEN` for ingest; don't give it an
operator token. After changing the map in `.env`, recreate the API with
`docker compose up -d --no-deps api`. Removing an entry revokes that client.

Use HTTPS for remote callback destinations. Loopback HTTP is accepted for
`localhost`, `127.0.0.0/8`, and `[::1]`. The callback rejects redirects. If the
callback and API share a trusted container network, set
`SEDIMENT_GATEWAY_LOCAL_HTTP_ORIGIN` to that one HTTP origin. The bundled Compose
profile uses `http://api:8000`. This exception matches the scheme, hostname, and
port exactly; it never authorizes OTLP delivery or another destination.

The callback uses five-second HTTP timeouts and preserves capture identity and
observation time across retries. Capture failures don't fail the model request.

If you agree to store the prepared payload on disk, set
`SEDIMENT_DELIVERY_DIR` to a private directory on persistent storage. The payload
can contain unredacted prompts, code, or credentials before server redaction.
The callback starts a replay worker for its process lifetime. Without this
setting, it reports `best_effort` and attempts direct delivery.
Unsafe, unavailable, or busy buffer storage also triggers one direct attempt
with a `best_effort` diagnostic. Repair the volume to restore durable recovery.
See [Preserve prepared payloads through outages](../capture/local-capture.md#preserve-prepared-payloads-through-outages)
for limits, permissions, and recovery commands.

If you collect raw fixtures for integration debugging, set `SEDIMENT_CAPTURE_DIR`
to an absolute private directory owned by the gateway user. This opt-in writes
unredacted prompts, responses, code, and possibly credentials. The callback
creates the directory with mode `0700` and both JSON files with mode `0600`.
It refuses permissive paths, foreign ownership, symlinks, and hardlinks.
An unsafe fixture destination logs `fixture_write_failed` without stopping
valid gateway delivery. Restrict access, use encrypted storage, and delete the
fixtures when the investigation ends. Basic redaction at the API doesn't protect
these local raw files.

Clients must carry a real Session identifier through metadata or a supported
protocol carrier. The API skips unresolved calls and logs
`gateway_ingest_skipped_no_session`. Upgrade the server before clients when
identity parsing changes.

Optional: to prune superseded tool output from model requests, follow
[Enable pruning in an existing LiteLLM gateway](../capture/managed-capture.md#enable-pruning-in-an-existing-litellm-gateway).

## Verify the deployment

For remote clients, check the public health endpoint. For Docker Desktop
evaluation, use the loopback health check from
[Start the API and database](#start-the-api-and-database):

```bash
curl -sf https://sediment-api.example.com/health
```

Run the operator profile to check database access and create its export and
staging volumes:

```bash
docker compose --profile operator run --rm operator sediment facts
docker compose ps -a postgres migrate api
```

`sediment facts` prints total and Derivation-visible rows. Zero counts are
expected before the first capture. Require a healthy API and PostgreSQL container
and a successful migration container (`Exited (0)`).

Inspect the PostgreSQL revision without changing it:

```bash
docker compose --profile operator run --rm operator sediment db status
```

Require `at_head`. Before enrollment, [create and restore a backup](#back-up-and-restore).
Record the revision, image identities, health result, database status, and
backup restore result. Then complete the
[team enrollment](run-pilot.md). Health and empty Fact counts
don't verify live capture; use the pilot's Session and forge checks for that.

Operator commands connect through the separate operator database role and don't
rerun provisioning. To stop and resume the same deployment, run
`docker compose down`, then `docker compose up --wait --wait-timeout 120`.
Keep `.env` and omit `--volumes` to retain credentials and Facts.

### Enable bundled LiteLLM

If you need the bundled Anthropic gateway, add `ANTHROPIC_API_KEY` to `.env`.
Keep the generated `LITELLM_MASTER_KEY` and gateway ingest token. The gateway
supports `claude-*` routing; other providers require a separate gateway
configuration. See the [gateway boundary](../../docker/gateway/README.md).
Agents authenticate with `LITELLM_MASTER_KEY`, which also administers the
gateway. [Distribute gateway routing](../capture/managed-capture.md#distribute-gateway-routing)
defines how agents reach it.

If you agree to store unredacted capture payloads on disk, set
`SEDIMENT_DELIVERY_DIR=/data/delivery/pending` in `.env`. The gateway uses the
`sediment-delivery` named volume and owns a replay worker for its process
lifetime. Leave the setting empty for direct best-effort delivery.

If you want the gateway to prune superseded tool output from model requests,
set `SEDIMENT_CONTEXT_PRUNE=supersede` in `.env`. Leave it empty to keep the
gateway a pass-through. See
[Prune superseded tool output](../capture/managed-capture.md#prune-superseded-tool-output).

From the deployment checkout, build and start the gateway with its source identity:

```bash
set -e
SEDIMENT_SOURCE_REVISION="$(git rev-parse HEAD)"
SEDIMENT_SOURCE_DIGEST="$(uv run --python 3.12.14 --no-project python scripts/security_image_assurance.py source-digest)"
export SEDIMENT_SOURCE_REVISION SEDIMENT_SOURCE_DIGEST
docker compose --profile gateway up --build --wait --wait-timeout 120
docker compose --profile gateway ps gateway
```

Compose checks gateway liveness. Also require a successful authenticated model
request and captured Inference call before declaring that path ready.
If you use external ingress, route its HTTPS gateway hostname to
`http://127.0.0.1:4000`. The bundled proxy instead serves the gateway at `/llm`
on the API hostname. The gateway receives provider and ingest credentials, but no
database credentials. Its callback sends Inference calls to `http://api:8000`.
A capture failure doesn't retract a successful model response.

If the volume is unsafe, unavailable, or busy, the callback attempts direct
delivery and logs the reason.
See [Preserve prepared payloads through outages](../capture/local-capture.md#preserve-prepared-payloads-through-outages)
for privacy, retention, capacity, and recovery limits. Upgrade the API before
the callback: a server without the capture envelope returns 422, which blocks
buffered entries until you upgrade and retry them.

To retry blocked entries after correcting the API or credentials, stop the
gateway so its callback releases the delivery lock. The one-off helper uses the
same configured volume, endpoint, and credentials:

```bash
docker compose --profile gateway stop gateway
docker compose --profile gateway run --rm --no-deps --entrypoint python gateway /app/sediment_delivery.py replay --retry-blocked
docker compose --profile gateway run --rm --no-deps --entrypoint python gateway /app/sediment_delivery.py status
```

If blocked entries remain, correct the reported failure and repeat the bounded
replay. When recovery is complete, restart the gateway and its automatic worker:

```bash
docker compose --profile gateway up -d --no-deps gateway
```

If the gateway exits, inspect `docker compose logs gateway`.

Use [Configure inference-call
capture](../capture/managed-capture.md#configure-inference-call-capture) to
route clients and verify both the model request path and the capture path.

### Enable agent-requested retrieval

Optional: set the retrieval settings from
[Enable agent-requested retrieval](resume-with-evidence.md#enable-agent-requested-retrieval)
in `.env`, and recreate the API with `docker compose up -d --no-deps api`.
Compose passes these settings only to the API. Before you upgrade an old
deployment, rename any ingest client named `retrieval`.

## Upgrade the deployment

Before upgrading, read `CHANGELOG.md`, review [release security evidence](security.md),
back up the database, and test restoration. Measure migration time on a restored
copy. Constraint validation and index builds can block table reads and writes;
schedule a maintenance window for large datasets.

Stop the API and gateway before changing database roles or credentials. Preserve
`POSTGRES_PASSWORD`: changing it in `.env` doesn't rotate an initialized server.

Choose the full hash of the commit you want to deploy. Pin the checkout to that
commit when upgrading, as you do for the first installation.

If your existing `.env` predates separate database roles and operator tokens,
prepare its replacement before running Compose against the updated checkout:

1. From the existing checkout, stop the API and gateway. Save the encrypted
   database backup and its restore record.
2. Update the checkout. Restrict the existing credential file before opening it,
   then generate a separate private candidate:

   ```bash
   SEDIMENT_REVISION='<full commit hash to deploy>'
   git fetch --tags origin &&
     git checkout --detach "$SEDIMENT_REVISION" &&
     test "$(git rev-parse HEAD)" = "$SEDIMENT_REVISION" &&
     chmod 600 .env &&
     uv run --python 3.12.14 --no-project python scripts/create_deploy_env.py --output .env.next
   ```

3. In a private editor, copy the existing `POSTGRES_PASSWORD`, `SEDIMENT_ORG_ID`,
   webhook secret, clone-host policy, provider key, and gateway master key into
   `.env.next`. Keep the agreed delivery-retention settings. Keep the
   generated migrator, runtime, operator database passwords, operator HTTP token,
   gateway ingest token, and matching named ingest map. If existing clients use
   `SEDIMENT_API_BEARER_TOKEN`, retain that value as an ingest-only compatibility
   credential. Keep `SEDIMENT_DEV_MODE=false`.
4. Replace the old file with `mv .env.next .env`. The candidate is mode 0600
   throughout.
5. Rerun the build and start commands from [Deploy the API](#deploy-the-api).
   If provisioning rejects unknown ownership or objects, investigate them
   instead of granting the API bootstrap authority. Do not start the old API
   against the confined roles.
6. After the API is healthy, reenroll operator clients with the operator token.
   Old capture clients can retain the compatibility ingest token until you
   migrate them.

For a deployment already using separate credentials, preserve its private
`.env` and use the same update sequence:

```bash
set -e
SEDIMENT_REVISION='<full commit hash to deploy>'
git fetch --tags origin
docker compose --profile gateway stop gateway api
git checkout --detach "$SEDIMENT_REVISION"
test "$(git rev-parse HEAD)" = "$SEDIMENT_REVISION"
SEDIMENT_SOURCE_REVISION="$(git rev-parse HEAD)"
SEDIMENT_SOURCE_DIGEST="$(uv run --python 3.12.14 --no-project python scripts/security_image_assurance.py source-digest)"
export SEDIMENT_SOURCE_REVISION SEDIMENT_SOURCE_DIGEST
docker compose up --build --force-recreate --wait --wait-timeout 120 postgres migrate api
curl --retry 30 --retry-connrefused --retry-delay 2 --max-time 5 \
  -fsS http://127.0.0.1:8000/health
```

If you use the gateway, rebuild and start that profile after the API is healthy.
Run fresh scans against the images you built; release evidence for different
image identities doesn't attest to your local build.

To rotate runtime, migrator, or operator database credentials, stop the API and
gateway, replace the corresponding private `.env` values, and rerun provisioning
before recreating their consumers. To rotate an ingest token, replace only that
entry in `SEDIMENT_INGEST_TOKENS`, update its enrolled client, and recreate the
API with `docker compose up --wait --wait-timeout 120 api`. To enroll another
client, generate a distinct token with
`uv run --no-project python -c 'import secrets; print(secrets.token_hex(32))'`,
add it under a unique name in `SEDIMENT_INGEST_TOKENS`, then recreate the API
with the same command. Keep existing entries; don't regenerate `.env`.
Client names `operator`, `legacy`, and `retrieval` are reserved.
The gateway's token must match its named map entry. Rotate the operator
HTTP token independently. Remove `SEDIMENT_API_BEARER_TOKEN` after migrating
legacy capture clients; that token grants ingest authority only.

Upgrade the API before gateway callbacks and capture clients. Verify
`sediment db status` reports `at_head` before resuming capture.

For `0011_inference_call_aliases`, stop every API replica and direct Fact writer
before provisioning. The migration copies provider and output tool-call
identifiers from every retained Inference call, including quarantined history,
and builds the physical lookup index in one transaction. It blocks parent reads and writes
and decodes one output row at a time; memory depends on the largest output and
its identifiers. Measure duration and disk growth on a restored database before
scheduling the maintenance window. Restart only the matching API build after
provisioning succeeds. Stale writers that omit the physical alias count fail
instead of creating unindexed Facts. See [Indexed call identifiers](../adr/0023-indexed-call-identifiers.md).

## Operating cadence

Each week, inspect storage usage, review failed scans and upstream fixes, and
check Fact growth and outcomes:

```bash
docker compose --profile operator run --rm operator sediment facts
docker compose --profile operator run --rm operator sediment report model
```

Apply security fixes within your deployment's update deadlines. Track the
support end date in `security-support.json` and the review expiries in the
[release evidence](security.md).

### Back up and restore

On a separate recovery machine, generate an [age identity](https://github.com/FiloSottile/age).
Keep its private key off the deployment host. Put only its public recipient in
`BACKUP_RECIPIENT` on that host. In Bash, stream a PostgreSQL custom archive
directly into encryption:

```bash
(
  set -euo pipefail
  umask 077
  install -d -m 700 "$HOME/sediment-backups"
  backup="$HOME/sediment-backups/sediment-$(date -u +%Y%m%dT%H%M%SZ).dump.age"
  set -o noclobber
  if ! docker compose exec -T postgres sh -ec \
    'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump -U sediment -d sediment --format=custom' \
    | age --recipient "${BACKUP_RECIPIENT:?Set the recovery public recipient}" > "$backup"; then
    echo 'Backup failed; inspect and remove the incomplete encrypted file.' >&2
    exit 1
  fi
  test -s "$backup"
  ls -l "$backup"
)
```

The file has mode 0600 from creation. An existing filename causes failure,
including a symbolic link. The pipeline leaves no plaintext archive on disk.
Copy the encrypted file off-host under your retention policy. Treat a failed
pipeline as an incomplete backup. [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)
takes a consistent database snapshot; it doesn't include git mirrors or exports.

On the recovery machine, create a separate empty disposable PostgreSQL
database. Use the Sediment source revision that produced the backup. Decrypt into `pg_restore --exit-on-error --no-owner
--no-privileges --dbname <disposable-database>` through a pipe with `pipefail`.
Use that database's private credential file instead of a password in arguments.
Restore into an empty database before running role provisioning; the latter
adopts restored known objects and reapplies the grants. Verify the schema
revision, Fact counts, quarantine log, and a representative export against the
backup record. Destroy only that disposable test database after verification.
Follow [pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html) for the
archive and connection options. Record the backup timestamp, restore result,
and recovery duration. Test restoration before enrollment and after
schema or backup-tool changes.

If you enable the bundled gateway, review unresolved completion identity:

```bash
docker compose logs api | grep gateway_ingest_skipped_no_session
```

Each line names an inference call that arrived without a resolvable Session id.

## Privacy and data handling

[Secure a deployment](security.md) describes what Sediment stores, how the
credentials divide authority, and the network paths. This section covers what
differs under Compose.

### Where data lives

| Volume | Contents |
| --- | --- |
| `sediment-postgres` | Facts and schema |
| `sediment-mirror` | Bare Git mirrors |
| `sediment-export` | Operator-created exports |
| `sediment-certificates` | HTTPS certificate private keys and renewal state |
| `sediment-staging` | Private, disposable Derivation and export payloads; may contain unredacted content |
| `sediment-delivery` | Optional unredacted gateway retries |

Operator query routes return captured content. Optional retrieval access returns
content from its authorized Sessions. Capture credentials don't authorize reads.
Host administrators can read container environments and mounted data.

Facts and mirrors have no automatic retention or expiry. Raw mirrors, optional
capture files, and encrypted backups remain sensitive even when Basic redaction
protects normalized Fact content. Authorize any raw capture retention explicitly.

Put Docker data and export storage on a dedicated filesystem or enforce a
volume-driver quota before deployment. Record its size and alert before
80% use. The mirror worker requires 1 GiB of free space, but doesn't enforce a
quota. Budget for backups and incident recovery.

### Quarantine and wholesale deletion

Quarantine excludes a Fact from every Derivation and export without changing
the Fact row. An append-only audit log records each quarantine and release.

Bulk inference-call quarantine dry-runs unless you pass `--apply`:

```bash
docker compose --profile operator run --rm operator sediment quarantine-inference-calls \
  --session-id sess-smoke --reason 'smoke-test facts'
docker compose --profile operator run --rm operator sediment quarantine-inference-calls \
  --session-id sess-smoke --reason 'smoke-test facts' --apply
```

Quarantine other Fact tables by Fact id:

```bash
docker compose --profile operator run --rm operator sediment quarantine pushes '<fact_id>' --reason '...'
docker compose --profile operator run --rm operator sediment release pushes '<fact_id>' --reason '...'
```

`sediment facts` shows the visible count. `sediment quarantine-log` shows the
audit trail and the quarantine-state Provenance value.

[Quarantine captured data](maintain.md#quarantine-captured-data) explains the
effect of a partial Edit observation quarantine.

To delete the deployment data, remove its Compose volumes. **This deletes Facts,
mirrors, exports, staging, and buffered deliveries. You can't undo it.** Save
any required database backup and exports first:

```bash
docker compose --profile gateway --profile https --profile operator down --volumes
```

To remove only the mirror, stop the stack, remove its volume, and restart:

```bash
docker compose down
docker volume rm sediment_sediment-mirror
docker compose up --wait --wait-timeout 120
```

The API recreates mirrors after later push webhooks.

### Network exposure

| Direction | Connection |
| --- | --- |
| Inbound | API on `127.0.0.1:8000`; gateway on `127.0.0.1:4000`. The `https` profile exposes proxy ports 80 and 443; external ingress supplies access otherwise. |
| Outbound from API | Git fetches to permitted clone hosts when mirroring is enabled. |
| Outbound from gateway | Model requests to Anthropic and capture callbacks to the API over the Compose network. |

An empty `SEDIMENT_ALLOWED_CLONE_HOSTS` list admits public hosts only.

To disable mirror fetches in this deployment, remove `SEDIMENT_MIRROR_PATH` from
the API's `environment` in `docker-compose.yml` and recreate the API. Compose
sets it directly; unsetting it in `.env` has no effect.

PostgreSQL has no published port. Only provisioning, API, and operator services
join its internal network. Local and TCP connections require password
authentication through SCRAM (Salted Challenge Response Authentication Mechanism).
The gateway has no database-network access.

Compose applies read-only roots, temporary-storage and resource limits, rotated
logs, and `no-new-privileges`. The API and gateway have no Linux capabilities.
PostgreSQL keeps only the capabilities required to initialize volume ownership
and drop privileges.

Keep the single API process unless you recalculate host and database capacity.
It permits two active reads, two mirror workers, and sixteen waiting mirror
jobs. Queries and reports share both read slots; a third read returns 503
without queueing. Read and mirror deadlines are 30 and 120 seconds. Memory
limits are 2 GiB for the API, 1 GiB for PostgreSQL, and 2 GiB for the gateway.
Reserve capacity for operator and migration jobs.

Review [the disposition register](../../security/dispositions.json) before a
deployment. Database isolation and resource limits reduce exposure to
unresolved native parser vulnerabilities. They don't remove vulnerable code or
protect stored Facts after a database-process compromise. A custom network, SQL
client, image, Git configuration, or credential distribution needs another
review.

For operation without public network access, provision software, container
images, and dependencies inside your network.

## Troubleshooting

- **Image builds or pulls hang:** check Docker registry access and the configured
  credential helper.
- **`/health` responds but other requests reach the wrong process:** inspect
  `docker ps`. Another container may own port 8000.
- **Ingest returns `503 database_unavailable`:** restore PostgreSQL access, then
  retry the retained request bytes. The same running service can reconnect.
  Committed Facts retain their original stored identities on duplicate delivery.
  Each Fact-type batch commits its Facts and Session rows together; one ingest
  operation can contain several batches. Database deduplication doesn't guarantee
  delivery of events that a sender never retained or sent.
- **The API doesn't start after PostgreSQL becomes healthy:** read
  `docker compose logs migrate api`. Run
  `docker compose --profile operator run --rm operator sediment db status` to distinguish an
  absent, behind, at-head, or ahead revision without changing it. Startup
  diagnostics name only the sanitized database target and never credentials.
- **Compose recreates another checkout's containers:** both checkouts selected
  the same project name. Keep the original deployment's `.env` intact. Set a
  distinct project name and ports in the evaluation checkout before starting it.
- **The bundled gateway exits during startup:** run
  `docker compose logs gateway`. Its entrypoint names a missing
  `ANTHROPIC_API_KEY` or `LITELLM_MASTER_KEY` before LiteLLM starts.
- **An agent receives no model response through the bundled gateway:** inspect
  `docker compose logs gateway` for client authentication, model routing, or
  Anthropic errors. The Sediment callback doesn't run until a model call
  succeeds.
- **An agent receives a response but no Inference call appears:** inspect
  `docker compose logs gateway api`. A callback authentication or ingest
  failure doesn't retract the successful model response.

Capture-path failures belong in [Configure local capture](../capture/local-capture.md#repair-or-recover-capture)
or [Roll out managed capture](../capture/managed-capture.md#verify-the-rollout).

## Teardown

1. Use [Uninstall capture](../capture/local-capture.md#uninstall-capture) on
   developer machines that received a local install.
2. Follow [Remove managed capture](../capture/managed-capture.md#remove-managed-capture)
   to remove fleet hooks, gateway callbacks, telemetry, and forge webhooks.
3. If you need the dataset, create and extract the backup from
   [Operating cadence](#operating-cadence).
4. Run `docker compose --profile gateway --profile https --profile operator down --volumes` on the host.
5. Remove the ingress routes and DNS records that served this deployment.

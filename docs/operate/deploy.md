# Deploy Sediment

Run the API, PostgreSQL Fact store, and Git mirror with Docker Compose on a
Linux virtual machine or Docker Desktop on macOS. For a local Docker evaluation,
complete sections 1, 2, and 5. For a shared pilot, complete sections 1–5 before
[enrolling developers](run-pilot.md). For evaluation without Docker, use the
[Quickstart](../quickstart.md).

## 1. Prerequisites

You need:

- a maintained host with at least 4 vCPUs and 8 GB of memory; allocate these
  resources to Docker Desktop's virtual machine when using it
- Docker Desktop or Docker Engine with Docker Compose
- Git, curl, uv, and the approved full Sediment commit hash

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

## 2. Deploy the API

Before building, review [release security evidence](security.md). On the
deployment host, check out the approved revision:

```bash
SEDIMENT_REVISION='<approved full commit hash>'
git clone https://github.com/sediment-ai/sediment.git sediment || exit 1
cd sediment || exit 1
git checkout --detach "$SEDIMENT_REVISION" || exit 1
test "$(git rev-parse HEAD)" = "$SEDIMENT_REVISION" || exit 1
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
[credential rotation](#6-upgrade-the-deployment).

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
follow [credential rotation](#6-upgrade-the-deployment) instead of replacing
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
{"status":"ok","version":"0.2.0"}
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

## 3. Expose a public HTTPS endpoint

Route your HTTPS API hostname to `http://127.0.0.1:8000` through a reverse
proxy or tunnel on the host. Keep the Compose ports bound to loopback. Preserve
request bodies and authorization headers, and rate-limit authentication attempts
at ingress; the API doesn't provide that limit.

If you use Cloudflare, follow its
[local tunnel setup](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/)
and [service installation](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/as-a-service/linux/).
Cloudflare carries requests through its edge. If that boundary is outside your
approved perimeter, use internal ingress.

Remote clients use the HTTPS deployment root, such as
`https://sediment-api.example.com`. `sediment login` rejects embedded credentials,
query strings, fragments, and non-root paths. HTTP is allowed only on literal
loopback hosts.

## 4. Configure capture

1. For private repositories, [configure read-only mirror credentials](../capture/managed-capture.md#configure-repository-mirrors).
2. [Configure GitHub webhooks](../capture/managed-capture.md#configure-push-and-ci-capture)
   for Pushes, pull requests, repository changes, and continuous integration (CI)
   outcomes.
3. Optional: [Enable bundled LiteLLM](#enable-bundled-litellm) or
   [connect an existing gateway](../capture/managed-capture.md#connect-an-existing-litellm-gateway)
   for Inference calls. Keep the developer's selected model unchanged.

## 5. Verify the deployment

For remote clients, check the public health endpoint. For Docker Desktop
evaluation, use the loopback health check from section 2:

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
[pilot handoff](run-pilot.md#prepare-the-deployment). Health and empty Fact counts
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

If you authorize persistent storage of unredacted capture payloads, set
`SEDIMENT_DELIVERY_DIR=/data/delivery/pending` in `.env`. The gateway uses the
`sediment-delivery` named volume and owns a replay worker for its process
lifetime. Leave the setting empty for direct best-effort delivery.

From the deployment checkout, build and start the gateway with its source identity:

```bash
set -e
SEDIMENT_SOURCE_REVISION="$(git rev-parse HEAD)"
SEDIMENT_SOURCE_DIGEST="$(uv run --python 3.12.14 --no-project python scripts/security_image_assurance.py source-digest)"
export SEDIMENT_SOURCE_REVISION SEDIMENT_SOURCE_DIGEST
docker compose --profile gateway up --build --wait --wait-timeout 120
docker compose --profile gateway ps gateway
```

The gateway has no Compose health check; require a successful authenticated model
request and captured Inference call before declaring that path ready.
Route a separate HTTPS gateway hostname to `http://127.0.0.1:4000` through
your ingress. The gateway receives provider and ingest credentials, but no
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

Optional: in private `.env`, set a distinct printable ASCII
`SEDIMENT_RETRIEVAL_TOKEN` of at least 24 characters and exactly one source setting:

| Setting | Grant |
| --- | --- |
| `SEDIMENT_RETRIEVAL_SESSION_ID` | One actual Session ID |
| `SEDIMENT_RETRIEVAL_SESSION_IDS` | JSON array of 1–32 unique Session IDs, within 16 KiB |

Recreate the API with `docker compose up -d --no-deps api`. Rotate the token when
the grant changes; the API cannot detect reuse across restarts. To revoke access,
remove the token and source setting, then recreate the API. Empty values are
invalid, including in development mode. Compose passes these settings only to
the API.

The grant includes future Facts and doesn't establish repository ownership.
Follow [Continue a task with captured evidence](resume-with-evidence.md) for
agent configuration and source limits. Before upgrading an old deployment,
rename any ingest client named `retrieval`; its secret doesn't become a read token.

## 6. Upgrade the deployment

Before upgrading, read `CHANGELOG.md`, review [release security evidence](security.md),
back up the database, and test restoration. Measure migration time on a restored
copy. Constraint validation and index builds can block table reads and writes;
schedule a maintenance window for large datasets.

Stop the API and gateway before changing database roles or credentials. Preserve
`POSTGRES_PASSWORD`: changing it in `.env` doesn't rotate an initialized server.

Choose the approved successor's full commit hash. Use a pinned checkout for
upgrades as well as first installation.

If your existing `.env` predates separate database roles and operator tokens,
prepare its replacement before running Compose against the updated checkout:

1. From the existing checkout, stop the API and gateway. Save the encrypted
   database backup and its restore record.
2. Update the checkout. Restrict the existing credential file before opening it,
   then generate a separate private candidate:

   ```bash
   SEDIMENT_REVISION='<approved successor full commit hash>'
   git fetch --tags origin || exit 1
   git checkout --detach "$SEDIMENT_REVISION" || exit 1
   test "$(git rev-parse HEAD)" = "$SEDIMENT_REVISION" || exit 1
   chmod 600 .env
   uv run --python 3.12.14 --no-project python scripts/create_deploy_env.py --output .env.next
   ```

3. In a private editor, copy the existing `POSTGRES_PASSWORD`, `SEDIMENT_ORG_ID`,
   webhook secret, clone-host policy, provider key, and gateway master key into
   `.env.next`. Preserve authorized delivery-retention settings. Keep the
   generated migrator, runtime, operator database passwords, operator HTTP token,
   gateway ingest token, and matching named ingest map. If existing clients use
   `SEDIMENT_API_BEARER_TOKEN`, retain that value as an ingest-only compatibility
   credential. Keep `SEDIMENT_DEV_MODE=false`.
4. Replace the old file with `mv .env.next .env`. The candidate is mode 0600
   throughout.
5. Rerun the build and start commands from [Deploy the API](#2-deploy-the-api).
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
SEDIMENT_REVISION='<approved successor full commit hash>'
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

## 7. Operating cadence

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

## 8. Privacy and data handling

### 8.1 What Sediment stores

The Fact store can contain model inputs and outputs, gateway payloads, patch
arguments, applied edit text, and Session-end file content. Mirrors contain
pushed Git history. Attribution notes contain Session identifiers and timestamps.

Basic redaction replaces recognized credentials before Fact storage. It isn't
comprehensive secret detection and doesn't rewrite existing Facts. Quarantine
Facts that contain credentials. Review the
[per-source privacy boundaries](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
before enabling capture.

### 8.2 Where data lives

| Volume | Contents |
| --- | --- |
| `sediment-postgres` | Facts and schema |
| `sediment-mirror` | Bare Git mirrors |
| `sediment-export` | Operator-created exports |
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

### 8.3 Quarantine and wholesale deletion

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

If you quarantine some Edit observations for a file in a Session, exclude that
file and Session's external-change diagnostics from comparisons. Record the
organization, harness, Session, file, and quarantine revision. Aggregates that
include them can understate totals without a partial-coverage flag; recomputation
doesn't repair missing windows. Retain quarantine until its original reason is
resolved. See [External edit windows](../explanation/how-capture-works.md#external-edit-windows).

To delete the deployment data, remove its Compose volumes. **This deletes Facts,
mirrors, exports, staging, and buffered deliveries. You cannot undo it.** Save
any required database backup and exports first:

```bash
docker compose --profile gateway --profile operator down --volumes
```

To remove only the mirror, stop the stack, remove its volume, and restart:

```bash
docker compose down
docker volume rm sediment_sediment-mirror
docker compose up --wait --wait-timeout 120
```

The API recreates mirrors after later push webhooks.

### 8.4 Network exposure

| Direction | Connection |
| --- | --- |
| Inbound | API on `127.0.0.1:8000`; optional gateway on `127.0.0.1:4000`. Ingress provides remote access. |
| Outbound from API | Git fetches to permitted clone hosts when mirroring is enabled. |
| Outbound from gateway | Model requests to Anthropic and capture callbacks to the API over the Compose network. |

Capture requires an ingest token or webhook signature. Reports and operator
queries require an operator token. Optional context routes accept retrieval or
operator credentials within the configured Session grant. Exact context evidence
reads can include reasoning and parts omitted by keyword selection. See
[Continue a task with captured evidence](resume-with-evidence.md).
`GET /health` is unauthenticated and returns no captured content. Request bodies
have size limits.

Treat each allowed clone host as a fetch trust decision. An explicit entry can
authorize a private address; an empty list admits public hosts only. Preserve
the reviewed Git configuration and proxy/header environment settings.

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
deployment. Database isolation and resource limits reduce exposure
to unresolved native parser vulnerabilities; they don't remove vulnerable code
or protect stored Facts after database-process compromise. A custom network,
SQL client, image, Git configuration, or credential distribution needs another
review. These controls don't establish Cyber Essentials certification.

Sediment has no telemetry, analytics, crash reporting, update checks, or license
validation. Exports are local JSONL files.

For operation without public network access, provision software, container
images, and dependencies inside the perimeter. Use internal ingress, Git remotes,
and model endpoints. The model endpoint determines where inference content goes;
an internal gateway alone doesn't keep that content inside the perimeter.

## 9. Troubleshooting

- **Image builds or pulls hang:** check Docker registry access and the configured
  credential helper.
- **`/health` responds but other requests reach the wrong process:** inspect
  `docker ps`. Another container may own port 8000.
- **Ingest returns `503 database_unavailable`:** restore PostgreSQL access, then
  retry the retained request bytes. The same running service can reconnect.
  Committed Facts retain their original stored identities on duplicate delivery.
  Each Fact-type batch commits its Facts and Session rows together; one ingest
  operation can contain several batches. Database deduplication does not guarantee
  delivery of events that a sender never retained or sent.
- **The API does not start after PostgreSQL becomes healthy:** read
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

## 10. Teardown

1. Use [Uninstall capture](../capture/local-capture.md#uninstall-capture) on
   developer machines that received a local install.
2. Follow [Remove managed capture](../capture/managed-capture.md#remove-managed-capture)
   to remove fleet hooks, gateway callbacks, telemetry, and forge webhooks.
3. If you need the dataset, create and extract the backup from
   [Operating cadence](#7-operating-cadence).
4. Run `docker compose --profile gateway --profile operator down --volumes` on the host.
5. Remove the ingress routes and DNS records that served this deployment.

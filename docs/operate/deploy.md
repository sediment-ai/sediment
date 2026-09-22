# Deploy Sediment

Use this guide to run the Sediment API, PostgreSQL Fact store, and git mirror
on a host. Every step states what it exposes and how
to verify it.

The deployment has PostgreSQL, a one-shot provisioning service, the API, an
operator profile, and named volumes. It doesn't include an LLM gateway unless you enable the
optional LiteLLM compose profile.

Sediment consumes completion callbacks after an LLM response. It doesn't sit in
the request path or share the gateway's availability boundary.

For the capture paths that feed the deployment, use
[Roll out managed capture](../capture/managed-capture.md). For developer
machines, use [Configure local capture](../capture/local-capture.md).

## 1. Prerequisites

You need:

- a maintained host with at least 4 vCPUs, 8 GB of memory, Docker Engine,
  and Compose v2
- dedicated storage with an enforced quota, encrypted backups, and a tested
  restoration procedure
- a checkout of `https://github.com/sediment-ai/sediment.git`
- a stable HTTPS endpoint for remote clients and forge webhooks
- uv for the private environment-file generator and release checks
- a maintained age installation and an off-host recovery identity for backups

Developer machines connect outbound to the deployment. Compose binds the API
and optional gateway to loopback. Your ingress is the only off-host path.

## 2. Deploy the API

For local evaluation, `sediment server` downloads and starts PostgreSQL under
`~/.sediment/server`. The [Quickstart](../quickstart.md) shows that setup. Use
`--root PATH` to keep its database, credentials, binaries, logs, and mirror in
another directory.

If you supply `SEDIMENT_BOOTSTRAP_DATABASE_URL`, `sediment server` provisions
that external database instead. Use a dedicated PostgreSQL database with an
explicit host and database in the URL. The command creates separate database
roles, applies migrations, and derives the API's restricted runtime connection.
It stores generated role passwords and API credentials in `server.env` under
its root. It doesn't start or stop the external database.

This runbook uses Docker Compose for a durable host; the images include their
database client library.

Create `.env` with separate generated credentials and owner-only permissions
before any secret reaches the file:

```bash
cd sediment
uv run --python 3.12.14 --no-project python scripts/create_deploy_env.py
```

The generator refuses an existing file or a directory writable by another user.
It generates separate bootstrap, migrator, API-runtime, and operator database
passwords, an operator HTTP token, a named gateway ingest token, a webhook
secret, and a gateway master key. It doesn't print the secrets. Keep `.env`
private; don't source it into a shell or copy it to developer machines.

If you hand-edit `.env` instead of running the generator, production still
refuses an operator token, ingest token, or webhook secret shorter than 24
characters. The API
applies no rate limiting to authentication attempts. Front it with a reverse
proxy or tunnel that throttles repeated failures.

Compose passes each service only its required credentials. The provisioning
service receives bootstrap authority and the three role passwords. The API
receives only its restricted database URL, operator HTTP token, ingest token
map, webhook secret, and optional retrieval settings. The operator profile receives its own database URL.
The gateway receives an ingest token and provider credentials. It has no
database credentials or database-network membership.

If you enable agent-requested retrieval, add both `SEDIMENT_RETRIEVAL_TOKEN` and
`SEDIMENT_RETRIEVAL_SESSION_ID` to the private `.env`. Use a distinct printable
ASCII token of at least 24 characters. Use the actual source Session identifier.
The API validates this pair even in development mode. Leave both unset to disable
retrieval; empty values are invalid. Compose passes the pair only to the API.
Restart the API after changing either setting. Rotate the token when changing
the Session. Removing both settings and restarting revokes access.
If an ingest client is named `retrieval`, rename that entry before upgrading.
The identifier is reserved; its old secret isn't reclassified.

The authoritative API settings loader is `apps/api/sediment_api/config.py`.
If a required setting is absent, Compose stops and names it.

Set `SEDIMENT_ORG_ID` for the deployment tenancy. If the mirror may clone only
specific hosts, set `SEDIMENT_ALLOWED_CLONE_HOSTS` to those hostnames.

Review the release evidence in [Check release and deployment security](security.md).
Record the source identity, then build and start the API:

```bash
set -e
SEDIMENT_SOURCE_REVISION="$(git rev-parse HEAD)"
SEDIMENT_SOURCE_DIGEST="$(uv run --python 3.12.14 --no-project python scripts/security_image_assurance.py source-digest)"
export SEDIMENT_SOURCE_REVISION SEDIMENT_SOURCE_DIGEST
docker compose up -d --build
```

Verify it from the host:

```bash
curl -s http://127.0.0.1:8000/health
```

```text
{"status":"ok","version":"0.1.0"}
```

Compose waits for PostgreSQL health, runs `sediment db provision`, and starts
the API after provisioning succeeds. Provisioning creates and confines the three
roles, adopts known legacy Sediment objects, and migrates under an advisory lock.
It preserves Fact rows. The API checks the exact schema revision and its
effective runtime privileges before starting workers. It refuses bootstrap or
excess database authority and invalid production credentials. If startup fails, read
`docker compose logs postgres migrate api`.

Runtime Facts and the physical schema live in `sediment-postgres`. Bare
repository mirrors live in
`sediment-mirror`. These volumes survive image rebuilds and
`docker compose down`.

### Enable bundled LiteLLM

If the deployment needs the optional gateway on-ramp, set these values in
`.env`:

```bash
ANTHROPIC_API_KEY=<upstream provider key>
LITELLM_MASTER_KEY=<key presented by agent clients>
```

The environment generator supplies the master key with an `sk-` prefix and a
named ingest credential for the callback. Add only the Anthropic provider key.
The supplied gateway supports Anthropic routing; other providers and gateway
database features need a separately maintained deployment. See the
[gateway boundary](../../docker/gateway/README.md).

Start the complete profile:

```bash
docker compose --profile gateway up -d --build
docker compose --profile gateway ps gateway
```

The gateway entrypoint refuses to start if either key is absent. Compose passes
the Anthropic key and LiteLLM master key only to the gateway container. The
profile mounts `litellm/config.yaml`, `litellm/sediment_callback.py`, and the shared
`cli/sediment_cli/delivery.py` helper read-only. It binds LiteLLM to
`127.0.0.1:4000`. The callback uses the exact local HTTP exception `http://api:8000` over the
Compose edge network. It rejects other remote plaintext destinations and
authenticated redirects.

If you authorize persistent storage of unredacted capture payloads, set
`SEDIMENT_DELIVERY_DIR=/data/delivery/pending` in `.env`. The gateway uses the
`sediment-delivery` named volume and owns a replay worker for its process
lifetime. Leave the setting empty for direct best-effort delivery.
If the volume is unsafe, unavailable, or busy, the callback attempts direct
delivery and logs the storage reason; repair the volume to restore buffering.
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

If the service doesn't remain running, inspect its startup and provider logs:

```bash
docker compose logs gateway
```

Use [Configure inference-call
capture](../capture/managed-capture.md#configure-inference-call-capture) to
route clients and verify both the model request path and the capture path.

## 3. Expose a public HTTPS endpoint

Any reverse proxy or tunnel can front the loopback-bound API. This example uses
one named Cloudflare tunnel. Add a second hostname only when you enable the
bundled gateway.

Create the tunnel and DNS routes:

```bash
cloudflared tunnel login
cloudflared tunnel create sediment
cloudflared tunnel route dns sediment sediment-api.example.com
cloudflared tunnel route dns sediment sediment-llm.example.com   # gateway only
```

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /home/<user>/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: sediment-api.example.com
    service: http://127.0.0.1:8000
  - hostname: sediment-llm.example.com
    service: http://127.0.0.1:4000
  - service: http_status:404
```

Replace both `<TUNNEL_ID>` values with the UUID that `cloudflared tunnel create`
printed. Use the absolute credentials path from the same command. If the
connector runs as root, that path starts with `/root/.cloudflared/`.

Validate the ingress rules before you install the connector:

```bash
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://sediment-api.example.com
```

On Linux, install and start the connector with the config path that belongs to
the login user:

```bash
sudo cloudflared --config /home/<user>/.cloudflared/config.yml service install
sudo systemctl start cloudflared
curl -s https://sediment-api.example.com/health
```

Remote CLI clients must use this HTTPS deployment root. `sediment login`
accepts HTTP only for `localhost`, an address in `127.0.0.0/8`, or `[::1]`.
It rejects embedded credentials, query strings, fragments, and non-root base
paths before it reads or sends a bearer token.

The Zero Trust dashboard can create the same tunnel and connector token. The
API doesn't depend on Cloudflare; an internal ingress works for an on-premises
forge and gateway.

## 4. Configure capture

The deployment stores no Facts until you connect capture sources. Configure
them in this order:

1. Use [Roll out managed capture](../capture/managed-capture.md) to connect a
   gateway, forge webhooks, private mirrors, and any fleet distribution.
2. Use [Configure local capture](../capture/local-capture.md) on each developer
   machine that isn't covered by the fleet bundle.

The server must understand a capture contract before clients send it. Roll out
server changes before gateway callbacks, shims, or fleet configuration.

[How capture works](../explanation/how-capture-works.md) explains which signal
each source records and how absent paths degrade.

## 5. Verify the deployment

Check the public health endpoint:

```bash
curl -sf https://sediment-api.example.com/health
```

Check the container and persistent volumes:

```bash
docker compose ps
docker volume inspect \
  sediment_sediment-postgres \
  sediment_sediment-mirror \
  sediment_sediment-export \
  sediment_sediment-staging
```

After you configure at least one capture path, inspect the Fact tables:

```bash
docker compose --profile operator run --rm operator sediment facts
```

The Fact-count command prints total and Derivation-visible rows. A configured
source that stays at zero needs capture-path diagnosis, not API redeployment.
Use the verification section in the matching capture guide.

Inspect the PostgreSQL revision without changing it:

```bash
docker compose --profile operator run --rm operator sediment db status
```

The status command reports `at_head` after Compose starts successfully.

## 6. Upgrade the deployment

Compose builds the API, PostgreSQL, and optional gateway from reviewed,
digest-pinned inputs. The API uses plain Psycopg and Debian-maintained libpq.
It doesn't bundle the binary driver's separate OpenSSL libraries. The runtime
excludes uv and installer wheels. PostgreSQL retains its Bookworm data layout
and locale and uses maintained `setpriv` for its privilege drop. The bundled
gateway's [documented boundary](../../docker/gateway/README.md) lists supported
providers, removed components, and guarded vendor patches.

Before an upgrade, inspect [release security evidence](security.md), back up the
database, and test restoration. Stop the API and gateway before changing roles
or credentials. Keep the existing bootstrap password for an existing PostgreSQL
volume: changing `POSTGRES_PASSWORD` alone doesn't rotate an initialized server.

If your existing `.env` predates separate database roles and operator tokens,
prepare its replacement before running Compose against the updated checkout:

1. From the existing checkout, stop the API and gateway. Save the encrypted
   database backup and its restore record.
2. Update the checkout. Restrict the existing credential file before opening it,
   then generate a separate private candidate:

   ```bash
   git pull --ff-only
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
docker compose --profile gateway stop gateway api
git pull --ff-only
set -e
SEDIMENT_SOURCE_REVISION="$(git rev-parse HEAD)"
SEDIMENT_SOURCE_DIGEST="$(uv run --python 3.12.14 --no-project python scripts/security_image_assurance.py source-digest)"
export SEDIMENT_SOURCE_REVISION SEDIMENT_SOURCE_DIGEST
docker compose up -d --build --force-recreate migrate api
curl -sf http://127.0.0.1:8000/health
```

If you use the gateway, rebuild and start that profile after the API is healthy.
Run fresh scans against the images you built; release evidence for different
image identities doesn't attest to your local build.

To rotate runtime, migrator, or operator database credentials, stop the API and
gateway, replace the corresponding private `.env` values, and rerun provisioning
before recreating their consumers. To rotate an ingest token, replace only that
entry in `SEDIMENT_INGEST_TOKENS`, update its enrolled client, and recreate the
API. The gateway's token must match its named map entry. Rotate the operator
HTTP token independently. Remove `SEDIMENT_API_BEARER_TOKEN` after migrating
legacy capture clients; that token grants ingest authority only.

Read `CHANGELOG.md` before you upgrade a gateway callback or client fleet. When
a release changes identity parsing or a wire contract, upgrade the server
first.

Most migrations in `packages/core/sediment_core/alembic/versions/` finish in
milliseconds. A migration that adds `CHECK` constraints or unique indexes to a
Fact table holds that table under an `ACCESS EXCLUSIVE` lock for the
constraint validation or index build. The lock blocks that table's reads and
writes for the duration, proportional to the table's row count rather than
to the number of statements. `0010_repository_identity` adds five such
constraints and two such indexes across `ci_outcomes`, `pushes`,
`session_commit_observations`, `pull_request_merges`, and
`pull_request_revisions`. On a deployment with substantial history in those
tables, schedule the upgrade for a maintenance window. Expect the migration
step to take longer than a schema-only release. Provisioning applies the
migration; API startup checks the schema and refuses an incomplete upgrade.
Measure upgrade duration against a restored copy of the deployment database.

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

Each week, check Fact growth and outcomes, inspect storage usage, and review
failed scans and upstream fixes:

```bash
docker compose --profile operator run --rm operator sediment facts
docker compose --profile operator run --rm operator sediment report model
```

Apply released security fixes according to your deployment's update policy and
before an applicable assessment deadline. Each release records its support
window in `security-support.json`. Component and vulnerability reviews expire
within 30 days; an
upstream support end or withdrawal can shorten either period.

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
and recovery duration. Test restoration before deployment and after
schema or backup-tool changes.

If you enable the bundled gateway, review unresolved completion identity:

```bash
docker compose logs api | grep gateway_ingest_skipped_no_session
```

Each line names an inference call that arrived without a resolvable Session id.

## 8. Privacy and data handling

### 8.1 What Sediment stores

Two stores carry source content. `inference_calls` contains structured input
and output messages plus the gateway payload. The git mirror contains pushed history from
each allowlisted repository.

Other Fact tables carry event metadata and identity. Transcript capture can
also contain model-written edit text and Session-end content for files that the
agent edited.

Basic redaction runs without configuration before a Fact store persists any
content-bearing Fact. It replaces high-confidence API keys and bearer
credentials with `[REDACTED_CREDENTIAL]` in normalized content and `raw`
payloads. It doesn't claim comprehensive secret detection. If an existing Fact
contains a credential, quarantine that Fact; Facts remain immutable and the
guard doesn't rewrite history.

[Privacy boundaries and ceilings](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
lists each capture payload. Attribution notes contain Session ids and
timestamps, not source content.

### 8.2 Where data lives

The deployment uses these named volumes:

- `sediment-postgres` for PostgreSQL runtime Facts and schema
- `sediment-mirror` for bare git mirrors
- `sediment-export` for operator-created exports
- `sediment-staging` for private, disposable Derivation and export staging;
  it holds unredacted payloads only while an operator command runs
- `sediment-delivery` for optional unredacted gateway retries

The API doesn't send captured data anywhere. Operator-authenticated query routes and privileged host access are the read
paths. Capture credentials cannot read datasets. A host administrator can still
read container environments and mounted data.

Facts and mirrors have no automatic retention or expiry. Raw mirrors, optional
capture files, and encrypted backups remain sensitive even when Basic redaction
protects normalized Fact content. Authorize any raw capture retention explicitly.

Put Docker data and export storage on a dedicated filesystem or enforce a
volume-driver quota before deployment. Record its size and alert before
80% use. The mirror worker reserves 1 GiB free space; that check is not a quota
and cannot bound repository size. Plan retention, backup space, and incident
capacity without relying on the host root filesystem.

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
docker compose --profile operator run --rm operator sediment quarantine pushes <fact_id> --reason '...'
docker compose --profile operator run --rm operator sediment release pushes <fact_id> --reason '...'
```

`sediment facts` shows the visible count. `sediment quarantine-log` shows the
audit trail and the quarantine-state Provenance value.

If you quarantine only some Edit observations for one file in a Session,
exclude that file and Session's external-change diagnostics from comparisons.
Record the affected organization, agent harness, Session, file, and quarantine
revision with the report. If an aggregate includes that scope, don't use its
external-change totals or rates as complete measurements. The remaining
observations can report understated totals without a partial-coverage flag.
Recomputation alone doesn't repair the missing windows. Retain quarantine
until its original reason is resolved; don't release Facts to improve a metric.
The [External edit windows limitation](../explanation/how-capture-works.md#external-edit-windows)
describes the affected diagnostic fields and their separation from training.

To delete every Fact, mirror, and operator-created export, stop the compose
project and remove its volumes:

```bash
docker compose --profile gateway down -v
```

This operation can't be undone. Extract the PostgreSQL database and any files
under `sediment-export` before you run it if you need them.

To remove only the mirror, stop the stack, remove its volume, and restart:

```bash
docker compose down
docker volume rm sediment_sediment-mirror
docker compose up -d
```

The API recreates mirrors after later push webhooks.

### 8.4 Network exposure

**Inbound.** Uvicorn listens on port 8000 inside the API container. Compose
binds it to `127.0.0.1:8000`. The optional gateway binds
`127.0.0.1:4000`.

Capture routes require an ingest token or webhook signature. Operator routes
require the separate operator token. The `/query/evidence` operations return
captured content to operators on the same API port. They add no outbound
connection. Keep the consuming agent's model endpoint inside your perimeter
when captured content must stay there; see
[Continue a task with captured evidence](resume-with-evidence.md).
`GET /health` is unauthenticated and returns no captured data. Request bodies
are capped before route processing.

**Outbound.** Repository mirroring is the API's only optional outbound
connection. A push webhook can trigger `git fetch` from its clone URL when
`SEDIMENT_MIRROR_PATH` is set.

If you enable bundled LiteLLM, the gateway container also sends model requests
to Anthropic and sends completed-call payloads to the API over the Compose
network. Provider credentials don't enter the API container.

Before git starts, the API enforces a transport allowlist, the configured clone
host allowlist, a server-side request forgery guard, and `GIT_ALLOW_PROTOCOL`.
Treat each allowed host as an explicit fetch trust decision. An explicit entry
can authorize a private forge address; an empty list admits public hosts only.
Keep Git configuration and proxy/header environment overrides at the reviewed
defaults unless you reassess the applicable curl dispositions.

Unset `SEDIMENT_MIRROR_PATH` to disable outbound repository fetches.

**Database and process confinement.** PostgreSQL has no published port. Only
provisioning, API, and operator services join its internal network. The checked
host-based authentication file requires SCRAM for local and TCP connections,
including existing volumes. The gateway cannot join that network.

Every service has a read-only root, bounded temporary storage, memory,
processor, process-count, and rotated-log limits. The API and gateway run
without Linux capabilities. PostgreSQL receives only the five capabilities
needed to initialize volume ownership and drop to its unprivileged server user.
Every service enables `no-new-privileges`.

One API process permits two active read workers, two active mirror workers,
and sixteen waiting mirror jobs. At most one read worker serves evidence
requests. Reads have a 30-second deadline; mirror work
has 120 seconds. Child termination and reaping precede capacity reuse. A busy
service returns 503 so a sender can retry retained bytes. Multiplying Uvicorn
processes multiplies these limits; keep the supplied single-process command
unless you recalculate host and database capacity. The default memory caps are
2 GiB for the API, 1 GiB for PostgreSQL, and 2 GiB for the gateway. Operator and
migration jobs need additional headroom.

Review [the disposition register](../../security/dispositions.json) before a
deployment. Database isolation and resource limits reduce exposure
to unresolved native parser vulnerabilities; they don't remove vulnerable code
or protect stored Facts after database-process compromise. A custom network,
SQL client, image, Git configuration, or credential distribution needs another
review. These controls don't establish Cyber Essentials certification.

**No phone-home path.** Sediment has no telemetry, analytics, crash reporting,
update checks, license validation, or cloud SDK. Exports are local JSONL files.

Cloudflare ingress makes captured requests transit Cloudflare's edge. Replace
it with internal ingress when that boundary doesn't fit the deployment.

For operation without public network access, prepare the required software,
container images, and dependencies inside the perimeter. Use internal ingress,
capture sources, Git remotes, and model endpoints. An internal gateway alone
doesn't keep model requests inside the perimeter; its configured model endpoint
determines their destination. Installation can still require public downloads
unless you provision its dependencies in advance.

## 9. Troubleshooting

- **`failed to solve: DeadlineExceeded`, or image pulls hang:** test
  `docker pull busybox`. If Docker Desktop's credential helper hangs, remove
  its `credsStore` entry and restart the helper.
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
- **Compose uses another checkout's `.env`:** the project name is `sediment`,
  so a second checkout can recreate the same containers. Stop the deployment
  from its owning directory before you start another checkout.
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
4. Run `docker compose --profile gateway down -v` on the host.
5. Delete the ingress tunnel and DNS records.

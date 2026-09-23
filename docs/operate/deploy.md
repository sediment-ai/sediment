# Deploy Sediment

Run the API, PostgreSQL Fact store, and git mirror on a shared host with Docker
Compose. For a single-machine evaluation, use the [Quickstart](../quickstart.md).

Complete deployment before [enrolling pilot developers](run-pilot.md).

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

Run these commands from the Sediment checkout. Before building, review
[release security evidence](security.md).

Create `.env` with separate generated credentials and owner-only permissions
before any secret reaches the file:

```bash
uv run --python 3.12.14 --no-project python scripts/create_deploy_env.py
```

The generator creates separate database, operator, ingest, webhook, and gateway
credentials. It refuses an existing file or a directory writable by another user.
Keep `.env` private. Don't source it or distribute it to developers.

Edit these deployment settings in `.env`:

| Setting | Action |
| --- | --- |
| `SEDIMENT_ORG_ID` | Set the deployment's organization identifier. |
| `SEDIMENT_ALLOWED_CLONE_HOSTS` | Set the permitted Git hosts; the default is `["github.com"]`. |
| `SEDIMENT_DEV_MODE` | Keep `false`. |

Distribute a named ingest token to each capture client. Reserve the operator
token for queries and reports. Production tokens and webhook secrets must have
at least 24 characters. The API doesn't rate-limit authentication attempts;
configure that limit at ingress.

If you enable agent-requested retrieval, add `SEDIMENT_RETRIEVAL_TOKEN` and exactly
one source setting to the private `.env`: `SEDIMENT_RETRIEVAL_SESSION_ID` for a
fixed source, or `SEDIMENT_RETRIEVAL_SESSION_IDS` for a JSON array of 1–32 unique
Session IDs. The plural JSON setting must fit 16 KiB. Use a distinct printable
ASCII token of at least 24 characters and actual Session identifiers. The API
validates this configuration even in development mode. Leave all three settings
unset to disable retrieval; empty values are invalid. Compose passes them only
to the API. Restart after changing configuration. Rotate the token whenever the
authorized set changes; the service cannot detect reuse across restarts.
Removing the token and source setting and restarting revokes access. The grant
includes future Facts in those Sessions and does not claim repository ownership.
See [Discover a previous Session](resume-with-evidence.md#discover-a-previous-session)
for agent configuration and the aggregate source limits.
If an ingest client is named `retrieval`, rename that entry before upgrading.
The identifier is reserved; its old secret isn't reclassified.

Build and start the deployment with its source identity:

```bash
set -e
SEDIMENT_SOURCE_REVISION="$(git rev-parse HEAD)"
SEDIMENT_SOURCE_DIGEST="$(uv run --python 3.12.14 --no-project python scripts/security_image_assurance.py source-digest)"
export SEDIMENT_SOURCE_REVISION SEDIMENT_SOURCE_DIGEST
docker compose up -d --build
```

Verify it from the host:

```bash
curl -fsS http://127.0.0.1:8000/health
```

```text
{"status":"ok","version":"0.2.0"}
```

Compose waits for PostgreSQL, provisions separate database roles, and applies
migrations before starting the API. The API checks the schema and runtime
privileges. If the health request fails, check startup status:

```bash
docker compose ps -a
docker compose logs postgres migrate api
```

Database and mirror volumes survive image rebuilds and `docker compose down`.

### Enable bundled LiteLLM

If you need the bundled Anthropic gateway, add `ANTHROPIC_API_KEY` to `.env`.
Keep the generated `LITELLM_MASTER_KEY` and gateway ingest token. The gateway
supports `claude-*` routing; other providers require a separate gateway
configuration. See the [gateway boundary](../../docker/gateway/README.md).

Start the complete profile:

```bash
docker compose --profile gateway up -d --build
docker compose --profile gateway ps gateway
```

The gateway binds to `127.0.0.1:4000`. It receives provider and ingest credentials,
but no database credentials. Its callback sends completed Inference calls to
`http://api:8000` over the Compose network. A capture failure doesn't retract a
successful model response.

If you authorize persistent storage of unredacted capture payloads, set
`SEDIMENT_DELIVERY_DIR=/data/delivery/pending` in `.env`. The gateway uses the
`sediment-delivery` named volume and owns a replay worker for its process
lifetime. Leave the setting empty for direct best-effort delivery.
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

## 3. Expose a public HTTPS endpoint

Use an HTTPS reverse proxy or tunnel for remote clients. This example uses
Cloudflare, which carries requests through its edge. If that boundary is outside
your approved perimeter, use internal ingress. For API-only deployment, omit the
`sediment-llm` DNS command and ingress entry.

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
sudo cloudflared --config '/home/<user>/.cloudflared/config.yml' service install
sudo systemctl start cloudflared
curl -s https://sediment-api.example.com/health
```

Remote clients must use the HTTPS deployment root, such as
`https://sediment-api.example.com`. `sediment login` accepts HTTP only on literal
loopback hosts. It rejects embedded credentials, query strings, fragments, and
non-root paths.

## 4. Configure capture

1. [Configure managed capture](../capture/managed-capture.md) for forge webhooks,
   private mirrors, and an optional gateway.
2. [Enroll pilot developers](run-pilot.md) or
   [configure one developer machine](../capture/local-capture.md).

Upgrade the API before clients when a release changes a capture contract.

## 5. Verify the deployment

Check the public health endpoint:

```bash
curl -sf https://sediment-api.example.com/health
```

After connecting a capture source, run the operator profile. This also creates
its export and staging volumes:

```bash
docker compose --profile operator run --rm operator sediment facts
docker compose ps -a
docker volume inspect \
  sediment_sediment-postgres \
  sediment_sediment-mirror \
  sediment_sediment-export \
  sediment_sediment-staging
```

`sediment facts` prints total and Derivation-visible rows. If an expected source
stays at zero, follow that source's capture verification steps.

Inspect the PostgreSQL revision without changing it:

```bash
docker compose --profile operator run --rm operator sediment db status
```

The status command reports `at_head` after Compose starts successfully.

## 6. Upgrade the deployment

Before upgrading, read `CHANGELOG.md`, review [release security evidence](security.md),
back up the database, and test restoration. Measure migration time on a restored
copy. Constraint validation and index builds can block table reads and writes;
schedule a maintenance window for large datasets.

Stop the API and gateway before changing database roles or credentials. Preserve
`POSTGRES_PASSWORD`: changing it in `.env` doesn't rotate an initialized server.

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
docker compose up -d --build --force-recreate postgres migrate api
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
and recovery duration. Test restoration before deployment and after
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

To delete the deployment data, remove its Compose volumes. **This deletes Facts,
mirrors, exports, staging, and buffered deliveries. You cannot undo it.** Save
any required database backup and exports first:

```bash
docker compose --profile gateway down -v
```

To remove only the mirror, stop the stack, remove its volume, and restart:

```bash
docker compose down
docker volume rm sediment_sediment-mirror
docker compose up -d
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

Keep the supplied single API process unless you recalculate host and database
capacity. Per-process limits are two active read workers, two active mirror
workers, and sixteen waiting mirror jobs. Evidence, commit and Session queries,
and reports share both read slots; reports have no reserved slot, and a third
read refuses immediately without queueing. Reads have a 30-second deadline;
mirror work has 120 seconds. Saturation returns 503. Memory limits are 2 GiB for
the API, 1 GiB for PostgreSQL, and 2 GiB for the gateway. Reserve additional
capacity for operator and migration jobs.

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

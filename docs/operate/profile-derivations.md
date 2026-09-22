# Profile reports and Derivations

Use this guide to measure memory and disk use before you qualify a deployment
for broad reports, canonical Derivations, or training exports. Run the complete
public command or HTTP request against isolated restored inputs.

## Prepare fixed inputs

Restore a backup into a separate PostgreSQL instance and copy its mirrors.
Configure the candidate to use those copies. Keep credentials in a protected
environment file. Do not print its contents.

Record the source revision, image digest, dependency versions, schema revision,
visible Fact counts, quarantine revision, and mirror revisions. For reports,
record the exact cohort and `as_of`. For Derivations, retain the policy and scope;
the bundle derives `as_of` from its evidence.

Use the same restored inputs for every comparison. Each Derivation retains one
read-only repeatable-read snapshot through construction and validation. Keep
ingest and mirror updates stopped on the isolated deployment during comparisons.

## Set separate resource budgets

Record each budget before starting:

| Resource | What to record |
| --- | --- |
| Container memory | Memory ceiling and swap setting; include all worker processes |
| Concurrency | Simultaneous report requests and worker count |
| Work unit | Largest content row, complete Session, and complete training group |
| Storage | Private staging allowance, output capacity, filesystem quota, and free space |
| Duration | Command or request deadline and cleanup deadline |

Use one workload at a time for the first measurements. Leave host memory for
PostgreSQL and unrelated services. A successful serial request does not qualify
several simultaneous workers in the same container.

FactStore checks transferred content before decoding: 64 MiB per content row,
256 MiB per complete Session input/output population, and 256 MiB per materialized
report or Attribution output population. Supporting identity row limits remain
independent of these byte limits. Offline semantic validation also checks the
256 MiB Session input/output envelope before rebuilding its complete histories;
raw audit payloads do not count toward that envelope.

`BundleLimits` defaults to 512 MiB per encoded record, 8 GiB per private record
store, and 64 MiB for explicit bundle materialization. Separate live stores have
separate quotas. Count construction, validated input copies, training stages,
publication staging, and completed output together when provisioning storage.
SFT, DPO, and diff-SFT default to 128 MiB of encoded source Facts and artifact
records per complete projection group. Git diff reads retain their existing
mirror-owner behavior. These byte allowances do not guarantee a particular
Python memory peak.

Consumer profiles use the same 64 MiB materialization allowance for their
complete native source population, projected rows, and adapted data plus evidence.
Settings, loader inputs, and prepared data plus evidence have separate checks
at that allowance. The NeMo source population includes complete Inference calls;
SWE-bench consumes Rollouts. Each profile refuses excess before publication.
These checks also apply to file-backed bundles. See
[Check consumer capacity](../exports/consumer-compatibility.md#check-consumer-capacity).

Qualify the exact `--profile` and its pinned optional environment separately.
Canonical export measurements don't qualify an upstream loader's memory use.
A profile refusal remains a failed capacity probe for that requested workload.

If a capacity check refuses the full workload, record a failed capacity gate.
Do not substitute a smaller cohort or treat the refusal as an eligibility skip.

## Measure commit investigations

Use a fixed commit, repository selector, and `as_of` for paired requests to
`GET /query/commit/{sha}`. Compare complete response values and record latency,
worker memory, PostgreSQL buffers, returned projection rows, and Git subprocess
counts. Run both an identity-qualified request and an unqualified request;
the latter can search several repository mirrors.

Vary these populations separately:

- Repeated Push and repository evidence history with unchanged names and claims.
- Distinct repository identities, names, and rename relationships.
- Earlier Pushes that require range inspection to discover a non-head target.
- Calls and output bytes inside the target owner's eligible candidate windows.

Include a missing commit and a capped non-head commit. A benchmark that requests
only Push heads doesn't cover complete owner discovery. Preserve captured
Session observations so mutable Git notes can't supply missing historical
evidence.

Target Attribution reads organization-wide candidates in the owner's Jaccard
window and observed note Sessions in the longer note window. Repository
witnesses reduce repeated rows transferred to Python; PostgreSQL still groups
scalar history. Git discovery can still inspect earlier Pushes. An improvement
in one component doesn't establish a proportional whole-query improvement.

Repeat the successful workload through the HTTP worker boundary and under the
supported concurrent query load. Record 409 capacity refusals, 503 admission or
deadline failures, concurrent ingest receipts, and health latency. The two worker
slots and 30-second deadline retain their operational meaning. Synthetic results
qualify only the declared fixture and resources; verify the partner workload
separately. [ADR 0024](../adr/0024-targeted-commit-investigations.md) defines the
selection and evidence contracts.

## Provide private disk storage

The Compose `operator` service sets `TMPDIR` to the private `sediment-staging`
volume, so its commands need no preparation. In any other container where
`/tmp` uses memory-backed storage, choose a dedicated disk-backed mount for
private staging and prepare the directory before starting the command:

```bash
umask 077
mkdir -p /data/export/.private-work
chmod 700 /data/export/.private-work
export TMPDIR=/data/export/.private-work
sediment derive --out /data/export/candidate-bundle
```

Use an unused output destination. The bundle writer prepares its publication
directory beside that destination so the final directory rename stays on one
filesystem. A Docker volume does not impose a storage quota by itself.

The bounded CLI paths use private file-backed records. Python callers must keep
`build_derived_bundle_context` or `open_derived_bundle` open until consumption
finishes. `build_derived_bundle` and `read_derived_bundle` explicitly materialize
small bundles and can refuse a workload that the bounded interfaces support.

## Measure memory and storage during execution

Sample throughout the command, including decoding, validation, and publication.
For every sample, record elapsed time, resident set size (RSS), container memory,
private staging bytes, output bytes, and free filesystem space. Use a monotonic
clock for elapsed time.

On Linux, inspect these sources:

| Measurement | Source |
| --- | --- |
| Process RSS and high-water mark | `/proc/<pid>/status`: `VmRSS`, `VmHWM` |
| Container usage and peak | `/sys/fs/cgroup/memory.current`, `memory.peak` |
| Anonymous memory and file cache | `/sys/fs/cgroup/memory.stat` |
| Memory-limit events | `/sys/fs/cgroup/memory.events`; container exit and out-of-memory state |
| Filesystem capacity | Filesystem free-space and quota tools for the staging mount |

Read the cgroup files while the candidate runs, or use a supervisor that keeps
the container alive long enough to save the final counters. Use a fresh container
for each serial run so a previous command's peak does not contaminate the result.
Report bytes or explicit MiB/GiB units. Process and container measurements count
different categories; retain both.

Count every concurrently live private directory. One store's limit is not the
whole command's storage limit. Sample allocated disk space as well as logical
file sizes when the filesystem's behavior matters.

Keep logs to stage names, counts, durations, sizes, hashes, and sanitized error
categories. Do not log prompts, responses, raw payloads, Fact bodies, credentials,
or connection URLs. Allocation tracers can add substantial memory overhead;
use them after a lower-overhead profile identifies the stage to investigate.

## Compare complete results

Run the same public report twice with the same cohort and `as_of`. Compare its
response and diagnostic counts. Then run an unscoped `sediment derive` twice in
fresh processes against the unchanged restored inputs. Compare all seven file
sizes and SHA-256 hashes, including the manifest.

From one resulting bundle, run DPO, SFT, diff-SFT, and every supported RLVR target.
Compare each offline export with its direct export from the same restored inputs.
Use the same recipe, mirrors, and verifier settings. Compare parsed JSON values,
row order, and counters across export modes. Compare bytes across repeated runs
within one mode; canonical bundles sort nested keys while direct exports retain
source key order. [Run derivations](run-derivations.md) lists the commands.

If the restored workload lacks qualifying evidence, an empty export verifies
only that execution path. Also exercise real Fact and git fixtures that produce
positive training rows, counted representation exclusions, and complete evidence
groups. Include cross-Session DPO comparisons and fragmented Rollouts. Never
invent labels or alter evidence to make a capacity check pass.

Verify failure behavior separately: malformed evidence, an oversized complete
group, or serialization failure must abort publication and clean handled staging.
After a forced process kill, remove only private directories owned by that ended
process. Retain completed artifacts for validation. File replacement is atomic
per file; interruption between training-file replacements can expose different
complete generations.

After serial workloads pass, send the intended simultaneous report count to one
candidate API container with the deployment memory budget. Measure the parent
and all children together. A controlled worker failure verifies error handling;
it does not pass the capacity gate.

## Locate the remaining cost

If memory rises during row collection, inspect selected columns, driver buffering,
and population materializers. If decoded content stays resident between groups,
inspect retained lists, dictionaries, and caches. If validation or serialization
creates the peak, inspect simultaneous record reconstruction and encoding buffers.
If container memory rises while RSS stays flat, inspect charged file-cache pages.

Retain required global identity and CI context when changing execution. A small
cohort can still need organization-wide evidence for correct ambiguity handling.
[ADR 0020](../adr/0020-bounded-derivation-execution.md) defines that boundary.

Record the final source and image digests, complete workload, peak measurements,
output comparisons, and unresolved gates in the qualification report. Keep
private evidence under the deployment's retention rules and remove only the
isolated resources created for the profile.

## Rehearse capture alongside batch work

From a source checkout, use `scripts/capacity_rehearsal.py` to test a declared
synthetic population. Install the locked workspace dependencies and PostgreSQL
client libraries first. Set `SEDIMENT_DATABASE_URL` through your protected
environment to an isolated administrative database whose role can create and
drop databases. The script creates and removes one random scratch database.
It doesn't migrate the administrative database.

```bash
uv sync --locked
uv run python scripts/capacity_rehearsal.py \
  --profile sim/profiles/capacity-smoke.json \
  --out /data/capacity-smoke
```

Use an unused output path on a disk-backed filesystem. The script creates a
private directory and retains synthetic artifacts, logs, and `report.json`.
The `receipts/` journals retain acknowledged gateway identities, timings, and
byte counts, including when a later gate fails. They contain no message bodies.
The report's `failures` list retains recorded failure categories. Its `reason`
field identifies the first recorded category, which can come from cleanup.
After the smoke run passes, repeat with `sim/profiles/capacity-pilot.json` and a
different output path. Exit status 0 qualifies the declared synthetic profile;
status 1 records a failed probe, and status 2 identifies an invocation failure.

Edit a copy of the profile to declare your workload. Every field is required;
unknown fields and invalid sizes fail before database or process creation.

| Dimension | Meaning |
| --- | --- |
| `schema_version` | Profile contract version, 1 |
| `developers` | Synthetic developer identities represented in historical Sessions |
| `history_weeks`, `sessions_per_week` | Historical Session population; Sessions per week is the total across developers |
| `calls_per_session` | Calls in each complete historical Session |
| `history_bytes`, `output_bytes` | ASCII bytes per user message and assistant response; later calls repeat all earlier turns |
| `live_interval_ms`, `max_live_calls` | Delay between sequential live requests and maximum live population |
| `job_timeout_seconds` | Deadline for each child job or HTTP report request |
| `max_process_rss_mib`, `max_workspace_mib` | Post-run limits for sampled aggregate process RSS and logical workspace bytes |

Read the report's measurement scope before using a passing result. The portable
sampler includes the runner, sender, API, workers, and batch processes. It excludes
PostgreSQL memory, container memory, and database storage. Its checks don't enforce
memory or filesystem ceilings. Record those deployment budgets separately.

The runner seeds the existing semantic scenarios, then starts a loopback HTTP
server. Live gateway capture continues during weekly reports, an unscoped
Derivation, and SFT and RLVR exports. Each operation must contain a completed
capture request. The runner checks duplicate receipts, reconciles receipts with
stored Inference calls, and requires positive training rows. It stops capture
before comparing repeated canonical builds from fixed inputs.

During the Derivation, the runner also delivers a signed Push for a fresh commit.
It requires a matching Session-to-commit observation after the HTTP receipt and
checks redelivery. If the observation deadline expires, inspect the synthetic
API log for the cause. An acknowledged Push doesn't prove background completion.
The redelivery check verifies the retained receipt and visible observation; it
doesn't certify completion of a second background refresh. The probe doesn't
guarantee contention on a mirror lock or exercise the full mirror queue.

If a job has no completed capture request within its execution interval, the
probe fails with `no_ingest_overlap`. That means the run lacks coexistence
evidence; it doesn't establish a deployment capacity failure. Choose a shorter
live interval or a larger declared workload before repeating the probe.

The runner handles SIGINT and SIGTERM by stopping its owned processes and removing
its scratch database. It records interruption as a failed run. SIGKILL and host
failure bypass this cleanup; retain the output directory for investigation.

The profiles don't convert lines of code into Inference calls. Historical gateway
timestamps spread a population over weeks; the semantic Push and CI evidence is
contemporary. This rehearsal doesn't reproduce months of changing repository
history. The local fixture uses development mode and file-based mirrors. For
deployment verification, repeat representative workloads under the deployment's
security settings and total resource limits.

## Measure agent evidence retrieval

Use `scripts/agent_evidence_benchmark.py` to measure a loopback API and its
disposable evidence workers against real PostgreSQL. Set
`SEDIMENT_TEST_DATABASE_URL` to an owned disposable cluster. The script creates,
migrates, and removes its own database. Sync the runtime checkout with
`uv sync --locked --python 3.12` before running it.

```bash
uv run python scripts/agent_evidence_benchmark.py \
  --runtime /path/to/sediment-checkout \
  --output /data/evidence-keyword \
  --sessions 8 --background 20000 \
  --modes keyword --samples 30 --waves 10 --clients 1 5 10
```

Use a different unused output directory for each run. Repeat with `--sessions 1`
and `--sessions 32` to vary the authorized source size. Hold this size fixed and
change `--background` to measure unrelated organization history. Compare clean
runtime revisions using the same script and arguments. Check fixture and
successful keyword response hashes before comparing timings.

Run each mode separately for comparable traffic conditions. `keyword` measures
discovery followed by selected-Session keyword retrieval. `exact` discovers a
preview and fetches its reference. `known` fetches a fixed known reference
without discovery. The latter two modes require the scoped exact API. They use
different selection semantics and do not establish equivalent context quality.

Inspect `report.json` for successful latency and capacity refusals separately,
response bytes, sampled API process-tree memory, and verified overlapping capture
receipts. The capture probe counts HTTP 503 refusals separately from stored
receipts and continues with a distinct event without retrying the refused event.
Read `diagnostic.json` for startup, source, selection, encoding, and
actual SQL plans. Diagnostic stage times are not public request latencies.
The sampler excludes PostgreSQL and can miss brief memory peaks. Record database
and host limits separately. A scripted chooser exercises transport; it measures
neither decision-model quality nor inference cost.

# Profile reports and Derivations

Use this guide to measure memory and disk use before you qualify a deployment
for broad reports, canonical Derivations, or training exports. Run the complete
public command or HTTP request against isolated restored inputs.

## Prepare fixed inputs

Restore a backup into a separate PostgreSQL instance and copy its mirrors.
Configure the candidate to use those copies. Keep credentials in a protected
environment file. Do not print its contents.

Record the Sediment release version, installed dependency versions, schema revision,
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
| Process memory | Memory ceiling and swap setting; include all worker processes |
| Concurrency | Simultaneous report requests and worker count |
| Work unit | Largest content row, complete Session, and complete training group |
| Storage | Private staging allowance, output capacity, filesystem quota, and free space |
| Duration | Command or request deadline and cleanup deadline |

Use one workload at a time for the first measurements. Leave host memory for
PostgreSQL and unrelated services. A successful serial request does not qualify
several simultaneous workers under the same resource limits.

| Encoded population | Default byte limit |
| --- | --- |
| One FactStore content row | 64 MiB |
| Complete Session input/output | 256 MiB; excludes raw audit payloads |
| Materialized report or Attribution output | 256 MiB |
| One bundle record | 512 MiB |
| One private record store | 8 GiB |
| Explicit bundle materialization | 64 MiB |
| Complete SFT, DPO, or diff-SFT projection group | 128 MiB |

These limits don't guarantee a Python memory peak. Independent identity-row caps
also apply. Count overlapping stores, validation copies, publication staging,
and completed output when provisioning storage.

Consumer profiles impose separate 64 MiB checks on source populations, projected
rows, adapted data and evidence, settings, and loader inputs. See
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

Use a separate deployment with representative, approved data. Run normal agent
capture while you execute the installed `sediment derive`, `sediment report`,
and `sediment export` commands. Record latency, failures, memory, and storage.

Compare receipt counts with stored Facts and inspect every exclusion. A quiet
workload doesn't establish peak capacity. Repeat the workload after a restart
to verify recovery.

The [maintainer performance rehearsals](../../CONTRIBUTING.md#maintainer-performance-rehearsals)
record the synthetic qualification procedures used during development.

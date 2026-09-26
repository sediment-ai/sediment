# Validate a deployment

Verify capture, data integrity, recovery, and capacity before expanding a pilot.
Use [Run a Cursor and Codex pilot](run-pilot.md) for enrollment commands.
Keep results and source evidence in protected storage outside the documentation
and record each check as verified, failed, unverified, or not applicable.

Validation applies to the tested configuration and workload. It doesn't establish
model improvement, calibrated Confidence, or an availability guarantee.

## Record the configuration and workload

Record these inputs before testing. Retain earlier attempts when repeating a check.

| Record | Include |
| --- | --- |
| Scope | Organization, repository identity, harnesses, models, capture channels, and excluded uses |
| Software | Server/client revisions, package and harness versions, image digests, lockfile digest, and consumer profile/version |
| Infrastructure | Host resources, storage quotas, network paths, credential authorities, sender supervisors, and backup destination |
| Evidence boundary | Timezone-aware window and cutoff, Fact snapshot, policy, quarantine revision, and required mirror objects/refs |
| Workload | Expected volume, payload sizes, concurrency, delivery latency, acceptable recovery time, and acceptable data loss |
| Evidence storage | Protected records, logs, artifact hashes, retention, and review results |
| Limits | Unsupported source shapes, best-effort channels, unverified environments, and observation caps |

## Verify the installed build

1. Review the selected revision's continuous integration (CI), shim,
   schema-compatibility, consumer, and release-rehearsal results. Retain the
   acceptance record and artifact hashes. Record failures and skips; a check
   that didn't run remains unverified.
2. Match installed revisions and artifact identities to that evidence. If you
   qualify a modified or unverified build, run the missing checks and
   [record its rehearsal](rehearse-release.md#record-a-revision-bound-rehearsal)
   against a disposable database.
3. Verify the deployed API, database, and live capture with the
   [deployment checks](deploy.md#5-verify-the-deployment) and
   [pilot procedure](run-pilot.md). Release checks use synthetic inputs.

To rehearse Docker installation on a machine with Docker and the Python workspace
installed, run:

```bash
SEDIMENT_TEST_DOCKER_PILOT=1 uv run pytest -q scripts/tests/test_docker_pilot.py
```

The check builds a separate Compose project with fresh credentials, volumes, and
an allocated loopback port. It verifies readiness, migrations, operator commands,
capture-only enrollment, credential permissions, synthetic ingestion and signed
webhooks, duplicate delivery, Git notes, restart persistence, and uninstall. It
removes only its test containers, volumes, and image tags. It doesn't configure
your agent settings or verify HTTPS ingress, private Git access, paid gateways,
or live harness delivery.

## Verify privacy, authorities, and source coverage

1. Agree each channel's payload with participants. Codex telemetry can include
   patch arguments without transcript capture. Gateway capture contains model
   inputs and outputs; transcript capture adds applied and observed text.
2. Verify deployment tenancy, separate ingest and operator credentials,
   authenticated network paths, private Git access, and credential permissions.
   Follow [Check release and deployment security](security.md).
3. Record each harness's executable path and version. pi capture requires a
   source checkout; follow the [pi integration guide](../capture/agent-integrations.md#pi).
   A Codex CLI profile check doesn't verify Desktop.
4. For marker-client upgrades, pause hooks across linked worktrees, reconcile
   markers, replace every helper, and restart harnesses. Don't mix old and
   generation-aware writers.
5. For buffered channels, verify approved private storage, quotas, encryption
   when required, and a supervised worker that restarts after login or reboot.
6. Run each harness's Session check from the [pilot guide](run-pilot.md). After
   flushing buffered delivery, require a running worker and no pending or
   blocked entries. Record unbuffered channels separately.

Use the [integration comparison](../capture/agent-integrations.md#compare-integrations)
and each harness's limits when selecting expected evidence. Implicit pi/Cursor
accepts don't enter the human-explicit accepted-work population. A Push receipt
doesn't prove mirror refresh or a Session-to-commit observation.

## Reconcile capture against independent records

Before inspecting Sediment output, record each controlled action's Session,
repository identity, harness/version, source time, supported shape, expected
Fact kinds and counts, and independent source evidence. Include gateway request
and forge delivery records.

Test successful, failed, and canceled tools; available explicit decisions;
repeated delivery; empty and long Sessions; single-file and multi-file changes;
and disabled consent. Retain unsupported cases with no expected Fact.

1. Match supported actions to unique Fact IDs after flushing. Compare content,
   source identifiers, times, and repository identity. Record whether each
   payload was emitted, prepared, queued, acknowledged, or stored.
2. Isolate or serialize OpenTelemetry Protocol (OTLP) trials to correlate each
   request, attempt, and receipt. Retain protected request digests, source IDs,
   and receiver counts. Receiver logs lack sender delivery IDs; uncorrelated
   concurrent requests leave request reconciliation unverified.
3. Check `received = translated + untranslated + malformed` for records and
   `candidates = stored + duplicates` for each Fact kind. Count malformed
   containers separately. Context-only untranslated records needn't create Facts.
4. After partial storage, reconcile the retained prefix and replay by Fact ID.
   Failed requests have no completed receipt. Retries aren't independent events.
5. Check local notes, remote notes, stored Session-to-commit observations, and
   Attribution separately. Keep quarantine and export exclusions separate from
   ingestion counts.

Record loss, duplicates, decline reasons, units, and unresolved observations.
Without an independent denominator, report counts and limits rather than a
completeness percentage. Health and Session doctor checks don't prove completeness.
For latency, distinguish event, preparation, receipt, and storage times, including
clock uncertainty across hosts.

## Exercise delivery and forge recovery

Use an isolated deployment and repositories that you control. Record fault times,
workload, recovery actions, Fact IDs, payload hashes, and remaining loss.

| Trial | Verify |
| --- | --- |
| Receiver outage and sender restart | Buffered bytes, identities, and observations survive source-file changes. |
| Lost acknowledgment after commit | Replay retains Fact IDs without adding logical Facts. |
| Worker or host restart | The supervisor restores one worker; measure backlog age and drainage. |
| Bad credentials, rotation, or wrong destination | Failures stay visible; correction doesn't expose secrets or redirect old payloads. |
| Capacity and expiry | Declines remain counted; partial publication doesn't count as complete capture. |
| Unbuffered outage | Measure that source's retry and loss behavior separately. |
| Push, notes, and CI | Forge records and Facts reconcile for the repository and commit. |
| Failed private fetch and repair | Verify recovery of the original commit's missing observations. |
| Branch creation, force push, and more than 20 commits | Head-only and capped processing leave omitted commits explicit. |
| Late note | Later observations retain their actual capture time; historical absence remains absent. |
| Concurrent marks and interrupted stamp | Markers survive; inspect the recorded commit before recovery. |

Use one commit operation at a time per worktree. Separate worktrees still share
the notes ref and can encounter a busy writer. If a stamp is busy or its outcome
is unknown, inspect its target before continuing.

Follow [sender buffer operations](../capture/local-capture.md#preserve-prepared-payloads-through-outages).
Buffer enrollment covers pi decisions and opted-in transcripts, with separate
gateway enrollment. It doesn't cover Cursor hooks, native Codex telemetry, or
forge webhooks. Retain receipts before they expire. Stopped workers don't enforce
expiry, and the buffer cannot recover data lost before enqueue. Capacity refusals
remain capture loss even when refusal behavior passes its test.

## Verify Derivations and review Attribution

1. [Freeze the evidence boundary](measure-agent-work.md#preserve-an-operational-result),
   including Facts, cutoff, cohort, quarantine revision, resolved policy and
   digest, software revision, and mirror objects/refs.
2. Build twice into fresh destinations and compare identities, counts, and
   hashes. Run shuffled-input determinism tests; repeated commands alone don't
   test independence from ingest order.
3. Inspect positive, negative, ambiguous, and quarantined controls. Check skip
   reasons. Quarantine and release must change visibility and Provenance without
   rewriting Facts.
4. Reconcile Facts, Attributions, Attributed completions, Rollouts, Segments,
   candidates, and output rows as separate populations. Preserve bundle and
   projection exclusions. Late observations mustn't alter an earlier cutoff.

Review a representative sample of Inference call–commit–file predictions across
Sessions. Keep challenge cases, such as similar code, concurrent Sessions,
renames, rejected work, and tenant/repository controls, separate from that sample.

Have reviewers judge relationships without predicted scores or methods. Retain
unknowns and disagreements. Report precision, uncertainty, conservative bounds,
and Session clustering. Recall requires independently enumerated true links.
Investigate wrong-tenant and wrong-repository associations before using the data.

Synthetic fixtures verify mechanics. Re-derive before evaluating a lower
threshold; filtering cannot recover discarded candidates. Keep tuning and
held-out cases separate. Before using Confidence as a probability or sample
weight, validate calibration against independent judgments for that recipe and
source. A score of 0.9 doesn't establish 90% correctness.

## Qualify exports with the intended consumer

If training is in scope, choose the [objective](../exports/training-exports.md),
Evidence recipe, [consumer profile](../exports/consumer-compatibility.md),
model/tokenizer, and environment before export.

1. Use a fresh destination. Validate the bundle and retain identities, hashes,
   splits, policy, quarantine revision, and recipe versions. An empty export
   can leave old files in a reused directory.
2. Verify exact rows or exclusions for positive and negative controls. Preserve
   source-to-row traceability and skips; zero output doesn't verify a positive case.
3. Check distinct chosen/rejected responses for direct preference optimization
   (DPO), eligibility for supervised fine-tuning (SFT), and Reward sources for
   reinforcement learning from verifiable rewards (RLVR).
4. Audit repeated tasks and near-duplicate prompts across splits. Session splitting
   doesn't isolate them. Freeze held-out data before training or tuning.
5. Run a consumer batch. Inspect templates, tokenization, tool content, loss masks,
   and DPO contrast after transformation. For training, verify finite loss and an
   intended training step; successful loading alone is insufficient.
6. For executable RLVR or Recovery, run positive and negative verifier controls.
   Retain environment digests, commands, exit status, and before/after results.
   Historical CI results don't establish an executable environment.

Test model improvement separately with a baseline, held-out metric, and
predeclared outcome criterion.

## Measure capacity under representative load

1. Measure retained populations and growth. Historical call identities and
   repository identity/rename populations have independent 50,000-row caps.
   A smaller recent cohort doesn't remove those requirements.
2. Record expected payload, Session, repository, Push range, concurrent report,
   and buffered outage sizes. Row limits don't bound decoding memory or cost.
3. Exercise ingestion, mirrors, reports, exports, bursts, idle periods, and
   worker restarts. Measure latency, errors, database waits, memory, disk,
   queue peaks, test duration, and headroom. Report refusals separately.
4. After failures, verify worker capacity reuse and reconcile source records.
   Don't split organization evidence to hide ambiguity or bypass a cap.

Review [process limits](deploy.md#84-network-exposure) before changing topology.
More API processes multiply host and database demand; configured limits don't
establish a throughput guarantee.

## Restore backups and investigate failures

1. Set acceptable data loss and time to usable service. Verify recovery access
   independently of the deployment.
2. Follow [Back up and restore](deploy.md#back-up-and-restore). Preserve encrypted
   off-host backups, mirrors, pinned software, configuration, and manifests.
   Database dumps exclude mirrors and exports. Pause capture and mirror work
   when needed to make a coordinated copy.
3. Restore into an empty, isolated database with matching software and role
   provisioning. Never target the operating database.
4. Verify schema, privileges, Facts, Session metadata, quarantine state, and Git
   objects. Reproduce a result at its fixed boundary, then verify fresh capture.
5. Measure data loss against independent source records and time to verified
   output. Include lost sender storage. Git refetch depends on remote retention
   and doesn't replace a backup.
6. Rehearse [quarantine](deploy.md#83-quarantine-and-wholesale-deletion), including
   affected-evidence identification, downstream suspension, incident records,
   and communication of limits.

Monitor backlogs, receipt failures, missing events, mirror outcomes, evidence
caps, resources, export skips, and backup age. Match frequency to your detection
objective. Repeat affected checks after upgrades, outages, credential changes,
or material configuration changes.

If you find unapproved content, corrupted evidence, unexplained loss, incorrect
associations, or invalid training inputs, stop the affected capture or downstream
use. Record the cause, scope, repair, reconciliation, and repeated checks before
resuming. Preserve absence instead of rewriting Facts. Keep private payloads out
of public issues and documentation.

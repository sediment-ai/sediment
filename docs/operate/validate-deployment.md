# Validate a deployment

Use this procedure to verify capture, evidence integrity, recovery, and capacity
for a self-hosted Sediment deployment. Record what you test and retain the
evidence in protected storage outside the documentation tree. Use the
[capture enrollment runbook](run-pilot.md) for installation commands.

A completed check establishes the tested configuration and workload. It doesn't
establish capture of unobservable activity, calibrated Confidence, model
improvement, or an availability guarantee.

## Record the configuration and workload

Before collecting evidence, record the following information:

| Record | Include |
| --- | --- |
| Scope | Organization, repository provider/host/ID, harnesses, models, approved capture channels, and excluded uses |
| Software | Full server/client revisions, package versions, image digests, runtime versions, lockfile digest, harness versions, and consumer profile/version |
| Infrastructure | Host resources, quotas, network paths, database/mirror storage, credential authorities, sender supervisors, and backup destination |
| Evidence boundary | Timezone-aware observation window and cutoff, Fact snapshot, policy, quarantine revision, and required mirror objects/refs |
| Workload and objectives | Expected event volume, payload sizes, concurrency, delivery latency, and acceptable recovery time and data loss |
| Evidence storage | Location, access controls, retention, source records, logs, artifact hashes, and review results |
| Limits | Unsupported source shapes, best-effort channels, unverified environments, and observation caps |

Select objectives for your workload before measuring it. Record results as
verified, failed, unverified, or not applicable, with evidence or a scope reason.
An unavailable runner or missing source record leaves the affected check
unverified. Retain earlier attempts when you repeat a check.

## Verify the installed build

1. Run the required continuous integration (CI), shim, schema-compatibility, and
   pinned consumer checks for the selected revision. Record failures and skips
   by name; a job that never starts doesn't establish a result.
2. From a clean checkout and disposable database, run the installed
   [release rehearsal](rehearse-release.md#run-the-no-publish-rehearsal).
   Retain its revision, runtime versions, acceptance record, and artifact hashes.
   Verify its exact synthetic counts and replay results.
3. Record the installed artifact identities. The synthetic rehearsal verifies
   its named stages; use real harness trials to verify your capture sources.

## Verify privacy, authorities, and source coverage

1. Confirm each channel's payload with participants before enabling capture.
   Native Codex telemetry can contain patch arguments without transcript
   enrollment. Gateway capture contains inputs and outputs. Transcript capture
   adds applied or observed text.
2. Verify organization binding, separate ingest and operator authorities, and
   authenticated network paths. Verify that operational reads require operator
   authority. Check private-repository credentials and restricted credential
   files through the [security procedure](security.md) and
   [deployment guide](deploy.md).
3. Record each harness's actual executable path and version. Installing the pi
   shim's development dependencies doesn't install the pi runtime. Verify the
   selected Codex command-line interface (CLI) profile; that result doesn't
   certify Codex Desktop.
4. During a marker-client upgrade, pause hooks across linked worktrees,
   reconcile pending markers, update every helper, and restart the harnesses.
   Don't mix old and generation-aware marker writers.
5. For each buffered channel, verify consent, private persistent storage,
   quotas, a supervised worker, and restart after login or reboot. Follow the
   documented unredacted-content handling. If you require encryption, verify
   that the storage layer provides it.
6. Run enrollment and a Session-specific doctor check through the
   [capture enrollment runbook](run-pilot.md). After a controlled flush, verify
   delivery status reports a running worker with no blocked or pending entries.
   Record unbuffered channels separately.

Keep these source limits visible when interpreting results:

| Source | Supported evidence | Limit |
| --- | --- | --- |
| Cursor desktop | Commit Attribution and implicit accepts for supported successful Agent writes | No native Inference calls or Edit observations; Tab changes don't imply a Developer decision |
| pi | Implicit accepts, commit Attribution, and opted-in transcript observations | Successful tools aren't human-explicit accepts; Inference calls require an approved gateway route |
| Codex CLI | Supported native decisions and completed single-file transcript changes | Automatic approvals aren't human-explicit judgments; multi-file transcript patches exceed the documented extractor scope |
| Gateway | Calls for the configured model/provider route | Native calls that bypass the gateway aren't in its denominator |
| Forge | Push, pull-request, and CI evidence from configured deliveries | Push receipt doesn't prove mirror refresh or Session observation |

Stock pi/Cursor implicit accepts don't satisfy the human-explicit accepted-work
population. An empty panel can be correct for that scope.

## Reconcile capture against independent records

Record controlled actions before inspecting Sediment's output. For each action,
retain its Session, repository identity, harness/version, source time, supported
shape, expected Fact kinds and multiplicity, and independent source evidence.
Use gateway request and forge delivery records for those channels.

Include successful, failed, and canceled tools; explicit decisions where
available; repeated delivery; empty work; long Sessions; single-file and
multi-file changes; and disabled consent. Test every enabled source version.
Keep unsupported cases in the record even when no Fact is expected.

1. Match each supported action to its expected unique Fact IDs after the
   controlled flush. Compare content, source IDs, event times, and repository
   identity as well as counts. Record where delivery stopped: emitted,
   prepared, queued, acknowledged, or stored.
2. Isolate or serialize controlled OpenTelemetry Protocol (OTLP) trials to
   associate each completed receipt with its request and attempt. Retain the
   protected request digest, source IDs, and receiver counts. Receiver count
   logs don't carry the sender delivery ID. If you can't independently correlate
   concurrent requests, mark request reconciliation unverified and use
   source-to-Fact identity checks for the measurable population.
3. At record grain, verify `received = translated + untranslated + malformed`.
   Count malformed containers separately. At each Fact-kind grain, verify
   `candidates = stored + duplicates`. Context-only untranslated records aren't
   automatically missing Facts.
4. If a request fails after partial storage, reconcile its retained prefix and
   replay by Fact identity. A failed request has no completed receipt. Replay
   attempts aren't independent source events.
5. Inspect the Session note, remote notes ref, stored Session-to-commit
   observation, and derived Attribution separately. Record quarantine,
   Attribution skips, and export exclusions separately from ingestion counts.

Record exact losses, duplicates, decline reasons, count units, and unresolved
observations. If no independent source denominator exists, report counts and
coverage limits without a capture-completeness percentage. `/health` establishes
liveness; Session doctor establishes limited presence. Neither proves
completeness. For latency, distinguish event time, extraction or preparation,
receipt, and storage; record clock uncertainty across hosts.

## Exercise delivery and forge recovery

Run fault trials on an isolated deployment and repositories that you control.
Record fault times, workload, recovery actions, Fact identities, payload hashes,
and remaining loss. Test each enrolled channel's actual recovery behavior.

| Trial | Verify |
| --- | --- |
| Buffered receiver outage and sender restart | Prepared payloads survive; replay after source-file changes preserves their bytes, identities, and observations |
| Acknowledgment lost after commit | Replay retains original Fact IDs without adding logical Facts |
| Worker termination and host restart | The supervisor restores one worker; inspect pending/blocked counts and oldest age, then measure backlog drainage |
| Bad credential, rotation, and wrong destination | Blocked entries remain visible; correction and deliberate retry don't log secrets or silently redirect payloads |
| Buffer capacity and expiry | Per-entry, total-byte, entry-count, and replay-window declines remain visible and counted; partial publication isn't complete-Session success |
| Unbuffered source outage | The source's own retry and loss behavior is measured separately |
| Push with notes and CI | Forge records, remote note edges, and retained Facts reconcile for the exact repository and commit |
| Failed private Git fetch, credential repair, and delivery replay | Retained Push and missing observations stay visible; verify recovery for the original commit |
| Branch creation, force Push, and a range over 20 commits | Head-only or capped processing stays explicit; omitted commits aren't covered |
| Note arriving after its triggering Push | Historical absence stays absent; later observations carry their actual capture time |
| Concurrent marks and interrupted stamp | Concurrent markers survive; inspect the pinned commit and note before attempting recovery on another commit |

Use one commit operation at a time per worktree. Use separate worktrees for
concurrent commits. If a stamp reports busy or an unknown outcome, inspect its
target before continuing. Linked worktrees share the notes ref, so separate
worktrees don't eliminate a busy notes-writer result.

The sender buffer covers enrolled pi decisions and opted-in transcript paths,
with separate gateway enrollment. It doesn't cover native Codex telemetry,
Cursor hooks, or forge webhooks. Follow
[workstation recovery](run-pilot.md#verify-workstation-recovery) and
[prepared-payload operations](../capture/local-capture.md#preserve-prepared-payloads-through-outages).

Default buffer ceilings are 8 MiB per entry, 256 MiB total, 2,048 active entries,
and a 24-hour replay window. Worker maintenance enforces expiry when it runs;
a stopped host can retain content longer. Retain evidence before seven-day
terminal-receipt retention expires. Queued/delivered counters don't establish
stored-Fact counts. The buffer can't recover events lost before enqueue.
Expected capacity refusals verify refusal behavior; they still represent loss
when they occur during ordinary collection.

## Verify Derivations and review Attribution

1. Freeze the evidence boundary through
   [Preserve an operational result](measure-agent-work.md#preserve-an-operational-result).
   Include the snapshot, cutoff, cohort, quarantine revision, resolved policy
   and digest, source revision, and required mirror objects/refs.
2. Build the canonical bundle twice into fresh destinations. Compare source
   identities, canonical counts, and hashes. Run the implementation's
   shuffled-input determinism tests; repeated command runs alone don't test
   arrival-order independence.
3. Inspect positive, negative, ambiguous, and quarantined controls. Verify skip
   reasons and the absence of invented associations. In the isolated deployment,
   quarantine/release changes visibility and Provenance without rewriting Facts.
4. Reconcile Facts, Attributions, Attributed completions, Rollouts, Segments,
   eligible candidates, and emitted rows as separate populations. Preserve
   upstream bundle exclusions alongside projector skips. Late observations
   mustn't change a historical view whose cutoff precedes them.

For accuracy review, freeze a representative sample of predicted
Inference call–commit–file links by method and use. Sample across Sessions;
related edits aren't independent observations. Keep challenge cases, such as
similar code, concurrent Sessions, renamed files, rejected work, and
repository/tenant controls, separate from the representative estimate.

Have reviewers judge source relationships without seeing predicted scores or
methods. Record disagreements and unknowns without forcing them into correct
labels. Report precision over reviewed predictions, unknowns, conservative
bounds, uncertainty intervals, and Session clustering. To estimate recall,
independently enumerate true links; predictions can't supply that denominator.
Investigate every wrong-tenant or wrong-repository association before using the
affected evidence. If coverage is insufficient, narrow the claim or collect
more evidence.

Synthetic precision fixtures verify matching mechanics. A post-filtered
threshold sweep can't recover candidates discarded by the initial policy;
re-derive before evaluating a lower threshold. Keep tuning cases separate from
held-out review cases. If you use Confidence as a probability or sample weight,
qualify calibration with independent judgments for that Evidence recipe and
source. An uncalibrated score of 0.9 doesn't establish 90% correctness.

## Qualify exports with the intended consumer

If you export training data, select the objective, Evidence recipe, consumer
profile/version, model/tokenizer, and environment before export. Follow
[Training exports](../exports/training-exports.md) and
[Consumer compatibility](../exports/consumer-compatibility.md).

1. Export to a fresh destination. Validate the bundle and retain its manifest,
   identities, hashes, splits, policy, quarantine revision, and recipe versions.
   Old files can survive an empty export in a reused directory.
2. Verify that positive and negative controls produce the exact expected rows
   or exclusions. A successful exit with zero rows doesn't verify a positive
   case. Preserve upstream and projection skips and source-to-row traceability.
3. For direct preference optimization (DPO), verify distinct mapped chosen and
   rejected responses and recipe label sources. Independently verify supervised
   fine-tuning (SFT) eligibility and reinforcement learning from verifiable
   rewards (RLVR) Reward sources.
4. Check leakage across the experiment's relevant units, including repeated
   tasks and near-duplicate prompts. Session splitting doesn't isolate those
   across Sessions. Freeze held-out data before training or tuning.
5. Run a real batch through the consumer. Inspect template/tokenizer behavior,
   tool content, target/loss masks, and DPO contrast after transformation. For a
   trainer, verify finite loss and an intended training step. A loader check
   doesn't establish that intended tokens receive training loss.
6. For executable RLVR or Recovery, run the verifier on positive and negative
   controls in the declared environment. Retain environment digests, commands,
   exit status, and expected before/after results. Historical CI results don't
   qualify an executable environment.

Consumer qualification doesn't establish model improvement. Evaluate that
claim through a separate experiment with a baseline, held-out metric, and
predeclared outcome criterion.

## Measure capacity under representative load

1. Measure existing organization populations and projected growth. Historical
   call identities and repository identity/rename populations each have an
   independent 50,000-row completeness cap. A smaller recent cohort doesn't
   remove those historical requirements.
2. Record maximum expected payload, Session, repository, Push range, concurrent
   report count, and buffered outage volume. Row-count limits don't bound
   output-message bytes or decoding cost.
3. Exercise representative ingestion, mirror work, reports, exports, traffic
   bursts, idle periods, and worker restarts in an isolated deployment. Measure
   latency, errors, database waits, memory, disk, and queue high-water marks.
   Record the tested duration and headroom. Report capacity refusals separately
   from successful results.
4. After timeouts or failed workers, verify capacity reuse and reconcile the
   independent source record. Don't split organization evidence to hide
   ambiguity or avoid a completeness cap.

Each application programming interface (API) process has two read workers with
no waiting queue, two mirror workers, sixteen waiting mirror jobs, a 30-second
read deadline, and a 120-second mirror deadline. More processes multiply host
and database demand. Consult [Network exposure](deploy.md#84-network-exposure)
before changing topology. These bounds don't establish a throughput guarantee.

## Restore backups and investigate failures

1. Record your recovery point objective (acceptable data loss) and recovery time
   objective (time to usable service). Verify recovery access independently of
   the failed deployment.
2. Follow [Back up and restore](deploy.md#back-up-and-restore). Preserve an
   encrypted off-host database backup, required mirror objects/refs, pinned
   software, configuration, policies, and protected artifact manifests. A dump
   excludes mirrors and exports. Establish a consistent boundary, such as
   quiescing capture and mirror work for a coordinated copy.
3. Restore into an empty database and isolated service using the matching
   revision and role-provisioning procedure. Never target the operating database.
4. Verify schema, privileges, Fact identities/counts/content, Session metadata,
   quarantine state, and Git objects. Reproduce a historical bundle/report/export
   at its fixed boundary, then verify fresh capture and authenticated access.
5. Measure data loss against the restored cutoff and independent source record,
   including lost sender storage. Measure recovery time through verified usable
   output. Refetching Git objects depends on remote retention; it isn't a backup.
6. Rehearse capture-loss and incorrect-evidence incidents through the
   [quarantine procedure](deploy.md#83-quarantine-and-wholesale-deletion).
   Verify that you can identify affected evidence, stop its downstream use,
   preserve incident records, and communicate its limits.

During operation, monitor delivery backlogs, receipt failures, missing events,
mirror outcomes, evidence caps, resources, report failures, export skips, and
backup age. Match monitoring frequency to your detection objective. After an
upgrade, credential change, outage, or material configuration change, repeat
the affected checks.

If you find incorrect associations, unapproved content, corrupted evidence,
unexplained loss, or invalid training inputs, stop affected capture or downstream
use. Record the cause, affected population, repair, reconciliation, and repeated
checks before resuming. Keep unrecoverable absence visible; never rewrite Facts
to make verification pass. Preserve diagnostics without copying private source
payloads into public issues or documentation.

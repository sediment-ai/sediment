# Shared bounded admission for evidence reads

Implementation tracker: [Issue #86](https://github.com/sediment-ai/sediment/issues/86).

## Problem

PR #85 reduces evidence-worker startup and adds exact reads, but evidence can occupy only one of two existing query/report slots. Synchronized five- and ten-agent bursts complete one flow and refuse four or nine, despite the second read slot being unused.

## Decision and scope

Let all evidence operations share the existing fixed two-slot query/report pool. Remove the evidence-only sublimit and its redundant bookkeeping in `WorkerSupervisor`. Preserve the aggregate two-worker ceiling, fail-fast capacity 503, 30-second deadline, process-group cleanup, worker isolation, operation byte limits, authority, and request-local Quarantine. No setting, waiting queue, retry, additional worker pool, dependency, database change, or persistent state.

This explicitly removes ADR 0021's reserved report-admission slot. Two evidence reads can therefore cause a concurrent report to receive the existing capacity refusal. The change does not promise fairness or report latency. Mirror workers and gateway ingest retain their existing paths.

The baseline is merged PR #85, commit `6d8c56dbe19dad1ebda36d0408a76d4e33410a05`. Follow the scaling-evolution and Supabase PostgreSQL guidance: use the existing resource budget before adding capacity. API remains two CPUs/2 GiB; database remains one CPU/1 GiB in deployment resource checks. Parent pool and worker connections keep the existing application ceiling.

## Acceptance

1. Every evidence kind can use the second slot. The third read refuses without spawning. Evidence, commit/Session queries, and reports share the same aggregate count.
2. Real subprocess tests use explicit barriers to verify occupancy, cancellation, failure/deadline cleanup, sibling survival, shutdown, and capacity release only after owned process groups exit. Retain existing trust-boundary, Quarantine, and exact-value checks.
3. Real gateway capture stores and reconciles a Fact while both read slots are occupied; health responds.
4. Compare the frozen merged #85 runtime with the candidate using the repaired benchmark: near-limit keyword and known-reference profiles; 30 single-client samples and ten synchronized waves at five and ten clients. Separate successful latency, throughput, and refusals. Preserve fixture/evidence hashes. Query/source-selection code does not change, so unrelated-history tests need no full remeasurement.
5. Exercise two simultaneous near-limit reads and representative object/token expansion shapes in owned containers under deployment CPU/memory limits. Retain cgroup memory peaks/events, database observations, capture receipts, health, and cleanup. Reject the policy change if it yields no throughput benefit or lacks measured memory headroom. Synthetic results qualify only the tested workloads, not all JSON shapes or partner capacity.
6. Amend the governing ADRs and maintained docs to remove the report reservation explicitly. Update the changelog. Independent implementation review and focused different-vendor review precede a review-ready PR.

## Tasks

- T1: shared admission implementation and behavioral regression tests.
- T2: paired public-path performance and container resource measurements.
- T3: documentation, security source review, and release-check closeout.

No change to Facts, Derivations, training labels, selection quality, or per-operation limits. This issue does not include automatic retry guidance tracked in #45.

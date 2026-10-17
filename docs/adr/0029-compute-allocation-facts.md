# ADR 0029 — Compute allocations are Facts; their effect is a Derivation

Status: accepted

## Context

Some execution platforms let a coding agent request more CPU or memory for
its Session. Granting every request turns "the agent asked for it" into an
autoscaling policy that nobody measured. An operator needs per-Session
evidence of what each Session ran with, who chose that allocation, and how
long the Session's work took.

Sediment captured the timing half: gateway Inference calls carry
`observed_at` and a gateway-measured `duration_ms`. No Fact, column, or
captured attribute recorded an execution environment's limits.

The question also has a causal trap. An agent that asks for more compute is
often working on a harder task. A report that compares requested allocations
with default ones can show more compute running slower, and the cause is the
task rather than the compute. Only random assignment separates the two.

## Decision

Sediment stores a Session-scoped `ComputeAllocation` Fact each time an
execution environment's allocation takes effect: once at start and again on
every resize. The Fact records the Session, the CPU limit in millicores, the
memory limit in bytes, the `allocation_source`, and the sender's
`occurred_at`. At least one limit is present; an absent limit stays null.
It stores limits, not usage.

`allocation_source` is a closed vocabulary: `default`, `agent_request`,
`operator`, and `randomized`. The sender declares it. A platform that
randomizes whether to grant a request records `randomized` for both the
granted and the declined branch.

`POST /ingest/compute` accepts the Fact from a bearer-authenticated
capture credential. Sediment stores the sender's assertion and doesn't
verify it against the platform, as with vendor-neutral CI outcomes
(ADR 0010). PostgreSQL deduplicates on the complete allocation:
organization, Session, `occurred_at`, source, and both limits.

`SessionComputeTiming` is a pure Derivation over Compute allocations and
Inference calls. The final allocation is the one with the latest
`occurred_at`; a tie between different values skips the Session. Each call
spans `[observed_at - duration_ms, observed_at]` in the gateway's clock.
Elapsed time runs from the earliest call start to the latest call end.
Between-call time is elapsed time with no call in flight. Allocation time
orders only allocations and is never compared with gateway time. A Session
without a visible call, or with a call that lacks `duration_ms`, is skipped
and counted.

The compute report groups Sessions by final allocation and compares median
durations with a fixed-seed bootstrap interval. It compares arms only within
one allocation source and marks each comparison `randomized` or
observational. It never presents an observational difference as an effect.

## Consequences

- Operators can retrieve each Session's allocations through the Session
  dossier and the report's per-Session rows.
- A causal answer requires the platform to randomize allocations and record
  `randomized`. Sediment can't recover random assignment from observational
  evidence.
- The report covers only Sessions whose calls pass through a gateway that
  measures `duration_ms`. Transcript-only capture produces no timing.
- A Session is the unit. A long interactive Session that holds several tasks
  compares as one, and between-call time includes time that a person spends
  waiting.
- A Session that changed allocation is compared under its final allocation.
  This measures the policy decision, granting a request, not the effect of
  each allocation interval.
- A randomized Session whose final allocation comes from another source
  leaves the randomized arms. The report counts those Sessions, because their
  departure can bias a randomized comparison.
- Session compute timing is a diagnostic. No Evidence recipe, Reward, or
  training row consumes it.
- Usage measurements, such as CPU seconds and peak memory, remain outside
  this decision.

# ADR 0024 — Commit investigations select work before loading content

Status: accepted

Implementation tracker: [Issue #79](https://github.com/sediment-ai/sediment/issues/79).

Preserves [ADR 0009](0009-canonical-attribution-contract.md),
[ADR 0013](0013-git-note-observation-facts.md), and
[ADR 0020](0020-bounded-derivation-execution.md). Amends the operational
repository evidence reads in [ADR 0019](0019-repository-identity-and-renames.md).

## Context

A commit investigation can return one Inference call after deriving every
eligible commit in an organization. The candidate window spans all selected
Pushes, so unrelated historical output reaches Python and the scorer. Complete
repository evidence also repeats the same name and identity across many Facts.
The complete-history row cap can refuse an investigation whose relevant evidence
fits within the query budget.

## Decision

Decision: select the target commit's owning Push before loading Attribution
content. Use compact repository metadata witnesses for operational commit
investigations. Keep the complete-history readers for consumers that declare
complete source populations, including bundles and reports.

### Target Attribution

The target-only entry point reads its Push metadata from the caller's Fact
snapshot. It selects the earliest eligible Push, ordered by capture time and
Fact ID, whose capped commit list contains the target in each qualified
repository. Ownership doesn't depend on whether that Push produces a match.
SQL uses `C` collation for Fact IDs to match Python's tie-break in every locale.
The commit cap, range fallbacks, similarity thresholds, and tie-breaking rules
retain their meanings.

The reader admits organization-wide candidates within that owner's Jaccard
window. The longer Git-note window admits only Sessions named by eligible
captured observations for the target. Both windows stop at the historical
boundary. Separate owners keep separate windows. The deriver diffs and scores
only the target commit and retains existing content refusal limits.

An explicit captured observation map, including an empty map, supplies note
Sessions. A mutable mirror note cannot change a historical investigation.
Captured observations remain the sole source of factual Session-to-commit
membership. Similarity-selected calls remain inferred evidence.

### Repository metadata witnesses

PostgreSQL selects existing scalar source representatives that preserve every
eligible repository name and identity claim. Ordinary evidence retains every
distinct identity/name state for each source role. Observation representatives
also distinguish the visible source Push's presence, identity, and name. The
reader includes those exact Push anchors. Rename representatives retain each
identity and old/new name pair.

The same read includes exact queried observation and CI source projections.
The pure repository resolver still verifies their source identity and
interprets names, claims, and inherited identity. Unresolved observations retain
their raw claims. Pull-request head roles, renamed labels, reused names, and
missing source Pushes remain visible to resolution.

Stored Push metadata has no source inheritance. The target-only reader resolves
its captured identity and name against the complete metadata context. That
shortcut applies only to Pushes that the reader obtains from the same snapshot;
it doesn't weaken the general resolver's exact-source checks for preloaded
Facts.

Witnesses contain original projections rather than fabricated Facts or
persisted resolver output. Complete source counts cannot be computed from the
compacted population. Complete bundle/report readers retain their declared
populations and diagnostics. The witness/source population retains a separate
explicit cap and refuses overflow instead of truncating proof.

All reads share organization, inclusive historical bounds, Quarantine, and one
read-only repeatable-read snapshot. This decision adds no stored state or schema
migration and changes no Fact, policy, training recipe, or response shape.

## Consequences

Call-content work depends on the target owners' eligible windows. Repository
transfer and Python memory depend on metadata diversity and queried source
evidence rather than repeated historical Facts. PostgreSQL still groups scalar
history to establish complete names and claims.

Finding an arbitrary non-head commit's earliest owner can still inspect earlier
Pushes and Git ranges. The reader groups at most 256 ordered Push projections.
One native Git ancestry check can reject a batch when every possible head
strictly precedes the target. The check excludes the target's parents and their
ancestry, with at most one remaining commit as a counterexample. Nonempty output
or a Git failure retains the exact per-Push range checks. An earlier non-head
owner still takes precedence over a direct-head owner in the same batch.

A miss can inspect the whole eligible Push population. Mixed or divergent
batches retain per-Push Git work, so worst-case discovery remains linear in
Push history. The batch changes neither captured membership nor policy caps.
Push metadata doesn't record complete commit membership, so an exact head index
cannot prove that an earlier Push omitted a capped non-head commit. Request
deadlines and resource refusals remain necessary.

Measurements must include the complete commit request, Python memory, repository
metadata work, and Git discovery. A selective lookup benchmark cannot establish
whole-query capacity. Separate growth in repeated history from growth in
repository diversity, target-window content, and time to discover an owner.

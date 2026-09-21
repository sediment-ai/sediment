# ADR 0020 — Bounded Derivation execution

Status: implemented

Amends [ADR 0016](0016-bundle-derivation-consistency.md). Preserves
[ADR 0001](0001-facts-not-derived-state.md),
[ADR 0012](0012-postgresql-fact-store.md),
[ADR 0015](0015-lossless-values-and-bundle-v2.md), and
[ADR 0019](0019-repository-identity-and-renames.md).

## Context

Complete evidence does not require simultaneously resident payloads. Eager Fact
reads, snapshot content caches, complete bundle objects, semantic validation,
and whole-file serialization make peak memory grow with historical payload
volume. Row limits do not bound message bytes. Disposable worker processes share
their container's memory budget.

## Decision

Decision: preserve complete evidence while bounding decoded content lifetime.
FactStore owns consistent, ordered incremental reads. Derivation orchestration
owns complete dependency groups. Bundle consumers can use validated file-backed
records; explicit materialization remains available for small workloads.

### Evidence and snapshots

One run uses a read-only repeatable-read Fact snapshot, its quarantine revision,
resolved policy, and stable mirror view. Every batch belongs to that same view.
An `as_of` timestamp alone does not replace transaction consistency.

Compact identity and supporting-evidence indices may span an organization.
Their established row limits remain. Cohort selection cannot narrow declared
alias ambiguity, repository identity evidence, or complete eligible CI lineage.
Content reads retain only the active bounded group and read/write buffers.
Snapshot caches cannot retain all decoded input histories or raw payloads.

The Session is the complete Rollout work unit. A Rollout contains all its
declared calls. Other consumers retain their own complete dependencies; DPO
prompt/model comparisons can span Sessions. Existing pure algorithms own these
relationships and all diagnostic vocabularies.

### Bundles and publication

Bundle access uses bounded records and explicit materialization under the
validation contract in ADR 0016. Bundle v4 fields, source populations, ordering,
lossless record encoding, counts, and hashes remain unchanged. An execution-only
change does not advance a schema, recipe, or Derivation policy version.

Incremental readers check file integrity and complete semantic relationships.
An unchecked record stream is not a trusted bundle. Training consumers cannot
substitute checksums for semantic validation or publish before all required
checks succeed. Cross-record validation can use compact indices and additional
passes; complete source records need not stay resident together.

Writers incrementally encode records and compute exact hashes, sizes, and counts.
They write the manifest after members succeed and retain atomic publication.
Failures preserve existing destinations and remove incomplete private staging.

Private temporary files may hold lossless run inputs or unpublished artifacts
for bounded execution. They are disposable execution resources, never persisted
domain state, a reusable report cache, or a second FactStore. Their owner controls
permissions, lifetime, cleanup, and byte capacity. PostgreSQL remains the sole
Fact store.

### Serialization across export modes

Decision: preserve each mode's historical bytes; compare parsed values across
modes. The canonical bundle serializes nested objects with sorted keys. A direct
training export keeps the source Fact's key order. Both byte sequences are
retained as published. Equality between a direct export and a bundle-backed
export of the same Derivation means equal parsed JSON values in equal row
order, never byte equality. Byte determinism holds within one mode: repeated
exports of the same inputs through the same mode produce identical bytes.
Normalizing key order in either mode would change published bytes and is not
adopted.

### Resource refusal

The execution contract includes payload byte, complete-group, and temporary-file
budgets. Checks must precede oversized decoding or staging growth. A capacity
failure is an explicit operation failure, never a semantic skip, truncated
population, partial Session, or successful smaller dataset. Existing controlled
HTTP failure handling remains the report interface.

The working set includes bounded shared context, the active dependency group,
and bounded buffers. This does not claim constant memory for arbitrary records
or arbitrary organization populations. Limits and supported workloads require
public-consumer measurements, including simultaneous workers where supported.

## Consequences

All-history Derivations remain recomputable. Bounded execution can require more
passes and temporary disk access. Long-running database snapshots and mirror
locks retain their operational cost; streaming does not shorten them by itself.

Validation remains mandatory at trust boundaries. Explicit materializing callers
remain responsible for their declared small-data envelope. Captured evidence is
not truncated to reduce memory. Physical content deduplication, persistent
Derivation caches, and distributed execution require separate decisions.

[Profile reports and Derivations](../operate/profile-derivations.md) defines
workload measurements and resource budgets.

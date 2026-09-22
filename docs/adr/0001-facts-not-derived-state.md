# ADR 0001 — Persist facts; derive everything else

Status: accepted

## Context

Persisting matcher output as source data ties each result to the policy and
arrival order that produced it. Later matcher improvements cannot recompute
missing inputs, and webhook retries require application-level repair logic.
Sediment needs retained observations that remain useful when policies change.

## Decision

Only **facts** are persisted: completions, developer decisions, pushes, CI
outcomes (PostgreSQL), plus the git mirror (full history + notes refs).
Correlation, reward linkage, triple assembly, and export are **derivations**:
pure functions of `(facts, policy)`, recomputable over all history, versioned
by the policy that produced them. A webhook is a *trigger* that may refresh a
derivation cache; the cache is never a source of truth and can always be
dropped and rebuilt.

[ADR 0023](0023-indexed-call-identifiers.md) permits an exact physical index
representation of identifiers already present in Inference call Facts. It
stores no attachment verdict or policy output; canonical Facts remain the
source of truth.

## Consequences

- No idempotency machinery beyond `UNIQUE` indexes on fact tables; no
  backfill healing; no first-write-wins mutation — webhook ordering cannot
  matter because derivations read facts, not arrival order.
- Policy/matcher changes re-derive history for free; near-misses are not
  lost because nothing below threshold is ever "not stored" — the facts are.
- Derivations must stay pure: any derivation reading wall-clock ingest order
  or mutating a fact table is an architecture violation.
- Cost: derivation needs a compute story (on-demand now; scheduled later).
  Acceptable at self-hosted single-org scale by design.
- Immutability creates an obligation: when bad facts are discovered (a leaked ingest token, a forged webhook, a
  corrupt translator), there must be a sanctioned way to exclude them
  without editing history — a **fact quarantine**: an append-only
  quarantine table consulted by every derivation-facing read, so poisoned
  facts drop out of all derived datasets on the next re-derivation while
  remaining stored for audit (and still blocking redelivery re-inserts via
  their UNIQUE keys). Facts are never deleted; they are quarantined.

## Bounded sender transport storage

[ADR 0017](0017-sender-transport-replay.md) permits
an opt-in sender-local buffer of prepared capture payloads and delivery metadata.
It exists only to deliver evidence to the PostgreSQL FactStore. Derivations never
read it; no Attribution, score, or training label is persisted there. Removing
a delivered transport copy doesn't mutate or remove a Fact. Its explicit consent,
capacity, replay window, and privacy limits remain part of that exception.

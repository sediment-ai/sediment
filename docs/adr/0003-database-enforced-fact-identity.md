# ADR 0003 — Database-enforced Fact identity

Status: accepted

## Context

Capture retries can deliver the same observation more than once. Application
scans and process-local locks cannot enforce identity across independent
writers. Fact identity and transaction boundaries belong in the database.

## Decision

`UNIQUE` indexes enforce natural Fact identity. A redelivery collapses at
insertion time and returns the retained Fact identity. Partial and expression
indexes represent optional key components without application-level dedup scans.
Fact batches and their Session metadata commit atomically.

[ADR 0012](0012-postgresql-fact-store.md) defines PostgreSQL as the sole Fact
store, the physical schema, transaction isolation, and migration boundary.
Pydantic Fact models remain the source of truth for validated domain shapes.

## Consequences

- Concurrent senders share one database identity rule.
- Redelivery succeeds without mutating an existing Fact.
- Application code does not reproduce database constraints with scans or locks.
- Schema changes preserve natural-key, receipt, and transaction contracts and
  include database-backed tests.

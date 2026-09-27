# ADR 0023 — Index captured call identifiers for Decision attachment

Status: accepted

Amends [ADR 0001](0001-facts-not-derived-state.md),
[ADR 0012](0012-postgresql-fact-store.md), and
[ADR 0015](0015-lossless-values-and-bundle-v2.md).

## Context

A Developer decision's `call_id` can match an Inference call's provider ID or
an output `ToolCallPart.id`. Attachment requires exactly one matching Fact in
the visible organization history. Restricting the search to the selected
Session or cohort can hide an older collision and attach evidence incorrectly.

Extracting identifiers from every retained output makes an interactive commit
investigation depend on unrelated content volume. PostgreSQL cannot parse the
serialized message fields under the lossless storage contract. Canonical
identifiers have no length ceiling, so an ordinary text B-tree index can also
reject an otherwise valid output tool identifier.

## Decision

Decision: the FactStore maintains an exact physical representation of captured
call identifiers. `inference_call_aliases` copies the organization, source Fact
ID, and each distinct provider or output tool-call ID. A deterministic ordinal
identifies each copied value within its source. Input-history identifiers,
tool responses, and arbitrary raw fields do not participate.

The canonical Inference call remains authoritative. The table is not a Fact,
an attachment result, or a Derivation cache. It stores no policy judgment,
uniqueness verdict, or inferred relationship. Canonical payloads, Fact counts,
exports, and schema versions retain their contracts. PostgreSQL does not parse
message content.

A native PostgreSQL hash index covers the single expression
`ARRAY[org_id, call_id]`. Exact array equality remains in every indexed lookup;
hash equality alone never establishes identity. The index admits long aliases
without an application digest or a length restriction. The source-Fact foreign
key is covered by the physical table's primary key.

### Atomic capture and migration

The storage seam copies identifiers from the Basic-redacted, validated Fact.
Only a successful parent INSERT writes aliases. The Fact, Session upsert, and
alias rows commit or roll back together. A duplicate delivery cannot add aliases
from incoming content that the database did not retain.

The parent stores a physical `call_alias_count` with no INSERT default. Its
non-null constraint rejects stale writers that omit the physical representation.
The count does not become a canonical Fact field or independently prove index
completeness. Atomic supported writes and a complete migration establish that
invariant; a foreign key alone does not.

The forward migration locks parent reads and writes and backfills every retained Fact,
including quarantined Facts. A frozen Python decoder reads only output messages
and existing scalar identifiers. It retains exceptional descriptive values in
the parent and copies only canonical identifier fields. It processes one output
row at a time and bounds insert batches. Memory still depends on the largest
source row and its identifiers.

Backfill, constraints, and index creation share the migration transaction. A
failure leaves the schema behind and rolls back partial physical state. Writers
stop during the maintenance window and restart on the matching build. Runtime
can read and append the physical rows; operator can read them. Neither role
can update or delete them. Sanctioned wholesale source deletion cascades to the
physical rows.

### Complete ambiguity checks with bounded witnesses

`read_inference_call_identity_witnesses` searches all visible source Facts for
each requested identifier. It applies organization equality, inclusive
`observed_at <= observed_through`, and effective Quarantine in the same
repeatable-read snapshot. It adds no Session filter or cohort lower bound.

One source Fact establishes a unique owner. Two distinct source Facts establish
ambiguity regardless of further matches. The reader selects at most two owners
per identifier in deterministic Fact-ID order and retains only the requested
aliases that those Facts witness. Several requested aliases can share one
returned `InferenceCallIdentity`. Repeated aliases within a Fact do not make
that Fact ambiguous with itself.

The reader refuses more than 30,000 distinct requested keys under the existing
composite-filter budget. It never treats a truncated uniqueness population as
complete. The two-witness bound is sufficient for the unique-or-drop Decision
join; it is not a complete list of matching Facts for general evidence reads.

Commit investigation reads metadata for its inferred calls and Decisions from
their actual Sessions, then requests witnesses for those Decisions' identifiers.
Alias lookup selects no input, output, or raw content. Unrelated large outputs
cannot cause an alias-decoding refusal. Attribution keeps its own content
budgets and wider candidate population.

Amendment, 2026-09-22: [Issue #93](https://github.com/sediment-ai/sediment/issues/93)
extends this attachment reader to scoped model and lifecycle reports. Each report
requests every non-null Decision identifier from its selected Sessions and checks
all visible organization history through `as_of`. More than 30,000 distinct keys
refuses the complete operation. The cohort and supporting-Fact bounds remain.
The report no longer materializes all organization call identities for attachment.

Complete-population identity reads used by bundles and unscoped consumers retain
their contract and limits. Witnesses cannot replace bundle identity evidence or
its offline completeness declarations.

## Consequences

Indexed attachment work follows the requested identifiers and their matching
source population. Many matching historical, future, or quarantined rows can
still increase database work. Query deadlines remain necessary. This decision
does not make the entire commit investigation independent of organization size:
Attribution, Push, and repository identity reads retain their wider populations.

Each distinct alias adds a physical row and index entry during capture. Upgrade
time, storage, and write overhead require measurement alongside read latency,
memory, and query plans. The migration requires a maintenance window; it does
not promise an online or constant-memory backfill.

## Contract checks

Store and public query tests compare indexed attachment with full historical
Facts under shuffled arrival, old and exact-boundary collisions, future Facts,
Quarantine/release, foreign organizations, repeated aliases, and three-or-more
owners. Snapshot tests retain visibility while another connection changes it.

Migration and capture tests cover exceptional content, long identifiers, real
hash collisions, atomic rollback, redelivery, stale writers, role grants, and
source deletion. SQL inspection excludes message/raw reads from lookup.
Synthetic `EXPLAIN (ANALYZE, BUFFERS)` measurements verify selective native-index
use as unrelated history grows; they do not qualify production capacity.

# ADR 0021 — Bounded evidence reads preserve source identity

Status: proposed

Date: 2026-09-21

Implementation tracker: [Issue #64](https://github.com/sediment-ai/sediment/issues/64).
The [implementation specification](../superpowers/specs/2026-09-21-context-evidence-design.md)
defines the version-1 wire shapes, limits, and acceptance criteria.

## Context

A coding agent in a fresh Session can need selected evidence from an earlier
Session. Sediment already captures structured Inference calls, but complete
Fact reads include unrelated content and provider payloads. Exposing those reads
would leave memory use and content disclosure dependent on the full history.

An evidence consumer needs exact source identity and bounded access. Selecting
useful context and deciding how to continue a task remain consumer concerns.
Retrieval alone cannot establish task success, code quality, or capture completeness.

## Decision

Decision: add three read-only operations over existing Inference call Facts:
a complete capped Session inventory, a selected-call message-part manifest,
and a fetch of explicitly selected parts. The alternatives are a stored
checkpoint service or a retrieval system with indexing and ranking. Both add
capture or interpretation decisions that this read interface doesn't require.

### Evidence identity and ownership

A reference names an Inference call Fact ID, input or output side, message
index, and part index. Ordered canonical content makes the reference stable
under [ADR 0008](0008-structured-inference-call-facts.md). Repeated input history
and repeated tool-call aliases remain separate occurrences. The interface
doesn't summarize, clip, deduplicate, merge, or replay message parts.

The requested Session scopes every read under
[ADR 0002](0002-session-aggregate-root.md). IDs travel in query parameters or
request bodies so valid IDs containing URL delimiters remain addressable.
Inventory reports complete visible storage coverage within its cap, with
`capture_completeness: "unknown"`. A Session with no captured Inference calls
cannot supply conversation content through this interface.

Only Facts remain persisted domain state under
[ADR 0001](0001-facts-not-derived-state.md). PostgreSQL remains the sole FactStore
under [ADR 0012](0012-postgresql-fact-store.md). Reads add no Fact, migration,
checkpoint, summary, cache, embedding index, or package. The single-team
interface remains open source under
[ADR 0006](0006-open-core-boundary.md).

### Authority and visibility

All operations require the deployment-wide operator authority from
[ADR 0018](0018-static-credential-authorities.md). The deployment supplies the
organization. A Session filter doesn't narrow the credential's authority.
Ingest credentials cannot read evidence. The reference command-line interface
(CLI) keeps operator credentials separate from harness capture configuration.

Each operation uses one read-only repeatable-read snapshot for scope,
Quarantine, byte preflight, and projection. The next request checks visibility
again. A reference identifies content; it doesn't grant continued access.
Separate requests share no database snapshot, watermark, or held transaction.
A backdated Fact can appear in a later inventory. A later Quarantine can refuse
a fetch but cannot recall a previously delivered packet.

Unavailable Facts share one failure shape whether absent, foreign,
cross-Session, or quarantined. Successful responses carry the snapshot's
Quarantine revision and `Cache-Control: no-store`. Reads exclude provider `raw`
and user identifiers. Diagnostics contain no message content or credentials.

### Capacity and representation

Version 1 fixes these operation limits: 1,000 inventory calls, 32 fetch
references, a 64 KiB fetch body, 8 MiB of selected stored variable-width
columns, and a 1 MiB encoded response. KiB and MiB use 1,024-byte units.
Source preflight includes selected scalar metadata as well as message columns.
It counts each selected row and column once before transferring those values.
A small part in an oversized source column can therefore be unavailable under
the source limit. PostgreSQL doesn't parse opaque message content to bypass it.

Capacity overflow refuses the complete operation. It never produces a
successful partial inventory or packet. Output encoding is bounded before
publication. Evidence uses at most one of the existing two query/report worker
slots and retains the 30-second deadline, cancellation, and process cleanup.
This reserves admission capacity; it doesn't guarantee database throughput or
report latency. These execution boundaries preserve
[ADR 0020](0020-bounded-derivation-execution.md).

Responses follow [ADR 0015](0015-lossless-values-and-bundle-v2.md): validate
declared Python shapes, then emit ASCII-escaped strict JSON. NUL, surrogate
strings, large integers, and finite numbers retain their values. An emitted
non-finite number refuses the whole response. A non-finite value in an omitted
part doesn't block its manifest or a finite selection. Evidence reads don't
use the bundle's `record_json` encoding. Some valid stored content therefore
remains unavailable through this operational interface.

### Reference consumer and training boundary

The operator CLI inventories, inspects, and fetches exact evidence over HTTP.
It bounds selection files and responses, validates complete success, and
publishes a mode-0600 packet atomically without replacing an existing path.
It calls no model or external retrieval service. A consumer can later use a
local selector or another retrieval model without changing source identity.
This decision adds no JEV integration or compatibility claim.

The reference demonstration resumes a controlled task in a fresh pi Session
with its original workspace intact. The operator verifies capture before
ending the source Session and records missing evidence explicitly. Historical
roles and tool calls remain data. The packet neither reconstructs the workspace
nor authorizes tool execution. Gateway and model endpoints must also remain
inside the perimeter for an entirely self-hosted demonstration.

Transcript capture remains narrow under
[ADR 0007](0007-transcript-parsing-client-side.md). The demonstration doesn't
extract a paused transcript to invent a checkpoint or freeze an early Edit
observation. It adds no canonical training artifact and preserves
[ADR 0004](0004-canonical-training-artifacts.md). Attribution, reports,
Derivations, Evidence recipes, and training exports retain their existing
interpretations. Retrieving a part never becomes a Reward or training label.

## Consequences

Consumers can reuse captured evidence through a bounded, source-linked
interface without loading a complete Session. Operators perform selection and
handoff explicitly. Autonomous read authority, semantic search, ranking,
context packing, and checkpoint capture require separate decisions.

The first implementation measures packet correctness and pipeline invariance.
An optional live demonstration records continuation outcome, bytes, latency,
and available token use. Fixture success doesn't prove crash recovery or
context-cost savings. Those claims require controlled comparisons that include
retrieval and inference costs.

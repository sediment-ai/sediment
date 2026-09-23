# ADR 0026 — Separate factual evidence access from usefulness selection

Status: implemented

Date: 2026-09-22

Extends [ADR 0021](0021-bounded-evidence-access.md),
[ADR 0022](0022-agent-requested-session-context.md), and
[ADR 0025](0025-authorized-session-candidate-discovery.md).
Implementation tracker: [Issue #84](https://github.com/sediment-ai/sediment/issues/84).
The [implementation specification](../superpowers/specs/2026-09-22-agent-evidence-performance-design.md)
defines the wire contracts, measurements, and acceptance criteria.

## Context

Keyword discovery can omit an authorized Session because its terms don't match,
its preview exceeds the response budget, or other candidates rank higher.
Selected-Session retrieval applies keyword selection again. A retrieval consumer
cannot use the operator-only inventory, manifest, or exact fetch to examine
evidence that the keyword baseline omits.

An external decision model needs to choose useful evidence without changing
what Sediment records or permits. Captured requirements and failed attempts
may have no commit observation. Git supplies code evolution; a captured
Session-to-commit observation supplies only its recorded relationship.

## Decision

Decision: expose the existing factual projections under the configured Session
grant. Sediment establishes source identity, existence, visibility, and bounded
read authority. The consumer judges usefulness. Keyword selection remains an
optional baseline with unchanged version-1 rules.

### Authorized enumeration and exact reads

The three `/query/context/evidence` operations reuse the inventory, manifest,
and exact part-fetch contracts from ADR 0021. Each request names a Session.
Retrieval and operator credentials can use these operations only within the
configured singleton or plural grant. The parent checks membership before
storage; the disposable worker repeats the check. Deployment configuration
still supplies the organization. Disabled retrieval disables these operations.
The existing `/query/evidence` routes remain operator-only.

`/v1/me` supplies the configured Session IDs. Inventory lists captured calls
without content. A manifest identifies canonical message-part occurrences.
Exact fetch accepts the references chosen by the consumer. None of these
operations needs a keyword query, utility score, commit witness, or model.
An omitted keyword candidate is not a denied read grant.

Each operation uses its own Quarantine-aware, read-only repeatable-read
snapshot. A reference cannot authorize a later read. Unavailable references
share a content-free refusal whether absent, foreign, cross-Session, or
quarantined. Responses exclude provider raw payloads and Fact user identity.

### Representation and capacity

Exact reads preserve occurrence identity, request order, repeated content at
distinct references, roles, finish reasons, and canonical scalar values.
They may return readable reasoning. The keyword baseline's reasoning exclusion
is a selection rule, not a per-part permission boundary. Historical roles and
tool calls remain evidence and never authorize execution.

ADR 0021's limits remain: 1,000 inventory calls, 32 distinct fetch references,
a 64 KiB streamed fetch body, 8 MiB of selected stored columns, and a 1 MiB
complete response. Exact fetch reads only the requested calls and message
sides. It still decodes each selected column; a small part inside an oversized
column can be unavailable. Overflow refuses the whole operation.

Evidence shares the two query/report slots under the amended admission contract
in ADR 0021. Deadlines, cancellation, process cleanup, strict JSON encoding,
and `Cache-Control: no-store` remain.
The native pi tools bound transport and forward validated original JSON text
so JavaScript cannot round canonical integers through reserialization.

### Consumer and training boundary

Native pi tools expose the granted Session set, inventory, manifest, and fetch
whenever retrieval is configured. Existing fixed-Session and discovery tools
keep their behavior. A consumer can use factual enumeration or an exact
keyword preview as the starting point for selection.

This interface adds no model adapter, vendor credential, learned ranking,
stored selection, Fact, or migration. A consumer's judgment does not become a
Fact, read grant, Reward, Confidence, or training label. Attribution and
training exports retain their existing inputs and interpretation.

## Consequences

An independent selector can inspect authorized uncommitted evidence that
keyword ranking omits. An already-known reference can avoid a complete Session
scan. Discovery itself retains its aggregate source limits and scan cost.
Exact fetch and keyword selection have different output semantics; their
timings cannot establish identical selection behavior.

Reproducible measurements separate successful latency, refusals, memory,
database work, startup, and selection. Scripted native acceptance proves
transport and authority. It does not establish JEV compatibility, semantic
recall, task improvement, or lower inference spending. Model-quality and cost
claims require a separate controlled evaluation.

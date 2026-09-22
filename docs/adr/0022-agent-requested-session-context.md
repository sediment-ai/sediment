# ADR 0022 — Agent-requested context from a fixed Session

Status: implementation in progress

[ADR 0023](0023-authorized-session-candidate-discovery.md) extends this contract
with discovery and selected retrieval within an explicit bounded Session set.
The fixed-source route described here retains its version-1 shape.

Date: 2026-09-21

Amends [ADR 0018](0018-static-credential-authorities.md) and extends
[ADR 0021](0021-bounded-evidence-access.md).
Implementation tracker: [Issue #69](https://github.com/sediment-ai/sediment/issues/69).
The [implementation specification](../superpowers/specs/2026-09-21-session-context-retrieval-design.md)
defines the version-1 selection, transport, and evaluation contracts.

## Context

A fresh coding agent can request evidence without loading an earlier Session's
complete conversation. The operator credential grants deployment-wide access,
which exceeds the authority needed to retrieve context from one Session.
The evidence store already preserves exact occurrence references, Quarantine,
strict representation, and bounded read execution.

## Decision

Decision: add one fixed retrieval authority and a read-only keyword selector.
A separate broker would duplicate lifecycle and request controls. A learned
selector would require a separate evaluation before replacing the baseline.

### Authority

Deployment configuration binds a retrieval token to one source Session.
`SEDIMENT_RETRIEVAL_TOKEN` and `SEDIMENT_RETRIEVAL_SESSION_ID` must appear together.
Both absent disables retrieval. The token meets the production secret rules
and differs from every other configured secret, including in development mode.
The deployment supplies the organization; requests cannot choose a Session or
organization. The reserved client identifier is `retrieval`.

The retrieval credential authorizes only `POST /query/context` and `/v1/me`.
The identity response includes its configured `source_session_id`.
Operator credentials can use the same query over that fixed Session.
Ingest credentials cannot use it. Existing ingest routes explicitly accept only
operator or ingest authority, and existing reads remain operator-only.
Unknown credentials return 401; a recognized credential without authority
returns 403. An operator receives 404 when context retrieval is disabled.
Capture enrollment and operator login reject retrieval credentials.

Configuration changes take effect on restart. Removing both settings revokes
retrieval access. Changing the source Session requires a different token so a
previous consumer doesn't inherit access to another Session. The service cannot
detect reuse across restarts. Operators must rename an ingest client named
`retrieval` before upgrading. No token database, grant service, or dynamic
policy engine is introduced.

### Selection and execution

The existing disposable evidence worker reads one complete bounded visible
Session in one read-only repeatable-read snapshot. The source excludes provider
raw payloads and the Fact's user identifier. Its independent limits are 1,000
visible calls, 8 MiB of selected stored columns, and 2,048 canonical parts.
Overflow refuses the operation; no successful partial scan is returned.

A pure versioned selector ranks distinct query-token overlap, then applies
explicit occurrence ordering, response-local repeated-content suppression,
and whole-part byte packing. It excludes reasoning and non-finite tool values
with closed counts. Scores aren't probabilities, Rewards, or training labels.
The response carries at most eight exact parts, source references, the
Quarantine revision, coverage, and unknown capture completeness. A shared bounded
strict encoder preserves canonical scalars and the existing evidence bytes.

The query shares the existing one-evidence-worker admission limit within two
query/report slots and the 30-second deadline. Every request rechecks Quarantine.
`Cache-Control: no-store` prohibits response caching. Retrieval writes no Fact,
summary, index, checkpoint, or cache. Training and Attribution paths retain their
existing interpretation.

### Agent integration and validation

The pi extension exposes one opt-in native tool using an independent retrieval
endpoint and token. It performs one bounded request without redirects or retries,
combines cancellation with a 35-second deadline, and forwards the original
validated JSON text so JavaScript doesn't round canonical integers. Historical
roles and tool calls remain evidence; the extension doesn't replay them.

An isolated agent environment receives no operator credentials, database access,
server configuration, or host mounts exposing them. A wholly self-hosted
continuation also uses an internal model and gateway endpoint.

A nine-run controlled comparison measures no history, full captured history,
and agent-requested evidence. Automated contract checks don't establish that
retrieval helps a live agent. Benefit remains unverified until the recorded
comparison satisfies the specification; lower cost isn't an acceptance gate.

## Consequences

One deployment can authorize one previous Session for agent-requested retrieval.
The keyword baseline runs without a retrieval model and supplies a comparison
for a later local selector. It doesn't establish Jev or CUA-S1 compatibility,
semantic relevance, cost savings, or general task improvement.

Concurrent grants, Session discovery, workspace restoration, checkpoint capture,
and automatic restart remain separate decisions. Repeated input histories count
toward source capacity. Responses can omit useful evidence because of keyword
mismatch or output limits. Retrieved content cannot be recalled after delivery.

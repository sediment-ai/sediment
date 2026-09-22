# ADR 0018 — Separate capture and operator credentials

Status: accepted; [ADR 0022](0022-agent-requested-session-context.md) adds fixed Session retrieval authority; [ADR 0025](0025-authorized-session-candidate-discovery.md) extends it to an explicit Session set.

## Context

One shared bearer token gives every capture client access to captured evidence
and operational reports. Rotating that token interrupts every client. A single
team needs independent capture revocation and a separate read boundary to protect
its evidence. These controls fall within [ADR 0006's open-core guarantee](0006-open-core-boundary.md#open-core-guarantees-never-gated).

This decision limits authentication changes to deployment controls that
preserve the existing self-hosted operation.

## Decision

Decision: deployment configuration defines two fixed authorities. No account
service, token database, dynamic policy engine, or tenant selection is added.

`SEDIMENT_OPERATOR_TOKEN` authorizes query, report, and Fact-inspection routes.
It also authorizes ingest for operator demonstrations.
`SEDIMENT_INGEST_TOKENS` maps client identifiers to independent ingest secrets.
`SEDIMENT_API_BEARER_TOKEN` remains an optional ingest-only compatibility secret.
Production rejects missing, placeholder, duplicate, or overlapping credentials.
It also rejects an operator, ingest, or webhook secret shorter than 24
characters.
Bearer secrets contain printable ASCII without whitespace so HTTP clients can
transmit them consistently.
Configuration validation and public diagnostics omit secret values.

Explicit route dependencies enforce authority. `/v1/me` accepts either authority
and returns its authority and configured client identifier with deployment and
version metadata. The fixed identifiers `operator` and `legacy` are reserved.
Unknown credentials return 401; known credentials without required authority
return 403. GitHub signatures and unauthenticated health checks retain their
existing boundaries.

Credential identity never selects the organization or a Fact's developer
identity. The deployment still binds tenancy, and capture evidence still supplies
developer identity under [ADR 0002](0002-session-aggregate-root.md).

The CLI stores operator login separately from verified ingest enrollment.
Capture installers and generated harness configuration consume only the latter.
An old profile containing one unclassified token cannot supply capture credentials
through an operator fallback. Explicit capture enrollment verifies ingest-only
authority before writing private configuration.

## Consequences

Removing a client from the configured map and restarting the API revokes that
client while other entries remain valid. Configuration reload is explicit; the
API does not promise hot reload or issue tokens. Operators must not reuse a
retired secret under another client identifier or the legacy setting.

Existing users of the legacy bearer token must obtain operator credentials for
read operations. Capture clients can retain an ingest-only credential. Operator
login and capture enrollment are separate steps for a remote deployment; the
local evaluation server can generate and enroll both deliberately.

Role-based access control, single sign-on, per-user authorization policies, and
centralized governance remain outside this fixed single-team boundary. No remote
quarantine, export, or mirror-deletion endpoint is introduced by this decision.

The 24-character floor blocks a hand-edited `.env` from booting with a
guessable secret. It doesn't add authentication throttling: no route in this
boundary applies rate limiting, backoff, or lockout. Guessing an
already-length-valid secret is still bounded only by request throughput.
Operators front the API with a reverse proxy or tunnel that rate-limits
authentication attempts.

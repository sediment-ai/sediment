# ADR 0006 — Open-core boundary: a complete, auditable single-team pipeline

Status: accepted

## Context

Sediment's core uses AGPL-3.0-or-later; harness shims use MIT.
The feature boundary follows the needs of a single team rather than
implementation difficulty. Capture and signal quality belong in the open core;
operating shared infrastructure across teams is a separate concern.

An RLVR training artifact includes an environment and verifiable reward. A
team needs to inspect how Sediment constructs that artifact. Operating
verification and measurement infrastructure across teams is a separate need.

## Decision

**The test:** if removing a feature makes a *single team's* pipeline
incomplete, untrustworthy, or unauditable, it is open source. If the feature
only makes sense because many teams, many models, or a compliance office
exist, it falls outside the open-core scope.

### Open-core guarantees (never gated)

- **All capture**, including every forge parser (GitLab/Bitbucket/ADO).
  A single team needs capture for its chosen forge.
- **The entire derivation layer** — mirror, notes/jaccard correlation, triple
  and rollout assembly. A team must be able to audit the reward path,
  including the correlator.
- **All export formats**, including the RLVR artifacts (`tasks.jsonl`,
  `rollouts.jsonl`) and local JSONL destinations. A team must be able to
  verify the construction of Verifiers-compatible environments in source.
- **Signal quality, always** — graded survival, transcript-derived
  signal, reward-policy nuances. The open pipeline produces the
  best labels Sediment can make; there is no degraded free tier of truth.
- **Data integrity and trust mechanisms** — fact quarantine, the eval
  holdout, Basic redaction (a fixed, deterministic credential-pattern set at
  the storage seam), and the append-only fact/quarantine logs.
- **The one-shot per-model outcome report (CLI)** — initial measurement
  and deployment measurement (ADR 0005).
- **The attribution stamper**, including the `--fleet` install script.
- **The PostgreSQL fact store and baseline deployment** — the physical Fact
  contract is defined in ADR 0012.

Forge parsers, graded survival, and transcript parsing belong in the open
core under the single-team test.

### Scope of proposals

The `enterprise-tier` label marks proposals outside the open-core scope.
Review proposed capabilities against the single-team test before implementation.
The label doesn't establish a product roadmap or availability commitment.

## Consequences

- Contributors and agents can check any feature against the test before
  choosing where it lives; PRs adding org-tier capability to the open core
  (or gating single-team capability) should be challenged in review.
- The open core remains a complete, auditable product for one team.
- PostgreSQL is open-source infrastructure and remains part of the open core.

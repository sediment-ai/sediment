# ADR 0025 — Discover candidates within an authorized Session set

Status: implemented; maintainer review pending

Date: 2026-09-22

Amends [ADR 0022](0022-agent-requested-session-context.md) and preserves the
fixed single-team authority boundary in
[ADR 0018](0018-static-credential-authorities.md).
Implementation tracker: [Issue #82](https://github.com/sediment-ai/sediment/issues/82).
The [implementation specification](../superpowers/specs/2026-09-22-session-candidate-discovery-design.md)
defines the version-1 wire shapes, limits, and acceptance criteria.

## Context

A fresh agent can know a task or commit without knowing which prior Session
contains its requirements or failed attempts. The fixed-Session tool requires
the operator to choose that source before the agent can search it.

Repository membership cannot supply a general read grant. Inference calls and
Session rows have no repository identity. An observed Session-to-commit edge
does not prove that every message in the Session belongs to that repository.
Uncommitted work may have no such edge. A relevance judgment cannot establish
access authority or missing captured identity.

## Decision

Decision: let deployment configuration authorize one bounded set of Sessions.
Add keyword candidate discovery and selected-Session retrieval within that set.
The existing singleton is the one-member case. A learned selector, repository
grant service, and stored index would add contracts before the source and
authority path is established.

### Authority

`SEDIMENT_RETRIEVAL_SESSION_IDS` accepts 1–32 unique validated IDs. It is
mutually exclusive with `SEDIMENT_RETRIEVAL_SESSION_ID`. Exactly one source
setting accompanies the independent retrieval token. Configuration caps the
plural JSON setting at 16 KiB. A grant authorizes the selected Sessions in the
deployment organization, including their future captured Facts; it is not a
snapshot, per-message permission, or inferred ownership claim.

Changing the authorized set requires token rotation and restart. As with the
singleton contract, the process cannot detect reuse across restarts. No account
service, token database, dynamic policy engine, or concurrent grant map is added.
This fixed single-team control stays open source under
[ADR 0006](0006-open-core-boundary.md).

The added discovery and selected routes accept retrieval or operator authority,
but both remain restricted to the configured set. The request cannot widen it.
Selected reads check membership before storage lookup and again in the worker.
Out-of-grant identifiers share one content-free refusal. Existing operator reads
remain operator-only, and capture credentials cannot use retrieval.

The singleton route and default pi tool retain their existing version-1 shapes.
Plural configuration requires an explicit selection rather than a first-member
default. Pi discovery is opt-in and uses the independent retrieval endpoint and
token. Historical evidence remains data, not executable instructions.

### Discovery and source identity

One read-only repeatable-read snapshot applies the organization and grant in
SQL before reading content. The complete set shares aggregate ceilings of
1,000 visible Inference calls, 8 MiB of selected stored variable-width columns,
and 2,048 canonical parts. Overflow refuses the whole operation. The existing
shared evidence-worker admission, deadlines, and process cleanup apply.

A pure versioned keyword selector produces at most eight Session candidates.
Each candidate has an exact whole-part preview or an observed commit witness.
The strict response fits a requested 4–64 KiB budget. Closed counts distinguish
ineligible parts, unmatched Sessions, and output omissions. No-match and
capture-completeness semantics remain explicit. Scores aren't probabilities,
Rewards, or training labels.

An optional commit anchor requires complete provider/host/repository-ID/SHA
identity. It prioritizes only direct identity-bearing Session-to-commit
observations in the grant whose exact source Push remains visible and agrees
on organization and identity. The join uses the source Push ID, not its final
commit. It performs no name resolution, legacy qualification, Attribution, or
mirror traversal. Text matches still discover uncommitted Session evidence.

Discovery writes no Fact, index, summary, checkpoint, or cache. The selected
retrieval reuses the existing exact source and keyword selector and rechecks
Quarantine in its own snapshot. A returned candidate neither grants additional
authority nor guarantees continued visibility. Attribution and training paths
retain their existing interpretation.

## Consequences

An agent can choose among several authorized histories without receiving the
operator credential or loading all histories into its model context. The
operator still defines the permitted Session set. An oversized set of histories
needs a narrower grant or a later explicitly designed indexed retrieval path.

The database and selection remain self-hosted and call no decision model. A
later model can rank authorized candidates against this deterministic baseline.
This decision establishes neither semantic recall, autonomous task resumption,
nor lower inference cost. Public-path acceptance proves discovery, exact reads,
and permission enforcement; it doesn't prove model judgment.

Automatic repository grants, organization-wide search, Git diff retrieval,
several simultaneous grant identities, and learned ranking remain separate
decisions. Commit witnesses expose recorded relationships while Git remains
the source of code evolution.

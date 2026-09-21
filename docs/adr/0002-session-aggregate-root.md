# ADR 0002 — Sessions are the aggregate root

Status: accepted

## Context

Retries, regenerations, and error-fix sequences relate several model calls
within one developer Session. Treating each call as an isolated event loses
those relationships. Session structure preserves them without requiring
capture to own the workstation.

## Decision

A `sessions` table is the aggregate root for developer-side facts. Every
inference call and developer-side fact carries a real `session_id`. The storage
seam merges session metadata without depending on fact arrival order.
`first_observed_at` and `last_observed_at` use fact observation time, never
source event time. A known `user_id` fills an absent value. Conflicting known
identities clear `user_id` and set a sticky `user_id_conflict` marker. The
session stores no scalar producer because one session can contain facts from
several agent harnesses. Repo-side facts (pushes, CI outcomes) are not
session-scoped; they join sessions through derivations (git-notes attribution
stamps session ids onto commits).

## Consequences

- Retry/regenerate mining, session-level acceptance, and transcript-derived
  training data become queries over one entity instead of heroic joins.
- Placeholder session ids are banned; a fact without a real session id is a
  capture bug to fix at the translator, not paper over.
- The session row stays thin (identity + bounds). Full transcript capture is
  a capture-phase decision, not blocked by this schema.
- Shuffled fact arrival produces the same session metadata. Operators can
  distinguish an unknown user from conflicting known identities.

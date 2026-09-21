# ADR 0013 — Git-note observation time is a Fact

Status: accepted

[ADR 0019](0019-repository-identity-and-renames.md) amends repository qualification
and deduplication for observations with captured provider repository identity.

## Context

A Git note records that a Session contributed to a commit. The repository
mirror exposes the latest value of that note, but it doesn't preserve when
Sediment first observed the relationship. A historical Derivation could
therefore read evidence fetched after its `as_of` boundary and change an
earlier result.

The Push timestamp can't supply the missing time. It records when Sediment
received a forge delivery, not when a successful mirror refresh exposed the
note.

## Decision

Sediment stores a `SessionCommitObservation` Fact after a successful mirror
refresh exposes a valid `refs/notes/sediment` Session-to-commit relationship.
The Fact records the repository, commit, Session, triggering Push, and capture
time. PostgreSQL preserves the first row for each repository-qualified
Session-to-commit relationship.

Capture examines only the commits bounded by the triggering Push and the
canonical Attribution commit cap. A failed refresh records no observations
from potentially stale mirror state. Missing or malformed notes record no
observation. Capture logs failures and preserves the already stored Push.

A repository-scoped observation lock serializes each Push from refresh through
persistence. The mirror lock covers only refresh and in-memory collection.
Capture releases the mirror lock before writing PostgreSQL so a Derivation that
holds a database snapshot can still acquire the mirror lock.

Historical reads select observations with `captured_at <= as_of`. Sediment
doesn't backfill existing notes because no Fact proves when Sediment first
observed them.

This decision doesn't change canonical Attribution or its Jaccard fallback in
ADR 0009.

## Consequences

- Historical Derivations can distinguish evidence available at a boundary
  from evidence fetched later.
- The Fact stores identifiers instead of note bodies or repository content.
- Redelivery and later Pushes don't change the first observation time.
- Deployments have no historical observation evidence for notes that predate
  this capture path.
- If a note reaches the remote only after the Push that carried its commit,
  bounded capture doesn't revisit that commit. A later Push can't backfill the
  missing observation without fabricating when Sediment first saw the note.
- Removing Jaccard remains a separate Attribution contract change.

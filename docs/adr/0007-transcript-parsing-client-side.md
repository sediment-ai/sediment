# ADR 0007 — Transcript parsing runs client-side; the observation is the fact, the score is a derivation

Status: accepted

## Context

Claude Code gives no graded post-acceptance edit-retention signal: its hooks fire
at apply time, so "accepted, then rewritten before commit" is invisible.
The session transcript (a local JSONL the hook payload points at via
`transcript_path`) contains what's needed: the tool's
originally-written text, and enough to observe the file's session-end state.
The open question was where parsing runs.

Shipping raw transcripts server-side would keep everything re-derivable,
but transcripts embed far more than the AI's edits — file reads of the whole
repo, shell output,
environment — a categorically larger consent and redaction surface than any
fact Sediment captures today, plus multi-MB-per-session volume.

Pure client-side *scoring* (ship one number per edit) has the opposite flaw:
it freezes the metric into immutable facts computed by whatever client
version happened to run. A later metric change cannot re-score that history.

## Decision

Parsing runs **client-side**, in a SessionEnd hook
(`sediment transcript`, installed opt-in via
`sediment install --transcripts`). The source-checkout
`scripts/sediment_transcript.py` path is a compatibility shim. What ships is the
**`(applied_text, observed_file_text)` text pair per applied edit** — the
`EditObservation` fact, joined to its `DeveloperDecision` by `call_id` —
through the existing OTLP `/v1/logs` door. The edit retention score is a pure
server-side derivation over the pair
(`sediment_derive.survival.attach_edit_retention`), with the metric supplied as
a scorer function. The wire event is
`sediment.edit_observation`. It carries `applied_text`, `observed_file_text`,
and a mandatory `agent` attribute.

The consent posture is parity, not expansion: the original text is already
captured as the completion at the gateway, and the session-end file state is
the near-commit state the mirror captures. Prompts, conversation, reads, and
tool results never leave the machine. Because the delta is not zero (an
uncommitted final state can ship), the hook is opt-in at install and
inert without an ingest endpoint configured.

The client validates that explicit endpoint before it reads or sends a bearer
token. Remote endpoints require HTTPS. Plain HTTP is limited to literal
`localhost`, IPv4 loopback addresses, and `[::1]`. The client rejects redirects
so a server can't forward an authenticated capture request to another origin.
Basic redaction remains a PostgreSQL storage-seam operation; the client doesn't
apply or redefine it.

## Consequences

- The metric stays a re-derivable policy choice: when the metric changes,
  all history re-scores — same posture as
  correlation's `policy_version`. Copilot's vendor-computed
  `survival_rate_four_gram` maps to captured `edit_retention_score`;
  `attach_edit_retention` never overwrites it.
- Known ceilings, accepted: sessions that die without a SessionEnd fire are
  missed (the notes/jaccard correlation backstop still covers them);
  Edit/Write only (no NotebookEdit/MultiEdit pairing); a SessionEnd refire
  collapses first-write-wins on
  `(org, agent_harness, session, call_id)`.
- Retry linkage extraction extends the client extractor rather than sending
  raw transcripts to the server. A `RetryLinkage` requires a
  human-explicit rejected Edit or Write, an intervening developer text entry,
  and a later successful call for the same file and session. It stores only
  identifiers and event metadata. It doesn't store the developer correction,
  the transcript, or duplicate attempt text, and it doesn't define a training
  evidence recipe.
- Opt-in tiering: richer capture
  arrives as a growing **menu of narrow, named fact types** — including
  `EditObservation`, `RejectedEdit`, and `RetryLinkage` — each tier a specific,
  purpose-built, auditable extraction with its own opt-in. A raw-transcript
  upload tier is explicitly rejected while no consumer exists that a narrow
  extraction can't serve more cheaply. It would require a separate fact type,
  and "ship the whole session, mine it later"
  is the consent posture this ADR exists to avoid. If a need only raw can
  serve ever materializes, it gets its own ADR, not a flag on this one.

# Attribution

An Attribution is an inferred join between an Inference call and a commit file.
An Attributed completion carries this estimate beside its joined Developer
decisions and CI outcomes. A Rollout retains the selected source when it binds
to commits. Factual Session relationships require separate captured
Session-to-commit observations.

The Attribution source affects Confidence. Sediment never discounts a
git-notes Attribution. It multiplies a jaccard Attribution's Confidence by the
similarity score ([Attribution
source](../../CONTEXT.md#attribution-source-attribution_source)).

This page explains the mechanism.

The [Derivation policy
procedure](../operate/run-derivations.md#set-derivation-policy) shows how an
operator controls both sources.

`derive_attributions` in
`packages/derive/sediment_derive/attribution.py` is a pure function of Facts,
the mirror, and policy. It returns `Attribution` dataclasses and stores
nothing, as [ADR 0001](../adr/0001-facts-not-derived-state.md) requires. The
same inputs can recompute the complete history.

## Attribution sources

Sediment reads each changed code file in the selected Push commits. The
`max_commits_per_push` policy caps the commits and keeps the newest. For each
file, Sediment tries two sources in order. Attribution implementation version 3
uses the shared Git section parser to decode filenames and extract hunk additions.
Quoted names, spaces, and authored lines beginning with `++` retain their meaning.
Binary, combined, unsupported, and malformed sections are counted and omitted;
valid neighboring sections remain eligible. Empty additions don't supply a
similarity target.

1. **Git notes (`attribution_source="git_notes"`), deterministic.** A client
   git hook writes the commit's `refs/notes/sediment` stamp. The stamp records
   which agent Sessions contributed. Sediment uses similarity only to rank
   inference calls inside those Sessions, under `git_notes.min_similarity`
   and `git_notes.lookback_window_minutes`. The note records a Session edge;
   call and file selection remain inferred. If a note is malformed, oversized,
   privacy-violating, or
   uses an unknown version, Sediment skips it, logs the reason, and tries
   jaccard.
2. **Jaccard (`attribution_source="jaccard"`), the fallback.** Sediment
   selects the best token-overlap match per file from the organization's
   inference calls inside `jaccard.lookback_window_minutes`. The match must
   meet `jaccard.min_similarity`.

An Attribution's identity is `(qualified repository, commit_sha, file_path)`.
The repository key uses organization, provider, host, and immutable provider ID.
A rename changes its captured labels without changing the key. Legacy labels
remain separate unless an exact source Push supplies identity evidence. Sediment keeps at
most one Attribution per changed file. Each identified Attribution retains its
source Push ID. Captured Session observations preserve their original Fact IDs;
a copied Git note or shared commit cannot supply another repository's identity. For the same key, a git-notes
Attribution always supersedes a jaccard Attribution, even when the jaccard
similarity score is higher.

Sediment treats the sources differently because a stamp records that a Session
contributed to a commit. Token overlap only estimates that relationship.
Without the Confidence discount, a dataset built mostly from jaccard
Attributions would appear to contain deterministic relationships.

## Concurrent marker capture

Each active `(tool, session_id)` marker has a local generation identified by a
universally unique identifier (UUID). Each edit refreshes that generation and
preserves the active marker's first timestamp.
Generations never enter Git notes or Facts. A short lock in the worktree's Git
directory protects complete marker-file replacements. Its acquisition deadline
is one second. Git commands and hook installation run outside this lock.

The stamper pins the commit SHA, snapshots marker generations, and records a
`stamp-started` diagnostic before reading or writing the note. It merges the
snapshot with that commit's existing valid note. After Git confirms the write,
cleanup removes only those generations. A concurrent edit survives for a later stamp,
even when its Session and timestamp match the snapshot. Squash union reloads
live markers under the same lock and preserves their generations and timestamps.
Legacy markers gain generations under the lock before stamping.
A malformed marker row, such as a legacy append that a crash cut short, is
skipped and counted as `marker_rows_skipped`; the next complete replacement
heals the file. Each row must contain valid UTF-8. Invalid bytes never become
replacement characters in tool or Session identities; healthy rows keep their
original Unicode values. Only an unreadable file refuses a write.

Linked worktrees share the notes ref. A separate nonblocking lock in their
common Git directory serializes Sediment stamps and notes reconciliation.
Contention reports `stamp_busy` or `notes_reconcile_busy` and consumes nothing.
A busy hook needs commit-specific inspection and retry. The lock prevents lost
updates; it doesn't guarantee that every concurrent hook completes a note.
The qualified scope permits concurrent edits and one commit operation per
worktree. External note editors fall outside that scope.

Before confirmed note success, markers remain available. Interruption after
success can retain generations already recorded in the note. A complete marker
replacement can survive a later directory-sync failure; diagnostics report
unconfirmed durability. This protocol retains evidence without promising
exactly-once stamping or automatic recovery after HEAD moves. Doctor reports
pending generations without treating old timestamps as proof of failed
consumption. Historical failure logs remain informational after a successful
retry. [Capture recovery](../capture/local-capture.md#recover-a-pending-stamp)
describes commit inspection and coordinated client upgrades.

## Gaps in git-notes Attribution

Sediment tries jaccard for each gap that follows. A qualifying match produces
a jaccard Attribution instead of a missing row. The Attribution source keeps
that fallback visible in Confidence and row metadata. The
`sediment report attribution-share --org <org>` command reports the git-notes
share and alerts on a decline.

- **A commit with no agent activity since the last stamped one** gets no
  note. The commit isn't attributed to an agent Session.
- **A repository without the per-repository installer has no git hooks.**
  User-level agent hooks still write markers, but no repository hook consumes
  them. The commits look normal and carry no note. A repository that an agent
  clones can enter this state. `mark` logs each marked-but-unhooked repository
  to `~/.sediment/attribution.log`. A fleet can use `init.templateDir` to add
  hooks to later clones.
- **`cherry-pick` and history rewrites other than `amend` and `rebase`** don't
  carry the note. `notes.rewriteRef` doesn't cover them.
- **A local `git merge --squash`** produces a correct union note, but
  only when the `prepare-commit-msg` hook is installed on the machine
  that runs the squash.
- **A forge-side squash merge**, such as GitHub's squash button, leaves
  the notes on the pull request's individual commits. The squash commit
  that the mirror fetches carries none. The mirror already fetches
  `refs/pull/*/head` for this case, but the Derivation doesn't read those
  refs for Attribution.

## The Push webhook as a trigger

`POST /ingest/github/push` refreshes the repository-qualified mirror, then runs the
Derivation scoped to that Push in a background task. Between refresh and
Derivation, the capture path stores immutable Session-to-commit observations
from the notes it read. One Push's commits bound this work. Sediment discards
the Attribution result apart from a log line; only captured Facts persist.
A policy change can score all captured commits again.

## Factual Session outcomes

Operational Session-to-commit outcomes require captured `SessionCommitObservation`
Facts through the report's `as_of` boundary. A live note or a similarity score,
including 1.0, cannot establish that historical relationship. Missing observations
count under `session_commit_unobserved` and leave the outcome unknown.

Canonical Attributed completions and Rollouts retain inferred Attribution and
carry matching source Facts in `session_commit_observations`. An observation
proves the Session edge only. Selecting an Inference call or file within that
Session remains inference. Version 1 Evidence recipes preserve their eligibility
and copy the selected sources into training metadata. See [ADR 0014](../adr/0014-factual-outcomes-and-training-evidence.md).

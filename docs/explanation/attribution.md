# Attribution

Attribution estimates which Inference call contributed to a commit file.
Sediment first looks for a Git note naming the contributing Sessions, then
uses text similarity to select a call. If that path produces no match, Sediment
tries similarity across the organization's candidate calls.

A Git note records a Session's relationship to a commit. It doesn't prove
which call authored a particular file. That distinction applies even when the
similarity score is 1.0.

[Run derivations](../operate/run-derivations.md) is the starting point for
operators. [How Derivation works](how-derivation-works.md) explains how
Attribution becomes part of a bundle. This page explains the matching process,
its gaps, and the evidence behind factual Session outcomes.

## The Push webhook as a trigger

After a Push webhook arrives, a background task refreshes the repository's
mirror. The capture path stores immutable Session-to-commit observations from
the notes that it reads. It then derives Attribution for that Push's commits.
Sediment logs counts and discards that Attribution result. Only captured Facts
persist.

A later Derivation can recompute Attribution from Facts, mirror evidence, and
policy. A webhook-triggered run doesn't create a saved bundle. The operator
builds one with the procedure in [Run derivations](../operate/run-derivations.md).

## Attribution sources

Sediment reads changed source-code files in the selected Push commits. The
`max_commits_per_push` policy limits that selection to the newest commits when
a Push exceeds the limit. Only nonblank added lines provide a similarity target.
For each eligible file, Sediment tries two sources in order:

1. **Git notes (`git_notes`).** The commit's `refs/notes/sediment` note names
   contributing Sessions. Sediment selects the best qualifying Inference call
   within those Sessions, using `git_notes.min_similarity` and
   `git_notes.lookback_window_minutes`.
2. **Jaccard (`jaccard`).** If no candidate from a Git note qualifies, Sediment selects
   the best token-overlap match among the organization's Inference calls within
   `jaccard.lookback_window_minutes`. The match must meet
   `jaccard.min_similarity`.

If a note is malformed, oversized, privacy-violating, or uses an unknown version,
Sediment skips it, logs the reason, and tries jaccard. If neither source supplies
a qualifying match, the file has no Attribution.

The lookback windows end at the Push's capture time plus
`post_push_grace_period_minutes`. They begin at the Push's capture time minus
the source's lookback duration. The grace period accommodates delayed capture.
[Set Derivation policy](../operate/run-derivations.md#set-derivation-policy)
shows the defaults and configuration.

For scoring, Sediment renders model-produced text and string values from
tool-call arguments. It excludes readable reasoning and tool responses.
The rendered text is temporary; it never becomes a Fact. The Git diff parser
preserves quoted filenames, spaces, and authored lines beginning with `++`.
It counts and omits binary, combined, unsupported, and malformed sections while
retaining eligible neighboring sections.

### One Attribution per commit file

An Attribution's identity is `(qualified repository, commit_sha, file_path)`.
The repository key uses organization, provider, host, and immutable provider
identifier. A rename changes the repository's labels without changing its key.
Legacy labels remain separate unless an exact source Push supplies identity
evidence.

Sediment retains at most one Attribution per key. A git-notes Attribution takes
precedence over a jaccard Attribution, even when the jaccard score is higher.
Each identified Attribution retains its source Push identifier. A copied note
or shared commit can't establish another repository's identity.

### Effect on Confidence

A git-notes Attribution receives no Attribution-source discount. A jaccard
Attribution multiplies Confidence by its similarity score. Other Confidence
factors still apply to both sources. A Git note narrows the candidate Sessions;
it doesn't turn inferred call-to-file authorship into a captured Fact.

## Gaps in git-notes Attribution

The following gaps can cause jaccard fallback. A qualifying fallback retains
`attribution_source="jaccard"`, so the uncertainty remains visible in metadata
and Confidence.

| Capture condition | Result |
| --- | --- |
| No pending agent activity marker | The commit receives no Session note from the stamper. Similarity may still produce Attribution. |
| Missing repository hooks | User-level hooks can write markers, but no repository hook consumes them into a note. This can affect repositories cloned after installation. |
| `cherry-pick` or rewrites other than `amend` and `rebase` | The configured note-rewrite rules don't carry the note. |
| Local `git merge --squash` without the `prepare-commit-msg` hook | The squash commit lacks the hook's union of Session notes. |
| Forge-side squash merge | The notes remain on the pull request's individual commits. The mirror fetches pull-request heads, but Attribution doesn't read those refs to recover the squash commit's note. |

[Configure local capture](../capture/local-capture.md) covers installation and
repair. [Measure agent work](../operate/measure-agent-work.md) covers
Attribution-share monitoring. A declining git-notes share can identify capture
gaps; it doesn't measure Attribution precision.

## Factual Session outcomes

A factual Session-to-commit outcome requires a captured
`SessionCommitObservation` through the report's `as_of` boundary. A live Git
note or similarity score can't establish that historical observation. Missing
observations count under `session_commit_unobserved` and leave the outcome
unknown.

Attributed completions and Rollouts retain inferred Attribution alongside
matching source Facts in `session_commit_observations`. Those Facts preserve
their original identifiers. An observation proves the Session-to-commit
relationship only; selecting a call or file within that Session remains
inference.

Version 1 Evidence recipes preserve their eligibility rules and copy the
selected sources into training metadata.
[Factual outcomes and training evidence](../adr/0014-factual-outcomes-and-training-evidence.md)
defines this distinction. Historical Attribution callers use captured
Session-to-commit observations through their observation boundary. An explicit
empty observation set doesn't authorize a read of live notes.

## Concurrent marker capture

This section describes the capture implementation for contributors. It explains
which concurrency guarantees the Session notes depend on.

Each active `(tool, session_id)` marker has a local generation identified by a
universally unique identifier (UUID). Each marker refresh assigns a generation and
preserves the active marker's first timestamp.
Generations never enter Git notes or Facts. A short lock in the worktree's Git
directory protects complete marker-file replacements. Its acquisition deadline
is one second. Git commands and hook installation run outside this lock.

The stamper pins the commit hash and snapshots marker generations. It records
a `stamp-started` diagnostic before reading or writing the note. It merges the
snapshot with that commit's existing valid note. After Git confirms the write,
cleanup removes only those generations. A concurrent edit survives for a later
stamp, even when its Session and timestamp match the snapshot. Squash union reloads
live markers under the same lock and preserves their generations and timestamps.
Legacy markers gain generations under the lock before stamping.
The stamper skips malformed marker rows, such as a legacy append interrupted
by a crash, and counts them under `marker_rows_skipped`. The next complete
replacement removes those rows. Each row must use valid UTF-8 encoding.
Invalid bytes never become replacement characters in tool or Session identities.
Healthy rows keep their original Unicode values. If the marker file is unreadable,
the stamper refuses to replace it.

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
retry. [Recover a pending stamp](../capture/local-capture.md#recover-a-pending-stamp)
describes commit inspection and coordinated client upgrades.

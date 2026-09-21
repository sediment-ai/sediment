# Review closeout — what blocks a merge

How to run and consume an adversarial review of a behavior-changing PR
before it merges (Claude Code: `/review-closeout`). [Scope governor](#scope-governor)
is the rule set that keeps review-triggered fixes from growing the PR past
its baseline. Merging always requires a maintainer's approval regardless of
any clean review.

## The contract

- **Blockers first.** Default review scope is findings that materially
  break the normal flow, outcome, or a safety boundary of the change.
  Widen to style/quality passes only when explicitly asked.
- **Advisory, never blindly applied.** Verify every finding by reading
  the real code path and adjacent files before fixing. Check dependency
  docs/source when a finding rests on external behavior.
- **Reject** unrealistic edge cases, speculative risks, broad rewrites,
  and fixes that over-complicate. Prefer the small fix at the right
  ownership boundary.
- **Sibling sweep.** When an accepted finding is a bug class, grep the
  PR's own scope for sibling instances and fix them together — stop at
  the PR's surfaces. A wider instance becomes an issue.
- **Consumer acceptance.** A data-integrity fix names its shared contract owner
  and affected consumers. Tests follow evidence through their public boundaries,
  including rejection or counted absence. A stage-local pass can't close a
  defect that breaks a downstream report or export.
- **Re-verify after fixing.** A review-triggered code change reruns
  focused tests and the review pass. Stop when a pass reports nothing
  actionable — never rerun a clean review for nicer wording.

## Scope governor

Before the first review pass, freeze a baseline: the issue, target
branch, intended behavior, changed files, and non-test LOC. Classify
every accepted finding before patching it:

- **In-scope blocker** — introduced by this diff, same ownership
  boundary, fixable without changing the task's contract. Fix it.
- **Follow-up** — real, but an adjacent bug class, cleanup, or wider
  hardening. File an issue rather than patching here.
- **Stop-and-escalate** — needs a new contract (schema, API, policy
  shape, process) or a design choice outside the original request.
  Comment on the issue and stop.

Hard stops — pause, reclassify everything, and report instead of
continuing to patch:

- the diff grows past ~2× the baseline's files or non-test LOC;
- two review-triggered fix cycles have not converged;
- the right fix is "define the canonical contract first";
- the PR no longer describes the same behavior or boundary it opened
  with.

Critical exceptions that justify breaking scope are only: active data
loss, crash, broken install/upgrade, release blocker, or concrete
security exposure.

## Cross-model second opinion

Every reviewer in one model family shares blind spots. For non-trivial
PRs, add one pass from a different vendor:

```bash
uv run python scripts/second_review.py --base origin/main
```

It freezes the branch diff into an empty temp workspace and runs the
codex CLI there against the bundle alone, with a scrubbed environment
allowlist. The banner identifies the requested or configured model. A zero
exit requires a valid final review object; missing or malformed results exit 2.
Zero means completion, including a review with findings, not merge approval.
Honest limits: codex's read-only sandbox still permits
filesystem-wide reads, so this is injection-resistant, not
injection-proof. Output is advisory under [the contract](#the-contract).
Skip it when codex isn't installed (the script says so and exits 2) —
and never feed it a diff that may contain secrets. CI scans every PR
diff for them (TruffleHog, `verified,unknown`, in
`.github/workflows/ci.yml`), but that gate is downstream of you: it
fires after the diff has already left the machine for codex.

## Reviewer isolation (staged rule)

A reviewer that loads the branch's own config/docs/hooks can be steered
by the diff it is judging. While every branch is maintainer-authored,
in-repo review subagents are acceptable. The moment the external-PR
triage flag in `docs/agents/issue-tracker.md` flips to yes: in-repo
subagent review of untrusted branches is off the table, and
`scripts/second_review.py` alone is **not sufficient** either — it
avoids ambient repo loading and scrubs the environment, but its
read-only sandbox cannot stop a steered reviewer from reading host
files. Untrusted-branch review requires a genuinely confined runner
(no host filesystem, no ambient credentials) — build or adopt one
then, not before.

## Final report

State: what was reviewed (command/diff base), tests run, each finding
accepted or rejected with a one-line why, and the clean result of the
final pass — or the consciously rejected remainder. A checked-off
review that skipped these steps is worse than no review.

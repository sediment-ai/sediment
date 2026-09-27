---
name: review-closeout
description: Run the closeout review procedure on a behavior-changing PR before merge — blockers-first adversarial review under the scope governor, optional codex cross-model pass, findings verified in source. Use when asked to review a branch/PR before merging, or after non-trivial fix rounds.
---

Read `docs/agents/review.md` and execute it: freeze the scope baseline,
review blockers-first, verify every finding in source before fixing,
classify each accepted finding (in-scope blocker / follow-up issue /
stop-and-escalate) and respect the hard stops.

For non-trivial PRs add the cross-model pass:
`uv run python scripts/second_review.py --base <PR base>` (advisory;
skip gracefully if codex is absent).

Close with the final-report shape the doc specifies. Merging still
requires a maintainer's approval.

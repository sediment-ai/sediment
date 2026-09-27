---
name: google-style
description: Google developer documentation style for prose in this repo — read docs/agents/writing-style.md and edit the draft against it. Use when writing or editing a page under docs/, when drafting a PR description or issue comment, or when asked to copy-edit, tighten, or style-check existing prose.
---

Read `docs/agents/writing-style.md`. It is the rule set — the Google
developer documentation style guide, the rules that drafts break by default,
the swap table, and this repo's overrides.

For a published page, settle its mode first (`docs/agents/doc-style.md`). A
rule that fits a how-to is wrong in explanation. Agent docs under
`docs/agents/` sit outside the four modes; skip the step for those.

Two passes over the draft, in this order:

1. **Cut.** Sweep once per swap-table row and once per banned word. A
   sentence that loses nothing when the word goes loses the word.
2. **Rebuild.** Passive to active with a named actor. Trailing conditions to
   leading conditions. A sentence carrying a second idea split in two. *We*
   to the named actor, Title Case headings to sentence case.

Done when every rule in `writing-style.md` has been applied to the whole
draft, not to its first paragraph. Close by naming the rules you applied,
one line each, and any you skipped and why — a silently rewritten draft
hides what changed.

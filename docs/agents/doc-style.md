# Documentation style — the four modes

Every page under `docs/` serves exactly one of four reader needs. Serving
two in one page is the defect this guide prevents: the reader who is
learning cannot skim, and the reader who is working cannot search.

This page is the whole rule set: which mode each page is, the rules that
hold here, and where a new page goes. Freshness and the same-PR rule are
a different job — [`docs/agents/doc-sync.md`](doc-sync.md).

Contributor doc. It is not published to the docs site.

## The four modes

| Mode | Informs | Reader is | Lives in |
|---|---|---|---|
| **Tutorial** | action | studying | `docs/quickstart.md` |
| **How-to** | action | working | `docs/operate/`, `docs/capture/`, `docs/exports/` |
| **Reference** | cognition | working | `docs/reference/` |
| **Explanation** | cognition | studying | `docs/explanation/`, `docs/adr/` |

Two questions place any page — or any single paragraph:

1. Does it inform **action** (do this) or **cognition** (know this)?
2. Does it serve **study** (acquiring a skill) or **work** (applying one)?

The most common defect here is a paragraph of *why* inside a how-to.
Cut it to one sentence and link to explanation.

## Tutorial

One page: [`docs/quickstart.md`](../quickstart.md). A lesson, not a task.
It exists so a stranger reaches a working capture and believes the
product works.

- Say the destination first: "Capture your first agent Session in about
  five minutes."
- Every step produces a visible result the reader can check. Ours is the
  house pattern: **command → verify command → the exact output it
  prints**. Keep it.
- Name what the reader should notice, and pre-empt the common miss: "No
  output after 30 seconds means the server did not start."
- **No alternatives.** No flags they might prefer, no other install
  tool, no "you could also". Every branch is a place to get lost. Send
  them to the how-to.
- **No explanation.** One clause is the budget ("Zeros are correct here
  — the note lives in git until a push webhook ingests it"). Anything
  longer is a link.
- It must work every time. A tutorial that fails once has failed.

## How-to

Most of `docs/`. The reader already knows what they want and needs to
get there.

- **Title the goal, not the topic.** "Comparing models with the outcome
  report" — not "Outcome report". A title that could label a chapter is
  the wrong title.
- Open with what the page achieves and who it is for.
- Order steps by dependency, so the reader never scrolls back.
- Conditional imperatives: "If you run your own gateway, do x."
- Omit completeness. Link `docs/reference/` for the full surface; a
  how-to shows the path that works, not every path.
- Explanation goes at the end or in a link — never between two steps.

## Reference

All three reference pages are generated — edit the source, never the page:
`docs/reference/cli.md` (the parsers), `docs/reference/api.md` (the routes),
`docs/reference/schema.md` (the Fact models and row dataclasses).

- Structure mirrors the code. If the reader can hold both in one head,
  it is right.
- Consistent shape per entry, so the eye lands in the same place twice.
- Austere. State facts. No instruction, no recommendation, no history.
- Examples are allowed and wanted. Recipes are not.
- Every user-facing surface earns a reference entry — commands, routes,
  and the row shapes readers consume (`docs/reference/schema.md`).

## Explanation

`docs/explanation/` for concepts, `docs/adr/` for decisions with a date
and consequences. Both are read away from the keyboard.

- Titles take an implicit "about": "About Attribution", "How survival is
  scored".
- Give the context a reader cannot infer: why the design is this way,
  what was rejected, what the constraint was.
- Admit alternatives and opinion. This is the only mode where "we chose
  X over Y, and here is the tradeoff" belongs.
- Keep it bounded — explanation has no natural end, so state the
  question the page answers and stop when it is answered.
- No instructions. A command in an explanation page is a smell.

## Where a new page goes

| The reader wants to… | Mode | Directory |
|---|---|---|
| learn Sediment by doing | tutorial | `docs/` (the quickstart is the only one) |
| accomplish a task they can name | how-to | `docs/operate/`, `docs/capture/`, `docs/exports/` |
| look something up mid-task | reference | `docs/reference/` |
| understand a concept | explanation | `docs/explanation/` |
| know why a decision was made | explanation | `docs/adr/` |

A dated record — a run, a measurement, a comparison — is not a doc mode.
Nobody updates its claims to stay current, so it does not belong in the
live tree: keeping records out is what lets the rest of `docs/` be true
today. Record such a result in the issue or the CHANGELOG entry that
prompted it.

You may still copy-edit one. Rewriting a sentence for clarity is not the
same as revising what it claims, so keep every number, every hedge, and
every caveat exactly as the record made them.

Add the page to the AGENTS.md router in the same PR, or
`scripts/check_docs.py` fails the build.

## Prose rules

[Writing style](writing-style.md) carries the whole rule set — Google
developer documentation style, the words to swap, and what changes for a
page under `docs/`. Read it before you draft. This page decides only which
mode you are writing in.

# Domain vocabulary and ADR rules for agents

Two rules for any skill exploring this codebase: use the glossary's vocabulary
exactly, and flag ADR conflicts instead of overriding them. This is a
**single-context** repo — one bounded context, one shared vocabulary, no
per-subdomain glossaries.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root — the ubiquitous language.
- **`docs/adr/`** — binding architecture (0001–0005 core, 0006 the open-core
  boundary, 0007 client-side transcript parsing, 0008 structured inference
  calls, 0009–0016 Attribution, evidence, storage, outcome and bundle contracts,
  0017 bounded sender transport storage, 0018 credential authorities, and 0019 repository identity).
  Read the ones touching your area.

If a glossary term or ADR you expect doesn't exist, **proceed silently** —
`/domain-modeling` creates them lazily when terms or decisions actually resolve.
Scope: lazily-created domain artifacts only. A routing row in AGENTS.md pointing
at a **missing file** is a bug to report, never something to skip silently.

## File structure

Source layout: [Where the code lives](../../CONTRIBUTING.md#where-the-code-lives). The `docs/`
subtree splits by reader need — `docs/adr/` (binding decisions 0001–0019),
`docs/explanation/` (concepts), `docs/operate/`, `docs/capture/`,
`docs/exports/` (how-tos), `docs/reference/` (generated), and
`docs/agents/` (these playbooks). Which mode a new page is: `docs/agents/doc-style.md`.

## Use the glossary's vocabulary

When your output names a domain concept (issue title, refactor proposal,
hypothesis, test name), use the `CONTEXT.md` term. Don't drift to synonyms the
glossary avoids — retired names (`GatewayEvent`, `DeveloperSignal`, `CICDEvent`)
are historical, not current.

A concept missing from the glossary is a signal: either you're inventing
language the project doesn't use (reconsider) or there's a real gap (note it for
`/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an ADR, surface it rather than silently overriding:

> _Contradicts ADR-0001 (Facts, not derived state) — but worth
> reopening because…_

The ADRs are load-bearing: contradicting one is a maintainer-level conversation,
not a PR-level judgment call.

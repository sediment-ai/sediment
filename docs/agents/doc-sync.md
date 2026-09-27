# Doc sync — keep docs true in the same PR

The repo's working agreement (docs/onboarding.md, authoritative) is that
docs update **in the same PR that changes behavior** — reviewed together,
merged together, never a follow-up. This checklist is how an implementing
agent (any harness) executes that before opening or updating a PR. CI
enforces the mechanical half via `scripts/check_docs.py`; this checklist
is the semantic half only an author with the diff in hand can do.

## When to run

Any PR that changes behavior, adds/renames/moves a module, tunes a knob or
default, adds/removes a capture source or skip reason, or adds a doc.
Doc-only and pure-test PRs still run step 5 (the checker). **Re-run steps
1–5 after any push that changes behavior, including review-feedback
pushes.** Step 1's base is the PR's base branch
(`gh pr view --json baseRefName`) — not necessarily `main`, and never only
your last commit.

## Procedure

1. **List the diff**: `git diff --name-only <base>...HEAD`.
2. **Map each changed path to its docs** via AGENTS.md: the package map's
   `Read first` column names the owning playbook and topical docs; the
   topical-docs table covers non-package surfaces.
3. **Grep beyond the mapping**: for every symbol you renamed, value you
   changed, and file you moved, run `git grep -n '<old name>' -- '*.md'`.
   The router mapping is a starting set, not a closed one — this step is
   what catches the doc the mapping missed.
4. **Update every hit in place**, guided by the [trigger →
   doc table](#trigger--doc-table) — same branch, same PR.
5. **Run the checker** and fix every failure:
   `uv run python scripts/check_docs.py`
6. **Tick the PR-template docs checkbox** if steps 1–5 ran (or mark it
   N/A per the template's own wording for doc-only/pure-test PRs).

## Trigger → doc table

| Your diff contains | Update |
|---|---|
| New module in a package | AGENTS.md package-map purpose cell + the playbook's module map + any topical doc describing that surface |
| Changed knob/default/threshold | The playbook **and any topical doc** stating the value (step 3 finds them), plus the coupled-knob partner the playbooks name; bump `policy_version` where one exists |
| New/changed capture source | `docs/agents/capture-translators.md` per-source section + **new** frozen fixtures (never regenerate existing ones) + fixture README row |
| New skip reason or changed vocabulary | The playbook's skip-vocabulary list |
| New/renamed/deleted `docs/*.md` | AGENTS.md router row (no-orphan rule — route the maintained page); for a page under `capture`, `explanation`, `exports`, or `operate`, update `docs/published-pages.json` |
| New `*Policy` or settings class | [Policy](../../CONTEXT.md#policy-policy-policy_version), the playbook, and the env-var surface list in `docs/agents/api-and-operations.md` |
| New CLI subcommand, flag, or script | `docs/agents/api-and-operations.md`, then regenerate the reference: `uv run python scripts/gen_cli_docs.py` (CI runs `--check`). Claude Code users: also the allowlist in `.claude/settings.json` if the command is safe to run unattended |
| New/changed HTTP route, auth, or response shape | [Service invariants](api-and-operations.md#service-invariants-appsapi), then regenerate spec and reference: `uv run python scripts/dump_openapi.py && uv run python scripts/gen_api_docs.py` (CI runs `--check` on both). A new route also needs an auth-map entry in `scripts/dump_openapi.py` and a contract entry in `scripts/gen_api_docs.py` — both fail the run until it has one |
| New/changed field on a Fact model, derived artifact, or training row | Regenerate the schemas, catalog, and schema reference: `uv run python scripts/gen_schema_docs.py` (CI runs `--check`). A new field needs a description in `scripts/gen_schema_docs.py`'s `COMMON` or `FIELDS` — the run fails until it has one |
| Renamed/moved symbol a doc cites | Step 3's repo-wide `git grep` over `*.md` |
| User-visible behavior change | A `CHANGELOG.md` entry |
| New glossary-worthy term | CONTEXT.md entry in the house format (definition + `_Avoid:_`) |

## What CI does not check

`check_docs.py` verifies structure (routes, caps, citations, paths,
links, each link's `#anchor`, and published-page manifest coverage).
HTTP(S) URLs stay outside local documentation-path checks. The checker also
requires every public package module to be named in a routed doc
(`# docs-exempt: <why>` opts out). In PR diff mode, it requires a CHANGELOG
touch when the diff adds a module or a script flag. Still on you at authoring time: concrete
values quoted in prose (the `0.7`s), glossary claims about bare class
names, and whether the CHANGELOG entry the gate demanded actually says
anything true.

Point at a section with a real link, never with a `§` in prose. CI
resolves an anchor and cannot resolve a `§`.

## Rules recap (from the playbooks)

Citations are `path` or `path::symbol`, never `path:line`. No code fences
in playbooks. Every `docs/agents/*.md` registers a line cap in
`scripts/check_docs.py` (playbook or adapter) in the same PR that adds it.
Caps are hard: cut lowest-value content to fit rather than raising a cap.
Delta-only: point at the authoritative doc, don't restate it.

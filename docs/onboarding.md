# Onboarding — humans and agents

This page tells you what to read in what order, how to run the checks
locally, and how to contribute your first pull request.

## Reading order

| Read | Why |
|---|---|
| 1. [`README.md`](../README.md) | The evidence store and its uses: evaluate agent work, reuse context, and build training datasets |
| 2. [`AGENTS.md`](../AGENTS.md) | The router: non-negotiable rules, package map, topical docs. Claude Code reaches it via [`CLAUDE.md`](../CLAUDE.md) and a `SessionStart` hook |
| 3. [`CONTEXT.md`](../CONTEXT.md) | The domain vocabulary: Facts, Derivations, Attributed completions, and Rollouts. Use its terms exactly. |
| 4. [`docs/adr/`](adr/) | The binding decisions: 0001–0005 core, 0006 open-core boundary, 0007 client-side transcript parsing, 0008 structured inference calls, 0009 canonical Attribution, [0010 canonical continuous integration outcomes](adr/0010-canonical-ci-outcome-facts.md), [0011 training-objective evidence](adr/0011-training-objectives-own-evidence-interpretation.md), [0012 PostgreSQL-only storage](adr/0012-postgresql-fact-store.md), [0013 Git-note observation Facts](adr/0013-git-note-observation-facts.md), [0014 factual outcomes and training evidence](adr/0014-factual-outcomes-and-training-evidence.md), [0015 lossless representation and bundle v2](adr/0015-lossless-values-and-bundle-v2.md), [0016 bundle derivation consistency](adr/0016-bundle-derivation-consistency.md), [0017 bounded sender transport storage](adr/0017-sender-transport-replay.md), [0018 credential authorities](adr/0018-static-credential-authorities.md), [0019 repository identity](adr/0019-repository-identity-and-renames.md), and [0023 indexed call identifiers](adr/0023-indexed-call-identifiers.md) |
| 5. The playbook for your area | AGENTS.md's package map (`Read first` column) routes you into [`docs/agents/`](agents/) |
| 6. [The issue tracker](agents/issue-tracker.md) | Labels, milestones, and how to pick up work |

Current status lives in `CHANGELOG.md` and the GitHub milestones. On a
deployment, the operator front door is the `sediment` CLI —
[`docs/agents/api-and-operations.md`](agents/api-and-operations.md).

## Local setup

Use the [contributor environment and checks](../CONTRIBUTING.md#contributor-environment-and-checks)
for Python 3.12, Node 24, and local validation. To install Sediment as an operator,
follow the [Quickstart](quickstart.md).

## Full pre-review check

Run the [contributor checks](../CONTRIBUTING.md#full-pre-review-check) before
opening a pull request. TruffleHog remains a CI-only check.

## Your first pull request

Automatic Python and security jobs skip draft pull requests. Marking a pull
request ready for review starts those checks. Manual workflow runs validate
drafts with full coverage. If GitHub-hosted jobs are unavailable, continue with
the local checks and retain their results; hosted validation remains pending.

CI runs lint, formatting, and documentation checks before database setup. An
added or modified Markdown file in `docs/explanation/`, `docs/agents/`, or
`docs/adr/`, or a change to `CONTEXT.md`, `CHANGELOG.md`, or the root `README.md`,
qualifies for the prose path only when every changed file qualifies. That path
keeps secret scans, generated references, and contributor and documentation
contract tests. It skips PostgreSQL, the full Python suite, release rehearsal,
native server checks, and artifact security scans.
Other paths, executable files, symlinks, deletions, renames, and unavailable Git
history select full validation. Manual and reusable release runs always select
full validation. `scripts/ci_preflight.py` owns the selection.

Dependabot checks each ecosystem daily and groups minor and patch updates.
Runtime-line migrations remain separate. Dependabot keeps Node typings on Node 24,
PostgreSQL on 17, and the API image on Python 3.12. The full Python run reports its 30 slowest tests
to identify further savings before changing test parallelism.

1. **Claim an issue** with a comment before starting. If you are an agent,
   filter `ready-for-agent`. If the specification lacks required information,
   comment with what is missing and stop. A maintainer changes its label.
2. **Small pull requests against `main`**, `Closes #<n>` in the description, CI
   green.
3. **Docs update in the same pull request** that changes behavior — follow
   [`docs/agents/doc-sync.md`](agents/doc-sync.md) (Claude Code:
   `/doc-sync`) before opening or updating the pull request.
4. **Fill the pull request template's architecture check honestly.**
5. Behavior-changing pull requests get an adversarial review before merge —
   [`docs/agents/review.md`](agents/review.md) (Claude Code:
   `/review-closeout`). Merging always requires a maintainer's approval.

## Working agreements

- Issues are the work surface. Milestones group them. Labels say who can
  act ([Issue tracker](agents/issue-tracker.md)).
- Decisions outlive threads. An architectural decision goes in an ADR.
  Vocabulary goes in `CONTEXT.md`. Semantics go in the relevant doc, in the
  same pull request.

# Onboarding — humans and agents

This page tells you what to read in what order, how to run the checks
locally, and how to contribute your first pull request.

## Reading order

| Read | Why |
|---|---|
| 1. [`README.md`](../README.md) | The evidence store and its uses: evaluate agent work, reuse context, and build training datasets |
| 2. [`AGENTS.md`](../AGENTS.md) | The router: non-negotiable rules, package map, topical docs. Claude Code reaches it via [`CLAUDE.md`](../CLAUDE.md) and a `SessionStart` hook |
| 3. [`CONTEXT.md`](../CONTEXT.md) | The domain vocabulary: Facts, Derivations, Attributed completions, and Rollouts. Use its terms exactly. |
| 4. [`docs/adr/`](adr/) | The binding decisions: 0001–0005 core, 0006 open-core boundary, 0007 client-side transcript parsing, 0008 structured inference calls, 0009 canonical Attribution, [0010 canonical continuous integration outcomes](adr/0010-canonical-ci-outcome-facts.md), [0011 training-objective evidence](adr/0011-training-objectives-own-evidence-interpretation.md), [0012 PostgreSQL-only storage](adr/0012-postgresql-fact-store.md), [0013 Git-note observation Facts](adr/0013-git-note-observation-facts.md), [0014 factual outcomes and training evidence](adr/0014-factual-outcomes-and-training-evidence.md), [0015 lossless representation and bundle v2](adr/0015-lossless-values-and-bundle-v2.md), [0016 bundle derivation consistency](adr/0016-bundle-derivation-consistency.md), [0017 bounded sender transport storage](adr/0017-sender-transport-replay.md), [0018 credential authorities](adr/0018-static-credential-authorities.md), and [0019 repository identity](adr/0019-repository-identity-and-renames.md) |
| 5. The playbook for your area | AGENTS.md's package map (`Read first` column) routes you into [`docs/agents/`](agents/) |
| 6. [The issue tracker](agents/issue-tracker.md) | Labels, milestones, and how to pick up work |

Current status lives in `CHANGELOG.md` and the GitHub milestones. On a
deployment, the operator front door is the `sediment` CLI —
[`docs/agents/api-and-operations.md`](agents/api-and-operations.md).

## Local setup

Use Python 3.12 and [uv](https://docs.astral.sh/uv/) for the Python workspace.
Use Node 24 for the pi shim. Never use `pip install` in this repository.

```bash
git clone https://github.com/sediment-ai/sediment.git && cd sediment
uv python pin 3.12
uv sync --locked
```

### Focused checks

PostgreSQL contract tests need a disposable PostgreSQL 17 instance and an
administrative role that can create and drop databases. Use its administrative
URL. The fixtures create isolated worker databases and remove them after the
suite. They retain a reusable migrated template database for later runs.

```bash
export SEDIMENT_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres
export SEDIMENT_DATABASE_URL="$SEDIMENT_TEST_DATABASE_URL"
```

With PostgreSQL available, run the smallest relevant package suite while you
work. For example:

```bash
uv run pytest packages/core
```

### Full pre-review check

With PostgreSQL available, run the Python and documentation checks that
continuous integration (CI) enforces.

The first command migrates the exact database in `SEDIMENT_DATABASE_URL`.
Use only the disposable instance from the setup procedure. The release
rehearsal creates, migrates, and removes its own scratch database.

```bash
uv run sediment db upgrade
uv run ruff check .
uv run ruff format --check .
uv run python scripts/add_spdx.py --check
uv run python scripts/check_docs.py --base origin/main
uv run python scripts/dump_openapi.py --check
uv run python scripts/gen_cli_docs.py --check
uv run python scripts/gen_api_docs.py --check
uv run python scripts/gen_schema_docs.py --check --compatibility-base origin/main
uv run pytest -q --durations=30
uv run python scripts/release_rehearsal.py --database-url "$SEDIMENT_DATABASE_URL"
```

The release rehearsal includes the first-party dependency metadata audit.
TruffleHog secret scanning is CI-only because this repository doesn't define a
supported local secret-audit command.

Run the TypeScript checks from `shims/pi/`:

```bash
npm ci --no-audit --no-fund
npm run typecheck
npm test
```

These commands run the local shim checks. The [shims workflow](../.github/workflows/shims.yml)
also builds and installs the Python wheels outside the checkout and sets
`SEDIMENT_PI_TEST_PYTHON` and `SEDIMENT_PI_TEST_INSTALLED_BIN` to exercise the
installed helper. Without the installed-bin setting, the local suite skips
that acceptance test.

Every ready pull request reports the stable `shims` check. The workflow runs
installed-helper checks when shim, delivery, dependency, or selector files change.
Other changes receive an explicit not-applicable result. Missing history or an
unreadable diff selects the checks; manual and selected push runs also execute
them. A failed selection or incomplete selected check fails the job.

After this workflow is present on `main`, require the GitHub Actions `shims`
check in the branch ruleset. Requiring it before the workflow reaches `main`
can block unrelated pull requests that still use the path-filtered workflow.

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

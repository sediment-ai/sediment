# Contributing

Thanks for looking at Sediment. This is a pre-alpha project with strong
architectural opinions; the fastest way to contribute well is to read two
documents first.

## Before you write code

1. **Read [AGENTS.md](AGENTS.md).** It is the router for this repo: the
   non-negotiable rules (Facts vs Derivations), the package map, and the
   topical docs. The rules there are enforced in review.
2. **Read the ADRs** in `docs/adr/` before changing anything structural.

## Support and project direction

For source-checkout setup and local capture verification, follow the
[Quickstart](docs/quickstart.md). For team enrollment, use
[Run a Cursor and Codex pilot](docs/operate/run-pilot.md). For contributor
setup and checks, use [Onboarding](docs/onboarding.md). The
[documentation site](https://docs.sediment.so) covers capture, deployment,
Derivations, and exports.

Search [existing issues](https://github.com/sediment-ai/sediment/issues) before
opening a [bug report or feature request](https://github.com/sediment-ai/sediment/issues/new/choose).
Use a minimal reproduction with sanitized commands and error text. Don't post
credentials, raw transcripts, prompts, or private repository content. Report
suspected vulnerabilities through the [Security policy](SECURITY.md).

The roadmap lives in [issues](https://github.com/sediment-ai/sediment/issues)
and [milestones](https://github.com/sediment-ai/sediment/milestones).
[CHANGELOG.md](CHANGELOG.md) records implemented changes. Before starting work,
claim the issue with a comment and read its acceptance criteria. Use a feature
request to propose work that the tracker doesn't cover.

## Where the code lives

```text
AGENTS.md         router: non-negotiable rules, package map, topical docs
CONTEXT.md        the ubiquitous language — use its terms exactly
packages/core     Fact models + active PostgreSQL Fact store
packages/capture  translators: gateway adapters, OTLP, forge webhooks
packages/derive   Attribution, Rollouts, Recovery pairs, the mirror
packages/export   Attributed completions, Reward, training rows, statistics
apps/api          FastAPI ingest service + the `sediment` operator CLI
cli/              the `sediment` CLI distribution installed on dev machines
scripts/          Attribution stamper, report wrappers, repair tools
shims/            harness-extension shims (the one TypeScript surface)
litellm/          gateway deployment glue (the logging callback)
sim/              synthetic scenario suite + live-agent driver
docs/             see docs/agents/doc-style.md before adding a page
```

Facts flow in through `packages/capture`, land in `packages/core`, and
everything downstream is a pure function over them —
[how Sediment works](docs/explanation/how-sediment-works.md) explains why
that boundary is the one to respect.

## Ground rules (the short version)

- **Facts are append-only.** Nothing mutates a Fact table.
- **Derivations are pure functions** of (Facts, policy) — recomputable
  over all history, independent of ingest order.
- Python 3.12. Node 24 and TypeScript only under `shims/`.
- Runtime and integration tests use real PostgreSQL and real git repos.
- PostgreSQL tests require a PostgreSQL 17 server. Set
  `SEDIMENT_TEST_DATABASE_URL` to an administrative database URL. The test
  suite creates and removes isolated databases under that server.
  Native backup and restore tests require compatible `pg_dump` and `pg_restore`
  clients on `PATH`. CI selects PostgreSQL 17 clients.

## Mechanics

- Setup: `uv sync --locked`. Run a focused package test while you work, such as
  `uv run pytest packages/core`.
- Before review, run the
  [full Python, documentation, SPDX, and shim checks](docs/onboarding.md#full-pre-review-check).
- For the complete suite, start PostgreSQL and set
  `SEDIMENT_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres`.
- For packaging changes, run
  `uv run pytest scripts/tests/test_release_rehearsal.py`, then follow the
  [no-publish release rehearsal](docs/operate/rehearse-release.md). The
  rehearsal validates all twelve distribution artifacts and exercises the six
  installed wheels against disposable PostgreSQL before a tag can publish them.
- Run the bounded-read memory contract with
  `uv run pytest packages/core/tests/test_postgres_projection_memory.py`.
  It creates one isolated migrated database through the same administrative
  URL and measures projections in spawned child processes. On Linux, it resets
  and reads the child's resident-memory peak through `/proc`; `ru_maxrss` can
  retain a parent's earlier peak across process startup. The control primes the
  parent peak and still requires full-row reads to exceed the unchanged limit.
- First-party Python files carry AGPL-3.0-or-later SPDX headers
  (`uv run python scripts/add_spdx.py --check`). Shim code carries MIT SPDX
  headers.
- Behavior changes update `CHANGELOG.md` and the affected doc under
  `docs/` in the same PR (CI enforces this via `scripts/check_docs.py`).
- `docs/reference/cli.md` is generated from the CLI's parsers; a new flag
  needs `uv run python scripts/gen_cli_docs.py` (CI fails on a stale
  page). Optional — have a commit that touches a parser do it for you:

  ```bash
  printf '#!/bin/sh\nh="$(git rev-parse --show-toplevel)/scripts/hooks/pre-commit"\n[ -x "$h" ] && exec "$h" "$@"\nexit 0\n' \
    > "$(git rev-parse --git-path hooks)/pre-commit"
  chmod +x "$(git rev-parse --git-path hooks)/pre-commit"
  ```

  A stub rather than a symlink to `scripts/hooks/pre-commit`, because
  `.git/hooks` is shared by every worktree: it resolves the script in the
  worktree you are committing from, and skips silently on a branch that
  does not have it.
- The PR template's checklist is real; fill it honestly.
- Use `Closes #<n>` only for an issue in the pull request's repository.

## Commits

Commit subjects follow [Conventional Commits
v1.0.0](https://www.conventionalcommits.org/en/v1.0.0/), with one house
rule: the scope vocabulary is closed.

```text
type(scope)!: description

optional body, after a blank line

Closes #123
```

The repository squash-merges, so **the pull request title is the subject
that lands on `main`** — write it in this form. CI checks the title;
intermediate commits on your branch are your own business, though the
opt-in hook below checks them too.

| Type | Use it for |
| --- | --- |
| `feat` | a capability that did not exist |
| `fix` | a defect corrected |
| `docs` | documentation only |
| `refactor` | behavior unchanged, structure changed |
| `perf` | a measured speed or memory improvement |
| `test` | tests only |
| `build` | packaging, distributions, the Dockerfile |
| `ci` | workflows and the checks they run |
| `chore` | anything left over — dependency bumps, config |
| `style` | formatting only, no code meaning changed |
| `revert` | reverting an earlier commit |

Scopes are the package map, plus the three surfaces that own commits
without being packages:

`core` · `capture` · `derive` · `export` · `api` · `cli` · `shims` ·
`sim` · `scripts` · `deps`

A scope is optional — omit it for a change that spans the tree. Inventing
one fails the check: widen `SCOPES` in `scripts/check_commit_msg.py` only
when a genuinely new surface lands.

- **Breaking changes** take `!` before the colon and a `BREAKING CHANGE:`
  footer explaining the migration. The six distributions release at one
  version, so a Fact-shape or CLI change that breaks a caller is a fact
  every reader needs at the top of the subject.
- **Keep the description under 72 characters**, lowercase unless it opens
  on an identifier (`README`, `PostgreSQL`), and with no trailing period. Over
  72 warns; it does not fail.
- **Footers** carry the references: `Closes #123` on the PR, and
  `Co-Authored-By:` where it applies.

```text
feat(core): add CHECK constraints mirroring model identity rules
fix(capture)!: session ids no longer fragment on padded input
docs: rewrite the client capture contract
chore(deps): bump actions/checkout from 4 to 7
```

Check a subject before you push it, or install the hook and have git do it:

```bash
uv run python scripts/check_commit_msg.py --title "feat(core): add a field"
```

```bash
printf '#!/bin/sh\nh="$(git rev-parse --show-toplevel)/scripts/hooks/commit-msg"\n[ -x "$h" ] && exec "$h" "$@"\nexit 0\n' \
  > "$(git rev-parse --git-path hooks)/commit-msg"
chmod +x "$(git rev-parse --git-path hooks)/commit-msg"
```

A stub rather than a symlink, for the same reason as the `pre-commit` hook
above: `.git/hooks` is shared by every worktree.

## Review and merging

Every PR gets a maintainer review; merging requires maintainer approval.
Maintainers manage labels, milestones, and issue closure under the
[issue tracker rules](docs/agents/issue-tracker.md). Architectural decisions
belong in [ADRs](docs/adr/), and domain vocabulary belongs in
[CONTEXT.md](CONTEXT.md). Behavior-changing pull requests follow the
[review contract](docs/agents/review.md).

## License

Contributions are accepted under the repository license
(AGPL-3.0-or-later), except contributions to `shims/`, which are accepted
under MIT (`shims/pi/LICENSE`). Submitting a PR certifies you have the
right to contribute the code under the license that covers the paths you
touched.


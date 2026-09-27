# AGENTS.md — Sediment

## What Sediment Is

A self-hosted pipeline that turns AI developer workflow traces into RL-ready
training data. It captures inference calls, developer accept/reject decisions,
Edit observations, Retry linkages, git pushes, and CI outcomes as **immutable
Facts**; derives Attributions, edit retention scores, and Reward linkage as
**pure, recomputable functions** over those Facts; and exports two canonical
artifacts — Attributed completions and Rollouts — projected into DPO/SFT and
RLVR training rows (plus the Fact-derived Recovery pair, ADR 0004's one
sanctioned exception). Full picture: `docs/explanation/how-sediment-works.md`.

The ADRs in `docs/adr/` are binding — read the ones touching your area before
changing anything structural. Current status is `CHANGELOG.md` plus the GitHub
milestones.

## The Non-Negotiable Rules

1. **Facts are the only persisted domain state.** A Fact is something that
   happened: an inference call, a decision, a push, a CI outcome. Facts are
   appended, never mutated. If you find yourself writing an UPDATE on a Fact
   table or storing the output of a matcher/policy, stop — that belongs in a
   Derivation. Two sanctioned exceptions: the opt-in sender transport buffer
   that Derivations never read (ADR 0017), and exact copies of captured call
   identifiers for indexed lookup, never attachment or policy output
   (ADR 0023).
2. **Derivations are pure functions** of (Facts, policy) and must be
   recomputable over all history. No Derivation may depend on wall-clock
   ingest order or webhook arrival order.
3. **Sessions are the aggregate root.** Developer-side Facts carry a
   `session_id`; the Session row is upserted at the storage seam. Never
   invent placeholder Session ids.
4. **Dedup lives in the database.** `UNIQUE` indexes enforce idempotency —
   do not re-implement dedup scans in application code.
5. **Schema is the source of truth.** All Fact shapes live in
   `packages/core/sediment_core/models.py`; never define them inline
   elsewhere. Derived shapes (`Attribution`, `Rollout`, `Turn`, `CommitRef`,
   `AttributedCompletion`, `RecoverySample`, `Provenance`, every `*Policy`)
   are frozen dataclasses — Pydantic is only for Facts, the notes wire
   contract, settings loaders, and HTTP request envelopes. A join key, a
   dedup-index component, or the tenancy key gets a validated type
   (`NonEmptyId`, `CommitSha`, `OrgId`, `RepoSlug`, `BranchName`,
   `AwareDatetime`), never a bare `str` or naive `datetime`.
6. **Training objectives own evidence interpretation.** Capture Facts once;
   each exporter interprets only the evidence its training objective needs.
   Every training row names one Evidence recipe and preserves the source of
   every label, eligibility decision, and Reward. Do not extend the legacy
   mixed DPO/SFT behavior; ADR 0011 defines its replacement.

## House Rules

- **Absent, never guessed at.** Anything unknowable is omitted visibly and
  logged — never fabricated. Derivations and projections skip ineligible
  inputs **and count them** under the module's closed skip-reason
  vocabulary; capture and Derivation fail-soft paths log and degrade rather
  than raise ([Fail-soft](CONTEXT.md#fail-soft)). Never silently dropped,
  never emitted half-formed.
- **Bump `policy_version`** when tuning any policy knob; the Provenance stamp
  keeps pre- and post-change datasets distinguishable. `DPOPolicy`,
  `SFTPolicy`, and `MirrorPolicy` carry no version field — call such tunings
  out in the PR description.
- **Every new Derivation ships determinism tests**: same Facts → identical
  output, and shuffled ingest order → identical output.
- **Climb before you build.** In order: does it need to exist, does this repo
  already have it, stdlib, a native platform or DB feature, an installed
  dependency, one line — then the minimum code that works. No interface with
  one implementation, no factory for one product, no config for a value that
  never changes. The ladder shortens the solution, never the reading: trace
  the real flow first.
- **Root cause, not symptom.** Grep every caller of the function you are
  about to touch. One guard in the shared function beats a guard in each
  caller, and patching only the path the issue names leaves every sibling
  broken.
- **`ponytail:` marks a deliberate simplification** so it reads as intent.
  Where the shortcut has a known ceiling, the comment names the ceiling and
  the upgrade path: `# ponytail: global lock, per-account locks if
  throughput matters`.
- **Non-trivial logic leaves one runnable check** — the smallest thing that
  fails if the logic breaks. It never relaxes the determinism rule above.
- **Never simplified away**: validation at trust boundaries, the fail-soft
  paths that keep Facts from being lost, SPDX headers, and anything the
  builder asked for by name. Shortest diff breaks ties between correct
  options; it is never a reason to ship the flimsier one.

## Communication

The [Google developer documentation style
guide](https://developers.google.com/style) governs every word an agent
writes here — chat replies, PR descriptions, issue comments, commit bodies,
and every page under `docs/`. `docs/agents/writing-style.md` pins the rules
drafts break by default and this repo's overrides; read it before writing a
`docs/` page, PR description, or issue comment; use `.skills/google-style/SKILL.md`.

The short list, which holds in every reply:

1. Use CONTEXT.md names exactly — never rotate synonyms. Spell out any
   other abbreviation on first use: RBAC (role-based access control).
2. Cut *currently*, *now*, *new*, *simply*, *just*, *easy*, *please note*.
3. Conditions before instructions. The reader is *you*; Sediment is never
   *we*.
4. Active voice with a named actor, present tense, one idea per sentence.
5. Answer first, and only the prose the builder asked for. Name a
   simplification and move on ("Did X; Y covers it. Need full X? Say so.").
   A walkthrough, report, or review the builder asked for is not fluff;
   give that in full.
6. Mark choices with "Decision:" — options and a default.
7. State uncertainty plainly: "I did not verify X."

Commit subjects and PR titles follow Conventional Commits
(`type(scope): description`); scopes are the package map plus `scripts`,
`sim`, and `deps`. The repo squash-merges, so the PR title is the subject
that lands. CI checks it; `scripts/check_commit_msg.py` runs the same check
locally. Full dialect: CONTRIBUTING.md §Commits.

## Licensing

AGPL-3.0-or-later (see `LICENSE`). Every first-party `.py` file begins with:

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
```

`uv run python scripts/add_spdx.py` inserts missing headers (idempotent);
CI runs `--check`. `shims/` is carved out as MIT (`shims/pi/LICENSE`,
`// SPDX-License-Identifier: MIT`) — shim code runs inside someone else's
harness process, where AGPL blocks adoption.

Open-core boundary: a single team's complete, auditable pipeline stays open —
see `docs/adr/0006-open-core-boundary.md`. Review cross-team operational
capabilities against that contract. Issues labeled `enterprise-tier` mark
proposals outside the open-core scope.

## Package Map

| Package | Purpose | Read first |
|---|---|---|
| `packages/core` | Fact models, Basic redaction, the PostgreSQL FactStore, bounded evidence projections | `docs/agents/fact-store.md` + `docs/agents/postgresql.md` |
| `packages/capture` | Gateway adapters, OTLP translators, forge parsers | `docs/agents/capture-translators.md` |
| `packages/derive` | Attribution, CI resolution, merge and edit retention, Rollouts, Recovery pairs, the mirror, eval splits | `docs/agents/derivations.md` |
| `packages/export` | Attributed completions, canonical schemas, trainer mapping and consumer profiles, DPO/SFT/Recovery/RLVR rows, reports, statistics | `docs/agents/exports-and-stats.md` + `docs/agents/statistics.md` |
| `apps/api` | FastAPI ingest, OTLP receiver, authenticated report and evidence reads, mirror GC | `docs/agents/api-and-operations.md` |
| `cli` | The `sediment` command (`sediment-cli` on PyPI): server, login, install, exports, transcript and Attribution clients | `docs/agents/api-and-operations.md` |
| `shims` | Harness extensions (pi); the one TypeScript surface | `docs/agents/capture-clients.md` |

Do not create a new package without a tracked issue.

### Topical docs

Every page under `docs/` has one route here; `scripts/check_docs.py` fails
on an orphan. Report a routed file that does not exist as a bug.

**Start here**

| Topic | Read |
|---|---|
| Reading order for humans and agents | `docs/onboarding.md` |
| Try Sediment locally | `docs/quickstart.md` |
| Generated references — edit the source, never the page: CLI flags, HTTP routes, schema fields, consumer profiles | `docs/reference/cli.md`; `docs/reference/api.md`; `docs/reference/schema.md`; `docs/reference/compatibility.md` |

**Concepts** (`docs/explanation/`)

| Topic | Read |
|---|---|
| Facts, Derivations, and the two artifacts | `docs/explanation/how-sediment-works.md` |
| Components, data flow, persistence, network boundaries | `docs/explanation/architecture.md` |
| Why Sediment: model outcomes, retained code, rework | `docs/explanation/operational-value.md` |
| Capture signals, edit survival, privacy boundaries | `docs/explanation/how-capture-works.md` |
| Snapshots, policy, cohorts, and the canonical bundle | `docs/explanation/how-derivation-works.md` |
| Attribution semantics (notes → jaccard) | `docs/explanation/attribution.md` |

**Operate a deployment** (`docs/operate/`)

| Topic | Read |
|---|---|
| Deploy on EC2 or your own host, then enroll a team | `docs/operate/deploy-ec2.md`; `docs/operate/deploy.md`; `docs/operate/run-pilot.md` |
| Upgrade, back up, rotate credentials, quarantine, tear down; stored data, network exposure, release checks | `docs/operate/maintain.md`; `docs/operate/security.md` |
| Run, scope, and recompute Derivations; profile them | `docs/operate/run-derivations.md`; [Profile reports and Derivations](CONTRIBUTING.md#profile-reports-and-derivations); `docs/adr/0020-bounded-derivation-execution.md` |
| Measure agent work: model outcomes, lifecycle, merge retention | `docs/operate/measure-agent-work.md` |
| Rehearse the Compose deployment from source | `docs/operate/rehearse-compose.md` |
| Bounded evidence reads and agent continuation | `docs/operate/resume-with-evidence.md`; ADRs 0021, 0022, 0025, 0026 |
| Synthetic scenarios | `sim/README.md` |

**Capture clients** (`docs/capture/`)

| Topic | Read |
|---|---|
| One developer machine: git hooks, agent hooks, transcripts, sender replay | `docs/capture/local-capture.md` |
| Choose an agent integration | `docs/capture/agent-integrations.md`; `docs/capture/agents/claude-code.md`; `docs/capture/agents/codex.md`; `docs/capture/agents/cursor.md`; `docs/capture/agents/pi.md` |
| Gateways, webhooks, mirrors, fleet distribution | `docs/capture/managed-capture.md` |

**Exports** (`docs/exports/`)

| Topic | Read |
|---|---|
| Choose a training objective and consumer profile | `docs/exports/training-exports.md`; `docs/exports/consumer-compatibility.md` |
| DPO pairs; SFT and diff-SFT rows; Recovery rows | `docs/exports/dpo.md`; `docs/exports/sft.md`; `docs/exports/recovery.md` |
| RLVR artifacts (`tasks.jsonl`, `rollouts.jsonl`, manifest) | `docs/exports/rlvr-export.md` |

## API Conventions (`apps/api`)

Ingest routers write Facts only; they don't compute or persist Derivation
output in the request path (ADR 0001). A Push delivery may trigger a
background Attribution Derivation after the response; that task logs
structured counts and discards its result. Query and report routes may
compute read-only Derivation output for their responses. Tenancy binds to the
deployment (`SEDIMENT_ORG_ID`), never to the request. Response shapes:

- Single Fact ingest (`/ingest/gateway`, `/ingest/ci`, and
  `/ingest/github/{push,ci,pull-request,repository}`) returns
  `{"fact_id": "<uuid>", "stored": <bool>}`. `stored: false` means a
  redelivery collapsed on a UNIQUE index (ADR 0003) — success, not an error.
- A route declining a payload (wrong `X-GitHub-Event`, nothing storable)
  returns 200 `{"skipped": true, "reason": "<why>"}` so senders don't retry.
- `POST /v1/logs` returns `{}` — the empty OTLP/HTTP JSON
  `ExportLogsServiceResponse`; per-record dedup is invisible to the exporter.
- Auth failures are 401 (bad bearer / bad `X-Hub-Signature-256`); malformed
  bodies from authenticated callers are 400/422, never 500.

## Language and Tooling

- Python 3.12 (`.python-version`; CI pins 3.12 and installs with
  `uv sync --locked`). uv for everything (`uv add`, never `pip install`).
- `shims/` is the only TypeScript, because pi requires it; nothing else may
  add a second toolchain. Node per `shims/pi/package.json` `engines`; run
  `npm ci && npm run typecheck && npm test` in the shim directory, mirrored
  by the `shims` CI job.
- ruff for lint and format (line length 88) is the only static check; there
  is no mypy gate.
- pytest; run one package with `uv run pytest packages/<name>`. Test-file
  basenames must be unique repo-wide, and `tests/` directories carry no
  `__init__.py`.
- Fixtures live in `tests/fixtures/` within the packages that need them.
  Capture's fixture filenames are a cross-package API — derive, apps/api, and
  `scripts/smoke.py` consume them by relative path. Wire-capture fixtures are
  frozen: never regenerate or trim them.
- Never mock Pydantic models — instantiate with real data. Derivation tests
  don't mock git either: they build real repositories.
- No print statements in library or service code — structured logging with
  ids in log lines. Operator CLIs and `scripts/` print; stdout is their
  interface.
- No cloud SDK imports outside `packages/export`.
- Adding a workspace member is five edits across three files: member
  `dependencies` + member `[tool.uv.sources]` (its own `pyproject.toml`),
  root `[project] dependencies` + root `[tool.uv.sources]`, and a
  Dockerfile `COPY` line for its `pyproject.toml`.

## Agent skills

Repository skills live in `.skills/`. Every harness reads the matching
`SKILL.md` before its task; resolve repository paths from the checkout root.
Use this index directly when the harness doesn't discover `.skills/` itself.

| Task | Skill | Maintained procedure |
|---|---|---|
| Prepare and publish a release | [.skills/sediment-release/SKILL.md](.skills/sediment-release/SKILL.md) | `CONTRIBUTING.md` |
| Sync docs before opening or updating a PR | [.skills/doc-sync/SKILL.md](.skills/doc-sync/SKILL.md) | `docs/agents/doc-sync.md` |
| Review a behavior-changing PR before merge | [.skills/review-closeout/SKILL.md](.skills/review-closeout/SKILL.md) | `docs/agents/review.md` |
| Write or edit prose | [.skills/google-style/SKILL.md](.skills/google-style/SKILL.md) | `docs/agents/writing-style.md`; `docs/agents/doc-style.md` |
| Edit or verify a Mermaid diagram | [.skills/beautiful-mermaid/SKILL.md](.skills/beautiful-mermaid/SKILL.md) | `docs/agents/mermaid-diagrams.md` |

Start with `docs/onboarding.md` and `CONTRIBUTING.md`. Claim an issue before
starting; PRs say `Closes #<n>`. GitHub issues live in `sediment-ai/sediment`;
follow `docs/agents/issue-tracker.md`. Merging requires a maintainer's approval.
Put domain terms in `CONTEXT.md` and architectural decisions in `docs/adr/`;
follow `docs/agents/domain.md`.

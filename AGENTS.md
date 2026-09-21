# AGENTS.md — Sediment

## What Sediment Is

A self-hosted pipeline that turns AI developer workflow traces into RL-ready
training data. It captures inference calls, developer accept/reject decisions,
Edit observations (retention within a Session plus external line deltas), Retry
linkages, git pushes, and CI outcomes as **immutable Facts**; derives
Attributions, edit retention scores, and Reward linkage as **pure,
recomputable functions** over those Facts; and
exports two canonical artifacts — Attributed completions and Rollouts —
projected into DPO/SFT and RLVR training rows (plus the Fact-derived Recovery
pair, ADR 0004's one sanctioned exception). Full picture: `docs/explanation/how-sediment-works.md`.
The architecture is governed by the
ADRs in `docs/adr/` (0001–0005 core, 0006 the open-core boundary, 0007
client-side transcript parsing, 0008 structured inference calls, 0009 canonical
Attribution, 0010 canonical CI outcomes, 0011 training-objective evidence, and
0012 the PostgreSQL-only Fact store, 0013 Git-note observation boundaries, 0014 factual outcomes and training evidence,
0015 lossless representation and bundle v2, 0016 bundle derivation consistency, 0017 bounded sender transport storage, 0018 credential authorities, and 0019 repository identity and renames)
— read them before changing anything structural. Current status is `CHANGELOG.md` plus the GitHub milestones.

Python 3.12 for pipeline code. Only `shims/` permits TypeScript (pi requires it).
Harness extensions use their own API; sediment-specific shims live here.
CI scopes Node to `shims/`; nothing else may add a second toolchain.

## The Non-Negotiable Rules

1. **Facts are the only persisted domain state.** ADR 0017 permits an opt-in transport buffer that Derivations never read. A Fact is something that happened:
   an inference call, a decision, a push, a CI outcome. Facts are appended, never
   mutated. If you find yourself writing an UPDATE on a Fact table or storing
   the output of a matcher/policy, stop — that belongs in a Derivation.
2. **Derivations are pure functions** of (Facts, policy) and must be
   recomputable over all history. No Derivation may depend on wall-clock
   ingest order or webhook arrival order.
3. **Sessions are the aggregate root.** Developer-side Facts carry a
   `session_id`; the Session row is upserted at the storage seam. Never
   invent placeholder Session ids.
4. **Dedup lives in the database.** `UNIQUE` indexes enforce idempotency —
   do not re-implement dedup scans in application code.
5. **Schema is the source of truth.** All Fact shapes live in
   `packages/core/sediment_core/models.py`. Never define Fact shapes inline
   elsewhere. Derived shapes (`Attribution`, `Rollout`, `Turn`, `CommitRef`,
   `AttributedCompletion`, `RecoverySample`, `Provenance`, every `*Policy`) are frozen
   dataclasses — Pydantic is only for Facts, the notes wire contract,
   settings loaders, and HTTP request envelopes. A field that is a join key,
   a dedup-index component, or the tenancy key gets a validated type
   (`NonEmptyId`, `CommitSha`, `OrgId`, `RepoSlug`, `BranchName`,
   `AwareDatetime`), never a bare `str` or naive `datetime` —
   route-local validation helpers are the defect that rule replaces.
6. **Training objectives own evidence interpretation.** Capture Facts once,
   then let each exporter interpret only the evidence appropriate to its
   training objective. Every training row names one Evidence recipe and
   preserves the source of every label, eligibility decision, and Reward. Do
   not extend the legacy mixed DPO/SFT behavior; ADR 0011 defines its
   replacement.

## House Rules

- **Absent, never guessed at.** Anything unknowable is omitted visibly and
  logged — never fabricated (`verification_command`, manifest entries, diffs,
  judgments). In Derivations and projections, ineligible inputs are skipped
  **and counted** under the module's closed skip-reason vocabulary; capture
  and Derivation fail-soft paths additionally log-and-degrade rather than
  raise ([Fail-soft](CONTEXT.md#fail-soft)). Never silently dropped, never emitted
  half-formed.
- **Bump `policy_version`** when tuning any policy knob; the Provenance
  stamp is what keeps pre- and post-change datasets distinguishable.
  (`DPOPolicy`/`SFTPolicy`/`MirrorPolicy` carry no version field — a known
  gap; call such tunings out in the PR description.)
- **Every new Derivation ships determinism tests**: same Facts → identical
  output, and shuffled ingest order → identical output.
- **Climb before you build.** In order: does it need to exist at all, does
  this repo already have it, stdlib, a native platform or DB feature, an
  already-installed dependency, one line — then the minimum code that
  works. Stop at the first rung that holds. No interface with one
  implementation, no factory for one product, no config for a value that
  never changes, no new dependency for what a few lines cover. The ladder
  shortens the solution, never the reading: trace the real flow first, then
  climb.
- **Root cause, not symptom.** An issue names a symptom. Grep every caller
  of the function you are about to touch before editing it — one guard in
  the shared function is a smaller diff than a guard in each caller, and
  patching only the path the issue names leaves every sibling broken.
- **`ponytail:` marks a deliberate simplification** so that simple reads as
  intent rather than oversight (existing uses in `scripts/smoke.py`,
  `scripts/second_review.py`). Where the shortcut has a known ceiling, the
  comment names that ceiling and the upgrade path: `# ponytail: global
  lock, per-account locks if throughput matters`.
- **Non-trivial logic leaves one runnable check** — the smallest thing that
  fails if the logic breaks. This is the floor for branches, parsers, and
  auth paths outside the Derivation layer; it never relaxes the determinism
  rule above.
- **Never simplified away**: validation at trust boundaries, the fail-soft
  paths that keep Facts from being lost, SPDX headers, and anything the
  builder asked for by name. Shortest diff breaks ties between correct
  options; it is never a reason to ship the flimsier one.

## Communication

The [Google developer documentation style
guide](https://developers.google.com/style) governs every word an agent
writes here — chat replies to the builder, PR descriptions, issue comments,
commit message bodies, and every page under `docs/`.
`docs/agents/writing-style.md` pins the rules that drafts break by default,
the words to swap, and this repo's overrides. Read it before writing a
`docs/` page, a PR description, or an issue comment (Claude Code:
`/google-style`).

The short list, which holds in every reply:

1. Use CONTEXT.md names exactly — never rotate synonyms. Spell out any
   other abbreviation on first use: RBAC (role-based access control).
2. Cut *currently*, *now*, *new*, *simply*, *just*, *easy*, *please note*.
3. Conditions before instructions. The reader is *you*; Sediment is never
   *we*.
4. Active voice with a named actor, present tense, one idea per sentence.
5. Answer first, prose second, and only the prose the builder asked for. Do
   not argue for a simplification at length — name it and move on ("Did X;
   Y covers it. Need full X? Say so."). A walkthrough, report, or review
   the builder asked for is not fluff; give that in full.
6. Mark choices with "Decision:" — options and a default.
7. State uncertainty plainly: "I did not verify X."

Commit subjects and PR titles follow Conventional Commits
(`type(scope): description`) with a closed scope vocabulary — the package
map plus `scripts`, `sim`, and `deps`. The repo squash-merges, so the PR
title is the subject that lands; CI checks it, and
`scripts/check_commit_msg.py` is the same check to run locally. Full
dialect, the type table and the opt-in hook: CONTRIBUTING.md §Commits.

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
| `packages/core` | Fact models + Basic redaction + PostgreSQL FactStore — source of truth | `docs/agents/fact-store.md` |
| `packages/capture` | Gateway adapters, OTLP translators, forge parsers | `docs/agents/capture-translators.md` |
| `packages/derive` | Inference-call views, mirror, notes/jaccard Attribution (`diff`, `precision_harness`, `precision_report`), CI resolution, merge retention, Attribution-share metric + decline alert, Rollouts, Recovery pairs, edit retention + final Fate, eval `split`, shared joins + historical Session observation binding (`session_commit`) | `docs/explanation/attribution.md` + `docs/agents/derivations.md` |
| `packages/export` | Attributed completions (`attributed_completions`), Confidence ladder (`label_confidence`), canonical schemas (`schema_contracts`, `schema_identity`), canonical-to-trainer mapping (`trainer`), consumer profiles (`compatibility`, `consumer_rlvr`), bounded bundle/training execution (`derived_bundle`, `bounded_training`, `staged_rows`, `_record_storage`), DPO/SFT/diff-SFT/recovery, RLVR (`rlvr`, `environment_manifest`, `verifier_commands`, `jsonl`), outcome and merge-retention reports, `significance`, `calibration`, `label_confidence_inspection`, `dataset_diagnostics`, `decision_latency` | `docs/agents/exports-and-stats.md` + `docs/agents/statistics.md` (+ `docs/exports/rlvr-export.md`) |
| `apps/api` | FastAPI ingest + OTLP receiver + authenticated operational-report reads + operator CLI (`sediment`: managed local PostgreSQL, quarantine, exports, reports, mirror GC) | `docs/agents/api-and-operations.md` |

All five are shipped. Do not create a new package without a tracked issue.

### Topical docs

Grouped by reader intent; `docs/` subdirectories mirror these groups.

**Start here**

| Topic | Read |
|---|---|
| New here (humans and agents) — reading order | `docs/onboarding.md` |
| Try Sediment locally (user journey entry) | `docs/quickstart.md` |
| Every `sediment` command and flag (generated — edit the parser) | `docs/reference/cli.md` |
| Every HTTP route, its auth and status codes (generated — edit the route) | `docs/reference/api.md` |
| Every Fact, derived artifact and training-row field (generated — edit the class) | `docs/reference/schema.md` |

**Concepts** (`docs/explanation/`)
| Topic | Read |
|---|---|
| How it works: Facts, Derivations, the two artifacts (`docs/explanation/`) | `docs/explanation/how-sediment-works.md` |
| Components, data flow, persistence, and network boundaries | `docs/explanation/architecture.md` |
| Why Sediment? Model outcomes, retained code, and rework | `docs/explanation/operational-value.md` |

**Operate a deployment** (`docs/operate/`)
| Topic | Read |
|---|---|
| Deploy, enroll a team, and verify a self-hosted installation | `docs/operate/deploy.md`; `docs/operate/security.md`; `docs/operate/run-pilot.md`; `docs/operate/validate-deployment.md` |
| Rehearse releases; measure and investigate agent work | `docs/operate/rehearse-release.md`; `docs/operate/lifecycle-report.md`; `docs/operate/measure-agent-work.md` |
| Network exposure: ports, outbound, perimeter | [Network exposure](docs/operate/deploy.md#84-network-exposure) |
| Quarantine / incident response | [Quarantine and wholesale deletion](docs/operate/deploy.md#83-quarantine-and-wholesale-deletion), then `docs/agents/fact-store.md` + `docs/agents/api-and-operations.md` |
| Sim: scenario explainer | `sim/README.md` |

**Capture clients** (`docs/capture/`)

| Topic | Read |
|---|---|
| Configure one developer machine, git hooks, agent hooks, and transcript capture | `docs/capture/local-capture.md` |
| Compare agent integrations and choose a guide | `docs/capture/agent-integrations.md` |
| Capture Claude Code work | `docs/capture/agents/claude-code.md` |
| Capture Codex work | `docs/capture/agents/codex.md` |
| Capture Cursor work | `docs/capture/agents/cursor.md` |
| Configure gateways, webhooks, mirrors, and fleet distribution | `docs/capture/managed-capture.md` |
| Capture signals, Attribution, edit survival, and privacy boundaries | `docs/explanation/how-capture-works.md` |
| What a harness shim must emit (`sediment.tool_decision`, gateway envelope) | `docs/agents/capture-clients.md` |

**Derive**

| Topic | Read |
|---|---|
| Facts, snapshots, policy, cohort selection, and the canonical bundle (`derived_bundle`) | `docs/explanation/how-derivation-works.md` |
| Attribution semantics (notes → jaccard) | `docs/explanation/attribution.md` |
| Run, scope, inspect, recompute, and export a Derivation | `docs/operate/run-derivations.md`; [profiling](docs/operate/profile-derivations.md); [bounded execution](docs/adr/0020-bounded-derivation-execution.md) |

**Exports and analysis** (`docs/exports/`)

| Topic | Read |
|---|---|
| Choose a training objective and consumer profile | `docs/exports/training-exports.md`; `docs/exports/consumer-compatibility.md`; `docs/reference/compatibility.md` |
| Export DPO pairs | `docs/exports/dpo.md` |
| Export SFT and diff-SFT rows | `docs/exports/sft.md` |
| Export Recovery rows | `docs/exports/recovery.md` |
| RLVR artifacts (`tasks.jsonl` / `rollouts.jsonl`, manifest) | `docs/exports/rlvr-export.md` |
| Why training objectives use separate evidence | `docs/adr/0011-training-objectives-own-evidence-interpretation.md` |

**Maintained implementation contracts**

| Topic | Read |
|---|---|
| PostgreSQL schema, migrations, snapshots, and runtime boundary | `docs/agents/postgresql.md` |
| Capture observations, source coverage, and final Fate | `docs/explanation/how-capture-works.md`; `docs/agents/capture-clients.md` |
| Merge retention and accepted-work lifecycle | `docs/operate/lifecycle-report.md`; `docs/agents/derivations.md` |
| Immutable Git-note Session-to-commit observations | `docs/adr/0013-git-note-observation-facts.md` |
| Factual outcomes and canonical representation | `docs/adr/0014-factual-outcomes-and-training-evidence.md`; `docs/adr/0015-lossless-values-and-bundle-v2.md`; `docs/adr/0016-bundle-derivation-consistency.md` |
| Sender delivery and replay | `docs/adr/0017-sender-transport-replay.md`; `docs/capture/local-capture.md` |
| Credentials and repository identity | `docs/adr/0018-static-credential-authorities.md`; `docs/adr/0019-repository-identity-and-renames.md` |
| Pipeline acceptance and release artifacts | `docs/operate/rehearse-release.md`; `docs/agents/exports-and-stats.md` |
| Contributor setup and issue maintenance | `CONTRIBUTING.md`; `docs/agents/issue-tracker.md` |

Maintained documentation and ADRs carry contributor-facing contracts.

Other topics: `CONTEXT.md` and `docs/adr/`. Report nonexistent routed files as bugs.

## API Conventions (`apps/api`)

Ingest routers write Facts only; they don't compute or persist Derivation output
in the request path (ADR 0001). A Push delivery may trigger a background Attribution
Derivation after the response. That task logs structured counts and discards its result.
Query and report routes may compute read-only Derivation output for their responses.
Tenancy binds to the deployment (`SEDIMENT_ORG_ID`), never to the request. Response shapes:

- Single Fact ingest (`/ingest/gateway`, `/ingest/github/push`,
  `/ingest/github/ci`, `/ingest/github/pull-request`, `/ingest/ci`)
  returns `{"fact_id": "<uuid>", "stored": <bool>}`. `stored: false` means a
  redelivery collapsed on a UNIQUE index (ADR 0003) — success, not an error.
- A route declining a payload (wrong `X-GitHub-Event`, nothing storable)
  returns 200 `{"skipped": true, "reason": "<why>"}` so senders don't retry.
- `POST /v1/logs` returns `{}` — the empty OTLP/HTTP JSON
  `ExportLogsServiceResponse`; per-record dedup is invisible to the exporter.
- Auth failures are 401 (bad bearer / bad `X-Hub-Signature-256`); malformed
  bodies from authenticated callers are 400/422, never 500.

## Language and Tooling

- Python 3.12; CI pins 3.12 (setup-uv) and installs with `uv sync --locked`
  — match it locally (`uv python pin 3.12` writes `.python-version`; there
  isn't one yet). uv for everything (`uv add`, never `pip install`).
  `shims/` is the scoped TypeScript exception (see above): Node ≥ 22.18,
  `npm ci && npm run typecheck && npm test` in the shim dir, mirrored by the
  `shims` CI job
- ruff for lint + format (line length 88); no mypy gate — ruff is the only
  static check
- pytest; run one package with `uv run pytest packages/<name>`. Test-file
  basenames must be unique repo-wide and `tests/` dirs carry no
  `__init__.py` (pytest rootdir collection breaks otherwise)
- Fixtures live in `tests/fixtures/` within the packages that need them.
  Capture's fixture filenames are a cross-package API — derive, apps/api,
  and `scripts/smoke.py` consume them by relative path. Wire-capture
  fixtures are frozen: never regenerate or trim them
- Never mock Pydantic models — instantiate with real data. Derivation tests
  don't mock git either: they build real repos (see the playbooks)
- No print statements in library/service code — structured logging with ids
  in log lines. Operator CLIs and `scripts/` are the exception: stdout is
  their interface, and they print.
- No cloud SDK imports outside `packages/export`
- Adding a workspace member is five edits across three files: member
  `dependencies` + member `[tool.uv.sources]` (its own `pyproject.toml`),
  root `[project] dependencies` + root `[tool.uv.sources]`, and a
  Dockerfile `COPY` line for its `pyproject.toml`

## Agent skills

Start with `docs/onboarding.md`. Claim an issue before starting; PRs say `Closes #<n>`.

### Issue tracker

Issues are GitHub issues in `sediment-ai/sediment` (`gh` CLI). Label
vocabulary (triage roles and repo extras), lifecycle rules, and the
external-PR triage flag: `docs/agents/issue-tracker.md`.

### Doc sync

Before opening or updating a PR, follow `docs/agents/doc-sync.md`
(Claude Code: `/doc-sync`) — the same-PR docs rule and full procedure live
there. CI enforces the mechanical half (`scripts/check_docs.py`: router
completeness, live routes, caps, resolvable citations). Which mode a page is
— tutorial, how-to, reference, explanation — and the rules each follows:
`docs/agents/doc-style.md`. `docs/agents/writing-style.md` governs the prose.
Read both before adding a page.

### Review closeout

Behavior-changing PRs get an adversarial review before merge — contract,
scope governor, and the optional codex cross-model pass live in
`docs/agents/review.md` (Claude Code: `/review-closeout`). Merging always
requires a maintainer's approval.

### Mermaid diagrams

Diagram syntax, style, and verification live in
`docs/agents/mermaid-diagrams.md` (Claude Code: `/beautiful-mermaid`). Read it before editing any
` ```mermaid ` block under `docs/` or in `README.md`.

### Domain docs

Single-context — `CONTEXT.md` + `docs/adr/` at the repo root; ADRs 0001–0019
are binding. Use `CONTEXT.md` terms exactly. Put architectural decisions in
`docs/adr/` and domain terms in `CONTEXT.md`. See `docs/agents/domain.md`.

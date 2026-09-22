# API & operations playbook — `apps/api`, CLI, scripts, deployment glue

Values drift, so the cited file wins. [API Conventions](../../AGENTS.md#api-conventions-appsapi) holds response contracts; `docs/operate/deploy.md` covers deployment. This file carries the remaining service, CLI, and glue invariants.

## Module map

| Module | Purpose |
|---|---|
| `sediment_api/main.py`, `workers.py`, `worker.py` | App lifespan; fixed worker processes and pipe protocol; security gate; PostgreSQL error handler; `/health` |
| `sediment_api/database.py` | One-shot PostgreSQL engine ownership for operator commands |
| `sediment_api/config.py` | `Settings` (pydantic-settings, `SEDIMENT_` prefix) + module singleton |
| `sediment_api/deps.py` | explicit ingest/operator/retrieval authority checks, `/v1/me` identity, `get_store`, body-size middleware, capped body reader, verified-webhook reader |
| `sediment_api/services/operational_reports.py` | Delivery-neutral lifecycle report request, result envelope, and canonical JSON serialization |
| `sediment_api/routers/` | `gateway.py`, `forge.py` (push/CI/pull-request merge/repository rename + mirror-refresh background chain), `otlp.py`, `query.py`, `ci_vendor.py`, `v1.py` (read-only probes), and `reports.py` (bounded operational reports) |
| `cli/sediment_cli/` (top-level member) + `sediment_api/reports/`, `mirror_gc.py` | the `sediment` command: `cli.py` verbs, `client.py` HTTP seam, `evidence.py` selection validation and private packet publication, `attribution.py` stamper, installer, and Cursor hook adapter, `transcript.py` client-side transcript parser, `ui.py` brand-palette styling; reports/GC stay api-side, the CLI forwards; ships in the API image and as the `sediment-cli` PyPI distribution |
| `litellm/sediment_callback.py` | Forwarder with stable capture metadata and optional process-owned replay worker; identity extraction stays server-side (`session_identity.py`) |
| `cli/sediment_cli/local_postgres.py` | Checksum-pinned native PostgreSQL download, private cluster initialization, readiness, root ownership lock, and owned-process shutdown for `sediment server` |
| `cli/sediment_cli/delivery.py` | Shared stdlib HTTP acknowledgment and private prepared-payload buffer; `sediment delivery enqueue/status/replay`; enqueue's `--fallback-direct` selects shared best-effort storage-fault delivery. Checkout entry `scripts/sediment_delivery.py`; copy the implementation for standalone deployment (ADR 0017) |
| `scripts/smoke.py` | Fires capture's frozen fixtures at a running deployment |
| `scripts/release_rehearsal.py` | Builds six distributions; exercises installed sender outage/restart/lost-response recovery, scratch PostgreSQL, HTTP reports, bundle v4, and strict training without publishing |
| `scripts/capacity_rehearsal.py` | Disposable synthetic capture/report/export qualification; strict profiles, complete histories, receipt conservation, and scoped resource measurements. See [Profile reports and Derivations](../operate/profile-derivations.md#rehearse-capture-alongside-batch-work) |
| `scripts/cursor_enterprise_fixture.py` | Guides one Cursor Enterprise macOS fixture run; isolates git and project hooks, retrieves optional enterprise evidence, sanitizes artifacts, and writes one ZIP |
| `scripts/turn_worktree_observation_spike.py`; `scripts/session_context_retrieval_eval.py` | Local turn-boundary diagnostic (no Fact or network request); isolated pi continuation comparison with frozen task selection and native tool preflight, private records, and enforced transport budgets, respectively. See [Continue a task](../operate/resume-with-evidence.md) |
| `sim/` | Synthetic scenario suite: `gen_repo.py` deterministic sim repo; `scenarios.py` catalog (in-process, org `simcorp`); `precision_report.py` notes/jaccard P/R gate |
| `sim/driver/` | Live-agent driver: `seed_remote.py` seeds the remote and hooks a clone; `tasks.py` scripted tasks; `run.py` drives agents, fails on any empty funnel layer; `drift_report.py` wire drift vs frozen fixtures. Operator-gated — `seed_remote.py` force-pushes, `run.py` pushes; neither belongs in the unattended allowlist |
## Service invariants (`apps/api`)

- **The import security gate rejects missing, placeholder, duplicate, or overlapping operator/ingest secrets.** `SEDIMENT_DEV_MODE=true` is the only opt-out. The optional `SEDIMENT_RETRIEVAL_TOKEN` / `SEDIMENT_RETRIEVAL_SESSION_ID` pair always validates, including development mode: strong distinct token, `NonEmptyId` source, and reserved ingest client name `retrieval`. [ADR 0022](../adr/0022-agent-requested-session-context.md) defines its authority. Lesser problems warn (`config.py`).
- Ingest accepts capture or operator authority; existing query, reports, and Fact reads require operator authority. `/v1/me` also accepts retrieval authority and reports its fixed `source_session_id`. Capture enrollment and operator login reject retrieval credentials. Unknown credentials return 401; insufficient authority returns 403. Legacy bearer credentials are ingest-only. [ADR 0018](../adr/0018-static-credential-authorities.md) defines revocation and compatibility.
- `enable_docs=False` closes all three of `/docs`, `/redoc`, `/openapi.json`.
- Router prefixes: `/ingest` (gateway and forge), `/query`, and read-only `/v1`. OTLP's fixed `/v1/logs` stores decisions, Edit observations, rejected edits, and Retry linkages; malformed linkages count one skip without dropping siblings.
- OTLP's organization-qualified `otlp_logs_received` receipt logs record/container dispositions and candidates, stored Facts, and duplicates for all four Fact types, including zeros. `routers/otlp.py` emits it only after every write succeeds. The decision batch uses bounded SQL statements in one transaction with its Session upserts. Later Fact kinds retain independent transactions. A later failure can leave a committed prefix across kinds; it emits no completed receipt, and database uniqueness accounts for the prefix on replay. The HTTP success body stays `{}`; see [Capture receipts](../explanation/how-capture-works.md#capture-receipts).
- **`settings` is a singleton built at import.** `tests/conftest.py` sets environment values before importing FastAPI; tests patch singleton attributes.
- The lifespan owns one bounded SQLAlchemy engine and engine-backed `FactStore`.
  It checks the exact Alembic head, then validates runtime privileges in production before starting workers. It never migrates or mutates schema. Ingest borrows its pool; expensive child jobs create separate single-connection engines.
  Shutdown stops owned process groups before disposing the main engine. API connections limit statements to 30 seconds, locks to five seconds, and idle transactions to 30 seconds; operator engines retain separate budgets.
- An absent, behind, ahead, unreachable, or unauthorized database blocks startup.
  Diagnostics expose only the operation and sanitized target. Post-start outages
  return `database_unavailable` 503 after authentication and validation;
  programmer errors return 500.
- **Every door bounds its body read** at `MAX_BODY_BYTES` (25 MiB, then 413)
  through `BodySizeLimitMiddleware`; `POST /query/evidence/read` uses 64 KiB and `POST /query/context` uses 16 KiB. It runs pre-auth on every door,
  because the server reads envelope bodies before it authenticates them. The
  webhook door keeps its own streaming reader, which uses a `bytearray` and
  ignores `Content-Length`.
- **422s never echo the input.** `main.py`'s `RequestValidationError` handler
  returns `type`, `loc`, and `msg`. The default handler 500'd instead, because
  it recursed over a nested body's `input`.
- **Verify the signature before examining the event type.** Otherwise GitHub's
  setup ping green-lights a misconfigured webhook. A missing bearer returns
  401, never 422. The gateway envelope sets `extra="forbid"`, so it rejects a
  client that names an org rather than ignoring the field.
- Gateway `capture` validates UUID and aware observation time together. `store_inference_call_receipt` owns retained Fact IDs; identity contradictions return content-free 409. `SEDIMENT_DELIVERY_DIR` enables private transport storage only, never capture; limits, recovery, and worker enrollment live in [Sender replay operations](../capture/local-capture.md#preserve-prepared-payloads-through-outages).
- **`POST /ingest/ci` uses provider-issued run identity, not a URL.** The sender
  declares the normalized `provider` and its pipeline `run_id`; the route does
  not infer either value from `run_url`. It accepts a nullable positive
  `run_attempt` and keeps `run_url` optional. Optional provider results, error
  evidence, and source-event fields pass through to `CIOutcome`; the route
  doesn't infer missing values. Canonical identity validation rejects NUL and surrogates before storage; descriptive content preserves them. Unknown fields remain a 422.
- GitHub repository-bearing routes return retained Fact receipts; conflicting identity returns content-free 409. The repository route stores a Repository rename even with mirrors disabled and never moves a directory.
- **Commit investigation separates sources.** `/query/commit/{sha}` groups by repository identity with observed names; unknown CI stays in `unresolved_ci_outcomes`, with closed `repository_skipped` source counts. Similarity-selected calls remain `relationship="inferred_call_to_file"` and cannot set factual `attributed` true. One request instant or explicit `as_of` bounds supporting evidence. `sediment commit` forwards name/provider/host/ID and boundary selectors and displays distinct lifetimes.
- **CI investigation starts from failure evidence.** Exact run lookup and failure pages select a complete repository provider/host/ID or an unambiguous name; multiple namespaces return 409 `repository_selector_ambiguous`. Failure pages keep half-open capture bounds and inclusive `as_of` (default `captured_before`). Cursors bind tenant, qualified key, boundary, filters, and quarantine revision; mismatches return 422. Complete identity overflow returns 409 `repository_evidence_limit`. Metadata-only rows link to commit inspection with their boundary and identity. The CLI preserves these closed diagnostics.
- Query responses validate their declared response model in Python, then ASCII-escape descriptive strings. Descriptive strings remain exact. A non-finite number in emitted content declines the whole response with 409 and `detail.reason="non_finite_number"`; stored Facts stay intact. Other errors keep their own handling.
- **Evidence reads require operator authority.** The three `/query/evidence` operations return strict, ASCII-escaped JSON with `Cache-Control: no-store`; request-local snapshots recheck Quarantine. The supervisor admits at most one evidence worker within its two read slots. Fixed source and response bounds decline whole requests; [ADR 0021](../adr/0021-bounded-evidence-access.md) defines the contract. `POST /query/context` accepts retrieval or operator authority for the configured Session; it shares that worker limit and emits versioned keyword selection under [ADR 0022](../adr/0022-agent-requested-session-context.md).
- **The Session dossier is metadata-only.** `GET /query/session/{session_id}` reads one repeatable-read snapshot, caps visible Session Facts and linked delivery evidence at 500 rows, and returns 409 instead of a partial result. It emits Fact time and identity, capture-presence fields, observation-backed Session commit summaries, exact-head Push receipts, and matching CI outcomes. Compatibility `attribution_sources` stays empty; unavailable similarity extrema and `attributed_files` are omitted. Missing observation evidence counts once under `session_commit_unobserved`; mirrors cannot establish identity. Response models exclude captured content, user identity, clone URLs, file paths, and the free-form CI reason.
- **Background chain** (`routers/forge.py`): the router schedules a mirror refresh only when `settings.mirror_path` and `push.repo` are set. It stores the Push first; failed work admission returns retryable 503 without removing the Fact.
  Redelivery retries admission. Only the same retained Push identity coalesces while active; distinct Pushes retain distinct observation work. Failed work releases its identity for retry.
  A repository observation lock spans refresh through persistence. The mirror lock keeps fetched notes stable during observation collection, then releases before PostgreSQL writes. The scoped Attribution result remains a log line, never stored (ADR 0001).
  The supervisor retains two query/report slots without a queue, two mirror slots, and at most 16 queued mirror jobs per API process. Read jobs have 30 seconds; mirror jobs have 120 seconds including queue time. Timeout/cancellation stops the process group, escalates after one second, and reaps it before releasing capacity.
  Queued requests stop at 1 MiB, results at 64 MiB, and diagnostics at 64 KiB. Mirror admission requires 1 GiB free on its volume; this reserve doesn't replace a host storage quota. Repository rename stores an immutable Fact without Git work.
  Complete snapshot identity evidence qualifies the retained Push and fetch location before Git work. Context failure or a known competing location declines refresh while preserving the Push.
- The router injects clone-URL confinement from settings. `sediment_derive` never reads it.
- **Operational report routes require explicit bounds.** `/v1/reports/model-outcomes` and `/v1/reports/accepted-work-lifecycle` accept a half-open cohort of at most 31 days plus `as_of`. Decision attachment requires all visible organization-wide call identities through `as_of`, capped at 50,000 rows independently of the cohort; overflow returns 409 without partial attachment. Report projections omit input/raw and separately refuse output rows over 64 MiB or output populations over 256 MiB before decoding. Identity reads enforce the per-row ceiling; row limits don't bound database scan work. They fix the cohort cap at 50,000 Inference calls, await bounded child assembly, cap serialized responses at 64 MiB, return version 1 scope-and-report envelopes, and persist nothing.

## Operator CLI (`cli/sediment_cli/cli.py`)

- Safety posture: bulk `quarantine-inference-calls` **dry-runs by default** and
  writes only with `--apply`. It refuses a filterless run unless you pass
  `--all`. Per-Fact verbs act immediately. Every verb requires `--reason`.
- Output vocabulary is normative. Say "quarantined" and "released", never
  "deleted". Advertise reversibility, and print `quarantine_revision` where it is
  relevant. On an expected validation, filesystem, or database error, write
  `error: …` to stderr and exit 1. Apply that boundary before opening a local
  store and around every pre-argparse dispatch.
- Styling lives in `cli/sediment_cli/ui.py` and applies to a TTY only. Pipes,
  `NO_COLOR`, and `TERM=dumb` all get byte-identical plain text
  (`cli/tests/test_cli_ui.py`). Help names the full installed command path.
  Direct script and fleet invocations of `attribution.py` use plain output.
- `main()` defers the `sediment_api/config.py` import, so `--help` works
  without a valid `SEDIMENT_ORG_ID`. `export dpo` defaults to `dpo_human`;
  `export sft` and `export diff-sft` default to `sft_curated`. Operators must
  pass `--recipe dpo_outcome` or `--recipe sft_verified` to select CI-derived
  evidence. `export recovery` accepts only `recovery_ci`. `export rlvr` requires
  `SEDIMENT_MIRROR_PATH` and one explicit `--target` choice: `sediment`,
  `swe-bench`, or `nemo-gym`. It surfaces the JSONL no-truncate guard. Only the
  Sediment target can write the experimental environment manifest. Canonical Derivation and direct/offline training commands keep private bundle contexts open through projection and publication; hashing uses fixed buffers, and sampling reads only requested records.
- `export sft`, `export dpo`, and `export rlvr` accept exact `--profile` names. RLVR profiles consume closed version-1 JSON `--consumer-config`; no environment setting substitutes missing task or response configuration. `scripts/gen_compatibility_docs.py --check` verifies the support reference. [Consumer exports](../exports/consumer-compatibility.md) define optional installation and private destination behavior.
- The remote verbs use `sediment_cli/client.py` and never open the Fact store, except for direct `facts` storage. `demo` verifies its fixed Session; org-wide totals are display only. `login` stores operator authority; `login --capture` verifies ingest-only authority and stores private enrollment separately. Input uses loopback `server.env`, `--with-token`, or a prompt.
  The client accepts HTTPS roots and HTTP only for `localhost`, `127.0.0.0/8`, or `[::1]`. It rejects credentials, URL control characters, queries, fragments, and non-root paths before token
  input and at the final HTTP seam. Capture endpoints also allow `/v1/logs`.
  Remote and doctor requests reject redirects without forwarding the bearer token.
- `evidence inventory|inspect|fetch` use operator HTTP access with a 40-second read timeout and 1 MiB streamed response cap. Fetch validates all selected parts before atomic, no-clobber, mode-`0600` publication; stdout carries only path and count. [Continue a task with captured evidence](../operate/resume-with-evidence.md) covers selection, limits, and controlled restart.
- Before migration, local `facts` counts physically present Fact tables; absent tables display `unavailable`. Database errors remain errors. Remote responses use the same absent-field display.
- Capture installers consume verified ingest enrollment only; explicit `SEDIMENT_INGEST_TOKEN` overrides need a live authority check. Old unclassified profiles require enrollment; no operator fallback exists. `install --codex-profile NAME` writes a private telemetry profile and preserves unrelated settings. `--transcripts` separately enables pi content; `--no-env` leaves process configuration to its owner.
- Capture clients support macOS and Linux; the console entry in `cli/sediment_cli/__init__.py` routes Windows installation to the stdlib installer refusal before importing POSIX operators. `uninstall --agents` strips verified managed Codex telemetry from each profile; skipped files produce diagnostics and nonzero status. [Uninstall capture](../capture/local-capture.md#uninstall-capture) covers cleanup.
- `doctor REPO --agent cursor|codex|pi --session-id ID` requires the selected Session's Developer decision and local HEAD note. `--transcripts` adds Edit observation proof; `--inference-calls` adds Session-level call proof. Unsupported, missing, incomplete, and unreadable requirements fail. Default doctor checks configuration, including endpoint validation, without claiming delivered evidence.
- `sediment server` manages PostgreSQL 17 under `~/.sediment/server` or `--root`: `postgres/` holds data, a versioned `postgresql-*` directory holds binaries, `postgres.log` holds database logs, and `mirror/` holds repositories. First use downloads a checksum-pinned archive from theseus-rs; macOS uses maintained Homebrew OpenSSL libraries. `server.lock` confines ownership to one process per root. The command waits for database readiness, provisions roles, and derives the runtime URL before starting the foreground API. Ctrl+C stops the API and its owned database; restart preserves data. `SEDIMENT_BOOTSTRAP_DATABASE_URL` selects external provisioning without managed database startup or shutdown.
  Mode-`0600` `server.env` preserves database/API secrets without printing them. Bootstrap, migrator, and operator database credentials never reach the API. Bare loopback login enrolls both local API authorities; explicit `--with-token` enrolls only the selected authority.
- `report <name>` (eight read-only reports) and `mirror-gc` dispatch before
  argparse and never construct `Settings`. The CLI forwards argv verbatim to
  the module in `sediment_api/reports/` or `sediment_api/mirror_gc.py`. `--org`
  runs through `normalize_org_id` and defaults from `SEDIMENT_ORG_ID`. Storage
  defaults from `SEDIMENT_DATABASE_URL` and `SEDIMENT_MIRROR_PATH`. Each one-shot command owns and disposes one engine. **An empty
  result prints "no data" and exits 0. It is not an error.** Every report takes
  `--json`; the model report renders Attribution share and alerts before its per-model rows.
  Its `--since-days` path uses `services/operational_reports.py` for bounded assembly; omitting the flag retains the explicit offline all-history path. The lifecycle CLI calls the service without an `OperationalReportScope`; other delivery boundaries can pass an explicit scope.
- Label-confidence inspection accepts `--recipe`; dataset diagnostics accepts
  `--dpo-recipe` and `--sft-recipe`. Reports stratify by recipe version and
  label or eligibility source, never pooling heterogeneous recipe evidence.
- `quarantine-inference-calls --provider` accepts the closed `GatewayProvider`
  vocabulary; `--between` bounds must be timezone-aware. Mode flags fail
  closed: bootstrap, sensitivity, and latency options reject cross-mode use.
- **`mirror-gc --apply` deletes mirror directories irreversibly.** It dry-runs by default. Retention groups by stable identity; output shows identity beside its representative label. An orphan identified mirror has no guessed label; legacy directories remain separate.
- The generated `docs/reference/cli.md` lists every per-report flag. `attribution_share_report --target-margin` plans `min_cases_for_decline_verdict` with the `MIN_DRIFT_CASES` planner.
  `report merge-retention --rows-out FILE` writes canonical version 3 `MergeRetention` rows without changing aggregate stdout. The command reports the destination and row count on stderr.

## Scripts (`scripts/`)

- The read-only reports and `mirror_gc.py` live in `sediment_api`, behind the CLI. Their `scripts/*.py` paths are import shims.
- Three stay script-only by design, because each takes a positional input
  rather than the store. `calibration_check.py` requires recipe and source
  metadata on each human label and reports Brier, ECE, AUROC, and bucket
  inversions per stratum. `corpus_sizing.py` does
  store-free planning math. `threshold_drift_report.py` reads a labelled-case
  manifest, and `--check-drift` exits 0 whatever the verdict.
- `sediment_attribution.py` is the stdlib-only notes client; `install --fleet`
  mutates developer git config — an operator action, never a test fixture.
- `sediment_transcript.py` is the stdlib checkout shim for packaged `sediment transcript`.
- `repair-notes` fixes a diverged notes ref. It pushes, so keep it out
  of the unattended allowlist.
- `doctor [REPO ...]` reports and never repairs. It exits 1 on any FAIL,
  and it is safe to run unattended. Only `--fetch` writes, and it writes one
  tracking ref. An absent agent reports `info`, never FAIL — and so does a
  check this build cannot make: an installed CLI carries no `shims/`, so the
  pi registration is uncheckable rather than broken.
- The stamper's `config.json` (`auto_install_remotes`) is owner policy.
  It makes `mark` install git hooks into matching repos, so review its prefixes
  the way you would review an ACL.
- `add_spdx.py` ROOTS covers `packages`, `apps`, `scripts`, `sim`, and
  `litellm`. CI runs `--check` over all of them.
- `check_docs.py` gates doc freshness (`docs/agents/doc-sync.md`); `ci_preflight.py` selects conservative prose and shim checks and rejects stale source reviews before automatic artifact builds (`docs/onboarding.md`, `docs/operate/security.md`). Its `shims` command defaults to testing when the PR diff is unavailable; the stable workflow result rejects incomplete selected work.
- `release_rehearsal.py` verifies source, actions, wheels, and installed commands
  outside the checkout. `scratch_database` overrides the administrative database selector; `_run_installed_worker` owns bounded process-group cleanup. The caller's database receives no corpus or migration. Every stage must reconcile before success; [Rehearse a release](../operate/rehearse-release.md) states the corpus and runtime limits.
- `dump_openapi.py` regenerates `openapi.yaml` and injects the auth schemes. `gen_api_docs.py` renders `docs/reference/api.md` from that spec plus its own
  map of response shapes and status codes. `gen_cli_docs.py` renders
  `docs/reference/cli.md` from every `build_parser`, so **declare flags
  there, never inside `main`**, or the page documents them as absent.
  `gen_schema_docs.py` renders the schema reference, Draft 2020-12 schemas, and
  catalog from one registry. It fails on undocumented fields; CI checks
  freshness and every versioned schema under `schemas/` at the compatibility base, including inactive versions absent from its catalog. Invalid Git bases fail; a valid revision before schema publication has an empty inventory. Published files remain immutable, and the catalog indexes active contracts. Missing map entries fail.
- `second_review.py` runs the cross-model closeout pass from
  `docs/agents/review.md` in an empty temporary workspace. It calls the codex
  CLI and spends codex credits, so do not run it unattended. Missing or malformed final review results exit 2 even when the CLI exits zero; a completed pass can still contain findings.
- `glossary_gap.py` flags classes with no CONTEXT.md entry. Advisory.

## Deployment glue

- `litellm/sediment_callback.py` is a forwarder: env vars, the
  `SEDIMENT_CAPTURE_DIR` debug dump, SLO coercion + the fallback payload
  (stamped with `metadata.requester_metadata`, lifted
  `requester_custom_headers`, and `end_user` — every spot the resolver reads, so
  identity survives the no-SLO path), the POST, and one broad try/except. It
  parses nothing. Put a new identity path in `session_identity.py`, never in
  this file. An inference call without a Session identifier still POSTs, and
  the server skips it observably.
- `SEDIMENT_GITHUB_HOST` defaults to `github.com` and supplies the trusted forge namespace. Request headers and clone URLs never supply it; representing another host does not certify its capture adapter.
- Environment prefixes are `SEDIMENT_`, `SEDIMENT_LABEL_CONFIDENCE_*`, `SEDIMENT_OPENENV_*`, `SEDIMENT_NEMO_GYM_*`, and `SEDIMENT_CAPTURE_DIR`. Settings ignore extra values.
  Compose scopes database, API, and provider values to their consumers. The optional gateway entrypoint names a missing Anthropic or LiteLLM master key;
  API-only deployment needs neither key. Misspelled variables are silently ignored, so grep before you rename one.
- Docker uses two `uv sync` layers, a non-root user, and pre-creates `/data/{mirror,export}`. A new workspace member needs its own `COPY …/pyproject.toml` line (AGENTS.md's checklist).

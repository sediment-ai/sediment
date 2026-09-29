# Changelog

## Unreleased

### Capture

- Restructure the Capture docs. Capture pi work moves out of Agent
  integrations into its own page, like the other agents. Agent integrations
  becomes a comparison page that also shows Inference-call support. To opt in
  to transcript capture, rerun `sediment install` with `--transcripts`; the
  generated environment already holds the endpoint and token. Configure local
  capture, Roll out managed capture, and the agent guides drop repeated steps,
  and managed capture puts the GitHub webhooks first.
- Add opt-in context pruning (ADR 0028). With
  `SEDIMENT_CONTEXT_PRUNE=supersede`, the LiteLLM capture callback replaces
  superseded tool output with a one-line stub before each model call: a file
  read that a later read, edit, or write of the file replaced, and a command
  run that a later identical run replaced. Captured Inference calls record the
  pruned request and carry a count-only `sediment_context` report on `raw`.
  OpenAI chat requests pass through unpruned, and the report counts them as
  skipped, because their tool results carry no error flag. The rule lives in erode, an MIT-licensed, stdlib-only package in
  `contrib/erode` that is meant to move to its own repository. The bundled
  gateway mounts it and passes the variable through. For other gateways, or
  agents that call a provider directly, run `erode proxy`. See
  [Prune superseded tool output](docs/capture/managed-capture.md#prune-superseded-tool-output).
- Add the recorder and instance draw for evaluation E1 of the context-pruning
  spec. `scripts/erode_record.py` runs the erode proxy with pruning off and
  saves each `POST /v1/messages` body, in send order, as private numbered files.
  `scripts/swe_bench_subset.py` draws E2's 50 SWE-bench Verified instances and
  E1's candidates outside them; `scripts/erode_eval_instances.json` holds the
  result.
- Add Codex CLI support to erode. The proxy prunes `POST /v1/responses`, and a
  Responses adapter parses Codex CLI 0.158.0's JavaScript `exec` calls with a
  strict grammar: it recognizes `cat`, `nl -ba`, `head`, `tail`, and `sed -n`
  reads and `apply_patch` edits, and stubs individual results inside a bundled
  output. Any other call shape passes through unchanged. Three recorded Codex
  Sessions are the test fixtures. See
  [Use erode with Codex CLI](contrib/erode/README.md#use-erode-with-codex-cli).

### Command line

- Change `sediment server` against an external database: it migrates at
  start instead of provisioning. Set `SEDIMENT_MIGRATOR_DATABASE_URL` and
  `SEDIMENT_DATABASE_URL`; each start runs the migrate step as the migrator,
  removes the migrator credential from its environment, and serves as the
  runtime role, as `coder server` does. The server refuses
  `SEDIMENT_BOOTSTRAP_DATABASE_URL`, so the administrator credential never
  reaches it. To upgrade a deployment that starts the server with the
  bootstrap URL, run `sediment db provision` once with that URL and the three
  role passwords from `server.env`, then replace the bootstrap URL with the
  two role URLs. The server without a database URL keeps managing and
  provisioning its private PostgreSQL cluster
  ([ADR 0027](docs/adr/0027-postgresql-without-superuser.md)).
- Wait up to two minutes for the migration lock, then fail with a
  diagnostic. `sediment db upgrade` and replicas starting together used to
  fail at once when another process held it.
- Add `SEDIMENT_MIGRATOR_ROLE`, `SEDIMENT_RUNTIME_ROLE`, and
  `SEDIMENT_OPERATOR_ROLE` to name the three database roles. They default to
  `sediment_migrator`, `sediment_runtime`, and `sediment_operator`. Each
  database URL's user must match its role, and every check uses the
  configured names, so two deployments can share one PostgreSQL instance
  without sharing roles. `sediment db upgrade` reads
  `SEDIMENT_MIGRATOR_DATABASE_URL` before `SEDIMENT_DATABASE_URL`.
- Add `sediment db provision --print-sql`, which prints the SQL that a
  database administrator runs to create the roles and grant database access,
  for the configured role names and database, without connecting. Provisioning
  runs the same statements, and `sediment db check` verifies their result.
- Let `sediment db provision` run as an administrator that isn't a
  superuser: one with `CREATEROLE` that owns the dedicated database, as on
  managed PostgreSQL services. Capability checks replace the superuser
  requirement and report every missing capability before the first change.
  An existing Sediment role that doesn't grant the administrator the `ADMIN`
  option fails with both fixes: grant `ADMIN` to this administrator, or switch
  to administrator-provisioned roles. Provisioning names only the role
  attributes that the administrator may set, and revokes each membership from
  its grantor, so a revoke never skips a grant silently. Only a superuser
  adopts tables that predate the role model
  ([ADR 0027](docs/adr/0027-postgresql-without-superuser.md)). No managed
  service is qualified yet.
- Extend `sediment db upgrade`. Connected as `sediment_migrator`, it verifies
  columns after Alembic, applies the owner's table, column, and sequence
  grants, and validates the database, its `public` schema, and all three
  roles, all under the migration lock. A validation failure names every
  failed check and its fix. `sediment db provision` runs the same step
  through the migrator credential instead of granting as the administrator.
  Another identity can still upgrade a single-owner database that grants no
  Sediment role access, and is refused on a provisioned one
  ([ADR 0027](docs/adr/0027-postgresql-without-superuser.md)).
- Add `sediment db check`, a read-only command that reports every failed
  database role and grant check at once, each with the statement that fixes
  it. It checks the migrator role, the dedicated database and its `public`
  schema, and the unchanged runtime and operator policies. Connected as an
  administrator, it also reports what provisioning needs from that
  administrator. Privilege diagnostics from provisioning and API startup now
  name the role, the privilege, and the fix
  ([ADR 0027](docs/adr/0027-postgresql-without-superuser.md)).
- Remove `sediment install --gateway-url` and `--gateway-key`. The operator
  distributes gateway routing and client credentials through managed settings
  or MDM; developers no longer receive a gateway key. Reinstalling drops
  `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and `SEDIMENT_GATEWAY_KEY` from
  the generated environment files. See
  [Distribute gateway routing](docs/capture/managed-capture.md#distribute-gateway-routing).
- Fix the version-mismatch warning so that it prints the command that
  installs the server's exact release instead of `uv tool upgrade
  sediment-cli`, which kept a pinned client unchanged and could move an
  unpinned client past an older server. The command handles upgrades,
  downgrades, and release candidates. `sediment doctor` uses the same command.
  A server version that isn't `X.Y.Z` or `X.Y.ZrcN` produces a fixed warning
  that doesn't repeat the server's text, and an unreadable `/v1/me` response
  stays silent.

### Deployment

- Document PostgreSQL without a superuser. Deploy Sediment on your own host
  replaces its superuser requirement with two ways to create the database
  roles: `sediment db provision` with an administrator that owns the
  database, or SQL from `sediment db provision --print-sql` that a database
  administrator runs. The server's environment holds the migrator and runtime
  URLs, never the administrator's. Deploy Sediment on EC2 provisions once with
  the PostgreSQL superuser. Maintain a deployment covers moving an existing
  deployment off the bootstrap URL, password rotation in both modes, `pg_dump`
  as the migrator, and a restore test into a copy with its own roles. No
  managed PostgreSQL service is qualified yet
  ([ADR 0027](docs/adr/0027-postgresql-without-superuser.md)).
- Restructure the Operate docs into one path: deploy on EC2 or on your own
  host, enroll your team, then measure agent work. Deploy Sediment becomes
  Deploy Sediment on your own host. Enroll your team replaces the Cursor, pi,
  and Codex pilot, creates capture tokens, and covers Claude Code. Maintain a
  deployment collects upgrades, backups, credential rotation, quarantine,
  troubleshooting, and teardown. Secure a deployment collects stored data,
  credential authority, network exposure, and release checks. Retrieval setup
  lives only in Continue a task with captured evidence, and Measure agent work
  absorbs the lifecycle report. Remove Validate a deployment and Review release
  verification. Move Profile reports and Derivations to CONTRIBUTING.md as a
  maintainer procedure.

### Contributor checks

- Generate CLI defaults and lookup indexes, API parameter constraints and
  nested request types, and links to canonical schemas. Correct reference
  descriptions of JSON values, capture timestamps, CI run identity, and
  Quarantine revisions. The optional reference hook also refreshes consumer
  compatibility when profile sources change.
- Test the pi shim against pi-coding-agent 0.87.1. Align the continuation
  comparison's exact version check and task-selection tests with the lockfile.
- Centralize repository agent skills in `.skills/`, including release preparation
  and publication. Route every skill from `AGENTS.md` and remove the repository
  Claude Code settings so agents can read the same procedures across harnesses.
  Require verification of the README PyPI badge during release closeout.
- Run the complete installed release rehearsal once per pull request instead of
  repeating it in the serial test pass. The rehearsal script checks the
  installed module locations, identities, and hashes that the repeated test
  checked, so tag releases enforce them too. Worker cleanup tests use a
  controlled process tree plus one installed-server interruption, and runtime
  failure tests share one validated build.
- Give packagers 14 days to ship an upstream runtime patch before the security
  gate fails. The OpenSSL 3.5.9, 3.6.5, and 4.0.3 releases on 2026-09-29 failed
  every client and gateway scan while no Wolfi, `cryptography`, or uv build
  carried them. The window starts at the first patch that is newer than the
  installed runtime, and the runtime evidence records each pending patch and its
  deadline. Fixes that a vulnerability scanner reports still fail at once.

## 0.3.0 — 2026-09-27

### Deployment

- Move the contributor Compose runbook from `docker/README.md` to
  [Rehearse the single-host Compose deployment](docs/operate/rehearse-compose.md)
  and publish it with the operator docs.
- Install EC2 deployments from a published Sediment package version, with the
  published PostgreSQL and Traefik images. Remove the source checkout and local
  image builds from the operator procedure. Document systemd startup,
  persistent data, and package upgrades.
- Stop a failed command block in Deploy Sediment, the Quickstart, or the
  Compose rehearsal without closing your terminal. The blocks chain their
  commands instead of calling `exit 1`.
- Publish the EC2 and Traefik setup as its own page, Deploy Sediment on EC2
  with Traefik. Deploy Sediment keeps the existing-host installation and the
  enrollment, upgrade, and data-handling procedures that both pages share.
- Add `create_deploy_env.py --domain` for a single-host pilot with PostgreSQL,
  the API, LiteLLM, and Traefik HTTPS. Generate private credentials, prompt for
  provider access, and configure one hostname with automatic certificate renewal.
- Wait for gateway readiness during Compose startup. Return 401 for missing or
  incorrect gateway keys without importing the removed Prisma dependency.
- Document checkout permissions required by the private credential generator
  on Ubuntu.
- Exercise both gateway protocols, streaming capture, HTTPS routing, credential
  boundaries, request limits, and persistence in an opt-in Compose acceptance test.

### Command line

- Keep concurrent enqueues to the transport buffer on macOS from declining
  with `storage_unavailable`. On APFS, racing opens that create the shared
  enqueue lock could fail. Callers that allow a direct fallback then sent the
  payload without buffering it. Enqueue creates the lock exclusively and opens
  the existing file when another publisher creates it first.
- Show the compact Sediment logo with `sediment --version` on supported
  interactive terminals, in the terminal's own text color, followed by the
  version number without a name or website footer. Preserve version-only output for pipes,
  `NO_COLOR`, `TERM=dumb`, narrow terminals, and unsupported text encodings.
- Read a missing `--mirror-path` without creating it. Reports and exports treat
  every mirror there as absent instead of writing lock files under that path,
  or failing when the path can't be created.

### Contributor checks

- Skip the artifact scans for pull requests that change only Markdown under
  `docker/` or `security/`, such as the gateway README. No image or scanner
  verdict depends on Markdown.
- Route agents through `AGENTS.md` alone. Claude Code reads it directly, so
  remove `CLAUDE.md` and the `SessionStart` hook that repeated it, and stop
  requiring `CLAUDE.md` in the docs check. Prune the router and correct its
  package map, ingest routes, and tooling notes.
- Test the pi shim against pi-coding-agent 0.86.1 and read its transcript-based
  tool declarations in the native test harnesses. Align the continuation
  comparison's version check with its locked evaluation image.
- Run the Python suite on four pytest-xdist workers. The PostgreSQL role
  contract runs alone first, and the deadline-bound installed rehearsals run
  alone last. Start the local-server platform checks beside the suite instead
  of after it. Keep the Intel macOS runner's libraries so that the job no
  longer compiles OpenSSL from source.
- Bind security dispositions to a review digest of the image build inputs
  instead of all production source, so that application changes no longer
  invalidate every review. Scan artifacts on pull requests only when a scan
  input changes; `main`, a daily run, and releases always scan, and a failure on
  `main` opens or updates one tracking issue.
- Run the required workflows on merge-queue groups.
- Update setup-uv to 10.2.0, uv to 0.12.19, and TruffleHog to 3.97.9. Keep the
  TruffleHog action, executable, and complete-tree scanner image pins aligned.

### Installation

- Ship the MIT-licensed pi extension in the CLI wheel and source distribution.
  `sediment install` registers it without a checkout or npm dependencies.
  With pi present, `doctor` fails when the extension is missing or unregistered.
  `sediment uninstall --agents` removes the registration.
- Load managed capture settings inside Cursor hooks so desktop Agent writes
  don't depend on inherited shell variables. Preserve process-owned settings
  with `install --no-env`. Doctor checks the managed file; capture failures
  retain Session markers and report content-free diagnostics.
- Refresh the Python 3.12.14 and PostgreSQL 17.11 container image digests.
- Use the maintained Hugging Face Hub 2 release in the gateway. Declare and test
  Tokenizers compatibility with that exact version without changing its code.
- Backport CPython's archive hard-link fix to the gateway's released Python
  runtime. Guard the patch against vendor source changes and retain its exact
  file identity in security evidence.
- Keep client installations on the reviewed SQLAlchemy 2.0 series. Pin the
  gateway's reviewed OpenSSL and supporting packages so rolling repository
  updates don't introduce an OpenSSL configuration-file conflict.
- Update the supplied gateway to LiteLLM 1.103.0, the release that the
  latest-stable security gate requires. The guarded vendor patch covers the
  Prisma SQLSTATE lookup that 1.103.0 adds and returns `None` from it when
  Prisma is absent. Remove the Vertex speech SDK that this release adds, which
  the gateway doesn't use. Drop the MCP 1.30.0 override: the vendor image ships
  the MCP 2.2.0 release that LiteLLM 1.103.0 requires.

- Wait for API health in Docker deployment instructions. Support separate
  Compose project image tags and loopback ports for local pilot rehearsals.
  Include operator volumes in the deployment teardown command.
- Generate named pilot capture credentials with repeatable `--ingest-client`
  options. Capture-only installation points developers to ingest enrollment.
- Let deployment smoke checks use the generated operator credential when the
  legacy bearer token is unset.
- Verify capture-only logins in `sediment doctor` without requiring an operator
  credential. Use the doctor user agent when verifying sourced ingest credentials
  through ingress filters. Operator containers no longer rerun role provisioning.
- Add an opt-in Docker rehearsal covering fresh installation, enrollment,
  synthetic capture, duplicate delivery, restart persistence, and cleanup.
- Discover Homebrew's PostgreSQL client library in release rehearsal and test
  setup, matching local server startup without an extra PATH adjustment.

## 0.2.0 — 2026-09-23

### Contributor checks

- Exclude HTTP(S) URLs from local documentation-path checks. Upstream evidence
  links no longer fail as missing Sediment files; local paths remain checked.
- Route changes to the root `README.md` through the existing prose checks in
  continuous integration (CI). Mixed changes, executable files, symlinks,
  deletions, renames, manual runs, and unavailable Git history retain full
  validation.

### Operational evidence

- Stream keyword retrieval one call at a time for fixed and granted Sessions.
  Raise keyword source capacity to 64 MiB and 16,384 parts with an 8 MiB row
  bound and a separate 32 MiB candidate-state budget. Completed answers retain
  exact version-1 ranking, references, counts, and Quarantine behavior. State
  overflow refuses the complete request; factual exact-read limits stay independent.
  Reuse bounded compiled encoding schemas while validating every value; pi
  accepts complete scans through 16,384 parts and preserves the closed
  state-capacity refusal without echoing server content.
- Declare the pilot target as 100 Sessions per week, 100 calls per Session, and
  24 weeks of history. Add resource-free workload planning with explicit limits
  and concurrent exact retrieval to the capture/report/export rehearsal.
  A declared profile does not establish passing capacity.
- Defer migration-only imports during evidence reads. Reject batches of earlier
  Pushes with a native Git ancestry proof before exact owner checks; uncertain
  and divergent batches retain existing range checks.
- Use indexed organization-wide identifier witnesses for scoped model and
  lifecycle report Decision attachment. Historical ambiguity and Quarantine
  remain complete; more than 30,000 requested identifiers refuses the report.
- Add a PostgreSQL storage calibration with repeated and varied message content,
  measured native compression, separate capacity refusals, and verified native
  backup/restore. Distinct Facts and the physical production schema stay intact.
- Let evidence use both existing query/report worker slots. The total remains
  two active reads per API process, with immediate capacity refusal for a third.
  Reports no longer have a reserved slot. Deadlines, cleanup, source limits,
  authority, and Quarantine checks remain unchanged.
- Add grant-scoped factual inventory, manifest, and exact part reads with native
  pi tools. Consumers can inspect authorized evidence independently of keyword
  selection, including uncommitted requirements, failed attempts, and readable
  reasoning. Exact fetch preserves occurrence identity and checks Quarantine
  on each request while avoiding unselected calls and message sides.
- Load report and forge handlers only for their worker operations. Add a
  reproducible agent-evidence benchmark with separate successful latency,
  capacity refusals, database plans, memory, and concurrent capture checks.

- Add bounded candidate discovery across an operator-authorized set of up to
  32 Sessions, with exact previews and optional repository-qualified commit
  witnesses. Pi can discover a source and retrieve its context under the same
  restricted credential. Aggregate source limits, counted output omissions,
  and fresh authorization and Quarantine checks preserve the evidence boundary.
  The fixed-Session tool remains the default; no learned selector is added.
- Add pi's opt-in `sediment_retrieve_context` tool and `POST /query/context` for
  keyword-selected exact evidence from one configured previous Session. Restrict
  retrieval credentials to that Session, preserve Quarantine and exact references,
  and bound complete source reads and whole-part responses. Reads persist nothing
  and don't change Attribution or training export semantics.
- Add a private, isolated pi continuation comparison with three repeated arms,
  enforced transport budgets, captured source verification, and independent
  final checks. Report live outcomes separately from automated contract tests.
- Add `--task env-profile` for a separate environment-profile parsing comparison.
  Freeze task selection and require it to match the source. Preserve the default
  invoice fixture, generation settings, attempt budgets, and acceptance rules.
- Add `--task shipment-totals` for instructed retrieval before task work. Its
  post-run check requires successful source retrieval before the first edit,
  write, or shell invocation. This protocol measures instructed retrieval and
  application, not an autonomous decision to retrieve.
- Preserve empty assistant content during pinned pi evaluation replay so the
  strict captured-prefix check can verify the single-user, text-only baseline.
- Add a native tool preflight with three fresh read, edit, and shell cycles on an
  unrelated fixture. An optional fourth cycle verifies retrieval against an
  accepted source Session, exact references, and subsequent result consumption.
  Passing this check doesn't establish continuation benefit.
- Reserve the ingest client name `retrieval`. Rename an existing client with that
  name before upgrading. Retrieval configuration validates even in development
  mode; changing the source Session requires rotating its token.
- Scope commit Attribution to the requested commit before loading call content.
  Stream Push metadata, retain the earliest eligible owner, and read only its
  candidate windows. Compact repeated repository evidence into original source
  witnesses while preserving names, ambiguity, historical bounds, and Quarantine.
  Captured observations supply note Sessions even when the eligible map is empty;
  mutable Git notes cannot alter a historical commit investigation.
- Investigate a commit without loading unrelated Inference call inputs and raw
  payloads. Supporting reads select exact-commit observations and CI outcomes,
  and decisions from the inferred calls' Sessions. Decision attachment preserves
  organization-wide call ambiguity, Quarantine, and historical boundaries.
  Indexed call-identifier lookup preserves ambiguity without parsing unrelated
  outputs. It retains at most two visible Fact witnesses per requested identifier;
  Attribution keeps its separate payload limits. A physical identifier table and
  parent count migrate from retained Facts, requiring a maintenance window.
  Canonical Fact schemas and complete bundle/report identity reads stay unchanged.
- Inventory a Session, inspect captured Inference-call structure, and fetch up to
  32 exact parts through operator-authenticated API routes and `sediment evidence`.
  Reads enforce fixed source and response limits and recheck Quarantine. The CLI
  publishes a private packet without overwriting an existing destination.
- Document controlled agent continuation with preserved workspace state. Evidence
  reads add no model dependency, persisted domain state, or training interpretation.

### Dependency maintenance

- Remove Expat from the API image. Its only consumer was `git-http-push`,
  which mirror fetches never use; Python's `pyexpat` bundles its own copy. The
  image label `io.sediment.removed-packages` records both removals, so the
  disclosed Bookworm Expat advisory no longer needs a disposition.
- Update the supplied gateway to LiteLLM 1.102.1 and verify the guarded vendor
  patch against its release source.
- Update the supplied gateway to fastapi-sso 0.23.0 to follow upstream's
  latest-release security support policy.

## 0.1.0 — 2026-09-21

### Installation

- Install the PyPI CLI and maintained host libraries through
  `curl -fsSL https://sediment.so/install.sh | sh`, then start with
  `sediment server`. macOS database commands discover Homebrew's keg-only
  PostgreSQL client library without a shell PATH change.
- Use `--capture-only` to install the CLI for an existing deployment without
  installing local-server libraries.

### Local server

- Start a local API and PostgreSQL database with `sediment server`. The command
  downloads verified PostgreSQL binaries, provisions database roles, and keeps
  data and credentials under `~/.sediment/server`. Ctrl+C stops both processes;
  a later start preserves the data. An explicit bootstrap database URL retains
  external database support.

### Dependency maintenance

- Update Python dependencies, consumer compatibility pins, Node 24 typings, uv,
  and GitHub Actions. Preserve Python 3.12, PostgreSQL 17, Node 24, and the
  existing Ruff rule selection.

### Initial public source

- Capture inference calls, Developer decisions, Edit observations, Retry linkages,
  pushes, repository changes, and CI outcomes as immutable Facts.
- Store Facts in PostgreSQL and derive Attribution, accepted-work lifecycle,
  model outcomes, Attributed completions, and Rollouts from retained evidence.
- Export training rows for DPO, SFT, diff-SFT, Recovery, and RLVR with versioned
  Evidence recipes, source metadata, and deterministic validation.
- Provide source installation, opt-in agent capture, self-hosted deployment,
  bounded Derivation execution, deployment verification, public vulnerability
  reporting, and contributor verification procedures.

- Provide a restricted Anthropic gateway based on LiteLLM 1.102.0. The supplied
  image excludes database clients and PgBouncer.

See [Quickstart](docs/quickstart.md) to install and start Sediment and
[Contributing](CONTRIBUTING.md) for development and reporting guidance.

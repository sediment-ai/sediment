# Changelog

## Unreleased

### Contributor checks

- Exclude HTTP(S) URLs from local documentation-path checks. Upstream evidence
  links no longer fail as missing Sediment files; local paths remain checked.
- Route changes to the root `README.md` through the existing prose checks in
  continuous integration (CI). Mixed changes, executable files, symlinks,
  deletions, renames, manual runs, and unavailable Git history retain full
  validation.

### Operational evidence

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

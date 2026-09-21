# Changelog

## Unreleased

### Operational evidence

- Add pi's opt-in `sediment_retrieve_context` tool and `POST /query/context` for
  keyword-selected exact evidence from one configured previous Session. Restrict
  retrieval credentials to that Session, preserve Quarantine and exact references,
  and bound complete source reads and whole-part responses. Reads persist nothing
  and don't change Attribution or training export semantics.
- Reserve the ingest client name `retrieval`. Rename an existing client with that
  name before upgrading. Retrieval configuration validates even in development
  mode; changing the source Session requires rotating its token.

- Inventory a Session, inspect captured Inference-call structure, and fetch up to
  32 exact parts through operator-authenticated API routes and `sediment evidence`.
  Reads enforce fixed source and response limits and recheck Quarantine. The CLI
  publishes a private packet without overwriting an existing destination.
- Document controlled agent continuation with preserved workspace state. Evidence
  reads add no model dependency, persisted domain state, or training interpretation.

### Dependency maintenance

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

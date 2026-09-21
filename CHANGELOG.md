# Changelog

## Unreleased

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

See [Quickstart](docs/quickstart.md) to try the source checkout and
[Contributing](CONTRIBUTING.md) for development and reporting guidance.

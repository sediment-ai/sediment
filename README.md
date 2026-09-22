<p align="left">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/sediment-logo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset=".github/assets/sediment-logo-light.svg">
    <img alt="Sediment" src=".github/assets/sediment-logo-light.svg" width="320">
  </picture>
</p>

[![Continuous integration](https://github.com/sediment-ai/sediment/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/sediment-ai/sediment/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sediment-cli)](https://pypi.org/project/sediment-cli/)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](CONTRIBUTING.md)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![Follow @sedimentai on X](https://img.shields.io/badge/Follow-%40sedimentai-000000?logo=x&logoColor=white)](https://x.com/sedimentai)

[Website](https://sediment.so) |
[Documentation](https://docs.sediment.so) |
[Changelog](CHANGELOG.md)

<!-- docs-home:start -->

Sediment is an open-source, self-hosted evidence store for coding agents.

Capture model calls, code changes, developer decisions, and check results on
your infrastructure.

- [Evaluate agent work](docs/operate/measure-agent-work.md): compare models by
  recorded decisions, code retention, and automated checks.
- [Reuse context](docs/operate/resume-with-evidence.md): give agents selected
  messages, tool calls, and results from a previous Session.
- [Build training datasets](docs/exports/training-exports.md): export examples
  for fine-tuning, preference training, and reinforcement learning.

<!-- docs-home:end -->

## Get started

On macOS with Homebrew, Debian, or Ubuntu:

```sh
curl -fsSL https://sediment.so/install.sh | sh
sediment server
```

If the installer prints a PATH instruction, run it before `sediment server`.

Sediment manages a local PostgreSQL database. Follow the
[Quickstart](docs/quickstart.md) to connect an agent and verify capture.

[Integrations](docs/capture/agent-integrations.md): Claude Code, Codex, Cursor,
pi, and Copilot Chat. Available signals vary by agent.
[Deploy a shared server](docs/operate/deploy.md).

## Architecture

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/architecture-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset=".github/assets/architecture-light.svg">
    <img alt="Agent, gateway, and repository events flow into immutable Facts in PostgreSQL, then into reports, agent context, and training datasets." src=".github/assets/architecture-light.svg" width="880">
  </picture>
</p>

[Architecture and network boundaries](docs/explanation/architecture.md) ·
[Captured data and privacy](docs/explanation/how-capture-works.md)

A pi agent can call `sediment_retrieve_context` to request keyword-selected
captured parts from one authorized previous Session. An operator can also select
parts with `sediment evidence inventory`, `inspect`, and `fetch`. Both paths use
an intact workspace; Sediment doesn't restore files or infer the next task.
Available evidence depends on the capture setup. The continuation guide defines
the authority, limits, and controlled comparison for measuring task benefit.

## Development

[Contributing](CONTRIBUTING.md) ·
[Issues](https://github.com/sediment-ai/sediment/issues) ·
[Security](SECURITY.md)

## License

[AGPL-3.0](LICENSE). Harness shims under `shims/` use the
[MIT license](shims/pi/LICENSE).

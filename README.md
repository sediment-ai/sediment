<p align="left">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/sediment-logo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset=".github/assets/sediment-logo-light.svg">
    <img alt="Sediment" src=".github/assets/sediment-logo-light.svg" width="320">
  </picture>
</p>

[Website](https://sediment.so) |
[Documentation](https://docs.sediment.so) |
[Changelog](CHANGELOG.md)

<!-- docs-home:start -->

Sediment turns coding-agent activity into training data. Run it on your own
infrastructure to capture model calls, code changes, developer feedback, and
automated check results.

Use Sediment to:

- See which agent edits remain after later edits, review, and merge.
- Compare models using recorded decisions, code retention, and automated checks.
- Export data for fine-tuning, preference training, and reinforcement learning.

Read the guides to [measuring agent work](docs/operate/measure-agent-work.md)
and [training exports](docs/exports/training-exports.md) for commands and examples.

<!-- docs-home:end -->

## Get started

Install Sediment and start a local server:

```sh
curl -fsSL https://sediment.so/install.sh | sh
sediment server
```

The installer installs `sediment-cli` from PyPI and prepares the host libraries
on macOS with Homebrew, Debian, and Ubuntu. If it prints a PATH instruction,
run that instruction before `sediment server`.

The server configures PostgreSQL automatically and keeps its database,
credentials, and mirror under `~/.sediment/server`. Follow the
[Quickstart](docs/quickstart.md) to connect an agent and verify capture.

The [agent integration guide](docs/capture/agent-integrations.md) covers
Claude Code, Codex, Cursor, pi, and Copilot Chat. Available signals vary by agent.
For a shared server, follow the [deployment guide](docs/operate/deploy.md).

## Architecture

Agent hooks, model gateways, and repository webhooks send events to your
Sediment server. It stores those events as append-only Facts in PostgreSQL
and computes reports and training exports from them.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/architecture-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset=".github/assets/architecture-light.svg">
    <img alt="Sediment architecture: agent and gateway events flow into PostgreSQL on your infrastructure, then into reports and training exports." src=".github/assets/architecture-light.svg" width="880">
  </picture>
</p>

The [architecture guide](docs/explanation/architecture.md) explains the
components and network boundaries. The
[capture guide](docs/explanation/how-capture-works.md) explains what data each
integration collects, including prompts and source code.

## Development

Start with [Contributing](CONTRIBUTING.md) and
[local setup](docs/onboarding.md#local-setup) for requirements and checks.
Use [GitHub issues](https://github.com/sediment-ai/sediment/issues) for bug
reports and feature requests. Follow the [security policy](SECURITY.md) to
report vulnerabilities.

## License

[AGPL-3.0](LICENSE). Harness shims under `shims/` use the
[MIT license](shims/pi/LICENSE).

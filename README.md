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

Sediment is the open-source, self-hosted evidence store for coding agents.

Capture what coding agents did and what happened to their work. Keep the
evidence on your infrastructure. Use it to evaluate agent work, provide agents
with context, and build training datasets.

Use Sediment to:

- **Evaluate agent work.** Compare models using recorded decisions, code
  retention, and automated checks. See which accepted edits reach review and merge.
- **Reuse evidence as context.** Retrieve selected captured messages, tool calls,
  and tool results for an agent continuing a task.
- **Build training datasets.** Export evidence-backed examples for fine-tuning,
  preference training, and reinforcement learning.

Start with [measuring agent work](docs/operate/measure-agent-work.md),
[continuing a task with captured evidence](docs/operate/resume-with-evidence.md),
or [training exports](docs/exports/training-exports.md).

Capture, storage, evidence reads, and exports run on your infrastructure.
Your agent's configured model endpoint determines where inference data goes.
Installation downloads software, and optional repository mirrors contact your
configured remotes. An internal deployment needs prepared dependencies and
internal endpoints. See the [network and trust
boundaries](docs/explanation/architecture.md#network-and-trust-boundaries).

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
and reuses them for reports, selected agent context, and training exports.
Evidence reads return captured source parts; Derivations compute relationships
and outcomes without changing the Facts.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/architecture-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset=".github/assets/architecture-light.svg">
    <img alt="Sediment stores captured coding-agent evidence as immutable Facts in PostgreSQL. Teams use reports to evaluate work, agents receive selected evidence as context, and training pipelines consume exports. Sediment runs on your infrastructure." src=".github/assets/architecture-light.svg" width="880">
  </picture>
</p>

The [architecture guide](docs/explanation/architecture.md) explains the
components and network boundaries. The
[capture guide](docs/explanation/how-capture-works.md) explains what data each
integration collects, including prompts and source code.

Evidence retrieval uses the API or `sediment evidence inventory`, `inspect`, and
`fetch`. An operator selects captured parts and gives the resulting packet to an
agent. The continuation guide uses an intact workspace; Sediment doesn't restore
files or infer the next task. Available evidence depends on the capture setup.

## Development

Start with [Contributing](CONTRIBUTING.md) and
[local setup](docs/onboarding.md#local-setup) for requirements and checks.
Use [GitHub issues](https://github.com/sediment-ai/sediment/issues) for bug
reports and feature requests. Follow the [security policy](SECURITY.md) to
report vulnerabilities.

## License

[AGPL-3.0](LICENSE). Harness shims under `shims/` use the
[MIT license](shims/pi/LICENSE).

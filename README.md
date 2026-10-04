<p align="left">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/sediment-logo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset=".github/assets/sediment-logo-light.svg">
    <img alt="Sediment" src=".github/assets/sediment-logo-light.svg" width="320">
  </picture>
</p>

[![Continuous integration](https://github.com/sediment-ai/sediment/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/sediment-ai/sediment/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sediment-cli?release=0.5.0)](https://pypi.org/project/sediment-cli/)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](CONTRIBUTING.md)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![Follow @sedimentai on X](https://img.shields.io/badge/Follow-%40sedimentai-000000?logo=x&logoColor=white)](https://x.com/sedimentai)
[![Join Discord](https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white)](https://discord.gg/RbPc6PFTb)

[Website](https://sediment.so) |
[Documentation](https://docs.sediment.so) |
[Changelog](CHANGELOG.md)

<!-- docs-home:start -->

Sediment is an open-source, self-hosted evidence store for coding agents.

You can't tell which agent output developers kept, which commits it reached, or
whether it passed CI. Sediment records model calls, code changes, developer
decisions, and check results on your infrastructure, and links them.

- [Evaluate agent work](docs/operate/measure-agent-work.md): compare models by
  decisions, retained code, and CI.
- [Reuse context](docs/operate/resume-with-evidence.md): give agents evidence
  from a previous Session.
- [Build training datasets](docs/exports/training-exports.md): export examples
  for fine-tuning, preference training, and reinforcement learning.

<!-- docs-home:end -->

## Example

Comparing two models on synthetic data, 30 calls each:

```text
$ sediment report model --org acme --compare claude-sonnet-4-5 gpt-5-codex
...
compare: claude-sonnet-4-5 vs gpt-5-codex
metric           prop_a   prop_b     diff       h   ci_low  ci_high       z   p_value        sig    n_a    n_b
ci_pass_rate      87.5%    72.2%   +15.3%  +0.388    -9.3%   +39.8%    1.25    0.2121         no     24     18  small n, less reliable
attribution_rate    80.0%    60.0%   +20.0%  +0.442    -2.6%   +42.6%    1.69    0.0910         no     30     30
```

## Get started

Install the CLI from PyPI with Python 3.12, then start the local server:

```sh
pipx install sediment-cli    # or: uv tool install sediment-cli
sediment server
```

`sediment server` needs host libraries: `libpq` and `openssl@3` with Homebrew,
or `git ca-certificates libpq5 libxml2 libzstd1 liblz4-1 zlib1g` with `apt-get`
on Debian and Ubuntu. The [installer](install.sh) adds them and the CLI for you:
`curl -fsSL https://sediment.so/install.sh | sh`.

- [Quickstart](docs/quickstart.md): connect an agent and verify capture.
- [Integrations](docs/capture/agent-integrations.md): Claude Code, Codex,
  Cursor, pi, and Copilot Chat.
- Run it for a team: [EC2](docs/operate/deploy-ec2.md),
  [your own host](docs/operate/deploy.md), then
  [enroll your team](docs/operate/run-pilot.md).
- Using a coding agent? Give it `sediment guide`.

## Architecture

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/architecture-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset=".github/assets/architecture-light.svg">
    <img alt="Agent, gateway, and repository events flow into Facts in PostgreSQL, then into reports, agent context, and training datasets." src=".github/assets/architecture-light.svg" width="880">
  </picture>
</p>

[Architecture and network boundaries](docs/explanation/architecture.md) ·
[Captured data and privacy](docs/explanation/how-capture-works.md)

## Development

[Contributing](CONTRIBUTING.md) ·
[Issues](https://github.com/sediment-ai/sediment/issues) ·
[Security](SECURITY.md)

## License

Copyright (C) 2026 PAULSEN'S LLC. Sediment is licensed under
[AGPL-3.0](LICENSE). Harness shims under `shims/` use the
[MIT license](shims/pi/LICENSE).

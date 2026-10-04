# sediment-cli

Sediment is an open-source, self-hosted evidence store for coding agents.

Coding agents write a growing share of your code, but you can't tell which of
their output a developer kept, which commits it reached, or whether those
commits passed CI. Sediment records that evidence on your infrastructure: model
calls, code changes, developer decisions, and check results. It links them so
you can compare models, give agents earlier context, and build training data.

This package installs the `sediment` command: the local server, agent capture,
reports, and dataset exports.

## Install

Use Python 3.12:

```sh
pipx install sediment-cli    # or: uv tool install sediment-cli
```

Inside a virtual environment, `pip install sediment-cli` also works.

`sediment server` downloads a PostgreSQL server that loads shared libraries
from your system: `libpq` and `openssl@3` with Homebrew, or
`git ca-certificates libpq5 libxml2 libzstd1 liblz4-1 zlib1g` with `apt-get` on Debian and Ubuntu. The
[installer](https://sediment.so/install.sh) installs them for you:

```sh
curl -fsSL https://sediment.so/install.sh | sh
```

## Get started

```sh
sediment server                              # terminal 1
sediment login http://127.0.0.1:8000         # terminal 2
sediment install /path/to/your/repository
```

The [Quickstart](https://github.com/sediment-ai/sediment/blob/main/docs/quickstart.md)
verifies capture end to end. If a coding agent helps you set up Sediment, give
it the output of `sediment guide`.

## Learn more

- [Documentation](https://docs.sediment.so)
- [Agent integrations](https://github.com/sediment-ai/sediment/blob/main/docs/capture/agent-integrations.md):
  Claude Code, Codex, Cursor, pi, and Copilot Chat
- [Source and issues](https://github.com/sediment-ai/sediment)

Sediment is licensed under
[AGPL-3.0-or-later](https://github.com/sediment-ai/sediment/blob/main/LICENSE).

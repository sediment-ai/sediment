#!/usr/bin/env bash
# Cloud Agent install step. Idempotent: Cursor reruns it on every Build, and an
# agent reruns it by hand when the Build came from another configuration.
set -euo pipefail

UV_VERSION="0.12.24"

if ! command -v uv >/dev/null 2>&1 || [ "$(uv --version | cut -d' ' -f2)" != "$UV_VERSION" ]; then
  curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

uv sync --locked

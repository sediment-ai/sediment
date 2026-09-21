#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Sediment CLI installer.
#
#   curl -fsSL https://raw.githubusercontent.com/sediment-ai/sediment/main/install.sh | sh
#
# Installs the `sediment` command (PyPI distribution `sediment-cli`) with
# the best tool present: uv, then pipx, then `pip install --user`. The
# server deployment recipe stays docker compose (README) — this installs
# the client/operator CLI. Local server/database commands need a maintained
# host libpq; capture-only clients do not. This installer never changes
# system packages.
#
# This script is the one install path, and it never asks a question or
# leaves a step to the reader: a machine with none of the three
# tools — or whose pipx/python3 is older than the 3.12 the wheel requires
# — gets uv bootstrapped here rather than an error naming a command to
# run next.
#
# Flags:
#   --version X.Y.Z   pin a version (default: latest)
#   --method M        force uv|pipx|pip instead of detecting
#   --dry-run         print the install command instead of running it
#
# SEDIMENT_INSTALL_METHOD=M is the env form of --method (CI exercises every
# branch this way without three tool installations).

set -eu

VERSION=""
METHOD="${SEDIMENT_INSTALL_METHOD:-detect}"
DRY_RUN=0
BOOTSTRAP_UV=0
UV_BOOTSTRAP_URL="https://astral.sh/uv/install.sh"

usage() {
    echo "usage: install.sh [--version X.Y.Z] [--method uv|pipx|pip] [--dry-run]"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --version)
            [ $# -ge 2 ] || { echo "error: --version needs a value" >&2; exit 1; }
            VERSION="$2"; shift 2 ;;
        --method)
            [ $# -ge 2 ] || { echo "error: --method needs a value" >&2; exit 1; }
            METHOD="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown argument $1" >&2; usage >&2; exit 1 ;;
    esac
done

SPEC="sediment-cli${VERSION:+==$VERSION}"

# Both the pipx and pip branches need an interpreter the wheel actually
# supports. pip refuses a requires-python it does not satisfy ("requires a
# different Python: 3.11.x not in '>=3.12'") — a resolver error that reads
# like a missing package — and pipx builds its venv with its own
# interpreter, so `apt install pipx` on Debian 12 (python3.11) fails the
# same way. Neither tool goes looking for a newer interpreter; uv does,
# which is why it is the fallback for both.
interpreter_is_supported() {
    [ -n "${1:-}" ] || return 1
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
        2>/dev/null
}

# The interpreter pipx would build the venv with. `pipx environment` exists
# from pipx 1.2; older pipx falls back to its own shebang, which names the
# same interpreter. Unknown (neither works) reads as unsupported: skipping
# pipx still lands a working install via python3 or uv, where guessing
# wrong lands none.
pipx_python() {
    # Empty output counts as a miss, not a success: `pipx environment` can
    # exit 0 with nothing to say, and an empty interpreter would then read
    # as "unsupported" without the shebang ever being tried.
    _py=$(pipx environment --value PIPX_DEFAULT_PYTHON 2>/dev/null) || _py=""
    if [ -z "$_py" ]; then
        _py=$(sed -n '1s|^#![[:space:]]*\([^[:space:]]*\).*|\1|p' \
            "$(command -v pipx)" 2>/dev/null)
    fi
    printf '%s\n' "$_py"
}

# Install uv, then put it on this run's PATH — its installer writes a shell
# profile, which does nothing for the shell already running.
bootstrap_uv() {
    command -v curl >/dev/null 2>&1 || {
        echo "error: need curl to install uv; install uv manually:" >&2
        echo "  https://docs.astral.sh/uv/getting-started/installation/" >&2
        exit 1
    }
    echo "no uv, pipx, or Python 3.12+ found — installing uv first..."
    curl -LsSf "$UV_BOOTSTRAP_URL" | sh
    PATH="${UV_INSTALL_DIR:-$HOME/.local/bin}:$HOME/.cargo/bin:$PATH"
    export PATH
    command -v uv >/dev/null 2>&1 || {
        echo "error: uv installed but is not on PATH; open a new shell and" >&2
        echo "       re-run this script" >&2
        exit 1
    }
}

if [ "$METHOD" = "detect" ]; then
    if command -v uv >/dev/null 2>&1; then METHOD="uv"
    elif command -v pipx >/dev/null 2>&1 &&
        interpreter_is_supported "$(pipx_python)"; then METHOD="pipx"
    elif interpreter_is_supported "$(command -v python3)"; then METHOD="pip"
    else METHOD="uv"; BOOTSTRAP_UV=1
    fi
fi

case "$METHOD" in
    uv)   set -- uv tool install "$SPEC" ;;
    pipx) set -- pipx install "$SPEC" ;;
    pip)  set -- python3 -m pip install --user "$SPEC" ;;
    *) echo "error: unknown method $METHOD (uv|pipx|pip)" >&2; exit 1 ;;
esac

if [ "$DRY_RUN" = 1 ]; then
    # `if`, not `[ … ] && …`: under set -e a false one-liner test is the
    # whole command's status and would exit the script.
    if [ "$BOOTSTRAP_UV" = 1 ]; then
        echo "would run: curl -LsSf $UV_BOOTSTRAP_URL | sh"
    fi
    echo "would run: $*"
    exit 0
fi

if [ "$BOOTSTRAP_UV" = 1 ]; then
    bootstrap_uv
fi

echo "installing $SPEC via $METHOD..."
"$@"

if ! command -v sediment >/dev/null 2>&1; then
    # uv/pipx/pip --user all install into a per-user bin dir that may not be
    # on PATH yet; name the usual suspects instead of guessing wrong.
    echo ""
    echo "note: 'sediment' is not on your PATH yet. Likely bin dirs:"
    echo "  uv:   ~/.local/bin        (uv tool update-shell adds it)"
    echo "  pipx: ~/.local/bin        (pipx ensurepath adds it)"
    echo "  pip:  \$(python3 -m site --user-base)/bin"
fi

echo ""
echo "To connect to a deployment:   sediment login <url>"
echo "Use an operator credential for login; then verify it with sediment facts."
echo "For capture, enroll a separate named ingest credential from stdin:"
echo "  sediment login <url> --capture --with-token"
echo "Capture-only installs do not need libpq; local server/database commands do."
echo "Local setup and maintained host libpq prerequisites:"
echo "  https://github.com/sediment-ai/sediment/blob/main/docs/quickstart.md"

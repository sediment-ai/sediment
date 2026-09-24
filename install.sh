#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# curl -fsSL https://sediment.so/install.sh | sh
#
# Install sediment-cli from PyPI with Python 3.12 and prepare the maintained
# host libraries used by `sediment server`. macOS requires Homebrew; Debian
# and Ubuntu require sudo access. --capture-only skips host package changes.
# Explicit pipx/pip methods require an existing Python 3.12 installation.

set -eu

VERSION=""
METHOD="${SEDIMENT_INSTALL_METHOD:-uv}"
DRY_RUN=0
CAPTURE_ONLY=0
UV_BOOTSTRAP_URL="https://astral.sh/uv/install.sh"

usage() {
    echo "usage: install.sh [--version X.Y.Z] [--method uv|pipx|pip] [--capture-only] [--dry-run]"
}

fail() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --version)
            [ $# -ge 2 ] || fail "--version needs a value"
            VERSION="$2"; shift 2 ;;
        --method)
            [ $# -ge 2 ] || fail "--method needs a value"
            METHOD="$2"; shift 2 ;;
        --capture-only) CAPTURE_ONLY=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown argument $1" ;;
    esac
done

# Keep the old environment value working without selecting system pip.
[ "$METHOD" != detect ] || METHOD=uv
case "$METHOD" in uv|pipx|pip) ;; *) fail "unknown method $METHOD (uv|pipx|pip)" ;; esac
: "${HOME:?error: HOME must name your user directory}"
ORIGINAL_PATH="$PATH"
SPEC="sediment-cli${VERSION:+==$VERSION}"

run() {
    if [ "$DRY_RUN" = 1 ]; then echo "would run: $*"; else "$@"; fi
}

if [ "$CAPTURE_ONLY" = 0 ]; then
    [ "$(id -u)" != 0 ] || fail "run this installer as your normal user; it uses sudo for system dependencies"
    SYSTEM=$(uname -s)
    case "$SYSTEM/$(uname -m)" in
        Darwin/arm64|Darwin/x86_64|Linux/aarch64|Linux/x86_64) ;;
        *) fail "local server setup requires macOS or Linux on a supported 64-bit processor" ;;
    esac
    case "$SYSTEM" in
        Darwin)
            if [ "$DRY_RUN" = 0 ]; then
                command -v brew >/dev/null 2>&1 || fail "install Homebrew from https://brew.sh, then retry"
            fi
            echo "Installing host packages with Homebrew: libpq openssl@3"
            run brew install libpq openssl@3
            ;;
        Linux)
            if [ "$DRY_RUN" = 0 ]; then
                command -v apt-get >/dev/null 2>&1 || fail "local server setup requires Debian or Ubuntu with apt-get"
                command -v sudo >/dev/null 2>&1 || fail "sudo is required to install the host packages"
            fi
            echo "Installing host packages with sudo apt-get: git ca-certificates libpq5 libxml2 libzstd1 liblz4-1 zlib1g"
            run sudo apt-get update
            run sudo apt-get install -y git ca-certificates libpq5 libxml2 libzstd1 liblz4-1 zlib1g
            ;;
        *) fail "local server setup requires macOS with Homebrew, Debian, or Ubuntu" ;;
    esac
fi

# Prefer a known user-owned bin directory already visible to the calling shell.
BIN_DIR="$HOME/.local/bin"
for candidate in "$HOME/.local/bin" "$HOME/bin"; do
    case ":$ORIGINAL_PATH:" in
        *":$candidate:"*)
            if [ -d "$candidate" ] && [ -O "$candidate" ] && [ -w "$candidate" ] && [ ! -L "$candidate" ]; then
                BIN_DIR="$candidate"
                break
            fi
            ;;
    esac
done

bootstrap_uv() {
    if [ "$DRY_RUN" = 1 ]; then
        echo "would download $UV_BOOTSTRAP_URL and run the completed installer with sh"
        return
    fi
    command -v curl >/dev/null 2>&1 || fail "curl is required to download uv"
    echo "Installing uv from $UV_BOOTSTRAP_URL"
    # Download completely before executing: POSIX sh has no pipefail, so a
    # failed `curl | sh` can otherwise run a partial script and report success.
    UV_SCRIPT=$(mktemp "${TMPDIR:-/tmp}/sediment-uv.XXXXXXXX")
    trap 'rm -f "$UV_SCRIPT"' 0
    trap 'exit 1' HUP INT TERM
    curl -fLsS --proto '=https' --tlsv1.2 "$UV_BOOTSTRAP_URL" -o "$UV_SCRIPT"
    UV_BIN_DIR="${UV_INSTALL_DIR:-$BIN_DIR}"
    UV_INSTALL_DIR="$UV_BIN_DIR" UV_NO_MODIFY_PATH=1 sh "$UV_SCRIPT"
    rm -f "$UV_SCRIPT"
    trap - 0 HUP INT TERM
    PATH="$UV_BIN_DIR:$PATH"
    export PATH
    command -v uv >/dev/null 2>&1 || fail "uv installation did not produce an executable"
}

PYTHON=python3.12
if [ "$METHOD" != uv ] && [ "$DRY_RUN" = 0 ]; then
    if ! command -v "$PYTHON" >/dev/null 2>&1; then PYTHON=python3; fi
    "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' 2>/dev/null ||
        fail "--method $METHOD requires Python 3.12; use --method uv to install it automatically"
fi

case "$METHOD" in
    uv)
        BIN_DIR="${UV_TOOL_BIN_DIR:-$BIN_DIR}"
        export UV_TOOL_BIN_DIR="$BIN_DIR"
        if ! command -v uv >/dev/null 2>&1; then bootstrap_uv; fi
        set -- uv tool install --python 3.12 --upgrade "$SPEC"
        ;;
    pipx)
        BIN_DIR="${PIPX_BIN_DIR:-$BIN_DIR}"
        export PIPX_BIN_DIR="$BIN_DIR"
        set -- pipx install --python "$PYTHON" "$SPEC"
        ;;
    pip)
        if [ "$DRY_RUN" = 0 ]; then BIN_DIR="$("$PYTHON" -m site --user-base)/bin"; fi
        set -- "$PYTHON" -m pip install --user "$SPEC"
        ;;
esac

echo "Installing $SPEC via $METHOD..."
run "$@"
[ "$DRY_RUN" = 0 ] || exit 0

EXECUTABLE="$BIN_DIR/sediment"
[ -x "$EXECUTABLE" ] || fail "installation did not produce $EXECUTABLE"
"$EXECUTABLE" --help >/dev/null || fail "the installed sediment command could not start"

# A child process cannot update its parent's PATH. Print one exact command,
# with shell quoting that also works when the home directory contains spaces.
if [ "$(PATH="$ORIGINAL_PATH" command -v sediment || true)" != "$EXECUTABLE" ]; then
    QUOTED_BIN=$(printf '%s' "$BIN_DIR" | sed "s/'/'\\\\''/g")
    printf '\nBefore running sediment in this terminal, run:\n'
    printf "  export PATH='%s':\"\$PATH\"\n" "$QUOTED_BIN"
fi

printf '\nInstalled %s.\n' "$SPEC"
if [ "$CAPTURE_ONLY" = 0 ]; then
    echo "Start your local server: sediment server"
else
    echo "Enroll capture: sediment login <url> --capture"
    echo "Then install repository hooks: sediment install /path/to/repo"
fi

# SPDX-License-Identifier: AGPL-3.0-or-later
# Shared helpers for verify-sediment scripts. Sourced, not executed.

set -euo pipefail

_verify_repo_root() {
  git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel
}

REPO_ROOT="$(_verify_repo_root)"
VERIFY_BIN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_verify_load_state() {
  local run_dir="${SEDIMENT_VERIFY_RUN_DIR:-}"
  if [[ -z "$run_dir" ]]; then
    echo "error: SEDIMENT_VERIFY_RUN_DIR is unset; eval launch exports first" >&2
    return 1
  fi
  if [[ ! -f "$run_dir/state.env" ]]; then
    echo "error: missing $run_dir/state.env" >&2
    return 1
  fi
  # shellcheck disable=SC1091
  source "$run_dir/state.env"
}

_verify_clean_env() {
  # Drop inherited SEDIMENT_* so a checkout .env cannot configure the server.
  # Keep SEDIMENT_VERIFY_* — those are this skill's run state.
  local name
  for name in $(compgen -e | grep '^SEDIMENT_' || true); do
    case "$name" in
      SEDIMENT_VERIFY_*) continue ;;
    esac
    unset "$name"
  done
}

_verify_sediment() {
  _verify_clean_env
  export HOME="$SEDIMENT_VERIFY_HOME"
  export PATH="$REPO_ROOT/.venv/bin:$PATH"
  cd "$SEDIMENT_VERIFY_HOME"
  uv run --project "$REPO_ROOT" sediment "$@"
}

_verify_free_port() {
  python3 - <<'PY'
import socket
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    print(s.getsockname()[1])
PY
}

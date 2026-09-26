# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sediment attribution stamper — the notes-attribution client.

Records which coding-agent sessions contributed to a commit as a git note under
``refs/notes/sediment``, so the server-side attribution derivation can join
commits to gateway/OTel sessions deterministically instead of by similarity
alone.

Stdlib only, no daemon. Nine subcommands:

  mark --tool NAME   agent-hook entry point: reads the agent's hook payload on
                     stdin, records a session marker in the repo's git dir,
                     and appends a ``mark`` line to
                     ``~/.sediment/attribution.log`` (once per new marker,
                     not per tool call); if that repo has no sediment post-commit hook
                     (so the marker can never be consumed — e.g. a clone the
                     agent created itself), also logs the ``unhooked-repo``
                     miss; in owner-allowlisted repos
                     (``auto_install_remotes`` in ``config.json``) it instead
                     installs/refreshes the git hooks itself, so the repo
                     stamps from its first agent commit onward
  cursor-hook        Cursor native-hook entry point: marks Agent and Tab edits;
                     successful Agent Write calls also emit one implicit-accept
                     developer decision when telemetry is configured
  stamp              post-commit hook: writes the note on HEAD from the
                     markers, logs ``note-created``, then consumes its
                     marker generations
  union-squash-notes MSG_FILE SOURCE
                     prepare-commit-msg hook: on a ``git merge --squash``
                     commit (each squashed branch commit already cleared its
                     own markers when it was stamped), reads the squashed
                     commits' SHAs from the still-available squash message
                     and unions their already-written notes back into the
                     local marker file, so the post-commit ``stamp`` step
                     above writes a correct note on the squash commit too
  push-notes REMOTE  pre-push hook: reconciles the notes ref with the remote
                     (fetch + ``cat_sort_uniq`` union merge), then pushes it
                     alongside the push, retrying once if the remote moved;
                     a final failure is logged to
                     ``~/.sediment/attribution.log``
  repair-notes [REMOTE]
                     operator command (not a hook, NOT best-effort): the same
                     reconcile + push on demand, for a machine whose notes
                     ref already diverged, or a fresh machine adopting
                     the remote's notes; exits non-zero on failure
  doctor [REPO ...]  operator command: one-shot health check of everything
                     attribution needs on this machine — agent hook entries,
                     the fleet git template, the attribution log's recorded
                     misses, and per REPO the git hook set, ``notes.rewriteRef``,
                     the notes ref against origin (the diverged state),
                     and markers a commit failed to consume. One line per
                     finding; exits 1 when any check FAILs. Read-only unless
                     ``--fetch``, which writes only the notes tracking ref
  install [REPO]     installs agent hooks (user-level) + git hooks (per-repo)
                     + ``notes.rewriteRef`` (local config) + the agent
                     telemetry env files generated from the ``sediment
                     login`` config (``--no-env`` skips; ``--user-id``
                     stamps per-developer attribution; ``--gateway-url``/
                     ``--gateway-key`` add the completions routing);
                     ``--transcripts`` opts in to the SessionEnd transcript
                     extractor and its PreToolUse snapshot hook
                     (sediment_transcript.py — ships edit text pairs, a
                     different privacy class, hence opt-in)
  install --fleet    emits the machine-wide MDM bundle (git ``init.templateDir``
                     hooks template, system-gitconfig fragment, Claude Code /
                     Codex hook fragments) to a directory; ``--apply``
                     provisions this machine directly (needs privileges)
  uninstall [REPO]   removes the per-repo git hooks; ``--agents`` also removes
                     the agent-hook entries

Privacy contract (normative — tested): the note payload contains ONLY ``tool``,
``session_id``, and timestamps. Local markers also carry a UUID generation.
No prompt text, no diff content, no file paths, no model names, no hostnames,
no developer identity
beyond what the commit object already carries.

``mark``/``stamp``/``union-squash-notes``/``push-notes`` are best-effort:
they always exit 0 so they can never break a tool call, a commit, or a push.
"""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from uuid import UUID, uuid4

try:
    from . import ui
except ImportError:  # pragma: no cover — run by path, not as a package
    # The checkout shim (scripts/sediment_attribution.py) and the fleet
    # bundle execute this file standalone, where no sibling ui module
    # exists. Those are hook/MDM piped contexts: plain text is the correct
    # output there, so a passthrough stand-in keeps the bytes identical.
    class ui:  # type: ignore[no-redef]  # ponytail: plain-text stand-in
        @staticmethod
        def style(text: str, *names: str, stream=None) -> str:
            return text

        @staticmethod
        def glyph(char: str, name: str, stream=None) -> str:
            return ""

        @staticmethod
        def error_line(msg: str) -> str:
            return f"error: {msg}"

        @staticmethod
        def warn_line(msg: str) -> str:
            return f"warning: {msg}"


NOTES_REF = "refs/notes/sediment"
MARKER_FILENAME = "sediment-sessions"
MARKER_LOCK_FILENAME = "sediment-sessions.lock"
NOTES_LOCK_FILENAME = "sediment-notes.lock"
SCHEMA_VERSION = 1
_DOCTOR_USER_AGENT = "sediment-doctor/1"
# Recursion guard: push-notes runs `git push`, which fires pre-push again.
PUSH_GUARD_ENV = "SEDIMENT_NOTES_PUSH_IN_PROGRESS"
HOOK_BLOCK_BEGIN = "# >>> sediment-attribution >>>"
HOOK_BLOCK_END = "# <<< sediment-attribution <<<"
# Group 1 is the block body; the trailing newline is consumed so a rewrite
# leaves the surrounding hook byte-identical.
_HOOK_BLOCK_RE = re.compile(
    re.escape(HOOK_BLOCK_BEGIN) + r"(.*?)" + re.escape(HOOK_BLOCK_END) + r"\n?",
    re.DOTALL,
)
# The three git hooks and the subcommand each one invokes. One table, so the
# per-repo installer, the fleet template, doctor and uninstall can never
# disagree about which hooks exist.
_REPO_HOOKS = (
    ("post-commit", "stamp"),
    ("prepare-commit-msg", 'union-squash-notes "$1" "$2"'),
    ("pre-push", 'push-notes "$1"'),
)
# Substrings used to recognise our entries in agent hook configs. The
# transcript extractor is installed and uninstalled by this installer too,
# but only behind the opt-in
# ``install --transcripts`` flag: it ships edit text pairs off the machine —
# a different privacy class than the stamper's own session-id-only hooks.
# The hook clients' invocation forms: the legacy/fleet script names, the
# packaged modules, and the installed `sediment`
# executable. Recognition must accept every generation — install heals old
# entries in place, and doctor must see an old install as ours, not absent.
_HOOK_COMMAND_TAGS = (
    "sediment_attribution.py",
    "attribution.py",
    "sediment_transcript.py",
    "sediment_cli/transcript.py",
)
_SEDIMENT_EXE_RE = re.compile(
    r'^"[^"]*/sediment(?:\.exe)?" (?:cursor-hook|mark|transcript)(?:\s|$)',
    re.IGNORECASE,
)


def _is_sediment_command(command: str) -> bool:
    normalized = command.replace("\\", "/")
    return any(tag in normalized for tag in _HOOK_COMMAND_TAGS) or bool(
        _SEDIMENT_EXE_RE.match(normalized)
    )


# Claude Code tools whose use means "this session touched the repo" (edits and
# shell — shell covers agent-run `git commit`). MultiEdit is gone in 2.x
# clients but 1.x dispatches matchers by exact token, so keep it for them.
CLAUDE_MATCHER = "Edit|MultiEdit|Write|NotebookEdit|Bash"

# Cursor's native hooks use a flat command list, not the nested Claude Code
# and Codex hook blocks. A matcher is valid on the Agent tool events only.
_CURSOR_HOOKS: dict[str, str | None] = {
    "postToolUse": "Write",
    "postToolUseFailure": "Write",
    "afterTabFileEdit": None,
}


def _git(args: list[str], cwd: str | Path | None = None) -> str | None:
    """Run git, returning stripped stdout or None on any failure."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _git_dir(cwd: str | Path) -> Path | None:
    """The repo's absolute git dir for ``cwd``, or None outside a work tree."""
    # --absolute-git-dir also succeeds in bare repos; require a work tree so
    # hooks running in odd contexts (bare mirrors) no-op.
    inside = _git(["rev-parse", "--is-inside-work-tree"], cwd)
    if inside != "true":
        return None
    git_dir = _git(["rev-parse", "--absolute-git-dir"], cwd)
    return Path(git_dir) if git_dir else None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _error(message: str) -> None:
    print(ui.error_line(message), file=sys.stderr)


def _warn(message: str) -> None:
    print(ui.warn_line(message), file=sys.stderr)


def _skipped(message: str) -> str:
    """Report a step skipped because what it wires is absent, and return the
    installer's status word."""
    print(ui.style(message, "dim", stream=sys.stderr), file=sys.stderr)
    return "skipped"


# ── attribution log ───────────────────────────────────────────────────────
#
# Local, greppable record of both the chain's healthy path (`mark`,
# `note-created`) and the misses that are otherwise invisible: a best-effort
# hook prints at most one stderr line, buried in git output — an unhooked
# repo an agent is editing, a notes push that cannot land. A `mark` with no
# following `note-created` for the same git_dir/session_id is a broken
# chain; doctor's `_DOCTOR_LOG_EVENTS` still names only the misses,
# so the breadcrumbs stay invisible to its summary and are meant to be
# grepped directly. Local file only; never shipped — the marker/note
# privacy contract is unchanged.

ATTRIBUTION_LOG_ENV = "SEDIMENT_ATTRIBUTION_LOG"
ATTRIBUTION_LOG_CAP_BYTES = 256 * 1024


def _log_path() -> Path:
    override = os.environ.get(ATTRIBUTION_LOG_ENV)
    if override:
        return Path(override)
    return Path.home() / ".sediment" / "attribution.log"


def _log_event(event: str, **fields: str) -> None:
    """Append one greppable JSON line to the local attribution log."""
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # ponytail: single-slot rotation, .1 overwritten each time
        if path.stat().st_size > ATTRIBUTION_LOG_CAP_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass
    line = json.dumps({"at": _now_iso(), "event": event, **fields})
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _post_commit_installed(hooks_dir: Path | None) -> bool:
    """True when the repo's effective post-commit hook carries our block.
    An unresolvable hooks dir counts as installed — stay quiet rather than
    cry wolf."""
    if hooks_dir is None:
        return True
    try:
        text = (hooks_dir / "post-commit").read_text(encoding="utf-8")
    except OSError:
        return False
    return HOOK_BLOCK_BEGIN in text


# ── auto-install (owner allowlist) ────────────────────────────────────────
#
# A platform owner can list remote prefixes whose repos `mark` may converge:
# on each new marker it (re-)runs the idempotent per-repo git-hook install,
# so a clone the agent created five seconds ago — or a checkout whose hooks
# predate a newer entry — is hooked before its first commit. Scoped to an
# explicit allowlist because installing everywhere would make `pre-push`
# push a notes ref (session UUIDs) to third-party remotes the org does not
# own. No config, or an empty list, means the feature is off.

CONFIG_ENV = "SEDIMENT_ATTRIBUTION_CONFIG"


def _config_paths() -> list[Path]:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return [Path(override)]
    return [
        # Fleet: MDM ships config.json next to the script (e.g. /opt/sediment).
        Path(__file__).resolve().parent / "config.json",
        # Per-user, no privileges needed.
        Path.home() / ".sediment" / "config.json",
    ]


def _load_auto_install_remotes() -> list[str] | None:
    """The owner's auto-install remote prefixes; None or [] means off.

    First config file found wins — a fleet config deliberately shadows a
    per-user one. A config that exists but does not parse, or carries the
    wrong shape, logs a ``config-error`` event and counts as off: a typo
    must not silently widen or narrow which repos get stamped.
    """
    for path in _config_paths():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue  # not present — try the next location
        try:
            cfg = json.loads(raw)
            if not isinstance(cfg, dict):
                raise ValueError("config must be a JSON object")
            remotes = cfg.get("auto_install_remotes", [])
            # Reject blank entries outright: "" normalizes to "/" and would
            # silently allowlist every local-path remote.
            if not (
                isinstance(remotes, list)
                and all(isinstance(r, str) and r.strip() for r in remotes)
            ):
                raise ValueError(
                    "auto_install_remotes must be a list of non-empty strings"
                )
        except ValueError as exc:
            _log_event("config-error", path=str(path), detail=str(exc)[:200])
            return None
        return remotes
    return None


def _normalize_remote(url: str) -> str:
    """Lower-cased ``host/path/`` with protocol, credentials, and ``.git``
    stripped, so one allowlist prefix covers https, ssh, and scp-like forms
    (``github.com/acme/`` matches ``https://github.com/acme/repo.git`` and
    ``git@github.com:acme/repo.git`` alike). The trailing slash makes the
    prefix match segment-aligned: ``.../repo/`` never matches ``.../repo2``.
    """
    url = url.strip().lower()
    for prefix in ("ssh://", "git://", "http://", "https://"):
        if url.startswith(prefix):
            url = url[len(prefix) :]
            break
    else:
        # scp-like: git@github.com:acme/repo → git@github.com/acme/repo
        if "@" in url.split("/", 1)[0] and ":" in url:
            url = url.replace(":", "/", 1)
    host, _, rest = url.partition("/")
    host = host.rpartition("@")[2]
    url = f"{host}/{rest}"
    if url.endswith(".git"):
        url = url[: -len(".git")]
    return url.rstrip("/") + "/"


def _remote_allowlisted(repo: str | Path, remotes: list[str]) -> bool:
    """True when the repo's origin URL falls under an allowlisted prefix."""
    url = _git(["remote", "get-url", "origin"], repo)
    if not url:
        return False  # no origin — nothing to match against
    normalized = _normalize_remote(url)
    return any(normalized.startswith(_normalize_remote(r)) for r in remotes)


def _install_repo_hooks(hooks_dir: Path, repo_path: Path) -> list[str]:
    """Write our block into the three git hooks and set notes.rewriteRef.

    Idempotent — shared by ``install`` and mark's allowlisted auto-install.
    Returns the hook names whose block landed (an existing non-sh hook is
    left alone with a stderr warning from ``_install_hook_block``).
    """
    installed = [
        name
        for name, sub in _REPO_HOOKS
        if _install_hook_block(hooks_dir / name, _script_invocation(sub))
    ]
    # notes.rewriteRef is multi-valued: --add alongside any pre-existing value
    # instead of overwriting it.
    existing = _git(["config", "--get-all", "notes.rewriteRef"], repo_path) or ""
    if NOTES_REF not in existing.splitlines():
        _git(["config", "--add", "notes.rewriteRef", NOTES_REF], repo_path)
    return installed


def _record_marker(tool: str, session_id: str, cwd: str) -> None:
    """Record one validated Session marker when ``cwd`` is a git worktree."""
    git_dir = _git_dir(cwd)
    if git_dir is None:
        return
    marker_path = git_dir / MARKER_FILENAME
    try:
        with _file_lock(git_dir / MARKER_LOCK_FILENAME, "marker_busy", timeout=1):
            entries = _marker_entries(marker_path)
            key = (tool, session_id)
            fresh = key not in entries
            if fresh:
                entries[key] = {
                    "tool": tool,
                    "session_id": session_id,
                    "stamped_at": _now_iso(),
                }
            entries[key]["generation"] = str(uuid4())
            _write_markers(marker_path, list(entries.values()))
    except _CaptureFailure as exc:
        _capture_failure(exc.reason, git_dir)
        return
    except OSError:
        _capture_failure("marker_write_failed", git_dir)
        return
    if not fresh:
        return  # Preserve the first active timestamp and breadcrumb.
    # A breadcrumb for the healthy path, not just the misses: a
    # session whose mark never gets followed by a stamp's note-created
    # narrows a broken chain to one greppable diff instead of a
    # cross-machine differential. Logged once per new marker, same as
    # the first insertion — refreshing a generation skips repeats.
    _log_event("mark", git_dir=str(git_dir), tool=tool, session_id=session_id)
    # Resolved once: `mark` runs on every agent tool call and
    # _hooks_dir shells out to git.
    hooks_dir = _hooks_dir(Path(cwd))
    hooked = _post_commit_installed(hooks_dir)
    remotes = _load_auto_install_remotes()
    if remotes and hooks_dir is not None and _remote_allowlisted(cwd, remotes):
        # Owner-allowlisted repo: converge the git hooks right here, so
        # the repo is stamped from its first agent commit onward.
        # Runs once per new marker (per session per repo) and also heals
        # partial/stale hook sets — the install is idempotent.
        _install_repo_hooks(hooks_dir, Path(cwd))
        if not hooked:
            _log_event(
                "auto-installed",
                git_dir=str(git_dir),
                tool=tool,
                session_id=session_id,
            )
    elif not hooked:
        _log_event(
            "unhooked-repo",
            git_dir=str(git_dir),
            tool=tool,
            session_id=session_id,
        )


def cmd_mark(tool: str) -> int:
    """Record {tool, session_id} for the repo the agent is editing.

    Reads the agent's hook payload (JSON) on stdin. Claude Code and Codex both
    carry ``session_id`` and ``cwd`` (Codex may use ``thread_id``).
    Exits 0 on every path; a marker miss
    only means notes attribution degrades to the jaccard fallback for that
    commit.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        session_id = payload.get("session_id") or payload.get("thread_id")
        if not isinstance(session_id, str) or not session_id:
            return 0
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            cwd = os.getcwd()
        _record_marker(tool, session_id, cwd)
    except Exception:
        pass  # best-effort: never fail the agent's tool call
    return 0


def _cursor_trail(message: str) -> None:
    """Write one bounded local Cursor capture diagnostic."""
    print(f"sediment-cursor-hook: {message}", file=sys.stderr)


def _cursor_repository_directory(payload: dict, event: str) -> str | None:
    """Resolve observed paths without using the hook process's working directory."""
    try:
        inputs = (
            payload if event == "afterTabFileEdit" else payload.get("tool_input", {})
        )
        key = "file_path" if event == "afterTabFileEdit" else "path"
        if not isinstance(inputs, dict):
            raise ValueError
        edited = None
        if key in inputs:
            value = inputs[key]
            if not isinstance(value, str) or not value or "\0" in value:
                raise ValueError
            edited = Path(value)

        if edited is not None and edited.is_absolute():
            # The edit identifies a nested repository even when cwd names its parent.
            paths = [edited.resolve()]
        else:
            cwd = payload.get("cwd")
            roots = (
                [cwd] if cwd not in (None, "") else payload.get("workspace_roots", [])
            )
            if not isinstance(roots, list):
                raise ValueError
            if not roots:
                _cursor_trail("no repository directory; Attribution mark skipped")
                return None
            paths = []
            for root in roots:
                if not isinstance(root, str) or not root or "\0" in root:
                    raise ValueError
                directory = Path(root)
                if not directory.is_absolute() or not directory.is_dir():
                    raise ValueError
                paths.append((directory / edited if edited else directory).resolve())

        repositories: dict[Path, str] = {}
        for path in sorted(set(paths)):
            if edited is not None and path.is_dir():
                raise ValueError
            directory = path.parent if edited is not None else path
            git_dir = _git_dir(directory)
            if git_dir is None:
                raise ValueError
            repositories[git_dir] = str(directory)
        if len(repositories) != 1:
            _cursor_trail("ambiguous repository directory; Attribution mark skipped")
            return None
        return next(iter(repositories.values()))
    except (OSError, RuntimeError, TypeError, ValueError):
        _cursor_trail("invalid repository directory; Attribution mark skipped")
        return None


def _capture_client_path(name: str) -> Path:
    for filename in (f"{name}.py", f"sediment_{name}.py"):
        path = Path(__file__).with_name(filename)
        if path.is_file():
            return path
    raise RuntimeError("capture helper is unavailable")


def _capture_client(name: str):
    """Load the same stdlib owner from the package or a complete fleet copy."""
    if __package__:
        return importlib.import_module(f".{name}", __package__)
    path = _capture_client_path(name)
    module_name = f"_sediment_enrollment_{name}"
    existing = sys.modules.get(module_name)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("capture helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve postponed annotations through the module registry.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _transcript_client():
    return _capture_client("transcript")


def _cursor_attribute(key: str, value: str | bool) -> dict:
    encoded = (
        {"boolValue": value} if isinstance(value, bool) else {"stringValue": value}
    )
    return {"key": key, "value": encoded}


def _cursor_decision_payload(
    session_id: str, call_id: str, occurred_at_ns: int, user_id: str | None
) -> dict:
    attributes = [
        _cursor_attribute("agent", "cursor"),
        _cursor_attribute("session.id", session_id),
        _cursor_attribute("tool_use_id", call_id),
        _cursor_attribute("tool_name", "Write"),
        _cursor_attribute("decision", "accept"),
        _cursor_attribute("explicit", False),
    ]
    resource = (
        {"attributes": [_cursor_attribute("user.id", user_id)]} if user_id else {}
    )
    return {
        "resourceLogs": [
            {
                "resource": resource,
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "body": {"stringValue": "sediment.tool_decision"},
                                "timeUnixNano": str(occurred_at_ns),
                                "attributes": attributes,
                            }
                        ]
                    }
                ],
            }
        ]
    }


def cmd_cursor_hook() -> int:
    """Translate one native Cursor hook payload without blocking Cursor."""
    occurred_at_ns = time.time_ns()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError):
        _cursor_trail("invalid JSON; skipping")
        return 0
    if not isinstance(payload, dict):
        _cursor_trail("input is not a JSON object; skipping")
        return 0

    session_id = payload.get("conversation_id")
    if not isinstance(session_id, str) or not session_id:
        _cursor_trail("missing conversation_id; skipping")
        return 0
    event = payload.get("hook_event_name")
    if not isinstance(event, str) or event not in _CURSOR_HOOKS:
        _cursor_trail("unsupported hook_event_name; skipping")
        return 0
    if event != "afterTabFileEdit" and payload.get("tool_name") != "Write":
        _cursor_trail("non-Write tool event; skipping")
        return 0

    cwd = _cursor_repository_directory(payload, event)
    if cwd is not None:
        try:
            _record_marker("cursor", session_id, cwd)
        except Exception:
            _cursor_trail("Attribution marking failed; decision capture continues")

    if event != "postToolUse":
        return 0
    call_id = payload.get("tool_use_id")
    if not isinstance(call_id, str) or not call_id:
        _cursor_trail("missing tool_use_id; decision skipped")
        return 0

    try:
        telemetry = _transcript_client()
        endpoint = telemetry._endpoint()
        if endpoint is None:
            return 0
        decision = _cursor_decision_payload(
            session_id,
            call_id,
            occurred_at_ns,
            telemetry._resource_user_id(),
        )
        telemetry._post(endpoint, decision)
    except Exception:
        _cursor_trail("decision delivery failed; skipping")
    return 0


class _CaptureFailure(Exception):
    """A content-free local capture outcome; never include exception text."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


_CAPTURE_FAILURE_REASONS = (
    "marker_busy",
    "marker_read_failed",
    "marker_write_failed",
    "marker_durability_unconfirmed",
    "stamp_busy",
    "stamp_target_failed",
    "stamp_log_failed",
    "note_read_failed",
    "note_invalid",
    "note_write_failed",
    "note_timeout",
    "stamp_cleanup_failed",
    "stamp_cleanup_unconfirmed",
    "notes_reconcile_busy",
    "notes_reconcile_failed",
    "notes_lock_failed",
)


def _capture_failure(reason: str, git_dir: Path | None, sha: str = "") -> None:
    try:
        _log_event(reason, git_dir=str(git_dir or ""), sha=sha)
    except OSError:
        pass  # The stderr diagnostic still reports an unwritable local log.
    target = f" sha={sha}" if sha else ""
    print(f"sediment-attribution: {reason}{target}", file=sys.stderr)


@contextmanager
def _file_lock(path: Path, busy: str, *, timeout: float = 0) -> Iterator[None]:
    """Lock a stable inode. Marker waits are bounded; notes never wait."""
    try:
        import fcntl
    except ImportError:
        raise OSError("capture requires POSIX file locks") from None

    with path.open("a+b") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _CaptureFailure(busy) from None
                time.sleep(min(0.01, remaining))
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _notes_lock_path(cwd: str | Path) -> Path:
    # Relative to cwd in the main work tree, absolute in a linked one;
    # --path-format=absolute would pin git >= 2.31 for nothing.
    common = _git(["rev-parse", "--git-common-dir"], cwd)
    if common is None:
        raise _CaptureFailure("notes_lock_failed")
    return Path(cwd, common) / NOTES_LOCK_FILENAME


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_markers(marker_path: Path, entries: list[dict]) -> None:
    """Publish one complete population while the caller holds its marker lock."""
    # Bounded cleanup of interrupted writers' private files, under the same lock.
    for path in islice(marker_path.parent.iterdir(), 128):
        if re.fullmatch(r"\.sediment-sessions\.[0-9a-f]{32}\.tmp", path.name):
            path.unlink(missing_ok=True)
    if not entries:
        marker_path.unlink(missing_ok=True)
    else:
        temporary = marker_path.with_name(f".sediment-sessions.{uuid4().hex}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for entry in entries:
                    handle.write(json.dumps(entry) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, marker_path)
        finally:
            temporary.unlink(missing_ok=True)
    try:
        _sync_directory(marker_path.parent)
    except OSError:
        raise _CaptureFailure("marker_durability_unconfirmed") from None


def _read_markers(marker_path: Path, *, strict: bool = False) -> list[dict]:
    """The rows that parse. A malformed row (a legacy append interrupted
    mid-write) is skipped and counted, never a reason to refuse the file: the
    next writer replaces the whole population under the lock and heals it.
    ``strict`` refuses only an unreadable file, where a rewrite would drop rows
    the reader never saw."""
    markers: list[dict] = []
    try:
        raw = marker_path.read_bytes()
    except FileNotFoundError:
        return markers
    except OSError:
        if strict:
            raise _CaptureFailure("marker_read_failed") from None
        return markers
    skipped = 0
    for row in raw.splitlines():
        try:
            # Replacement decoding would invent tool or Session identities.
            line = row.decode("utf-8").strip()
            if not line:
                continue
            entry = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("tool"), str)
            and isinstance(entry.get("session_id"), str)
        ):
            markers.append(entry)
        else:
            skipped += 1
    if skipped and strict:
        try:
            _log_event(
                "marker_rows_skipped",
                git_dir=str(marker_path.parent),
                count=str(skipped),
            )
        except OSError:
            pass
    return markers


def _marker_entries(marker_path: Path) -> dict[tuple[str, str], dict]:
    """Normalize legacy rows under the marker lock; preserve first timestamps."""
    entries: dict[tuple[str, str], dict] = {}
    for marker in _read_markers(marker_path, strict=True):
        key = (marker["tool"], marker["session_id"])
        if key in entries:
            continue
        entry = {
            name: marker[name]
            for name in ("tool", "session_id", "stamped_at")
            if name in marker
        }
        generation = marker.get("generation")
        try:
            UUID(generation)
        except (ValueError, TypeError, AttributeError):
            generation = str(uuid4())
        entry["generation"] = generation
        entries[key] = entry
    return entries


def cmd_stamp() -> int:
    """post-commit: write the attribution note on HEAD from the markers.

    Keep Git outside the short marker lock. A successful note consumes only
    its snapshot generations; later marks survive even for the same Session.
    The common notes mutex serializes local writers across linked worktrees.
    """
    git_dir = None
    sha = ""
    stage = "stamp_target_failed"
    try:
        cwd = os.getcwd()
        git_dir = _git_dir(cwd)
        if git_dir is None:
            return 0
        marker_path = git_dir / MARKER_FILENAME
        if not marker_path.exists():
            return 0
        sha = _git(["rev-parse", "--verify", "HEAD^{commit}"], cwd) or ""
        if not sha:
            raise _CaptureFailure("stamp_target_failed")
        stage = "notes_lock_failed"
        with _file_lock(_notes_lock_path(cwd), "stamp_busy"):
            stage = "marker_write_failed"
            with _file_lock(git_dir / MARKER_LOCK_FILENAME, "marker_busy", timeout=1):
                snapshot = _marker_entries(marker_path)
                if not snapshot:
                    return 0
                normalized = list(snapshot.values())
                if normalized != _read_markers(marker_path, strict=True):
                    _write_markers(marker_path, normalized)
            stage = "stamp_log_failed"
            _log_event(
                "stamp-started",
                git_dir=str(git_dir),
                sha=sha,
                session_ids=",".join(sorted({key[1] for key in snapshot})),
            )
            stage = "note_read_failed"
            sessions = {
                (s["tool"], s["session_id"]): s
                for s in _read_note_sessions(sha, cwd, strict=True)
            }
            stamped_at = _now_iso()
            for tool, session_id in snapshot:
                sessions.setdefault(
                    (tool, session_id),
                    {
                        "tool": tool,
                        "session_id": session_id,
                        "stamped_at": stamped_at,
                    },
                )
            payload = json.dumps(
                {
                    "v": SCHEMA_VERSION,
                    "sessions": [sessions[key] for key in sorted(sessions)],
                }
            )
            stage = "note_write_failed"
            result = subprocess.run(
                ["git", "notes", f"--ref={NOTES_REF}", "add", "-f", "-F", "-", sha],
                input=payload,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                raise _CaptureFailure("note_write_failed")
            # Log the pinned target before cleanup, including interrupted cleanup.
            stage = "stamp_log_failed"
            _log_event(
                "note-created",
                git_dir=str(git_dir),
                sha=sha,
                session_ids=",".join(sorted({s[1] for s in sessions})),
            )
            stage = "stamp_cleanup_failed"
            try:
                with _file_lock(
                    git_dir / MARKER_LOCK_FILENAME, "marker_busy", timeout=1
                ):
                    live = _marker_entries(marker_path)
                    remaining = [
                        entry
                        for key, entry in live.items()
                        if key not in snapshot
                        or entry["generation"] != snapshot[key]["generation"]
                    ]
                    _write_markers(marker_path, remaining)
            except _CaptureFailure as exc:
                reason = (
                    "stamp_cleanup_unconfirmed"
                    if exc.reason == "marker_durability_unconfirmed"
                    else "stamp_cleanup_failed"
                )
                raise _CaptureFailure(reason) from None
    except _CaptureFailure as exc:
        _capture_failure(exc.reason, git_dir, sha)
    except subprocess.TimeoutExpired:
        _capture_failure("note_timeout", git_dir, sha)
    except Exception:
        _capture_failure(stage, git_dir, sha)
    return 0


_SQUASH_COMMIT_SHA_RE = re.compile(r"^commit ([0-9a-f]{40})$", re.MULTILINE)


def _read_note_sessions(
    sha: str, cwd: str | Path, *, strict: bool = False
) -> list[dict]:
    """The ``sessions`` list from ``sha``'s note, or ``[]`` if it has none.

    Tolerant of whitespace-concatenated payloads: ``cmd_stamp``'s post-commit
    ``git notes add -f`` and the ``notes.rewriteRef`` rewrite that a
    ``git commit --amend``/rebase triggers can both attach a note to the same
    new HEAD, and (``notes.rewriteMode`` defaulting to ``concatenate``) the
    rewrite appends the copied note to the stamped one instead of replacing
    it. The server-side parser already unions such concatenated payloads
    (``sediment_derive/notes.py::_parse_note_body``); this reader must too,
    or a squash-merge of a branch containing an amended-with-new-marker commit
    drops that commit's sessions from the squash union.
    """
    try:
        result = subprocess.run(
            ["git", "notes", f"--ref={NOTES_REF}", "show", sha],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "LC_ALL": "C"},
        )
        if result.returncode != 0:
            # Only this explicit missing-note result permits a first write.
            if (
                result.returncode == 1
                and result.stderr.strip() == f"error: no note found for object {sha}."
            ):
                return []
            raise _CaptureFailure("note_read_failed")
        return _parse_note_sessions(result.stdout)
    except _CaptureFailure:
        if strict:
            raise
    except subprocess.TimeoutExpired:
        if strict:
            raise _CaptureFailure("note_timeout") from None
    except (OSError, UnicodeError):
        if strict:
            raise _CaptureFailure("note_read_failed") from None
    return []


def _parse_note_sessions(raw: str) -> list[dict]:
    """Stdlib equivalent of the canonical v1 concatenated-note contract."""
    try:
        if not raw.strip() or len(raw.encode("utf-8")) > 64 * 1024:
            raise ValueError
        sessions: dict[tuple[str, str], dict] = {}
        decoder = json.JSONDecoder()
        index = 0
        while index < len(raw):
            if raw[index].isspace():
                index += 1
                continue
            payload, index = decoder.raw_decode(raw, index)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"v", "sessions"}
                or type(payload["v"]) is not int
                or payload["v"] != SCHEMA_VERSION
                or not isinstance(payload["sessions"], list)
            ):
                raise ValueError
            for session in payload["sessions"]:
                if (
                    not isinstance(session, dict)
                    or set(session) != {"tool", "session_id", "stamped_at"}
                    or not all(isinstance(value, str) for value in session.values())
                ):
                    raise ValueError
                sessions.setdefault((session["tool"], session["session_id"]), session)
        return list(sessions.values())
    except (ValueError, UnicodeError, RecursionError):
        raise _CaptureFailure("note_invalid") from None


def cmd_union_squash_notes(msg_file: str, source: str) -> int:
    """prepare-commit-msg: fold squashed commits' notes into local markers.

    ``git merge --squash`` creates one brand-new commit; note-rewrite copying
    (``notes.rewriteRef``) only applies to amend/rebase/filter-branch, never
    to a squash-merge's new commit, and by the time it lands each squashed
    branch commit has already cleared its own markers (stamped individually
    when it was made — see ``cmd_stamp``). Nothing at the squash commit's own
    post-commit stamp step can recover that attribution on its own.

    Deliberately does **not** gate on ``source`` (git's classification,
    ``prepare-commit-msg``'s second argument) or read from ``msg_file``
    (git's third positional arg, unused here): ``source`` is only
    ``"squash"`` when the commit editor would show the unedited
    ``SQUASH_MSG`` content verbatim — an explicit ``git commit -m "..."``
    after the squash (the common case; most developers write their own
    message) classifies as ``"message"`` instead, even though
    ``.git/SQUASH_MSG`` still exists and still lists the squashed SHAs, and
    ``msg_file`` then holds only the developer's own text, not the squash
    list. Checking ``<git-dir>/SQUASH_MSG`` directly, and reading the
    squashed SHAs from *that* file, is the reliable signal regardless of how
    the commit message was supplied. Reading each listed SHA's
    already-written note and unioning those sessions into the local marker
    file lets the *existing* post-commit ``stamp`` step write a correct
    union note on the squash commit, with no new note-writing logic.
    """
    git_dir = None
    try:
        cwd = os.getcwd()
        git_dir = _git_dir(cwd)
        if git_dir is None:
            return 0
        squash_msg = git_dir / "SQUASH_MSG"
        if not squash_msg.exists():
            return 0
        text = squash_msg.read_text(encoding="utf-8")
        shas = _SQUASH_COMMIT_SHA_RE.findall(text)
        if not shas:
            return 0
        unioned: dict[tuple[str, str], dict] = {}
        for sha in shas:
            for session in _read_note_sessions(sha, cwd, strict=True):
                unioned.setdefault((session["tool"], session["session_id"]), session)
        if not unioned:
            return 0
        marker_path = git_dir / MARKER_FILENAME
        with _file_lock(git_dir / MARKER_LOCK_FILENAME, "marker_busy", timeout=1):
            live = _marker_entries(marker_path)
            for key, session in unioned.items():
                if key not in live:
                    live[key] = {**session, "generation": str(uuid4())}
            _write_markers(marker_path, list(live.values()))
    except _CaptureFailure as exc:
        _capture_failure(exc.reason, git_dir)
    except Exception:
        _capture_failure("marker_write_failed", git_dir)
    return 0


# Where the remote's notes land during reconcile. A plain tracking ref, so a
# failed merge can never damage the real local ref.
NOTES_REMOTE_TRACKING_REF = "refs/notes/sediment-remote"


def _reconcile_notes(remote: str) -> bool:
    """Union-merge the remote's notes ref into the local one, best-effort.

    A notes ref is an ordinary commit-ish ref, so a plain push only succeeds
    when local is a fast-forward of remote — a machine that stamped before
    ever fetching the remote notes builds a disjoint root and can never push
    again. Reconciling first repairs that state and keeps it from arising.
    ``cat_sort_uniq`` is lossless under the reader contract: the
    server-side parser (sediment_derive/notes.py::_parse_note_body) already
    parses any-whitespace-concatenated payloads and unions sessions per
    (tool, session_id), so a line-level union merge is safe and idempotent.
    """
    git_dir = None
    try:
        git_dir = _git_dir(os.getcwd())
        with _file_lock(_notes_lock_path(os.getcwd()), "notes_reconcile_busy"):
            return _reconcile_notes_locked(remote)
    except _CaptureFailure as exc:
        _capture_failure(exc.reason, git_dir)
    except Exception:
        _capture_failure("notes_reconcile_failed", git_dir)
    return False


def _reconcile_notes_locked(remote: str) -> bool:
    """The caller owns the common notes mutex, including the tracking-ref fetch."""
    if _git(["fetch", remote, f"+{NOTES_REF}:{NOTES_REMOTE_TRACKING_REF}"]) is None:
        return True  # remote unreachable or has no notes ref yet — the push decides
    if _git(["rev-parse", "--verify", "--quiet", NOTES_REF]) is None:
        # Fresh machine: adopt the remote's history outright.
        if _git(["update-ref", NOTES_REF, NOTES_REMOTE_TRACKING_REF]) is None:
            raise _CaptureFailure("notes_reconcile_failed")
        return True
    if (
        _git(["merge-base", "--is-ancestor", NOTES_REMOTE_TRACKING_REF, NOTES_REF])
        is not None
    ):
        return True  # local already contains the remote's history
    if (
        _git(
            [
                "notes",
                f"--ref={NOTES_REF}",
                "merge",
                "-s",
                "cat_sort_uniq",
                NOTES_REMOTE_TRACKING_REF,
            ]
        )
        is None
    ):
        raise _CaptureFailure("notes_reconcile_failed")
    return True


def _reconciled_push(remote: str) -> subprocess.CompletedProcess:
    """Reconcile then push the notes ref, retrying once if the remote moved.

    A concurrent push from another machine moving the remote tip
    mid-operation is expected, not exceptional — it has happened in
    practice.
    """
    env = dict(os.environ, **{PUSH_GUARD_ENV: "1"})
    result: subprocess.CompletedProcess
    for _attempt in range(2):
        if not _reconcile_notes(remote):
            return subprocess.CompletedProcess(
                ["git", "push"], 1, "", "notes_reconcile_incomplete"
            )
        result = subprocess.run(
            ["git", "push", remote, f"{NOTES_REF}:{NOTES_REF}"],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        if result.returncode == 0:
            break
    return result


def _push_failure_detail(result: subprocess.CompletedProcess) -> str:
    """Last stderr line, else last stdout line, else the exit code.

    A ``git push`` can fail with a message only on stdout (a remote-side
    pre-receive hook, for example); ``exit N`` alone names nothing.
    """
    stderr = result.stderr.strip().splitlines()
    if stderr:
        return stderr[-1]
    stdout = result.stdout.strip().splitlines()
    if stdout:
        return stdout[-1]
    return f"exit {result.returncode}"


def _log_push_failure(remote: str, detail: str) -> None:
    """Log a ``notes-push-failed`` event naming the checkout when known.

    ``git_dir`` matches ``unhooked-repo`` so ``doctor`` can point at the
    failing checkout the way it points at unhooked ones.
    """
    fields: dict[str, str] = {"remote": remote, "detail": detail}
    git_dir = _git_dir(os.getcwd())
    if git_dir is not None:
        fields["git_dir"] = str(git_dir)
    _log_event("notes-push-failed", **fields)


def cmd_push_notes(remote: str) -> int:
    """pre-push: reconcile then push the notes ref, best-effort.

    A final failure prints one stderr line and logs a ``notes-push-failed``
    event to the local attribution log — a stderr line inside ``git push``
    output is not a signal anyone sees.

    Guarded against recursion (our own `git push` fires pre-push again) via an
    environment variable rather than parsing the hook's stdin, so it composes
    with other pre-push hooks that may have consumed stdin already.
    """
    try:
        if os.environ.get(PUSH_GUARD_ENV):
            return 0
        if _git(["rev-parse", "--verify", "--quiet", NOTES_REF]) is None:
            return 0  # nothing to push
        result = _reconciled_push(remote)
        if result.returncode != 0:
            detail = _push_failure_detail(result)
            print(
                f"sediment-attribution: notes push to {remote} failed "
                f"(push continues): {detail}",
                file=sys.stderr,
            )
            _log_push_failure(remote, detail)
    except Exception:
        pass  # never abort the developer's push
    return 0


def cmd_repair_notes(remote: str) -> int:
    """Operator command: reconcile the notes ref with ``remote`` and push.

    The on-demand fix for a machine already in the diverged state, safe to
    run any time — fetch, union merge, push are each idempotent. On a fresh
    machine with no local notes ref it adopts the remote's. Unlike the
    hooks this is NOT best-effort: it reports what happened and exits
    non-zero on failure so operators and scripts can trust the result.
    """
    if _git(["rev-parse", "--is-inside-work-tree"]) != "true":
        print("repair-notes: not inside a git work tree", file=sys.stderr)
        return 1
    if not _reconcile_notes(remote):
        return 1
    if _git(["rev-parse", "--verify", "--quiet", NOTES_REF]) is None:
        print(f"repair-notes: no notes ref locally or on {remote}; nothing to do")
        return 0
    result = _reconciled_push(remote)
    if result.returncode != 0:
        detail = _push_failure_detail(result)
        print(f"repair-notes: push to {remote} failed: {detail}", file=sys.stderr)
        _log_push_failure(remote, detail)
        return 1
    print(
        f"{ui.glyph('✓', 'phosphor')}repair-notes: notes ref reconciled "
        f"and pushed to {remote}"
    )
    return 0


# ── doctor ────────────────────────────────────────────────────────────────
#
# `doctor` detects missing agent-clone hooks, diverged notes refs, and hook
# sets missing prepare-commit-msg by inspecting local configuration and refs.
#
# Read-only by default: it never writes a ref, a config, or a hook. The one
# state it cannot classify without a write is the notes ref when the remote
# tip is not a local object — `--fetch` opts into the tracking-ref fetch
# that resolves it, and is the flag a diverged machine needs (see
# ``_doctor_notes_state``).

# Status words. Only DOCTOR_FAIL moves the exit code.
DOCTOR_FAIL = "FAIL"
DOCTOR_OK = "ok"
# `info` is a fact the operator may want and no verdict — an unset fleet
# templateDir, unpushed notes. Never counts toward the exit code: a doctor
# that goes red for normal states is a doctor operators learn to ignore.
DOCTOR_INFO = "info"

# Any invocation generation: *attribution.py (legacy script or packaged
# module) or the installed `sediment` executable — doctor existence-checks
# whichever one the hook references.
_SCRIPT_PATH_RE = re.compile(
    r'"([^"]*(?:(?:attribution|transcript)\.py|/sediment(?:\.exe)?))"'
)
# Log events worth surfacing: each one records a stamp that did not happen.
_DOCTOR_LOG_EVENTS = (
    "unhooked-repo",
    "notes-push-failed",
    "config-error",
    *_CAPTURE_FAILURE_REASONS,
)

Finding = tuple[str, str, str]


def _parse_instant(value: str) -> datetime | None:
    """An offset-aware datetime from an ISO-8601 timestamp, or None.

    Both timestamps doctor compares are written by tools that always emit an
    offset (``_now_iso`` in UTC, git ``%cI`` in the committer's local zone),
    so a naive result means the input was not one of ours — treated as
    unreadable rather than silently assumed to be UTC.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _max_instant(values: Iterable[str]) -> datetime | None:
    parsed = [instant for value in values if (instant := _parse_instant(value))]
    return max(parsed) if parsed else None


def _hook_block_body(hook_file: Path) -> str | None:
    """Our marked block's contents in ``hook_file``, or None when absent.

    A missing file, a binary one, and a file whose block was deleted by hand
    all read the same: absent. Callers pass the path under the hooks dir git
    will actually execute (``_hooks_dir``), never a guessed one.
    """
    try:
        content = hook_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = _HOOK_BLOCK_RE.search(content)
    return match.group(1) if match else None


def _referenced_script(block: str) -> Path | None:
    """The stamper path a hook block or agent hook command invokes.

    Both invocation builders quote it (``_script_invocation`` uses
    ``sys.executable``, ``_fleet_invocation`` a PATH ``python3``), so one
    quoted-path match covers per-repo and fleet installs alike.
    """
    match = _SCRIPT_PATH_RE.search(block)
    return Path(match.group(1)) if match else None


def _iter_hooks(blocks: list) -> Iterator[dict]:
    """Every hook entry inside an agent config's event list. Shapes that are
    not ours to read are skipped rather than raised on — a hand-edited config
    must never crash install, uninstall, or doctor."""
    for block in blocks:
        if isinstance(block, dict):
            for hook in block.get("hooks", []):
                if isinstance(hook, dict):
                    yield hook


def _agent_hook_commands(path: Path, event: str) -> list[str] | None:
    """Our ``mark`` commands under ``event`` in an agent config.

    ``[]`` means the file parsed and holds none of ours; None means the file
    exists but cannot be trusted (unreadable, invalid JSON, wrong shape) —
    a different finding than absent, and the same refusal ``install`` makes.
    """
    if not path.exists():
        return []
    config = _load_json(path)
    if config is None:
        return None
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        return None
    blocks = hooks.get(event, [])
    if not isinstance(blocks, list):
        return None
    return [
        hook["command"]
        for hook in _iter_hooks(blocks)
        if isinstance(hook.get("command"), str)
        and _is_sediment_command(hook["command"])
    ]


def _doctor_agent_hook(
    check: str,
    paths: list[Path],
    subcommand: str,
    *,
    event: str = "PostToolUse",
    required: bool = True,
) -> Finding:
    """One finding for an agent hook entry that may live in several files.

    Claude Code reads an MDM-managed file *and* the per-user one; either
    carrying the entry means the agent marks. A file that does not parse is
    reported as its own failure rather than counted as absent — that is the
    state where ``install`` refuses to write and says nothing else.

    An agent this machine does not have reports info, never FAIL —
    ``install`` skips it on the same presence gate, and a doctor that goes
    red for a Codex-less machine gets ignored (``_doctor_pi_extension``
    already carries this rule).
    """
    if not any(path.parent.exists() for path in paths):
        return (
            DOCTOR_INFO,
            check,
            f"not detected ({paths[-1].parent} does not exist)",
        )
    searched = []
    for path in paths:
        commands = _agent_hook_commands(path, event)
        if commands is None:
            return (
                DOCTOR_FAIL,
                check,
                f"{path} is not valid JSON — install refuses it",
            )
        matching = [
            c
            for c in commands
            if subcommand in c
            or (
                subcommand.startswith(" transcript ")
                and subcommand.removeprefix(" transcript") in c
                and any(
                    tag in c
                    for tag in ("sediment_transcript.py", "sediment_cli/transcript.py")
                )
            )
        ]
        if matching:
            script = _referenced_script(matching[0])
            if script is not None and not script.exists():
                return (
                    DOCTOR_FAIL,
                    check,
                    f"{path} invokes {script}, which does not exist — "
                    "re-run install to repoint it",
                )
            return (DOCTOR_OK, check, f"present in {path}")
        searched.append(str(path))
    if required:
        return (
            DOCTOR_FAIL,
            check,
            f"no '{subcommand}' entry in {' or '.join(searched)} — "
            "sessions are never marked; run install",
        )
    return (
        DOCTOR_INFO,
        check,
        f"not installed in {' or '.join(searched)} (opt in with --transcripts)",
    )


def _doctor_fleet(findings: list[Finding]) -> None:
    """Check ``init.templateDir`` — the fleet's only stamping mechanism.

    Reads the *effective* value rather than the system scope alone: a global
    or repo-level override is what git will really use when cloning, and a
    fleet check that misses the override reports a template that is not
    actually in play.
    """
    template_dir = _git(["config", "--get", "init.templateDir"])
    if not template_dir:
        findings.append(
            (
                DOCTOR_INFO,
                "fleet template",
                "init.templateDir unset — not a fleet machine",
            )
        )
        return
    if not _is_our_template_dir(template_dir):
        findings.append(
            (
                DOCTOR_FAIL,
                "fleet template",
                f"init.templateDir is {template_dir}, whose post-commit is not "
                "sediment's — new clones will not stamp",
            )
        )
        return
    block = _hook_block_body(Path(template_dir) / "hooks" / "post-commit")
    script = _referenced_script(block or "")
    if script is None:
        findings.append(
            (
                DOCTOR_FAIL,
                "fleet template",
                f"{template_dir} post-commit names no stamper script — "
                "re-run install --fleet",
            )
        )
        return
    if not script.exists():
        findings.append(
            (
                DOCTOR_FAIL,
                "fleet template",
                f"{template_dir} invokes {script}, which does not exist — "
                "every clone stamps nothing",
            )
        )
        return
    # Byte comparison, because the script carries no version constant. A
    # difference is not a failure: doctor may be running from a checkout at a
    # different revision than the deployed copy, which is normal and says
    # nothing about which is newer.
    running = Path(__file__).resolve()
    try:
        differs = (
            script.resolve() != running and script.read_bytes() != running.read_bytes()
        )
    except OSError as exc:
        # e.g. a root-owned 0600 deployed copy under an unprivileged doctor
        # run. Degrade to a finding like every sibling read; a traceback here
        # would swallow the rest of the report.
        findings.append(
            (
                DOCTOR_INFO,
                "fleet template",
                f"could not read {script} to compare revisions: {exc}",
            )
        )
        return
    if differs:
        findings.append(
            (
                DOCTOR_INFO,
                "fleet template",
                f"{script} differs from the running {running} — "
                "confirm which revision the fleet should be on",
            )
        )
        return
    findings.append((DOCTOR_OK, "fleet template", f"{template_dir} invokes {script}"))


def _doctor_log(findings: list[Finding]) -> None:
    """Summarize the attribution log's misses, and name the repos to re-run in.

    Informational on purpose. These are historical events, and the live
    checks above and below decide the exit code: failing on the log would
    keep doctor red long after the cause was fixed, until someone deleted a
    file. The value here is the pointer — an unhooked repo nobody passed to
    doctor is otherwise invisible, which is exactly how it stayed hidden for
    months.
    """
    path = _log_path()
    counts: dict[str, int] = {}
    repos: list[str] = []
    stamps: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        findings.append((DOCTOR_INFO, "attribution log", f"{path}: no events recorded"))
        return
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        event = entry.get("event")
        if event not in _DOCTOR_LOG_EVENTS:
            continue
        counts[event] = counts.get(event, 0) + 1
        # Parsed, not string-compared: the log is append-only UTC today, but
        # a string max would silently mis-order the moment it is not.
        stamps.append(str(entry.get("at", "")))
        git_dir = entry.get("git_dir")
        if (
            event == "unhooked-repo"
            and isinstance(git_dir, str)
            and git_dir not in repos
        ):
            repos.append(git_dir)
    if not counts:
        findings.append((DOCTOR_INFO, "attribution log", f"{path}: no misses recorded"))
        return
    summary = ", ".join(f"{count} {event}" for event, count in sorted(counts.items()))
    latest = _max_instant(stamps)
    detail = summary
    if latest is not None:
        detail += f" (most recent {latest.isoformat(timespec='seconds')})"
    if repos:
        detail += f"; unhooked: {', '.join(repos)} — pass these to doctor or install"
    findings.append((DOCTOR_INFO, "attribution log", detail))


def _doctor_hooks(findings: list[Finding], label: str, repo: Path) -> None:
    """Check that all three git hooks carry a block that can actually run.

    Deliberately not an exact match against what ``install`` would write
    today: a hook installed by a different interpreter, or by a fleet copy
    at a different prefix, is correct. What must hold is that the block
    exists, invokes the right subcommand, and names a script still on disk —
    the three ways a hook set goes stale (missing entry, wrong entry, moved
    checkout).
    """
    hooks_dir = _hooks_dir(repo)
    if hooks_dir is None:
        findings.append(
            (
                DOCTOR_FAIL,
                f"hooks[{label}]",
                "cannot resolve the hooks dir git would run",
            )
        )
        return
    broken = []
    for name, invocation in _REPO_HOOKS:
        subcommand = invocation.split()[0]  # the block also carries "$1"/"$2"
        block = _hook_block_body(hooks_dir / name)
        if block is None:
            broken.append(f"{name} missing")
            continue
        if subcommand not in block:
            broken.append(f"{name} does not invoke {subcommand}")
            continue
        script = _referenced_script(block)
        if script is None:
            broken.append(f"{name} names no stamper script")
        elif not script.exists():
            broken.append(f"{name} invokes {script}, which does not exist")
    if broken:
        findings.append(
            (
                DOCTOR_FAIL,
                f"hooks[{label}]",
                f"{'; '.join(broken)} (in {hooks_dir}) — run install {repo}",
            )
        )
        return
    findings.append((DOCTOR_OK, f"hooks[{label}]", f"all three current in {hooks_dir}"))


def _doctor_rewrite_ref(findings: list[Finding], label: str, repo: Path) -> None:
    values = (
        _git(["config", "--get-all", "notes.rewriteRef"], repo) or ""
    ).splitlines()
    if NOTES_REF in values:
        findings.append((DOCTOR_OK, f"notes.rewriteRef[{label}]", NOTES_REF))
        return
    findings.append(
        (
            DOCTOR_FAIL,
            f"notes.rewriteRef[{label}]",
            f"{NOTES_REF} not configured — amend and rebase drop the note; "
            f"run install {repo}",
        )
    )


def _doctor_notes_state(
    findings: list[Finding], label: str, repo: Path, fetch: bool
) -> None:
    """Classify the notes ref against the remote's, without writing by default.

    The diverged state — local and remote notes histories with no common
    ancestor — makes every push a silent no-op forever. Classifying it needs
    both tips as local objects, and the machines that have the bug are
    precisely the ones that never fetched the remote's notes, so the honest
    default is to say the tip is unresolvable and name the flag that
    resolves it. ``--fetch`` writes only ``NOTES_REMOTE_TRACKING_REF``, the
    same ref ``push-notes`` already owns; the real notes ref is untouched.
    """
    check = f"notes ref[{label}]"
    remote = _git(["remote", "get-url", "origin"], repo)
    local = _git(["rev-parse", "--verify", "--quiet", NOTES_REF], repo)
    if not remote:
        state = "local only" if local else "none yet"
        findings.append((DOCTOR_INFO, check, f"no origin remote; {state} locally"))
        return
    listing = _git(["ls-remote", "origin", NOTES_REF], repo)
    if listing is None:
        # None is command failure (offline, auth, deleted remote) — a
        # different fact from an empty listing, and asserting "no notes ref
        # on origin yet" here would report green on the one machine state
        # this check exists to surface (divergence behind a broken remote).
        findings.append(
            (DOCTOR_INFO, check, "could not reach origin to check the notes ref")
        )
        return
    remote_sha = listing.split()[0] if listing else None
    if remote_sha is None:
        detail = "no notes ref on origin yet"
        findings.append(
            (DOCTOR_INFO, check, f"{detail}; local ref exists" if local else detail)
        )
        return
    if local is None:
        findings.append(
            (
                DOCTOR_INFO,
                check,
                f"nothing stamped locally yet; origin has {remote_sha[:8]}",
            )
        )
        return
    if local == remote_sha:
        findings.append((DOCTOR_OK, check, f"in sync with origin at {local[:8]}"))
        return
    if _git(["cat-file", "-e", f"{remote_sha}^{{commit}}"], repo) is None:
        if not fetch:
            findings.append(
                (
                    DOCTOR_INFO,
                    check,
                    f"origin is at {remote_sha[:8]}, which is not a local object — "
                    "cannot tell behind from diverged read-only, and this check "
                    "cannot fail without it; re-run with --fetch",
                )
            )
            return
        _git(["fetch", "origin", f"+{NOTES_REF}:{NOTES_REMOTE_TRACKING_REF}"], repo)
        if _git(["cat-file", "-e", f"{remote_sha}^{{commit}}"], repo) is None:
            findings.append(
                (
                    DOCTOR_FAIL,
                    check,
                    f"could not fetch origin's notes ref ({remote_sha[:8]})",
                )
            )
            return
    ahead = _git(["rev-list", "--count", f"{remote_sha}..{local}"], repo)
    behind = _git(["rev-list", "--count", f"{local}..{remote_sha}"], repo)
    if ahead is None or behind is None:
        findings.append((DOCTOR_FAIL, check, "could not compare the notes refs"))
        return
    if behind != "0" and ahead != "0":
        findings.append(
            (
                DOCTOR_FAIL,
                check,
                f"DIVERGED from origin ({ahead} local, {behind} remote commits) — "
                "every push silently drops stamps; fix with "
                f"repair-notes in {repo}",
            )
        )
        return
    if ahead != "0":
        findings.append(
            (
                DOCTOR_INFO,
                check,
                f"{ahead} commit(s) ahead of origin — the next push carries them",
            )
        )
        return
    findings.append(
        (
            DOCTOR_INFO,
            check,
            f"{behind} commit(s) behind origin — the next push reconciles",
        )
    )


def _doctor_markers(findings: list[Finding], label: str, repo: Path) -> None:
    """Inspect pending generations without writing or guessing consumption.

    Legacy generation-free markers retain their timestamp-based inspection.
    """
    check = f"markers[{label}]"
    git_dir = _git_dir(repo)
    if git_dir is None:
        return  # cmd_doctor already reported this path as not a work tree
    markers = _read_markers(git_dir / MARKER_FILENAME)
    if not markers:
        findings.append((DOCTOR_OK, check, "no unconsumed markers"))
        return
    if any("generation" in marker for marker in markers):
        findings.append(
            (
                DOCTOR_INFO,
                check,
                f"{len(markers)} pending generation(s) — inspect the commit note before retrying; "
                "marker timestamps do not establish consumption",
            )
        )
        return
    head_at = _git(["log", "-1", "--format=%cI", "HEAD"], repo)
    if not head_at:
        findings.append(
            (DOCTOR_INFO, check, f"{len(markers)} marker(s), no commit yet")
        )
        return
    # Compare instants, never the strings. Markers are stamped in UTC and git
    # reports %cI in the committer's local offset, so string ordering calls a
    # marker from 12:57+00:00 "older" than a commit at 20:47+09:00 — the same
    # instant, two hours apart, and a false consumption failure on every
    # machine east of UTC.
    newest_at = _max_instant(str(m.get("stamped_at", "")) for m in markers)
    head_instant = _parse_instant(head_at)
    if newest_at is None or head_instant is None:
        findings.append(
            (DOCTOR_INFO, check, f"{len(markers)} marker(s) with no readable timestamp")
        )
        return
    newest = newest_at.isoformat(timespec="seconds")
    if newest_at < head_instant:
        findings.append(
            (
                DOCTOR_FAIL,
                check,
                f"{len(markers)} marker(s) last written {newest}, older than HEAD "
                f"({head_at}) — the commit did not consume them; check the "
                "post-commit hook",
            )
        )
        return
    findings.append(
        (
            DOCTOR_INFO,
            check,
            f"{len(markers)} marker(s) newer than HEAD — not yet committed",
        )
    )


def _doctor_pi_extension() -> Finding:
    """The pi shim's registration finding.

    pi is an opt-in second harness: a machine without it reports info, never
    FAIL (a doctor that goes red for normal states gets ignored). pi present
    but the shim unregistered is FAIL — those sessions are never marked.

    Installed CLIs carry the same MIT extension as source checkouts.
    """
    path = _pi_settings_path()
    if not path.parent.exists():
        return (DOCTOR_INFO, "pi extension", "pi not detected (no ~/.pi/agent)")
    config = _load_json(path)
    if config is None:
        return (
            DOCTOR_FAIL,
            "pi extension",
            f"{path} is not valid JSON — install refuses it",
        )
    shim = _pi_extension_dir()
    if shim is None:
        return (
            DOCTOR_FAIL,
            "pi extension",
            "extension files missing — reinstall sediment-cli, then run install",
        )
    extensions = config.get("extensions")
    if isinstance(extensions, list) and str(shim) in extensions:
        return (DOCTOR_OK, "pi extension", f"registered in {path}")
    return (
        DOCTOR_FAIL,
        "pi extension",
        f"not registered in {path} — pi sessions are never marked; run install",
    )


def _doctor_cursor_hooks() -> Finding:
    path = _cursor_hooks_path()
    if not path.parent.exists():
        return (DOCTOR_INFO, "cursor hooks", "Cursor not detected (no ~/.cursor)")
    config = _load_json(path)
    if config is None:
        return (
            DOCTOR_FAIL,
            "cursor hooks",
            f"{path} is not valid JSON — install refuses it",
        )
    version = config.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        return (
            DOCTOR_FAIL,
            "cursor hooks",
            f"{path} has unsupported Cursor hook version {version!r}",
        )
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        return (DOCTOR_FAIL, "cursor hooks", f"{path} has a non-object 'hooks' key")
    for event, matcher in _CURSOR_HOOKS.items():
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            return (DOCTOR_FAIL, "cursor hooks", f"{path} has a non-list {event}")
        entry_error = _cursor_hook_entries_error(entries)
        if entry_error is not None:
            return (
                DOCTOR_FAIL,
                "cursor hooks",
                f"{path} has invalid {event} entry: {entry_error}",
            )
        ours = [entry for entry in entries if _is_cursor_hook(entry)]
        if len(ours) != 1:
            return (
                DOCTOR_FAIL,
                "cursor hooks",
                f"{event} has {len(ours)} Sediment entries; run install",
            )
        script = _referenced_script(ours[0]["command"])
        if script is not None and not script.exists():
            return (
                DOCTOR_FAIL,
                "cursor hooks",
                f"{path} invokes {script}, which does not exist — re-run install",
            )
        desired = _cursor_hook_entry(_script_invocation("cursor-hook"), matcher)
        if ours[0].get("command") != desired["command"]:
            return (
                DOCTOR_FAIL,
                "cursor hooks",
                f"{event} has a stale command; run install",
            )
        if ours[0] != desired:
            return (
                DOCTOR_FAIL,
                "cursor hooks",
                f"{event} has a stale matcher or shape; run install",
            )
    return (DOCTOR_OK, "cursor hooks", f"present in {path}")


def cmd_doctor(
    repos: list[str],
    fetch: bool,
    agent: str | None = None,
    session_id: str | None = None,
    transcripts: bool = False,
    inference_calls: bool = False,
) -> int:
    """One-shot client-side health check. Exits 1 when any check FAILs.

    Exit codes: 0 every check passed (``info`` findings do not count), 1 at
    least one FAIL, 2 bad arguments (argparse). Suitable for an
    MDM-scheduled script: one line per finding on stdout, verdict in the
    exit code.
    """
    findings: list[Finding] = []
    for harness, paths in (
        ("claude-code", _claude_settings_candidates()),
        ("codex", [_codex_hooks_path()]),
    ):
        if agent is not None and agent != harness:
            continue
        findings.append(
            _doctor_agent_hook(f"{harness} hook", paths, f"mark --tool {harness}")
        )
        findings.append(
            _doctor_agent_hook(
                f"{harness} transcript hook",
                paths,
                f" transcript --agent {harness}",
                event="SessionEnd",
                required=transcripts,
            )
        )
        if harness == "claude-code":
            findings.append(
                _doctor_agent_hook(
                    "claude-code snapshot hook",
                    paths,
                    " transcript snapshot --agent claude-code",
                    event="PreToolUse",
                    required=False,
                )
            )
    if agent in (None, "cursor"):
        findings.append(_doctor_cursor_hooks())
    if agent in (None, "pi"):
        findings.append(_doctor_pi_extension())
    if agent is not None:
        required_checks = {f"{agent} hook", "cursor hooks", "pi extension"}
        if transcripts:
            required_checks.add(f"{agent} transcript hook")
        findings = [
            (
                DOCTOR_FAIL
                if status == DOCTOR_INFO and check in required_checks
                else status,
                check,
                detail,
            )
            for status, check, detail in findings
        ]
        if agent == "pi" and transcripts:
            enabled = os.environ.get("SEDIMENT_PI_TRANSCRIPTS") == "1"
            findings.append(
                (
                    DOCTOR_OK if enabled else DOCTOR_FAIL,
                    "pi transcript opt-in",
                    "enabled"
                    if enabled
                    else "set SEDIMENT_PI_TRANSCRIPTS=1 in the pi environment",
                )
            )
    _doctor_fleet(findings)
    _doctor_log(findings)
    if agent is None:
        _doctor_server(findings)
    findings.append(
        _doctor_capture_endpoint(required=agent in ("cursor", "pi") or transcripts)
    )
    findings.append(_doctor_delivery(os.environ.get("SEDIMENT_DELIVERY_DIR")))
    for repo in repos:
        path = Path(repo).resolve()
        label = str(path)
        if _git_dir(path) is None:
            findings.append((DOCTOR_FAIL, f"repo[{label}]", "not a git work tree"))
            continue
        _doctor_hooks(findings, label, path)
        _doctor_rewrite_ref(findings, label, path)
        _doctor_notes_state(findings, label, path, fetch)
        _doctor_markers(findings, label, path)
        if agent is not None and session_id is not None:
            _doctor_session_evidence(
                findings, path, agent, session_id, transcripts, inference_calls
            )
    width = max(len(status) for status, _, _ in findings)
    for status, check, detail in findings:
        # info stays neutral — phosphor is for passing checks only.
        color = {DOCTOR_FAIL: "iron-oxide", DOCTOR_OK: "phosphor"}.get(status, "dim")
        print(f"{ui.style(f'{status:<{width}}', color)}  {check}: {detail}")
    failures = sum(1 for status, _, _ in findings if status == DOCTOR_FAIL)
    if failures:
        # Flush first: the two streams are separately buffered, so without
        # this the verdict lands above the findings it summarizes whenever
        # stdout is a pipe (the MDM-script case).
        sys.stdout.flush()
        verdict = ui.style(
            f"{failures} check(s) failed.", "iron-oxide", stream=sys.stderr
        )
        print(f"\n{verdict}", file=sys.stderr)
    return 1 if failures else 0


def _sediment_executable() -> str | None:
    """The selected ``sediment`` command, or None on a bare checkout.

    Resolved at INSTALL time and embedded absolute: hook runtime PATH (a
    Dock-launched agent, a bare git env) cannot be trusted to contain the
    user's tool bin dir. Honor an explicitly invoked CLI, including a retained
    source checkout. Bare scripts skip unrelated virtualenv shims on PATH.
    # ponytail: ".venv" name test covers uv's convention; a custom-named
    # venv would need sys.prefix probing — add it when someone hits it.
    """
    invoked = Path(sys.argv[0]).absolute()
    if invoked.name == "sediment" and invoked.is_file() and os.access(invoked, os.X_OK):
        return str(invoked)
    for d in os.get_exec_path():
        cand = Path(d) / "sediment"
        if ".venv" in cand.parts:
            continue
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _repo_root_file(*relpath: str) -> Path | None:
    """Resolve a checkout file by walking up from this module. Returns None
    when installed from a wheel, where checkout-only assets are absent."""
    here = Path(__file__).resolve()
    for parent in here.parents[:5]:
        cand = parent.joinpath(*relpath)
        if cand.exists():
            return cand
    return None


def _script_invocation(subcommand: str) -> str:
    # Prefer the selected `sediment` command. Source installations must retain
    # their checkout while its hooks are enrolled. Fallback for bare scripts:
    # this file via
    # sys.executable, not a PATH-dependent `python3` — hooks run in whatever
    # environment git/the agent provides, where python3 may be absent.
    exe = _sediment_executable()
    if exe:
        return f'"{exe}" {subcommand} || true'
    script = Path(__file__).resolve()
    return f'"{sys.executable}" "{script}" {subcommand} || true'


def _hooks_dir(repo: Path) -> Path | None:
    """The hooks dir git will actually execute from, or None if unresolvable.

    ``--git-path hooks`` is authoritative: it applies core.hooksPath (including
    tilde expansion, which a naive config read misses) and resolves the COMMON
    git dir in linked worktrees — where ``--absolute-git-dir`` points at the
    per-worktree dir whose hooks/ git never runs.
    """
    out = _git(["rev-parse", "--path-format=absolute", "--git-path", "hooks"], repo)
    return Path(out) if out else None


# Interpreters whose scripts can safely receive an appended sh block. A
# substring test would wrongly admit fish/pwsh (both contain "sh"), whose
# syntax our block would break.
_POSIX_SHELLS = frozenset({"sh", "bash", "dash", "ash", "ksh", "zsh"})


def _sh_compatible(content: str) -> bool:
    """True if a hook script can safely receive an appended sh block."""
    first = content.splitlines()[0] if content else ""
    if not first.startswith("#!"):
        return True  # git runs shebang-less hooks through sh
    parts = first[2:].strip().split()
    if not parts:
        return False
    interpreter = Path(parts[0]).name
    if interpreter == "env":
        rest = [p for p in parts[1:] if not p.startswith("-")]  # skip env flags
        interpreter = Path(rest[0]).name if rest else ""
    return interpreter in _POSIX_SHELLS


def _install_hook_block(hook_file: Path, command: str) -> bool:
    """Insert/replace our marked block in a hook script, preserving the rest.

    Refuses (returns False) when the existing hook is not an sh script —
    appending sh to a python/binary hook would break every commit/push, the
    one thing the stamper must never do.
    """
    block = f"{HOOK_BLOCK_BEGIN}\n{command}\n{HOOK_BLOCK_END}\n"
    if hook_file.exists():
        try:
            content = hook_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            _warn(
                f"{hook_file} is not a text hook script; "
                "not modified — install this hook manually"
            )
            return False
        if not _sh_compatible(content):
            _warn(
                f"{hook_file} is not an sh script; "
                "not modified — install this hook manually"
            )
            return False
        if _HOOK_BLOCK_RE.search(content):
            # Replace via a function, not the string form: `block` embeds an
            # absolute script path, so a literal `\` or a `\g`/`\1` sequence in
            # it would be parsed as a replacement escape and raise re.error
            # (breaks the re-install path on Windows paths like C:\…).
            content = _HOOK_BLOCK_RE.sub(lambda _m: block, content)
        else:
            if content and not content.endswith("\n"):
                content += "\n"
            content += block
    else:
        content = f"#!/bin/sh\n{block}"
    hook_file.parent.mkdir(parents=True, exist_ok=True)
    hook_file.write_text(content, encoding="utf-8")
    hook_file.chmod(hook_file.stat().st_mode | 0o755)
    return True


def _remove_hook_block(hook_file: Path) -> None:
    if not hook_file.exists():
        return
    try:
        content = hook_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return  # not a text hook we ever wrote to
    hook_file.write_text(_HOOK_BLOCK_RE.sub("", content), encoding="utf-8")


def _claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _codex_hooks_path() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home else Path.home() / ".codex") / "hooks.json"


def _cursor_hooks_path() -> Path:
    return Path.home() / ".cursor" / "hooks.json"


def _pi_settings_path() -> Path:
    return Path.home() / ".pi" / "agent" / "settings.json"


def _pi_extension_dir() -> Path | None:
    """Resolve the packaged MIT extension, or the editable checkout's shim."""
    packaged = Path(__file__).resolve().parent / "_pi"
    if (packaged / "index.ts").is_file():
        return packaged
    return _repo_root_file("shims", "pi")


def _load_json(path: Path) -> dict | None:
    """Load an agent config: {} for a missing file, None for one we must not
    touch (unreadable, invalid JSON, or non-object top level).

    The None path is load-bearing: writing back a default over a file that
    merely failed to parse would destroy the user's settings.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write via temp file + rename so a crash can't truncate the config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".sediment-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _is_our_hook(hook: dict) -> bool:
    """True if this hook entry is one we installed (stamper or transcript
    extractor). Tolerates a non-string ``command`` (a malformed-but-parseable
    config): ``in`` against a non-str would raise TypeError and crash
    install/uninstall — the best-effort installer must never do that."""
    command = hook.get("command", "")
    return isinstance(command, str) and _is_sediment_command(command)


def _is_cursor_hook(hook: object) -> bool:
    if not isinstance(hook, dict):
        return False
    command = hook.get("command")
    return (
        isinstance(command, str)
        and "cursor-hook" in command
        and _is_sediment_command(command)
    )


def _has_our_entry(blocks: list) -> bool:
    return any(_is_our_hook(hook) for hook in _iter_hooks(blocks))


def _install_agent_entry(
    path: Path, block: dict, label: str, event: str = "PostToolUse"
) -> str:
    """Add a hook block to an agent's JSON config; returns a status word.

    Refuses to touch a config it can't fully parse ("skipped") — never
    rewrites a file it didn't understand.
    """
    config = _load_json(path)
    if config is None:
        _warn(
            f"{path} exists but is not valid JSON; {label} hook NOT "
            "installed — fix the file and re-run install"
        )
        return "skipped"
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        _warn(
            f"{path} has a non-object 'hooks' key; {label} hook NOT "
            "installed — fix the file and re-run install"
        )
        return "skipped"
    blocks = hooks.setdefault(event, [])
    if not isinstance(blocks, list):
        _warn(
            f"{path} has a non-list {event}; {label} hook NOT "
            "installed — fix the file and re-run install"
        )
        return "skipped"
    ours = [hook for hook in _iter_hooks(blocks) if _is_our_hook(hook)]
    if ours:
        # An existing entry may point at an old script path (moved checkout,
        # fleet prefix migration). "Already present" would leave a stale
        # command that silently no-ops behind `|| true` — update it in place.
        desired = block["hooks"][0]["command"]
        if all(hook.get("command") == desired for hook in ours):
            return "already present"
        for hook in ours:
            hook["command"] = desired
        _write_json_atomic(path, config)
        return "updated"
    blocks.append(block)
    _write_json_atomic(path, config)
    return "added"


def _claude_hook_block(command: str) -> dict:
    """The Claude Code PostToolUse entry — the one place CLAUDE_MATCHER is
    wired into a fragment (per-user install and the fleet bundle both build
    from here, so the doc'd MDM fragment can never drift from the installer)."""
    return {
        "matcher": CLAUDE_MATCHER,
        "hooks": [{"type": "command", "command": command}],
    }


def _codex_hook_block(command: str) -> dict:
    return {"hooks": [{"type": "command", "command": command}]}


def _install_claude_hook() -> str:
    if not _claude_settings_path().parent.exists():
        return _skipped(
            "claude-code hook: skipped (Claude Code not detected — "
            f"{_claude_settings_path().parent} does not exist)"
        )
    return _install_agent_entry(
        _claude_settings_path(),
        _claude_hook_block(_script_invocation("mark --tool claude-code")),
        "claude-code",
    )


def _install_codex_hook() -> str:
    if not _codex_hooks_path().parent.exists():
        return _skipped(
            "codex hook: skipped (Codex not detected — "
            f"{_codex_hooks_path().parent} does not exist)"
        )
    return _install_agent_entry(
        _codex_hooks_path(),
        _codex_hook_block(_script_invocation("mark --tool codex")),
        "codex",
    )


def _cursor_hook_entry(command: str, matcher: str | None) -> dict:
    entry = {"command": command}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def _cursor_hook_entries_error(entries: list[object]) -> str | None:
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return f"entry {index} is not an object"
        if not isinstance(entry.get("command"), str):
            return f"entry {index} has a non-string command"
        if "matcher" in entry and not isinstance(entry["matcher"], str):
            return f"entry {index} has a non-string matcher"
    return None


def _install_cursor_hooks() -> str:
    path = _cursor_hooks_path()
    if not path.parent.exists():
        return _skipped(
            "cursor hooks: skipped (Cursor not detected — ~/.cursor does not exist)"
        )
    config = _load_json(path)
    if config is None:
        _warn(
            f"{path} exists but is not valid JSON; cursor hooks NOT "
            "installed — fix the file and re-run install"
        )
        return "skipped"
    has_version = "version" in config
    version = config.get("version")
    if has_version and (
        not isinstance(version, int) or isinstance(version, bool) or version != 1
    ):
        _warn(
            f"{path} has unsupported Cursor hook version {version!r}; cursor hooks "
            "NOT installed"
        )
        return "skipped"
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        _warn(
            f"{path} has a non-object 'hooks' key; cursor hooks NOT installed — "
            "fix the file and re-run install"
        )
        return "skipped"
    for event in _CURSOR_HOOKS:
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            _warn(
                f"{path} has a non-list {event}; cursor hooks NOT installed — "
                "fix the file and re-run install"
            )
            return "skipped"
        entry_error = _cursor_hook_entries_error(entries)
        if entry_error is not None:
            _warn(
                f"{path} has invalid {event} entry: {entry_error}; cursor hooks "
                "NOT installed — fix the file and re-run install"
            )
            return "skipped"

    command = _script_invocation("cursor-hook")
    changed = not has_version
    found_existing = False
    for event, matcher in _CURSOR_HOOKS.items():
        entries = hooks.get(event, [])
        desired = _cursor_hook_entry(command, matcher)
        ours = [entry for entry in entries if _is_cursor_hook(entry)]
        found_existing = found_existing or bool(ours)
        if len(ours) == 1 and ours[0] == desired:
            continue
        hooks[event] = [entry for entry in entries if not _is_cursor_hook(entry)]
        hooks[event].append(desired)
        changed = True
    if not changed:
        return "already present"
    config["version"] = 1
    config["hooks"] = hooks
    try:
        _write_json_atomic(path, config)
    except OSError as exc:
        _warn(
            f"{path} could not be written ({type(exc).__name__}); cursor hooks "
            "NOT installed"
        )
        return "skipped"
    return "updated" if found_existing else "added"


def _install_pi_extension() -> str:
    """Register the shim in pi's ``settings.json`` extensions list.

    pi auto-discovers extension dirs listed there; no hook fragment needed —
    the shim invokes ``mark --tool pi`` itself. Same safety posture as the
    hook installers: a settings file we cannot fully parse is refused,
    never rewritten.

    Gated on pi's config directory existing — the same presence check
    ``_doctor_pi_extension`` uses. Absent → skipped, no file written.
    """
    path = _pi_settings_path()
    if not path.parent.exists():
        return _skipped(
            "pi extension: skipped (pi not detected — ~/.pi/agent does not exist)"
        )
    shim = _pi_extension_dir()
    if shim is None:
        return _skipped(
            "pi extension: skipped (extension files missing — "
            "reinstall sediment-cli, then run install)"
        )
    config = _load_json(path)
    if config is None:
        _warn(
            f"{path} exists but is not valid JSON; pi extension NOT "
            "installed — fix the file and re-run install"
        )
        return "skipped"
    extensions = config.setdefault("extensions", [])
    if not isinstance(extensions, list):
        _warn(
            f"{path} has a non-list 'extensions' key; pi extension "
            "NOT installed — fix the file and re-run install"
        )
        return "skipped"
    entry = str(shim)
    if entry in extensions:
        return "already present"
    extensions.append(entry)
    _write_json_atomic(path, config)
    return "added"


def _remove_pi_extension() -> bool:
    """Drop our entry from pi's extensions list; foreign entries are kept."""
    path = _pi_settings_path()
    config = _load_json(path)
    if config is None:
        return False
    extensions = config.get("extensions")
    if not isinstance(extensions, list):
        return False
    shim = _pi_extension_dir()
    if shim is None:
        return False
    entry = str(shim)
    if entry not in extensions:
        return False
    extensions.remove(entry)
    _write_json_atomic(path, config)
    return True


def _transcript_invocation(subcommand: str = "", *, agent: str = "claude-code") -> str:
    argument = f" {subcommand}" if subcommand else ""
    executable = _sediment_executable()
    if executable is not None:
        return f'"{executable}" transcript{argument} --agent {agent} || true'
    script = _capture_client_path("transcript").resolve()
    return f'"{sys.executable}" "{script}"{argument} --agent {agent} || true'


def _install_transcript_hook() -> str:
    """Opt-in (ADR 0007): the SessionEnd extractor ships edit text pairs,
    so it is never installed implicitly with the stamper's own hooks."""
    invocation = _transcript_invocation()
    return _install_agent_entry(
        _claude_settings_path(),
        {"hooks": [{"type": "command", "command": invocation}]},
        "claude-code transcript",
        event="SessionEnd",
    )


def _install_codex_transcript_hook() -> str:
    """Opt-in Codex SessionEnd extractor for structured patch events."""
    path = _codex_hooks_path()
    if not path.parent.exists():
        return _skipped(
            "codex SessionEnd hook: skipped (Codex not detected — "
            f"{path.parent} does not exist)"
        )
    invocation = _transcript_invocation(agent="codex")
    return _install_agent_entry(
        path,
        {"hooks": [{"type": "command", "command": invocation}]},
        "codex transcript",
        event="SessionEnd",
    )


# Only the edit tools: the snapshot hook reads the file each call is about to
# write, so Bash and the read-only tools have nothing for it to do.
CLAUDE_EDIT_MATCHER = "Edit|Write"


def _install_snapshot_hook() -> str:
    """The external-delta snapshots, same ``--transcripts`` opt-in.

    Bundled with the SessionEnd extractor rather than flagged separately:
    the counts are only ever shipped by that extractor, so installing one
    without the other either produces a cache nothing reads or a session-end
    pass with no windows to report.
    """
    invocation = _transcript_invocation("snapshot")
    return _install_agent_entry(
        _claude_settings_path(),
        {
            "matcher": CLAUDE_EDIT_MATCHER,
            "hooks": [{"type": "command", "command": invocation}],
        },
        "claude-code snapshot",
        event="PreToolUse",
    )


def _remove_agent_entries(path: Path) -> bool:
    """Remove OUR hook entries only; co-resident foreign hooks are kept."""
    config = _load_json(path)
    if config is None:
        return False
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event, blocks in list(hooks.items()):
        if not isinstance(blocks, list):
            continue
        kept_blocks = []
        for block in blocks:
            if not isinstance(block, dict) or not _has_our_entry([block]):
                kept_blocks.append(block)
                continue
            # Drop only our entries; keep any foreign hooks sharing the block.
            foreign = [
                h
                for h in block.get("hooks", [])
                if not (isinstance(h, dict) and _is_our_hook(h))
            ]
            changed = True
            if foreign:
                kept_blocks.append({**block, "hooks": foreign})
        hooks[event] = kept_blocks
    if changed:
        _write_json_atomic(path, config)
    return changed


def _remove_cursor_entries() -> bool:
    path = _cursor_hooks_path()
    config = _load_json(path)
    if config is None or config.get("version", 1) != 1:
        return False
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in _CURSOR_HOOKS:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = [entry for entry in entries if not _is_cursor_hook(entry)]
        if len(kept) != len(entries):
            hooks[event] = kept
            changed = True
    if changed:
        try:
            _write_json_atomic(path, config)
        except OSError as exc:
            _warn(
                f"{path} could not be written ({type(exc).__name__}); cursor "
                "hook entries NOT removed"
            )
            return False
    return changed


# ── agent env wiring ───────────────────────────────────────────────────────
#
# `sediment install` writes the telemetry env the runbook used to ask users
# to copy by hand (docs/operate/deploy.md) — generated from the login
# config, so the missing-OTEL_LOGS_EXPORTER class of copy-paste bug cannot
# recur. Two files, both 0600 (they carry the bearer token):
# a fish conf.d file (auto-loaded, no profile editing) and a POSIX env.sh
# sourced from a marker-guarded block appended to existing profiles.

ENV_BLOCK_BEGIN = "# >>> sediment env >>>"
ENV_BLOCK_END = "# <<< sediment env <<<"
_PROFILE_NAMES = (".zprofile", ".bashrc", ".profile")


def _valid_server_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        candidate = host[:-1] if host.endswith(".") else host
        try:
            candidate = candidate.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        if "." in candidate and all(
            character.isdigit() or character == "." for character in candidate
        ):
            return False
        labels = candidate.split(".")
        return (
            bool(candidate)
            and len(candidate) <= 253
            and all(
                label
                and len(label) <= 63
                and label[0].isalnum()
                and label[-1].isalnum()
                and all(character.isalnum() or character == "-" for character in label)
                for label in labels
            )
        )
    return True


def _safe_server_url(url: str) -> bool:
    """Validate saved config in this standalone stdlib-only fleet file.

    Keep this boundary synchronized with ``client.validate_server_url``.
    """
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        return False
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        host = parsed.hostname
        parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or not _valid_server_host(host)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
    ):
        return False
    if parsed.scheme == "https" or host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback and (
        isinstance(address, ipaddress.IPv4Address)
        or address == ipaddress.IPv6Address("::1")
    )


_CAPTURE_LOGIN = (
    "run sediment login <url> --capture --with-token to enroll an ingest credential"
)


def _cli_config(*, capture: bool = False) -> tuple[str, str] | None:
    """(url, token) for the current server from ``~/.sediment/config.json``
    (written by ``sediment login``) — parsed directly so this module
    stays stdlib-only. None when not logged in."""
    path = Path.home() / ".sediment" / "config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        return None
    current = (
        os.environ.get("SEDIMENT_URL", cfg.get("current"))
        if capture
        else cfg.get("current")
    )
    if not isinstance(current, str) or not current or not _safe_server_url(current):
        return None
    servers = cfg.get("servers", {})
    if not isinstance(servers, dict):
        return None
    entry = servers.get(current)
    if capture:
        override = os.environ.get("SEDIMENT_INGEST_TOKEN")
        if override:
            _verify_capture_override(current, override)
            return current, override
        if entry is None:
            return None
        if (
            not isinstance(entry, dict)
            or entry.get("capture_authority") != "ingest"
            or not isinstance(entry.get("capture_client_id"), str)
            or not entry["capture_client_id"]
        ):
            raise ValueError(_CAPTURE_LOGIN)
        token = entry.get("capture_token")
        if (
            not isinstance(token, str)
            or not token
            or token == entry.get("token")
            or any(ord(c) < 32 or ord(c) == 127 for c in token)
        ):
            raise ValueError(_CAPTURE_LOGIN)
        return current, token
    token = entry.get("token") if isinstance(entry, dict) else None
    if isinstance(token, str) and token:
        return current, token
    return None


def _verify_capture_override(url: str, token: str) -> None:
    """Verify an explicit override with the destination before writing any file."""
    if not _safe_server_url(url) or any(ord(c) < 32 or ord(c) == 127 for c in token):
        raise ValueError("capture override must have verified ingest authority")
    request = urllib.request.Request(
        f"{url.rstrip('/')}/v1/me",
        headers={"Authorization": f"Bearer {token}", "User-Agent": _DOCTOR_USER_AGENT},
    )
    try:
        opener = urllib.request.build_opener(_RejectRedirects())
        with opener.open(request, timeout=5) as response:
            raw = response.read(8193)
        if len(raw) > 8192:
            raise ValueError
        identity = json.loads(raw)
        if (
            not isinstance(identity, dict)
            or identity.get("authority") != "ingest"
            or not isinstance(identity.get("client_id"), str)
            or not identity["client_id"]
        ):
            raise ValueError
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError):
        raise ValueError(
            "capture override must have verified ingest authority"
        ) from None


def _sh_env_file() -> Path:
    return Path.home() / ".sediment" / "env.sh"


def _fish_env_file() -> Path:
    return Path.home() / ".config" / "fish" / "conf.d" / "sediment.fish"


def _env_pairs(
    url: str,
    token: str,
    user_id: str | None,
    gateway_url: str | None,
    gateway_key: str | None,
) -> list[tuple[str, str]]:
    """The §5.1 block, generated. The exporter appends /v1/logs itself, so
    the endpoint is the bare server URL."""
    pairs = [
        ("CLAUDE_CODE_ENABLE_TELEMETRY", "1"),
        ("OTEL_LOGS_EXPORTER", "otlp"),
        ("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json"),
        ("OTEL_EXPORTER_OTLP_ENDPOINT", url),
        ("OTEL_EXPORTER_OTLP_HEADERS", f"Authorization=Bearer {token}"),
        ("OTEL_LOG_TOOL_DETAILS", "1"),
        ("SEDIMENT_INGEST_TOKEN", token),
    ]
    try:
        _transcript_client()._validated_endpoint(url)
        pairs.append(("SEDIMENT_OTLP_ENDPOINT", url))
    except ValueError:
        _warn(f"capture endpoint rejected; {_CAPTURE_ENDPOINT_FORMS}")
    except (ImportError, OSError, RuntimeError, SyntaxError):
        _warn("capture helper unavailable; dedicated hook delivery is disabled")
    if user_id:
        pairs.append(("OTEL_RESOURCE_ATTRIBUTES", f"user.id={user_id}"))
    if gateway_url:
        pairs.append(("ANTHROPIC_BASE_URL", gateway_url))
    if gateway_key:
        pairs.append(("ANTHROPIC_AUTH_TOKEN", gateway_key))
        pairs.append(("SEDIMENT_GATEWAY_KEY", gateway_key))
    return pairs


def _write_0600(path: Path, content: str) -> None:
    """0600 on create AND on rewrite — O_CREAT's mode applies only at
    creation, and these files carry the bearer token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)


def _write_env_files(pairs: list[tuple[str, str]]) -> None:
    header = "# Written by `sediment install` — rewritten on re-run.\n"
    sh = header + "".join(f"export {k}={shlex.quote(v)}\n" for k, v in pairs)
    # fish accepts the same single-quote escaping shlex emits.
    fish = header + "".join(f"set -gx {k} {shlex.quote(v)}\n" for k, v in pairs)
    _write_0600(_sh_env_file(), sh)
    _write_0600(_fish_env_file(), fish)


def _wire_profiles() -> list[Path]:
    """Append the guarded source block to profiles that exist (idempotent);
    when none exist, create .zprofile + .profile so both login-shell
    families pick it up. fish needs nothing — conf.d auto-loads."""
    block = (
        f"\n{ENV_BLOCK_BEGIN}\n"
        '[ -f "$HOME/.sediment/env.sh" ] && . "$HOME/.sediment/env.sh"\n'
        f"{ENV_BLOCK_END}\n"
    )
    targets = [Path.home() / n for n in _PROFILE_NAMES if (Path.home() / n).exists()]
    if not targets:
        targets = [Path.home() / ".zprofile", Path.home() / ".profile"]
    wired: list[Path] = []
    for target in targets:
        content = target.read_text(encoding="utf-8") if target.exists() else ""
        if ENV_BLOCK_BEGIN not in content:
            target.write_text(content + block, encoding="utf-8")
        wired.append(target)
    return wired


def _unwire_env() -> list[str]:
    """Uninstall's inverse: remove the env files and strip the guarded
    blocks. Returns human-readable descriptions of what was removed."""
    removed: list[str] = []
    for path in (_sh_env_file(), _fish_env_file()):
        try:
            path.unlink()
            removed.append(str(path))
        except OSError:
            pass
    # Also eat the blank line _wire_profiles prepends, so wire/unwire cycles
    # leave the profile byte-identical.
    pattern = re.compile(
        r"\n?"
        + re.escape(ENV_BLOCK_BEGIN)
        + r".*?"
        + re.escape(ENV_BLOCK_END)
        + r"\n?",
        re.DOTALL,
    )
    for name in _PROFILE_NAMES:
        profile = Path.home() / name
        try:
            content = profile.read_text(encoding="utf-8")
        except OSError:
            continue
        stripped = pattern.sub("", content)
        if stripped != content:
            profile.write_text(stripped, encoding="utf-8")
            removed.append(f"{profile} (env block)")
    return removed


def cmd_install_env(
    user_id: str | None,
    gateway_url: str | None,
    gateway_key: str | None,
    transcripts: bool = False,
) -> str:
    """Wire the agent telemetry env from the login config; the install
    summary string says what happened."""
    cfg = _cli_config(capture=True)
    if cfg is None:
        return f"skipped (capture credential is absent; {_CAPTURE_LOGIN})"
    url, token = cfg
    pairs = _env_pairs(url, token, user_id, gateway_url, gateway_key)
    try:
        previous = _sh_env_file().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        previous = []
    if transcripts or "export SEDIMENT_PI_TRANSCRIPTS=1" in previous:
        pairs.append(("SEDIMENT_PI_TRANSCRIPTS", "1"))
    directory = _delivery_directory(previous)
    if directory:
        pairs.append(("SEDIMENT_DELIVERY_DIR", directory))
    _write_env_files(pairs)
    wired = _wire_profiles()
    profiles = ", ".join(p.name for p in wired)
    return (
        f"wrote {_sh_env_file()} and {_fish_env_file()} (0600), sourced from "
        f"{profiles}; restart running agent sessions to pick it up"
    )


def _delivery_directory(previous: list[str] | None = None) -> str | None:
    """Preserve explicit consent as literal data; never source saved shell code."""
    if "SEDIMENT_DELIVERY_DIR" in os.environ:
        value = os.environ["SEDIMENT_DELIVERY_DIR"]
    else:
        if previous is None:
            try:
                previous = _sh_env_file().read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                previous = []
        value = ""
        for line in previous:
            if not line.startswith("export SEDIMENT_DELIVERY_DIR="):
                continue
            fields = shlex.split(line)
            if len(fields) != 2 or fields[0] != "export":
                raise ValueError("saved delivery enrollment is invalid")
            value = fields[1].partition("=")[2]
    value = value.strip()
    if any(character in value for character in "\0\r\n"):
        raise ValueError("delivery enrollment is invalid")
    return value or None


def _doctor_delivery(directory: str | None) -> Finding:
    directory = (directory or "").strip()
    if not directory:
        return (DOCTOR_INFO, "delivery", "best_effort; buffering is disabled")
    try:
        helper = _capture_client("delivery")
        if (
            getattr(helper, "FORMAT_VERSION", None) != 1
            or not callable(getattr(helper, "status", None))
            or not callable(getattr(helper, "watch", None))
        ):
            raise RuntimeError
    except (ImportError, OSError, ValueError, RuntimeError, SyntaxError):
        return (
            DOCTOR_FAIL,
            "delivery",
            "helper unavailable; install the matching helper before starting a worker",
        )
    try:
        state = helper.status(directory)
    except (OSError, ValueError, RuntimeError):
        return (
            DOCTOR_FAIL,
            "delivery",
            "private storage is unsafe or unreadable; worker enrollment is incomplete",
        )
    if (
        not isinstance(state, dict)
        or state.get("format_version") != 1
        or type(state.get("worker_running")) is not bool
    ):
        return (
            DOCTOR_FAIL,
            "delivery",
            "helper status is incompatible; install the matching helper before starting a worker",
        )
    path = Path(directory)
    if not path.is_dir():
        return (
            DOCTOR_FAIL,
            "delivery",
            "private storage is missing; start a supervised delivery worker",
        )
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return (
            DOCTOR_FAIL,
            "delivery",
            "private storage is not writable; worker enrollment is incomplete",
        )
    if not state["worker_running"]:
        return (
            DOCTOR_FAIL,
            "delivery",
            "helper available, private writable storage; worker is not running; start delivery replay --watch under a process supervisor",
        )
    return (
        DOCTOR_OK,
        "delivery",
        "buffered; helper available, private writable storage, worker running",
    )


_CAPTURE_ENDPOINT_FORMS = (
    "use HTTPS for remote hosts or HTTP for literal loopback; "
    "use the origin or /v1/logs path without credentials, query, or fragment"
)


def _doctor_capture_endpoint(required: bool = False) -> Finding:
    configured = os.environ.get("SEDIMENT_OTLP_ENDPOINT")
    if not configured:
        return (
            DOCTOR_FAIL if required else DOCTOR_INFO,
            "capture endpoint",
            "SEDIMENT_OTLP_ENDPOINT is unset; dedicated hook delivery is disabled",
        )
    try:
        _transcript_client()._validated_endpoint(configured)
    except ValueError:
        return (DOCTOR_FAIL, "capture endpoint", f"rejected; {_CAPTURE_ENDPOINT_FORMS}")
    except (ImportError, OSError, RuntimeError, SyntaxError):
        return (
            DOCTOR_FAIL,
            "capture endpoint",
            "capture helper unavailable; install the matching capture clients",
        )
    return (DOCTOR_OK, "capture endpoint", "SEDIMENT_OTLP_ENDPOINT is accepted")


_CODEX_OTEL_BEGIN = "# BEGIN SEDIMENT CODEX TELEMETRY"
_CODEX_OTEL_END = "# END SEDIMENT CODEX TELEMETRY"
_CODEX_OTEL_BLOCK = re.compile(
    rf"^{_CODEX_OTEL_BEGIN}\n.*?^{_CODEX_OTEL_END}\n?", re.MULTILINE | re.DOTALL
)


def _without_codex_telemetry(original: str) -> str:
    """Verify that removing the marked table preserves every unmanaged setting."""
    try:
        parsed = tomllib.loads(original)
        managed = _CODEX_OTEL_BLOCK.findall(original)
        if (
            original.count(_CODEX_OTEL_BEGIN) != len(managed)
            or original.count(_CODEX_OTEL_END) != len(managed)
            or len(managed) > 1
            or (managed and "otel" not in parsed)
        ):
            raise ValueError
        remaining = _CODEX_OTEL_BLOCK.sub("", original)
        unmanaged = tomllib.loads(remaining)
        if "otel" in unmanaged or unmanaged != {
            key: value for key, value in parsed.items() if key != "otel"
        }:
            raise ValueError
    except (tomllib.TOMLDecodeError, ValueError):
        raise ValueError(
            "Codex profile contains malformed content or an unmanaged [otel] table; choose another profile"
        ) from None
    return remaining


def _install_codex_profile(name: str) -> Path:
    """Resolve login credentials into one managed table, preserving other settings."""
    cfg = _cli_config(capture=True)
    if cfg is None:
        raise ValueError(_CAPTURE_LOGIN)
    url, token = cfg
    try:
        endpoint = _transcript_client()._validated_endpoint(url)
    except ValueError:
        raise ValueError(
            f"capture endpoint rejected; {_CAPTURE_ENDPOINT_FORMS}"
        ) from None
    except (ImportError, OSError, RuntimeError, SyntaxError):
        raise ValueError(
            "capture helper unavailable; install the matching capture clients"
        ) from None
    if "\r" in token or "\n" in token:
        raise ValueError("configured token cannot be used as an HTTP header")
    path = _codex_hooks_path().with_name(f"{name}.config.toml")
    if path.is_symlink():
        raise ValueError(
            "Codex profile is a symbolic link; choose a regular profile file"
        )
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    remaining = _without_codex_telemetry(original)
    prefix = remaining + ("\n" if remaining and not remaining.endswith("\n") else "")
    block = (
        f'{_CODEX_OTEL_BEGIN}\n[otel]\nenvironment = "sediment"\n'
        "exporter = { otlp-http = { endpoint = "
        + json.dumps(endpoint)
        + ', protocol = "json", headers = { "Authorization" = '
        + json.dumps(f"Bearer {token}")
        + " } } }\nlog_user_prompt = false\n"
        + f"{_CODEX_OTEL_END}\n"
    )
    tomllib.loads(prefix + block)
    _write_0600(path, prefix + block)
    return path


def _uninstall_codex_profiles() -> bool:
    """Remove verified managed telemetry, reporting profiles that need attention."""
    directory = _codex_hooks_path().parent
    try:
        profiles = sorted(
            path for path in directory.iterdir() if path.name.endswith(".config.toml")
        )
    except FileNotFoundError:
        return True
    except OSError:
        _warn(f"Codex telemetry cleanup skipped: cannot read {directory}")
        return False
    complete = True
    for path in profiles:
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError
            original = path.read_text(encoding="utf-8")
            if _CODEX_OTEL_BEGIN not in original and _CODEX_OTEL_END not in original:
                continue
            remaining = _without_codex_telemetry(original)
            if remaining.strip():
                _write_0600(path, remaining)
            else:
                path.unlink()
        except (OSError, UnicodeError, ValueError):
            _warn(
                f"Codex telemetry cleanup skipped {path}: "
                "malformed, unreadable, or not a regular file; inspect it manually"
            )
            complete = False
            continue
        print(f"{ui.glyph('✓', 'phosphor')}removed managed Codex telemetry from {path}")
    return complete


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        # Reject before urllib parses Location; malformed redirect targets can
        # raise ValueError before redirect_request() runs.
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _doctor_session_evidence(
    findings: list[Finding],
    repo: Path,
    agent: str,
    session_id: str,
    transcripts: bool,
    inference_calls: bool,
) -> None:
    """Require visible Session evidence and its local HEAD note, without persisting it."""
    marked = any(
        entry.get("tool") == agent and entry.get("session_id") == session_id
        for entry in _read_note_sessions("HEAD", repo)
    )
    findings.append(
        (
            DOCTOR_OK if marked else DOCTOR_FAIL,
            "commit Session note",
            "observed in HEAD"
            if marked
            else "missing from HEAD for this harness and Session",
        )
    )
    required = [("developer_decision", "Developer decision")]
    for requested, kind, check in (
        (transcripts, "edit_observation", "Edit observation"),
        (inference_calls, "inference_call", "Session Inference call"),
    ):
        if requested:
            if agent == "cursor":
                findings.append((DOCTOR_FAIL, check, "unsupported for Cursor"))
            else:
                required.append((kind, check))
    cfg = _cli_config()
    if cfg is None:
        findings.append(
            (DOCTOR_FAIL, "Session evidence", "not logged in; run sediment login")
        )
        return
    url, token = cfg
    request = urllib.request.Request(
        f"{url.rstrip('/')}/query/session/{urllib.parse.quote(session_id, safe='')}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": _DOCTOR_USER_AGENT},
    )
    try:
        opener = urllib.request.build_opener(_RejectRedirects())
        with opener.open(request, timeout=5) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError
        body = json.loads(raw)
        if not isinstance(body, dict) or not isinstance(body.get("found"), bool):
            raise ValueError
        if not body["found"]:
            findings.append((DOCTOR_FAIL, "Session evidence", "Session is missing"))
            return
        if (
            body.get("session_id") != session_id
            or type(body.get("omitted_events")) is not int
        ):
            raise ValueError
        if body["omitted_events"] != 0:
            findings.append(
                (DOCTOR_FAIL, "Session evidence", "incomplete; some events are omitted")
            )
            return
        timeline = body.get("timeline")
        if not isinstance(timeline, list):
            raise ValueError
        for entry in timeline:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("fact_id"), str)
                or not entry["fact_id"]
                or not isinstance(entry.get("occurred_at"), str)
                or _parse_instant(entry["occurred_at"]) is None
                or entry.get("event_type")
                not in {
                    "inference_call",
                    "developer_decision",
                    "edit_observation",
                    "rejected_edit",
                    "retry_linkage",
                }
            ):
                raise ValueError
    except urllib.error.HTTPError as exc:
        findings.append(
            (DOCTOR_FAIL, "Session evidence", f"HTTP {exc.code}; verification failed")
        )
        return
    except (urllib.error.URLError, TimeoutError, OSError):
        findings.append(
            (DOCTOR_FAIL, "Session evidence", "unreachable; verification failed")
        )
        return
    except (ValueError, TypeError, UnicodeError):
        findings.append(
            (
                DOCTOR_FAIL,
                "Session evidence",
                "unreadable response; verification failed",
            )
        )
        return
    for kind, check in required:
        observed = any(
            entry["event_type"] == kind
            and (kind == "inference_call" or entry.get("agent_harness") == agent)
            for entry in timeline
        )
        findings.append(
            (
                DOCTOR_OK if observed else DOCTOR_FAIL,
                check,
                "observed in this Session" if observed else "missing from this Session",
            )
        )


def _doctor_server(findings: list[Finding]) -> None:
    """Server section: reachability, token validity, version skew.
    stdlib urllib, so the copied-file fleet form stays dependency-free."""
    cfg = _cli_config()
    authority = "operator"
    login = "sediment login"
    if cfg is None:
        authority = "ingest"
        login = "sediment login <url> --capture"
        try:
            cfg = _cli_config(capture=True)
        except ValueError:
            findings.append((DOCTOR_FAIL, "server", _CAPTURE_LOGIN))
            return
    if cfg is None:
        findings.append(
            (
                DOCTOR_INFO,
                "server",
                "no credential enrolled; run sediment login <url> --capture "
                "for capture or sediment login <url> for operator reads",
            )
        )
        return
    url, token = cfg
    check = f"server[{url}]"
    request = urllib.request.Request(
        f"{url.rstrip('/')}/v1/me",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": _DOCTOR_USER_AGENT,
        },
    )
    try:
        opener = urllib.request.build_opener(_RejectRedirects())
        with opener.open(request, timeout=5) as resp:
            raw = resp.read(8193)
        if len(raw) > 8192:
            raise ValueError
        body = json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = (
            f"token rejected (401) — re-run {login}"
            if exc.code == 401
            else f"HTTP {exc.code}"
        )
        findings.append((DOCTOR_FAIL, check, detail))
        return
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        findings.append((DOCTOR_FAIL, check, f"unreachable ({exc.__class__.__name__})"))
        return
    except (ValueError, TypeError, UnicodeError):
        findings.append(
            (DOCTOR_FAIL, check, "unreadable identity response; verification failed")
        )
        return
    if not isinstance(body, dict) or body.get("authority") != authority:
        findings.append(
            (
                DOCTOR_FAIL,
                check,
                f"{authority} authority required; run {login} with an {authority} token",
            )
        )
        return
    detail = f"reachable, {authority} token valid (org {body.get('org_id')})"
    try:
        from sediment_api import __version__ as client_version
    except ImportError:  # standalone copied-file run — skew unknowable
        client_version = None
    server_version = body.get("version")
    if server_version and client_version and server_version != client_version:
        findings.append(
            (
                DOCTOR_INFO,
                check,
                f"{detail}; version skew: server {server_version}, client "
                f"{client_version} — uv tool upgrade sediment-cli",
            )
        )
    else:
        findings.append((DOCTOR_OK, check, detail))


def cmd_install(
    repo: str,
    agents: bool,
    transcripts: bool = False,
    env: bool = True,
    user_id: str | None = None,
    gateway_url: str | None = None,
    gateway_key: str | None = None,
    codex_profile: str | None = None,
) -> int:
    repo_path = Path(repo).resolve()
    if _git_dir(repo_path) is None:
        _error(f"{repo_path} is not a git work tree")
        return 1
    hooks_dir = _hooks_dir(repo_path)
    if hooks_dir is None:
        _error(f"could not resolve the hooks dir for {repo_path}")
        return 1
    if codex_profile is not None:
        try:
            profile = _install_codex_profile(codex_profile)
        except (OSError, UnicodeError, ValueError) as exc:
            _error(
                str(exc)
                if isinstance(exc, ValueError)
                else "Codex profile could not be written"
            )
            return 1
        print(
            f"Codex telemetry profile: wrote {profile} (0600); start codex --profile {codex_profile}"
        )
    installed = _install_repo_hooks(hooks_dir, repo_path)
    ok = ui.glyph("✓", "phosphor")
    print(
        f"{ok}installed git hooks ({', '.join(installed) or 'none'}) in {hooks_dir} "
        "and set notes.rewriteRef"
    )
    if agents:
        claude_status = _install_claude_hook()
        codex_status = _install_codex_hook()
        cursor_status = _install_cursor_hooks()
        pi_status = _install_pi_extension()
        print(
            f"{ok}agent hooks: "
            f"claude-code {claude_status}, codex {codex_status}, "
            f"cursor {cursor_status}, pi extension {pi_status}"
        )
        if codex_status != "skipped":
            print(
                "Codex skips hooks until you trust them. In Codex, run /hooks "
                "and trust the Sediment hook."
            )
        # Env wiring is user-level like the agent hooks, so it rides the
        # same flag pair: --no-agents implies no env wiring.
        if env:
            try:
                detail = cmd_install_env(user_id, gateway_url, gateway_key, transcripts)
            except ValueError as exc:
                _error(str(exc))
                return 1
            except (OSError, UnicodeError, RuntimeError, ImportError):
                _error(
                    "agent environment could not be written; enrollment is incomplete"
                )
                return 1
            print(f"{ok}agent env: {detail}")
    if transcripts:
        verdict, _, detail = _doctor_capture_endpoint()
        if verdict == DOCTOR_FAIL:
            _warn(f"capture endpoint {detail}")
        if not env or not agents:
            print(
                "For hook delivery, set SEDIMENT_OTLP_ENDPOINT and SEDIMENT_INGEST_TOKEN in the agent environment. For pi Edit observations, also set SEDIMENT_PI_TRANSCRIPTS=1."
            )
        print(f"{ok}transcript hook (SessionEnd): {_install_transcript_hook()}")
        print(
            f"{ok}codex transcript hook (SessionEnd): "
            f"{_install_codex_transcript_hook()}"
        )
        print(f"{ok}snapshot hook (PreToolUse): {_install_snapshot_hook()}")
    try:
        directory = (
            _delivery_directory()
            if agents and env
            else os.environ.get("SEDIMENT_DELIVERY_DIR")
        )
    except (OSError, UnicodeError, ValueError):
        _error("delivery enrollment could not be read")
        return 1
    verdict, check, detail = _doctor_delivery(directory)
    print(f"{verdict}  {check}: {detail}")
    return 1 if verdict == DOCTOR_FAIL else 0


def cmd_uninstall(repo: str, agents: bool) -> int:
    repo_path = Path(repo).resolve()
    if _git_dir(repo_path) is not None:
        hooks_dir = _hooks_dir(repo_path)
        if hooks_dir is not None:
            for name, _ in _REPO_HOOKS:
                _remove_hook_block(hooks_dir / name)
            # Remove only OUR value; other notes.rewriteRef entries survive.
            _git(
                ["config", "--fixed-value", "--unset", "notes.rewriteRef", NOTES_REF],
                repo_path,
            )
            print(f"{ui.glyph('✓', 'phosphor')}removed git hooks from {hooks_dir}")
    if agents:
        ok = ui.glyph("✓", "phosphor")
        for path in (_claude_settings_path(), _codex_hooks_path()):
            if _remove_agent_entries(path):
                print(f"{ok}removed agent hook entries from {path}")
        if _remove_cursor_entries():
            print(f"{ok}removed cursor hook entries from {_cursor_hooks_path()}")
        if _remove_pi_extension():
            print(f"{ok}removed pi extension from {_pi_settings_path()}")
        for removed in _unwire_env():
            print(f"{ok}removed {removed}")
        if not _uninstall_codex_profiles():
            return 1
    return 0


def _fleet_invocation(prefix: str, subcommand: str) -> str:
    # `python3` from PATH, not sys.executable: the bundle runs on machines
    # that are not this one.
    return f'python3 "{prefix}/sediment_attribution.py" {subcommand} || true'


def _managed_claude_settings_path() -> Path:
    """Claude Code's managed-settings.json location. The fleet bundle targets
    POSIX machines only (hooks invoke `python3`, prefix is a POSIX path);
    Native Windows capture is unsupported."""
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def _claude_settings_candidates() -> list[Path]:
    """Every file a Claude Code hook entry can legitimately live in, in the
    order Claude Code layers them: the MDM-managed file (what
    ``install --fleet --apply`` writes) then the per-user one (what
    ``install`` writes). Either carrying our entry means sessions get marked,
    so ``doctor`` must look in both before calling a machine unhooked."""
    return [_managed_claude_settings_path(), _claude_settings_path()]


def _emit_fleet_bundle(out: Path, prefix: str) -> bool:
    """Write the MDM bundle into ``out``; hooks reference the stamper at
    ``prefix``. Re-runs regenerate in place (the template hooks go through
    ``_install_hook_block``, so foreign content in a pre-existing hook is
    preserved and a non-sh hook is refused, same as the per-repo installer).
    """
    hooks_dir = out / "git-template" / "hooks"
    ok = all(
        # a realized list, not a generator: all() would stop consuming a
        # generator at the first False and skip the second hook's install
        [
            _install_hook_block(hooks_dir / name, _fleet_invocation(prefix, sub))
            for name, sub in _REPO_HOOKS
        ]
    )
    (out / "sediment_attribution.py").write_bytes(Path(__file__).resolve().read_bytes())
    for name in ("transcript", "delivery"):
        (out / f"sediment_{name}.py").write_bytes(
            _capture_client_path(name).read_bytes()
        )
    (out / "gitconfig").write_text(
        f"[init]\n\ttemplateDir = {prefix}/git-template\n"
        f"[notes]\n\trewriteRef = {NOTES_REF}\n",
        encoding="utf-8",
    )
    fragments = {
        "claude-managed-settings.json": _claude_hook_block(
            _fleet_invocation(prefix, "mark --tool claude-code")
        ),
        "codex-hooks.json": _codex_hook_block(
            _fleet_invocation(prefix, "mark --tool codex")
        ),
    }
    for name, block in fragments.items():
        # Same writer --apply uses, so the emitted fragment bytes can never
        # diverge from what the installer itself would write.
        _write_json_atomic(out / name, {"hooks": {"PostToolUse": [block]}})
    return ok


def _is_our_template_dir(template_dir: str) -> bool:
    """True when an existing init.templateDir is a sediment-managed template
    (its post-commit carries our marker block) — safe to repoint on a prefix
    migration. Anything else, including a deleted dir, is treated as foreign.
    """
    try:
        hook = Path(template_dir) / "hooks" / "post-commit"
        return HOOK_BLOCK_BEGIN in hook.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def _system_config(*args: str) -> bool:
    if _git(["config", "--system", *args]) is not None:
        return True
    _error("could not write the system gitconfig (re-run with privileges?)")
    return False


def cmd_install_fleet(out: str | None, apply: bool, prefix: str | None) -> int:
    """Emit the fleet bundle (default), or ``--apply`` it to this machine:
    bundle into ``prefix`` + system gitconfig keys + Claude managed settings.

    Unlike the hook subcommands this is admin-facing, so failures are loud
    (non-zero exit) — but nothing it doesn't own is ever overwritten: a
    foreign ``init.templateDir`` or a managed-settings file that doesn't
    parse is left untouched with a warning.
    """
    prefix = (prefix or "/opt/sediment").rstrip("/")
    # The prefix lands inside sh double quotes and a gitconfig value on every
    # fleet machine: relative paths silently break stamping fleet-wide (hooks
    # are best-effort), and `"`/`\`/`$`/backtick corrupt the quoting or the
    # gitconfig escape syntax. POSIX-absolute only.
    if not prefix.startswith("/") or any(c in prefix for c in '"\\$`\n'):
        _error(
            '--prefix must be an absolute POSIX path without ", \\, $, '
            "backticks, or newlines"
        )
        return 2
    out_dir = Path(prefix) if apply else Path(out or "sediment-fleet")
    try:
        ok = _emit_fleet_bundle(out_dir, prefix)
    except (OSError, RuntimeError) as exc:
        hint = " (re-run with privileges?)" if apply else ""
        _error(f"could not write the fleet bundle to {out_dir}: {exc}{hint}")
        return 1
    print(f"{ui.glyph('✓', 'phosphor')}fleet bundle written to {out_dir}")
    if not ok:
        # Don't hand out an incomplete bundle — and never wire system config
        # at a template dir whose hooks we were refused from installing into
        # (that would activate the foreign hook on every future clone).
        _error("bundle incomplete — fix the hook files warned about above and re-run")
        return 1
    if not apply:
        print(
            "distribute the bundle via MDM, or re-run with --apply on a "
            "machine to provision it directly"
        )
        return 0

    gitconfig_ok = True
    template_dir = f"{prefix}/git-template"
    existing = _git(["config", "--system", "--get", "init.templateDir"])
    if existing is not None and existing != template_dir:
        if _is_our_template_dir(existing):
            # Prefix migration: the old value is our own template — repoint.
            gitconfig_ok = _system_config("init.templateDir", template_dir)
        else:
            _warn(
                f"system init.templateDir is already set to "
                f"{existing!r}; not overwritten — merge {out_dir}/gitconfig "
                "into it manually"
            )
            gitconfig_ok = False
    elif existing is None:
        gitconfig_ok = _system_config("init.templateDir", template_dir)
    rewrite = _git(["config", "--system", "--get-all", "notes.rewriteRef"]) or ""
    if NOTES_REF not in rewrite.splitlines():
        gitconfig_ok = (
            _system_config("--add", "notes.rewriteRef", NOTES_REF) and gitconfig_ok
        )
    managed = _managed_claude_settings_path()
    block = _claude_hook_block(_fleet_invocation(prefix, "mark --tool claude-code"))
    try:
        status = _install_agent_entry(managed, block, "claude-code managed")
    except OSError as exc:
        _error(f"could not write {managed}: {exc} (re-run with privileges?)")
        status = "failed"
    print(
        f"system gitconfig {'applied' if gitconfig_ok else 'FAILED (see above)'}; "
        f"claude-code managed settings {status}; codex has no system-level hooks "
        f"file — distribute {out_dir}/codex-hooks.json into each user's "
        "~/.codex/hooks.json"
    )
    return 0 if gitconfig_ok and status not in ("skipped", "failed") else 1


def build_parser() -> argparse.ArgumentParser:
    """The attribution verbs' argparse tree, extracted so the generated CLI
    reference can walk it — these flags live nowhere else, since
    ``cli.py`` carries help stubs only."""
    parser = argparse.ArgumentParser(prog="sediment", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("cursor-hook", help="translate one native Cursor hook event")

    p_mark = sub.add_parser("mark", help="record a session marker (agent hook)")
    # Registration twin of AgentHarness (models.py): a harness's shim can
    # only mark once its tool value is accepted here. The script is
    # stdlib-only by design, so the list is duplicated, not imported.
    p_mark.add_argument(
        "--tool", required=True, choices=["claude-code", "codex", "cursor", "pi"]
    )

    sub.add_parser("stamp", help="write the attribution note on HEAD (post-commit)")

    p_squash = sub.add_parser(
        "union-squash-notes",
        help="fold squashed commits' notes into local markers (prepare-commit-msg)",
    )
    p_squash.add_argument("msg_file")
    p_squash.add_argument("source")
    p_squash.add_argument("commit_sha", nargs="?", default=None)

    p_push = sub.add_parser(
        "push-notes", help="reconcile and push the notes ref (pre-push)"
    )
    p_push.add_argument("remote")

    p_repair = sub.add_parser(
        "repair-notes",
        help="reconcile the notes ref with a remote and push (operator fix)",
    )
    p_repair.add_argument("remote", nargs="?", default="origin")

    p_doctor = sub.add_parser(
        "doctor",
        help="check capture configuration or verify one captured Session",
    )
    p_doctor.add_argument(
        "repos",
        nargs="*",
        metavar="REPO",
        help="also run the per-repo checks on each REPO (default: agent and "
        "machine-level checks only)",
    )
    p_doctor.add_argument(
        "--fetch",
        action="store_true",
        help="allow one fetch into the notes tracking ref, so a notes ref "
        "whose remote tip is not a local object can be classified as behind "
        "vs diverged; without it doctor writes nothing at all",
    )
    p_doctor.add_argument(
        "--agent",
        choices=("cursor", "codex", "pi"),
        help="verify this harness's installation and captured Session evidence",
    )
    p_doctor.add_argument(
        "--session-id", help="verify this exact Session with --agent and one REPO"
    )
    p_doctor.add_argument(
        "--transcripts",
        action="store_true",
        help="also require this harness's Edit observation and transcript opt-in",
    )
    p_doctor.add_argument(
        "--inference-calls",
        action="store_true",
        help="also require an Inference call in the selected Session",
    )

    p_install = sub.add_parser("install", help="install hooks for a repo")
    p_install.add_argument("repo", nargs="?", default=None)
    p_install.add_argument(
        "--no-agents",
        action="store_true",
        help="skip user-level hooks for Claude Code, Codex, Cursor, and pi",
    )
    p_install.add_argument(
        "--transcripts",
        action="store_true",
        help="opt in to the SessionEnd transcript extractor: ships "
        "applied edit text and observed file text to the ingest endpoint, "
        "plus the PreToolUse snapshot hook whose line hashes let it report "
        "how many lines something other than the agent changed; also enable "
        "pi Edit observations in the generated environment",
    )
    p_install.add_argument(
        "--no-env",
        action="store_true",
        help="skip writing the agent telemetry env files; by default "
        "install generates them from the `sediment login` config",
    )
    p_install.add_argument(
        "--user-id",
        default=None,
        help="stamp OTEL_RESOURCE_ATTRIBUTES=user.id=<value> into the env "
        "files (per-developer attribution)",
    )
    p_install.add_argument(
        "--codex-profile",
        metavar="NAME",
        help="write a Codex telemetry profile with resolved login credentials (0600); preserve its model and unrelated settings",
    )
    p_install.add_argument(
        "--gateway-url",
        default=None,
        help="also wire ANTHROPIC_BASE_URL to this LLM gateway",
    )
    p_install.add_argument(
        "--gateway-key",
        default=None,
        help="also wire ANTHROPIC_AUTH_TOKEN and SEDIMENT_GATEWAY_KEY "
        "(the key agents present to the gateway)",
    )
    p_install.add_argument(
        "--fleet",
        action="store_true",
        help="emit the machine-wide MDM bundle instead of a per-repo install",
    )
    fleet_target = p_install.add_mutually_exclusive_group()
    fleet_target.add_argument(
        "--out",
        metavar="DIR",
        default=None,
        help="fleet: emit the bundle to DIR (default: ./sediment-fleet)",
    )
    fleet_target.add_argument(
        "--apply",
        action="store_true",
        help="fleet: provision this machine directly — bundle into --prefix, "
        "system gitconfig, Claude Code managed settings (needs privileges)",
    )
    p_install.add_argument(
        "--prefix",
        default=None,
        help="fleet: absolute POSIX path where MDM installs the bundle; hooks "
        "reference the stamper there (default: /opt/sediment)",
    )

    p_uninstall = sub.add_parser("uninstall", help="remove hooks from a repo")
    p_uninstall.add_argument("repo", nargs="?", default=".")
    p_uninstall.add_argument(
        "--agents",
        action="store_true",
        help="also remove user-level agent hooks, generated env, and managed Codex telemetry",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "install" and os.name == "nt":
        _error("Windows capture is unsupported; run sediment install on macOS or Linux")
        return 1
    if args.command == "cursor-hook":
        return cmd_cursor_hook()
    if args.command == "mark":
        return cmd_mark(args.tool)
    if args.command == "stamp":
        return cmd_stamp()
    if args.command == "union-squash-notes":
        return cmd_union_squash_notes(args.msg_file, args.source)
    if args.command == "push-notes":
        return cmd_push_notes(args.remote)
    if args.command == "repair-notes":
        return cmd_repair_notes(args.remote)
    if args.command == "doctor":
        verification = (
            args.agent is not None
            or args.session_id is not None
            or args.transcripts
            or args.inference_calls
        )
        if verification and (
            args.agent is None
            or not args.session_id
            or not args.session_id.strip()
            or any(ord(char) < 32 for char in args.session_id)
            or len(args.repos) != 1
        ):
            _error(
                "verification requires one REPO, --agent, and a nonempty --session-id"
            )
            return 2
        return cmd_doctor(
            args.repos,
            args.fetch,
            args.agent,
            args.session_id,
            args.transcripts,
            args.inference_calls,
        )
    if args.command == "install":
        # Reject flag mixes that would otherwise be silently ignored — an
        # admin who typos the mode must not get a different install with
        # exit 0.
        per_user_flags = (
            args.repo is not None
            or args.no_agents
            or args.transcripts
            or args.no_env
            or args.user_id is not None
            or args.gateway_url is not None
            or args.gateway_key is not None
            or args.codex_profile is not None
        )
        if args.fleet:
            if per_user_flags:
                _error(
                    "REPO, --no-agents, --transcripts, --codex-profile, and the env "
                    "flags (--no-env/--user-id/--gateway-*) do not apply "
                    "to --fleet"
                )
                return 2
            return cmd_install_fleet(args.out, args.apply, args.prefix)
        if args.out is not None or args.apply or args.prefix is not None:
            _error("--out/--apply/--prefix require --fleet")
            return 2
        if args.codex_profile is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.codex_profile
        ):
            _error(
                "profile name must use 1–64 letters, digits, underscores, or hyphens and start with a letter or digit"
            )
            return 2
        return cmd_install(
            args.repo or ".",
            agents=not args.no_agents,
            transcripts=args.transcripts,
            env=not args.no_env,
            user_id=args.user_id,
            gateway_url=args.gateway_url,
            gateway_key=args.gateway_key,
            codex_profile=args.codex_profile,
        )
    if args.command == "uninstall":
        return cmd_uninstall(args.repo, agents=args.agents)
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())

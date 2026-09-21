# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collect sanitized Cursor Enterprise correlation fixtures on macOS."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator


COLLECTOR_VERSION = "1"
ARTIFACT_STATUSES = frozenset(
    {
        "captured",
        "not_observed",
        "not_configured",
        "not_available",
        "access_denied",
        "no_matching_record",
        "request_failed",
        "invalid_input",
    }
)
CURSOR_HOOKS: dict[str, str | None] = {
    "postToolUse": "Write",
    "postToolUseFailure": "Write",
    "afterTabFileEdit": None,
}
SYNTHETIC_FIXTURE_PATH = "sediment_cursor_fixture.py"
SYNTHETIC_COMMIT_MESSAGE = "test: collect Cursor Enterprise fixture"
SYNTHETIC_SUCCESS_CONTENT = 'SEDIMENT_CURSOR_FIXTURE = "successful-agent-write"\n'
SYNTHETIC_TAB_CONTENT = 'SEDIMENT_CURSOR_TAB = "accepted-tab-edit"\n'
CURSOR_API_BASE = "https://api.cursor.com"
MAX_JSON_BYTES = 25 * 1024 * 1024
ARCHIVE_MEMBERS = frozenset(
    {
        "hooks/post-tool-use.json",
        "hooks/post-tool-use-failure.json",
        "hooks/after-tab-file-edit.json",
        "ai-code/commits.json",
        "ai-code/changes.json",
        "ai-code/commit-details.json",
        "otel/cursor-api-request.json",
        "gateway/request.json",
    }
)
_HOOK_FILENAMES = {
    "postToolUse": "post-tool-use.jsonl",
    "postToolUseFailure": "post-tool-use-failure.jsonl",
    "afterTabFileEdit": "after-tab-file-edit.jsonl",
}
_PRESERVED_IDENTIFIERS = frozenset(
    {
        "conversation_id",
        "generation_id",
        "tool_use_id",
        "cursor.conversation.id",
        "cursor.request.id",
        "cursor.usage_event.id",
        "cursor.event.id",
        "cursor.source_event.id",
        "changeId",
    }
)
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_DETECT_CURSOR = object()


class CollectorError(RuntimeError):
    """Base class for expected collector failures."""


class PreflightError(CollectorError):
    """The target machine or repository isn't safe for collection."""


class SanitizationError(CollectorError):
    """Sanitized output still contains a protected value."""


class CleanupError(CollectorError):
    """The collector can't prove that temporary git state belongs to the run."""


@dataclass(frozen=True)
class HookSnapshot:
    """Original project-hook state for byte-for-byte restoration."""

    config_path: Path
    cursor_dir_existed: bool
    config_existed: bool
    original_bytes: bytes | None
    original_mode: int | None


@dataclass(frozen=True)
class RedactionContext:
    """Run-owned values that must not leave the collector."""

    repository_path: Path
    worktree_path: Path
    state_path: Path
    repository_name: str
    commit_sha: str
    sensitive_values: tuple[str, ...] = ()
    synthetic_strings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepositoryPreflight:
    """Validated repository inputs used to create an isolated worktree."""

    repository_path: Path
    original_branch: str
    original_commit: str
    user_email: str
    cursor_version: str


@dataclass(frozen=True)
class WorktreeState:
    """Run-owned git worktree and branch identity."""

    repository_path: Path
    worktree_path: Path
    branch_name: str
    starting_commit: str
    expected_commit: str


@dataclass(frozen=True)
class ArtifactResult:
    """One possible archive artifact and its observed status."""

    status: str
    data: Any = None
    diagnostic: str | None = None


@dataclass(frozen=True)
class _ApiResponse:
    status: str
    data: Any = None
    diagnostic: str | None = None


def _validate_hook_config(raw: bytes, path: Path) -> dict[str, Any]:
    try:
        config = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise PreflightError(f"Cursor project hooks are invalid: {path}") from error
    if not isinstance(config, dict) or config.get("version") != 1:
        raise PreflightError(f"Cursor project hooks require version 1: {path}")
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        raise PreflightError(f"Cursor project hooks require a hooks object: {path}")
    for event, entries in hooks.items():
        if not isinstance(event, str) or not isinstance(entries, list):
            raise PreflightError(f"Cursor project hooks have an invalid event: {path}")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
                raise PreflightError(
                    f"Cursor project hooks have an invalid command: {path}"
                )
            matcher = entry.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                raise PreflightError(
                    f"Cursor project hooks have an invalid matcher: {path}"
                )
    return config


def capture_hook_snapshot(repository: Path) -> HookSnapshot:
    """Validate and record the repository's Cursor project hooks."""
    cursor_dir = repository / ".cursor"
    config_path = cursor_dir / "hooks.json"
    if cursor_dir.is_symlink() or (cursor_dir.exists() and not cursor_dir.is_dir()):
        raise PreflightError(
            f"Cursor project directory isn't a regular directory: {cursor_dir}"
        )
    if not config_path.exists():
        return HookSnapshot(config_path, cursor_dir.exists(), False, None, None)
    if not config_path.is_file() or config_path.is_symlink():
        raise PreflightError(
            f"Cursor project hooks aren't a regular file: {config_path}"
        )
    raw = config_path.read_bytes()
    _validate_hook_config(raw, config_path)
    return HookSnapshot(
        config_path,
        True,
        True,
        raw,
        stat.S_IMODE(config_path.stat().st_mode),
    )


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.sediment-{uuid.uuid4().hex}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def install_project_hooks(
    repository: Path,
    snapshot: HookSnapshot,
    *,
    state_dir: Path,
    script_path: Path,
    python_executable: Path,
) -> None:
    """Add run-owned Cursor project hooks without replacing foreign hooks."""
    if snapshot.config_existed:
        assert snapshot.original_bytes is not None
        config = _validate_hook_config(snapshot.original_bytes, snapshot.config_path)
    else:
        config = {"version": 1, "hooks": {}}
    hooks = config["hooks"]
    for event, matcher in CURSOR_HOOKS.items():
        command = shlex.join(
            [
                str(python_executable.resolve()),
                str(script_path.resolve()),
                "--record-hook",
                event,
                "--state-dir",
                str(state_dir.resolve()),
            ]
        )
        entry: dict[str, str] = {"command": command}
        if matcher is not None:
            entry["matcher"] = matcher
        hooks.setdefault(event, []).append(entry)
    content = (json.dumps(config, indent=2, sort_keys=True) + "\n").encode()
    _atomic_write(snapshot.config_path, content, snapshot.original_mode or 0o600)


def restore_project_hooks(snapshot: HookSnapshot) -> None:
    """Restore project hooks and remove only paths that this run created."""
    if snapshot.config_existed:
        assert snapshot.original_bytes is not None
        assert snapshot.original_mode is not None
        _atomic_write(
            snapshot.config_path, snapshot.original_bytes, snapshot.original_mode
        )
        return
    try:
        snapshot.config_path.unlink()
    except FileNotFoundError:
        pass
    if not snapshot.cursor_dir_existed:
        try:
            snapshot.config_path.parent.rmdir()
        except (FileNotFoundError, OSError):
            pass


def hook_record_path(state_dir: Path, event: str) -> Path:
    """Return the private JSON Lines path for one supported hook event."""
    try:
        filename = _HOOK_FILENAMES[event]
    except KeyError as error:
        raise ValueError(f"unsupported Cursor hook event: {event}") from error
    return state_dir / "hooks" / filename


def _valid_hook_payload(event: str, payload: object) -> bool:
    if event not in CURSOR_HOOKS or not isinstance(payload, dict):
        return False
    if payload.get("hook_event_name") != event:
        return False
    conversation_id = payload.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return False
    if event in {"postToolUse", "postToolUseFailure"}:
        tool_use_id = payload.get("tool_use_id")
        return (
            payload.get("tool_name") == "Write"
            and isinstance(tool_use_id, str)
            and bool(tool_use_id)
        )
    file_path = payload.get("file_path")
    return isinstance(file_path, str) and Path(file_path).is_absolute()


def record_hook_payload(event: str, raw: str, state_dir: Path) -> bool:
    """Validate and append one native hook payload to private run state."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return False
    if not _valid_hook_payload(event, payload):
        return False
    path = hook_record_path(state_dir, event)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        stream.write("\n")
    os.chmod(path, 0o600)
    return True


def read_hook_artifact(state_dir: Path, event: str) -> dict[str, Any] | None:
    """Read the latest valid payload for one hook event."""
    path = hook_record_path(state_dir, event)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if _valid_hook_payload(event, payload):
            return payload
    return None


def _redacted_shape(value: Any, placeholder: str) -> Any:
    if isinstance(value, dict):
        return {key: _redacted_shape(item, placeholder) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted_shape(item, placeholder) for item in value]
    return placeholder


def _sensitive_value_variants(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    basic = base64.b64encode(f"{value}:".encode()).decode()
    return value, basic


def _redact_string(value: str, context: RedactionContext) -> str:
    replacements = (
        (str(context.worktree_path), "<synthetic-workspace>"),
        (str(context.state_path), "<collector-state>"),
        (str(context.repository_path), "<redacted-repository>"),
        (context.repository_name, "<redacted-repository>"),
        *(
            (variant, "<redacted-sensitive-value>")
            for secret in context.sensitive_values
            for variant in _sensitive_value_variants(secret)
        ),
        *((source, "<redacted-source>") for source in context.synthetic_strings),
    )
    for sensitive, replacement in replacements:
        if sensitive:
            value = value.replace(sensitive, replacement)
    value = _EMAIL_RE.sub("<redacted-email>", value)
    return re.sub(
        r"(?<![:/\w])/(?:Users|Volumes|private|var|tmp|home|opt)/[^\s\"'<>]+",
        "<redacted-path>",
        value,
    )


def sanitize_artifact(value: Any, context: RedactionContext, *, _key: str = "") -> Any:
    """Remove content and identity fields while preserving documented join ids."""
    if isinstance(value, dict):
        attribute_key = value.get("key")
        if isinstance(attribute_key, str) and "value" in value:
            normalized_attribute = attribute_key.casefold().replace("-", "_")
            identity_attribute = normalized_attribute in {
                "user.id",
                "user_id",
                "userid",
                "encoded_user_id",
            }
            sensitive_attribute = any(
                marker in normalized_attribute
                for marker in (
                    "authorization",
                    "cookie",
                    "secret",
                    "token",
                    "prompt",
                    "completion",
                    "input",
                    "output",
                    "response",
                    "content",
                    "message",
                    "transcript",
                    "argument",
                )
            )
            attribute_redaction = None
            if identity_attribute:
                attribute_redaction = "<redacted-user-id>"
            elif sensitive_attribute:
                attribute_redaction = "<redacted-content>"
            return {
                key: (
                    _redacted_shape(item, attribute_redaction)
                    if key == "value" and attribute_redaction is not None
                    else sanitize_artifact(item, context, _key=key)
                )
                for key, item in value.items()
            }
        return {
            key: sanitize_artifact(item, context, _key=key)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_artifact(item, context, _key=_key) for item in value]

    normalized = _key.casefold().replace("-", "_")
    if _key in _PRESERVED_IDENTIFIERS:
        return value
    if _key == "commitHash":
        return value if value == context.commit_sha else "<redacted-commit-hash>"
    if "email" in normalized:
        return "<redacted-email>"
    if normalized in {"userid", "user_id", "encoded_user_id"}:
        return "<redacted-user-id>"
    if normalized in {"branch", "branchname", "branch_name"}:
        return "<redacted-branch>"
    if normalized in {"commitmessage", "commit_message", "message"}:
        return (
            SYNTHETIC_COMMIT_MESSAGE
            if value == SYNTHETIC_COMMIT_MESSAGE
            else _redacted_shape(value, "<redacted-commit-message>")
        )
    if (
        any(
            marker in normalized
            for marker in (
                "authorization",
                "api_key",
                "apikey",
                "password",
                "cookie",
                "secret",
                "bearer",
            )
        )
        or normalized == "token"
    ):
        return _redacted_shape(value, "<redacted-credential>")
    if normalized in {"error", "error_text", "failure", "failure_reason"}:
        return _redacted_shape(value, "<redacted-error>")
    if normalized in {
        "prompt",
        "transcript",
        "conversation_summary",
        "summary",
        "title",
        "tldr",
        "overview",
        "summary_bullets",
        "summarybullets",
        "tool_input",
        "tool_output",
        "model_output",
        "response",
        "content",
        "text",
        "code",
        "diff",
        "edit",
        "edits",
    }:
        return _redacted_shape(value, "<redacted-content>")
    if normalized in {
        "cwd",
        "path",
        "filepath",
        "file_path",
        "filename",
        "file_name",
        "repo",
        "repository",
        "reponame",
        "repo_name",
        "workspace",
    }:
        if normalized in {
            "filepath",
            "file_path",
            "filename",
            "file_name",
            "path",
        } and isinstance(value, str):
            filename = Path(value).name
            if filename == SYNTHETIC_FIXTURE_PATH:
                return f"<synthetic-workspace>/{SYNTHETIC_FIXTURE_PATH}"
            return "<redacted-path>"
        return "<redacted-repository>"
    if isinstance(value, str):
        return _redact_string(value, context)
    return value


def assert_no_sensitive_bytes(content: bytes, context: RedactionContext) -> None:
    """Reject output that still contains a credential, identity, path, or source."""
    sensitive = [
        str(context.repository_path),
        str(context.worktree_path),
        str(context.state_path),
        *(
            variant
            for secret in context.sensitive_values
            for variant in _sensitive_value_variants(secret)
        ),
        *context.synthetic_strings,
        *(source.rstrip("\r\n") for source in context.synthetic_strings),
    ]
    for value in sensitive:
        if not value:
            continue
        encoded_variants = {
            os.fsencode(value),
            json.dumps(value)[1:-1].encode(),
        }
        if any(encoded and encoded in content for encoded in encoded_variants):
            raise SanitizationError("sanitized output contains a protected value")
    quoted_repository = json.dumps(context.repository_name).encode()
    if context.repository_name and quoted_repository in content:
        raise SanitizationError("sanitized output contains a repository name")
    if re.search(rb"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", content):
        raise SanitizationError("sanitized output contains an email address")


def _run_git(repository: Path, *args: str, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CollectorError(f"git {args[0]} failed") from error
    if check and result.returncode != 0:
        raise CollectorError(f"git {args[0]} failed")
    return result.stdout.strip()


def preflight_repository(
    repository: Path,
    *,
    platform_name: str | None = None,
    cursor_version: str | None | object = _DETECT_CURSOR,
    python_version: tuple[int, int] | None = None,
) -> RepositoryPreflight:
    """Refuse an unsuitable repository before the collector writes anything."""
    detected_python = python_version or sys.version_info[:2]
    if detected_python < (3, 12):
        raise PreflightError("the Cursor fixture collector requires Python 3.12")
    detected_platform = (
        platform_name if platform_name is not None else platform.system()
    )
    if detected_platform != "Darwin":
        raise PreflightError("the Cursor fixture collector requires macOS")
    if cursor_version is _DETECT_CURSOR:
        cursor_version = detect_cursor_version()
    if not isinstance(cursor_version, str) or not cursor_version:
        raise PreflightError("Cursor desktop isn't installed or detectable")
    repository = repository.expanduser().resolve()
    if not repository.is_dir():
        raise PreflightError("repository path isn't a directory")
    try:
        root = Path(_run_git(repository, "rev-parse", "--show-toplevel")).resolve()
    except CollectorError as error:
        raise PreflightError("the selected path isn't a git worktree") from error
    if _run_git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PreflightError("the collector requires a clean repository")
    if not _run_git(root, "remote", "get-url", "origin", check=False):
        raise PreflightError("the repository requires an origin remote")
    user_name = _run_git(root, "config", "user.name", check=False)
    user_email = _run_git(root, "config", "user.email", check=False)
    if not user_name:
        raise PreflightError("git user.name isn't configured")
    if not user_email:
        raise PreflightError("git user.email isn't configured")
    if (root / SYNTHETIC_FIXTURE_PATH).exists():
        raise PreflightError(
            f"the synthetic fixture path already exists: {SYNTHETIC_FIXTURE_PATH}"
        )
    capture_hook_snapshot(root)
    branch = _run_git(root, "branch", "--show-current") or "(detached)"
    commit = _run_git(root, "rev-parse", "HEAD")
    return RepositoryPreflight(root, branch, commit, user_email, cursor_version)


def detect_cursor_version() -> str | None:
    """Return the local Cursor desktop version without changing configuration."""
    executable = shutil.which("cursor")
    if executable:
        try:
            result = subprocess.run(
                [executable, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result and result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0][:120]
    application = Path("/Applications/Cursor.app")
    if not application.exists():
        return None
    try:
        result = subprocess.run(
            ["mdls", "-raw", "-name", "kMDItemVersion", str(application)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "detected"
    version = result.stdout.strip()
    return version[:120] if result.returncode == 0 and version else "detected"


def create_isolated_worktree(
    preflight: RepositoryPreflight, state_dir: Path, run_id: str
) -> WorktreeState:
    """Create the run-owned linked worktree and temporary branch."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", run_id):
        raise CollectorError("run id has an invalid shape")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    worktree = state_dir / "worktree"
    branch = f"sediment/cursor-fixture-{run_id}"
    if worktree.exists():
        raise CollectorError("run worktree path already exists")
    if _run_git(
        preflight.repository_path,
        "show-ref",
        "--verify",
        f"refs/heads/{branch}",
        check=False,
    ):
        raise CollectorError("run branch already exists")
    try:
        _run_git(
            preflight.repository_path,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            preflight.original_commit,
        )
    except CollectorError:
        if worktree.exists():
            raise CleanupError(f"remove the incomplete worktree at {worktree}")
        raise
    return WorktreeState(
        preflight.repository_path,
        worktree,
        branch,
        preflight.original_commit,
        preflight.original_commit,
    )


def _owned_worktree_identity(state: WorktreeState) -> tuple[str, str] | None:
    if not state.worktree_path.is_dir():
        return None
    try:
        root = Path(
            _run_git(state.worktree_path, "rev-parse", "--show-toplevel")
        ).resolve()
        branch = _run_git(state.worktree_path, "symbolic-ref", "--short", "HEAD")
        commit = _run_git(state.worktree_path, "rev-parse", "HEAD")
    except CollectorError:
        return None
    if root != state.worktree_path.resolve():
        return None
    return branch, commit


def cleanup_isolated_worktree(state: WorktreeState) -> None:
    """Remove only the worktree and branch whose identity matches run state."""
    identity = _owned_worktree_identity(state)
    if identity is None or identity[0] != state.branch_name:
        raise CleanupError(f"worktree ownership changed; inspect {state.worktree_path}")
    if identity[1] != state.expected_commit:
        raise CleanupError(f"worktree commit changed; inspect {state.worktree_path}")
    branch_ref = f"refs/heads/{state.branch_name}"
    branch_commit = _run_git(
        state.repository_path, "rev-parse", "--verify", branch_ref, check=False
    )
    if branch_commit != state.expected_commit:
        raise CleanupError(f"run branch commit changed; inspect {state.worktree_path}")
    if _run_git(
        state.worktree_path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
    ):
        raise CleanupError(
            f"worktree has uncommitted changes; inspect {state.worktree_path}"
        )
    _run_git(
        state.repository_path,
        "worktree",
        "remove",
        "--force",
        str(state.worktree_path),
    )
    try:
        _run_git(
            state.repository_path,
            "update-ref",
            "-d",
            branch_ref,
            state.expected_commit,
        )
    except CollectorError as error:
        raise CleanupError("run branch commit changed during cleanup") from error


def cleanup_private_state(state_dir: Path, *, worktree_removed: bool) -> None:
    """Remove raw evidence only after a run-owned worktree is absent or removed."""
    worktree_path = state_dir / "worktree"
    if worktree_path.exists() and not worktree_removed:
        raise CleanupError(f"cleanup couldn't prove ownership; inspect {worktree_path}")
    try:
        shutil.rmtree(state_dir)
    except FileNotFoundError:
        return
    except OSError as error:
        raise CleanupError(
            f"remove the private collector state at {state_dir}"
        ) from error


def commit_synthetic_fixture(state: WorktreeState) -> str:
    """Commit only the fixed synthetic fixture path and return its SHA."""
    fixture = state.worktree_path / SYNTHETIC_FIXTURE_PATH
    if not fixture.is_file() or fixture.is_symlink():
        raise CollectorError("the synthetic fixture file is missing")
    _run_git(state.worktree_path, "add", "--", SYNTHETIC_FIXTURE_PATH)
    staged_raw = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        cwd=state.worktree_path,
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    staged = [os.fsdecode(item) for item in staged_raw.split(b"\0") if item]
    if staged != [SYNTHETIC_FIXTURE_PATH]:
        raise CollectorError("the staged path set isn't the synthetic fixture")
    _run_git(
        state.worktree_path,
        "commit",
        "-m",
        SYNTHETIC_COMMIT_MESSAGE,
        "--",
        SYNTHETIC_FIXTURE_PATH,
    )
    return _run_git(state.worktree_path, "rev-parse", "HEAD")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep the Admin API credential on the original request only."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class CursorApiClient:
    """Small Cursor Admin API client that keeps its key in process memory."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = CURSOR_API_BASE,
        timeout: float = 20.0,
    ) -> None:
        if not api_key:
            raise ValueError("Cursor Admin API key is empty")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def _safe_excerpt(self, body: bytes) -> str:
        excerpt = body[:256].decode("utf-8", errors="replace")
        excerpt = excerpt.replace(self._api_key, "<redacted-credential>")
        return _EMAIL_RE.sub("<redacted-email>", excerpt)

    def get(self, path: str, query: dict[str, str | int]) -> _ApiResponse:
        url = f"{self._base_url}{path}?{urllib.parse.urlencode(query)}"
        credential = base64.b64encode(f"{self._api_key}:".encode()).decode()
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {credential}",
                "User-Agent": f"sediment-cursor-fixture/{COLLECTOR_VERSION}",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                body = response.read(MAX_JSON_BYTES + 1)
                status_code = response.status
        except urllib.error.HTTPError as error:
            body = error.read(257)
            status = "access_denied" if error.code == 401 else "request_failed"
            return _ApiResponse(
                status,
                diagnostic=f"HTTP {error.code}: {self._safe_excerpt(body)}",
            )
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            return _ApiResponse(
                "request_failed", diagnostic=f"request failed: {type(error).__name__}"
            )
        if status_code < 200 or status_code >= 300:
            return _ApiResponse("request_failed", diagnostic=f"HTTP {status_code}")
        if len(body) > MAX_JSON_BYTES:
            return _ApiResponse(
                "request_failed", diagnostic="response exceeded size limit"
            )
        try:
            data = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            return _ApiResponse(
                "request_failed",
                diagnostic=f"invalid JSON: {self._safe_excerpt(body)}",
            )
        if not isinstance(data, dict):
            return _ApiResponse(
                "request_failed", diagnostic="response wasn't an object"
            )
        return _ApiResponse("captured", data=data)


def _all_with_status(
    status: str, diagnostic: str | None = None
) -> dict[str, ArtifactResult]:
    return {
        name: ArtifactResult(status, diagnostic=diagnostic)
        for name in ("commits", "changes", "commit_details")
    }


def collect_ai_code_artifacts(
    client: CursorApiClient,
    commit_sha: str,
    *,
    start_date: str,
    end_date: str,
    max_attempts: int = 9,
    poll_interval: float = 20.0,
) -> dict[str, ArtifactResult]:
    """Poll commit metrics, then retrieve change and detail responses."""
    if max_attempts < 1 or max_attempts > 9:
        raise ValueError("max_attempts must be between 1 and 9")
    query: dict[str, str | int] = {
        "startDate": start_date,
        "endDate": end_date,
        "page": 1,
        "pageSize": 1000,
    }
    matching: dict[str, Any] | None = None
    commits_data: dict[str, Any] | None = None
    for attempt in range(max_attempts):
        response = client.get("/analytics/ai-code/commits", query)
        if response.status != "captured":
            return _all_with_status(response.status, response.diagnostic)
        commits_data = response.data
        items = commits_data.get("items")
        if not isinstance(items, list):
            return _all_with_status(
                "request_failed", "commits response has no items list"
            )
        matching = next(
            (
                item
                for item in items
                if isinstance(item, dict) and item.get("commitHash") == commit_sha
            ),
            None,
        )
        if matching is not None:
            break
        if attempt + 1 < max_attempts:
            time.sleep(poll_interval)
    if matching is None or commits_data is None:
        return _all_with_status("no_matching_record")

    filtered_query = dict(query)
    user_id = matching.get("userId")
    if isinstance(user_id, str) and user_id:
        filtered_query["user"] = user_id
    changes_response = client.get("/analytics/ai-code/changes", filtered_query)
    changes = ArtifactResult(
        changes_response.status,
        changes_response.data,
        changes_response.diagnostic,
    )
    details_response = client.get(
        f"/analytics/ai-code/commits/{urllib.parse.quote(commit_sha, safe='')}",
        {"startDate": start_date, "endDate": end_date},
    )
    detail_status = details_response.status
    if details_response.diagnostic and (
        details_response.diagnostic.startswith("HTTP 403")
        or details_response.diagnostic.startswith("HTTP 404")
    ):
        detail_status = "not_available"
    details = ArtifactResult(
        detail_status, details_response.data, details_response.diagnostic
    )
    return {
        "commits": ArtifactResult("captured", commits_data),
        "changes": changes,
        "commit_details": details,
    }


def _contains_string(value: Any, expected: str) -> bool:
    if value == expected:
        return True
    if isinstance(value, dict):
        return any(_contains_string(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_string(item, expected) for item in value)
    return False


def load_optional_json(
    path: Path | None, *, expected_kind: str | None = None
) -> ArtifactResult:
    """Load one bounded normalized JSON export without guessing its absence."""
    if path is None:
        return ArtifactResult("not_configured")
    try:
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size > MAX_JSON_BYTES
        ):
            return ArtifactResult("invalid_input")
        data = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, ValueError):
        return ArtifactResult("invalid_input")
    if not isinstance(data, (dict, list)):
        return ArtifactResult("invalid_input")
    if expected_kind == "otel" and not _contains_string(data, "cursor.api.request"):
        return ArtifactResult("invalid_input")
    if expected_kind == "gateway" and not isinstance(data, dict):
        return ArtifactResult("invalid_input")
    if expected_kind not in {None, "otel", "gateway"}:
        raise ValueError(f"unsupported optional JSON kind: {expected_kind}")
    return ArtifactResult("captured", data)


def _correlation_identifiers(value: Any) -> set[str]:
    identifiers: set[str] = set()
    if isinstance(value, dict):
        attribute_key = value.get("key")
        attribute_value = value.get("value")
        if attribute_key in _PRESERVED_IDENTIFIERS and isinstance(
            attribute_value, dict
        ):
            string_value = attribute_value.get("stringValue")
            if isinstance(string_value, str) and string_value:
                identifiers.add(string_value)
        for key, item in value.items():
            if (
                (key in _PRESERVED_IDENTIFIERS or key == "commitHash")
                and isinstance(item, str)
                and item
            ):
                identifiers.add(item)
            identifiers.update(_correlation_identifiers(item))
    elif isinstance(value, list):
        for item in value:
            identifiers.update(_correlation_identifiers(item))
    return identifiers


def correlate_artifact(optional_data: Any, captured: dict[str, ArtifactResult]) -> str:
    """Report whether an optional record shares a documented join identifier."""
    optional_ids = _correlation_identifiers(optional_data)
    captured_ids: set[str] = set()
    for artifact in captured.values():
        if artifact.status == "captured":
            captured_ids.update(_correlation_identifiers(artifact.data))
    return "matched" if optional_ids & captured_ids else "unmatched"


def build_manifest(
    *,
    run_id: str,
    cursor_version: str,
    macos_version: str,
    started_at: str,
    finished_at: str,
    commit_sha: str,
    artifacts: dict[str, ArtifactResult],
    correlations: dict[str, str],
) -> dict[str, Any]:
    """Build the public manifest and validate every artifact status."""
    for artifact in artifacts.values():
        if artifact.status not in ARTIFACT_STATUSES:
            raise ValueError(f"invalid artifact status: {artifact.status}")
    return {
        "collector_version": COLLECTOR_VERSION,
        "run_id": run_id,
        "cursor_version": cursor_version,
        "macos_version": macos_version,
        "started_at": started_at,
        "finished_at": finished_at,
        "synthetic_commit_sha": commit_sha,
        "artifacts": {
            name: {
                "status": artifact.status,
                **({"correlation": correlations[name]} if name in correlations else {}),
            }
            for name, artifact in sorted(artifacts.items())
        },
    }


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def write_archive(
    output_directory: Path,
    run_id: str,
    manifest: dict[str, Any],
    artifacts: dict[str, ArtifactResult],
    context: RedactionContext,
) -> Path:
    """Sanitize and write one private archive without partial artifacts."""
    unknown = set(artifacts) - ARCHIVE_MEMBERS
    if unknown:
        raise ValueError(f"unsupported archive member: {sorted(unknown)[0]}")
    members = {"manifest.json": _json_bytes(sanitize_artifact(manifest, context))}
    for name, artifact in artifacts.items():
        if artifact.status != "captured":
            continue
        if artifact.data is None:
            raise ValueError(f"captured artifact has no data: {name}")
        members[name] = _json_bytes(sanitize_artifact(artifact.data, context))
    combined = b"".join(members.values())
    assert_no_sensitive_bytes(combined, context)
    output_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = output_directory / f"cursor-enterprise-fixtures-{run_id}.zip"
    temporary_file = tempfile.NamedTemporaryFile(
        mode="w+b",
        prefix=f".{archive.name}.",
        suffix=".tmp",
        dir=output_directory,
        delete=False,
    )
    temporary = Path(temporary_file.name)
    try:
        with temporary_file:
            with zipfile.ZipFile(
                temporary_file, "w", compression=zipfile.ZIP_DEFLATED
            ) as bundle:
                for name, content in sorted(members.items()):
                    bundle.writestr(name, content)
        os.replace(temporary, archive)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return archive


def _payload_matches_worktree(
    event: str, payload: dict[str, Any], worktree: Path
) -> bool:
    worktree = worktree.resolve()
    if event in {"postToolUse", "postToolUseFailure"}:
        cwd = payload.get("cwd")
        return isinstance(cwd, str) and Path(cwd).resolve() == worktree
    file_path = payload.get("file_path")
    return (
        isinstance(file_path, str)
        and Path(file_path).resolve() == (worktree / SYNTHETIC_FIXTURE_PATH).resolve()
    )


def collect_recorded_hook_artifacts(
    state_dir: Path, worktree: Path
) -> dict[str, ArtifactResult]:
    """Validate the run's three expected hook results."""
    fixture = worktree / SYNTHETIC_FIXTURE_PATH
    try:
        fixture_content = fixture.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise CollectorError(
            "the successful Agent Write didn't create the fixture"
        ) from error
    if SYNTHETIC_SUCCESS_CONTENT not in fixture_content:
        raise CollectorError("the successful Agent Write produced unexpected content")

    captured: dict[str, ArtifactResult] = {}
    archive_names = {
        "postToolUse": "hooks/post-tool-use.json",
        "postToolUseFailure": "hooks/post-tool-use-failure.json",
        "afterTabFileEdit": "hooks/after-tab-file-edit.json",
    }
    for event, archive_name in archive_names.items():
        payload = read_hook_artifact(state_dir, event)
        if payload is None:
            if event == "postToolUseFailure":
                captured[archive_name] = ArtifactResult("not_observed")
                continue
            label = "successful Agent Write" if event == "postToolUse" else "Tab edit"
            raise CollectorError(f"Cursor didn't record the {label} hook")
        if not _payload_matches_worktree(event, payload, worktree):
            raise CollectorError(
                f"the {event} hook doesn't match the synthetic worktree"
            )
        captured[archive_name] = ArtifactResult("captured", payload)
    if SYNTHETIC_TAB_CONTENT not in fixture_content:
        raise CollectorError("the accepted Tab edit produced unexpected content")
    return captured


def open_cursor_workspace(worktree: Path) -> None:
    """Open the isolated worktree in Cursor desktop."""
    try:
        result = subprocess.run(
            ["open", "-a", "Cursor", str(worktree)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CollectorError(
            "Cursor desktop couldn't open the synthetic worktree"
        ) from error
    if result.returncode != 0:
        raise CollectorError("Cursor desktop couldn't open the synthetic worktree")


def guide_capture_steps(
    worktree: Path,
    state_dir: Path,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> dict[str, ArtifactResult]:
    """Guide the operator through successful, failed, and Tab edit capture."""
    fixture = worktree / SYNTHETIC_FIXTURE_PATH
    output_fn("\nStep 1 of 3 — successful Agent Write")
    output_fn(
        "In Cursor Agent, create sediment_cursor_fixture.py with exactly this line:\n"
        + SYNTHETIC_SUCCESS_CONTENT.rstrip()
    )
    input_fn("After Cursor finishes the Write, press Enter: ")
    success = read_hook_artifact(state_dir, "postToolUse")
    if success is None or not _payload_matches_worktree(
        "postToolUse", success, worktree
    ):
        raise CollectorError("Cursor didn't record the successful Agent Write hook")
    try:
        if fixture.read_text(encoding="utf-8") != SYNTHETIC_SUCCESS_CONTENT:
            raise CollectorError(
                "the successful Agent Write produced unexpected content"
            )
    except (OSError, UnicodeError) as error:
        raise CollectorError(
            "the successful Agent Write didn't create the fixture"
        ) from error

    output_fn("\nStep 2 of 3 — failed or denied Agent Write")
    fixture.chmod(0o444)
    try:
        output_fn(
            "Ask Cursor Agent to replace the fixture value with denied-agent-write. "
            "Don't approve a permission override."
        )
        input_fn("After the attempt finishes, press Enter: ")
    finally:
        fixture.chmod(0o644)

    output_fn("\nStep 3 of 3 — accepted Tab edit")
    output_fn(
        "On a new line in sediment_cursor_fixture.py, use Cursor Tab to accept exactly:\n"
        + SYNTHETIC_TAB_CONTENT.rstrip()
    )
    input_fn("After you accept the Tab edit, press Enter: ")
    return collect_recorded_hook_artifacts(state_dir, worktree)


def _verify_only_fixture_changed(worktree: Path) -> None:
    status_lines = _run_git(
        worktree, "status", "--porcelain=v1", "--untracked-files=all"
    ).splitlines()
    paths = {line[3:] for line in status_lines if len(line) >= 4}
    if paths != {SYNTHETIC_FIXTURE_PATH}:
        raise CollectorError(
            "the synthetic worktree contains an unexpected changed path"
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def run_collector(
    repository: Path,
    *,
    otel_json: Path | None,
    gateway_json: Path | None,
    output_directory: Path,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    open_cursor_fn: Callable[[Path], None] = open_cursor_workspace,
    capture_steps_fn: Callable[..., dict[str, ArtifactResult]] = guide_capture_steps,
    platform_name: str | None = None,
    cursor_version: str | None | object = _DETECT_CURSOR,
    macos_version: str | None = None,
) -> Path:
    """Run one isolated collection and return the sanitized ZIP path."""
    preflight = preflight_repository(
        repository,
        platform_name=platform_name,
        cursor_version=cursor_version,
    )
    output_directory = output_directory.expanduser().resolve()
    if output_directory == preflight.repository_path or output_directory.is_relative_to(
        preflight.repository_path
    ):
        raise PreflightError(
            "the archive output directory must be outside the repository"
        )

    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    started_at = _utc_now()
    state_dir = Path(tempfile.mkdtemp(prefix=f"sediment-cursor-{run_id}-"))
    os.chmod(state_dir, 0o700)
    worktree_state: WorktreeState | None = None
    hooks: HookSnapshot | None = None
    hooks_restored = False
    cleaned_worktree = False
    try:
        worktree_state = create_isolated_worktree(preflight, state_dir, run_id)
        hooks = capture_hook_snapshot(worktree_state.worktree_path)
        install_project_hooks(
            worktree_state.worktree_path,
            hooks,
            state_dir=state_dir,
            script_path=Path(__file__),
            python_executable=Path(sys.executable),
        )
        open_cursor_fn(worktree_state.worktree_path)
        hook_artifacts = capture_steps_fn(
            worktree_state.worktree_path, state_dir, input_fn, output_fn
        )
        restore_project_hooks(hooks)
        hooks_restored = True
        _verify_only_fixture_changed(worktree_state.worktree_path)
        commit_sha = commit_synthetic_fixture(worktree_state)
        worktree_state = replace(worktree_state, expected_commit=commit_sha)

        api_key = getpass_fn(
            "Cursor Admin API key (press Enter to record not_configured): "
        )
        if api_key:
            client = CursorApiClient(api_key)
            api_artifacts = collect_ai_code_artifacts(
                client,
                commit_sha,
                start_date=started_at,
                end_date="now",
            )
        else:
            api_artifacts = _all_with_status("not_configured")
        archive_artifacts = {
            **hook_artifacts,
            "ai-code/commits.json": api_artifacts["commits"],
            "ai-code/changes.json": api_artifacts["changes"],
            "ai-code/commit-details.json": api_artifacts["commit_details"],
            "otel/cursor-api-request.json": load_optional_json(
                otel_json, expected_kind="otel"
            ),
            "gateway/request.json": load_optional_json(
                gateway_json, expected_kind="gateway"
            ),
        }
        correlation_sources = {
            name: artifact
            for name, artifact in archive_artifacts.items()
            if name not in {"otel/cursor-api-request.json", "gateway/request.json"}
        }
        correlations: dict[str, str] = {}
        for name in ("otel/cursor-api-request.json", "gateway/request.json"):
            artifact = archive_artifacts[name]
            correlations[name] = (
                correlate_artifact(artifact.data, correlation_sources)
                if artifact.status == "captured"
                else artifact.status
            )
        context = RedactionContext(
            repository_path=preflight.repository_path,
            worktree_path=worktree_state.worktree_path,
            state_path=state_dir,
            repository_name=preflight.repository_path.name,
            commit_sha=commit_sha,
            sensitive_values=tuple(
                value for value in (api_key, preflight.user_email) if value
            ),
            synthetic_strings=(SYNTHETIC_SUCCESS_CONTENT, SYNTHETIC_TAB_CONTENT),
        )
        finished_at = _utc_now()
        manifest = build_manifest(
            run_id=run_id,
            cursor_version=preflight.cursor_version,
            macos_version=macos_version or platform.mac_ver()[0],
            started_at=started_at,
            finished_at=finished_at,
            commit_sha=commit_sha,
            artifacts=archive_artifacts,
            correlations=correlations,
        )
        archive = write_archive(
            output_directory, run_id, manifest, archive_artifacts, context
        )
        input_fn("Close the temporary Cursor workspace, then press Enter: ")
        output_fn(f"Sanitized fixture archive: {archive}")
        return archive
    finally:
        if hooks is not None and not hooks_restored:
            restore_project_hooks(hooks)
        if worktree_state is not None:
            cleanup_isolated_worktree(worktree_state)
            cleaned_worktree = True
        cleanup_private_state(state_dir, worktree_removed=cleaned_worktree)


def build_parser() -> argparse.ArgumentParser:
    """Build the standalone collector argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Guide a Cursor Enterprise operator through one sanitized "
            "macOS fixture collection."
        )
    )
    parser.add_argument(
        "repository", nargs="?", type=Path, help="clean tracked repository"
    )
    parser.add_argument(
        "--otel-json", type=Path, help="normalized Cursor OpenTelemetry JSON export"
    )
    parser.add_argument(
        "--gateway-json", type=Path, help="sanitized gateway request JSON"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.cwd(),
        help="archive directory (default: starting directory)",
    )
    parser.add_argument("--record-hook", help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", type=Path, help=argparse.SUPPRESS)
    return parser


@contextmanager
def termination_signal_guard() -> Iterator[None]:
    """Route process termination through the collector's interruption cleanup."""
    handled = [signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled.append(signal.SIGHUP)
    previous = {item: signal.getsignal(item) for item in handled}

    def interrupt(signum: int, frame: Any) -> None:
        del signum, frame
        raise KeyboardInterrupt

    try:
        for item in handled:
            signal.signal(item, interrupt)
        yield
    finally:
        for item, handler in previous.items():
            signal.signal(item, handler)


def main(argv: list[str] | None = None) -> int:
    """Run the collector or its private hook-recording mode."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.record_hook is not None:
        try:
            raw = sys.stdin.read(MAX_JSON_BYTES + 1)
            if args.state_dir is not None and len(raw.encode()) <= MAX_JSON_BYTES:
                record_hook_payload(args.record_hook, raw, args.state_dir)
        except Exception:
            pass
        return 0
    if args.repository is None:
        parser.error("repository is required")
    try:
        with termination_signal_guard():
            run_collector(
                args.repository,
                otel_json=args.otel_json,
                gateway_json=args.gateway_json,
                output_directory=args.output,
            )
    except KeyboardInterrupt:
        print(
            "error: collection interrupted; temporary state was cleaned up",
            file=sys.stderr,
        )
        return 130
    except CollectorError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

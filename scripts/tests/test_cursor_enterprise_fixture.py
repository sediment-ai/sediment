# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for the guided Cursor Enterprise fixture collector."""

from __future__ import annotations

import importlib.util
import base64
import json
import os
import stat
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "cursor_enterprise_fixture.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("cursor_enterprise_fixture", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _git(repository: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repository(tmp_path: Path, *, with_origin: bool = True) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Fixture Partner")
    _git(repository, "config", "user.email", "partner@example.test")
    (repository / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, "add", "tracked.py")
    _git(repository, "commit", "-qm", "fixture")
    if with_origin:
        _git(repository, "remote", "add", "origin", "https://example.test/org/repo.git")
    return repository


class _CursorApiHandler(BaseHTTPRequestHandler):
    responses: dict[str, list[tuple[int, object, float]]] = {}
    requests: list[tuple[str, str | None]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        route = urlsplit(self.path).path
        self.requests.append((self.path, self.headers.get("Authorization")))
        choices = self.responses.get(route, [(404, {"error": "missing"}, 0.0)])
        status, body, delay = choices.pop(0) if len(choices) > 1 else choices[0]
        if delay:
            time.sleep(delay)
        encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except BrokenPipeError:
            pass

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def cursor_api_server():
    _CursorApiHandler.responses = {}
    _CursorApiHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CursorApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _CursorApiHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_artifact_status_vocabulary_is_closed() -> None:
    assert mod.ARTIFACT_STATUSES == frozenset(
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


@pytest.mark.parametrize(
    "original",
    [
        b"{not json",
        json.dumps({"version": 2, "hooks": {}}).encode(),
        json.dumps({"version": 1, "hooks": []}).encode(),
        json.dumps({"version": 1, "hooks": {"postToolUse": {}}}).encode(),
        json.dumps({"version": 1, "hooks": {"postToolUse": [{"command": 7}]}}).encode(),
        json.dumps(
            {
                "version": 1,
                "hooks": {"postToolUse": [{"command": "foreign", "matcher": 7}]},
            }
        ).encode(),
    ],
)
def test_hook_snapshot_rejects_incompatible_configuration_untouched(
    tmp_path: Path, original: bytes
) -> None:
    config = tmp_path / ".cursor" / "hooks.json"
    config.parent.mkdir()
    config.write_bytes(original)

    with pytest.raises(mod.PreflightError, match="Cursor project hooks"):
        mod.capture_hook_snapshot(tmp_path)

    assert config.read_bytes() == original


def test_hook_install_is_additive_and_restores_original_bytes_and_mode(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".cursor" / "hooks.json"
    config.parent.mkdir()
    original = (
        b'{"version":1,"theme":"dark","hooks":{"postToolUse":'
        b'[{"command":"./foreign.sh"}]}}\n'
    )
    config.write_bytes(original)
    config.chmod(0o640)
    snapshot = mod.capture_hook_snapshot(tmp_path)
    state_dir = tmp_path.parent / "state"
    state_dir.mkdir(mode=0o700)

    mod.install_project_hooks(
        tmp_path,
        snapshot,
        state_dir=state_dir,
        script_path=SCRIPT,
        python_executable=Path(sys.executable),
    )

    installed = json.loads(config.read_text(encoding="utf-8"))
    assert installed["theme"] == "dark"
    assert installed["hooks"]["postToolUse"][0] == {"command": "./foreign.sh"}
    for event, matcher in mod.CURSOR_HOOKS.items():
        entry = installed["hooks"][event][-1]
        assert "--record-hook" in entry["command"]
        assert str(state_dir) in entry["command"]
        if matcher is None:
            assert "matcher" not in entry
        else:
            assert entry["matcher"] == matcher

    mod.restore_project_hooks(snapshot)

    assert config.read_bytes() == original
    assert stat.S_IMODE(config.stat().st_mode) == 0o640


def test_hook_restore_removes_only_collector_created_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    snapshot = mod.capture_hook_snapshot(repo)
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)

    mod.install_project_hooks(
        repo,
        snapshot,
        state_dir=state_dir,
        script_path=SCRIPT,
        python_executable=Path(sys.executable),
    )
    assert (repo / ".cursor" / "hooks.json").exists()

    mod.restore_project_hooks(snapshot)

    assert not (repo / ".cursor").exists()


def test_hook_snapshot_refuses_cursor_directory_symlink(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repository / ".cursor").symlink_to(outside, target_is_directory=True)

    with pytest.raises(mod.PreflightError, match="Cursor project directory"):
        mod.capture_hook_snapshot(repository)

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        (
            "postToolUse",
            {
                "hook_event_name": "postToolUse",
                "conversation_id": "conversation-1",
                "tool_name": "Write",
                "tool_use_id": "tool-1",
                "cwd": "/private/tmp/repo",
                "tool_input": {"path": "/private/tmp/repo/fixture.py"},
            },
        ),
        (
            "postToolUseFailure",
            {
                "hook_event_name": "postToolUseFailure",
                "conversation_id": "conversation-1",
                "tool_name": "Write",
                "tool_use_id": "tool-2",
                "cwd": "/private/tmp/repo",
                "failure": "permission denied",
            },
        ),
        (
            "afterTabFileEdit",
            {
                "hook_event_name": "afterTabFileEdit",
                "conversation_id": "conversation-1",
                "file_path": "/private/tmp/repo/fixture.py",
                "edits": [{"text": "secret source"}],
            },
        ),
    ],
)
def test_recorder_classifies_each_cursor_event(
    tmp_path: Path, event: str, payload: dict[str, object]
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)

    assert mod.record_hook_payload(event, json.dumps(payload), state_dir)

    captured = mod.read_hook_artifact(state_dir, event)
    assert captured == payload
    record_path = mod.hook_record_path(state_dir, event)
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("event", "raw"),
    [
        ("postToolUse", "{not json"),
        ("postToolUse", "[]"),
        ("postToolUse", json.dumps({"hook_event_name": "postToolUse"})),
        (
            "postToolUse",
            json.dumps(
                {
                    "hook_event_name": "postToolUse",
                    "conversation_id": "conversation-1",
                    "tool_name": "Read",
                    "tool_use_id": "tool-1",
                }
            ),
        ),
        (
            "postToolUse",
            json.dumps(
                {
                    "hook_event_name": "postToolUseFailure",
                    "conversation_id": "conversation-1",
                    "tool_name": "Write",
                    "tool_use_id": "tool-1",
                }
            ),
        ),
        (
            "afterTabFileEdit",
            json.dumps(
                {
                    "hook_event_name": "afterTabFileEdit",
                    "conversation_id": "conversation-1",
                    "file_path": "relative.py",
                }
            ),
        ),
    ],
)
def test_recorder_rejects_malformed_or_mismatched_hook_input(
    tmp_path: Path, event: str, raw: str
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)

    assert not mod.record_hook_payload(event, raw, state_dir)
    assert mod.read_hook_artifact(state_dir, event) is None


def test_sanitizer_removes_sensitive_values_and_preserves_join_identifiers(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "private" / "partner-repo"
    worktree = tmp_path / "state" / "worktree"
    state_dir = tmp_path / "state"
    context = mod.RedactionContext(
        repository_path=repo,
        worktree_path=worktree,
        state_path=state_dir,
        repository_name="partner-repo",
        commit_sha="a" * 40,
        sensitive_values=("key_super_secret", "developer@example.com"),
        synthetic_strings=("SUPER SECRET SOURCE",),
    )
    payload = {
        "conversation_id": "conversation-1",
        "generation_id": "generation-1",
        "tool_use_id": "tool-1",
        "cursor.conversation.id": "conversation-1",
        "cursor.request.id": "request-1",
        "cursor.usage_event.id": "usage-1",
        "cursor.event.id": "event-1",
        "cursor.source_event.id": "source-1",
        "changeId": "change-1",
        "commitHash": "a" * 40,
        "authorization": "Basic key_super_secret",
        "userEmail": "developer@example.com",
        "userId": "user_private",
        "repoName": "partner-repo",
        "branchName": "feature/private-customer",
        "commitMessage": "customer ticket and private details",
        "cwd": str(repo),
        "file_path": str(worktree / mod.SYNTHETIC_FIXTURE_PATH),
        "filePath": "src/private-customer-name.py",
        "prompt": "SUPER SECRET SOURCE",
        "tool_input": {"path": str(repo / "private.py"), "content": "secret"},
        "tool_output": "secret output",
        "error": "permission denied for developer@example.com",
    }

    sanitized = mod.sanitize_artifact(payload, context)
    serialized = json.dumps(sanitized, sort_keys=True)

    for identifier in (
        "conversation-1",
        "generation-1",
        "tool-1",
        "request-1",
        "usage-1",
        "event-1",
        "source-1",
        "change-1",
        "a" * 40,
    ):
        assert identifier in serialized
    for secret in (
        "key_super_secret",
        "developer@example.com",
        "user_private",
        "partner-repo",
        str(repo),
        str(worktree),
        "SUPER SECRET SOURCE",
        "secret output",
        "private-customer-name.py",
        "permission denied",
        "feature/private-customer",
        "customer ticket and private details",
    ):
        assert secret not in serialized
    assert sanitized["userEmail"] == "<redacted-email>"
    assert sanitized["userId"] == "<redacted-user-id>"
    assert sanitized["file_path"] == (
        f"<synthetic-workspace>/{mod.SYNTHETIC_FIXTURE_PATH}"
    )
    assert sanitized["filePath"] == "<redacted-path>"
    assert sanitized["branchName"] == "<redacted-branch>"
    assert sanitized["commitMessage"] == "<redacted-commit-message>"


def test_sanitizer_removes_absolute_paths_from_untyped_strings(tmp_path: Path) -> None:
    context = mod.RedactionContext(
        repository_path=tmp_path / "repo",
        worktree_path=tmp_path / "state" / "worktree",
        state_path=tmp_path / "state",
        repository_name="repo",
        commit_sha="9" * 40,
    )

    sanitized = mod.sanitize_artifact(
        {"diagnostic": "read /Users/partner/Library/private.json then retry"},
        context,
    )

    assert sanitized == {"diagnostic": "read <redacted-path> then retry"}


def test_sanitizer_redacts_otel_content_attributes_but_keeps_join_attributes(
    tmp_path: Path,
) -> None:
    context = mod.RedactionContext(
        repository_path=tmp_path / "repo",
        worktree_path=tmp_path / "state" / "worktree",
        state_path=tmp_path / "state",
        repository_name="repo",
        commit_sha="8" * 40,
    )
    payload = {
        "attributes": [
            {
                "key": "cursor.conversation.id",
                "value": {"stringValue": "conversation-1"},
            },
            {
                "key": "gen_ai.prompt",
                "value": {"stringValue": "private customer prompt"},
            },
            {
                "key": "gen_ai.completion",
                "value": {"stringValue": "private model output"},
            },
            {
                "key": "user.id",
                "value": {"stringValue": "encoded-private-developer"},
            },
        ]
    }

    sanitized = mod.sanitize_artifact(payload, context)

    assert sanitized["attributes"][0]["value"]["stringValue"] == "conversation-1"
    assert sanitized["attributes"][1]["value"]["stringValue"] == ("<redacted-content>")
    assert sanitized["attributes"][2]["value"]["stringValue"] == ("<redacted-content>")
    assert sanitized["attributes"][3]["value"]["stringValue"] == ("<redacted-user-id>")


def test_leak_scan_rejects_credentials_emails_paths_and_source(
    tmp_path: Path,
) -> None:
    context = mod.RedactionContext(
        repository_path=tmp_path / "partner-repo",
        worktree_path=tmp_path / "worktree",
        state_path=tmp_path / "state",
        repository_name="partner-repo",
        commit_sha="b" * 40,
        sensitive_values=("key_secret",),
        synthetic_strings=("SYNTHETIC PRIVATE SOURCE",),
    )

    for leaked in (
        b"key_secret",
        b"person@example.com",
        os.fsencode(context.repository_path),
        b"SYNTHETIC PRIVATE SOURCE",
    ):
        with pytest.raises(mod.SanitizationError):
            mod.assert_no_sensitive_bytes(leaked, context)


def test_preflight_refuses_dirty_repository_before_writing(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(mod.PreflightError, match="clean repository"):
        mod.preflight_repository(
            repository, platform_name="Darwin", cursor_version="1.2.3"
        )

    assert not (repository / ".cursor").exists()


def test_preflight_requires_origin_and_git_identity(tmp_path: Path) -> None:
    repository = _repository(tmp_path, with_origin=False)

    with pytest.raises(mod.PreflightError, match="origin"):
        mod.preflight_repository(
            repository, platform_name="Darwin", cursor_version="1.2.3"
        )

    _git(repository, "remote", "add", "origin", "https://example.test/repo.git")
    _git(repository, "config", "user.email", "")
    with pytest.raises(mod.PreflightError, match="user.email"):
        mod.preflight_repository(
            repository, platform_name="Darwin", cursor_version="1.2.3"
        )


def test_preflight_requires_macos_and_cursor(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(mod.PreflightError, match="macOS"):
        mod.preflight_repository(
            repository, platform_name="Linux", cursor_version="1.2.3"
        )
    with pytest.raises(mod.PreflightError, match="Cursor desktop"):
        mod.preflight_repository(
            repository, platform_name="Darwin", cursor_version=None
        )


def test_preflight_requires_python_312(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(mod.PreflightError, match="Python 3.12"):
        mod.preflight_repository(
            repository,
            platform_name="Darwin",
            cursor_version="1.2.3",
            python_version=(3, 11),
        )


def test_preflight_refuses_existing_synthetic_fixture(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    fixture = repository / mod.SYNTHETIC_FIXTURE_PATH
    fixture.write_text("user-owned\n", encoding="utf-8")
    _git(repository, "add", mod.SYNTHETIC_FIXTURE_PATH)
    _git(repository, "commit", "-qm", "add user fixture")

    with pytest.raises(mod.PreflightError, match="synthetic fixture path"):
        mod.preflight_repository(
            repository, platform_name="Darwin", cursor_version="1.2.3"
        )


def test_isolated_worktree_keeps_original_checkout_and_cleans_owned_state(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    preflight = mod.preflight_repository(
        repository, platform_name="Darwin", cursor_version="1.2.3"
    )
    original_branch = _git(repository, "branch", "--show-current")
    original_head = _git(repository, "rev-parse", "HEAD")
    state_dir = tmp_path / "private-state"
    state_dir.mkdir(mode=0o700)

    state = mod.create_isolated_worktree(preflight, state_dir, "run123")

    assert state.worktree_path.exists()
    assert _git(repository, "branch", "--show-current") == original_branch
    assert _git(repository, "rev-parse", "HEAD") == original_head

    mod.cleanup_isolated_worktree(state)

    assert not state.worktree_path.exists()
    branches = _git(repository, "branch", "--format=%(refname:short)").splitlines()
    assert state.branch_name not in branches


def test_cleanup_refuses_a_worktree_that_changed_branch(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    preflight = mod.preflight_repository(
        repository, platform_name="Darwin", cursor_version="1.2.3"
    )
    state_dir = tmp_path / "private-state"
    state_dir.mkdir(mode=0o700)
    state = mod.create_isolated_worktree(preflight, state_dir, "run123")
    _git(state.worktree_path, "switch", "--detach", "-q")

    with pytest.raises(mod.CleanupError, match="ownership"):
        mod.cleanup_isolated_worktree(state)

    assert state.worktree_path.exists()
    _git(state.worktree_path, "switch", "-q", state.branch_name)
    mod.cleanup_isolated_worktree(state)


def test_cleanup_refuses_a_worktree_with_an_unrecorded_commit(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    preflight = mod.preflight_repository(
        repository, platform_name="Darwin", cursor_version="1.2.3"
    )
    state_dir = tmp_path / "private-state"
    state_dir.mkdir(mode=0o700)
    state = mod.create_isolated_worktree(preflight, state_dir, "run123")
    added = state.worktree_path / "partner-work.txt"
    added.write_text("preserve\n", encoding="utf-8")
    _git(state.worktree_path, "add", "partner-work.txt")
    _git(state.worktree_path, "commit", "-qm", "partner work")

    with pytest.raises(mod.CleanupError, match="commit"):
        mod.cleanup_isolated_worktree(state)

    assert state.worktree_path.exists()
    assert added.read_text(encoding="utf-8") == "preserve\n"
    assert (
        state.branch_name
        in _git(repository, "branch", "--format=%(refname:short)").splitlines()
    )
    _git(state.worktree_path, "reset", "--hard", "-q", state.starting_commit)
    mod.cleanup_isolated_worktree(state)


@pytest.mark.parametrize("change_kind", ["ignored", "modified", "untracked"])
def test_cleanup_refuses_a_dirty_worktree(tmp_path: Path, change_kind: str) -> None:
    repository = _repository(tmp_path)
    if change_kind == "ignored":
        (repository / ".gitignore").write_text("*.private\n", encoding="utf-8")
        _git(repository, "add", ".gitignore")
        _git(repository, "commit", "-qm", "ignore private files")
    preflight = mod.preflight_repository(
        repository, platform_name="Darwin", cursor_version="1.2.3"
    )
    state_dir = tmp_path / "private-state"
    state_dir.mkdir(mode=0o700)
    state = mod.create_isolated_worktree(preflight, state_dir, "run123")
    changed_name = {
        "ignored": "partner-work.private",
        "modified": "tracked.py",
        "untracked": "partner-work.txt",
    }[change_kind]
    changed = state.worktree_path / changed_name
    changed.write_text("preserve\n", encoding="utf-8")

    with pytest.raises(mod.CleanupError, match="uncommitted"):
        mod.cleanup_isolated_worktree(state)

    assert state.worktree_path.exists()
    assert changed.read_text(encoding="utf-8") == "preserve\n"
    if change_kind == "modified":
        _git(state.worktree_path, "restore", "tracked.py")
    else:
        changed.unlink()
    mod.cleanup_isolated_worktree(state)


def test_private_state_cleanup_preserves_an_unverified_worktree(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    worktree = state_dir / "worktree"
    worktree.mkdir(parents=True)
    sentinel = worktree / "user-data"
    sentinel.write_text("preserve\n", encoding="utf-8")

    with pytest.raises(mod.CleanupError, match="inspect"):
        mod.cleanup_private_state(state_dir, worktree_removed=False)

    assert sentinel.read_text(encoding="utf-8") == "preserve\n"
    mod.cleanup_private_state(state_dir, worktree_removed=True)
    assert not state_dir.exists()


def test_commit_refuses_any_extra_staged_path(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    preflight = mod.preflight_repository(
        repository, platform_name="Darwin", cursor_version="1.2.3"
    )
    state_dir = tmp_path / "private-state"
    state_dir.mkdir(mode=0o700)
    state = mod.create_isolated_worktree(preflight, state_dir, "run123")
    fixture = state.worktree_path / mod.SYNTHETIC_FIXTURE_PATH
    fixture.write_text("FIXTURE = True\n", encoding="utf-8")
    foreign = state.worktree_path / "foreign.txt"
    foreign.write_text("foreign\n", encoding="utf-8")
    _git(state.worktree_path, "add", "foreign.txt")

    with pytest.raises(mod.CollectorError, match="staged path"):
        mod.commit_synthetic_fixture(state)

    _git(state.worktree_path, "reset", "-q", "HEAD", "--", "foreign.txt")
    foreign.unlink()
    commit_sha = mod.commit_synthetic_fixture(state)
    state = replace(state, expected_commit=commit_sha)
    assert commit_sha == _git(state.worktree_path, "rev-parse", "HEAD")
    assert _git(state.worktree_path, "show", "--format=", "--name-only").strip() == (
        mod.SYNTHETIC_FIXTURE_PATH
    )
    mod.cleanup_isolated_worktree(state)


def test_api_client_collects_commit_changes_and_details_without_leaking_key(
    cursor_api_server, tmp_path: Path
) -> None:
    base_url, handler = cursor_api_server
    commit_sha = "c" * 40
    handler.responses = {
        "/analytics/ai-code/commits": [
            (
                200,
                {
                    "items": [
                        {
                            "commitHash": commit_sha,
                            "userId": "user_private",
                            "userEmail": "partner@example.test",
                            "repoName": "private-repo",
                        }
                    ],
                    "totalCount": 1,
                    "page": 1,
                    "pageSize": 1000,
                },
                0.0,
            )
        ],
        "/analytics/ai-code/changes": [
            (200, {"items": [{"changeId": "change-1"}]}, 0.0)
        ],
        f"/analytics/ai-code/commits/{commit_sha}": [
            (200, {"commitHash": commit_sha, "details": []}, 0.0)
        ],
    }
    api_key = "key_super_secret"
    client = mod.CursorApiClient(api_key, base_url=base_url, timeout=1.0)

    artifacts = mod.collect_ai_code_artifacts(
        client,
        commit_sha,
        start_date="2026-09-03T00:00:00Z",
        end_date="now",
        max_attempts=1,
        poll_interval=0,
    )

    assert {name: artifact.status for name, artifact in artifacts.items()} == {
        "commits": "captured",
        "changes": "captured",
        "commit_details": "captured",
    }
    expected_auth = "Basic " + base64.b64encode(f"{api_key}:".encode()).decode()
    assert handler.requests
    assert all(authorization == expected_auth for _, authorization in handler.requests)
    assert all(api_key not in request_path for request_path, _ in handler.requests)
    assert api_key not in repr(artifacts)
    assert not any(
        api_key in path.read_text(errors="ignore")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"error": "unauthorized key_super_secret"}, "access_denied"),
        (500, {"error": "internal key_super_secret"}, "request_failed"),
        (200, b"{not json key_super_secret", "request_failed"),
    ],
)
def test_api_failures_have_closed_statuses_and_no_key_in_diagnostics(
    cursor_api_server, status: int, body: object, expected: str
) -> None:
    base_url, handler = cursor_api_server
    handler.responses = {"/analytics/ai-code/commits": [(status, body, 0.0)]}
    api_key = "key_super_secret"
    client = mod.CursorApiClient(api_key, base_url=base_url, timeout=1.0)

    artifacts = mod.collect_ai_code_artifacts(
        client,
        "d" * 40,
        start_date="7d",
        end_date="now",
        max_attempts=1,
        poll_interval=0,
    )

    assert artifacts["commits"].status == expected
    assert api_key not in repr(artifacts)


def test_api_client_refuses_redirect_without_forwarding_authorization() -> None:
    received_authorization: list[str | None] = []

    class DestinationHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            received_authorization.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    destination = ThreadingHTTPServer(("127.0.0.1", 0), DestinationHandler)
    destination_thread = threading.Thread(target=destination.serve_forever, daemon=True)
    destination_thread.start()

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{destination.server_port}/outside-cursor",
            )
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    redirect_thread.start()
    try:
        client = mod.CursorApiClient(
            "key_redirect_secret",
            base_url=f"http://127.0.0.1:{redirect.server_port}",
            timeout=1.0,
        )

        response = client.get("/analytics/ai-code/commits", {})

        assert response.status == "request_failed"
        assert received_authorization == []
    finally:
        redirect.shutdown()
        redirect.server_close()
        redirect_thread.join()
        destination.shutdown()
        destination.server_close()
        destination_thread.join()


def test_api_timeout_is_request_failed(cursor_api_server) -> None:
    base_url, handler = cursor_api_server
    handler.responses = {"/analytics/ai-code/commits": [(200, {"items": []}, 0.1)]}
    client = mod.CursorApiClient("key_timeout", base_url=base_url, timeout=0.01)

    artifacts = mod.collect_ai_code_artifacts(
        client,
        "e" * 40,
        start_date="7d",
        end_date="now",
        max_attempts=1,
        poll_interval=0,
    )

    assert artifacts["commits"].status == "request_failed"


def test_missing_detail_access_and_no_matching_commit_are_visible(
    cursor_api_server,
) -> None:
    base_url, handler = cursor_api_server
    commit_sha = "f" * 40
    handler.responses = {
        "/analytics/ai-code/commits": [
            (200, {"items": [{"commitHash": commit_sha}]}, 0.0)
        ],
        "/analytics/ai-code/changes": [(200, {"items": []}, 0.0)],
        f"/analytics/ai-code/commits/{commit_sha}": [
            (404, {"error": "limited alpha"}, 0.0)
        ],
    }
    artifacts = mod.collect_ai_code_artifacts(
        mod.CursorApiClient("key_alpha", base_url=base_url),
        commit_sha,
        start_date="7d",
        end_date="now",
        max_attempts=1,
        poll_interval=0,
    )
    assert artifacts["commit_details"].status == "not_available"

    handler.responses = {
        "/analytics/ai-code/commits": [
            (200, {"items": []}, 0.0),
            (200, {"items": []}, 0.0),
        ]
    }
    missing = mod.collect_ai_code_artifacts(
        mod.CursorApiClient("key_missing", base_url=base_url),
        "0" * 40,
        start_date="7d",
        end_date="now",
        max_attempts=2,
        poll_interval=0,
    )
    assert {artifact.status for artifact in missing.values()} == {"no_matching_record"}


def test_optional_import_reports_valid_invalid_and_correlation(tmp_path: Path) -> None:
    valid_path = tmp_path / "otel.json"
    valid_path.write_text(
        json.dumps({"cursor.conversation.id": "conversation-1"}), encoding="utf-8"
    )
    invalid_path = tmp_path / "gateway.json"
    invalid_path.write_text("{not json", encoding="utf-8")

    valid = mod.load_optional_json(valid_path)
    invalid = mod.load_optional_json(invalid_path)
    absent = mod.load_optional_json(None)

    assert valid.status == "captured"
    assert invalid.status == "invalid_input"
    assert absent.status == "not_configured"
    captured = {
        "hook": mod.ArtifactResult("captured", {"conversation_id": "conversation-1"})
    }
    assert mod.correlate_artifact(valid.data, captured) == "matched"
    assert (
        mod.correlate_artifact({"cursor.request.id": "request-unmatched"}, captured)
        == "unmatched"
    )
    otel_shape = {
        "attributes": [
            {
                "key": "cursor.conversation.id",
                "value": {"stringValue": "conversation-1"},
            }
        ]
    }
    assert mod.correlate_artifact(otel_shape, captured) == "matched"


def test_optional_otel_validation_requires_cursor_api_request(tmp_path: Path) -> None:
    valid = tmp_path / "valid.json"
    valid.write_text(
        json.dumps(
            {
                "body": {"stringValue": "cursor.api.request"},
                "attributes": [],
            }
        ),
        encoding="utf-8",
    )
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"event": "other"}), encoding="utf-8")

    assert mod.load_optional_json(valid, expected_kind="otel").status == "captured"
    assert mod.load_optional_json(wrong, expected_kind="otel").status == (
        "invalid_input"
    )


def test_archive_contains_only_captured_sanitized_artifacts(tmp_path: Path) -> None:
    encoded_key = base64.b64encode(b"key_archive_secret:").decode()
    context = mod.RedactionContext(
        repository_path=tmp_path / "partner-repo",
        worktree_path=tmp_path / "state" / "worktree",
        state_path=tmp_path / "state",
        repository_name="partner-repo",
        commit_sha="1" * 40,
        sensitive_values=("key_archive_secret", "partner@example.test"),
        synthetic_strings=("PRIVATE SOURCE",),
    )
    artifacts = {
        "hooks/post-tool-use.json": mod.ArtifactResult(
            "captured",
            {
                "conversation_id": "conversation-1",
                "cwd": str(context.repository_path),
                "prompt": "PRIVATE SOURCE",
            },
        ),
        "hooks/post-tool-use-failure.json": mod.ArtifactResult("not_observed"),
        "ai-code/commits.json": mod.ArtifactResult(
            "captured",
            {
                "items": [
                    {
                        "commitHash": context.commit_sha,
                        "userEmail": "partner@example.test",
                    }
                ]
            },
        ),
        "ai-code/commit-details.json": mod.ArtifactResult(
            "captured",
            {
                "commitHash": context.commit_sha,
                "title": "Private customer task",
                "tldr": "Private prompt summary",
                "overview": "Private repository overview",
                "summaryBullets": [
                    "Private first detail",
                    "Private second detail",
                ],
            },
        ),
        "ai-code/changes.json": mod.ArtifactResult(
            "captured",
            {
                "items": [
                    {
                        "changeId": "change-private-path",
                        "filePath": "src/private-customer-name.py",
                    }
                ]
            },
        ),
        "otel/cursor-api-request.json": mod.ArtifactResult("not_configured"),
        "gateway/request.json": mod.ArtifactResult(
            "captured", {"diagnostic": f"Basic {encoded_key}"}
        ),
    }
    manifest = mod.build_manifest(
        run_id="run123",
        cursor_version="1.2.3",
        macos_version="15.6",
        started_at="2026-09-04T10:00:00Z",
        finished_at="2026-09-04T10:05:00Z",
        commit_sha=context.commit_sha,
        artifacts=artifacts,
        correlations={"otel/cursor-api-request.json": "not_configured"},
    )

    archive = mod.write_archive(
        tmp_path / "output", "run123", manifest, artifacts, context
    )

    with zipfile.ZipFile(archive) as bundle:
        assert set(bundle.namelist()) == {
            "manifest.json",
            "hooks/post-tool-use.json",
            "ai-code/commits.json",
            "ai-code/commit-details.json",
            "ai-code/changes.json",
            "gateway/request.json",
        }
        combined = b"".join(bundle.read(name) for name in bundle.namelist())
        details = json.loads(bundle.read("ai-code/commit-details.json"))
    mod.assert_no_sensitive_bytes(combined, context)
    assert b"conversation-1" in combined
    assert context.commit_sha.encode() in combined
    assert encoded_key.encode() not in combined
    assert b"private-customer-name.py" not in combined
    assert details["title"] == "<redacted-content>"
    assert details["tldr"] == "<redacted-content>"
    assert details["overview"] == "<redacted-content>"
    assert details["summaryBullets"] == [
        "<redacted-content>",
        "<redacted-content>",
    ]


def test_archive_temporary_file_is_private_while_zip_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_modes: list[int] = []
    zip_file = mod.zipfile.ZipFile

    class InspectingZipFile(zip_file):
        def __enter__(self):
            assert self.fp is not None
            observed_modes.append(stat.S_IMODE(os.fstat(self.fp.fileno()).st_mode))
            return super().__enter__()

    monkeypatch.setattr(mod.zipfile, "ZipFile", InspectingZipFile)
    context = mod.RedactionContext(
        repository_path=tmp_path / "repo",
        worktree_path=tmp_path / "state" / "worktree",
        state_path=tmp_path / "state",
        repository_name="repo",
        commit_sha="3" * 40,
    )
    old_umask = os.umask(0o022)
    try:
        mod.write_archive(
            tmp_path / "output",
            "run123",
            {"run_id": "run123"},
            {},
            context,
        )
    finally:
        os.umask(old_umask)

    assert observed_modes == [0o600]


def test_manifest_rejects_status_outside_closed_vocabulary() -> None:
    with pytest.raises(ValueError, match="artifact status"):
        mod.build_manifest(
            run_id="run123",
            cursor_version="1.2.3",
            macos_version="15.6",
            started_at="start",
            finished_at="finish",
            commit_sha="2" * 40,
            artifacts={"hook": mod.ArtifactResult("invented")},
            correlations={},
        )


def _synthetic_capture(worktree: Path, state_dir: Path, *args: object) -> dict:
    del args
    fixture = worktree / mod.SYNTHETIC_FIXTURE_PATH
    fixture.write_text(mod.SYNTHETIC_SUCCESS_CONTENT, encoding="utf-8")
    payloads = {
        "postToolUse": {
            "hook_event_name": "postToolUse",
            "conversation_id": "conversation-run",
            "tool_name": "Write",
            "tool_use_id": "tool-success",
            "cwd": str(worktree),
            "tool_input": {
                "path": str(fixture),
                "content": mod.SYNTHETIC_SUCCESS_CONTENT,
            },
        },
        "postToolUseFailure": {
            "hook_event_name": "postToolUseFailure",
            "conversation_id": "conversation-run",
            "tool_name": "Write",
            "tool_use_id": "tool-failure",
            "cwd": str(worktree),
            "error": "permission denied",
        },
        "afterTabFileEdit": {
            "hook_event_name": "afterTabFileEdit",
            "conversation_id": "conversation-run",
            "file_path": str(fixture),
            "edits": [{"text": mod.SYNTHETIC_TAB_CONTENT}],
        },
    }
    for event, payload in payloads.items():
        assert mod.record_hook_payload(event, json.dumps(payload), state_dir)
    fixture.write_text(
        mod.SYNTHETIC_SUCCESS_CONTENT + mod.SYNTHETIC_TAB_CONTENT,
        encoding="utf-8",
    )
    return mod.collect_recorded_hook_artifacts(state_dir, worktree)


def test_record_hook_mode_always_exits_zero_and_records_valid_input(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    payload = {
        "hook_event_name": "postToolUse",
        "conversation_id": "conversation-cli",
        "tool_name": "Write",
        "tool_use_id": "tool-cli",
        "cwd": str(tmp_path),
    }

    valid = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--record-hook",
            "postToolUse",
            "--state-dir",
            str(state_dir),
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )
    malformed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--record-hook",
            "postToolUse",
            "--state-dir",
            str(state_dir),
        ],
        input="{not json",
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert valid.returncode == malformed.returncode == 0
    assert valid.stdout == valid.stderr == malformed.stdout == malformed.stderr == ""
    assert mod.read_hook_artifact(state_dir, "postToolUse") == payload


def test_help_documents_partner_entry_point_and_hides_recorder() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "repository" in result.stdout
    assert "--otel-json" in result.stdout
    assert "--gateway-json" in result.stdout
    assert "--output" in result.stdout
    assert "--record-hook" not in result.stdout


def test_recorded_hooks_require_success_and_tab_but_expose_missing_failure(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    fixture = worktree / mod.SYNTHETIC_FIXTURE_PATH
    fixture.write_text(mod.SYNTHETIC_SUCCESS_CONTENT, encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    success = {
        "hook_event_name": "postToolUse",
        "conversation_id": "conversation-1",
        "tool_name": "Write",
        "tool_use_id": "tool-1",
        "cwd": str(worktree),
    }
    assert mod.record_hook_payload("postToolUse", json.dumps(success), state_dir)

    with pytest.raises(mod.CollectorError, match="Tab"):
        mod.collect_recorded_hook_artifacts(state_dir, worktree)

    tab = {
        "hook_event_name": "afterTabFileEdit",
        "conversation_id": "conversation-1",
        "file_path": str(fixture),
    }
    assert mod.record_hook_payload("afterTabFileEdit", json.dumps(tab), state_dir)
    fixture.write_text(
        mod.SYNTHETIC_SUCCESS_CONTENT + mod.SYNTHETIC_TAB_CONTENT,
        encoding="utf-8",
    )
    artifacts = mod.collect_recorded_hook_artifacts(state_dir, worktree)
    assert artifacts["hooks/post-tool-use.json"].status == "captured"
    assert artifacts["hooks/post-tool-use-failure.json"].status == "not_observed"
    assert artifacts["hooks/after-tab-file-edit.json"].status == "captured"


def test_guided_capture_runs_three_steps_and_allows_unobserved_failure(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    fixture = worktree / mod.SYNTHETIC_FIXTURE_PATH
    prompts: list[str] = []

    def respond(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            fixture.write_text(mod.SYNTHETIC_SUCCESS_CONTENT, encoding="utf-8")
            payload = {
                "hook_event_name": "postToolUse",
                "conversation_id": "conversation-guide",
                "tool_name": "Write",
                "tool_use_id": "tool-guide",
                "cwd": str(worktree),
            }
            assert mod.record_hook_payload(
                "postToolUse", json.dumps(payload), state_dir
            )
        elif len(prompts) == 3:
            fixture.write_text(
                mod.SYNTHETIC_SUCCESS_CONTENT + mod.SYNTHETIC_TAB_CONTENT,
                encoding="utf-8",
            )
            payload = {
                "hook_event_name": "afterTabFileEdit",
                "conversation_id": "conversation-guide",
                "file_path": str(fixture),
            }
            assert mod.record_hook_payload(
                "afterTabFileEdit", json.dumps(payload), state_dir
            )
        return ""

    artifacts = mod.guide_capture_steps(
        worktree, state_dir, respond, lambda message: None
    )

    assert len(prompts) == 3
    assert artifacts["hooks/post-tool-use-failure.json"].status == "not_observed"
    assert stat.S_IMODE(fixture.stat().st_mode) == 0o644


def test_collector_success_writes_archive_and_cleans_temporary_git_state(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    original_head = _git(repository, "rev-parse", "HEAD")
    output = tmp_path / "output"
    otel = tmp_path / "otel.json"
    otel.write_text(
        json.dumps(
            {
                "body": {"stringValue": "cursor.api.request"},
                "cursor.conversation.id": "conversation-other",
            }
        ),
        encoding="utf-8",
    )
    observed: dict[str, Path] = {}

    def capture(worktree: Path, state_dir: Path, *args: object) -> dict:
        observed["worktree"] = worktree
        observed["state"] = state_dir
        return _synthetic_capture(worktree, state_dir, *args)

    archive = mod.run_collector(
        repository,
        otel_json=otel,
        gateway_json=None,
        output_directory=output,
        input_fn=lambda prompt: "",
        output_fn=lambda message: None,
        getpass_fn=lambda prompt: "",
        open_cursor_fn=lambda worktree: None,
        capture_steps_fn=capture,
        platform_name="Darwin",
        cursor_version="1.2.3",
        macos_version="15.6",
    )

    assert archive.exists()
    assert _git(repository, "rev-parse", "HEAD") == original_head
    assert _git(repository, "status", "--porcelain=v1") == ""
    assert not observed["worktree"].exists()
    assert not observed["state"].exists()
    assert not any(
        branch.startswith("sediment/cursor-fixture-")
        for branch in _git(
            repository, "branch", "--format=%(refname:short)"
        ).splitlines()
    )
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
    assert manifest["artifacts"]["ai-code/commits.json"]["status"] == ("not_configured")
    assert manifest["artifacts"]["hooks/post-tool-use.json"]["status"] == ("captured")
    assert manifest["artifacts"]["otel/cursor-api-request.json"]["correlation"] == (
        "unmatched"
    )


def test_termination_handler_uses_interrupt_cleanup_path() -> None:
    with mod.termination_signal_guard():
        handler = mod.signal.getsignal(mod.signal.SIGTERM)
        with pytest.raises(KeyboardInterrupt):
            handler(mod.signal.SIGTERM, None)


@pytest.mark.parametrize(
    "failure", [KeyboardInterrupt(), mod.CollectorError("handled")]
)
def test_collector_cleans_worktree_after_interruption_or_handled_failure(
    tmp_path: Path, failure: BaseException
) -> None:
    repository = _repository(tmp_path)
    output = tmp_path / "output"
    observed: dict[str, Path] = {}

    def fail(worktree: Path, state_dir: Path, *args: object) -> dict:
        del args
        observed["worktree"] = worktree
        observed["state"] = state_dir
        raise failure

    with pytest.raises(type(failure)):
        mod.run_collector(
            repository,
            otel_json=None,
            gateway_json=None,
            output_directory=output,
            input_fn=lambda prompt: "",
            output_fn=lambda message: None,
            getpass_fn=lambda prompt: "",
            open_cursor_fn=lambda worktree: None,
            capture_steps_fn=fail,
            platform_name="Darwin",
            cursor_version="1.2.3",
            macos_version="15.6",
        )

    assert _git(repository, "status", "--porcelain=v1") == ""
    assert not observed["worktree"].exists()
    assert not observed["state"].exists()
    assert not output.exists()

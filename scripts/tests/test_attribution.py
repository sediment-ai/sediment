# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the notes-attribution stamper (scripts/sediment_attribution.py).

Every test drives the real CLI against real git repos (no mocks), per the repo
testing conventions. The amend/rebase tests double as the rewrite-survival
verification: post-commit fires on --amend, and notes.rewriteRef carries the
note across history rewrites.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "cli" / "sediment_cli" / "attribution.py"
TRANSCRIPT = Path(__file__).parents[2] / "cli" / "sediment_cli" / "transcript.py"


@pytest.fixture(autouse=True)
def _hermetic_path(monkeypatch):
    """Pin PATH for every test: the stamper resolves an installed `sediment`
    executable from PATH at install time, so a developer's real
    ~/.local/bin/sediment would silently flip every hook fixture from the
    checkout-fallback form to the installed form (and break the moved-script
    doctor test). /usr/bin:/bin keeps git and sh available; subprocesses run
    via absolute sys.executable."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


NOTES_REF = "refs/notes/sediment"


@pytest.fixture(autouse=True)
def _isolate_real_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # run_cli redirects the attribution log and config per call, but the
    # in-process tests (mod.main, _log_event) read os.environ directly — one
    # such path stamped a phantom notes-push-failed into the developer's real
    # ~/.sediment/attribution.log on every suite run, and doctor then reported
    # dozens of them. Isolate at the process-environment level so no
    # invocation path can escape.
    monkeypatch.setenv("SEDIMENT_ATTRIBUTION_LOG", str(tmp_path / "attribution.log"))
    monkeypatch.setenv("SEDIMENT_ATTRIBUTION_CONFIG", str(tmp_path / "no-config.json"))
    for name in (
        "SEDIMENT_OTLP_ENDPOINT",
        "SEDIMENT_INGEST_TOKEN",
        "SEDIMENT_API_BEARER_TOKEN",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "SEDIMENT_DELIVERY_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def _load_module():
    """Import the stamper as a module for direct unit tests (scripts/ is not a
    package, so load it by path)."""
    spec = importlib.util.spec_from_file_location("sediment_attribution", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_cli(
    args: list[str],
    cwd: Path,
    stdin: str = "",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    # Redirect the attribution log into the test tree so the suite never
    # writes to the developer's real ~/.sediment/attribution.log, and pin the
    # config to a nonexistent path so a developer's real config.json can
    # never flip auto-install on under the tests.
    env = {
        **os.environ,
        "SEDIMENT_ATTRIBUTION_LOG": str(attribution_log(cwd)),
        "SEDIMENT_ATTRIBUTION_CONFIG": str(cwd / "no-config.json"),
        **(extra_env or {}),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


class _CursorDecisionCapture(BaseHTTPRequestHandler):
    requests: list[tuple[str, str | None, dict]] = []
    response_status = 200

    def do_POST(self) -> None:  # noqa: N802 — stdlib callback name
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.requests.append(
            (self.path, self.headers.get("Authorization"), json.loads(body))
        )
        self.send_response(self.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def cursor_decision_endpoint(monkeypatch):
    monkeypatch.setenv("SEDIMENT_INGEST_TOKEN", "cursor-token")
    _CursorDecisionCapture.requests.clear()
    _CursorDecisionCapture.response_status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CursorDecisionCapture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def attribution_log(cwd: Path) -> Path:
    # Keep logs beside the repo. Mark logs successful operations and misses,
    # so a test that `git add -A`s
    # the repo after marking would otherwise sweep this file into a commit
    # -- and a later stamp's write to it then blocks any git checkout as an
    # uncommitted local change to a tracked file.
    return cwd.parent / f"{cwd.name}-attribution-test.log"


def log_entries(repo: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in attribution_log(repo).read_text().strip().splitlines()
    ]


def log_events(repo: Path) -> list[str]:
    return [entry["event"] for entry in log_entries(repo)]


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, f"git {args} failed: {out.stderr}"
    return out.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@local")
    git(path, "config", "user.name", "t")
    (path / "a.py").write_text("x = 1\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    return path


def mark(
    repo: Path,
    tool: str = "claude-code",
    session: str = "sess-1",
    extra_env: dict[str, str] | None = None,
) -> None:
    payload = json.dumps({"session_id": session, "cwd": str(repo)})
    result = run_cli(
        ["mark", "--tool", tool], cwd=repo, stdin=payload, extra_env=extra_env
    )
    assert result.returncode == 0


def git_dir(repo: Path) -> Path:
    return Path(git(repo, "rev-parse", "--absolute-git-dir"))


def hooks_dir(repo: Path) -> Path:
    return git_dir(repo) / "hooks"


def marker_file(repo: Path) -> Path:
    return git_dir(repo) / "sediment-sessions"


def note_on(repo: Path, ref: str = "HEAD") -> dict | None:
    out = subprocess.run(
        ["git", "notes", f"--ref={NOTES_REF}", "show", ref],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout) if out.returncode == 0 else None


def test_mark_writes_and_dedupes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    mark(repo)
    mark(repo)  # same session — must dedupe
    mark(repo, tool="codex", session="sess-2")
    lines = marker_file(repo).read_text().strip().splitlines()
    assert len(lines) == 2
    entries = [json.loads(line) for line in lines]
    assert {(e["tool"], e["session_id"]) for e in entries} == {
        ("claude-code", "sess-1"),
        ("codex", "sess-2"),
    }


def test_mark_uses_thread_id_fallback(tmp_path: Path) -> None:
    # Codex payloads may carry thread_id instead of session_id.
    repo = make_repo(tmp_path / "r")
    payload = json.dumps({"thread_id": "th-9", "cwd": str(repo)})
    assert run_cli(["mark", "--tool", "codex"], cwd=repo, stdin=payload).returncode == 0
    [entry] = [json.loads(line) for line in marker_file(repo).read_text().splitlines()]
    assert entry["session_id"] == "th-9"


def test_mark_outside_worktree_is_noop(tmp_path: Path) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    payload = json.dumps({"session_id": "s", "cwd": str(outside)})
    result = run_cli(["mark", "--tool", "claude-code"], cwd=outside, stdin=payload)
    assert result.returncode == 0
    assert not list(outside.rglob("sediment-sessions"))


def test_mark_malformed_stdin_exits_zero(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    for bad in ("", "not json", '["list"]', '{"cwd": 3}'):
        assert run_cli(["mark", "--tool", "codex"], cwd=repo, stdin=bad).returncode == 0
    assert not marker_file(repo).exists()


def _cursor_payload(repo: Path, **overrides: object) -> dict:
    payload = {
        "conversation_id": "cursor-session-1",
        "generation_id": "cursor-generation-1",
        "hook_event_name": "postToolUse",
        "tool_name": "Write",
        "tool_use_id": "cursor-call-1",
        "cwd": str(repo),
        "tool_input": {"path": str(repo / "private.py"), "content": "secret"},
        "tool_output": '{"result":"private output"}',
        "model": "private-model",
        "transcript_path": "/private/transcript.jsonl",
        "user_email": "private@example.com",
    }
    payload.update(overrides)
    return payload


def _otlp_string_attributes(record: dict) -> dict[str, object]:
    values: dict[str, object] = {}
    for attribute in record["attributes"]:
        value = attribute["value"]
        values[attribute["key"]] = next(iter(value.values()))
    return values


def _native_cursor_payload(repo: Path) -> dict:
    fixture = Path(__file__).parent / "fixtures/cursor/post-tool-use-3.18.25.json"
    return json.loads(fixture.read_text().replace("/fixture/repository", str(repo)))


@pytest.mark.parametrize("include_tool_input", [False, True])
def test_cursor_native_workspace_write_reaches_commit_note(
    tmp_path: Path, include_tool_input: bool
) -> None:
    repo = make_repo(tmp_path / "edited")
    unrelated = make_repo(tmp_path / "hook-working-directory")
    payload = _native_cursor_payload(repo)
    (repo / "cursor_native.py").write_text(payload["tool_input"]["contents"])
    if not include_tool_input:
        del payload["tool_input"]
    assert (
        run_cli(["install", str(repo), "--no-agents", "--no-env"], cwd=repo).returncode
        == 0
    )

    result = run_cli(["cursor-hook"], cwd=unrelated, stdin=json.dumps(payload))

    assert result.returncode == 0, result.stderr
    assert marker_file(repo).exists(), result.stderr
    assert not marker_file(unrelated).exists()
    git(repo, "add", "cursor_native.py")
    git(repo, "commit", "-qm", "write from Cursor")
    [session] = note_on(repo)["sessions"]
    assert session["tool"] == "cursor"
    assert session["session_id"] == payload["conversation_id"]
    assert session["stamped_at"]


@pytest.mark.parametrize("reverse_roots", [False, True])
def test_cursor_absolute_write_selects_nested_repository(
    tmp_path: Path, reverse_roots: bool
) -> None:
    outer = make_repo(tmp_path / "outer")
    nested = make_repo(outer / "nested")
    other = make_repo(tmp_path / "other")
    payload = _native_cursor_payload(nested)
    payload["cwd"] = str(outer)
    payload["workspace_roots"] = [str(outer), str(other)]
    if reverse_roots:
        payload["workspace_roots"].reverse()
    (nested / "cursor_native.py").write_text(payload["tool_input"]["contents"])
    assert (
        run_cli(
            ["install", str(nested), "--no-agents", "--no-env"], cwd=nested
        ).returncode
        == 0
    )

    result = run_cli(["cursor-hook"], cwd=other, stdin=json.dumps(payload))

    assert result.returncode == 0, result.stderr
    assert marker_file(nested).exists(), result.stderr
    assert not marker_file(outer).exists()
    assert not marker_file(other).exists()
    git(nested, "add", "cursor_native.py")
    git(nested, "commit", "-qm", "write in nested repository")
    [session] = note_on(nested)["sessions"]
    assert session["tool"] == "cursor"
    assert session["session_id"] == payload["conversation_id"]
    assert session["stamped_at"]


@pytest.mark.parametrize("event", ["postToolUse", "postToolUseFailure"])
def test_cursor_relative_write_resolves_within_one_workspace(
    tmp_path: Path, event: str
) -> None:
    workspace = tmp_path / "workspace"
    repo = make_repo(workspace / "nested")
    payload = _native_cursor_payload(repo)
    payload["workspace_roots"] = [str(workspace)]
    payload["tool_input"]["path"] = "nested/cursor_native.py"
    payload["hook_event_name"] = event

    result = run_cli(["cursor-hook"], cwd=tmp_path, stdin=json.dumps(payload))

    assert result.returncode == 0, result.stderr
    assert marker_file(repo).exists(), result.stderr


@pytest.mark.parametrize("relative_path", [None, "cursor_native.py"])
def test_cursor_ambiguous_roots_skip_mark_but_keep_decision(
    tmp_path: Path, cursor_decision_endpoint: str, relative_path: str | None
) -> None:
    first = make_repo(tmp_path / "first")
    second = make_repo(tmp_path / "second")
    payload = _native_cursor_payload(first)
    payload["workspace_roots"] = [str(first), str(second)]
    if relative_path is None:
        del payload["tool_input"]
    else:
        payload["tool_input"]["path"] = relative_path
        # An existing same-named file doesn't establish which root the tool used.
        (first / relative_path).write_text("unrelated prior file\n")

    result = run_cli(
        ["cursor-hook"],
        cwd=first,
        stdin=json.dumps(payload),
        extra_env={"SEDIMENT_OTLP_ENDPOINT": cursor_decision_endpoint},
    )

    assert result.returncode == 0, result.stderr
    assert not marker_file(first).exists()
    assert not marker_file(second).exists()
    assert "ambiguous repository directory; Attribution mark skipped" in result.stderr
    assert len(_CursorDecisionCapture.requests) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"workspace_roots": "not-an-array"},
        {"workspace_roots": ["relative/path"]},
        {"workspace_roots": [None]},
        {"workspace_roots": ["/fixture/repository", 17]},
        {"workspace_roots": ["/fixture/repository", "/missing-directory"]},
        {"cwd": "relative/path"},
        {"tool_input": {"path": 17}},
        {"tool_input": {"path": "/missing-directory/file.py"}},
        {"tool_input": {"path": "\u0000"}},
        {"tool_input": {"path": "/fixture/repository"}},
    ],
)
def test_cursor_invalid_repository_paths_skip_mark_with_diagnostic(
    tmp_path: Path, cursor_decision_endpoint: str, overrides: dict
) -> None:
    repo = make_repo(tmp_path / "repository")
    payload = _native_cursor_payload(repo)
    del payload["tool_input"]
    payload.update(
        json.loads(json.dumps(overrides).replace("/fixture/repository", str(repo)))
    )

    result = run_cli(
        ["cursor-hook"],
        cwd=repo,
        stdin=json.dumps(payload),
        extra_env={"SEDIMENT_OTLP_ENDPOINT": cursor_decision_endpoint},
    )

    assert result.returncode == 0, result.stderr
    assert not marker_file(repo).exists()
    assert "repository directory; Attribution mark skipped" in result.stderr
    assert len(_CursorDecisionCapture.requests) == 1
    assert str(repo) not in result.stderr


def test_cursor_successful_write_marks_and_emits_implicit_accept(
    tmp_path: Path, cursor_decision_endpoint: str
) -> None:
    repo = make_repo(tmp_path / "r")
    payload = _cursor_payload(repo)

    result = run_cli(
        ["cursor-hook"],
        cwd=repo,
        stdin=json.dumps(payload),
        extra_env={
            "SEDIMENT_OTLP_ENDPOINT": cursor_decision_endpoint,
            "SEDIMENT_INGEST_TOKEN": "cursor-token",
            "OTEL_RESOURCE_ATTRIBUTES": "team=design,user.id=cursor-user",
        },
    )

    assert result.returncode == 0, result.stderr
    [marker] = [json.loads(line) for line in marker_file(repo).read_text().splitlines()]
    assert (marker["tool"], marker["session_id"]) == (
        "cursor",
        "cursor-session-1",
    )
    [(path, authorization, body)] = _CursorDecisionCapture.requests
    assert path == "/v1/logs"
    assert authorization == "Bearer cursor-token"
    resource = body["resourceLogs"][0]["resource"]
    assert _otlp_string_attributes(resource) == {"user.id": "cursor-user"}
    [record] = body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    assert record["body"] == {"stringValue": "sediment.tool_decision"}
    assert int(record["timeUnixNano"]) > 0
    assert _otlp_string_attributes(record) == {
        "agent": "cursor",
        "session.id": "cursor-session-1",
        "tool_use_id": "cursor-call-1",
        "tool_name": "Write",
        "decision": "accept",
        "explicit": False,
    }
    serialized = json.dumps(body)
    for private_value in (
        "secret",
        "private output",
        "private-model",
        "/private/transcript.jsonl",
        "private@example.com",
        "private.py",
    ):
        assert private_value not in serialized


@pytest.mark.parametrize("event", ["postToolUseFailure", "afterTabFileEdit"])
def test_cursor_failure_and_tab_mark_without_decision(
    tmp_path: Path, cursor_decision_endpoint: str, event: str
) -> None:
    repo = make_repo(tmp_path / event)
    overrides: dict[str, object] = {
        "hook_event_name": event,
        "conversation_id": f"cursor-{event}",
    }
    if event == "afterTabFileEdit":
        overrides.update(
            cwd=None,
            file_path=str(repo / "tab.py"),
            tool_name=None,
            tool_use_id=None,
        )
    payload = _cursor_payload(repo, **overrides)

    result = run_cli(
        ["cursor-hook"],
        cwd=tmp_path,
        stdin=json.dumps(payload),
        extra_env={"SEDIMENT_OTLP_ENDPOINT": cursor_decision_endpoint},
    )

    assert result.returncode == 0, result.stderr
    [marker] = [json.loads(line) for line in marker_file(repo).read_text().splitlines()]
    assert marker == {
        "tool": "cursor",
        "session_id": f"cursor-{event}",
        "stamped_at": marker["stamped_at"],
        "generation": marker["generation"],
    }
    assert _CursorDecisionCapture.requests == []


def test_cursor_missing_call_id_marks_without_inventing_decision(
    tmp_path: Path, cursor_decision_endpoint: str
) -> None:
    repo = make_repo(tmp_path / "r")
    payload = _cursor_payload(repo, tool_use_id="")

    result = run_cli(
        ["cursor-hook"],
        cwd=repo,
        stdin=json.dumps(payload),
        extra_env={"SEDIMENT_OTLP_ENDPOINT": cursor_decision_endpoint},
    )

    assert result.returncode == 0
    [marker] = [json.loads(line) for line in marker_file(repo).read_text().splitlines()]
    assert marker["session_id"] == "cursor-session-1"
    assert _CursorDecisionCapture.requests == []


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        "[]",
        json.dumps({"hook_event_name": [], "conversation_id": "cursor-session"}),
        json.dumps({"hook_event_name": {}, "conversation_id": "cursor-session"}),
        json.dumps({"hook_event_name": "postToolUse", "conversation_id": ""}),
        json.dumps(
            {"hook_event_name": "unsupported", "conversation_id": "cursor-session"}
        ),
    ],
)
def test_cursor_malformed_or_unsupported_input_exits_zero(
    tmp_path: Path, cursor_decision_endpoint: str, payload: str
) -> None:
    repo = make_repo(tmp_path / "r")

    result = run_cli(
        ["cursor-hook"],
        cwd=repo,
        stdin=payload,
        extra_env={"SEDIMENT_OTLP_ENDPOINT": cursor_decision_endpoint},
    )

    assert result.returncode == 0
    assert not marker_file(repo).exists()
    assert _CursorDecisionCapture.requests == []


def test_cursor_invalid_endpoint_and_delivery_failure_exit_zero(tmp_path: Path) -> None:
    for index, endpoint in enumerate(
        ("http://ingest.example.com", "http://127.0.0.1:9")
    ):
        repo = make_repo(tmp_path / f"r-{index}")

        result = run_cli(
            ["cursor-hook"],
            cwd=repo,
            stdin=json.dumps(_cursor_payload(repo)),
            extra_env={
                "SEDIMENT_OTLP_ENDPOINT": endpoint,
                "SEDIMENT_INGEST_TOKEN": "must-not-leak",
            },
        )

        assert result.returncode == 0
        assert "must-not-leak" not in result.stderr
        assert marker_file(repo).exists()


def test_cursor_non_2xx_delivery_exits_zero(
    tmp_path: Path, cursor_decision_endpoint: str
) -> None:
    repo = make_repo(tmp_path / "r")
    _CursorDecisionCapture.response_status = 503

    result = run_cli(
        ["cursor-hook"],
        cwd=repo,
        stdin=json.dumps(_cursor_payload(repo)),
        extra_env={"SEDIMENT_OTLP_ENDPOINT": cursor_decision_endpoint},
    )

    assert result.returncode == 0
    assert marker_file(repo).exists()
    assert "decision delivery failed" in result.stderr


def test_mark_unhooked_repo_logs_once_per_session(tmp_path: Path) -> None:
    # A repo with no sediment post-commit hook (e.g. a clone the agent made
    # itself): the marker can never be consumed, so mark must leave a
    # greppable trace — once per new marker, not per tool call.
    repo = make_repo(tmp_path / "r")
    mark(repo)
    mark(repo)  # same session — deduped marker, no second log line
    mark(repo, session="sess-2")
    entries = log_entries(repo)
    assert len(entries) == 4  # mark + unhooked-repo per new marker
    assert [e["event"] for e in entries] == [
        "mark",
        "unhooked-repo",
        "mark",
        "unhooked-repo",
    ]
    assert {e["session_id"] for e in entries} == {"sess-1", "sess-2"}
    assert entries[0]["git_dir"] == str(repo / ".git")


def test_mark_hooked_repo_logs_mark_but_no_miss(tmp_path: Path) -> None:
    # Mark leaves a breadcrumb even in the healthy case, so a missing
    # note-created for the same session is diagnosable without a cross-pod
    # differential — but a hooked repo logs no unhooked-repo/config-error
    # miss, same as before.
    repo = make_repo(tmp_path / "r")
    assert run_cli(["install", str(repo), "--no-agents"], cwd=repo).returncode == 0
    mark(repo)
    assert marker_file(repo).exists()
    assert log_events(repo) == ["mark"]


def test_attribution_log_rotates_at_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    log = tmp_path / "attribution.log"
    monkeypatch.setenv("SEDIMENT_ATTRIBUTION_LOG", str(log))
    monkeypatch.setattr(mod, "ATTRIBUTION_LOG_CAP_BYTES", 64)
    log.write_text("x" * 100 + "\n")
    mod._log_event(
        "unhooked-repo",
        git_dir=str(tmp_path / ".git"),
        tool="claude-code",
        session_id="sess-1",
    )
    assert (tmp_path / "attribution.log.1").read_text().startswith("x")
    [line] = log.read_text().strip().splitlines()
    assert json.loads(line)["session_id"] == "sess-1"


def write_config(tmp_path: Path, remotes: list[str] | str) -> dict[str, str]:
    """Write a config file (raw string = deliberately malformed) and return
    the env override pointing mark at it."""
    config = tmp_path / "config.json"
    payload = (
        remotes
        if isinstance(remotes, str)
        else json.dumps({"auto_install_remotes": remotes})
    )
    config.write_text(payload)
    return {"SEDIMENT_ATTRIBUTION_CONFIG": str(config)}


def test_mark_auto_installs_in_allowlisted_repo(tmp_path: Path) -> None:
    # An agent edits a fresh clone whose origin the owner allowlisted — mark
    # installs the hooks, so the very first commit after the edit is stamped.
    repo = make_repo(tmp_path / "r")
    make_remote(tmp_path, repo)
    env = write_config(tmp_path, [str(tmp_path) + "/"])
    mark(repo, extra_env=env)
    hooks = hooks_dir(repo)
    for name in ("post-commit", "prepare-commit-msg", "pre-push"):
        assert ">>> sediment-attribution >>>" in (hooks / name).read_text()
    assert git(repo, "config", "--get-all", "notes.rewriteRef") == NOTES_REF
    assert log_events(repo) == ["mark", "auto-installed"]
    # a second session in the now-hooked repo converges silently — one more
    # mark breadcrumb, but no second auto-installed (already hooked by now)
    mark(repo, session="sess-2", extra_env=env)
    assert log_events(repo) == ["mark", "auto-installed", "mark"]
    # end to end: the next commit is stamped by the just-installed hook
    (repo / "b.py").write_text("y = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "agent work")
    note = note_on(repo)
    assert note is not None
    assert {s["session_id"] for s in note["sessions"]} == {"sess-1", "sess-2"}


def test_mark_skips_install_when_remote_not_allowlisted(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    make_remote(tmp_path, repo)
    env = write_config(tmp_path, ["github.com/acme-corp/"])
    mark(repo, extra_env=env)
    hooks = hooks_dir(repo)
    assert not (hooks / "post-commit").exists()
    assert log_events(repo) == ["mark", "unhooked-repo"]


def test_mark_without_origin_remote_skips_install(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")  # no origin at all
    env = write_config(tmp_path, [str(tmp_path) + "/"])
    mark(repo, extra_env=env)
    hooks = hooks_dir(repo)
    assert not (hooks / "post-commit").exists()
    assert log_events(repo) == ["mark", "unhooked-repo"]


def test_mark_heals_partial_hook_set_in_allowlisted_repo(tmp_path: Path) -> None:
    # A checkout missing prepare-commit-msg converges on the next agent edit.
    repo = make_repo(tmp_path / "r")
    make_remote(tmp_path, repo)
    assert run_cli(["install", str(repo), "--no-agents"], cwd=repo).returncode == 0
    hooks = hooks_dir(repo)
    (hooks / "prepare-commit-msg").unlink()
    env = write_config(tmp_path, [str(tmp_path) + "/"])
    mark(repo, extra_env=env)
    assert ">>> sediment-attribution >>>" in (hooks / "prepare-commit-msg").read_text()
    # hooked before → silent refresh: a mark breadcrumb, no auto-installed
    assert log_events(repo) == ["mark"]


def test_mark_config_error_logs_and_does_not_install(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    make_remote(tmp_path, repo)
    mark(repo, extra_env=write_config(tmp_path, "not json {"))
    hooks = hooks_dir(repo)
    assert not (hooks / "post-commit").exists()
    assert log_events(repo) == ["mark", "config-error", "unhooked-repo"]
    # wrong shape is a config error too, not a silent off-switch
    repo2 = make_repo(tmp_path / "r2")
    make_remote(tmp_path / "second", repo2)
    mark(repo2, extra_env=write_config(tmp_path, '{"auto_install_remotes": "x"}'))
    assert log_events(repo2) == ["mark", "config-error", "unhooked-repo"]
    # a blank entry would normalize to "/" and match every local-path
    # remote — rejected as a config error, not silently honored
    repo3 = make_repo(tmp_path / "r3")
    make_remote(tmp_path / "third", repo3)
    mark(repo3, extra_env=write_config(tmp_path, [str(tmp_path) + "/", ""]))
    hooks3 = hooks_dir(repo3)
    assert not (hooks3 / "post-commit").exists()
    assert log_events(repo3) == ["mark", "config-error", "unhooked-repo"]


def test_normalize_remote_covers_url_forms() -> None:
    mod = _load_module()
    n = mod._normalize_remote
    assert n("https://github.com/Acme/Repo.git") == "github.com/acme/repo/"
    assert n("git@github.com:acme/repo.git") == "github.com/acme/repo/"
    assert n("ssh://git@github.com/acme/repo") == "github.com/acme/repo/"
    assert n("github.com/acme/") == "github.com/acme/"
    # segment-aligned: a repo prefix never matches a sibling repo
    assert not n("https://github.com/acme/repo2").startswith(n("github.com/acme/repo"))
    # an allowlisted prefix embedded in a foreign URL's path must not match
    assert not n("https://evil.com/github.com/acme/x").startswith(n("github.com/acme/"))


def test_stamp_writes_note_and_clears_markers(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    mark(repo)
    mark(repo, tool="codex", session="sess-2")
    assert run_cli(["stamp"], cwd=repo).returncode == 0
    note = note_on(repo)
    assert note is not None
    assert note["v"] == 1
    assert [(s["tool"], s["session_id"]) for s in note["sessions"]] == [
        ("claude-code", "sess-1"),
        ("codex", "sess-2"),
    ]
    assert not marker_file(repo).exists()  # cleared after stamping


def test_stamp_without_markers_writes_nothing(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    assert run_cli(["stamp"], cwd=repo).returncode == 0
    assert note_on(repo) is None


def test_stamp_logs_note_created_with_sha_and_session_ids(tmp_path: Path) -> None:
    # Pair with mark's breadcrumb so a session that marked but never
    # shows up here is diagnosable from one machine's log alone.
    repo = make_repo(tmp_path / "r")
    mark(repo)
    mark(repo, tool="codex", session="sess-2")
    assert run_cli(["stamp"], cwd=repo).returncode == 0
    head = git(repo, "rev-parse", "HEAD")

    entries = log_entries(repo)
    [note_created] = [e for e in entries if e["event"] == "note-created"]
    assert note_created["git_dir"] == str(repo / ".git")
    assert note_created["sha"] == head
    assert note_created["session_ids"] == "sess-1,sess-2"


def test_stamp_without_markers_does_not_log_note_created(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    assert run_cli(["stamp"], cwd=repo).returncode == 0
    assert not attribution_log(repo).exists()  # early return: nothing to log


def test_note_payload_privacy_contract(tmp_path: Path) -> None:
    # The note must contain ONLY v + sessions[{tool, session_id, stamped_at}].
    repo = make_repo(tmp_path / "r")
    mark(repo)
    run_cli(["stamp"], cwd=repo)
    note = note_on(repo)
    assert note is not None
    assert set(note.keys()) == {"v", "sessions"}
    for session in note["sessions"]:
        assert set(session.keys()) == {"tool", "session_id", "stamped_at"}


def test_note_survives_amend_via_rewrite_ref(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    git(repo, "config", "notes.rewriteRef", NOTES_REF)
    mark(repo)
    run_cli(["stamp"], cwd=repo)
    old_head = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-q", "--amend", "-m", "amended")
    assert git(repo, "rev-parse", "HEAD") != old_head
    note = note_on(repo)  # the new HEAD
    assert note is not None and note["sessions"][0]["session_id"] == "sess-1"


def test_note_survives_rebase_via_rewrite_ref(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    git(repo, "config", "notes.rewriteRef", NOTES_REF)
    default = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    # feature branch with a stamped commit
    git(repo, "checkout", "-qb", "feature")
    (repo / "b.py").write_text("y = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "feature work")
    mark(repo, session="sess-rebase")
    run_cli(["stamp"], cwd=repo)
    # advance the default branch so the rebase rewrites the feature commit
    git(repo, "checkout", "-q", default)
    (repo / "c.py").write_text("z = 3\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "mainline moved")
    git(repo, "checkout", "-q", "feature")
    git(repo, "rebase", "-q", default)
    note = note_on(repo)  # the rebased feature commit
    assert note is not None and note["sessions"][0]["session_id"] == "sess-rebase"


def make_remote(tmp_path: Path, repo: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    return remote


def notes_tree_entries(repo: Path) -> set[str]:
    """Annotated-commit SHAs present in ``repo``'s notes ref.

    Reads the notes tree directly (entry names are the annotated SHAs,
    possibly fanned out into subdirectories) so it works even when the
    annotated commits themselves are absent from the repo — a notes push
    carries the note objects, not the commits they annotate.
    """
    out = git(repo, "ls-tree", "-r", "--name-only", NOTES_REF)
    return {line.replace("/", "") for line in out.splitlines()}


def test_push_notes_pushes_ref(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    remote = make_remote(tmp_path, repo)
    mark(repo)
    run_cli(["stamp"], cwd=repo)
    assert run_cli(["push-notes", "origin"], cwd=repo).returncode == 0
    assert git(remote, "rev-parse", "--verify", NOTES_REF)  # on the remote


def test_push_notes_without_ref_is_noop(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    make_remote(tmp_path, repo)
    assert run_cli(["push-notes", "origin"], cwd=repo).returncode == 0


def test_push_notes_never_fails_on_bad_remote(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    mark(repo)
    run_cli(["stamp"], cwd=repo)
    assert run_cli(["push-notes", "nonexistent"], cwd=repo).returncode == 0


def test_push_notes_reconciles_diverged_ref(tmp_path: Path) -> None:
    # Machine B stamped before ever fetching the remote notes ref, so its
    # notes history has a disjoint root and a plain non-merging
    # push is rejected non-fast-forward forever. push-notes must reconcile
    # (fetch + cat_sort_uniq union merge) and land BOTH machines' notes.
    a = make_repo(tmp_path / "a")
    remote = make_remote(tmp_path, a)
    mark(a, session="sess-a")
    run_cli(["stamp"], cwd=a)
    assert run_cli(["push-notes", "origin"], cwd=a).returncode == 0
    a_head = git(a, "rev-parse", "HEAD")

    b = make_repo(tmp_path / "b")
    (b / "b.py").write_text("y = 2\n")
    git(b, "add", "-A")
    git(b, "commit", "-qm", "b work")
    git(b, "remote", "add", "origin", str(remote))
    mark(b, session="sess-b")
    run_cli(["stamp"], cwd=b)
    b_head = git(b, "rev-parse", "HEAD")
    assert run_cli(["push-notes", "origin"], cwd=b).returncode == 0

    assert notes_tree_entries(remote) >= {a_head, b_head}
    # B's local ref absorbed A's note in the merge, and its own survived.
    assert notes_tree_entries(b) >= {a_head, b_head}
    # No push failure was logged — the reconcile made the push succeed. (The
    # log does hold mark's unhooked-repo lines: these test repos have no
    # git hooks.)
    assert "notes-push-failed" not in log_events(b)


def test_repair_notes_repairs_diverged_machine(tmp_path: Path) -> None:
    a = make_repo(tmp_path / "a")
    remote = make_remote(tmp_path, a)
    mark(a, session="sess-a")
    run_cli(["stamp"], cwd=a)
    assert run_cli(["push-notes", "origin"], cwd=a).returncode == 0
    a_head = git(a, "rev-parse", "HEAD")

    diverged = make_repo(tmp_path / "diverged")
    git(diverged, "remote", "add", "origin", str(remote))
    mark(diverged, session="sess-d")
    run_cli(["stamp"], cwd=diverged)
    result = run_cli(["repair-notes"], cwd=diverged)  # default remote: origin
    assert result.returncode == 0
    assert "reconciled and pushed" in result.stdout
    assert notes_tree_entries(remote) >= {a_head, git(diverged, "rev-parse", "HEAD")}


def test_repair_notes_adopts_remote_on_fresh_machine(tmp_path: Path) -> None:
    a = make_repo(tmp_path / "a")
    remote = make_remote(tmp_path, a)
    mark(a, session="sess-a")
    run_cli(["stamp"], cwd=a)
    assert run_cli(["push-notes", "origin"], cwd=a).returncode == 0
    a_head = git(a, "rev-parse", "HEAD")

    fresh = make_repo(tmp_path / "fresh")
    git(fresh, "remote", "add", "origin", str(remote))
    assert run_cli(["repair-notes", "origin"], cwd=fresh).returncode == 0
    assert a_head in notes_tree_entries(fresh)


def test_repair_notes_with_no_refs_anywhere_is_noop(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    make_remote(tmp_path, repo)
    result = run_cli(["repair-notes", "origin"], cwd=repo)
    assert result.returncode == 0
    assert "nothing to do" in result.stdout


def test_push_notes_final_failure_logs_event(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    git(repo, "remote", "add", "origin", str(tmp_path / "missing.git"))
    mark(repo)
    run_cli(["stamp"], cwd=repo)
    assert run_cli(["push-notes", "origin"], cwd=repo).returncode == 0  # best-effort
    [push_event] = [e for e in log_entries(repo) if e["event"] == "notes-push-failed"]
    assert push_event["remote"] == "origin"
    assert push_event["git_dir"] == str(git_dir(repo))


def test_push_failure_detail_falls_back_to_stdout() -> None:
    # stderr wins when git prints one; stdout is the fallback when stderr is
    # empty, and the exit code is the last resort when both are empty.
    mod = _load_module()
    stderr_case = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="fatal: line one\nfatal: line two\n"
    )
    assert mod._push_failure_detail(stderr_case) == "fatal: line two"
    stdout_case = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="remote: denied\n", stderr=""
    )
    assert mod._push_failure_detail(stdout_case) == "remote: denied"
    empty_case = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=""
    )
    assert mod._push_failure_detail(empty_case) == "exit 1"


def test_install_creates_hooks_and_config(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    result = run_cli(["install", str(repo), "--no-agents"], cwd=repo)
    assert result.returncode == 0
    hooks = hooks_dir(repo)
    for name in ("post-commit", "prepare-commit-msg", "pre-push"):
        content = (hooks / name).read_text()
        assert "sediment-attribution" in content
        assert (hooks / name).stat().st_mode & 0o111  # executable
    assert git(repo, "config", "notes.rewriteRef") == NOTES_REF


def test_install_is_idempotent_and_preserves_existing_hooks(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    hooks = hooks_dir(repo)
    hooks.mkdir(exist_ok=True)
    (hooks / "post-commit").write_text("#!/bin/sh\necho existing-hook\n")
    run_cli(["install", str(repo), "--no-agents"], cwd=repo)
    run_cli(["install", str(repo), "--no-agents"], cwd=repo)  # twice
    content = (hooks / "post-commit").read_text()
    assert content.count("echo existing-hook") == 1  # preserved
    assert content.count("# >>> sediment-attribution >>>") == 1  # not duplicated


def test_install_respects_core_hooks_path(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    custom = repo / ".husky"
    custom.mkdir()
    git(repo, "config", "core.hooksPath", ".husky")
    run_cli(["install", str(repo), "--no-agents"], cwd=repo)
    assert "sediment-attribution" in (custom / "post-commit").read_text()


def test_install_in_linked_worktree_targets_common_hooks_dir(tmp_path: Path) -> None:
    # Adversarial-review finding: --absolute-git-dir/hooks in a linked worktree
    # is the per-worktree dir git never executes hooks from. Install must land
    # in the COMMON hooks dir, and the e2e mark→commit→note flow must work.
    main = make_repo(tmp_path / "main")
    linked = tmp_path / "linked"
    git(main, "worktree", "add", "-q", str(linked))
    result = run_cli(["install", str(linked), "--no-agents"], cwd=linked)
    assert result.returncode == 0
    common_hooks = main / ".git" / "hooks"
    assert "sediment-attribution" in (common_hooks / "post-commit").read_text()
    mark(linked, session="sess-wt")
    (linked / "wt.py").write_text("w = 1\n")
    git(linked, "add", "-A")
    git(linked, "commit", "-qm", "worktree commit")
    note = note_on(linked)
    assert note is not None and note["sessions"][0]["session_id"] == "sess-wt"


def test_install_refuses_corrupt_agent_config(tmp_path: Path) -> None:
    # Adversarial-review finding (merge-blocker): a settings.json with a JSON
    # typo must NOT be replaced with a default — refuse and preserve the file.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    corrupt = '{"model": "opus", "permissions": {"allow": ["Bash"]},}'
    (home / ".claude" / "settings.json").write_text(corrupt)
    repo = make_repo(tmp_path / "r")
    result = run_cli(
        ["install", str(repo)],
        cwd=repo,
        extra_env={"HOME": str(home), "CODEX_HOME": str(tmp_path / "cx")},
    )
    assert result.returncode == 0  # git hooks still install
    assert "NOT installed" in result.stderr
    assert (home / ".claude" / "settings.json").read_text() == corrupt  # untouched


def test_install_skips_non_dict_hooks_key(tmp_path: Path) -> None:
    # {"hooks": []} previously crashed with AttributeError mid-install.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    original = json.dumps({"hooks": []})
    (home / ".claude" / "settings.json").write_text(original)
    repo = make_repo(tmp_path / "r")
    result = run_cli(
        ["install", str(repo)],
        cwd=repo,
        extra_env={"HOME": str(home), "CODEX_HOME": str(tmp_path / "cx")},
    )
    assert result.returncode == 0
    assert "Traceback" not in result.stderr
    assert (home / ".claude" / "settings.json").read_text() == original


def test_install_preserves_existing_notes_rewrite_ref(tmp_path: Path) -> None:
    # notes.rewriteRef is multi-valued; a pre-existing value must survive
    # install AND uninstall (we --add ours, --fixed-value --unset only ours).
    repo = make_repo(tmp_path / "r")
    git(repo, "config", "notes.rewriteRef", "refs/notes/other")
    run_cli(["install", str(repo), "--no-agents"], cwd=repo)
    values = git(repo, "config", "--get-all", "notes.rewriteRef").splitlines()
    assert set(values) == {"refs/notes/other", NOTES_REF}
    run_cli(["uninstall", str(repo)], cwd=repo)
    values = git(repo, "config", "--get-all", "notes.rewriteRef").splitlines()
    assert values == ["refs/notes/other"]


def test_install_refuses_non_sh_hook(tmp_path: Path) -> None:
    # Appending sh to a python hook would break every push — must refuse.
    repo = make_repo(tmp_path / "r")
    hooks = hooks_dir(repo)
    hooks.mkdir(exist_ok=True)
    python_hook = "#!/usr/bin/env python3\nprint('hi')\n"
    (hooks / "pre-push").write_text(python_hook)
    result = run_cli(["install", str(repo), "--no-agents"], cwd=repo)
    assert result.returncode == 0
    assert (hooks / "pre-push").read_text() == python_hook  # untouched
    assert "not an sh script" in result.stderr
    # the other hook still installs
    assert "sediment-attribution" in (hooks / "post-commit").read_text()


def test_install_refuses_fish_hook(tmp_path: Path) -> None:
    # "fish" contains the substring "sh" — a naive check would admit it and
    # our appended sh block would break the fish hook. Must refuse.
    repo = make_repo(tmp_path / "r")
    hooks = hooks_dir(repo)
    hooks.mkdir(exist_ok=True)
    fish_hook = "#!/usr/bin/env fish\necho hi\n"
    (hooks / "post-commit").write_text(fish_hook)
    result = run_cli(["install", str(repo), "--no-agents"], cwd=repo)
    assert result.returncode == 0
    assert (hooks / "post-commit").read_text() == fish_hook  # untouched
    assert "not an sh script" in result.stderr
    # a bash-via-env hook IS accepted
    (hooks / "post-commit").write_text("#!/usr/bin/env bash\necho hi\n")
    run_cli(["install", str(repo), "--no-agents"], cwd=repo)
    assert "sediment-attribution" in (hooks / "post-commit").read_text()


def test_stamp_dedupes_duplicate_markers(tmp_path: Path) -> None:
    # Concurrent marks can append the same session twice (read-then-append is
    # not atomic); stamp must dedupe and use one stamped_at for the note.
    repo = make_repo(tmp_path / "r")
    entry = json.dumps(
        {
            "tool": "claude-code",
            "session_id": "sess-dup",
            "stamped_at": "2026-07-09T00:00:00+00:00",
        }
    )
    marker_file(repo).write_text(entry + "\n" + entry + "\n")
    run_cli(["stamp"], cwd=repo)
    note = note_on(repo)
    assert note is not None
    assert len(note["sessions"]) == 1
    assert note["sessions"][0]["session_id"] == "sess-dup"


def test_squash_merge_unions_notes_via_installed_hooks(tmp_path: Path) -> None:
    # A local `git merge --squash` creates a brand-new commit that
    # note-rewrite copying (rebase/amend only) never reaches, and by then
    # each squashed branch commit already cleared its own markers when it
    # was individually stamped -- so the union has to come from reading
    # those already-written notes back, not from any leftover markers.
    repo = make_repo(tmp_path / "r")
    run_cli(["install", str(repo), "--no-agents"], cwd=repo)
    default = git(repo, "rev-parse", "--abbrev-ref", "HEAD")

    git(repo, "checkout", "-qb", "feature")
    mark(repo, session="sess-a")
    (repo / "a.py").write_text("x = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "feature commit 1")  # installed post-commit stamps

    mark(repo, tool="codex", session="sess-b")
    (repo / "b.py").write_text("y = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "feature commit 2")  # installed post-commit stamps

    git(repo, "checkout", "-q", default)
    git(repo, "merge", "--squash", "-q", "feature")
    # prepare-commit-msg (union-squash-notes) + post-commit (stamp) both fire
    git(repo, "commit", "-qm", "squash feature")

    note = note_on(repo)
    assert note is not None
    assert {(s["tool"], s["session_id"]) for s in note["sessions"]} == {
        ("claude-code", "sess-a"),
        ("codex", "sess-b"),
    }
    assert not marker_file(repo).exists()  # stamp still clears after unioning


def test_squash_merge_of_amended_commit_keeps_attribution(tmp_path: Path) -> None:
    # Regression: an agent amends a commit it just stamped AND records a new
    # marker between the commit and the amend. notes.rewriteRef (rewriteMode
    # defaults to concatenate) appends the copied note to the post-commit
    # stamp's note, so the amended commit's note becomes two concatenated
    # JSON objects. _read_note_sessions must parse that tolerantly -- as the
    # server-side _parse_note_body already does -- or a later `git merge
    # --squash` of this branch drops the amended commit's sessions from the
    # squash union.
    repo = make_repo(tmp_path / "r")
    run_cli(["install", str(repo), "--no-agents"], cwd=repo)
    default = git(repo, "rev-parse", "--abbrev-ref", "HEAD")

    git(repo, "checkout", "-qb", "feature")
    mark(repo, session="sess-a")
    (repo / "a.py").write_text("x = 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "feature commit")  # post-commit stamps sess-a
    # NEW marker between commit and amend -- the failing shape.
    mark(repo, tool="codex", session="sess-b")
    (repo / "a.py").write_text("x = 3\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--amend", "-m", "feature commit amended")

    # The amended commit's note is two concatenated payloads; a strict reader
    # returns []. A tolerant reader unions both sessions.
    amended_sha = git(repo, "rev-parse", "HEAD")
    mod = _load_module()
    sessions = {
        (s["tool"], s["session_id"])
        for s in mod._read_note_sessions(amended_sha, str(repo))
    }
    assert sessions == {("claude-code", "sess-a"), ("codex", "sess-b")}

    # End-to-end: squash-merge must land both sessions on the squash commit.
    git(repo, "checkout", "-q", default)
    git(repo, "merge", "--squash", "-q", "feature")
    git(repo, "commit", "-qm", "squash feature")
    note = note_on(repo)
    assert note is not None
    assert {(s["tool"], s["session_id"]) for s in note["sessions"]} == {
        ("claude-code", "sess-a"),
        ("codex", "sess-b"),
    }
    assert not marker_file(repo).exists()  # stamp still clears after unioning


def _squash_msg_path(repo: Path) -> Path:
    return git_dir(repo) / "SQUASH_MSG"


def test_union_squash_notes_ignores_source_and_msg_file_args(tmp_path: Path) -> None:
    # `source`/`msg_file` (git's own hook args) are deliberately not the
    # signal: an explicit `git commit -m "..."` after a squash classifies as
    # source="message", not "squash", and msg_file then holds the
    # developer's own text -- neither is a reliable "was this a squash"
    # check. The real signal is <git-dir>/SQUASH_MSG, which is absent here
    # (no squash merge ever happened in this repo), so this must no-op
    # regardless of what source/msg_file claim.
    repo = make_repo(tmp_path / "r")
    mark(repo, session="sess-existing")
    msg_file = repo / "COMMIT_EDITMSG"
    msg_file.write_text("an ordinary commit message\n")
    result = run_cli(["union-squash-notes", str(msg_file), "squash"], cwd=repo)
    assert result.returncode == 0
    # Existing markers (from a real mark(), not a squash) are untouched.
    lines = marker_file(repo).read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["session_id"] == "sess-existing"


def test_union_squash_notes_merges_with_existing_local_markers(tmp_path: Path) -> None:
    # A marker already on the target branch (an agent edit made directly on
    # main before the squash-merge lands) must survive the union, not be
    # clobbered by it.
    repo = make_repo(tmp_path / "r")
    default = git(repo, "rev-parse", "--abbrev-ref", "HEAD")

    git(repo, "checkout", "-qb", "feature")
    mark(repo, session="sess-feature")
    (repo / "f.py").write_text("f = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "feature commit")
    run_cli(["stamp"], cwd=repo)
    feature_sha = git(repo, "rev-parse", "HEAD")

    git(repo, "checkout", "-q", default)
    mark(repo, tool="codex", session="sess-local")  # local, pre-squash marker
    _squash_msg_path(repo).write_text(
        f"Squashed commit of the following:\n\ncommit {feature_sha}\n"
    )
    result = run_cli(["union-squash-notes", "ignored", "squash"], cwd=repo)
    assert result.returncode == 0

    entries = [json.loads(line) for line in marker_file(repo).read_text().splitlines()]
    assert {(e["tool"], e["session_id"]) for e in entries} == {
        ("codex", "sess-local"),
        ("claude-code", "sess-feature"),
    }


def test_union_squash_notes_no_squashed_commits_is_noop(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    _squash_msg_path(repo).write_text("Squashed commit of the following:\n\n")
    result = run_cli(["union-squash-notes", "ignored", "squash"], cwd=repo)
    assert result.returncode == 0
    assert not marker_file(repo).exists()


def test_union_squash_notes_no_squash_msg_file_exits_zero(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    result = run_cli(["union-squash-notes", "ignored", "squash"], cwd=repo)
    assert result.returncode == 0


def test_uninstall_removes_prepare_commit_msg_hook(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    run_cli(["install", str(repo), "--no-agents"], cwd=repo)
    hook = repo / ".git" / "hooks" / "prepare-commit-msg"
    assert hook.exists()
    run_cli(["uninstall", str(repo)], cwd=repo)
    assert "sediment-attribution" not in hook.read_text()


def test_uninstall_keeps_foreign_hook_sharing_our_block(tmp_path: Path) -> None:
    # A user may add their own command to our block; uninstall must remove
    # only our entry, not the whole block.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}")
    repo = make_repo(tmp_path / "r")
    env = {"HOME": str(home), "CODEX_HOME": str(tmp_path / "cx")}
    run_cli(["install", str(repo)], cwd=repo, extra_env=env)
    settings_path = home / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["hooks"]["PostToolUse"][0]["hooks"].append(
        {"type": "command", "command": "my-formatter.sh"}
    )
    settings_path.write_text(json.dumps(settings))
    run_cli(["uninstall", str(repo), "--agents"], cwd=repo, extra_env=env)
    settings = json.loads(settings_path.read_text())
    [block] = settings["hooks"]["PostToolUse"]
    assert [h["command"] for h in block["hooks"]] == ["my-formatter.sh"]


def test_push_notes_failure_message_includes_prefix(tmp_path: Path) -> None:
    # Precedence bug regression: a failed push with EMPTY stderr must still
    # print the full diagnostic, not a blank line.
    repo = make_repo(tmp_path / "r")
    mark(repo)
    run_cli(["stamp"], cwd=repo)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    (fakebin / "git").write_text(
        f'#!/bin/sh\nif [ "$1" = "push" ]; then exit 1; fi\nexec "{real_git}" "$@"\n'
    )
    (fakebin / "git").chmod(0o755)
    result = run_cli(
        ["push-notes", "origin"],
        cwd=repo,
        extra_env={"PATH": f"{fakebin}:{os.environ['PATH']}"},
    )
    assert result.returncode == 0
    assert "notes push to origin failed" in result.stderr


def test_claude_matcher_covers_multiedit(tmp_path: Path) -> None:
    # 1.x clients dispatch matchers by exact token; MultiEdit must be listed.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    repo = make_repo(tmp_path / "r")
    run_cli(
        ["install", str(repo)],
        cwd=repo,
        extra_env={"HOME": str(home), "CODEX_HOME": str(tmp_path / "cx")},
    )
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    matcher = settings["hooks"]["PostToolUse"][0]["matcher"]
    assert "MultiEdit" in matcher.split("|")


def test_uninstall_removes_only_our_block(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    hooks = hooks_dir(repo)
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-push").write_text("#!/bin/sh\necho keep-me\n")
    run_cli(["install", str(repo), "--no-agents"], cwd=repo)
    run_cli(["uninstall", str(repo)], cwd=repo)
    content = (hooks / "pre-push").read_text()
    assert "keep-me" in content
    assert "sediment-attribution" not in content
    out = subprocess.run(
        ["git", "config", "notes.rewriteRef"], cwd=repo, capture_output=True
    )
    assert out.returncode != 0  # unset


def test_agent_hook_install_and_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point HOME/CODEX_HOME at temp dirs so the user's real configs are never
    # touched; pre-seed a Claude settings.json to prove we merge, not clobber.
    home = tmp_path / "home"
    codex_home = tmp_path / "codex"
    codex_home.mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"model": "opus", "hooks": {"SessionStart": [{"hooks": []}]}})
    )
    repo = make_repo(tmp_path / "r")
    env_patch = {"HOME": str(home), "CODEX_HOME": str(codex_home)}
    result = run_cli(["install", str(repo)], cwd=repo, extra_env=env_patch)
    assert result.returncode == 0
    assert "run /hooks and trust the Sediment hook" in result.stdout
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["model"] == "opus"  # untouched
    assert len(settings["hooks"]["SessionStart"]) == 1  # untouched
    assert any(
        "mark --tool claude-code" in h["command"]
        for b in settings["hooks"]["PostToolUse"]
        for h in b["hooks"]
    )
    codex_cfg = json.loads((codex_home / "hooks.json").read_text())
    assert any(
        "mark --tool codex" in h["command"]
        for b in codex_cfg["hooks"]["PostToolUse"]
        for h in b["hooks"]
    )
    # idempotent: run again, no duplicates
    run_cli(["install", str(repo)], cwd=repo, extra_env=env_patch)
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["PostToolUse"]) == 1
    # uninstall --agents removes our entries but nothing else
    run_cli(["uninstall", str(repo), "--agents"], cwd=repo, extra_env=env_patch)
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["PostToolUse"] == []
    assert settings["model"] == "opus"


def test_cursor_hook_install_is_additive_idempotent_and_uninstall_is_scoped(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    cursor_dir = home / ".cursor"
    cursor_dir.mkdir(parents=True)
    config_path = cursor_dir / "hooks.json"
    foreign = {"command": "./hooks/foreign.sh", "timeout": 30}
    config_path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "sessionStart": [foreign],
                    "postToolUse": [{"command": "./hooks/another.sh"}],
                },
            }
        )
    )
    repo = make_repo(tmp_path / "r")
    env = {"HOME": str(home), "CODEX_HOME": str(tmp_path / "codex")}

    first = run_cli(["install", str(repo)], cwd=repo, extra_env=env)
    second = run_cli(["install", str(repo)], cwd=repo, extra_env=env)

    assert first.returncode == second.returncode == 0
    config = json.loads(config_path.read_text())
    assert config["version"] == 1
    assert config["theme"] == "dark"
    assert config["hooks"]["sessionStart"] == [foreign]
    expected = {
        "postToolUse": "Write",
        "postToolUseFailure": "Write",
        "afterTabFileEdit": None,
    }
    for event, matcher in expected.items():
        entries = config["hooks"][event]
        sediment = [entry for entry in entries if "cursor-hook" in entry["command"]]
        assert len(sediment) == 1
        if matcher is None:
            assert "matcher" not in sediment[0]
        else:
            assert sediment[0]["matcher"] == matcher
    assert config["hooks"]["postToolUse"][0] == {"command": "./hooks/another.sh"}

    removed = run_cli(["uninstall", str(repo), "--agents"], cwd=repo, extra_env=env)

    assert removed.returncode == 0
    config = json.loads(config_path.read_text())
    assert config["hooks"]["sessionStart"] == [foreign]
    assert config["hooks"]["postToolUse"] == [{"command": "./hooks/another.sh"}]
    assert config["hooks"]["postToolUseFailure"] == []
    assert config["hooks"]["afterTabFileEdit"] == []


@pytest.mark.parametrize(
    "original",
    [
        "{not json",
        json.dumps({"version": None, "hooks": {}}),
        json.dumps({"version": 2, "hooks": {}}),
        json.dumps({"version": 1, "hooks": []}),
        json.dumps({"version": 1, "hooks": {"postToolUse": {}}}),
        json.dumps({"version": 1, "hooks": {"postToolUse": [None]}}),
        json.dumps({"version": 1, "hooks": {"postToolUse": [{"command": 7}]}}),
        json.dumps(
            {
                "version": 1,
                "hooks": {"postToolUse": [{"command": "foreign", "matcher": 7}]},
            }
        ),
    ],
)
def test_cursor_install_refuses_incompatible_config_untouched(
    tmp_path: Path, original: str
) -> None:
    home = tmp_path / "home"
    cursor_dir = home / ".cursor"
    cursor_dir.mkdir(parents=True)
    config_path = cursor_dir / "hooks.json"
    config_path.write_text(original)
    repo = make_repo(tmp_path / "r")

    result = run_cli(
        ["install", str(repo)],
        cwd=repo,
        extra_env={"HOME": str(home), "CODEX_HOME": str(tmp_path / "codex")},
    )

    assert result.returncode == 0
    assert "cursor" in result.stderr
    assert config_path.read_text() == original


def test_install_skips_agents_when_not_detected(tmp_path: Path) -> None:
    # Agents whose config directories don't exist must be skipped, never
    # fabricate config from nothing.
    home = tmp_path / "home"
    home.mkdir()
    # No ~/.claude, no ~/.codex, no ~/.pi/agent — every agent is absent.
    repo = make_repo(tmp_path / "r")
    env_patch = {"HOME": str(home), "CODEX_HOME": str(tmp_path / "codex")}
    result = run_cli(["install", str(repo)], cwd=repo, extra_env=env_patch)
    assert result.returncode == 0  # git hooks still install
    assert "skipped" in result.stderr
    assert "claude-code" in result.stderr
    assert "codex" in result.stderr
    assert "pi extension" in result.stderr
    # No agent config files were fabricated.
    assert not (home / ".claude").exists()
    assert not (home / ".claude" / "settings.json").exists()
    assert not (tmp_path / "codex").exists()
    assert not (tmp_path / "codex" / "hooks.json").exists()
    assert not (home / ".pi").exists()
    assert not (home / ".pi" / "agent" / "settings.json").exists()


def test_install_picks_up_agent_after_reinstall(tmp_path: Path) -> None:
    # Re-running install after installing the agent picks it up.
    home = tmp_path / "home"
    home.mkdir()
    codex_home = tmp_path / "codex"
    repo = make_repo(tmp_path / "r")
    env_patch = {"HOME": str(home), "CODEX_HOME": str(codex_home)}

    # First install: no agents present.
    result = run_cli(["install", str(repo)], cwd=repo, extra_env=env_patch)
    assert result.returncode == 0
    assert "skipped" in result.stderr

    # Simulate installing pi: create ~/.pi/agent/ with its own settings.
    (home / ".pi" / "agent").mkdir(parents=True)
    (home / ".pi" / "agent" / "settings.json").write_text(
        json.dumps({"extensions": ["/other/ext.ts"]})
    )
    # Second install: pi detected, picks it up.
    result = run_cli(["install", str(repo)], cwd=repo, extra_env=env_patch)
    assert result.returncode == 0
    assert "pi extension: skipped" not in result.stderr
    assert "pi extension added" in result.stdout
    settings = json.loads((home / ".pi" / "agent" / "settings.json").read_text())
    assert len(settings["extensions"]) == 2  # foreign + ours


def test_end_to_end_commit_and_push(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    remote = make_remote(tmp_path, repo)
    run_cli(["install", str(repo), "--no-agents"], cwd=repo)
    mark(repo, session="sess-e2e")
    (repo / "new.py").write_text("n = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "agent work")  # post-commit hook stamps
    note = note_on(repo)
    assert note is not None and note["sessions"][0]["session_id"] == "sess-e2e"
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    git(repo, "push", "-q", "origin", branch)  # pre-push hook carries the note
    assert git(remote, "rev-parse", "--verify", NOTES_REF)


def test_reinstall_hook_block_with_backslash_command_does_not_crash(
    tmp_path: Path,
) -> None:
    # _install_hook_block builds the replacement from an absolute script path;
    # a `\` or `\g`/`\1` in it would be read as a regex-replacement escape and
    # raise re.error on the RE-INSTALL (replace) path. A Windows-style path is
    # the real-world trigger; assert idempotent replace with no crash.
    mod = _load_module()
    hook = tmp_path / "post-commit"
    command = r'"C:\Program Files\py.exe" "C:\opt\gamma_1.py" stamp || true'

    assert mod._install_hook_block(hook, command) is True  # first: append
    assert mod._install_hook_block(hook, command) is True  # second: replace
    content = hook.read_text()
    assert content.count(mod.HOOK_BLOCK_BEGIN) == 1  # single block, not doubled
    assert command in content


def test_is_our_hook_tolerates_non_string_command() -> None:
    mod = _load_module()
    assert mod._is_our_hook({"command": None}) is False
    assert mod._is_our_hook({"command": 123}) is False
    assert mod._is_our_hook({}) is False
    assert mod._is_our_hook({"command": 'x "y/sediment_attribution.py" z'}) is True
    assert mod._is_our_hook({"command": '"p" "q/attribution.py" mark'}) is True
    assert (
        mod._is_our_hook({"command": '"/u/.local/bin/sediment" mark || true'}) is True
    )


def test_transcript_hook_ownership_requires_a_sediment_invocation() -> None:
    mod = _load_module()
    for command in (
        'python3 "/opt/sediment/sediment_transcript.py" --agent claude-code',
        f'"{sys.executable}" "{TRANSCRIPT}" --agent claude-code || true',
        '"/u/.local/bin/sediment" transcript --agent claude-code || true',
    ):
        assert mod._is_our_hook({"command": command}) is True
    assert (
        mod._is_our_hook(
            {"command": 'python3 "/opt/another-tool/transcript.py" --archive'}
        )
        is False
    )


FLEET_FIXTURES = Path(__file__).parent / "fixtures" / "fleet"
FLEET_BUNDLE_FILES = (
    "git-template/hooks/post-commit",
    "git-template/hooks/prepare-commit-msg",
    "git-template/hooks/pre-push",
    "gitconfig",
    "claude-managed-settings.json",
    "codex-hooks.json",
)


def test_fleet_emit_matches_fixtures(tmp_path: Path) -> None:
    # The emitted fragments ARE the doc'd MDM recipe — byte-for-byte. The
    # fixtures pin them so the script stays the single source of truth
    # (CLAUDE_MATCHER drift has drifted from the docs before).
    out = tmp_path / "bundle"
    result = run_cli(
        ["install", "--fleet", "--out", str(out), "--prefix", "/opt/sediment"],
        cwd=tmp_path,
    )
    assert result.returncode == 0
    for rel in FLEET_BUNDLE_FILES:
        expected = (FLEET_FIXTURES / Path(rel).name).read_text()
        assert (out / rel).read_text() == expected, rel
    # the bundle carries the stamper itself, and the template hooks run
    assert (out / "sediment_attribution.py").read_bytes() == SCRIPT.read_bytes()
    for name in ("post-commit", "prepare-commit-msg", "pre-push"):
        assert (out / "git-template" / "hooks" / name).stat().st_mode & 0o111


def test_fleet_emit_default_out_dir(tmp_path: Path) -> None:
    result = run_cli(["install", "--fleet"], cwd=tmp_path)
    assert result.returncode == 0
    assert (tmp_path / "sediment-fleet" / "gitconfig").exists()


def test_fleet_emit_is_idempotent(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    args = ["install", "--fleet", "--out", str(out), "--prefix", "/opt/sediment"]
    assert run_cli(args, cwd=tmp_path).returncode == 0
    first = {rel: (out / rel).read_text() for rel in FLEET_BUNDLE_FILES}
    assert run_cli(args, cwd=tmp_path).returncode == 0
    for rel in FLEET_BUNDLE_FILES:
        assert (out / rel).read_text() == first[rel], rel


def test_fleet_flags_require_and_exclude(tmp_path: Path) -> None:
    # No silently-ignored flag mixes: fleet flags demand --fleet, per-repo
    # args are rejected with --fleet, --out and --apply exclude each other.
    for args in (
        ["install", "--out", "x"],
        ["install", "--apply"],
        ["install", "--prefix", "/opt/sediment"],
        ["install", str(tmp_path), "--fleet"],
        ["install", "--fleet", "--no-agents"],
        ["install", "--fleet", "--out", "x", "--apply"],
    ):
        result = run_cli(args, cwd=tmp_path)
        assert result.returncode == 2, args
        assert "error" in result.stderr, args


def test_fleet_rejects_unsafe_prefix(tmp_path: Path) -> None:
    # A relative prefix silently breaks stamping fleet-wide (hooks are
    # best-effort); backslashes corrupt the gitconfig fragment's escaping.
    for prefix in ("opt/sediment", "C:\\Sediment", '/opt/sedi"ment', "/opt/$x"):
        result = run_cli(["install", "--fleet", "--prefix", prefix], cwd=tmp_path)
        assert result.returncode == 2, prefix
        assert "absolute POSIX path" in result.stderr, prefix
    assert not (tmp_path / "sediment-fleet").exists()  # nothing emitted


def fleet_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[object, Path, Path, Path]:
    """Load the module wired for a safe --apply: system gitconfig redirected
    via GIT_CONFIG_SYSTEM, managed settings into tmp. Returns
    (module, prefix, system gitconfig path, managed-settings path)."""
    mod = _load_module()
    prefix = tmp_path / "opt" / "sediment"
    system_gitconfig = tmp_path / "system-gitconfig"
    managed = tmp_path / "managed" / "managed-settings.json"
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_gitconfig))
    monkeypatch.setattr(mod, "_managed_claude_settings_path", lambda: managed)
    return mod, prefix, system_gitconfig, managed


def git_file_config(config: str | Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "config", "--file", str(config), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_fleet_apply_provisions_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, prefix, system_gitconfig, managed = fleet_apply(tmp_path, monkeypatch)
    rc = mod.main(["install", "--fleet", "--apply", "--prefix", str(prefix)])
    assert rc == 0
    # bundle landed in the prefix itself — where the hooks point
    assert (prefix / "sediment_attribution.py").exists()
    hook = (prefix / "git-template" / "hooks" / "post-commit").read_text()
    assert f'"{prefix}/sediment_attribution.py" stamp' in hook
    # system gitconfig wired
    out = git_file_config(system_gitconfig, "--get", "init.templateDir")
    assert out.stdout.strip() == f"{prefix}/git-template"
    out = git_file_config(system_gitconfig, "--get-all", "notes.rewriteRef")
    assert out.stdout.strip() == NOTES_REF
    # managed settings carry the marker hook with the canonical matcher
    settings = json.loads(managed.read_text())
    [block] = settings["hooks"]["PostToolUse"]
    assert block["matcher"] == mod.CLAUDE_MATCHER
    assert (
        f'"{prefix}/sediment_attribution.py" mark --tool claude-code'
        in (block["hooks"][0]["command"])
    )


def test_fleet_apply_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, prefix, system_gitconfig, managed = fleet_apply(tmp_path, monkeypatch)
    args = ["install", "--fleet", "--apply", "--prefix", str(prefix)]
    assert mod.main(args) == 0
    assert mod.main(args) == 0
    out = git_file_config(system_gitconfig, "--get-all", "notes.rewriteRef")
    assert out.stdout.splitlines() == [NOTES_REF]  # not duplicated
    settings = json.loads(managed.read_text())
    assert len(settings["hooks"]["PostToolUse"]) == 1


def test_fleet_apply_preserves_existing_rewrite_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, prefix, system_gitconfig, _ = fleet_apply(tmp_path, monkeypatch)
    git_file_config(system_gitconfig, "notes.rewriteRef", "refs/notes/other")
    assert mod.main(["install", "--fleet", "--apply", "--prefix", str(prefix)]) == 0
    out = git_file_config(system_gitconfig, "--get-all", "notes.rewriteRef")
    assert set(out.stdout.splitlines()) == {"refs/notes/other", NOTES_REF}


def test_fleet_apply_refuses_foreign_template_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # A pre-existing init.templateDir belongs to someone else — never clobber.
    mod, prefix, system_gitconfig, _ = fleet_apply(tmp_path, monkeypatch)
    git_file_config(system_gitconfig, "init.templateDir", "/somewhere/else")
    rc = mod.main(["install", "--fleet", "--apply", "--prefix", str(prefix)])
    assert rc == 1
    assert "not overwritten" in capsys.readouterr().err
    out = git_file_config(system_gitconfig, "--get", "init.templateDir")
    assert out.stdout.strip() == "/somewhere/else"


def test_fleet_apply_updates_stale_prefix_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Re-apply with a new --prefix must rewrite the managed-settings command;
    # "already present" would leave a hook invoking the deleted old prefix,
    # silently killing notes attribution behind `|| true`.
    mod, _, system_gitconfig, managed = fleet_apply(tmp_path, monkeypatch)
    old_prefix = tmp_path / "opt" / "old"
    new_prefix = tmp_path / "opt" / "new"
    assert mod.main(["install", "--fleet", "--apply", "--prefix", str(old_prefix)]) == 0
    assert mod.main(["install", "--fleet", "--apply", "--prefix", str(new_prefix)]) == 0
    settings = json.loads(managed.read_text())
    [block] = settings["hooks"]["PostToolUse"]  # updated in place, not duplicated
    [hook] = block["hooks"]
    assert str(new_prefix) in hook["command"]
    assert str(old_prefix) not in hook["command"]
    # init.templateDir repointed too: the old value carried our marker block
    out = git_file_config(system_gitconfig, "--get", "init.templateDir")
    assert out.stdout.strip() == f"{new_prefix}/git-template"


def test_fleet_apply_stops_before_config_when_hook_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # A refused (non-sh) hook in the prefix template dir must abort --apply
    # BEFORE init.templateDir is wired: pointing the system gitconfig at a
    # template dir carrying a foreign hook would activate it on every clone.
    mod, prefix, system_gitconfig, _ = fleet_apply(tmp_path, monkeypatch)
    hooks_dir = prefix / "git-template" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "post-commit").write_text("#!/usr/bin/env python3\nprint('x')\n")
    rc = mod.main(["install", "--fleet", "--apply", "--prefix", str(prefix)])
    assert rc == 1
    assert "bundle incomplete" in capsys.readouterr().err
    out = git_file_config(system_gitconfig, "--get", "init.templateDir")
    assert out.stdout.strip() == ""  # never wired


def test_fleet_apply_refuses_corrupt_managed_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    mod, prefix, _, managed = fleet_apply(tmp_path, monkeypatch)
    managed.parent.mkdir(parents=True)
    corrupt = '{"hooks": {,}'
    managed.write_text(corrupt)
    rc = mod.main(["install", "--fleet", "--apply", "--prefix", str(prefix)])
    assert rc == 1
    assert "NOT installed" in capsys.readouterr().err
    assert managed.read_text() == corrupt  # untouched


def test_install_uninstall_survive_non_string_command_in_config(tmp_path: Path) -> None:
    # A malformed-but-parseable agent config (a hook whose command is null)
    # must not crash install/uninstall — the installer is best-effort.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps(
            {"hooks": {"PostToolUse": [{"hooks": [{"type": "command"}]}]}}
        )  # a hook dict with no (→ null-like) command
    )
    repo = make_repo(tmp_path / "r")
    env = {"HOME": str(home), "CODEX_HOME": str(tmp_path / "cx")}

    result = run_cli(["install", str(repo)], cwd=repo, extra_env=env)
    assert result.returncode == 0
    assert "Traceback" not in result.stderr
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    # our entry was added alongside the pre-existing foreign hook
    assert any(
        "mark --tool claude-code" in h.get("command", "")
        for b in settings["hooks"]["PostToolUse"]
        for h in b["hooks"]
        if isinstance(h.get("command"), str)
    )

    result = run_cli(["uninstall", str(repo), "--agents"], cwd=repo, extra_env=env)
    assert result.returncode == 0
    assert "Traceback" not in result.stderr


def test_transcripts_flag_installs_session_end_hook(tmp_path: Path) -> None:
    # Opt-in only (ADR 0007): the default install must never add the
    # SessionEnd extractor — it ships edit text pairs, a different privacy
    # class than the stamper's session-id-only hooks.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    codex_home = tmp_path / "cx"
    codex_home.mkdir()
    repo = make_repo(tmp_path / "r")
    env = {"HOME": str(home), "CODEX_HOME": str(codex_home)}
    run_cli(["install", str(repo)], cwd=repo, extra_env=env)
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert "SessionEnd" not in settings["hooks"]

    result = run_cli(["install", str(repo), "--transcripts"], cwd=repo, extra_env=env)
    assert result.returncode == 0
    assert "transcript hook (SessionEnd): added" in result.stdout
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    [block] = settings["hooks"]["SessionEnd"]
    [hook] = block["hooks"]
    assert f'"{sys.executable}" "{TRANSCRIPT}"' in hook["command"]
    assert hook["command"].endswith("--agent claude-code || true")
    codex = json.loads((codex_home / "hooks.json").read_text())
    [codex_block] = codex["hooks"]["SessionEnd"]
    [codex_hook] = codex_block["hooks"]
    assert f'"{sys.executable}" "{TRANSCRIPT}"' in codex_hook["command"]
    assert codex_hook["command"].endswith("--agent codex || true")

    # uninstall --agents removes the transcript entry along with the marks.
    run_cli(["uninstall", str(repo), "--agents"], cwd=repo, extra_env=env)
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["SessionEnd"] == []
    codex = json.loads((codex_home / "hooks.json").read_text())
    assert codex["hooks"]["SessionEnd"] == []
    assert not any(
        "sediment" in h.get("command", "")
        for blocks in settings["hooks"].values()
        for b in blocks
        for h in b.get("hooks", [])
    )


def test_transcript_install_preserves_a_foreign_transcript_hook(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    foreign = 'python3 "/opt/another-tool/transcript.py" --archive'
    settings_path = home / ".claude" / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [{"hooks": [{"type": "command", "command": foreign}]}]
                }
            }
        )
    )
    repo = make_repo(tmp_path / "r")
    env = {"HOME": str(home), "CODEX_HOME": str(tmp_path / "cx")}

    result = run_cli(["install", str(repo), "--transcripts"], cwd=repo, extra_env=env)

    assert result.returncode == 0
    settings = json.loads(settings_path.read_text())
    commands = [
        hook["command"]
        for block in settings["hooks"]["SessionEnd"]
        for hook in block["hooks"]
    ]
    assert foreign in commands
    assert any("sediment" in command for command in commands if command != foreign)


def test_transcript_uninstall_preserves_a_foreign_transcript_hook(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    foreign = 'python3 "/opt/another-tool/transcript.py" --archive'
    settings_path = home / ".claude" / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [{"hooks": [{"type": "command", "command": foreign}]}]
                }
            }
        )
    )
    repo = make_repo(tmp_path / "r")
    env = {"HOME": str(home), "CODEX_HOME": str(tmp_path / "cx")}

    result = run_cli(["uninstall", str(repo), "--agents"], cwd=repo, extra_env=env)

    assert result.returncode == 0
    settings = json.loads(settings_path.read_text())
    assert len(settings["hooks"]["SessionEnd"]) == 1
    [block] = settings["hooks"]["SessionEnd"]
    [hook] = block["hooks"]
    assert hook["command"] == foreign


def test_transcripts_flag_rejected_with_fleet(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "install", "--fleet", "--transcripts"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 2
    assert "--transcripts" in result.stderr


# doctor: one test per failure class, each built as the real broken state
# rather than a mocked one. Every test pins HOME, CODEX_HOME and both git
# config scopes into tmp_path: doctor reads the developer's real agent configs
# and real git config otherwise, which would make these tests pass or fail
# depending on whose machine ran them.


def doctor_env(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        # Empty, nonexistent files: git treats a missing config file as empty,
        # so init.templateDir is unset unless a test sets it.
        "GIT_CONFIG_GLOBAL": str(home / "gitconfig-global"),
        "GIT_CONFIG_SYSTEM": str(home / "gitconfig-system"),
    }


def doctor(
    cwd: Path, home: Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return run_cli(
        ["doctor", *args], cwd=cwd, extra_env={**doctor_env(home), **(extra_env or {})}
    )


def findings(result: subprocess.CompletedProcess) -> dict[str, str]:
    """Findings keyed by check name, so a test asserts on the check it is
    about and stays insensitive to the order or the presence of others."""
    out = {}
    for line in result.stdout.splitlines():
        if ": " not in line or line.split(maxsplit=1)[0] not in ("ok", "FAIL", "info"):
            continue
        status, rest = line.split(maxsplit=1)
        check, _, detail = rest.partition(": ")
        out[check] = f"{status} {detail}"
    return out


def installed_repo(tmp_path: Path, home: Path, name: str = "r") -> Path:
    """A repo in the state a correct install leaves: git hooks, agent hook
    entries under the fixture HOME, notes.rewriteRef.

    Creates the agent config directories so the presence gate lets the
    per-agent installers through."""
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".cursor").mkdir(parents=True, exist_ok=True)
    (home / ".pi" / "agent").mkdir(parents=True, exist_ok=True)
    repo = make_repo(tmp_path / name)
    result = run_cli(["install", str(repo)], cwd=repo, extra_env=doctor_env(home))
    assert result.returncode == 0, result.stderr
    return repo


def test_doctor_passes_on_a_correct_install(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = installed_repo(tmp_path, home)
    result = doctor(repo, home, str(repo))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    found = findings(result)
    assert found[f"hooks[{repo}]"].startswith("ok")
    assert found[f"notes.rewriteRef[{repo}]"].startswith("ok")


def test_doctor_reports_missing_agent_hook_entries(tmp_path: Path) -> None:
    # The agent is on this machine but install never ran: no agent entry
    # means no session is ever marked, and nothing downstream can notice.
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    repo = make_repo(tmp_path / "r")
    result = doctor(repo, home)
    assert result.returncode == 1
    codex = findings(result)["codex hook"]
    assert codex.startswith("FAIL")
    assert "mark --tool codex" in codex and "run install" in codex


def test_doctor_reports_cursor_absent_healthy_malformed_and_stale(
    tmp_path: Path,
) -> None:
    absent_home = tmp_path / "absent-home"
    absent_home.mkdir()
    repo = make_repo(tmp_path / "r")
    absent = doctor(repo, absent_home)
    assert findings(absent)["cursor hooks"].startswith("info")

    home = tmp_path / "home"
    installed_repo(tmp_path, home, name="installed")
    healthy = doctor(repo, home)
    assert findings(healthy)["cursor hooks"].startswith("ok")

    path = home / ".cursor" / "hooks.json"
    path.write_text("{not json")
    malformed = doctor(repo, home)
    assert malformed.returncode == 1
    assert "not valid JSON" in findings(malformed)["cursor hooks"]

    path.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "postToolUse": [
                        {
                            "command": '"/deleted/sediment" cursor-hook || true',
                            "matcher": "Write",
                        }
                    ],
                    "postToolUseFailure": [
                        {
                            "command": '"/deleted/sediment" cursor-hook || true',
                            "matcher": "Write",
                        }
                    ],
                    "afterTabFileEdit": [
                        {"command": '"/deleted/sediment" cursor-hook || true'}
                    ],
                },
            }
        )
    )
    stale = doctor(repo, home)
    assert stale.returncode == 1
    assert "does not exist" in findings(stale)["cursor hooks"]


def test_doctor_rejects_cursor_hook_with_obsolete_arguments(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = installed_repo(tmp_path, home)
    path = home / ".cursor" / "hooks.json"
    config = json.loads(path.read_text())
    config["hooks"]["postToolUse"][0]["command"] += " --obsolete"
    path.write_text(json.dumps(config))

    result = doctor(repo, home)

    assert result.returncode == 1
    assert "stale command" in findings(result)["cursor hooks"]


@pytest.mark.parametrize(
    "invalid_entry",
    [None, {}, {"command": 7}, {"command": "foreign", "matcher": 7}],
)
def test_doctor_rejects_invalid_cursor_hook_entries(
    tmp_path: Path, invalid_entry: object
) -> None:
    home = tmp_path / "home"
    repo = installed_repo(tmp_path, home)
    path = home / ".cursor" / "hooks.json"
    config = json.loads(path.read_text())
    config["hooks"]["postToolUse"].append(invalid_entry)
    path.write_text(json.dumps(config))

    result = doctor(repo, home)

    assert result.returncode == 1
    assert "invalid postToolUse entry" in findings(result)["cursor hooks"]


def test_cursor_install_write_failure_is_fail_soft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_module()
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        mod,
        "_write_json_atomic",
        lambda _path, _data: (_ for _ in ()).throw(PermissionError("secret")),
    )

    status = mod._install_cursor_hooks()

    assert status == "skipped"
    warning = capsys.readouterr().err
    assert "cursor hooks NOT installed" in warning
    assert "PermissionError" in warning
    assert "secret" not in warning


def test_cursor_uninstall_write_failure_is_fail_soft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_module()
    home = tmp_path / "home"
    cursor_dir = home / ".cursor"
    cursor_dir.mkdir(parents=True)
    path = cursor_dir / "hooks.json"
    original = {
        "version": 1,
        "hooks": {
            event: [
                mod._cursor_hook_entry(mod._script_invocation("cursor-hook"), matcher)
            ]
            for event, matcher in mod._CURSOR_HOOKS.items()
        },
    }
    path.write_text(json.dumps(original))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        mod,
        "_write_json_atomic",
        lambda _path, _data: (_ for _ in ()).throw(PermissionError("secret")),
    )

    removed = mod._remove_cursor_entries()

    assert removed is False
    assert json.loads(path.read_text()) == original
    warning = capsys.readouterr().err
    assert "cursor hook entries NOT removed" in warning
    assert "PermissionError" in warning
    assert "secret" not in warning


def test_install_no_agents_help_names_every_supported_agent(tmp_path: Path) -> None:
    result = run_cli(["install", "--help"], cwd=tmp_path)

    assert result.returncode == 0
    assert "Claude Code, Codex, Cursor, and pi" in " ".join(result.stdout.split())


def test_doctor_does_not_fail_for_an_agent_this_machine_lacks(tmp_path: Path) -> None:
    # install skips an undetected agent, so doctor must not call the
    # same machine broken — otherwise every Codex-less laptop exits 1 and
    # the quickstart has no verification step that can pass.
    home = tmp_path / "home"
    home.mkdir()
    repo = make_repo(tmp_path / "r")
    result = doctor(repo, home)
    # Codex only: the claude-code check also reads an MDM-managed path
    # outside HOME, so its verdict is machine-dependent.
    codex = findings(result)["codex hook"]
    assert codex.startswith("info"), codex
    assert "not detected" in codex


def test_doctor_finds_claude_entry_in_managed_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fleet machine has no per-user ~/.claude/settings.json — the entry is
    # in the MDM-managed file. Calling that machine unhooked would send an
    # operator chasing a working install. In-process so the managed path can
    # be redirected off the real /Library or /etc location.
    mod = _load_module()
    home = tmp_path / "home"
    managed = tmp_path / "managed" / "managed-settings.json"
    managed.parent.mkdir(parents=True)
    managed.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f'python3 "{SCRIPT}" '
                                    "mark --tool claude-code || true",
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    monkeypatch.setattr(mod, "_managed_claude_settings_path", lambda: managed)
    monkeypatch.setenv("HOME", str(home))
    finding = mod._doctor_agent_hook(
        "claude-code hook", mod._claude_settings_candidates(), "mark --tool claude-code"
    )
    assert finding[0] == mod.DOCTOR_OK
    assert str(managed) in finding[2]


def test_doctor_flags_unparseable_agent_config(tmp_path: Path) -> None:
    # install refuses to touch a config that does not parse and says so once,
    # on stderr, at install time. doctor is the only thing that can say it
    # later — reporting "absent" here would suggest a fix that will not work.
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "hooks.json").write_text("{not json")
    repo = make_repo(tmp_path / "r")
    result = doctor(repo, home)
    assert result.returncode == 1
    assert "not valid JSON" in findings(result)["codex hook"]


def test_doctor_flags_repo_with_no_hooks(tmp_path: Path) -> None:
    # An agent-created clone nobody ran install on.
    home = tmp_path / "home"
    home.mkdir()
    repo = make_repo(tmp_path / "r")
    result = doctor(repo, home, str(repo))
    assert result.returncode == 1
    hooks = findings(result)[f"hooks[{repo}]"]
    assert hooks.startswith("FAIL")
    for name in ("post-commit", "prepare-commit-msg", "pre-push"):
        assert f"{name} missing" in hooks


def test_doctor_flags_hook_set_missing_prepare_commit_msg(tmp_path: Path) -> None:
    # A partial hook installation can silently lose squash-merge Attribution.
    # Check each required hook rather than treating any hook as complete.
    home = tmp_path / "home"
    repo = installed_repo(tmp_path, home)
    (repo / ".git" / "hooks" / "prepare-commit-msg").unlink()
    result = doctor(repo, home, str(repo))
    assert result.returncode == 1
    hooks = findings(result)[f"hooks[{repo}]"]
    assert "prepare-commit-msg missing" in hooks
    assert "post-commit" not in hooks  # the two intact hooks are not blamed


def test_doctor_flags_hook_pointing_at_a_moved_script(tmp_path: Path) -> None:
    # A hook whose block survives a moved/deleted checkout keeps exiting 0
    # behind `|| true` while stamping nothing — indistinguishable from
    # working, which is the whole problem.
    home = tmp_path / "home"
    repo = installed_repo(tmp_path, home)
    hook = repo / ".git" / "hooks" / "post-commit"
    moved = tmp_path / "moved-checkout" / "sediment_attribution.py"
    hook.write_text(hook.read_text().replace(str(SCRIPT), str(moved)))
    result = doctor(repo, home, str(repo))
    assert result.returncode == 1
    assert "does not exist" in findings(result)[f"hooks[{repo}]"]


def test_doctor_flags_missing_notes_rewrite_ref(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = installed_repo(tmp_path, home)
    git(repo, "config", "--unset-all", "notes.rewriteRef")
    result = doctor(repo, home, str(repo))
    assert result.returncode == 1
    assert findings(result)[f"notes.rewriteRef[{repo}]"].startswith("FAIL")


def test_doctor_flags_foreign_template_dir(tmp_path: Path) -> None:
    # init.templateDir pointing anywhere but a sediment template means every
    # future clone on this machine starts unhooked.
    home = tmp_path / "home"
    home.mkdir()
    foreign = tmp_path / "someone-elses-template"
    (foreign / "hooks").mkdir(parents=True)
    (foreign / "hooks" / "post-commit").write_text("#!/bin/sh\necho hi\n")
    repo = make_repo(tmp_path / "r")
    global_config = doctor_env(home)["GIT_CONFIG_GLOBAL"]
    git_file_config(global_config, "init.templateDir", str(foreign))
    result = doctor(repo, home)
    assert result.returncode == 1
    fleet = findings(result)["fleet template"]
    assert fleet.startswith("FAIL")
    assert "not sediment's" in fleet


def test_doctor_unset_template_dir_is_not_a_failure(tmp_path: Path) -> None:
    # Most machines are per-repo installs. Failing them for having no fleet
    # template would make doctor red everywhere and worth ignoring.
    home = tmp_path / "home"
    repo = installed_repo(tmp_path, home)
    result = doctor(repo, home, str(repo))
    assert result.returncode == 0
    assert findings(result)["fleet template"].startswith("info")


def test_doctor_distinguishes_unreachable_origin_from_no_notes_ref(
    tmp_path: Path,
) -> None:
    # ls-remote failure (offline, auth, deleted remote) is a different fact
    # from "origin has no notes ref yet" — asserting the latter on failure
    # reports green on a diverged machine behind a broken remote.
    home = tmp_path / "home"
    repo = installed_repo(tmp_path, home)
    git(repo, "remote", "add", "origin", str(tmp_path / "gone"))
    result = doctor(repo, home, str(repo))
    state = findings(result)[f"notes ref[{repo}]"]
    assert state.startswith("info")
    assert "could not reach origin" in state
    assert "no notes ref on origin yet" not in state


def test_doctor_unreadable_fleet_script_degrades_not_tracebacks(
    tmp_path: Path,
) -> None:
    # A root-owned 0600 deployed copy under an unprivileged doctor run: the
    # byte-compare read must degrade to a finding, not swallow the report.
    home = tmp_path / "home"
    home.mkdir()
    out = tmp_path / "bundle"
    prefix = tmp_path / "deployed"
    emit = run_cli(
        ["install", "--fleet", "--out", str(out), "--prefix", str(prefix)],
        cwd=tmp_path,
    )
    assert emit.returncode == 0, emit.stderr
    shutil.copytree(out, prefix)
    deployed_script = prefix / "sediment_attribution.py"
    deployed_script.chmod(0o000)
    global_config = doctor_env(home)["GIT_CONFIG_GLOBAL"]
    git_file_config(global_config, "init.templateDir", str(prefix / "git-template"))
    try:
        result = doctor(tmp_path, home)
    finally:
        deployed_script.chmod(0o644)
    assert "Traceback" not in result.stderr
    fleet = findings(result)["fleet template"]
    assert fleet.startswith("info")
    assert "could not read" in fleet


def diverged_notes_repo(tmp_path: Path, home: Path) -> Path:
    """A repo whose notes history has no common ancestor with the remote's,
    so every push is silently rejected forever."""
    seeder = installed_repo(tmp_path, home, name="seeder")
    remote = make_remote(tmp_path, seeder)
    mark(seeder, session="sess-seed")
    run_cli(["stamp"], cwd=seeder)
    assert run_cli(["push-notes", "origin"], cwd=seeder).returncode == 0

    repo = installed_repo(tmp_path, home, name="diverged")
    git(repo, "remote", "add", "origin", str(remote))
    mark(repo, session="sess-diverged")
    run_cli(["stamp"], cwd=repo)  # stamped without ever fetching: disjoint root
    return repo


def test_doctor_detects_diverged_notes_ref_with_fetch(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = diverged_notes_repo(tmp_path, home)
    result = doctor(repo, home, str(repo), "--fetch")
    assert result.returncode == 1
    state = findings(result)[f"notes ref[{repo}]"]
    assert "DIVERGED" in state
    assert "repair-notes" in state  # the finding names its own fix


def test_doctor_without_fetch_says_it_cannot_classify(tmp_path: Path) -> None:
    # Read-only is the default, and a diverged machine is exactly one that
    # never fetched the remote notes — so doctor must say the tip is
    # unresolvable and name --fetch, not guess.
    home = tmp_path / "home"
    repo = diverged_notes_repo(tmp_path, home)
    result = doctor(repo, home, str(repo))
    state = findings(result)[f"notes ref[{repo}]"]
    assert "--fetch" in state
    assert "DIVERGED" not in state


def test_doctor_writes_nothing_without_fetch(tmp_path: Path) -> None:
    # "Best-effort read-only; never mutates anything". The tracking ref
    # the --fetch path writes is the one observable difference.
    home = tmp_path / "home"
    repo = diverged_notes_repo(tmp_path, home)
    before = git(repo, "show-ref")
    notes_before = git(repo, "rev-parse", NOTES_REF)
    assert "sediment-remote" not in before
    assert doctor(repo, home, str(repo)).returncode == 0
    assert git(repo, "show-ref") == before  # nothing written at all

    assert doctor(repo, home, str(repo), "--fetch").returncode == 1
    assert "sediment-remote" in git(repo, "show-ref")  # --fetch writes only this
    # The real notes ref is never touched: doctor reports, repair-notes fixes.
    assert git(repo, "rev-parse", NOTES_REF) == notes_before


def test_doctor_default_run_cannot_fail_on_a_diverged_ref(tmp_path: Path) -> None:
    # A consequence of the read-only default worth pinning: on a diverged
    # machine that never fetched, a plain `doctor` exits 0. The finding says
    # so in words, and a scheduled fleet run must pass --fetch (docs say so).
    # If this ever needs to be a FAIL, it is a one-line change here and there.
    home = tmp_path / "home"
    repo = diverged_notes_repo(tmp_path, home)
    result = doctor(repo, home, str(repo))
    assert result.returncode == 0
    state = findings(result)[f"notes ref[{repo}]"]
    assert "cannot fail without it" in state


def test_doctor_reports_in_sync_notes_ref(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = installed_repo(tmp_path, home)
    make_remote(tmp_path, repo)
    mark(repo)
    run_cli(["stamp"], cwd=repo)
    assert run_cli(["push-notes", "origin"], cwd=repo).returncode == 0
    result = doctor(repo, home, str(repo))
    assert result.returncode == 0, result.stdout
    assert findings(result)[f"notes ref[{repo}]"].startswith("ok")


def test_doctor_flags_markers_older_than_head(tmp_path: Path) -> None:
    # stamp deletes the marker file when it writes a note, so markers left
    # behind a newer commit mean the post-commit hook did not run — the
    # consumption failure that otherwise shows up months later as a commit
    # with no note.
    home = tmp_path / "home"
    repo = installed_repo(tmp_path, home)
    marker_file(repo).write_text(
        json.dumps(
            {
                "tool": "claude-code",
                "session_id": "sess-stale",
                "stamped_at": "2020-01-01T00:00:00+00:00",
            }
        )
        + "\n"
    )
    result = doctor(repo, home, str(repo))
    assert result.returncode == 1
    markers = findings(result)[f"markers[{repo}]"]
    assert markers.startswith("FAIL")
    assert "older than HEAD" in markers


def test_doctor_compares_marker_instants_not_strings(tmp_path: Path) -> None:
    # Regression: markers are stamped in UTC and git reports %cI in the
    # committer's local offset. Comparing the strings called a marker at
    # 12:57+00:00 older than a commit at 20:47+09:00 — the same instant two
    # hours apart — so every machine east of UTC got a false consumption
    # failure.
    home = tmp_path / "home"
    repo = installed_repo(tmp_path, home)
    (repo / "b.py").write_text("y = 2\n")
    git(repo, "add", "-A")
    subprocess.run(
        ["git", "commit", "-qm", "east of utc"],
        cwd=repo,
        check=True,
        timeout=60,
        env={
            **os.environ,
            "GIT_COMMITTER_DATE": "2026-08-05T20:47:29+09:00",
            "GIT_AUTHOR_DATE": "2026-08-05T20:47:29+09:00",
        },
    )
    # 12:57Z is 21:57+09:00 — ten minutes AFTER the commit, but a lower string.
    marker_file(repo).write_text(
        json.dumps(
            {
                "tool": "claude-code",
                "session_id": "sess-later",
                "stamped_at": "2026-08-05T12:57:17+00:00",
            }
        )
        + "\n"
    )
    result = doctor(repo, home, str(repo))
    markers = findings(result)[f"markers[{repo}]"]
    assert markers.startswith("info"), markers
    assert "not yet committed" in markers
    assert result.returncode == 0, result.stdout


def test_doctor_summarizes_log_misses_without_failing(tmp_path: Path) -> None:
    # The log is history, and the live checks decide the verdict: failing on
    # it would keep doctor red long after the cause was fixed. Its value is
    # naming the unhooked repos nobody passed on the command line.
    home = tmp_path / "home"
    repo = installed_repo(tmp_path, home)
    attribution_log(repo).write_text(
        "\n".join(
            json.dumps(entry)
            for entry in (
                {
                    "at": "2026-08-01T00:00:00+00:00",
                    "event": "unhooked-repo",
                    "git_dir": "/elsewhere/clone/.git",
                },
                {
                    "at": "2026-08-02T00:00:00+00:00",
                    "event": "notes-push-failed",
                    "remote": "origin",
                },
                {"at": "2026-08-03T00:00:00+00:00", "event": "auto-installed"},
            )
        )
        + "\n"
    )
    result = doctor(repo, home, str(repo))
    assert result.returncode == 0, result.stdout
    log = findings(result)["attribution log"]
    assert log.startswith("info")
    assert "1 unhooked-repo" in log and "1 notes-push-failed" in log
    assert "/elsewhere/clone/.git" in log  # the actionable half
    assert "auto-installed" not in log  # a success, not a miss


def test_doctor_flags_a_path_that_is_not_a_repo(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    result = doctor(tmp_path, home, str(plain))
    assert result.returncode == 1
    assert findings(result)[f"repo[{plain}]"].startswith("FAIL")


def test_doctor_with_no_repo_args_runs_machine_checks_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = installed_repo(tmp_path, home)
    result = doctor(repo, home)
    assert result.returncode == 0, result.stdout
    assert set(findings(result)) == {
        "claude-code hook",
        "claude-code transcript hook",
        "claude-code snapshot hook",
        "codex hook",
        "codex transcript hook",
        "cursor hooks",
        "pi extension",  # info on machines without pi; never a verdict there
        "fleet template",
        "attribution log",
        "server",  # info when not logged in
        "capture endpoint",  # info when dedicated hook delivery is unset
        "delivery",  # info when durable delivery is not enrolled
    }


def _pi_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    (home / ".pi" / "agent").mkdir(parents=True)
    return {"HOME": str(home), "CODEX_HOME": str(tmp_path / "cx")}


def _pi_settings(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".pi" / "agent" / "settings.json"


def test_install_registers_pi_extension(tmp_path: Path) -> None:
    env = _pi_env(tmp_path)
    repo = make_repo(tmp_path / "r")
    result = run_cli(["install", str(repo)], cwd=repo, extra_env=env)
    assert result.returncode == 0
    settings = json.loads(_pi_settings(tmp_path).read_text())
    [entry] = settings["extensions"]
    assert Path(entry).parent.name == "shims"
    assert Path(entry).name == "pi"
    assert Path(entry).is_dir()  # points at the real shim, not a stale path
    # Idempotent: a second install must not duplicate the entry.
    run_cli(["install", str(repo)], cwd=repo, extra_env=env)
    assert json.loads(_pi_settings(tmp_path).read_text())["extensions"] == [entry]


def test_install_pi_preserves_foreign_settings(tmp_path: Path) -> None:
    env = _pi_env(tmp_path)
    _pi_settings(tmp_path).write_text(
        json.dumps({"extensions": ["/other/ext.ts"], "theme": "dark"})
    )
    repo = make_repo(tmp_path / "r")
    run_cli(["install", str(repo)], cwd=repo, extra_env=env)
    settings = json.loads(_pi_settings(tmp_path).read_text())
    assert settings["theme"] == "dark"
    assert settings["extensions"][0] == "/other/ext.ts"  # foreign entry kept
    assert len(settings["extensions"]) == 2


def test_install_pi_refuses_corrupt_settings(tmp_path: Path) -> None:
    env = _pi_env(tmp_path)
    corrupt = '{"extensions": [",}'
    _pi_settings(tmp_path).write_text(corrupt)
    repo = make_repo(tmp_path / "r")
    result = run_cli(["install", str(repo)], cwd=repo, extra_env=env)
    assert result.returncode == 0  # git hooks still install
    assert "NOT installed" in result.stderr
    assert _pi_settings(tmp_path).read_text() == corrupt  # untouched


def test_install_pi_skips_non_list_extensions(tmp_path: Path) -> None:
    env = _pi_env(tmp_path)
    original = json.dumps({"extensions": "oops"})
    _pi_settings(tmp_path).write_text(original)
    repo = make_repo(tmp_path / "r")
    result = run_cli(["install", str(repo)], cwd=repo, extra_env=env)
    assert result.returncode == 0
    assert "NOT installed" in result.stderr
    assert _pi_settings(tmp_path).read_text() == original


def test_uninstall_agents_removes_pi_entry(tmp_path: Path) -> None:
    env = _pi_env(tmp_path)
    _pi_settings(tmp_path).write_text(json.dumps({"extensions": ["/other/ext.ts"]}))
    repo = make_repo(tmp_path / "r")
    run_cli(["install", str(repo)], cwd=repo, extra_env=env)
    result = run_cli(["uninstall", str(repo), "--agents"], cwd=repo, extra_env=env)
    assert result.returncode == 0
    extensions = json.loads(_pi_settings(tmp_path).read_text())["extensions"]
    assert extensions == ["/other/ext.ts"]  # ours gone, foreign kept


def test_doctor_pi_extension(tmp_path: Path) -> None:
    env = _pi_env(tmp_path)
    repo = make_repo(tmp_path / "r")

    def pi_line() -> str:
        result = run_cli(["doctor", str(repo)], cwd=repo, extra_env=env)
        return next(
            line for line in result.stdout.splitlines() if "pi extension" in line
        )

    assert pi_line().startswith("FAIL")  # pi present, extension not registered
    run_cli(["install", str(repo)], cwd=repo, extra_env=env)
    assert pi_line().startswith("ok")


def test_doctor_pi_not_detected_is_info_not_fail(tmp_path: Path) -> None:
    # pi is an opt-in second harness: a machine without it must not go red.
    env = {"HOME": str(tmp_path / "home"), "CODEX_HOME": str(tmp_path / "cx")}
    (tmp_path / "home").mkdir()
    result = run_cli(["doctor"], cwd=tmp_path, extra_env=env)
    line = next(line for line in result.stdout.splitlines() if "pi extension" in line)
    assert line.startswith("info")


def test_doctor_pi_missing_packaged_extension_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing extension cannot pass enrollment on a machine with pi."""
    module = _load_module()
    settings = tmp_path / ".pi" / "agent" / "settings.json"
    settings.parent.mkdir(parents=True)
    shim = tmp_path / "checkout" / "shims" / "pi"
    settings.write_text(json.dumps({"extensions": [str(shim)]}), encoding="utf-8")
    monkeypatch.setattr(module, "_pi_settings_path", lambda: settings)

    monkeypatch.setattr(module, "_pi_extension_dir", lambda: shim)
    assert module._doctor_pi_extension()[0] == module.DOCTOR_OK

    monkeypatch.setattr(module, "_pi_extension_dir", lambda: None)
    status, check, detail = module._doctor_pi_extension()
    assert status == module.DOCTOR_FAIL
    assert check == "pi extension"
    assert "reinstall" in detail


def test_mark_accepts_pi_tool(tmp_path: Path) -> None:
    # The pi shim fires `mark --tool pi`; argparse must accept it —
    # the live-pi e2e caught exit 2 here, silently swallowing every mark.
    repo = make_repo(tmp_path / "r")
    mark(repo, tool="pi", session="sess-pi")
    [entry] = marker_file(repo).read_text().splitlines()
    assert json.loads(entry)["tool"] == "pi"


def test_script_invocation_prefers_installed_sediment(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    exe = tmp_path / "bin" / "sediment"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(mod.os, "get_exec_path", lambda: [str(exe.parent)])
    assert mod._script_invocation("mark --tool pi") == f'"{exe}" mark --tool pi || true'


def test_script_invocation_skips_venv_shims(tmp_path, monkeypatch) -> None:
    # A worktree venv is the deleted-checkout rot this feature ends — its
    # shim must never be embedded even when it is first on PATH.
    mod = _load_module()
    venv_exe = tmp_path / ".venv" / "bin" / "sediment"
    venv_exe.parent.mkdir(parents=True)
    venv_exe.write_text("#!/bin/sh\n")
    venv_exe.chmod(0o755)
    monkeypatch.setattr(mod.os, "get_exec_path", lambda: [str(venv_exe.parent)])
    invocation = mod._script_invocation("stamp")
    assert str(venv_exe) not in invocation  # never the venv shim
    assert "attribution.py" in invocation  # the checkout fallback


def _login_config(home: Path, url: str = "http://127.0.0.1:8000") -> None:
    cfg_dir = home / ".sediment"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps(
            {
                "current": url,
                "servers": {
                    url: {
                        "token": "operator-405",
                        "org_id": "acme",
                        "capture_token": "tok-405",
                        "capture_authority": "ingest",
                        "capture_client_id": "developer",
                    }
                },
            }
        )
    )


def test_env_wiring_writes_0600_files_with_the_runbook_block(
    tmp_path, monkeypatch
) -> None:
    mod = _load_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    _login_config(tmp_path)
    summary = mod.cmd_install_env("developer", None, None)
    assert "restart running agent sessions" in summary
    sh = tmp_path / ".sediment" / "env.sh"
    fish = tmp_path / ".config" / "fish" / "conf.d" / "sediment.fish"
    for f in (sh, fish):
        assert os.stat(f).st_mode & 0o777 == 0o600
    body = sh.read_text()
    assert "export OTEL_LOGS_EXPORTER=otlp" in body
    assert "export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8000" in body
    assert "'Authorization=Bearer tok-405'" in body
    assert "export SEDIMENT_INGEST_TOKEN=tok-405" in body
    assert "export OTEL_RESOURCE_ATTRIBUTES=user.id=developer" in body
    assert "ANTHROPIC_BASE_URL" not in body  # no gateway flags given
    assert "set -gx OTEL_LOGS_EXPORTER otlp" in fish.read_text()
    assert "set -gx SEDIMENT_INGEST_TOKEN tok-405" in fish.read_text()


def test_env_wiring_gives_both_harnesses_the_gateway_key(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    _login_config(tmp_path)

    mod.cmd_install_env(
        None,
        "https://gateway.example.com",
        "gateway-key-405",
    )

    sh = (tmp_path / ".sediment" / "env.sh").read_text()
    fish = (tmp_path / ".config" / "fish" / "conf.d" / "sediment.fish").read_text()
    assert "export ANTHROPIC_AUTH_TOKEN=gateway-key-405" in sh
    assert "export SEDIMENT_GATEWAY_KEY=gateway-key-405" in sh
    assert "set -gx ANTHROPIC_AUTH_TOKEN gateway-key-405" in fish
    assert "set -gx SEDIMENT_GATEWAY_KEY gateway-key-405" in fish


def test_env_wiring_profile_block_is_idempotent(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    _login_config(tmp_path)
    profile = tmp_path / ".zprofile"
    profile.write_text("# mine\n")
    mod.cmd_install_env(None, None, None)
    mod.cmd_install_env(None, None, None)
    content = profile.read_text()
    assert content.count(mod.ENV_BLOCK_BEGIN) == 1
    assert content.startswith("# mine\n")


def test_env_wiring_skips_when_not_logged_in(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    summary = mod.cmd_install_env(None, None, None)
    assert "capture credential is absent" in summary
    assert not (tmp_path / ".sediment" / "env.sh").exists()


def test_unwire_env_removes_files_and_blocks(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    _login_config(tmp_path)
    profile = tmp_path / ".profile"
    profile.write_text("keep me\n")
    mod.cmd_install_env(None, None, None)
    removed = mod._unwire_env()
    assert len(removed) == 3  # env.sh, fish file, one profile block
    assert profile.read_text() == "keep me\n"
    assert not (tmp_path / ".sediment" / "env.sh").exists()


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class _FakeOpener:
    def __init__(self, response=None, error=None) -> None:
        self._response = response
        self._error = error
        self.request = None

    def open(self, request, timeout):
        self.request = request
        if self._error is not None:
            raise self._error
        return self._response


def test_doctor_server_ok_and_skew(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    _login_config(tmp_path)
    opener = _FakeOpener(
        _FakeResponse(
            b'{"org_id": "acme", "version": "9.9.9", "authority": "operator"}'
        )
    )
    monkeypatch.setattr(
        mod.urllib.request,
        "build_opener",
        lambda *_handlers: opener,
    )
    findings: list = []
    mod._doctor_server(findings)
    [(status, check, detail)] = findings
    assert check == "server[http://127.0.0.1:8000]"
    # The loaded module imports sediment_api's real version, which differs
    # from 9.9.9 → skew is info, never FAIL.
    assert status in (mod.DOCTOR_OK, mod.DOCTOR_INFO)
    assert "token valid (org acme)" in detail
    assert opener.request.get_header("User-agent") == "sediment-doctor/1"


def test_doctor_server_unreachable_fails(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    _login_config(tmp_path)

    monkeypatch.setattr(
        mod.urllib.request,
        "build_opener",
        lambda *_handlers: _FakeOpener(error=mod.urllib.error.URLError("refused")),
    )
    findings: list = []
    mod._doctor_server(findings)
    [(status, _check, detail)] = findings
    assert status == mod.DOCTOR_FAIL
    assert "unreachable" in detail


@pytest.mark.parametrize("authority", ["ingest", "operator"])
def test_doctor_checks_capture_only_enrollment(tmp_path, monkeypatch, authority):
    mod = _load_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SEDIMENT_URL", raising=False)
    monkeypatch.delenv("SEDIMENT_INGEST_TOKEN", raising=False)
    url = "http://127.0.0.1:8000"
    (tmp_path / ".sediment").mkdir()
    (tmp_path / ".sediment/config.json").write_text(
        json.dumps(
            {
                "current": url,
                "servers": {
                    url: {
                        "capture_token": "private-capture-token",
                        "capture_authority": "ingest",
                        "capture_client_id": "alice",
                    }
                },
            }
        )
    )
    opener = _FakeOpener(
        _FakeResponse(
            json.dumps(
                {
                    "org_id": "pilot",
                    "authority": authority,
                    "client_id": "alice",
                }
            ).encode()
        )
    )
    monkeypatch.setattr(mod.urllib.request, "build_opener", lambda *_: opener)

    findings = []
    mod._doctor_server(findings)

    [(status, check, detail)] = findings
    assert check == f"server[{url}]"
    assert "private-capture-token" not in detail
    assert opener.request.get_header("Authorization") == "Bearer private-capture-token"
    if authority == "ingest":
        assert status == mod.DOCTOR_OK
        assert "ingest token valid" in detail
    else:
        assert status == mod.DOCTOR_FAIL
        assert "--capture" in detail


def test_doctor_verifies_sourced_capture_credentials_behind_a_user_agent_filter(
    tmp_path, monkeypatch
):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            agent = self.headers.get("User-Agent", "")
            received.append((self.path, self.headers.get("Authorization"), agent))
            # Match an ingress that rejects the default urllib signature.
            self.send_response(403 if agent.startswith("Python-urllib") else 200)
            self.end_headers()
            self.wfile.write(
                b'{"org_id":"acme","authority":"ingest","client_id":"developer"}'
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        home = tmp_path / "home"
        monkeypatch.delenv("SEDIMENT_URL", raising=False)
        _login_config(home, url)
        config_path = home / ".sediment/config.json"
        config = json.loads(config_path.read_text())
        del config["servers"][url]["token"]
        config_path.write_text(json.dumps(config))
        repo = installed_repo(tmp_path, home)
        result = subprocess.run(
            [
                "/bin/sh",
                "-ec",
                '. "$HOME/.sediment/env.sh"; exec "$@"',
                "capture-doctor",
                sys.executable,
                str(SCRIPT),
                "doctor",
                str(repo),
            ],
            cwd=repo,
            env={**os.environ, **doctor_env(home)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert findings(result)[f"server[{url}]"] == (
            "ok reachable, ingest token valid (org acme)"
        )
        assert received
        assert all(
            request == ("/v1/me", "Bearer tok-405", "sediment-doctor/1")
            for request in received
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "url",
    [
        "http://sediment.example.com",
        "http://127.0.0.1\t:8000",
        "\x01http://127.0.0.1:8000",
    ],
    ids=["remote-http", "embedded-control-character", "leading-c0-control"],
)
def test_doctor_rejects_an_unsafe_saved_url_before_urlopen(
    url, tmp_path, monkeypatch
) -> None:
    mod = _load_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    _login_config(tmp_path, url)
    monkeypatch.setattr(
        mod.urllib.request,
        "build_opener",
        lambda *_handlers: pytest.fail("opened an unsafe saved URL"),
    )

    findings: list = []
    mod._doctor_server(findings)

    [(status, _check, detail)] = findings
    assert status == mod.DOCTOR_INFO
    assert "sediment login <url>" in detail


@pytest.mark.parametrize(
    "location",
    ["/redirect-target", "http://[bad"],
    ids=["relative", "malformed"],
)
def test_doctor_rejects_redirect_without_opening_the_target(
    location, tmp_path, monkeypatch
) -> None:
    mod = _load_module()
    monkeypatch.setenv("HOME", str(tmp_path))
    requests: list[tuple[str, str | None]] = []

    class RedirectingHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append((self.path, self.headers.get("Authorization")))
            if self.path == "/v1/me":
                self.send_response(302)
                self.send_header("Location", location)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"org_id": "redirected"}')

        def log_message(self, _format: str, *_args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        _login_config(tmp_path, f"http://{host}:{port}")
        findings: list = []
        mod._doctor_server(findings)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert [path for path, _authorization in requests] == ["/v1/me"]
    assert requests[0][1] is not None
    assert requests[0][1].startswith("Bearer ")
    [(status, _check, detail)] = findings
    assert status == mod.DOCTOR_FAIL
    assert "HTTP 302" in detail


def test_transcripts_flag_installs_snapshot_hook(tmp_path: Path) -> None:
    # The external-delta snapshots ride the same opt-in: they read the
    # files the agent edits, so a default install must never add them.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    repo = make_repo(tmp_path / "r")
    env = {"HOME": str(home), "CODEX_HOME": str(tmp_path / "cx")}
    run_cli(["install", str(repo)], cwd=repo, extra_env=env)
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert "PreToolUse" not in settings["hooks"]

    result = run_cli(["install", str(repo), "--transcripts"], cwd=repo, extra_env=env)
    assert result.returncode == 0
    assert "snapshot hook (PreToolUse): added" in result.stdout
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    [block] = settings["hooks"]["PreToolUse"]
    [hook] = block["hooks"]
    # Only the write tools: the hook reads the file each call is about to
    # change, so Bash and the read-only tools have nothing for it to do.
    assert block["matcher"] == "Edit|Write"
    assert f'"{sys.executable}" "{TRANSCRIPT}"' in hook["command"]
    assert hook["command"].endswith("snapshot --agent claude-code || true")

    run_cli(["uninstall", str(repo), "--agents"], cwd=repo, extra_env=env)
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["PreToolUse"] == []

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native enrollment checks use real configs, git hooks, and HTTP exchanges."""

import json
import os
import re
import subprocess
import sys
import threading
import textwrap
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sediment_cli import attribution


RELEASE_GUIDE = Path(__file__).parents[2] / "CONTRIBUTING.md"


def _guide_shell_block(containing: str, path: Path) -> str:
    guide = path.read_text(encoding="utf-8")
    matches = [
        textwrap.dedent(block)
        for _, block in re.findall(
            r"(?m)^( *)```bash\n(.*?)\n\1```", guide, flags=re.DOTALL
        )
        if containing in block
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.fixture
def enrollment(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("SEDIMENT_", "OTEL_", "CODEX_")):
            monkeypatch.delenv(name)
    home = tmp_path / "home"
    home.mkdir()
    for name in (".claude", ".codex", ".cursor", ".pi/agent"):
        (home / name).mkdir(parents=True)
    for key, value in {
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_GLOBAL": str(home / "gitconfig"),
        "GIT_CONFIG_SYSTEM": str(home / "gitconfig-system"),
        "SEDIMENT_ATTRIBUTION_LOG": str(home / "attribution.log"),
        "SEDIMENT_ATTRIBUTION_CONFIG": str(home / "attribution-config.json"),
    }.items():
        monkeypatch.setenv(key, value)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Pilot")
    git(repo, "config", "user.email", "pilot@example.invalid")
    git(repo, "commit", "--allow-empty", "-qm", "initial")
    assert run(repo, "install", "--no-env").returncode == 0
    return repo, home


def git(repo, *args):
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def run(repo, *args, env=None):
    return subprocess.run(
        [sys.executable, str(Path(attribution.__file__)), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, **(env or {})},
    )


def login(home, url="https://capture.example.invalid", token="secret-enrollment"):
    path = home / ".sediment/config.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "current": url,
                "servers": {
                    url: {
                        "token": f"operator-{token}",
                        "capture_token": token,
                        "capture_authority": "ingest",
                        "capture_client_id": "developer",
                    }
                },
            }
        )
    )


@pytest.mark.parametrize(
    "endpoint, expected",
    [
        ("", "info"),
        ("https://capture.example.invalid", "ok"),
        ("https://capture.example.invalid/v1/logs", "ok"),
        ("http://127.0.0.1:8000", "ok"),
        ("http://localhost:8000", "ok"),
        ("http://[::1]:8000", "ok"),
        ("http://capture.example.invalid/private-token", "FAIL"),
        # Keep the credential-bearing URL a runtime value, not a scan target.
        ("https://name:" + "secret@capture.example.invalid", "FAIL"),
        ("https://capture.example.invalid?token=private-token", "FAIL"),
        ("https://capture.example.invalid/custom", "FAIL"),
        ("http://127.1:8000", "FAIL"),
    ],
)
def test_endpoint_doctor_matches_transport_without_exposing_values(
    enrollment, endpoint, expected
):
    repo, _ = enrollment
    result = run(repo, "doctor", env={"SEDIMENT_OTLP_ENDPOINT": endpoint})
    assert f"{expected}" in result.stdout
    finding = next(
        line for line in result.stdout.splitlines() if "capture endpoint:" in line
    )
    assert finding.startswith(expected)
    assert (result.returncode == 1) == (expected == "FAIL")
    if endpoint:
        assert endpoint not in result.stdout + result.stderr


def test_transcript_install_warns_about_rejected_endpoint(enrollment):
    repo, _ = enrollment
    result = run(
        repo,
        "install",
        "--transcripts",
        "--no-env",
        env={"SEDIMENT_OTLP_ENDPOINT": "https://capture.invalid/private-token"},
    )
    assert result.returncode == 0
    assert "capture endpoint rejected" in result.stderr
    assert "private-token" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "name", ["sediment_cli/transcript.py", "sediment_transcript.py", "bin/sediment"]
)
@pytest.mark.parametrize("dangling", [False, True])
def test_doctor_recognizes_transcript_command_generations(enrollment, name, dangling):
    repo, home = enrollment
    script = home / name
    script.parent.mkdir(parents=True, exist_ok=True)
    if not dangling:
        script.touch()
    invocation = (
        f'"{script}" transcript'
        if name == "bin/sediment"
        else f'"{sys.executable}" "{script}"'
    )
    settings_path = home / ".claude/settings.json"
    settings = json.loads(settings_path.read_text())
    for event, args in (
        ("SessionEnd", "--agent claude-code"),
        ("PreToolUse", "snapshot --agent claude-code"),
    ):
        settings["hooks"][event] = [
            {"hooks": [{"type": "command", "command": f"{invocation} {args} || true"}]}
        ]
    settings_path.write_text(json.dumps(settings))

    result = run(repo, "doctor")

    for check in ("transcript", "snapshot"):
        finding = next(
            line
            for line in result.stdout.splitlines()
            if f"claude-code {check} hook:" in line
        )
        assert finding.startswith("FAIL" if dangling else "ok"), finding
        assert ("does not exist" if dangling else "present in") in finding


def test_generated_endpoint_and_pi_opt_in_are_independent(enrollment):
    repo, home = enrollment
    login(home)
    assert run(repo, "install").returncode == 0
    sh = home / ".sediment/env.sh"
    fish = home / ".config/fish/conf.d/sediment.fish"
    assert (
        "export SEDIMENT_OTLP_ENDPOINT=https://capture.example.invalid"
        in sh.read_text()
    )
    assert "SEDIMENT_PI_TRANSCRIPTS" not in sh.read_text()
    assert run(repo, "install", "--transcripts").returncode == 0
    assert "export SEDIMENT_PI_TRANSCRIPTS=1" in sh.read_text()
    assert "set -gx SEDIMENT_PI_TRANSCRIPTS 1" in fish.read_text()
    assert run(repo, "install").returncode == 0
    assert "export SEDIMENT_PI_TRANSCRIPTS=1" in sh.read_text()


def test_no_env_transcript_install_explains_pi_opt_in(enrollment):
    repo, home = enrollment
    login(home)
    result = run(repo, "install", "--transcripts", "--no-env")
    assert result.returncode == 0
    assert "SEDIMENT_PI_TRANSCRIPTS=1" in result.stdout
    assert "SEDIMENT_OTLP_ENDPOINT" in result.stdout
    assert not (home / ".sediment/env.sh").exists()


def test_codex_profile_preserves_settings_and_refreshes_resolved_secret(enrollment):
    repo, home = enrollment
    login(home, token='secret-"-token')
    profile = home / ".codex/pilot.config.toml"
    original = 'model = "gpt-6-astra"\n# preserve comment\n[features]\nimage_generation = true\n'
    profile.write_text(original)
    profile.chmod(0o644)
    first = run(repo, "install", "--codex-profile", "pilot", "--no-env")
    assert first.returncode == 0, first.stderr
    content = profile.read_text()
    assert content.startswith(original)
    parsed = tomllib.loads(content)
    assert parsed["model"] == "gpt-6-astra"
    assert parsed["features"] == {"image_generation": True}
    assert (
        parsed["otel"]["exporter"]["otlp-http"]["headers"]["Authorization"]
        == 'Bearer secret-"-token'
    )
    assert parsed["otel"]["log_user_prompt"] is False
    assert profile.stat().st_mode & 0o777 == 0o600
    assert "secret-" not in first.stdout + first.stderr
    assert "codex --profile pilot" in first.stdout
    again = run(repo, "install", "--codex-profile", "pilot", "--no-env")
    assert again.returncode == 0
    assert profile.read_text() == content
    login(home, token="refreshed-private-token")
    assert run(repo, "install", "--codex-profile", "pilot", "--no-env").returncode == 0
    assert "Bearer refreshed-private-token" in profile.read_text()
    assert profile.read_text().startswith(original)
    assert profile.read_text().count("[otel]") == 1


@pytest.mark.parametrize(
    "original",
    [
        '[otel]\nenvironment="other"\n',
        'model="unterminated\n',
        "# BEGIN SEDIMENT CODEX TELEMETRY\n",
        'description="""\n# BEGIN SEDIMENT CODEX TELEMETRY\nprivate description\n# END SEDIMENT CODEX TELEMETRY\n"""\n',
    ],
)
def test_codex_profile_refuses_unmanaged_or_invalid_content(enrollment, original):
    repo, home = enrollment
    login(home)
    profile = home / ".codex/pilot.config.toml"
    profile.write_text(original)
    result = run(repo, "install", "--codex-profile", "pilot")
    assert result.returncode == 1
    assert profile.read_text() == original
    assert "secret-enrollment" not in result.stdout + result.stderr


@pytest.mark.parametrize("name", ["../escape", "/tmp/escape", "bad name", ".", ""])
def test_codex_profile_rejects_unsafe_names(enrollment, name):
    repo, home = enrollment
    login(home)
    result = run(repo, "install", "--codex-profile", name)
    assert result.returncode == 2
    assert "profile name" in result.stderr


def test_enrollment_invalid_login_shape_fails_without_traceback(enrollment):
    repo, home = enrollment
    login(home)
    config = home / ".sediment/config.json"
    config.write_text(
        json.dumps({"current": "https://capture.invalid", "servers": ["malformed"]})
    )
    result = run(repo, "install", "--codex-profile", "pilot")
    assert result.returncode == 1
    assert "login <url> --capture" in result.stderr
    assert "Traceback" not in result.stderr


def test_codex_profile_refuses_to_change_the_table_of_unmanaged_settings(enrollment):
    repo, home = enrollment
    login(home)
    assert run(repo, "install", "--codex-profile", "pilot").returncode == 0
    profile = home / ".codex/pilot.config.toml"
    # TOML keeps this setting in [otel], despite the intervening end comment.
    original = profile.read_text() + 'unmanaged_setting="preserve-table"\n'
    profile.write_text(original)
    result = run(repo, "install", "--codex-profile", "pilot")
    assert result.returncode == 1
    assert profile.read_text() == original


def test_uninstall_agents_removes_all_managed_codex_profiles(enrollment):
    repo, home = enrollment
    login(home)
    codex = home / ".codex"
    original = '# personal settings\nmodel="gpt-6-astra"\n'
    (codex / "pilot.config.toml").write_text(original)
    for name in ("pilot", "only-telemetry", "comments"):
        assert run(repo, "install", "--codex-profile", name).returncode == 0
    comments = codex / "comments.config.toml"
    comments.write_text(comments.read_text() + "# keep this comment\n")
    unrelated = codex / "other.config.toml"
    unrelated.write_text('[otel]\nenvironment="other"\n')
    # Without --agents, user-level telemetry remains enrolled.
    assert run(repo, "uninstall").returncode == 0
    assert "Bearer secret-enrollment" in (codex / "pilot.config.toml").read_text()

    result = run(repo, "uninstall", "--agents")

    assert result.returncode == 0, result.stderr
    assert (codex / "pilot.config.toml").read_text() == original
    assert not (codex / "only-telemetry.config.toml").exists()
    assert comments.read_text() == "# keep this comment\n"
    assert unrelated.read_text() == '[otel]\nenvironment="other"\n'
    assert "secret-enrollment" not in result.stdout + result.stderr
    for name in ("pilot", "only-telemetry", "comments"):
        assert f"{name}.config.toml" in result.stdout
    again = run(repo, "uninstall", "--agents")
    assert again.returncode == 0
    assert "Codex telemetry" not in again.stdout


@pytest.mark.parametrize(
    "invalid",
    [
        '# BEGIN SEDIMENT CODEX TELEMETRY\nmodel="private-token\n',
        "# END SEDIMENT CODEX TELEMETRY\n",
        'description="""\n# BEGIN SEDIMENT CODEX TELEMETRY\nprivate-token\n'
        '# END SEDIMENT CODEX TELEMETRY\n"""\n',
        '# BEGIN SEDIMENT CODEX TELEMETRY\n[otel]\nenvironment="sediment"\n'
        '# END SEDIMENT CODEX TELEMETRY\nunmanaged="private-token"\n',
    ],
)
def test_uninstall_agents_reports_invalid_codex_profiles_without_rewriting(
    enrollment, invalid
):
    repo, home = enrollment
    login(home)
    assert run(repo, "install", "--codex-profile", "valid").returncode == 0
    path = home / ".codex/broken.config.toml"
    path.write_text(invalid)

    result = run(repo, "uninstall", "--agents")

    assert result.returncode == 1
    assert path.read_text() == invalid
    assert "broken.config.toml" in result.stderr
    assert "skipped" in result.stderr
    assert "private-token" not in result.stdout + result.stderr
    assert not (home / ".codex/valid.config.toml").exists()


@pytest.mark.parametrize("kind", ["symlink", "unreadable", "invalid-encoding"])
def test_uninstall_agents_reports_unreadable_codex_profiles(enrollment, kind):
    repo, home = enrollment
    login(home)
    assert run(repo, "install", "--codex-profile", "pilot").returncode == 0
    path = home / ".codex/pilot.config.toml"
    original = path.read_bytes()
    if kind == "symlink":
        target = home / "foreign.toml"
        path.rename(target)
        path.symlink_to(target)
    elif kind == "unreadable":
        path.chmod(0)
    else:
        original += b"\xff"
        path.write_bytes(original)
    try:
        result = run(repo, "uninstall", "--agents")
    finally:
        if kind == "unreadable":
            path.chmod(0o600)

    assert result.returncode == 1
    assert path.read_bytes() == original
    assert "pilot.config.toml" in result.stderr
    assert "skipped" in result.stderr
    assert "Traceback" not in result.stderr
    assert "secret-enrollment" not in result.stdout + result.stderr
    if kind == "symlink":
        assert path.is_symlink()


@pytest.mark.parametrize("entry", ["standalone", "installed"])
@pytest.mark.parametrize("args", [["install"], ["install", "--fleet"]])
def test_windows_install_refuses_before_posix_imports(enrollment, entry, args):
    repo, home = enrollment
    before = {
        str(path.relative_to(home)): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }
    probe = """
import importlib.metadata, importlib.util, os, sys
from pathlib import Path
entry, source, *args = sys.argv[1:]
console = next(iter(importlib.metadata.entry_points(group="console_scripts", name="sediment")))
sys.modules["fcntl"] = None
if entry == "standalone":
    spec = importlib.util.spec_from_file_location("capture_entry", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    main = module.main
else:
    main = console.load()
os.name = "nt"
raise SystemExit(main(args))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, entry, attribution.__file__, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert "Windows" in result.stderr
    assert "macOS or Linux" in result.stderr
    assert "Traceback" not in result.stderr
    assert "fcntl" not in result.stderr
    assert before == {
        str(path.relative_to(home)): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }


def test_marker_missing_posix_locks_logs_and_degrades(enrollment):
    repo, home = enrollment
    probe = """
import importlib.util, sys
sys.modules["fcntl"] = None
spec = importlib.util.spec_from_file_location("capture_entry", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
raise SystemExit(module.main(["mark", "--tool", "codex"]))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, attribution.__file__],
        input=json.dumps({"session_id": "pilot-session", "cwd": str(repo)}),
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "marker_write_failed" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (repo / ".git/sediment-sessions").exists()
    assert "marker_write_failed" in (home / "attribution.log").read_text()


def test_source_cli_install_binds_hooks_to_the_invoked_installation(enrollment):
    repo, home = enrollment
    older = home / "bin/sediment"
    older.parent.mkdir()
    older.write_text("#!/bin/sh\nexit 1\n")
    older.chmod(0o755)
    source_cli = Path(sys.executable).with_name("sediment")
    result = subprocess.run(
        [str(source_cli), "install", str(repo), "--transcripts", "--no-env"],
        cwd=home,
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "PATH": f"{older.parent}:{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    hooks = [
        repo / ".git/hooks/post-commit",
        home / ".codex/hooks.json",
        home / ".claude/settings.json",
        home / ".cursor/hooks.json",
    ]
    for path in hooks:
        content = path.read_text()
        assert str(source_cli) in content
        assert str(older) not in content
    claude = json.loads((home / ".claude/settings.json").read_text())
    transcript_command = claude["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
    assert f'"{source_cli}" transcript' in transcript_command


def event(kind="developer_decision", agent="codex"):
    return {
        "event_type": kind,
        "agent_harness": agent,
        "fact_id": f"{kind}-fact",
        "occurred_at": "2026-09-09T12:00:00+00:00",
    }


@pytest.fixture
def capture_server():
    state = {
        "body": {
            "found": True,
            "session_id": "pilot-session",
            "omitted_events": 0,
            "timeline": [event()],
        },
        "status": 200,
        "requests": [],
    }

    class Capture(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            state["requests"].append((self.path, self.headers.get("Authorization")))
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(state.get("raw", json.dumps(state["body"]).encode()))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def note(repo, agent="codex", session_id="pilot-session"):
    git(
        repo,
        "notes",
        "--ref=refs/notes/sediment",
        "add",
        "-f",
        "-m",
        json.dumps(
            {
                "v": 1,
                "sessions": [
                    {
                        "tool": agent,
                        "session_id": session_id,
                        "stamped_at": "2026-09-15T00:00:00+00:00",
                    }
                ],
            }
        ),
        "HEAD",
    )


def verify(repo, endpoint, *flags, agent="codex"):
    return run(
        repo,
        "doctor",
        str(repo),
        "--agent",
        agent,
        "--session-id",
        "pilot-session",
        *flags,
        env={"SEDIMENT_OTLP_ENDPOINT": endpoint, "SEDIMENT_PI_TRANSCRIPTS": "1"},
    )


@pytest.mark.parametrize("agent", ["cursor", "codex", "pi"])
def test_enrollment_requires_selected_harness_fact_and_exact_head_note(
    enrollment, capture_server, agent
):
    repo, home = enrollment
    endpoint, state = capture_server
    login(home, endpoint)
    note(repo, agent)
    state["body"]["timeline"] = [event(agent=agent)]
    # An unrelated harness config cannot make this harness's verification fail.
    (home / ".claude/settings.json").write_text("invalid unrelated config")

    result = verify(repo, endpoint, agent=agent)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Developer decision: observed" in result.stdout
    assert "commit Session note: observed" in result.stdout
    assert "claude-code" not in result.stdout
    assert state["requests"] == [
        ("/query/session/pilot-session", "Bearer operator-secret-enrollment")
    ]
    assert "secret-enrollment" not in result.stdout + result.stderr


@pytest.mark.parametrize("mismatch", ["harness", "session", "missing-note"])
def test_enrollment_cannot_use_another_harness_or_session_note(
    enrollment, capture_server, mismatch
):
    repo, home = enrollment
    endpoint, state = capture_server
    login(home, endpoint)
    if mismatch == "harness":
        note(repo)
        state["body"]["timeline"] = [event(agent="pi")]
    elif mismatch == "session":
        note(repo, session_id="another-session")
    result = verify(repo, endpoint)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "missing" in result.stdout


@pytest.mark.parametrize(
    "changes",
    [
        {"found": False},
        {"omitted_events": 1},
        {"timeline": []},
        {"timeline": [None]},
        {"session_id": "wrong-session"},
        {"timeline": [{"event_type": "developer_decision", "agent_harness": "codex"}]},
    ],
)
def test_enrollment_declines_absent_incomplete_or_malformed_evidence(
    enrollment, capture_server, changes
):
    repo, home = enrollment
    endpoint, state = capture_server
    login(home, endpoint)
    note(repo)
    state["body"].update(changes)
    result = verify(repo, endpoint)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("status", [401, 409, 503, 302])
def test_enrollment_http_failure_is_bounded_and_rejects_redirects(
    enrollment, capture_server, status
):
    repo, home = enrollment
    endpoint, state = capture_server
    login(home, endpoint)
    note(repo)
    state["status"] = status
    state["body"] = {"detail": "private-server-text"}
    result = verify(repo, endpoint)
    assert result.returncode == 1
    assert f"HTTP {status}" in result.stdout
    assert "private-server-text" not in result.stdout + result.stderr
    assert len(state["requests"]) == 1


@pytest.mark.parametrize(
    "raw",
    [b"not json private-server-text", b"[]", b"x" * (8 * 1024 * 1024 + 1)],
    ids=["invalid-json", "wrong-shape", "oversized"],
)
def test_enrollment_rejects_unreadable_or_oversized_response(
    enrollment, capture_server, raw
):
    repo, home = enrollment
    endpoint, state = capture_server
    login(home, endpoint)
    note(repo)
    state["raw"] = raw
    result = verify(repo, endpoint)
    assert result.returncode == 1
    assert "unreadable" in result.stdout
    assert "private-server-text" not in result.stdout + result.stderr


@pytest.mark.parametrize("agent", ["codex", "pi"])
def test_enrollment_optional_content_and_calls_must_be_observed(
    enrollment, capture_server, agent
):
    repo, home = enrollment
    endpoint, state = capture_server
    login(home, endpoint)
    note(repo, agent)
    assert run(repo, "install", "--transcripts", "--no-env").returncode == 0
    state["body"]["timeline"] = [event(agent=agent)]
    missing = verify(repo, endpoint, "--transcripts", "--inference-calls", agent=agent)
    assert missing.returncode == 1
    assert "Edit observation: missing" in missing.stdout
    assert "Session Inference call: missing" in missing.stdout
    state["body"]["timeline"] += [
        event("edit_observation", agent),
        event("inference_call", None),
    ]
    observed = verify(repo, endpoint, "--transcripts", "--inference-calls", agent=agent)
    assert observed.returncode == 0, observed.stdout + observed.stderr


def test_cursor_enrollment_reports_content_and_calls_as_unsupported(
    enrollment, capture_server
):
    repo, home = enrollment
    endpoint, state = capture_server
    login(home, endpoint)
    note(repo, "cursor")
    state["body"]["timeline"] = [event(agent="cursor")]
    result = verify(
        repo, endpoint, "--transcripts", "--inference-calls", agent="cursor"
    )
    assert result.returncode == 1
    assert "Edit observation: unsupported for Cursor" in result.stdout
    assert "Session Inference call: unsupported for Cursor" in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ("--agent", "codex"),
        ("--session-id", "session"),
        ("--transcripts",),
        ("--inference-calls",),
        ("--agent", "codex", "--session-id", ""),
    ],
)
def test_enrollment_flags_require_one_repository_agent_and_session(enrollment, args):
    repo, _ = enrollment
    result = run(repo, "doctor", *args)
    assert result.returncode == 2
    assert "one REPO" in result.stderr


def test_delivery_enrollment_is_explicit_and_doctor_reports_worker(
    enrollment, tmp_path
):
    repo, home = enrollment
    direct = run(repo, "doctor")
    assert "delivery: best_effort" in direct.stdout
    directory = tmp_path / "pending"
    result = run(repo, "doctor", env={"SEDIMENT_DELIVERY_DIR": str(directory)})
    assert result.returncode == 1
    assert "delivery:" in result.stdout and "worker" in result.stdout
    assert not directory.exists(), "doctor must not initialize or enable storage"
    login(home)
    installed = run(repo, "install", env={"SEDIMENT_DELIVERY_DIR": str(directory)})
    assert "delivery:" in installed.stdout
    assert "SEDIMENT_DELIVERY_DIR=" in (home / ".sediment/env.sh").read_text()
    # A reinstall keeps the earlier explicit storage consent without evaluating shell.
    run(repo, "install")
    assert str(directory) in (home / ".sediment/env.sh").read_text()


def test_release_rehearsal_block_binds_clean_revision_and_test_database() -> None:
    block = _guide_shell_block("ACCEPTANCE_TMP=", RELEASE_GUIDE)

    assert "SEDIMENT_TEST_DATABASE_URL=" in block
    assert "SEDIMENT_DATABASE_URL" not in block
    assert "--database-url" not in block
    assert block.index('test "$(git rev-parse HEAD)" = "$SEDIMENT_REVISION"') < (
        block.index("scripts/release_rehearsal.py")
    )
    assert "git status --porcelain=v1 --untracked-files=all" in block
    assert "worktree=clean" in block
    assert "if {" in block
    assert "&&\n  uv run python scripts/release_rehearsal.py" in block
    assert 'mv "$ACCEPTANCE_TMP" "$ACCEPTANCE_LOG"' in block
    assert 'rm -f "$ACCEPTANCE_TMP"\n  exit 1' in block


def test_release_rehearsal_block_executes_as_an_atomic_record(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Pilot")
    git(repo, "config", "user.email", "pilot@example.invalid")
    git(repo, "commit", "--allow-empty", "-qm", "initial")
    revision = git(repo, "rev-parse", "HEAD")
    block = _guide_shell_block("ACCEPTANCE_TMP=", RELEASE_GUIDE).replace(
        "SEDIMENT_REVISION='<approved full commit hash>'",
        f"SEDIMENT_REVISION='{revision}'",
    )
    binary = tmp_path / "bin"
    binary.mkdir()
    uv = binary / "uv"
    uv.write_text(
        "#!/bin/sh\nset -e\n"
        'test "$*" = "run python scripts/release_rehearsal.py"\n'
        'test "$SEDIMENT_TEST_DATABASE_URL" = '
        '"postgresql+psycopg://postgres:postgres@localhost:5432/postgres"\n'
        'printf "pipeline acceptance: {\\"fixture_version\\": 1}\\n"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{binary}:{os.environ['PATH']}",
    }

    env.pop("SEDIMENT_TEST_DATABASE_URL", None)

    result = subprocess.run(
        ["/bin/sh", "-c", block],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    acceptance = home / "sediment-acceptance" / (f"pipeline-acceptance-{revision}.log")
    assert acceptance.read_text(encoding="utf-8").splitlines() == [
        f"sediment_revision={revision}",
        "worktree=clean",
        'pipeline acceptance: {"fixture_version": 1}',
    ]
    acceptance.unlink()
    for invalid in (
        block.replace(
            "export SEDIMENT_TEST_DATABASE_URL=", "SEDIMENT_TEST_DATABASE_URL="
        ),
        block.replace("uv run python scripts/release_rehearsal.py", "uv wrong-command"),
    ):
        failed_contract = subprocess.run(
            ["/bin/sh", "-c", invalid],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert failed_contract.returncode == 1
        assert not acceptance.exists()
    (repo / "dirty").write_text("uncommitted\n", encoding="utf-8")

    failed = subprocess.run(
        ["/bin/sh", "-c", block],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert failed.returncode == 1
    assert not acceptance.exists()
    assert not acceptance.with_suffix(".log.tmp").exists()


def test_fleet_bundle_ships_standalone_transport_implementation(enrollment, tmp_path):
    repo, _ = enrollment
    bundle = tmp_path / "fleet"
    result = run(repo, "install", "--fleet", "--out", str(bundle))
    assert result.returncode == 0, result.stderr
    helper = bundle / "sediment_delivery.py"
    transcript = bundle / "sediment_transcript.py"
    assert helper.exists(), "fleet bundle omitted replay ownership"
    assert transcript.exists(), "fleet bundle omitted the shared sender"
    assert "runpy" not in helper.read_text(), (
        "distributed helper must not require a checkout"
    )
    result = subprocess.run(
        [sys.executable, "-I", str(helper), "status"],
        env={**os.environ, "SEDIMENT_DELIVERY_DIR": str(tmp_path / "absent")},
        capture_output=True,
        text=True,
        timeout=10,
        cwd=bundle,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["pending"] == 0
    assert not (tmp_path / "absent").exists()


def test_delivery_doctor_observes_real_worker_without_writes(enrollment, tmp_path):
    from sediment_cli import delivery

    repo, _ = enrollment
    directory = tmp_path / "private-source-buffer"
    stopped = threading.Event()
    worker = threading.Thread(
        target=delivery.watch,
        args=(directory,),
        kwargs={"env": {}, "stop_event": stopped},
    )
    worker.start()
    try:
        import time

        deadline = time.monotonic() + 3
        while (
            not delivery.status(directory)["worker_running"]
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert delivery.status(directory)["worker_running"]
        before = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
            for path in directory.iterdir()
        }
        result = run(repo, "doctor", env={"SEDIMENT_DELIVERY_DIR": str(directory)})
        assert result.returncode == 0, result.stdout + result.stderr
        finding = next(
            line for line in result.stdout.splitlines() if "delivery:" in line
        )
        assert finding.startswith("ok") and "worker running" in finding
        assert str(directory) not in result.stdout + result.stderr
        after = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
            for path in directory.iterdir()
        }
        assert after == before
    finally:
        stopped.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    result = run(repo, "doctor", env={"SEDIMENT_DELIVERY_DIR": str(directory)})
    assert result.returncode == 1
    assert "worker is not running" in result.stdout


def test_delivery_enrollment_survives_supervised_process_restart(enrollment, tmp_path):
    import time

    from sediment_cli import delivery

    repo, home = enrollment
    login(home)
    directory = tmp_path / "private-source-buffer"
    env = {**os.environ, "SEDIMENT_DELIVERY_DIR": str(directory)}

    def start_worker():
        process = subprocess.Popen(
            [sys.executable, delivery.__file__, "replay", "--watch"],
            cwd=repo,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 3
        while (
            not delivery.status(directory)["worker_running"]
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert delivery.status(directory)["worker_running"]
        return process

    worker = start_worker()
    try:
        installed = run(repo, "install", env={"SEDIMENT_DELIVERY_DIR": str(directory)})
        assert installed.returncode == 0, installed.stdout + installed.stderr
        doctor = run(repo, "doctor", env={"SEDIMENT_DELIVERY_DIR": str(directory)})
        assert "delivery: buffered" in doctor.stdout
        assert "worker running" in doctor.stdout

        worker.terminate()
        assert worker.wait(timeout=3) == 0
        assert not delivery.status(directory)["worker_running"]

        worker = start_worker()
        doctor = run(repo, "doctor", env={"SEDIMENT_DELIVERY_DIR": str(directory)})
        assert "delivery: buffered" in doctor.stdout
        assert "worker running" in doctor.stdout
    finally:
        worker.terminate()
        worker.wait(timeout=3)


def test_delivery_doctor_rejects_unsafe_storage_without_exposing_path(
    enrollment, tmp_path
):
    repo, _ = enrollment
    directory = tmp_path / "private-source-buffer"
    directory.mkdir(mode=0o755)
    result = run(repo, "doctor", env={"SEDIMENT_DELIVERY_DIR": str(directory)})
    assert result.returncode == 1
    finding = next(line for line in result.stdout.splitlines() if "delivery:" in line)
    assert finding.startswith("FAIL") and "unsafe" in finding
    assert str(directory) not in result.stdout + result.stderr
    assert list(directory.iterdir()) == []
    assert directory.stat().st_mode & 0o777 == 0o755


def test_delivery_install_preserves_literal_consent_and_explicit_disable(
    enrollment, tmp_path
):
    import shlex

    repo, home = enrollment
    login(home)
    directory = tmp_path / "buffer $(touch SHOULD_NOT_EXIST) 'quoted'"
    first = run(repo, "install", env={"SEDIMENT_DELIVERY_DIR": str(directory)})
    assert first.returncode == 1
    assert "start a supervised delivery worker" in first.stdout
    assert not directory.exists()
    second = run(repo, "install")
    assert second.returncode == 1
    shell = (home / ".sediment/env.sh").read_text()
    fish = (home / ".config/fish/conf.d/sediment.fish").read_text()
    assert f"export SEDIMENT_DELIVERY_DIR={shlex.quote(str(directory))}" in shell
    assert f"set -gx SEDIMENT_DELIVERY_DIR {shlex.quote(str(directory))}" in fish
    assert not (repo / "SHOULD_NOT_EXIST").exists()
    disabled = run(repo, "install", env={"SEDIMENT_DELIVERY_DIR": ""})
    assert disabled.returncode == 0
    assert "best_effort" in disabled.stdout
    assert "SEDIMENT_DELIVERY_DIR" not in (home / ".sediment/env.sh").read_text()


def test_fleet_standalone_resolves_copied_clients_and_hook_paths(enrollment, tmp_path):
    repo, _ = enrollment
    bundle = tmp_path / "fleet"
    assert run(repo, "install", "--fleet", "--out", str(bundle)).returncode == 0
    entry = bundle / "sediment_attribution.py"
    probe = """
import json, runpy, sys
from pathlib import Path
namespace = runpy.run_path(sys.argv[1])
print(json.dumps({
    'transcript': Path(namespace['_transcript_client']().__file__).name,
    'delivery': Path(namespace['_capture_client']('delivery').__file__).name,
    'invocation': namespace['_transcript_invocation'](),
}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(entry)],
        cwd=bundle,
        capture_output=True,
        text=True,
        env=os.environ,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["transcript"] == "sediment_transcript.py"
    assert observed["delivery"] == "sediment_delivery.py"
    assert str(bundle / "sediment_transcript.py") in observed["invocation"]
    # A second fleet generation also copies complete source implementations.
    regenerated = tmp_path / "regenerated"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(entry),
            "install",
            "--fleet",
            "--out",
            str(regenerated),
        ],
        cwd=bundle,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    for name in (
        "sediment_attribution.py",
        "sediment_transcript.py",
        "sediment_delivery.py",
    ):
        assert (regenerated / name).read_bytes() == (bundle / name).read_bytes()


@pytest.mark.parametrize("endpoint", ["", "http://127.0.0.1:8000/v1/logs/"])
@pytest.mark.parametrize("has_transcript", [False, True])
def test_standalone_doctor_reports_missing_helper_without_traceback(
    enrollment, tmp_path, endpoint, has_transcript
):
    repo, _ = enrollment
    directory = tmp_path / "isolated"
    directory.mkdir()
    script = directory / "sediment_attribution.py"
    script.write_bytes(Path(attribution.__file__).read_bytes())
    if has_transcript:
        (directory / "sediment_transcript.py").write_bytes(
            Path(attribution.__file__).with_name("transcript.py").read_bytes()
        )
    result = subprocess.run(
        [sys.executable, "-I", str(script), "doctor"],
        cwd=repo,
        env={
            **os.environ,
            "SEDIMENT_DELIVERY_DIR": str(tmp_path / "private"),
            "SEDIMENT_OTLP_ENDPOINT": endpoint,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "delivery: helper unavailable" in result.stdout
    assert "Traceback" not in result.stderr


def test_whitespace_buffer_setting_is_disabled_in_enrollment_and_doctor(monkeypatch):
    monkeypatch.setenv("SEDIMENT_DELIVERY_DIR", "   ")
    assert attribution._delivery_directory([]) is None
    finding = attribution._doctor_delivery("   ")
    assert finding == (
        attribution.DOCTOR_INFO,
        "delivery",
        "best_effort; buffering is disabled",
    )


@pytest.mark.parametrize("mode", ["success", "redirect", "duplicate"])
def test_recovery_session_query_uses_operator_transport(enrollment, mode):
    repo, home = enrollment
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            if mode == "redirect" and self.path != "/redirected":
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.end_headers()
                return
            if self.headers.get("Authorization") != "Bearer operator-recovery-ingest":
                self.send_response(403)
                self.end_headers()
                return
            fact = {"event_type": "edit_observation", "fact_id": "recovery-edit"}
            body = json.dumps(
                {
                    "found": True,
                    "omitted_events": 0,
                    "timeline": [fact] * (2 if mode == "duplicate" else 1),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    login(home, url=url, token="recovery-ingest")
    probe = textwrap.dedent(
        """\
        from sediment_cli.client import get_json

        dossier = get_json("/query/session/recovery-session")
        assert dossier["found"] and dossier["omitted_events"] == 0
        matching = [
            event
            for event in dossier["timeline"]
            if event["event_type"] == "edit_observation"
        ]
        assert len(matching) == 1
        print(f'recovery Fact verified: {matching[0]["fact_id"]}')
        """
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=repo,
            env={
                **os.environ,
                "SEDIMENT_URL": url,
                "SEDIMENT_INGEST_TOKEN": "recovery-ingest",
            },
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert requests == [
        ("/query/session/recovery-session", "Bearer operator-recovery-ingest")
    ]
    assert (result.returncode == 0) is (mode == "success"), result.stderr
    if mode == "success":
        assert "recovery Fact verified: recovery-edit" in result.stdout
    assert "recovery-ingest" not in result.stdout + result.stderr

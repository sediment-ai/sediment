# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the packaged transcript extractor.

Unit tests drive the pure extraction over synthetic transcript JSONL plus
real files on disk; the end-to-end test runs the real script as a SessionEnd
hook against a real local HTTP server (no mocks, per repo conventions); and
the wire cross-pin feeds the client's own payload straight into the server
translator so the two ends of the contract can never drift apart.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "cli" / "sediment_cli" / "transcript.py"
CHECKOUT_SHIM = Path(__file__).parent.parent / "sediment_transcript.py"
TRANSCRIPT_FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"

from sediment_core import AgentHarness  # noqa: E402


def _load_module():
    spec = importlib.util.spec_from_file_location("sediment_transcript", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def test_rejected_configured_endpoint_reports_credential_safe_diagnostic(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("SEDIMENT_OTLP_ENDPOINT", raising=False)
    assert mod._endpoint() is None
    assert capsys.readouterr().err == ""

    configured = "http://ingest.example.com"
    token = "secret-token-must-not-leak"
    monkeypatch.setenv("SEDIMENT_OTLP_ENDPOINT", configured)
    monkeypatch.setenv("SEDIMENT_INGEST_TOKEN", token)

    assert mod._endpoint() is None
    diagnostic = capsys.readouterr().err
    assert diagnostic.count("configured ingest endpoint rejected") == 1
    assert configured not in diagnostic
    assert token not in diagnostic


def test_authenticated_transport_requires_https_except_literal_loopback(
    monkeypatch,
) -> None:
    cases = {
        "https://ingest.example.com": "https://ingest.example.com/v1/logs",
        "http://localhost:8000": "http://localhost:8000/v1/logs",
        "http://127.42.0.1:8000": "http://127.42.0.1:8000/v1/logs",
        "http://[::1]:8000": "http://[::1]:8000/v1/logs",
        " \thttp://localhost:8000/v1/logs/ \n": "http://localhost:8000/v1/logs",
        "http://ingest.example.com": None,
        "http://192.168.1.10:8000": None,
        "http://localhost:8000/v1/logs?token=private": None,
        # Assemble the synthetic credential URL so secret scans stay meaningful.
        "http://user:" + "private@localhost:8000/v1/logs": None,
        "http://local\nhost:8000/v1/logs": None,
    }
    for configured, expected in cases.items():
        monkeypatch.setenv("SEDIMENT_OTLP_ENDPOINT", configured)
        assert mod._endpoint() == expected


def test_authenticated_transport_rejects_redirect_without_second_request(
    monkeypatch,
) -> None:
    requests: list[tuple[str, str | None]] = []

    class RedirectingCapture(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            requests.append((self.path, self.headers.get("Authorization")))
            if self.path == "/v1/logs":
                self.send_response(302)
                self.send_header("Location", "/redirect-target")
            else:
                self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), RedirectingCapture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SEDIMENT_INGEST_TOKEN", "secret-token")
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/logs"
    monkeypatch.setenv("SEDIMENT_OTLP_ENDPOINT", endpoint)
    try:
        with pytest.raises(RuntimeError, match="redirect_rejected"):
            mod._post(endpoint, {})
        assert requests == [("/v1/logs", "Bearer secret-token")]
    finally:
        server.shutdown()
        server.server_close()


def _assistant_edit(tool_use_id, file_path, text, *, name="Edit", ts=None):
    field = "new_string" if name == "Edit" else "content"
    return {
        "type": "assistant",
        "timestamp": ts or "2026-07-24T01:00:00.000Z",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": name,
                    "input": {"file_path": file_path, field: text},
                }
            ]
        },
    }


def _tool_result(tool_use_id, *, is_error=False):
    return {
        "type": "user",
        "timestamp": "2026-07-24T01:00:01.000Z",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": is_error,
                }
            ]
        },
    }


def _write_transcript(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\njunk not json\n",
        encoding="utf-8",
    )
    return path


def _codex_session(session_id: str, cwd: Path) -> dict:
    return {
        "type": "session_meta",
        "payload": {"id": session_id, "cwd": str(cwd)},
    }


def _codex_patch(
    call_id: str,
    changes: dict,
    *,
    success: bool = True,
) -> dict:
    return {
        "timestamp": "2026-08-28T21:35:56.000Z",
        "type": "event_msg",
        "payload": {
            "type": "patch_apply_end",
            "call_id": call_id,
            "success": success,
            "status": "completed" if success else "failed",
            "changes": changes,
        },
    }


def _retry_fixture() -> list[dict]:
    return [
        json.loads(line)
        for line in (TRANSCRIPT_FIXTURES / "claude_permission_deny_retry.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def test_applied_edit_pairs_use_canonical_content_names(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("x = 2  # hand-tuned after the agent", encoding="utf-8")
    entries = [
        _assistant_edit("toolu-1", str(target), "x = 1"),
        _tool_result("toolu-1"),
    ]
    [pair] = mod.build_pairs(entries, agent="claude-code")
    assert pair["applied_text"] == "x = 1"
    assert pair["observed_file_text"] == "x = 2  # hand-tuned after the agent"
    assert "original" not in pair
    assert "final" not in pair
    assert pair["tool_use_id"] == "toolu-1"
    assert pair["time_unix_nano"] > 0


def test_applied_edit_wire_event_uses_canonical_name() -> None:
    assert mod.EVENT_NAME == "sediment.edit_observation"


def test_payload_emits_canonical_content_names() -> None:
    pair = {
        "tool_use_id": "toolu-1",
        "tool_name": "Edit",
        "file_path": "/repo/app.py",
        "time_unix_nano": 1,
        "applied_text": "canonical applied",
        "observed_file_text": "canonical observed",
    }
    payload = mod.build_payload("sess-1", [pair], agent="claude-code")
    [record] = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    attrs = {attribute["key"] for attribute in record["attributes"]}
    assert "applied_text" in attrs
    assert "observed_file_text" in attrs
    assert "original" not in attrs
    assert "final" not in attrs


def test_pair_and_payload_builders_require_agent() -> None:
    with pytest.raises(TypeError):
        mod.build_pairs([])
    with pytest.raises(TypeError):
        mod.build_payload("sess-1", [])


def test_rejected_and_resultless_edits_skipped(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("whatever", encoding="utf-8")
    entries = [
        _assistant_edit("toolu-rejected", str(target), "a"),
        _tool_result("toolu-rejected", is_error=True),
        _assistant_edit("toolu-interrupted", str(target), "b"),  # no result at all
    ]
    assert mod.build_pairs(entries, agent="claude-code") == []


def test_deleted_file_reads_as_empty_observed_file_text(tmp_path: Path) -> None:
    gone = tmp_path / "deleted.py"
    entries = [
        _assistant_edit("toolu-1", str(gone), "short-lived"),
        _tool_result("toolu-1"),
    ]
    [pair] = mod.build_pairs(entries, agent="claude-code")
    assert pair["observed_file_text"] == ""


def test_oversized_pair_dropped(tmp_path: Path) -> None:
    target = tmp_path / "big.py"
    target.write_text("small", encoding="utf-8")
    entries = [
        _assistant_edit("toolu-1", str(target), "x" * (mod.MAX_TEXT_BYTES + 1)),
        _tool_result("toolu-1"),
    ]
    assert mod.build_pairs(entries, agent="claude-code") == []


def test_write_tool_uses_content_field(tmp_path: Path) -> None:
    target = tmp_path / "new.py"
    target.write_text("print('hi')", encoding="utf-8")
    entries = [
        _assistant_edit("toolu-1", str(target), "print('hi')", name="Write"),
        _tool_result("toolu-1"),
    ]
    [pair] = mod.build_pairs(entries, agent="claude-code")
    assert pair["tool_name"] == "Write"
    assert pair["applied_text"] == "print('hi')"


def test_foreign_session_lines_skipped(tmp_path: Path) -> None:
    # A resumed session's transcript embeds the prior session's history,
    # each line stamped with its own sessionId. Only the current session's
    # edits (and sid-less lines, for robustness) may ship — re-emitting the
    # prior session's would duplicate them under the wrong session id.
    target = tmp_path / "app.py"
    target.write_text("final", encoding="utf-8")
    entries = [
        {**_assistant_edit("toolu-old", str(target), "old"), "sessionId": "sess-old"},
        {**_tool_result("toolu-old"), "sessionId": "sess-old"},
        {**_assistant_edit("toolu-own", str(target), "own"), "sessionId": "sess-own"},
        {**_tool_result("toolu-own"), "sessionId": "sess-own"},
        _assistant_edit("toolu-unstamped", str(target), "unstamped"),
        _tool_result("toolu-unstamped"),
    ]
    pairs = mod.build_pairs(entries, "sess-own", agent="claude-code")
    assert [p["tool_use_id"] for p in pairs] == ["toolu-own", "toolu-unstamped"]
    # Without a session filter (unit callers), everything still extracts.
    assert len(mod.build_pairs(entries, agent="claude-code")) == 3


def test_non_edit_tools_and_junk_ignored(tmp_path: Path) -> None:
    entries = [
        {
            "type": "assistant",
            "timestamp": "2026-07-24T01:00:00Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-read",
                        "name": "Read",
                        "input": {"file_path": "/etc/passwd"},
                    },
                    {"type": "text", "text": "some reply"},
                ]
            },
        },
        _tool_result("toolu-read"),
        {"type": "summary", "summary": "compacted"},
    ]
    assert mod.build_pairs(entries, agent="claude-code") == []


def test_payload_cross_pins_with_server_translator(tmp_path: Path) -> None:
    # The client's own payload must translate into an EditObservation fact — the
    # wire contract test between scripts/ and packages/capture.
    from sediment_capture import parse_otlp_edit_observations

    target = tmp_path / "app.py"
    target.write_text("final text", encoding="utf-8")
    entries = [
        _assistant_edit("toolu-1", str(target), "original text"),
        _tool_result("toolu-1"),
    ]
    payload = mod.build_payload(
        "sess-42",
        mod.build_pairs(entries, agent="claude-code"),
        agent="claude-code",
    )
    [outcome] = parse_otlp_edit_observations(payload, org_id="acme")
    assert outcome.session_id == "sess-42"
    assert outcome.call_id == "toolu-1"
    assert outcome.file_path == str(target)
    assert (outcome.applied_text, outcome.observed_file_text) == (
        "original text",
        "final text",
    )
    assert outcome.occurred_at.year == 2026


def test_codex_patch_pairs_added_text_with_session_end_file(tmp_path: Path) -> None:
    target = tmp_path / "addition.py"
    target.write_text(
        "def add(left, right):\n    return left + right\n", encoding="utf-8"
    )
    entries = [
        _codex_session("sess-codex", tmp_path),
        _codex_patch(
            "call-patch",
            {
                str(target): {
                    "type": "update",
                    "move_path": None,
                    "unified_diff": (
                        "@@ -1,2 +1,2 @@\n"
                        " def add(left, right):\n"
                        "-    pass\n"
                        "+    return left + right\n"
                    ),
                }
            },
        ),
    ]

    [pair] = mod.build_pairs(entries, "sess-codex", agent="codex")

    assert pair["tool_use_id"] == "call-patch"
    assert pair["tool_name"] == "apply_patch"
    assert pair["file_path"] == str(target)
    assert pair["applied_text"] == "    return left + right"
    assert pair["observed_file_text"] == target.read_text(encoding="utf-8")
    assert "pass" not in pair["applied_text"]


def test_codex_shell_apply_patch_pairs_added_text_with_session_end_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "addition.py"
    target.write_text(
        "def add(left, right):\n    return left + right\n", encoding="utf-8"
    )
    command = (
        "apply_patch <<'PATCH'\n"
        "*** Begin Patch\n"
        "*** Update File: addition.py\n"
        "@@\n"
        " def add(left, right):\n"
        "-    return left - right\n"
        "+    return left + right\n"
        "*** End Patch\n"
        "PATCH"
    )
    entries = [
        _codex_session("sess-codex", tmp_path),
        {
            "timestamp": "2026-08-28T22:12:36.898Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": command}),
                "call_id": "call-shell-patch",
            },
        },
        {
            "timestamp": "2026-08-28T22:12:37.084Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-shell-patch",
                "output": (
                    "Exit code: 0\n"
                    "Wall time: 0.1 seconds\nOutput:\n"
                    "Success. Updated the following files:\nM addition.py\n"
                ),
            },
        },
    ]

    [pair] = mod.build_pairs(entries, "sess-codex", agent="codex")

    assert pair["tool_use_id"] == "call-shell-patch"
    assert pair["tool_name"] == "exec_command"
    assert pair["file_path"] == str(target)
    assert pair["applied_text"] == "    return left + right"
    assert pair["observed_file_text"] == target.read_text(encoding="utf-8")
    assert "return left - right" not in json.dumps(pair)


def test_codex_patch_requires_matching_session_success_and_one_file(
    tmp_path: Path, capsys
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("first = True\n", encoding="utf-8")
    second.write_text("second = True\n", encoding="utf-8")
    change = {
        "type": "add",
        "content": "value = True\n",
    }

    assert (
        mod.build_pairs(
            [
                _codex_session("another-session", tmp_path),
                _codex_patch("call-wrong-session", {str(first): change}),
            ],
            "sess-codex",
            agent="codex",
        )
        == []
    )
    assert (
        mod.build_pairs(
            [
                _codex_session("sess-codex", tmp_path),
                _codex_patch("call-failed", {str(first): change}, success=False),
            ],
            "sess-codex",
            agent="codex",
        )
        == []
    )
    assert (
        mod.build_pairs(
            [
                _codex_session("sess-codex", tmp_path),
                _codex_patch("call-multi", {str(first): change, str(second): change}),
            ],
            "sess-codex",
            agent="codex",
        )
        == []
    )
    assert "multi-file patch" in capsys.readouterr().err
    assert (
        mod.build_pairs(
            [
                _codex_session("sess-codex", tmp_path),
                _codex_patch(
                    "call-no-diff",
                    {str(first): {"type": "delete", "move_path": None}},
                ),
            ],
            "sess-codex",
            agent="codex",
        )
        == []
    )
    assert "unsupported_patch_kind" in capsys.readouterr().err


def test_codex_shell_patch_predicate_matches_the_decision_translator() -> None:
    # The transcript parser and the server-side decision translator must accept
    # the same shell commands, or a decision Fact appears with no matching
    # Edit observation. Leading whitespace is fine; a non-heredoc mention of
    # apply_patch is not an edit.
    heredoc = (
        "\n  apply_patch <<'PATCH'\n"
        "*** Begin Patch\n"
        "*** Update File: calc.py\n"
        "@@\n"
        "+value = True\n"
        "*** End Patch\n"
        "PATCH"
    )
    assert mod._codex_shell_patch(heredoc) == ("calc.py", "value = True")
    assert mod._codex_shell_patch("echo apply_patch <<'PATCH'") is None
    assert (
        mod._codex_shell_patch(
            "apply_patch *** Begin Patch\n*** Update File: calc.py\n*** End Patch\n"
        )
        is None
    )


def test_payload_privacy_contract(tmp_path: Path) -> None:
    # Normative: only the documented attribute keys ever ship — no prompts,
    # no conversation, no tool results, no raw transcript.
    target = tmp_path / "app.py"
    target.write_text("final", encoding="utf-8")
    entries = [
        {"type": "user", "timestamp": "t", "message": {"content": "secret prompt"}},
        _assistant_edit("toolu-1", str(target), "original"),
        _tool_result("toolu-1"),
    ]
    payload = mod.build_payload(
        "sess-1",
        mod.build_pairs(entries, agent="claude-code"),
        agent="claude-code",
    )
    [record] = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    assert {a["key"] for a in record["attributes"]} == {
        "session.id",
        "tool_use_id",
        "tool_name",
        "file_path",
        "applied_text",
        "observed_file_text",
        "agent",  # harness identity — an enum value, never content
    }
    assert "secret prompt" not in json.dumps(payload)


class _Capture(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict, dict]] = []
    bodies: list[bytes] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Capture.bodies.append(body)
        _Capture.requests.append((self.path, dict(self.headers), json.loads(body)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def capture_server():
    """A live local collector; yields the port the hook should POST to."""
    _Capture.requests.clear()
    _Capture.bodies.clear()
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()


def _run_hook(payload: dict, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_checkout_shim_runs_session_end_and_snapshot(
    tmp_path: Path, capture_server
) -> None:
    target = tmp_path / "app.py"
    target.write_text("survived", encoding="utf-8")
    transcript = _write_transcript(
        tmp_path,
        [
            _assistant_edit("toolu-shim", str(target), "survived"),
            _tool_result("toolu-shim"),
        ],
    )
    cache = tmp_path / "cache"
    env = {
        "PATH": "/usr/bin:/bin",
        "SEDIMENT_OTLP_ENDPOINT": f"http://127.0.0.1:{capture_server}",
        "SEDIMENT_INGEST_TOKEN": "synthetic-token",
        "SEDIMENT_DELTA_CACHE": str(cache),
    }
    session_end = subprocess.run(
        [sys.executable, str(CHECKOUT_SHIM), "--agent", "claude-code"],
        input=json.dumps(
            {"session_id": "sess-shim", "transcript_path": str(transcript)}
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert session_end.returncode == 0, session_end.stderr
    [(path, _, _)] = _Capture.requests
    assert path == "/v1/logs"

    snapshot = subprocess.run(
        [
            sys.executable,
            str(CHECKOUT_SHIM),
            "snapshot",
            "--agent",
            "claude-code",
        ],
        input=json.dumps(
            {
                "session_id": "sess-shim",
                "tool_use_id": "toolu-snapshot",
                "tool_name": "Write",
                "tool_input": {"file_path": str(target), "content": "changed"},
            }
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert snapshot.returncode == 0, snapshot.stderr
    assert list(cache.rglob("*.json"))


def test_end_to_end_session_end_hook(tmp_path: Path, capture_server) -> None:
    target = tmp_path / "app.py"
    target.write_text("survived", encoding="utf-8")
    transcript = _write_transcript(
        tmp_path,
        [
            _assistant_edit("toolu-e2e", str(target), "as written by the agent"),
            _tool_result("toolu-e2e"),
        ],
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "SEDIMENT_OTLP_ENDPOINT": f"http://127.0.0.1:{capture_server}",
        "SEDIMENT_INGEST_TOKEN": "tok-123",
        "OTEL_RESOURCE_ATTRIBUTES": "team=x,user.id=developer-1",
    }
    result = _run_hook(
        {"session_id": "sess-e2e", "transcript_path": str(transcript)},
        env,
        "--agent",
        "claude-code",
    )
    assert result.returncode == 0, result.stderr
    [(path, headers, payload)] = _Capture.requests
    assert path == "/v1/logs"
    assert headers.get("Authorization") == "Bearer tok-123"
    assert headers.get("User-Agent") == "sediment-delivery/1"
    [record] = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    attrs = {a["key"]: a["value"]["stringValue"] for a in record["attributes"]}
    assert attrs["session.id"] == "sess-e2e"
    assert attrs["observed_file_text"] == "survived"
    resource = payload["resourceLogs"][0]["resource"]
    assert {"key": "user.id", "value": {"stringValue": "developer-1"}} in (
        resource["attributes"]
    )


def test_bearer_token_from_otlp_headers_env(tmp_path: Path, capture_server) -> None:
    target = tmp_path / "app.py"
    target.write_text("v", encoding="utf-8")
    transcript = _write_transcript(
        tmp_path,
        [_assistant_edit("toolu-1", str(target), "v"), _tool_result("toolu-1")],
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "SEDIMENT_OTLP_ENDPOINT": f"http://127.0.0.1:{capture_server}",
        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer%20tok-otel",
    }
    result = _run_hook(
        {"session_id": "s", "transcript_path": str(transcript)},
        env,
        "--agent",
        "claude-code",
    )
    assert result.returncode == 0, result.stderr
    [(_, headers, _)] = _Capture.requests
    assert headers.get("Authorization") == "Bearer tok-otel"


def test_generic_otel_endpoint_never_activates_the_hook(
    tmp_path: Path, capture_server
) -> None:
    # Normative (privacy): /v1/logs is the standard OTLP path, so any
    # collector in a machine's generic telemetry config would accept this
    # payload. Edit text pairs ship only to an explicit SEDIMENT_OTLP_ENDPOINT
    # — OTEL_EXPORTER_OTLP_ENDPOINT alone means NOT opted in.
    target = tmp_path / "app.py"
    target.write_text("v", encoding="utf-8")
    transcript = _write_transcript(
        tmp_path,
        [_assistant_edit("toolu-1", str(target), "v"), _tool_result("toolu-1")],
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{capture_server}",
    }
    result = _run_hook({"session_id": "s", "transcript_path": str(transcript)}, env)
    assert result.returncode == 0, result.stderr
    assert _Capture.requests == []


def test_unconfigured_or_broken_paths_always_exit_zero(tmp_path: Path) -> None:
    # No endpoint = not opted in: exit 0 before touching anything.
    result = _run_hook(
        {"session_id": "s", "transcript_path": "/nope"}, {"PATH": "/usr/bin:/bin"}
    )
    assert result.returncode == 0
    assert result.stdout == ""
    # Endpoint set but unreachable, transcript missing, stdin garbage — the
    # hook still must never fail the session end.
    env = {"PATH": "/usr/bin:/bin", "SEDIMENT_OTLP_ENDPOINT": "http://127.0.0.1:9"}
    assert (
        _run_hook(
            {"session_id": "s", "transcript_path": "/nope"},
            env,
            "--agent",
            "claude-code",
        ).returncode
        == 0
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--agent", "claude-code"],
        input="not json",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0


def _pi_header(session_id: str = "sess-pi") -> dict:
    return {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-08-05T01:00:00.000Z",
        "cwd": "/repo",
    }


def _pi_tool_call(call_id: str, name: str, arguments: dict, *, ts_ms=1782578510649):
    return {
        "type": "message",
        "id": f"e-{call_id}",
        "parentId": None,
        "timestamp": "2026-08-05T01:00:01.000Z",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": call_id,
                    "name": name,
                    "arguments": arguments,
                }
            ],
            "timestamp": ts_ms,
        },
    }


def _pi_tool_result(call_id: str, *, is_error=False, ts_ms=1782578510650):
    return {
        "type": "message",
        "id": f"r-{call_id}",
        "parentId": f"e-{call_id}",
        "timestamp": "2026-08-05T01:00:02.000Z",
        "message": {
            "role": "toolResult",
            "toolCallId": call_id,
            "toolName": "write",
            "content": [],
            "isError": is_error,
            "timestamp": ts_ms,
        },
    }


def test_pi_write_and_edit_pairs(tmp_path: Path) -> None:
    written = tmp_path / "new.py"
    written.write_text("print('final')", encoding="utf-8")
    edited = tmp_path / "app.py"
    edited.write_text("edited on disk", encoding="utf-8")
    entries = [
        _pi_header(),
        _pi_tool_call(
            "call-w", "write", {"path": str(written), "content": "print('hi')"}
        ),
        _pi_tool_result("call-w"),
        _pi_tool_call(
            "call-e",
            "edit",
            {"path": str(edited), "edits": [{"oldText": "x", "newText": "y"}]},
        ),
        _pi_tool_result("call-e"),
    ]
    pairs = mod.build_pairs(entries, "sess-pi", agent="pi")
    by_id = {p["tool_use_id"]: p for p in pairs}
    assert by_id["call-w"]["applied_text"] == "print('hi')"
    assert by_id["call-w"]["observed_file_text"] == "print('final')"
    assert by_id["call-w"]["tool_name"] == "write"
    assert by_id["call-e"]["applied_text"] == "y"
    assert by_id["call-e"]["time_unix_nano"] == 1782578510649_000_000


def test_pi_multi_span_edit_joins_new_texts(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("final", encoding="utf-8")
    entries = [
        _pi_header(),
        _pi_tool_call(
            "call-e",
            "edit",
            {
                "path": str(target),
                "edits": [
                    {"oldText": "a", "newText": "b"},
                    {"oldText": "c", "newText": "d"},
                ],
            },
        ),
        _pi_tool_result("call-e"),
    ]
    [pair] = mod.build_pairs(entries, "sess-pi", agent="pi")
    assert pair["applied_text"] == "b\nd"


def test_pi_error_and_resultless_calls_excluded(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("final", encoding="utf-8")
    entries = [
        _pi_header(),
        _pi_tool_call("call-err", "write", {"path": str(target), "content": "x"}),
        _pi_tool_result("call-err", is_error=True),
        _pi_tool_call("call-open", "write", {"path": str(target), "content": "y"}),
        _pi_tool_call("call-read", "read", {"path": "/etc/passwd"}),
        _pi_tool_result("call-read"),
    ]
    assert mod.build_pairs(entries, "sess-pi", agent="pi") == []


def test_pi_session_header_mismatch_ships_nothing(tmp_path: Path) -> None:
    # The shim pairs (session_id, session file) by construction; a mismatch
    # means the wrong file was handed over — emitting under a foreign session
    # id would corrupt the join, so nothing ships (ADR 0002).
    target = tmp_path / "app.py"
    target.write_text("final", encoding="utf-8")
    entries = [
        _pi_header("sess-other"),
        _pi_tool_call("call-w", "write", {"path": str(target), "content": "x"}),
        _pi_tool_result("call-w"),
    ]
    assert mod.build_pairs(entries, "sess-pi", agent="pi") == []


def test_payload_carries_agent_attribute(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("final", encoding="utf-8")
    entries = [
        _assistant_edit("toolu-1", str(target), "original"),
        _tool_result("toolu-1"),
    ]
    pairs = mod.build_pairs(entries, agent="claude-code")
    [record] = mod.build_payload("sess-1", pairs, agent="pi")["resourceLogs"][0][
        "scopeLogs"
    ][0]["logRecords"]
    attrs = {a["key"]: a["value"] for a in record["attributes"]}
    assert attrs["agent"] == {"stringValue": "pi"}
    [record] = mod.build_payload("sess-1", pairs, agent="claude-code")["resourceLogs"][
        0
    ]["scopeLogs"][0]["logRecords"]
    attrs = {a["key"]: a["value"] for a in record["attributes"]}
    assert attrs["agent"] == {"stringValue": "claude-code"}


def test_pi_payload_cross_pins_with_server_translator(tmp_path: Path) -> None:
    from sediment_capture import parse_otlp_edit_observations

    target = tmp_path / "app.py"
    target.write_text("final text", encoding="utf-8")
    entries = [
        _pi_header(),
        _pi_tool_call("call-1", "write", {"path": str(target), "content": "original"}),
        _pi_tool_result("call-1"),
    ]
    payload = mod.build_payload(
        "sess-pi", mod.build_pairs(entries, "sess-pi", agent="pi"), agent="pi"
    )
    [outcome] = parse_otlp_edit_observations(payload, org_id="acme")
    assert outcome.agent_harness is AgentHarness.PI
    assert outcome.session_id == "sess-pi"
    assert (outcome.applied_text, outcome.observed_file_text) == (
        "original",
        "final text",
    )


def test_unknown_agent_exits_zero_without_posting(capture_server) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "SEDIMENT_OTLP_ENDPOINT": f"http://127.0.0.1:{capture_server}",
    }
    proc = _run_hook(
        {"session_id": "s", "transcript_path": "/nope"}, env, "--agent", "bogus"
    )
    assert proc.returncode == 0
    assert _Capture.requests == []


def test_missing_agent_exits_zero_without_posting(
    tmp_path: Path, capture_server
) -> None:
    target = tmp_path / "app.py"
    target.write_text("observed", encoding="utf-8")
    transcript = _write_transcript(
        tmp_path,
        [
            _assistant_edit("toolu-1", str(target), "applied"),
            _tool_result("toolu-1"),
        ],
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "SEDIMENT_OTLP_ENDPOINT": f"http://127.0.0.1:{capture_server}",
    }
    proc = _run_hook({"session_id": "s", "transcript_path": str(transcript)}, env)
    assert proc.returncode == 0
    assert _Capture.requests == []


def test_end_to_end_pi_session(tmp_path: Path, capture_server) -> None:
    target = tmp_path / "app.py"
    target.write_text("survived", encoding="utf-8")
    session_file = _write_transcript(
        tmp_path,
        [
            _pi_header("sess-pi-e2e"),
            _pi_tool_call(
                "call-e2e", "write", {"path": str(target), "content": "as written"}
            ),
            _pi_tool_result("call-e2e"),
        ],
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "SEDIMENT_OTLP_ENDPOINT": f"http://127.0.0.1:{capture_server}",
        "SEDIMENT_INGEST_TOKEN": "synthetic-token",
    }
    proc = _run_hook(
        {"session_id": "sess-pi-e2e", "transcript_path": str(session_file)},
        env,
        "--agent",
        "pi",
    )
    assert proc.returncode == 0, proc.stderr
    [(_, _, payload)] = _Capture.requests
    [record] = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    attrs = {a["key"]: a["value"]["stringValue"] for a in record["attributes"]}
    assert attrs["agent"] == "pi"
    assert attrs["session.id"] == "sess-pi-e2e"
    assert attrs["applied_text"] == "as written"
    assert attrs["observed_file_text"] == "survived"


# External-delta snapshots. The PreToolUse hook is the only baseline that
# survives Claude Code's ~10 KiB cap on toolUseResult.originalFile, so these
# drive it the way the harness does: snapshot with the file in its pre-edit
# state, then let the edit (and whatever else) land on disk before session
# end.


@pytest.fixture(autouse=True)
def delta_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the snapshot cache into the test tree — no test may write to
    the developer's real one. Tests that read the cache take this path."""
    cache = tmp_path / "cache"
    monkeypatch.setenv("SEDIMENT_DELTA_CACHE", str(cache))
    for name in (
        "SEDIMENT_OTLP_ENDPOINT",
        "SEDIMENT_INGEST_TOKEN",
        "SEDIMENT_API_BEARER_TOKEN",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "SEDIMENT_DELIVERY_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    return cache


def _snapshot(session_id, call_id, file_path, **tool_input) -> None:
    mod.cmd_snapshot(
        {
            "session_id": session_id,
            "tool_use_id": call_id,
            "tool_name": tool_input.pop("tool_name", "Edit"),
            "tool_input": {"file_path": str(file_path), **tool_input},
        }
    )


def test_external_lines_counted_between_agent_edit_and_session_end(
    tmp_path: Path,
) -> None:
    target = tmp_path / "app.py"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-1", target, old_string="b", new_string="B")
    target.write_text("a\nB\nc\n", encoding="utf-8")  # the agent's edit lands
    target.write_text("a\nB\nc\nd\ne\n", encoding="utf-8")  # something else appends

    entries = [_assistant_edit("toolu-1", str(target), "B"), _tool_result("toolu-1")]
    [pair] = mod.build_pairs(entries, "sess-1", agent="claude-code")
    assert (pair["external_lines_added"], pair["external_lines_removed"]) == (2, 0)


def test_untouched_file_reports_zero_not_absent(tmp_path: Path) -> None:
    # Zero is a real observation — "nobody else touched it" — and must be
    # distinguishable from the no-window case below.
    target = tmp_path / "app.py"
    target.write_text("a\nb\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-1", target, old_string="b", new_string="B")
    target.write_text("a\nB\n", encoding="utf-8")

    entries = [_assistant_edit("toolu-1", str(target), "B"), _tool_result("toolu-1")]
    [pair] = mod.build_pairs(entries, "sess-1", agent="claude-code")
    assert (pair["external_lines_added"], pair["external_lines_removed"]) == (0, 0)


def test_no_snapshot_omits_counts_entirely(tmp_path: Path) -> None:
    # Hook not installed, or installed mid-session: absent, never zero.
    target = tmp_path / "app.py"
    target.write_text("a\nB\n", encoding="utf-8")
    entries = [_assistant_edit("toolu-1", str(target), "B"), _tool_result("toolu-1")]
    [pair] = mod.build_pairs(entries, "sess-1", agent="claude-code")
    assert "external_lines_added" not in pair
    assert "external_lines_removed" not in pair


def test_replacement_of_agent_lines_counts_both_sides(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("keep\nold\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-1", target, old_string="old", new_string="agent")
    target.write_text("keep\nagent\n", encoding="utf-8")
    target.write_text("keep\nhand\nwritten\n", encoding="utf-8")

    entries = [
        _assistant_edit("toolu-1", str(target), "agent"),
        _tool_result("toolu-1"),
    ]
    [pair] = mod.build_pairs(entries, "sess-1", agent="claude-code")
    assert (pair["external_lines_added"], pair["external_lines_removed"]) == (2, 1)


def test_second_agent_edit_closes_the_first_window(tmp_path: Path) -> None:
    # The agent's own later edit must not read as an external change: the
    # first window closes at the second edit's pre-state, not at session end.
    target = tmp_path / "app.py"
    target.write_text("a\nb\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-1", target, old_string="b", new_string="B")
    target.write_text("a\nB\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-2", target, old_string="B", new_string="C\nD")
    target.write_text("a\nC\nD\n", encoding="utf-8")

    entries = [
        _assistant_edit("toolu-1", str(target), "B"),
        _tool_result("toolu-1"),
        _assistant_edit("toolu-2", str(target), "C\nD", ts="2026-07-24T01:00:02.000Z"),
        _tool_result("toolu-2"),
    ]
    first, second = sorted(
        mod.build_pairs(entries, "sess-1", agent="claude-code"),
        key=lambda p: p["tool_use_id"],
    )
    assert (first["external_lines_added"], first["external_lines_removed"]) == (0, 0)
    assert (second["external_lines_added"], second["external_lines_removed"]) == (0, 0)


def test_unpredictable_edit_opens_no_window(tmp_path: Path) -> None:
    # A stale old_string means the call will not apply as described, so the
    # post state cannot be predicted — omitted, never guessed.
    target = tmp_path / "app.py"
    target.write_text("a\nb\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-1", target, old_string="not-in-the-file", new_string="B")
    entries = [_assistant_edit("toolu-1", str(target), "B"), _tool_result("toolu-1")]
    [pair] = mod.build_pairs(entries, "sess-1", agent="claude-code")
    assert "external_lines_added" not in pair


def _snap(call_id, ts, pre, post, file_path="app.py"):
    return {
        "call_id": call_id,
        "file_path": file_path,
        "ts": ts,
        "pre": mod._line_hashes(pre),
        "post": None if post is None else mod._line_hashes(post),
    }


def test_rejected_calls_are_transparent_to_the_window_chain() -> None:
    # A rejected call never touched the file, so it neither opens nor closes
    # a window. Letting it close one would end the applied edit's window
    # early and strand the change after it on a call that ships no pair.
    snapshots = [
        _snap("toolu-applied", 1, "a\nb\n", "a\nB\n"),
        _snap("toolu-rejected", 2, "a\nB\n", "a\nNOPE\n"),
    ]
    observed_file_states = {"app.py": "a\nB\nhand\n"}
    deltas = mod.external_deltas(snapshots, observed_file_states, {"toolu-applied"})
    assert deltas["toolu-applied"] == (1, 0)
    assert "toolu-rejected" not in deltas


def test_unpredictable_post_still_closes_the_previous_window() -> None:
    # post opens a window, pre closes one — so a call whose post could not be
    # predicted still bounds its predecessor.
    snapshots = [
        _snap("toolu-1", 1, "a\n", "a\nb\n"),
        _snap("toolu-2", 2, "a\nb\nsneaked\n", None),
    ]
    deltas = mod.external_deltas(
        snapshots, {"app.py": "whatever\n"}, {"toolu-1", "toolu-2"}
    )
    assert deltas["toolu-1"] == (1, 0)  # "sneaked", not the session-end state
    assert "toolu-2" not in deltas


def test_snapshot_cache_stores_no_file_content(tmp_path: Path, delta_cache) -> None:
    # Normative: the cache is local-only, but it still holds hashes, never
    # lines — the wire carries counts alone.
    target = tmp_path / "app.py"
    target.write_text("api_key = 'SUPER_SECRET_VALUE'\n", encoding="utf-8")
    _snapshot(
        "sess-1", "toolu-1", target, old_string="'SUPER_SECRET_VALUE'", new_string="X"
    )
    blob = "".join(p.read_text(encoding="utf-8") for p in delta_cache.rglob("*.json"))
    assert blob, "snapshot should have been written"
    assert "SUPER_SECRET_VALUE" not in blob
    assert "api_key" not in blob


def test_session_end_clears_the_cache(tmp_path: Path, delta_cache) -> None:
    target = tmp_path / "app.py"
    target.write_text("a\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-1", target, old_string="a", new_string="b")
    assert list(delta_cache.rglob("*.json"))
    mod._clear_cache("sess-1")
    assert not list(delta_cache.rglob("*.json"))


def test_counts_cross_pin_with_server_translator(tmp_path: Path) -> None:
    # The counts are the only non-string attribute the client emits; pin the
    # OTLP int encoding against the server translator, not just the shape.
    from sediment_capture import parse_otlp_edit_observations

    target = tmp_path / "app.py"
    target.write_text("a\nb\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-1", target, old_string="b", new_string="B")
    target.write_text("a\nB\n", encoding="utf-8")
    target.write_text("a\nB\nextra\n", encoding="utf-8")

    entries = [_assistant_edit("toolu-1", str(target), "B"), _tool_result("toolu-1")]
    payload = mod.build_payload(
        "sess-1",
        mod.build_pairs(entries, "sess-1", agent="claude-code"),
        agent="claude-code",
    )
    [outcome] = parse_otlp_edit_observations(payload, org_id="acme")
    assert outcome.external_lines_added == 1
    assert outcome.external_lines_removed == 0


def test_deleted_file_counts_the_whole_removal(tmp_path: Path) -> None:
    # observed_file_text="" is a real observation, not a gap — the boundary
    # that has bitten this repo before. An agent edit whose file is gone by
    # session end reports every line as externally removed.
    target = tmp_path / "app.py"
    target.write_text("a\nb\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-1", target, old_string="b", new_string="B")
    target.write_text("a\nB\n", encoding="utf-8")
    target.unlink()

    entries = [_assistant_edit("toolu-1", str(target), "B"), _tool_result("toolu-1")]
    [pair] = mod.build_pairs(entries, "sess-1", agent="claude-code")
    assert pair["observed_file_text"] == ""
    assert (pair["external_lines_added"], pair["external_lines_removed"]) == (0, 2)


def test_empty_file_snapshot_is_a_window_not_a_gap(tmp_path: Path) -> None:
    # A Write creating a file snapshots pre=[] — falsy, but a real state.
    target = tmp_path / "new.py"
    _snapshot("sess-1", "toolu-1", target, tool_name="Write", content="one\ntwo\n")
    target.write_text("one\ntwo\n", encoding="utf-8")

    entries = [
        _assistant_edit("toolu-1", str(target), "one\ntwo\n", name="Write"),
        _tool_result("toolu-1"),
    ]
    [pair] = mod.build_pairs(entries, "sess-1", agent="claude-code")
    assert (pair["external_lines_added"], pair["external_lines_removed"]) == (0, 0)


def test_cache_sweep_cannot_escape_its_own_root(tmp_path: Path, monkeypatch) -> None:
    # The stale-session sweep deletes directories. It must only ever reach
    # children of the cache root this hook created — pointing the override at
    # a home directory must not turn a sweep into deleting that home.
    home = tmp_path / "home"
    (home / "irreplaceable").mkdir(parents=True)
    (home / "irreplaceable" / "thesis.txt").write_text("years", encoding="utf-8")
    old = time.time() - 400 * 24 * 3600
    os.utime(home / "irreplaceable", (old, old))
    monkeypatch.setenv("SEDIMENT_DELTA_CACHE", str(home))

    target = tmp_path / "app.py"
    target.write_text("a\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-1", target, old_string="a", new_string="b")
    mod._clear_cache("sess-1")

    assert (home / "irreplaceable" / "thesis.txt").exists()
    # Our own dirs still live under a path we made, and got cleaned.
    assert not list((home / "sediment" / "deltas").glob("sess-1"))


def test_cache_sweep_reaps_stale_sessions(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("a\n", encoding="utf-8")
    _snapshot("sess-dead", "toolu-1", target, old_string="a", new_string="b")
    dead = mod._cache_dir("sess-dead")
    old = time.time() - 400 * 24 * 3600
    os.utime(dead, (old, old))

    _snapshot("sess-live", "toolu-2", target, old_string="a", new_string="c")
    mod._clear_cache("sess-live")
    assert not dead.exists()


def test_failed_post_keeps_the_snapshots(
    tmp_path: Path, monkeypatch, delta_cache, capsys
) -> None:
    # A dropped POST must not cost the counts: the next fire needs them.
    monkeypatch.setenv("SEDIMENT_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("SEDIMENT_INGEST_TOKEN", "synthetic-token")
    target = tmp_path / "app.py"
    target.write_text("a\nb\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-1", target, old_string="b", new_string="B")
    target.write_text("a\nB\nhand\n", encoding="utf-8")
    transcript = _write_transcript(
        tmp_path,
        [_assistant_edit("toolu-1", str(target), "B"), _tool_result("toolu-1")],
    )

    payload = json.dumps({"session_id": "sess-1", "transcript_path": str(transcript)})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert mod.main(["--agent", "claude-code"]) == 0  # unreachable remains fail-soft
    assert list(delta_cache.rglob("*.json")), "snapshots must survive a failed POST"
    summary, diagnostic = _emission_summary(capsys)
    assert summary["delivery_mode"] == "best_effort"
    assert summary["outcome"] == "partial"
    assert summary["unsuccessful_requests"] == {"pending": 1}
    assert summary["unsubmitted_requests"] == 0
    assert "snapshots cannot reconstruct" in diagnostic
    assert not (tmp_path / "delivery").exists()


def test_rejected_edit_does_not_hide_a_later_external_change(tmp_path: Path) -> None:
    # Agent edits, proposes a second edit the human rejects, then the human
    # fixes the file by hand. That is the most diagnostic human-correction
    # pattern there is, and the applied edit's tail must carry it — the
    # rejected call ships no pair, so a window ending at its pre-state would
    # drop the change and report a confident (0, 0).
    target = tmp_path / "app.py"
    target.write_text("a\nb\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-applied", target, old_string="b", new_string="B")
    target.write_text("a\nB\n", encoding="utf-8")  # applied
    _snapshot("sess-1", "toolu-rejected", target, old_string="B", new_string="NOPE")
    # rejected: the file is untouched by it
    target.write_text("a\nB\nhand-written\n", encoding="utf-8")  # human fixes it

    entries = [
        _assistant_edit("toolu-applied", str(target), "B"),
        _tool_result("toolu-applied"),
        _assistant_edit(
            "toolu-rejected", str(target), "NOPE", ts="2026-07-24T01:00:02.000Z"
        ),
        _tool_result("toolu-rejected", is_error=True),
    ]
    [pair] = mod.build_pairs(entries, "sess-1", agent="claude-code")
    assert pair["tool_use_id"] == "toolu-applied"
    assert (pair["external_lines_added"], pair["external_lines_removed"]) == (1, 0)


def test_dropped_pair_costs_its_file_the_counts(tmp_path: Path) -> None:
    # An applied edit whose pair is dropped still consumed a window. The
    # survivors on that file must not look fully covered — the server sums
    # windows forward and would understate by an unknown amount.
    target = tmp_path / "app.py"
    target.write_text("a\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-small", target, old_string="a", new_string="A")
    target.write_text("A\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-big", target, old_string="A", new_string="B")
    target.write_text("B\n", encoding="utf-8")

    entries = [
        _assistant_edit("toolu-small", str(target), "A"),
        _tool_result("toolu-small"),
        # Oversized original: this pair drops, taking its window with it.
        _assistant_edit(
            "toolu-big",
            str(target),
            "x" * (mod.MAX_TEXT_BYTES + 1),
            ts="2026-07-24T01:00:02.000Z",
        ),
        _tool_result("toolu-big"),
    ]
    [pair] = mod.build_pairs(entries, "sess-1", agent="claude-code")
    assert pair["tool_use_id"] == "toolu-small"
    assert "external_lines_added" not in pair


def test_a_drop_on_one_file_leaves_other_files_covered(tmp_path: Path) -> None:
    good, bad = tmp_path / "good.py", tmp_path / "bad.py"
    good.write_text("a\n", encoding="utf-8")
    bad.write_text("z\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-good", good, old_string="a", new_string="A")
    good.write_text("A\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-bad", bad, old_string="z", new_string="Z")
    bad.write_text("Z\n", encoding="utf-8")

    entries = [
        _assistant_edit("toolu-good", str(good), "A"),
        _tool_result("toolu-good"),
        _assistant_edit("toolu-bad", str(bad), "x" * (mod.MAX_TEXT_BYTES + 1)),
        _tool_result("toolu-bad"),
    ]
    [pair] = mod.build_pairs(entries, "sess-1", agent="claude-code")
    assert pair["tool_use_id"] == "toolu-good"
    assert (pair["external_lines_added"], pair["external_lines_removed"]) == (0, 0)


def test_missing_snapshot_never_blames_the_agents_own_next_edit(tmp_path: Path) -> None:
    # The false-positive this whole measurement must never produce. Edit 1 is
    # snapshotted; edit 2 is applied but its snapshot is missing (hook
    # installed mid-session, unreadable file, failed write). Without the
    # whole-timeline check, edit 1's window would close against session-end
    # content and report edit 2 — the agent's own work — as external.
    target = tmp_path / "app.py"
    target.write_text("a\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-1", target, old_string="a", new_string="A")
    target.write_text("A\n", encoding="utf-8")
    # edit 2 applies with NO snapshot recorded
    target.write_text("A\nagent-second-edit\n", encoding="utf-8")

    entries = [
        _assistant_edit("toolu-1", str(target), "A"),
        _tool_result("toolu-1"),
        _assistant_edit(
            "toolu-2",
            str(target),
            "agent-second-edit",
            ts="2026-07-24T01:00:02.000Z",
        ),
        _tool_result("toolu-2"),
    ]
    pairs = mod.build_pairs(entries, "sess-1", agent="claude-code")
    assert len(pairs) == 2
    for pair in pairs:
        assert "external_lines_added" not in pair, pair["tool_use_id"]


def test_unpredictable_post_costs_its_file_the_counts(tmp_path: Path) -> None:
    # An applied edit with no usable window is a hole like any other.
    target = tmp_path / "app.py"
    target.write_text("a\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-1", target, old_string="a", new_string="A")
    target.write_text("A\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-2", target, old_string="gone-stale", new_string="B")
    target.write_text("A\nB\n", encoding="utf-8")

    entries = [
        _assistant_edit("toolu-1", str(target), "A"),
        _tool_result("toolu-1"),
        _assistant_edit("toolu-2", str(target), "B", ts="2026-07-24T01:00:02.000Z"),
        _tool_result("toolu-2"),
    ]
    for pair in mod.build_pairs(entries, "sess-1", agent="claude-code"):
        assert "external_lines_added" not in pair, pair["tool_use_id"]


def test_dotted_session_ids_never_become_a_cache_path(tmp_path: Path) -> None:
    # The session id becomes a directory that _clear_cache deletes. "." and
    # ".." satisfy the charset but are not children of anything, so a
    # malformed payload could walk out of the root and take a sibling
    # session's snapshots with it.
    for hostile in (".", "..", "..."):
        assert mod._cache_dir(hostile) is None, hostile

    target = tmp_path / "app.py"
    target.write_text("a\n", encoding="utf-8")
    _snapshot("innocent-session", "toolu-1", target, old_string="a", new_string="b")
    survivor = mod._cache_dir("innocent-session")
    assert survivor.exists()

    mod._clear_cache("..")  # must be a no-op, not a rampage
    assert survivor.exists()
    # And the hostile id writes nothing in the first place.
    _snapshot("..", "toolu-2", target, old_string="a", new_string="c")
    assert not (mod._cache_root().parent / "toolu-2.json").exists()


def _raise_home() -> Path:
    raise RuntimeError("Could not determine home directory.")


def test_cache_no_ops_when_no_base_resolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No override, no XDG dir, and no passwd entry for the uid: the cache
    # must no-op rather than raise and take the whole emit down with it.
    monkeypatch.delenv("SEDIMENT_DELTA_CACHE", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(mod.Path, "home", staticmethod(_raise_home))
    assert mod._cache_root() is None
    assert mod._cache_dir("sess-1") is None
    target = tmp_path / "app.py"
    target.write_text("a\n", encoding="utf-8")
    _snapshot("sess-1", "toolu-1", target, old_string="a", new_string="b")
    assert mod._load_snapshots("sess-1") == []
    mod._clear_cache("sess-1")  # must not raise


def test_emit_posts_without_resolvable_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture_server
) -> None:
    monkeypatch.delenv("SEDIMENT_DELTA_CACHE", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(mod.Path, "home", staticmethod(_raise_home))
    monkeypatch.setenv("SEDIMENT_OTLP_ENDPOINT", f"http://127.0.0.1:{capture_server}")
    monkeypatch.setenv("SEDIMENT_INGEST_TOKEN", "synthetic-token")
    target = tmp_path / "app.py"
    target.write_text("final", encoding="utf-8")
    transcript = _write_transcript(
        tmp_path,
        [_assistant_edit("toolu-1", str(target), "orig"), _tool_result("toolu-1")],
    )
    payload = json.dumps({"session_id": "sess-1", "transcript_path": str(transcript)})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert mod.main(["--agent", "claude-code"]) == 0
    [(path, _, body)] = _Capture.requests
    assert path == "/v1/logs"
    [record] = body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    attrs = {a["key"]: a["value"]["stringValue"] for a in record["attributes"]}
    assert attrs["session.id"] == "sess-1"
    assert attrs["observed_file_text"] == "final"


# Rejected edits. The reject markers are copied from real transcripts: entry-level
# `toolUseResult` is the exact string, and the model-visible content opens
# with the phrase. Both were present on all 8 real rejects surveyed; each is
# accepted alone so neither is a single point of failure.


def _reject_result(tool_use_id, *, tool_use_result=True, phrase=True):
    entry = {
        "type": "user",
        "timestamp": "2026-07-24T01:00:01.000Z",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": True,
                    "content": (
                        "The user doesn't want to proceed with this tool use. "
                        "The tool use was rejected (eg. if it was a mistake)."
                        if phrase
                        else "<tool_use_error>something else</tool_use_error>"
                    ),
                }
            ]
        },
    }
    if tool_use_result:
        entry["toolUseResult"] = "User rejected tool use"
    return entry


def _error_result(tool_use_id, text):
    return {
        "type": "user",
        "timestamp": "2026-07-24T01:00:01.000Z",
        "toolUseResult": f"Error: {text}",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": True,
                    "content": f"<tool_use_error>{text}</tool_use_error>",
                }
            ]
        },
    }


def test_user_rejected_edit_yields_the_proposed_text() -> None:
    entries = [
        _assistant_edit("toolu-1", "/repo/app.py", "def better(): ..."),
        _reject_result("toolu-1"),
    ]
    [rejected] = mod.extract_rejected_edits(entries)
    assert rejected["tool_use_id"] == "toolu-1"
    assert rejected["proposed"] == "def better(): ..."
    assert rejected["file_path"] == "/repo/app.py"
    assert rejected["tool_name"] == "Edit"
    assert rejected["time_unix_nano"] > 0


def test_either_reject_marker_alone_is_enough() -> None:
    edit = _assistant_edit("toolu-1", "/repo/app.py", "proposed")
    only_result = [edit, _reject_result("toolu-1", phrase=False)]
    only_phrase = [edit, _reject_result("toolu-1", tool_use_result=False)]
    assert len(mod.extract_rejected_edits(only_result)) == 1
    assert len(mod.extract_rejected_edits(only_phrase)) == 1


def test_tool_errors_are_not_rejections() -> None:
    # The model's text was never judged by anyone, so it is not a preference
    # signal. These are the real Edit failure modes from the transcripts.
    for text in (
        "String to replace not found in file.",
        "File has not been read yet. Read it first before writing to it.",
        "File has been modified since read, either by the user or by a linter.",
    ):
        entries = [
            _assistant_edit("toolu-1", "/repo/app.py", "proposed"),
            _error_result("toolu-1", text),
        ]
        assert mod.extract_rejected_edits(entries) == [], text


def test_word_rejected_in_tool_output_is_not_a_rejection() -> None:
    # `git push` prints "! [rejected]"; source files contain the word. A
    # substring test turned 8 real rejects into 23 during this issue's survey.
    entries = [
        _assistant_edit("toolu-1", "/repo/app.py", "proposed"),
        _error_result("toolu-1", "To github.com:o/r.git\n ! [rejected]  main -> main"),
    ]
    assert mod.extract_rejected_edits(entries) == []


def test_applied_and_interrupted_edits_are_not_rejections(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("applied", encoding="utf-8")
    entries = [
        _assistant_edit("toolu-ok", str(target), "applied"),
        _tool_result("toolu-ok"),
        _assistant_edit("toolu-interrupted", str(target), "never resolved"),
    ]
    assert mod.extract_rejected_edits(entries) == []
    assert [
        p["tool_use_id"] for p in mod.build_pairs(entries, agent="claude-code")
    ] == ["toolu-ok"]


def test_rejected_write_uses_the_content_field() -> None:
    entries = [
        _assistant_edit("toolu-1", "/repo/new.py", "whole file", name="Write"),
        _reject_result("toolu-1"),
    ]
    [rejected] = mod.extract_rejected_edits(entries)
    assert rejected["tool_name"] == "Write"
    assert rejected["proposed"] == "whole file"


def test_oversized_rejected_edit_dropped() -> None:
    entries = [
        _assistant_edit("toolu-1", "/repo/app.py", "x" * (mod.MAX_TEXT_BYTES + 1)),
        _reject_result("toolu-1"),
    ]
    assert mod.extract_rejected_edits(entries) == []


def test_foreign_session_rejections_skipped() -> None:
    entries = [
        {
            **_assistant_edit("toolu-old", "/repo/a.py", "old"),
            "sessionId": "sess-old",
        },
        {**_reject_result("toolu-old"), "sessionId": "sess-old"},
        {
            **_assistant_edit("toolu-own", "/repo/a.py", "own"),
            "sessionId": "sess-own",
        },
        {**_reject_result("toolu-own"), "sessionId": "sess-own"},
    ]
    assert [
        r["tool_use_id"] for r in mod.extract_rejected_edits(entries, "sess-own")
    ] == ["toolu-own"]


def test_payload_carries_both_record_types(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("applied", encoding="utf-8")
    entries = [
        _assistant_edit("toolu-ok", str(target), "applied"),
        _tool_result("toolu-ok"),
        _assistant_edit("toolu-no", "/repo/other.py", "refused"),
        _reject_result("toolu-no"),
    ]
    payload = mod.build_payload(
        "sess-1",
        mod.build_pairs(entries, agent="claude-code"),
        agent="claude-code",
        rejected=mod.extract_rejected_edits(entries),
    )
    records = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    bodies = [r["body"]["stringValue"] for r in records]
    assert bodies == ["sediment.edit_observation", "sediment.rejected_edit"]
    attrs = {a["key"]: a["value"]["stringValue"] for a in records[1]["attributes"]}
    # A refused edit never reached the file, so no observed state ships.
    assert "observed_file_text" not in attrs
    assert attrs["proposed"] == "refused"
    assert set(attrs) == {
        "session.id",
        "tool_use_id",
        "tool_name",
        "file_path",
        "proposed",
        "agent",
    }


def test_rejected_payload_cross_pins_with_server_translator() -> None:
    # The two ends of the refusal contract, pinned against each other.
    from sediment_capture import parse_otlp_edit_observations, parse_otlp_rejected_edits

    entries = [
        _assistant_edit("toolu-no", "/repo/app.py", "def worse(): ..."),
        _reject_result("toolu-no"),
    ]
    payload = mod.build_payload(
        "sess-42",
        [],
        agent="claude-code",
        rejected=mod.extract_rejected_edits(entries),
    )
    [rejected] = parse_otlp_rejected_edits(payload, org_id="acme")
    assert rejected.session_id == "sess-42"
    assert rejected.call_id == "toolu-no"
    assert rejected.proposed == "def worse(): ..."
    assert rejected.file_path == "/repo/app.py"
    assert rejected.raw == {"tool_name": "Edit"}
    # A refusal is not an edit observation — no fabricated pair rides along.
    assert parse_otlp_edit_observations(payload, org_id="acme") == []


def test_permission_deny_then_retry_fixture_emits_linkage_identifiers_only() -> None:
    [linkage] = mod.extract_retry_linkages(
        _retry_fixture(), "6a33dc79-9a77-444d-afa8-6ad48d500916"
    )
    assert linkage == {
        "rejected_call_id": "toolu_rejected",
        "accepted_call_id": "toolu_accepted",
        "tool_name": "Edit",
        "file_path": "/repo/app.py",
        "time_unix_nano": 1786682856100000000,
    }

    payload = mod.build_payload(
        "6a33dc79-9a77-444d-afa8-6ad48d500916",
        [],
        agent="claude-code",
        linkages=[linkage],
    )
    [record] = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    attrs = {attribute["key"] for attribute in record["attributes"]}
    assert record["body"]["stringValue"] == "sediment.retry_linkage"
    assert attrs == {
        "session.id",
        "rejected_call_id",
        "accepted_call_id",
        "tool_name",
        "file_path",
        "agent",
    }
    assert "Use the bounded comparison instead." not in json.dumps(payload)
    assert "return rejected" not in json.dumps(payload)
    assert "return accepted" not in json.dumps(payload)


def test_retry_linkage_extraction_is_stable_under_shuffled_entries() -> None:
    entries = _retry_fixture()
    assert mod.extract_retry_linkages(entries) == mod.extract_retry_linkages(
        list(reversed(entries))
    )


def test_retry_linkage_excludes_tool_failure_and_missing_user_correction() -> None:
    entries = _retry_fixture()
    failed = json.loads(json.dumps(entries))
    failed[-1]["message"]["content"][0]["is_error"] = True
    assert mod.extract_retry_linkages(failed) == []

    no_correction = [entries[0], entries[1], entries[3], entries[4]]
    assert mod.extract_retry_linkages(no_correction) == []


def test_retry_linkage_excludes_cross_file_and_cross_session_attempts() -> None:
    entries = _retry_fixture()
    cross_file = json.loads(json.dumps(entries))
    cross_file[3]["message"]["content"][0]["input"]["file_path"] = "/repo/b.py"
    assert mod.extract_retry_linkages(cross_file) == []

    cross_session = json.loads(json.dumps(entries))
    cross_session[3]["sessionId"] = "other-session"
    cross_session[4]["sessionId"] = "other-session"
    assert mod.extract_retry_linkages(cross_session) == []
    assert mod.extract_retry_linkages(cross_session, entries[0]["sessionId"]) == []


@pytest.mark.parametrize(
    ("entry_index", "component"),
    [
        (0, "rejected call"),
        (1, "rejection outcome"),
        (2, "developer text"),
        (3, "accepted retry call"),
        (4, "accepted outcome"),
    ],
)
@pytest.mark.parametrize(
    "session_value",
    [None, "", "other-session"],
    ids=["missing", "empty", "mismatch"],
)
def test_retry_linkage_requires_explicit_matching_session_on_every_component(
    entry_index: int, component: str, session_value: str | None
) -> None:
    del component  # Gives each parametrized case a readable test id.
    entries = json.loads(json.dumps(_retry_fixture()))
    expected_session = entries[0]["sessionId"]
    if session_value is None:
        entries[entry_index].pop("sessionId")
    else:
        entries[entry_index]["sessionId"] = session_value

    assert mod.extract_retry_linkages(entries, expected_session) == []


def test_pure_regenerate_sequence_remains_excluded() -> None:
    entries = _retry_fixture()
    entries[1] = _tool_result("toolu_rejected")
    assert mod.extract_retry_linkages(entries) == []


def test_end_to_end_hook_ships_refusals_too(tmp_path: Path, capture_server) -> None:
    # Through the real subprocess, not the helpers: main() walks the
    # transcript twice (applied edits, then refusals), so the entries must be
    # materialised. Pass a generator instead and refusals silently become
    # zero — the one regression that would leave every other test green.
    target = tmp_path / "app.py"
    target.write_text("kept", encoding="utf-8")
    transcript = _write_transcript(
        tmp_path,
        [
            _assistant_edit("toolu-ok", str(target), "kept"),
            _tool_result("toolu-ok"),
            _assistant_edit("toolu-no", "/repo/refused.py", "def refused(): ..."),
            _reject_result("toolu-no"),
        ],
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "SEDIMENT_OTLP_ENDPOINT": f"http://127.0.0.1:{capture_server}",
        "SEDIMENT_INGEST_TOKEN": "synthetic-token",
        "SEDIMENT_DELTA_CACHE": str(tmp_path / "cache"),
    }
    result = _run_hook(
        {"session_id": "sess-mix", "transcript_path": str(transcript)},
        env,
        "--agent",
        "claude-code",
    )
    assert result.returncode == 0, result.stderr
    [(_, _, payload)] = _Capture.requests
    records = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    bodies = [r["body"]["stringValue"] for r in records]
    assert "sediment.edit_observation" in bodies
    assert "sediment.rejected_edit" in bodies, "refusals lost on the real path"
    refused = next(
        r for r in records if r["body"]["stringValue"] == "sediment.rejected_edit"
    )
    attrs = {a["key"]: a["value"]["stringValue"] for a in refused["attributes"]}
    assert attrs["proposed"] == "def refused(): ..."
    assert attrs["tool_use_id"] == "toolu-no"


def test_refusal_only_session_still_posts(tmp_path: Path, capture_server) -> None:
    # A session whose every edit was refused has no pairs at all. It must
    # still POST — the old `if not pairs: return 0` would have dropped it.
    transcript = _write_transcript(
        tmp_path,
        [
            _assistant_edit("toolu-no", "/repo/a.py", "refused"),
            _reject_result("toolu-no"),
        ],
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "SEDIMENT_OTLP_ENDPOINT": f"http://127.0.0.1:{capture_server}",
        "SEDIMENT_INGEST_TOKEN": "synthetic-token",
        "SEDIMENT_DELTA_CACHE": str(tmp_path / "cache"),
    }
    result = _run_hook(
        {"session_id": "sess-all-refused", "transcript_path": str(transcript)},
        env,
        "--agent",
        "claude-code",
    )
    assert result.returncode == 0, result.stderr
    assert _Capture.requests, "a refusal-only session must still ship"
    [(_, _, payload)] = _Capture.requests
    [record] = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    assert record["body"]["stringValue"] == "sediment.rejected_edit"


_CODEX_COMMAND = (
    "apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: counter.js\n@@\n"
    "-old(counter, delta, result);\n+++counter; update_total(counter, delta, result);\n"
    "*** End Patch\nPATCH"
)
_CODEX_OUTPUT = (
    "Chunk ID: abc\nWall time: 0.1000 seconds\nProcess exited with code 0\nOutput:\nok"
)
_CODEX_APPLIED = "++counter; update_total(counter, delta, result);"


def _codex_shell_entries(
    cwd, *, output=_CODEX_OUTPUT, command=_CODEX_COMMAND, **arguments
):
    return [
        _codex_session("sess-codex", cwd),
        {
            "timestamp": "2026-08-28T22:12:36.898Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "shell",
                "arguments": json.dumps({"cmd": command, **arguments}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "shell",
                "output": output,
            },
        },
    ]


def test_codex_native_variants_preserve_authored_increment_and_retention(tmp_path):
    from sediment_capture import parse_otlp_edit_observations
    from sediment_derive.survival_scoring import four_gram_containment

    source = (TRANSCRIPT_FIXTURES / "codex_native_file_changes.jsonl").read_text()
    entries = [
        json.loads(line)
        for line in source.replace("__ROOT__", str(tmp_path)).splitlines()
    ]
    (tmp_path / "counter.js").write_text(_CODEX_APPLIED + "\n")
    pairs = mod.build_pairs(entries, "sess-codex", agent="codex")
    assert len(pairs) == 2
    assert pairs[0]["applied_text"] == "old(counter, delta, result);\n"
    assert pairs[1]["applied_text"] == _CODEX_APPLIED
    facts = parse_otlp_edit_observations(
        mod.build_payload("sess-codex", pairs, agent="codex"), org_id="acme"
    )
    assert len(facts) == 2
    updated = next(fact for fact in facts if fact.call_id == "native-update")
    assert (
        four_gram_containment(updated.applied_text, updated.observed_file_text) == 1.0
    )


@pytest.mark.parametrize("workdir_kind", ["absolute", "relative", "absent"])
def test_codex_shell_uses_execution_directory_and_preserves_increment(
    tmp_path, workdir_kind
):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "counter.js"
    target.write_text(_CODEX_APPLIED)
    kwargs = (
        {}
        if workdir_kind == "absent"
        else {"workdir": str(project) if workdir_kind == "absolute" else "project"}
    )
    cwd = project if workdir_kind == "absent" else tmp_path
    entries = _codex_shell_entries(cwd, **kwargs)
    [pair] = mod.build_pairs(entries, "sess-codex", agent="codex")
    assert pair["file_path"] == str(target)
    assert pair["applied_text"] == pair["observed_file_text"] == _CODEX_APPLIED


@pytest.mark.parametrize("invalid", [None, "", 12, [], "missing", "not-directory"])
def test_codex_invalid_explicit_workdir_never_falls_back(tmp_path, invalid, capsys):
    (tmp_path / "counter.js").write_text(_CODEX_APPLIED)
    (tmp_path / "not-directory").write_text("file")
    entries = _codex_shell_entries(tmp_path, workdir=invalid)
    assert mod.build_pairs(entries, "sess-codex", agent="codex") == []
    assert capsys.readouterr().err.count("execution_directory_invalid") == 1


@pytest.mark.parametrize(
    "command", ["cd project && " + _CODEX_COMMAND, _CODEX_COMMAND + "\ncd project"]
)
def test_codex_directory_changing_command_declines_without_execution(
    tmp_path, command, capsys
):
    entries = _codex_shell_entries(tmp_path, command=command)
    assert mod.build_pairs(entries, "sess-codex", agent="codex") == []
    assert capsys.readouterr().err.count("execution_directory_unknown") == 1


@pytest.mark.parametrize(
    "output,reason",
    [
        ("Exit code: 0\nWall time: 0.1 seconds\nOutput:\nok", None),
        (_CODEX_OUTPUT, None),
        (_CODEX_OUTPUT.replace("code 0", "code 1"), "execution_failed"),
        ("Wall time: 0.1 seconds\nOutput:\nExit code: 0\n", "execution_status_unknown"),
        ("stdout\nExit code: 0\n", "execution_status_unknown"),
        (
            "Exit code: 0\nProcess exited with code 1\nWall time: 0.1 seconds\nOutput:\n",
            "execution_status_conflict",
        ),
        (
            "Exit code: false\nWall time: 0.1 seconds\nOutput:\n",
            "execution_status_unknown",
        ),
        ("", "execution_status_unknown"),
    ],
)
def test_codex_result_status_comes_only_from_anchored_metadata(
    tmp_path, output, reason, capsys
):
    (tmp_path / "counter.js").write_text(_CODEX_APPLIED)
    pairs = mod.build_pairs(
        _codex_shell_entries(tmp_path, output=output), "sess-codex", agent="codex"
    )
    if reason:
        assert pairs == []
        assert capsys.readouterr().err.count(reason) == 1
    else:
        assert len(pairs) == 1


@pytest.mark.parametrize(
    "kind", ["unreadable", "unresolved-parent", "dangling-link", "missing", "empty"]
)
def test_codex_observed_file_state_distinguishes_missing_from_unreadable(
    tmp_path, kind, capsys
):
    path = "counter.js"
    target = tmp_path / path
    if kind == "unreadable":
        target.write_bytes(b"\xff")
    elif kind == "unresolved-parent":
        path = "missing-parent/counter.js"
    elif kind == "dangling-link":
        target.symlink_to(tmp_path / "absent-target")
    elif kind == "empty":
        target.write_text("")
    entries = [
        _codex_session("sess-codex", tmp_path),
        _codex_patch("native", {path: {"type": "add", "content": _CODEX_APPLIED}}),
    ]
    pairs = mod.build_pairs(entries, "sess-codex", agent="codex")
    if kind in {"missing", "empty"}:
        assert len(pairs) == 1
        assert pairs[0]["observed_file_text"] == ""
    else:
        assert pairs == []
        assert capsys.readouterr().err.count("observation_unreadable") == 1


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"type": "delete"}, "unsupported_patch_kind"),
        (
            {"type": "add", "unified_diff": "@@ -0,0 +1 @@\n+invented\n"},
            "malformed_patch",
        ),
        ({"type": "update", "unified_diff": "+unframed\n"}, "malformed_patch"),
        (
            {"type": "update", "unified_diff": "@@ -1,2 +1,2 @@\n-old\n+new\n"},
            "malformed_patch",
        ),
    ],
)
def test_codex_native_patch_grammar_declines_unproved_proposals(
    tmp_path, change, reason, capsys
):
    entries = [
        _codex_session("sess-codex", tmp_path),
        _codex_patch("native", {"counter.js": change}),
    ]
    assert mod.build_pairs(entries, "sess-codex", agent="codex") == []
    assert capsys.readouterr().err.count(reason) == 1


def test_codex_missing_directory_is_unknown_but_explicit_absolute_workdir_suffices(
    tmp_path, capsys
):
    entries = _codex_shell_entries(tmp_path)
    del entries[0]["payload"]["cwd"]
    assert mod.build_pairs(entries, "sess-codex", agent="codex") == []
    assert capsys.readouterr().err.count("execution_directory_unknown") == 1
    (tmp_path / "counter.js").write_text(_CODEX_APPLIED)
    arguments = json.loads(entries[1]["payload"]["arguments"])
    arguments["workdir"] = str(tmp_path)
    entries[1]["payload"]["arguments"] = json.dumps(arguments)
    assert len(mod.build_pairs(entries, "sess-codex", agent="codex")) == 1


def test_codex_conflicting_result_records_decline_once(tmp_path, capsys):
    entries = _codex_shell_entries(tmp_path)
    entries.append(
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "shell",
                "output": _CODEX_OUTPUT.replace("code 0", "code 1"),
            },
        }
    )
    assert mod.build_pairs(entries, "sess-codex", agent="codex") == []
    assert capsys.readouterr().err.count("execution_status_conflict") == 1


def test_codex_stdout_cannot_override_nonzero_metadata(tmp_path, capsys):
    output = _CODEX_OUTPUT.replace("code 0", "code 1") + "\nExit code: 0\n"
    assert (
        mod.build_pairs(
            _codex_shell_entries(tmp_path, output=output), "sess-codex", agent="codex"
        )
        == []
    )
    assert capsys.readouterr().err.count("execution_failed") == 1


@pytest.mark.parametrize(
    "command",
    [
        _CODEX_COMMAND.replace("<<'PATCH'", "<<PATCH").replace(
            "+++counter", "+$counter"
        ),
        _CODEX_COMMAND + " ",
    ],
)
def test_codex_shell_grammar_declines_expansion_and_invalid_terminator(
    tmp_path, command
):
    assert (
        mod.build_pairs(
            _codex_shell_entries(tmp_path, command=command), "sess-codex", agent="codex"
        )
        == []
    )


def test_codex_oversized_hunk_count_declines_without_losing_valid_sibling(
    tmp_path, capsys
):
    bad = {
        "type": "update",
        "unified_diff": "@@ -1 +1," + "9" * 5000 + " @@\n-old\n+new\n",
    }
    good = {"type": "add", "content": "kept"}
    (tmp_path / "good.js").write_text("kept")
    entries = [
        _codex_session("sess-codex", tmp_path),
        _codex_patch("bad", {"bad.js": bad}),
        _codex_patch("good", {"good.js": good}),
    ]
    [pair] = mod.build_pairs(entries, "sess-codex", agent="codex")
    assert pair["tool_use_id"] == "good"
    assert capsys.readouterr().err.count("malformed_patch") == 1


def test_codex_deleted_increment_has_zero_retention(tmp_path):
    from sediment_derive.survival_scoring import four_gram_containment

    entries = [
        _codex_session("sess-codex", tmp_path),
        _codex_patch(
            "deleted-increment",
            {
                "counter.js": {
                    "type": "update",
                    "move_path": None,
                    "unified_diff": "@@ -1 +1 @@\n-old(counter);\n+"
                    + _CODEX_APPLIED
                    + "\n",
                }
            },
        ),
    ]
    [pair] = mod.build_pairs(entries, "sess-codex", agent="codex")
    assert pair["applied_text"] == _CODEX_APPLIED
    assert pair["observed_file_text"] == ""
    assert (
        four_gram_containment(pair["applied_text"], pair["observed_file_text"]) == 0.0
    )


@pytest.mark.parametrize("context", ["", "@@\n", "@@ function counter\n"])
@pytest.mark.parametrize("move", [False, True])
@pytest.mark.parametrize("end_of_file", [False, True])
def test_codex_shell_first_update_hunk_preserves_text_and_retention(
    tmp_path, context, move, end_of_file
):
    from sediment_capture import parse_otlp_edit_observations
    from sediment_derive.survival_scoring import four_gram_containment

    target = tmp_path / ("moved.js" if move else "counter.js")
    prefix = "function counter\n" if context.startswith("@@ ") else ""
    target.write_text(prefix + _CODEX_APPLIED + "\n")
    body = (
        ("*** Move to: moved.js\n" if move else "")
        + context
        + "-old(counter, delta, result);\n+"
        + _CODEX_APPLIED
        + "\n"
        + ("*** End of File\n" if end_of_file else "")
    )
    command = (
        "apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: counter.js\n"
        + body
        + "*** End Patch\nPATCH"
    )
    [pair] = mod.build_pairs(
        _codex_shell_entries(tmp_path, command=command), "sess-codex", agent="codex"
    )
    assert pair["file_path"] == str(target)
    assert pair["applied_text"] == _CODEX_APPLIED
    assert pair["observed_file_text"] == target.read_text()
    [fact] = parse_otlp_edit_observations(
        mod.build_payload("sess-codex", [pair], agent="codex"), org_id="acme"
    )
    assert four_gram_containment(fact.applied_text, fact.observed_file_text) == 1.0


@pytest.mark.parametrize(
    "body",
    [
        "",
        "*** End of File\n",
        "+valid\nunprefixed body\n",
        "+valid\n*** End of File\n+after end\n",
        "+valid\n*** Update File: sibling.js\n+other\n",
    ],
)
def test_codex_shell_headerless_hunk_keeps_malformed_body_guard(tmp_path, body, capsys):
    (tmp_path / "counter.js").write_text("valid\n")
    command = (
        "apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: counter.js\n"
        + body
        + "*** End Patch\nPATCH"
    )
    assert (
        mod.build_pairs(
            _codex_shell_entries(tmp_path, command=command), "sess-codex", agent="codex"
        )
        == []
    )
    reason = "unsupported_patch_kind" if "sibling.js" in body else "malformed_patch"
    assert capsys.readouterr().err.count(reason) == 1


def _large_transcript(tmp_path, count=32):
    target = tmp_path / "large.py"
    observed = "a" * (256 * 1024)
    target.write_text(observed)
    entries = []
    expected = []
    for index in range(count):
        call_id = f"large-{index}"
        entries.extend(
            [
                _assistant_edit(call_id, str(target), "X", ts="2026-07-24T01:00:00Z"),
                _tool_result(call_id),
            ]
        )
        expected.append(
            {
                "body": {"stringValue": "sediment.edit_observation"},
                "timeUnixNano": "1784854800000000000",
                "attributes": [
                    {"key": key, "value": {"stringValue": value}}
                    for key, value in {
                        "session.id": "sess-1",
                        "tool_use_id": call_id,
                        "tool_name": "Edit",
                        "file_path": str(target),
                        "applied_text": "X",
                        "observed_file_text": observed,
                        "agent": "claude-code",
                    }.items()
                ],
            }
        )
    return target, _write_transcript(tmp_path, entries), expected


def test_32_observation_hook_posts_complete_bounded_requests(tmp_path, capture_server):
    from sediment_cli.delivery import MAX_ENTRY_BYTES

    _, transcript, expected = _large_transcript(tmp_path)
    result = _run_hook(
        {"session_id": "sess-1", "transcript_path": str(transcript)},
        {
            "SEDIMENT_OTLP_ENDPOINT": f"http://127.0.0.1:{capture_server}",
            "SEDIMENT_INGEST_TOKEN": "synthetic-token",
            "OTEL_RESOURCE_ATTRIBUTES": "user.id=original-user",
            "SEDIMENT_DELTA_CACHE": str(tmp_path / "cache"),
        },
        "--agent",
        "claude-code",
    )
    assert result.returncode == 0, result.stderr
    assert len(_Capture.bodies) > 1, result.stderr
    assert all(len(body) <= MAX_ENTRY_BYTES for body in _Capture.bodies)
    decoded = [json.loads(body) for body in _Capture.bodies]
    assert [
        record
        for payload in decoded
        for record in payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    ] == expected
    assert all(
        payload["resourceLogs"][0]["resource"]
        == {
            "attributes": [
                {"key": "user.id", "value": {"stringValue": "original-user"}}
            ]
        }
        for payload in decoded
    )


@pytest.mark.parametrize("headroom", [0, -1])
def test_transcript_packing_uses_exact_encoded_boundary(monkeypatch, headroom):
    delivery = mod._delivery_client()
    records = [
        {"body": {"stringValue": "sediment.edit_observation"}, "text": text}
        for text in ['é雪 😀 "\\\n', "next"]
    ]
    payload = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [{"key": "user.id", "value": {"stringValue": "雪"}}]
                },
                "scopeLogs": [{"logRecords": records}],
            }
        ]
    }
    encoded = json.dumps(payload, allow_nan=False).encode()
    monkeypatch.setattr(delivery, "MAX_ENTRY_BYTES", len(encoded) + headroom)
    prepared, oversized = mod._prepare_requests("http://127.0.0.1:9", payload)
    assert oversized == 0
    assert len(prepared) == (1 if headroom == 0 else 2)
    assert all(len(request.body) <= delivery.MAX_ENTRY_BYTES for request, _ in prepared)
    assert [
        record
        for request, _ in prepared
        for record in json.loads(request.body)["resourceLogs"][0]["scopeLogs"][0][
            "logRecords"
        ]
    ] == records
    if headroom == 0:
        assert prepared[0][0].body == encoded


def test_transcript_packing_preserves_mixed_records_and_encodes_each_once(monkeypatch):
    pairs = [
        {
            "tool_use_id": "edit",
            "time_unix_nano": 1784854800000000001,
            "tool_name": "Edit",
            "file_path": "a.py",
            "applied_text": "é\\\n",
            "observed_file_text": '"kept"',
            "external_lines_added": 2,
            "external_lines_removed": 0,
        }
    ]
    rejected = [
        {
            "tool_use_id": "rejected",
            "time_unix_nano": 1784854800000000002,
            "tool_name": "Write",
            "file_path": "a.py",
            "proposed": "雪",
        }
    ]
    linkages = [
        {
            "rejected_call_id": "rejected",
            "accepted_call_id": "edit",
            "time_unix_nano": 1784854800000000003,
            "tool_name": "Write",
            "file_path": "a.py",
        }
    ]
    payload = mod.build_payload(
        "sess-1",
        pairs,
        agent="claude-code",
        rejected=rejected,
        linkages=linkages,
    )
    records = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    largest_single = max(
        len(
            json.dumps(
                {
                    "resourceLogs": [
                        {
                            "resource": payload["resourceLogs"][0]["resource"],
                            "scopeLogs": [{"logRecords": [record]}],
                        }
                    ]
                }
            ).encode()
        )
        for record in records
    )
    original_dumps = json.dumps
    encoded_records = []

    def count_encoding(value, *args, **kwargs):
        if any(value is record for record in records):
            encoded_records.append(value)
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(mod.json, "dumps", count_encoding)
    monkeypatch.setattr(mod._delivery_client(), "MAX_ENTRY_BYTES", largest_single)
    prepared, oversized = mod._prepare_requests("http://127.0.0.1:9", payload)
    assert oversized == 0
    assert len(prepared) == 3
    assert encoded_records == records
    assert [
        record
        for request, _ in prepared
        for record in json.loads(request.body)["resourceLogs"][0]["scopeLogs"][0][
            "logRecords"
        ]
    ] == records
    assert [count for _, count in prepared] == [1, 1, 1]


def _emission_summary(capsys):
    lines = capsys.readouterr().err.splitlines()
    [summary] = [
        line.split("emission_summary ", 1)[1]
        for line in lines
        if "emission_summary " in line
    ]
    return json.loads(summary), "\n".join(lines)


def _small_emission(tmp_path, monkeypatch, count=4):
    target = tmp_path / "source.py"
    target.write_text("before\n")
    _snapshot("sess-1", "small-0", target, old_string="before", new_string="after")
    target.write_text("after\n")
    transcript = _write_transcript(
        tmp_path,
        [
            entry
            for index in range(count)
            for entry in (
                _assistant_edit(f"small-{index}", str(target), "after"),
                _tool_result(f"small-{index}"),
            )
        ],
    )
    monkeypatch.setenv("SEDIMENT_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("SEDIMENT_DELIVERY_DIR", str(tmp_path / "delivery"))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "sess-1",
                    "transcript_path": str(transcript),
                }
            )
        ),
    )
    monkeypatch.setattr(mod._delivery_client(), "MAX_ENTRY_BYTES", 900)
    return target, transcript


def test_individually_oversized_record_keeps_valid_siblings_and_snapshots(
    tmp_path, monkeypatch, delta_cache, capsys
):
    target, transcript = _small_emission(tmp_path, monkeypatch, count=2)
    entries = [
        _assistant_edit("small-0", str(target), "after"),
        _tool_result("small-0"),
        _assistant_edit("metadata" * 500, str(target), "after"),
        _tool_result("metadata" * 500),
        _assistant_edit("small-1", str(target), "after"),
        _tool_result("small-1"),
    ]
    _write_transcript(tmp_path, entries)
    assert mod.main(["--agent", "claude-code"]) == 0
    summary, diagnostic = _emission_summary(capsys)
    assert summary["candidate_records"] == {
        "edit_observations": 3,
        "rejected_edits": 0,
        "retry_linkages": 0,
    }
    assert summary["record_too_large"] == 1
    assert summary["queued_requests"] == summary["queued_records"] == 2
    assert summary["unsubmitted_records"] == 0
    assert summary["outcome"] == "partial"
    assert "snapshots cannot reconstruct" in diagnostic
    assert list(delta_cache.rglob("*.json"))
    assert mod._delivery_client().status(tmp_path / "delivery")["pending"] == 2


@pytest.mark.parametrize(
    "status", ["pending", "blocked", "declined", "io_error", "publication_error"]
)
def test_partial_publication_counts_attempts_and_unsubmitted_suffix(
    tmp_path, monkeypatch, delta_cache, capsys, status
):
    _small_emission(tmp_path, monkeypatch)
    delivery = mod._delivery_client()
    actual_deliver = delivery.deliver
    attempted = []

    def deliver(request):
        attempted.append(request)
        if len(attempted) == 2:
            if status == "io_error":
                raise OSError("private-source-token")
            if status == "publication_error":
                raise RuntimeError("private-source-token")
            return delivery.Disposition(
                request.delivery_id, status, "transport_failure"
            )
        return actual_deliver(request)

    monkeypatch.setattr(delivery, "deliver", deliver)
    assert mod.main(["--agent", "claude-code"]) == 0
    summary, diagnostic = _emission_summary(capsys)
    interrupted = status in {"io_error", "publication_error"}
    expected_accepted = 1 if interrupted else 3
    expected_unsubmitted = 2 if interrupted else 0
    assert summary["prepared_requests"] == 4
    assert len(attempted) == 4 - expected_unsubmitted
    assert summary["queued_requests"] == summary["queued_records"] == expected_accepted
    assert summary["unsuccessful_requests"] == {status: 1}
    assert summary["unsuccessful_records"] == {status: 1}
    assert summary["unsubmitted_requests"] == expected_unsubmitted
    assert summary["unsubmitted_records"] == expected_unsubmitted
    assert summary["outcome"] == "partial"
    assert "private-source-token" not in diagnostic
    assert list(delta_cache.rglob("*.json"))
    assert delivery.status(tmp_path / "delivery")["pending"] == expected_accepted


def test_full_buffer_declines_chunks_without_losing_accepted_prefix(
    tmp_path, monkeypatch, delta_cache, capsys
):
    _small_emission(tmp_path, monkeypatch)
    delivery = mod._delivery_client()
    monkeypatch.setattr(delivery, "MAX_ACTIVE_ENTRIES", 2)
    assert mod.main(["--agent", "claude-code"]) == 0
    summary, _ = _emission_summary(capsys)
    assert summary["queued_requests"] == summary["queued_records"] == 2
    assert summary["unsuccessful_requests"] == {"declined": 2}
    assert summary["unsuccessful_records"] == {"declined": 2}
    assert summary["unsubmitted_requests"] == summary["unsubmitted_records"] == 0
    assert delivery.status(tmp_path / "delivery")["pending"] == 2
    assert list(delta_cache.rglob("*.json"))


def test_multirequest_process_replay_preserves_postgres_facts_after_lost_ack(
    tmp_path, delta_cache, postgres_store
):
    from sediment_capture import parse_otlp_logs
    from sediment_cli import delivery

    received = []
    stored_counts = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/logs"
            assert self.headers["Authorization"] == "Bearer synthetic-token"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            capture = parse_otlp_logs(json.loads(body), org_id="acme")
            stored_counts.append(
                sum(
                    postgres_store.store_edit_observation(fact)
                    for fact in capture.edit_observations
                )
            )
            first_delivery = body not in received
            received.append(body)
            if first_delivery:
                # PostgreSQL committed each Fact; the sender never receives its
                # acknowledgment and must retain the exact prepared request.
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        target, transcript, expected = _large_transcript(tmp_path)
        _snapshot("sess-1", "large-0", target, old_string="a", new_string="X")
        assert list(delta_cache.rglob("*.json"))
        directory = tmp_path / "delivery"
        env = {
            **os.environ,
            "SEDIMENT_OTLP_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
            "SEDIMENT_INGEST_TOKEN": "synthetic-token",
            "SEDIMENT_DELIVERY_DIR": str(directory),
            "SEDIMENT_DELTA_CACHE": str(delta_cache),
        }
        result = _run_hook(
            {"session_id": "sess-1", "transcript_path": str(transcript)},
            env,
            "--agent",
            "claude-code",
        )
        assert result.returncode == 0, result.stderr
        entries = [
            path.read_bytes().split(b"\n", 1) for path in directory.glob("*.entry")
        ]
        entries.sort(key=lambda entry: json.loads(entry[0])["enqueued_at"])
        originals = [body for _, body in entries]
        assert len(originals) == 2
        assert received == []
        assert not list(delta_cache.rglob("*.json"))
        assert [
            record
            for body in originals
            for record in json.loads(body)["resourceLogs"][0]["scopeLogs"][0][
                "logRecords"
            ]
        ] == expected
        target.write_text("changed after enqueue")
        transcript.unlink()

        def replay():
            result = subprocess.run(
                [sys.executable, delivery.__file__, "replay"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        first = replay()
        assert first["attempted"] == first["pending"] == 2
        assert received == originals
        retained = postgres_store.read_edit_observations("acme")
        assert len(retained) == 32
        assert {fact.call_id for fact in retained} == {
            f"large-{index}" for index in range(32)
        }
        assert all(
            fact.session_id == "sess-1"
            and fact.file_path == str(target)
            and fact.applied_text == "X"
            and fact.observed_file_text == "a" * (256 * 1024)
            and fact.occurred_at.isoformat() == "2026-07-24T01:00:00+00:00"
            for fact in retained
        )
        assert sum(stored_counts) == 32
        # Make retry scheduling due explicitly; don't sleep or change payloads.
        for path in directory.glob("*.state"):
            state = json.loads(path.read_bytes())
            state["next_attempt"] = 0
            path.write_text(json.dumps(state))
        second = replay()
        assert second["attempted"] == second["acknowledged"] == 2
        assert sorted(received[2:]) == sorted(originals)
        assert stored_counts[2:] == [0, 0]
        assert postgres_store.read_edit_observations("acme") == retained
        assert delivery.status(directory)["pending"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_buffered_transcript_replays_original_observation(
    tmp_path, monkeypatch, delta_cache, capture_server
):
    endpoint = f"http://127.0.0.1:{capture_server}"
    directory = tmp_path / "delivery"
    monkeypatch.setenv("SEDIMENT_DELIVERY_DIR", str(directory))
    monkeypatch.setenv("SEDIMENT_OTLP_ENDPOINT", endpoint)
    monkeypatch.setenv("SEDIMENT_INGEST_TOKEN", "token")
    target = tmp_path / "source.py"
    target.write_text("before\n")
    _snapshot("sess-1", "toolu-1", target, old_string="before", new_string="captured")
    target.write_text("captured\n")
    transcript = _write_transcript(
        tmp_path,
        [_assistant_edit("toolu-1", str(target), "captured"), _tool_result("toolu-1")],
    )
    hook = {"session_id": "sess-1", "transcript_path": str(transcript)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(hook)))
    assert mod.main(["--agent", "claude-code"]) == 0
    assert directory.exists(), "prepared observation was not retained"
    assert _Capture.requests == [], "buffered capture attempted HTTP before replay"
    assert not list(delta_cache.rglob("*.json")), (
        "durable acceptance must clear snapshots"
    )
    target.write_text("edited after original observation\n")
    transcript.unlink()  # replay cannot depend on either mutable source
    from sediment_cli import delivery

    result = subprocess.run(
        [sys.executable, delivery.__file__, "replay"],
        env={**os.environ, "SEDIMENT_DELIVERY_DIR": str(directory)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["acknowledged"] == 1
    [(_, _, payload)] = _Capture.requests
    [record] = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    values = {a["key"]: a["value"].get("stringValue") for a in record["attributes"]}
    assert values["observed_file_text"] == "captured\n"
    assert values["tool_use_id"] == "toolu-1"
    assert values["session.id"] == "sess-1"
    assert record["timeUnixNano"] == "1784854800000000000"


@pytest.mark.parametrize("authenticated", [False, True])
def test_storage_fault_clears_snapshots_only_after_direct_acknowledgment(
    tmp_path, monkeypatch, delta_cache, capture_server, authenticated
):
    directory = tmp_path / "unsafe"
    directory.mkdir(mode=0o755)
    monkeypatch.setenv("SEDIMENT_DELIVERY_DIR", str(directory))
    monkeypatch.setenv("SEDIMENT_OTLP_ENDPOINT", f"http://127.0.0.1:{capture_server}")
    if authenticated:
        monkeypatch.setenv("SEDIMENT_INGEST_TOKEN", "synthetic-token")
    target = tmp_path / "source.py"
    target.write_text("before\n")
    _snapshot("sess-1", "toolu-1", target, old_string="before", new_string="captured")
    target.write_text("captured\n")
    transcript = _write_transcript(
        tmp_path,
        [_assistant_edit("toolu-1", str(target), "captured"), _tool_result("toolu-1")],
    )
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps({"session_id": "sess-1", "transcript_path": str(transcript)})
        ),
    )
    assert mod.main(["--agent", "claude-code"]) == 0
    assert bool(list(delta_cache.rglob("*.json"))) is not authenticated
    assert len(_Capture.requests) == int(authenticated)
    if authenticated:
        record = _Capture.requests[0][2]["resourceLogs"][0]["scopeLogs"][0][
            "logRecords"
        ][0]
        values = {a["key"]: a["value"].get("stringValue") for a in record["attributes"]}
        assert values["observed_file_text"] == "captured\n"
        assert values["session.id"] == "sess-1"
        assert values["tool_use_id"] == "toolu-1"
        assert record["timeUnixNano"] == "1784854800000000000"
    assert list(directory.iterdir()) == []


def test_buffer_directory_does_not_enable_transcript_capture(tmp_path, monkeypatch):
    directory = tmp_path / "delivery"
    monkeypatch.setenv("SEDIMENT_DELIVERY_DIR", str(directory))
    monkeypatch.delenv("SEDIMENT_OTLP_ENDPOINT", raising=False)

    class Unreadable:
        def read(self):
            raise AssertionError("capture consent must precede reading stdin")

    monkeypatch.setattr("sys.stdin", Unreadable())
    assert mod.main(["--agent", "pi"]) == 0
    assert not directory.exists()


@pytest.mark.parametrize("agent", ["claude-code", "codex", "pi"])
@pytest.mark.parametrize("failure", ["missing", "invalid_utf8"])
def test_unreadable_transcript_retains_snapshots_without_exposing_source(
    tmp_path, monkeypatch, delta_cache, capsys, agent, failure
):
    directory = tmp_path / "delivery"
    monkeypatch.setenv("SEDIMENT_DELIVERY_DIR", str(directory))
    monkeypatch.setenv("SEDIMENT_OTLP_ENDPOINT", "http://127.0.0.1:9")
    target = tmp_path / "source.py"
    target.write_text("before\n")
    _snapshot("sess-1", "toolu-1", target, old_string="before", new_string="after")
    before = {path.name: path.read_bytes() for path in delta_cache.rglob("*.json")}
    assert before
    transcript = tmp_path / "private-transcript-source.jsonl"
    if failure == "invalid_utf8":
        transcript.write_bytes(b"private-source-text\xff\n")
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps({"session_id": "sess-1", "transcript_path": str(transcript)})
        ),
    )
    capsys.readouterr()
    assert mod.main(["--agent", agent]) == 0
    assert {
        path.name: path.read_bytes() for path in delta_cache.rglob("*.json")
    } == before
    assert not directory.exists()
    diagnostic = capsys.readouterr().err
    assert "transcript_unreadable" in diagnostic
    assert str(transcript) not in diagnostic
    assert "private-source-text" not in diagnostic
    assert "Traceback" not in diagnostic


@pytest.mark.parametrize("content", ["", '{"type":"system","text":"no edit"}\n'])
def test_readable_transcript_without_edits_clears_snapshots(
    tmp_path, monkeypatch, delta_cache, content
):
    directory = tmp_path / "delivery"
    monkeypatch.setenv("SEDIMENT_DELIVERY_DIR", str(directory))
    monkeypatch.setenv("SEDIMENT_OTLP_ENDPOINT", "http://127.0.0.1:9")
    target = tmp_path / "source.py"
    target.write_text("before\n")
    _snapshot("sess-1", "toolu-1", target, old_string="before", new_string="after")
    assert list(delta_cache.rglob("*.json"))
    transcript = tmp_path / "readable-transcript.jsonl"
    transcript.write_text(content)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps({"session_id": "sess-1", "transcript_path": str(transcript)})
        ),
    )
    assert mod.main(["--agent", "claude-code"]) == 0
    assert not list(delta_cache.rglob("*.json"))
    assert not directory.exists()

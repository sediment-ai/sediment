# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source files → public extractor → HTTP delivery → receiver and Fate."""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from sediment_capture import parse_otlp_logs
from sediment_cli import transcript
from sediment_core import AgentHarness, DeveloperDecision, InteractionMode
from sediment_derive import attach_edit_retention
from sediment_derive.survival import derive_fate_result
from sediment_derive.survival_scoring import four_gram_containment

SESSION = "child-session"
NANOS = 1789214340000000000
TIMESTAMP = "2026-09-12T11:59:00+00:00"


def _header(directory: Path, **extra) -> dict:
    return {
        "type": "session",
        "version": 3,
        "id": SESSION,
        "timestamp": "2026-09-12T12:00:00+00:00",
        "cwd": str(directory),
        **extra,
    }


def _pi_call(call_id: str, path: str, text: str) -> list[dict]:
    return [
        {
            "type": "message",
            "id": f"entry-{call_id}",
            "parentId": None,
            "timestamp": TIMESTAMP,
            "message": {
                "role": "assistant",
                "timestamp": NANOS // 1_000_000,
                "content": [
                    {
                        "type": "toolCall",
                        "id": call_id,
                        "name": "write",
                        "arguments": {"path": path, "content": text},
                    }
                ],
            },
        },
        {
            "type": "message",
            "id": f"result-{call_id}",
            "parentId": f"entry-{call_id}",
            "timestamp": TIMESTAMP,
            "message": {"role": "toolResult", "toolCallId": call_id, "isError": False},
        },
    ]


def _write(path: Path, entries: list[dict], *, newline="\n") -> None:
    path.write_text(
        newline.join(json.dumps(e, ensure_ascii=False) for e in entries) + newline
    )


@pytest.fixture
def extract(tmp_path, monkeypatch):
    for name in (
        "SEDIMENT_OTLP_ENDPOINT",
        "SEDIMENT_INGEST_TOKEN",
        "SEDIMENT_API_BEARER_TOKEN",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "SEDIMENT_DELIVERY_DIR",
        "OTEL_RESOURCE_ATTRIBUTES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SEDIMENT_DELTA_CACHE", str(tmp_path / "cache"))
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            assert self.path == "/v1/logs"
            assert self.headers["Authorization"] == "Bearer source-integrity-test"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(
                parse_otlp_logs(json.loads(body), org_id="source-integrity")
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Receiver)
    worker = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    worker.start()
    monkeypatch.setenv(
        "SEDIMENT_OTLP_ENDPOINT", f"http://127.0.0.1:{server.server_port}"
    )
    monkeypatch.setenv("SEDIMENT_INGEST_TOKEN", "source-integrity-test")

    def run(path, agent="pi", *, mode="inprocess"):
        received.clear()
        hook = json.dumps({"session_id": SESSION, "transcript_path": str(path)})
        if mode == "inprocess":
            monkeypatch.setattr("sys.stdin", io.StringIO(hook))
            assert transcript.main(["--agent", agent]) == 0
        else:
            if mode == "cli":
                command = [
                    str(Path(sys.executable).with_name("sediment")),
                    "transcript",
                ]
            else:
                from sediment_cli import delivery

                fleet = tmp_path / "standalone"
                fleet.mkdir()
                entry = fleet / "sediment_transcript.py"
                shutil.copyfile(transcript.__file__, entry)
                shutil.copyfile(delivery.__file__, fleet / "sediment_delivery.py")
                command = [sys.executable, "-I", str(entry)]
            result = subprocess.run(
                [*command, "--agent", agent],
                input=hook,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0, result.stderr
        return received.copy()

    yield run
    server.shutdown()
    worker.join(timeout=2)
    server.server_close()


def _assert_retained(capture, call_id, content):
    assert capture.records_received == capture.records_translated == 1
    assert capture.records_untranslated == capture.records_malformed == 0
    [observation] = capture.edit_observations
    assert observation.call_id == call_id
    assert observation.session_id == SESSION
    assert observation.occurred_at == datetime.fromisoformat(TIMESTAMP)
    assert observation.applied_text == observation.observed_file_text == content
    [fate] = derive_fate_result([observation], four_gram_containment).fates
    assert fate.score == 1.0
    assert fate.fate.value == "unmodified"
    decision = DeveloperDecision(
        org_id=observation.org_id,
        agent_harness=observation.agent_harness,
        session_id=SESSION,
        call_id=call_id,
        accepted=True,
        explicit=False,
        interaction_mode=InteractionMode.AGENT,
        occurred_at=observation.occurred_at,
        file_path=observation.file_path,
    )
    [joined] = attach_edit_retention([decision], [observation], four_gram_containment)
    assert joined.edit_retention_score == 1.0


@pytest.mark.parametrize("unrelated", [False, True])
def test_pi_relative_path_uses_source_cwd(tmp_path, monkeypatch, extract, unrelated):
    repo, launcher = tmp_path / "repo", tmp_path / "launcher"
    repo.mkdir()
    launcher.mkdir()
    content = "retained source content\n"
    (repo / "app.py").write_text(content)
    if unrelated:
        (launcher / "app.py").write_text("private unrelated content\n")
    source = tmp_path / "session.jsonl"
    _write(source, [_header(repo), *_pi_call("real-call", "app.py", content)])
    monkeypatch.chdir(launcher)
    [capture] = extract(source)
    _assert_retained(capture, "real-call", content)
    assert capture.edit_observations[0].file_path == str(repo / "app.py")


@pytest.mark.parametrize("cwd", [None, "", "relative", 42])
def test_pi_unknown_cwd_declines_relative_observation(
    tmp_path, monkeypatch, extract, capsys, cwd
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.py").write_text("wrong launcher file")
    source = tmp_path / "session.jsonl"
    _write(source, [_header(tmp_path, cwd=cwd), *_pi_call("call", "app.py", "source")])
    assert extract(source) == []
    diagnostic = capsys.readouterr().err
    assert "reason=execution_directory_invalid skipped_edits=1" in diagnostic
    assert "wrong launcher file" not in diagnostic


def test_pi_absolute_path_needs_no_cwd_fallback(tmp_path, extract):
    target = tmp_path / "app.py"
    target.write_text("source")
    source = tmp_path / "session.jsonl"
    _write(
        source, [_header(tmp_path, cwd=None), *_pi_call("call", str(target), "source")]
    )
    [capture] = extract(source)
    _assert_retained(capture, "call", "source")


def test_pi_fork_excludes_inherited_messages_and_keeps_real_child_call(
    tmp_path, extract, capsys
):
    target = tmp_path / "app.py"
    target.write_text("child content")
    parent, source = tmp_path / "parent.jsonl", tmp_path / "child.jsonl"
    inherited = _pi_call("parent-call", str(target), "parent content")
    _write(parent, [_header(tmp_path, id="parent-session"), *inherited])
    copied = copy.deepcopy(inherited)
    copied[0]["parentId"] = "re-chained-by-native-fork"
    _write(
        source,
        [
            _header(tmp_path, parentSession=str(parent)),
            *copied,
            *_pi_call("child-call", str(target), "child content"),
        ],
    )
    [capture] = extract(source)
    _assert_retained(capture, "child-call", "child content")
    assert "reason=inherited_entry skipped_entries=2" in capsys.readouterr().err


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "malformed",
        "conflict",
        "duplicate",
        "oversized",
        "outside",
        "symlink",
        "traversal",
        "self",
        "directory",
        "fifo",
    ],
)
def test_pi_unproved_parent_declines_without_content_leak(
    tmp_path, extract, capsys, fault
):
    target = tmp_path / "app.py"
    target.write_text("child content")
    scope = tmp_path / "sessions"
    scope.mkdir()
    parent, source = scope / "parent.jsonl", scope / "child.jsonl"
    inherited = _pi_call("parent-call", str(target), "parent content")
    parent_entries = [_header(tmp_path, id="parent-session"), *inherited]
    _write(parent, parent_entries)
    reference = str(parent)
    if fault == "missing":
        parent.unlink()
    elif fault == "malformed":
        parent.write_text("malformed private source\n")
    elif fault == "conflict":
        altered = copy.deepcopy(parent_entries)
        altered[1]["message"]["content"][0]["arguments"]["content"] = "different source"
        _write(parent, altered)
    elif fault == "duplicate":
        _write(parent, [*parent_entries, parent_entries[1]])
    elif fault == "oversized":
        with parent.open("wb") as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
    elif fault in {"outside", "symlink", "traversal"}:
        outside = tmp_path / "outside.jsonl"
        _write(outside, parent_entries)
        if fault == "outside":
            reference = str(outside)
        elif fault == "symlink":
            parent.unlink()
            parent.symlink_to(outside)
        else:
            reference = str(scope / ".." / "outside.jsonl")
    elif fault == "self":
        reference = str(source)
    elif fault == "directory":
        parent.unlink()
        parent.mkdir()
    elif fault == "fifo":
        parent.unlink()
        os.mkfifo(parent)
    _write(
        source,
        [
            _header(tmp_path, parentSession=reference),
            *inherited,
            *_pi_call("child-call", str(target), "child content"),
        ],
    )
    assert extract(source) == []
    diagnostic = capsys.readouterr().err
    assert "reason=parent_source_unverified skipped_entries=4" in diagnostic
    assert str(tmp_path) not in diagnostic
    assert "content" not in diagnostic


def test_pi_parent_is_not_read_without_capture_consent(tmp_path, monkeypatch, extract):
    source = tmp_path / "child.jsonl"
    _write(source, [_header(tmp_path, parentSession=str(tmp_path / "parent.jsonl"))])
    monkeypatch.delenv("SEDIMENT_OTLP_ENDPOINT")
    monkeypatch.setattr(
        Path, "read_text", lambda *a, **kw: pytest.fail("source read without consent")
    )
    assert extract(source) == []


@pytest.mark.parametrize("agent", ["pi", "codex", "claude-code"])
@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_lf_jsonl_preserves_unicode_content_through_receiver(
    tmp_path, extract, agent, separator
):
    content = f"const separator = '{separator}';\n"
    target = tmp_path / "app.js"
    target.write_text(content)
    if agent == "pi":
        entries = [_header(tmp_path), *_pi_call("call", str(target), content)]
    elif agent == "codex":
        entries = [
            {"type": "session_meta", "payload": {"id": SESSION, "cwd": str(tmp_path)}},
            {
                "type": "event_msg",
                "timestamp": TIMESTAMP,
                "payload": {
                    "type": "patch_apply_end",
                    "success": True,
                    "call_id": "call",
                    "changes": {str(target): {"type": "add", "content": content}},
                },
            },
        ]
    else:
        entries = [
            {
                "type": "assistant",
                "sessionId": SESSION,
                "timestamp": TIMESTAMP,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call",
                            "name": "Write",
                            "input": {"file_path": str(target), "content": content},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "sessionId": SESSION,
                "timestamp": TIMESTAMP,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call",
                            "content": "done",
                            "is_error": False,
                        }
                    ],
                },
            },
        ]
    source = tmp_path / "session.jsonl"
    _write(source, entries, newline="\r\n")
    # A malformed physical sibling remains fail-soft without changing framing.
    with source.open("a") as stream:
        stream.write("malformed sibling\n")
    [capture] = extract(source, agent)
    _assert_retained(capture, "call", content)
    assert capture.edit_observations[0].agent_harness == AgentHarness(agent)


@pytest.mark.parametrize(
    "path", ["@app.py", "~/app.py", "file://app.py", "app\u00a0.py"]
)
def test_pi_unsupported_path_form_is_counted_without_reading_lookalike(
    tmp_path, extract, capsys, path
):
    lookalike = tmp_path / path
    lookalike.parent.mkdir(parents=True, exist_ok=True)
    lookalike.write_text("unrelated content")
    source = tmp_path / "session.jsonl"
    _write(source, [_header(tmp_path), *_pi_call("call", path, "source content")])
    assert extract(source) == []
    assert "reason=unsupported_path skipped_edits=1" in capsys.readouterr().err


def test_pi_parent_content_comparison_preserves_json_types(tmp_path, extract, capsys):
    target = tmp_path / "app.py"
    target.write_text("child content")
    parent, source = tmp_path / "parent.jsonl", tmp_path / "child.jsonl"
    inherited = _pi_call("parent-call", str(target), "parent content")
    inherited[0]["message"]["metadata"] = {"value": True}
    _write(parent, [_header(tmp_path, id="parent-session"), *inherited])
    changed = copy.deepcopy(inherited)
    changed[0]["message"]["metadata"]["value"] = 1
    _write(
        source,
        [
            _header(tmp_path, parentSession=str(parent)),
            *changed,
            *_pi_call("child-call", str(target), "child content"),
        ],
    )
    assert extract(source) == []
    assert (
        "reason=parent_source_unverified skipped_entries=4" in capsys.readouterr().err
    )


@pytest.mark.parametrize("mode", ["cli", "standalone"])
def test_distributed_extractor_keeps_source_path_and_fork_ownership(
    tmp_path, extract, mode
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("child content")
    parent, source = tmp_path / "parent.jsonl", tmp_path / "child.jsonl"
    inherited = _pi_call("parent-call", "app.py", "parent content")
    # The immediate parent may itself be forked: no ancestor access is needed.
    _write(
        parent,
        [
            _header(repo, id="parent-session", parentSession="absent-ancestor.jsonl"),
            *inherited,
        ],
    )
    _write(
        source,
        [
            _header(repo, parentSession=parent.name),
            *inherited,
            *_pi_call("child-call", "app.py", "child content"),
        ],
    )
    [capture] = extract(source, mode=mode)
    _assert_retained(capture, "child-call", "child content")


def test_unreadable_parent_preserves_snapshots_and_reports_count(
    tmp_path, extract, monkeypatch, capsys
):
    target = tmp_path / "app.py"
    target.write_text("content")
    parent, source = tmp_path / "parent.jsonl", tmp_path / "child.jsonl"
    _write(parent, [_header(tmp_path, id="parent-session")])
    _write(
        source,
        [
            _header(tmp_path, parentSession=str(parent)),
            *_pi_call("child-call", str(target), "content"),
        ],
    )
    snapshots = transcript._cache_dir(SESSION)
    snapshots.mkdir(parents=True)
    snapshot = snapshots / "original.json"
    snapshot.write_bytes(b"original snapshot bytes")
    original_open = os.open

    def unreadable(path, flags, *args, **kwargs):
        if path == parent.name:
            raise PermissionError("private exception text")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", unreadable)
    assert extract(source) == []
    assert snapshot.read_bytes() == b"original snapshot bytes"
    diagnostic = capsys.readouterr().err
    assert "reason=parent_source_unverified skipped_entries=2" in diagnostic
    assert "private exception text" not in diagnostic
    assert str(tmp_path) not in diagnostic


def test_pi_fork_header_cannot_be_bypassed_by_malformed_prefix(
    tmp_path, extract, capsys
):
    target = tmp_path / "app.py"
    target.write_text("content")
    source = tmp_path / "child.jsonl"
    _write(
        source,
        [
            {"type": "unexpected-prefix"},
            _header(tmp_path, parentSession="missing.jsonl"),
            *_pi_call("parent-call", str(target), "content"),
        ],
    )
    assert extract(source) == []
    assert (
        "reason=parent_source_unverified skipped_entries=2" in capsys.readouterr().err
    )


def test_pi_header_conflict_counts_absence(tmp_path, extract, capsys):
    target = tmp_path / "app.py"
    target.write_text("content")
    source = tmp_path / "child.jsonl"
    _write(
        source,
        [
            _header(tmp_path, id="other-session"),
            *_pi_call("call", str(target), "content"),
        ],
    )
    assert extract(source) == []
    assert "reason=session_mismatch skipped_entries=2" in capsys.readouterr().err

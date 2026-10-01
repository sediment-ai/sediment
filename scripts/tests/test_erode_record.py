# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for scripts/erode_record.py over real sockets.

A stub upstream answers like the Anthropic Messages API, and an
``http.client`` agent sends requests through the recorder. ``scripts/`` is
not a package, so the module is loaded by path.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import stat
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "erode_record.py"
MARKER = "SYNTHETIC-CONTENT-MARKER"


def _load_module():
    spec = importlib.util.spec_from_file_location("erode_record", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


record = _load_module()


def _sse(event: dict) -> bytes:
    return b"event: %s\ndata: %s\n\n" % (
        event["type"].encode(),
        json.dumps(event).encode(),
    )


STREAM = b"".join(
    _sse(event)
    for event in [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "model": "claude-test",
                "content": [],
                "usage": {
                    "input_tokens": 12,
                    "cache_creation_input_tokens": 300,
                    "cache_read_input_tokens": 4000,
                    "cache_creation": {"ephemeral_5m_input_tokens": 300},
                    "output_tokens": 1,
                    "service_tier": "standard",
                },
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": MARKER},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 57},
        },
        {"type": "message_stop"},
    ]
)


class Upstream:
    """Answers each request with the next queued reply and records headers."""

    def __init__(self) -> None:
        self.headers: list[dict] = []
        self.replies: list[tuple[int, str, list[bytes]]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                owner.headers.append(dict(self.headers.items()))
                status, content_type, chunks = owner.replies.pop(0)
                self.send_response_only(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]


@pytest.fixture
def setup(tmp_path) -> Iterator[tuple[Upstream, int, Path]]:
    upstream = Upstream()
    run_dir = record.prepare_run_dir(tmp_path / "recordings", "run-a")
    server = record.make_server(upstream.url, run_dir, port=0)
    threads = [
        threading.Thread(target=s.serve_forever, daemon=True)
        for s in (upstream.server, server)
    ]
    for thread in threads:
        thread.start()
    yield upstream, server.server_address[1], run_dir
    for s in (server, upstream.server):
        s.shutdown()
        s.server_close()


def _post(port: int, path: str, body: bytes) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(
        "POST",
        path,
        body=body,
        headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"},
    )
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, payload


def _body(text: str) -> bytes:
    # Compact separators, like the bodies Claude Code sends.
    request = {"model": "m", "messages": [{"role": "user", "content": text}]}
    return json.dumps(request, separators=(",", ":")).encode()


def test_records_bodies_and_usage_counts(setup):
    upstream, port, run_dir = setup
    # Split the stream mid-line so the reader must buffer across chunks.
    upstream.replies = [
        (200, "text/event-stream", [STREAM[:50], STREAM[50:333], STREAM[333:]]),
        (529, "application/json", [b'{"type":"error","error":{"message":"x"}}']),
        (
            200,
            "application/json",
            [
                json.dumps(
                    {
                        "model": "claude-test",
                        "content": [{"type": "text", "text": MARKER}],
                        "usage": {"input_tokens": 5, "output_tokens": 2},
                    }
                ).encode()
            ],
        ),
        (200, "application/json", [b"{}"]),
    ]
    first, retried, plain = _body(f"{MARKER} 1"), _body("2"), _body("3")

    status, payload = _post(port, "/v1/messages?beta=true", first)
    assert (status, payload) == (200, STREAM)
    assert _post(port, "/v1/messages", retried)[0] == 529
    assert _post(port, "/v1/messages", plain)[0] == 200
    assert _post(port, "/v1/messages/count_tokens", _body("4"))[0] == 200

    files = sorted(p.name for p in run_dir.iterdir())
    assert files == ["000001.json", "000002.rejected", "000003.json", "usage.jsonl"]
    assert (run_dir / "000001.json").read_bytes() == first
    assert (run_dir / "000002.rejected").read_bytes() == retried
    for path in run_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700

    usage_text = (run_dir / "usage.jsonl").read_text()
    assert MARKER not in usage_text
    lines = [json.loads(line) for line in usage_text.splitlines()]
    assert lines == [
        {
            "request": 1,
            "status": 200,
            "model": "claude-test",
            "usage": {
                "input_tokens": 12,
                "cache_creation_input_tokens": 300,
                "cache_read_input_tokens": 4000,
                "cache_creation": {"ephemeral_5m_input_tokens": 300},
                "output_tokens": 57,
            },
        },
        {"request": 2, "status": 529, "model": None, "usage": {}},
        {
            "request": 3,
            "status": 200,
            "model": "claude-test",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
    ]
    # Recorded calls reach the upstream uncompressed; others are untouched.
    assert [h.get("Accept-Encoding") for h in upstream.headers] == [
        "identity",
        "identity",
        "identity",
        "gzip",
    ]


def test_unreachable_upstream_leaves_a_partial_body(tmp_path):
    run_dir = record.prepare_run_dir(tmp_path, "run-b")
    # Port 9 (discard) has no listener on the loopback interface.
    server = record.make_server("http://127.0.0.1:9", run_dir, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert _post(server.server_address[1], "/v1/messages", _body("x"))[0] == 502
    finally:
        server.shutdown()
        server.server_close()
    assert sorted(p.name for p in run_dir.iterdir()) == ["000001.partial"]


def test_run_dir_refuses_reuse_and_paths(tmp_path):
    run_dir = record.prepare_run_dir(tmp_path, "run")
    (run_dir / "000001.json").write_text("{}")
    with pytest.raises(ValueError, match="not empty"):
        record.prepare_run_dir(tmp_path, "run")
    for bad in ("", ".", "..", "a/b"):
        with pytest.raises(ValueError):
            record.prepare_run_dir(tmp_path, bad)

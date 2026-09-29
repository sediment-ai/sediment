# SPDX-License-Identifier: AGPL-3.0-or-later
"""The E1 recorder and instance draw: scripts/erode_record.py, swe_bench_subset.py.

``scripts/`` is not a package, so each module is loaded by path.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


record = _load("erode_record")
subset = _load("swe_bench_subset")


class _Upstream(BaseHTTPRequestHandler):
    """Echoes each request body back, so the test sees what was forwarded."""

    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _post(port: int, path: str, body: bytes) -> bytes:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("POST", path, body=body, headers={"x-api-key": "secret"})
    return connection.getresponse().read()


def test_recorder_saves_messages_bodies_in_order_and_forwards_unchanged(tmp_path):
    upstream = _serve(ThreadingHTTPServer(("127.0.0.1", 0), _Upstream))
    out = tmp_path / "run-1"
    proxy = _serve(
        record.recording_server(
            f"http://127.0.0.1:{upstream.server_port}", out, "127.0.0.1", 0
        )
    )
    try:
        # A body pruning would change: a large read superseded by a later read.
        big = "x" * 6000
        messages = [{"role": "user", "content": "fix"}]
        for n in range(1, 5):
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"t{n}",
                            "name": "Read" if n < 3 else "Bash",
                            "input": {"file_path": "a.py"}
                            if n < 3
                            else {"command": f"ls {n}"},
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": f"t{n}", "content": big}
                    ],
                }
            )
        first = json.dumps({"model": "m", "messages": messages}).encode()
        second = b'{"model":"m","messages":[]}'
        port = proxy.server_port
        assert _post(port, "/v1/messages?beta=true", first) == first
        assert _post(port, "/v1/messages", second) == second
        assert _post(port, "/v1/messages/count_tokens", b"{}") == b"{}"
    finally:
        proxy.shutdown()
        upstream.shutdown()

    files = sorted(out.iterdir())
    assert [f.name for f in files] == ["00001.json", "00002.json"]
    assert [f.read_bytes() for f in files] == [first, second]
    assert all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in files)
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert b"secret" not in b"".join(f.read_bytes() for f in files)


def test_recorder_refuses_a_non_empty_directory(tmp_path):
    (tmp_path / "00001.json").write_text("{}")
    with pytest.raises(ValueError, match="not empty"):
        record.recording_server("http://127.0.0.1:9", tmp_path, "127.0.0.1", 0)


def test_draw_is_seeded_disjoint_and_order_independent():
    ids = [f"repo__repo-{n}" for n in range(500)]
    result = subset.draw(ids)
    assert result == subset.draw(list(reversed(ids)))
    assert len(result["e2"]) == 50 and result["e2"] == sorted(result["e2"])
    assert len(result["e1_candidates"]) == 10
    assert not set(result["e2"]) & set(result["e1_candidates"])
    assert subset.draw(ids, seed=1) != result
    with pytest.raises(ValueError, match="not unique"):
        subset.draw(ids + ids[:1])


def test_committed_instances_match_the_draw():
    committed = json.loads((SCRIPTS / "erode_eval_instances.json").read_text())
    assert committed["seed"] == subset.SEED and committed["instances"] == 500
    assert len(committed["e2"]) == 50 and len(committed["e1_candidates"]) == 10
    assert not set(committed["e2"]) & set(committed["e1_candidates"])

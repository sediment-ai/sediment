# SPDX-License-Identifier: MIT
"""The standalone pruning proxy over real sockets: a stub upstream records what
arrives, and an ``http.client`` agent reads what comes back."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from erode import cli, proxy as erode_proxy
from erode.core import PrunePolicy, prune

EVENTS = [b"event: message_start\ndata: {}\n\n", b"event: ping\ndata: {}\n\n"]


class Upstream:
    """Records requests; answers per ``self.reply`` or streams ``EVENTS``."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict, bytes]] = []
        self.reply: tuple[int, dict, bytes] | None = None
        self.first_event_read = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                owner.requests.append(
                    (self.command, self.path, dict(self.headers.items()), body)
                )
                if owner.reply is not None:
                    status, headers, payload = owner.reply
                    self.send_response_only(status, "Custom Reason")
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response_only(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for index, event in enumerate(EVENTS):
                    if index:
                        # The next event waits until the agent has the first:
                        # a buffering proxy would deadlock here.
                        assert owner.first_event_read.wait(5)
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")

            do_GET = do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/base" % self.server.server_address[1]


def _serve(server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def upstream() -> Iterator[Upstream]:
    stub = Upstream()
    _serve(stub.server)
    yield stub
    stub.server.shutdown()
    stub.server.server_close()


def _proxy(url: str, prune: bool = True):
    server = erode_proxy.make_server(url, port=0, prune=prune)
    _serve(server)
    return server


@pytest.fixture
def proxy(upstream: Upstream) -> Iterator[http.client.HTTPConnection]:
    server = _proxy(upstream.url)
    agent = http.client.HTTPConnection(*server.server_address, timeout=10)
    yield agent
    agent.close()
    server.shutdown()
    server.server_close()


def _request() -> dict:
    content = "x" * 5000
    messages: list[dict] = [{"role": "user", "content": "Fix a.py."}]
    for step in range(1, 5):
        name, arguments = ("Read", {"file_path": "a.py"})
        if step > 2:
            name, arguments = ("Bash", {"command": f"ls {step}"})
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"t{step}",
                        "name": name,
                        "input": arguments,
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"t{step}",
                        "content": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        )
    return {
        "model": "claude-test",
        "max_tokens": 64,
        "stream": True,
        "system": [
            {"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}
        ],
        "unknown_provider_field": {"kept": [1, 2.5, None]},
        "messages": messages,
    }


def test_binds_to_loopback_by_default(upstream: Upstream) -> None:
    server = erode_proxy.make_server(upstream.url, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert server.RequestHandlerClass.prune_enabled is True
    finally:
        server.server_close()


def test_messages_are_pruned_and_other_fields_untouched(upstream, proxy) -> None:
    upstream.first_event_read.set()
    request = _request()
    proxy.request("POST", "/v1/messages?beta=true", body=json.dumps(request))
    proxy.getresponse().read()
    method, path, _, body = upstream.requests[0]
    assert (method, path) == ("POST", "/base/v1/messages?beta=true")
    sent = json.loads(body)
    expected, report = prune(request["messages"], PrunePolicy())
    assert report["stubbed_results"] == 1
    assert sent == {**request, "messages": expected}
    stubbed = sent["messages"][2]["content"][0]
    assert stubbed["cache_control"] == {"type": "ephemeral"}


def test_streaming_is_relayed_in_order_without_buffering(upstream, proxy) -> None:
    proxy.request("POST", "/v1/chat/completions", body=b'{"messages": []}')
    response = proxy.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream"
    received = response.read1(len(EVENTS[0]))
    assert received == EVENTS[0]
    upstream.first_event_read.set()
    rest = response.read()
    assert received + rest == b"".join(EVENTS)


def test_agent_headers_are_forwarded_unchanged(upstream, proxy) -> None:
    upstream.first_event_read.set()
    headers = {
        "x-api-key": "sk-synthetic",
        "Authorization": "Bearer synthetic",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "prompt-caching-2024-07-31",
        "X-Claude-Code-Session-Id": "session-1",
        "Content-Type": "application/json",
    }
    proxy.request("POST", "/v1/messages", body=b'{"messages": []}', headers=headers)
    proxy.getresponse().read()
    received = upstream.requests[0][2]
    for name, value in headers.items():
        assert received[name] == value
    assert received["Host"] == upstream.url.split("/")[2]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/v1/models", b""),
        ("POST", "/v1/complete", b'{"messages": "left alone"}'),
        ("POST", "/v1/messages", b"{not json"),
        ("POST", "/v1/messages", b'["a", "list"]'),
    ],
)
def test_unknown_routes_and_malformed_requests_pass_through(
    upstream, proxy, method, path, body
) -> None:
    upstream.reply = (200, {"Content-Type": "application/json"}, b"{}")
    proxy.request(method, path, body=body or None)
    assert proxy.getresponse().read() == b"{}"
    assert upstream.requests[0][0:2] == (method, "/base" + path)
    assert upstream.requests[0][3] == body


def test_prunable_body_passes_through_when_disabled(upstream: Upstream) -> None:
    upstream.reply = (200, {}, b"{}")
    server = _proxy(upstream.url, prune=False)
    agent = http.client.HTTPConnection(*server.server_address, timeout=10)
    try:
        body = json.dumps(_request()).encode()
        agent.request("POST", "/v1/messages", body=body)
        agent.getresponse().read()
        assert upstream.requests[0][3] == body
    finally:
        agent.close()
        server.shutdown()
        server.server_close()


def test_upstream_error_is_returned_unchanged_without_retry(upstream, proxy) -> None:
    error = b'{"type":"error","error":{"type":"rate_limit_error"}}'
    upstream.reply = (429, {"retry-after": "7", "request-id": "req_1"}, error)
    proxy.request("POST", "/v1/messages", body=json.dumps(_request()))
    response = proxy.getresponse()
    assert (response.status, response.reason) == (429, "Custom Reason")
    assert response.getheader("retry-after") == "7"
    assert response.getheader("request-id") == "req_1"
    assert response.read() == error
    assert len(upstream.requests) == 1


def test_unreachable_upstream_is_a_502() -> None:
    probe = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    closed_port = probe.server_address[1]
    probe.server_close()
    server = _proxy(f"http://127.0.0.1:{closed_port}")
    agent = http.client.HTTPConnection(*server.server_address, timeout=10)
    try:
        agent.request("POST", "/v1/messages", body=b"{}")
        assert agent.getresponse().status == 502
    finally:
        agent.close()
        server.shutdown()
        server.server_close()


def test_rejects_an_upstream_that_is_not_a_url() -> None:
    with pytest.raises(ValueError):
        erode_proxy.make_server("api.anthropic.com", port=0)


def _cached_request() -> dict:
    """Claude Code's shape: cache_control breakpoints on tools, system, and
    message blocks, including the tool result that gets stubbed."""
    ephemeral = {"type": "ephemeral"}
    request = _request()
    for index, message in enumerate(request["messages"]):
        for block in message["content"] if isinstance(message["content"], list) else []:
            if block.get("type") == "tool_result":
                block["content"] = f"result {index} " + "x" * 5000
    request["messages"][2]["content"][0]["cache_control"] = {
        "type": "ephemeral",
        "ttl": "1h",
    }
    request["messages"][-1]["content"].append(
        {"type": "text", "text": "Continue. ünïcode ✓", "cache_control": ephemeral}
    )
    request["tools"] = [
        {"name": "Read", "input_schema": {"type": "object"}, "cache_control": ephemeral}
    ]
    return request


def test_cache_control_bytes_survive_outside_stubbed_results(upstream, proxy) -> None:
    upstream.first_event_read.set()
    request = _cached_request()
    # JSON.stringify's compact form, which Claude Code sends.
    body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    proxy.request("POST", "/v1/messages", body=body)
    proxy.getresponse().read()
    forwarded = upstream.requests[0][3]
    stubbed = request["messages"][2]["content"][0]["content"]
    expected, report = prune(request["messages"], PrunePolicy())
    assert report["stubbed_results"] == 1
    stub = expected[2]["content"][0]["content"]
    # The only byte change is the stubbed result's content string.
    old, new = json.dumps(stubbed), json.dumps(stub, ensure_ascii=False)
    assert body.count(old.encode()) == 1
    assert forwarded == body.replace(old.encode(), new.encode())
    assert forwarded.count(b'"cache_control":') == body.count(b'"cache_control":')


def test_provider_managed_context_passes_through(upstream, proxy) -> None:
    upstream.first_event_read.set()
    request = {**_request(), "context_management": {"edits": [{"type": "x"}]}}
    body = json.dumps(request).encode()
    proxy.request("POST", "/v1/messages", body=body)
    proxy.getresponse().read()
    assert upstream.requests[0][3] == body


def test_cli_requires_an_upstream(monkeypatch) -> None:
    monkeypatch.delenv("ERODE_UPSTREAM", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["proxy"])
    assert exit_info.value.code == 2


def test_cli_rejects_an_unknown_mode(monkeypatch) -> None:
    monkeypatch.setenv("ERODE_MODE", "aggressive")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["proxy", "--upstream", "http://127.0.0.1:1"])
    assert exit_info.value.code == 2

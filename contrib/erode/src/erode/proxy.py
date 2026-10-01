# SPDX-License-Identifier: MIT
"""
The erode proxy: a standard-library HTTP pass-through around ``prune_request``.

Run it with ``erode proxy --upstream <url>`` and point an agent at it, for
example ``ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude``. The proxy prunes
``POST /v1/messages`` and ``POST /v1/responses`` bodies and logs
``POST /v1/chat/completions`` bodies as skipped. Every other request, and any
body that isn't a JSON object with a ``messages`` or ``input`` list, is
forwarded unchanged. With pruning off, the proxy is a pure pass-through.

The proxy forwards the agent's headers, including its credentials, and stores
nothing. It adds no retries, streams the upstream response as it arrives, and
answers 502 only when the upstream can't be reached. A gateway behind the proxy
records the pruned request, which is exactly what the model received.
"""

from __future__ import annotations

import http.client
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from erode.core import PrunePolicy, prune_request

logger = logging.getLogger("erode.proxy")

PRUNED_ROUTES = frozenset({"/v1/chat/completions", "/v1/messages", "/v1/responses"})
# Connection-scoped headers (RFC 9110 section 7.6.1) belong to one hop only.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "expect",
        "host",
        "content-length",
    }
)
UPSTREAM_TIMEOUT_SECONDS = 600


def _nominated(values: list[str] | None) -> frozenset[str]:
    """Header names that a Connection header nominates for this hop only."""
    return frozenset(
        name.strip().lower()
        for value in values or ()
        for name in value.split(",")
        if name.strip()
    )


def prune_body(method: str, path: str, body: bytes, enabled: bool) -> bytes:
    """Return the body to forward: pruned when it's a known chat request."""
    if not enabled or method != "POST" or urlsplit(path).path not in PRUNED_ROUTES:
        return body
    try:
        request = json.loads(body)
        pruned, report = prune_request(request, PrunePolicy())
        if report is None:
            return body
        forwarded = body
        if report["stubbed_results"]:
            # Compact separators match JavaScript's JSON.stringify, which agents
            # such as Claude Code send. Encoding can fail on untouched content.
            forwarded = json.dumps(
                pruned, ensure_ascii=False, separators=(",", ":")
            ).encode()
    except Exception:  # noqa: BLE001 — pruning must never fail a model call
        logger.warning("erode_prune reason=prune_failed")
        return body
    logger.info(
        "erode_prune policy_version=%s stubbed_results=%d bytes_removed=%d skipped=%s",
        report["policy_version"],
        report["stubbed_results"],
        report["bytes_removed"],
        report.get("skipped", "none"),
    )
    return forwarded


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = urlsplit("http://127.0.0.1:4000")
    prune_enabled = False

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.debug("request " + format, *args)

    def _body(self) -> bytes:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass  # discard trailers
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _forward(self) -> None:
        try:
            body = self._body()
        except ValueError:
            self.send_error(400, "Malformed request body")
            return
        body = prune_body(self.command, self.path, body, self.prune_enabled)
        dropped = HOP_BY_HOP | _nominated(self.headers.get_all("Connection"))
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in dropped
        }
        headers["Host"] = self.upstream.netloc
        if body or self.command in ("POST", "PUT", "PATCH"):
            headers["Content-Length"] = str(len(body))
        connection_class = (
            http.client.HTTPSConnection
            if self.upstream.scheme == "https"
            else http.client.HTTPConnection
        )
        upstream = connection_class(
            self.upstream.netloc, timeout=UPSTREAM_TIMEOUT_SECONDS
        )
        try:
            upstream.request(
                self.command,
                self.upstream.path.rstrip("/") + self.path,
                body=body or None,
                headers=headers,
            )
            response = upstream.getresponse()
        except OSError:
            upstream.close()
            logger.warning("erode_proxy reason=upstream_unreachable")
            self.send_error(502, "Upstream unreachable")
            return
        try:
            self._relay(response)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True  # the agent hung up mid-response
        finally:
            upstream.close()

    def _relay(self, response: http.client.HTTPResponse) -> None:
        self.send_response_only(response.status, response.reason)
        length = response.getheader("Content-Length")
        bodiless = self.command == "HEAD" or response.status in (204, 304)
        dropped = HOP_BY_HOP | _nominated(response.msg.get_all("Connection"))
        for name, value in response.getheaders():
            if name.lower() not in dropped:
                self.send_header(name, value)
        if bodiless:
            if length is not None:
                self.send_header("Content-Length", length)
            self.end_headers()
            return
        chunked = length is None
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", length)
        self.end_headers()
        sent, complete = 0, True
        # read1 returns whatever has arrived, so server-sent events stream.
        while True:
            try:
                chunk = response.read1(65536)
            except (http.client.HTTPException, OSError):
                complete = False  # the upstream broke off mid-body
                break
            if not chunk:
                break
            if chunked:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            else:
                self.wfile.write(chunk)
            self.wfile.flush()
            sent += len(chunk)
        if not chunked and length.isdigit() and sent != int(length):
            complete = False
        if not complete:
            # The framing can't report truncation; closing the connection does.
            logger.warning("erode_proxy reason=upstream_truncated")
            self.close_connection = True
            return
        if chunked:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward


def make_server(
    upstream: str, host: str = "127.0.0.1", port: int = 8787, prune: bool = True
) -> ThreadingHTTPServer:
    parsed = urlsplit(upstream)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("upstream must be an http:// or https:// URL")
    handler = type(
        "ConfiguredProxyHandler",
        (ProxyHandler,),
        {"upstream": parsed, "prune_enabled": prune},
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server

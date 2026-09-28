# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Standalone pruning proxy: a stdlib HTTP pass-through around ``sediment_context``.

Deployment glue, not an installed package (ADR 0027). Copy this file and
``sediment_context.py`` into one directory, then run:

    SEDIMENT_CONTEXT_PRUNE=supersede \\
        python3 sediment_prune_proxy.py --upstream https://api.anthropic.com

Point an agent at it, for example ``ANTHROPIC_BASE_URL=http://127.0.0.1:8787``,
or chain it in front of a gateway. ``POST /v1/chat/completions`` and
``POST /v1/messages`` bodies are pruned; every other request, and any body
that isn't a JSON object with a ``messages`` list, is forwarded unchanged.
Without ``SEDIMENT_CONTEXT_PRUNE=supersede`` the proxy is a pure pass-through.

The proxy forwards the agent's headers, including its credentials, and stores
nothing. It adds no retries, streams the upstream response as it arrives, and
answers 502 only when the upstream can't be reached. Downstream capture records
the pruned request, which is exactly what the model received.
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import sediment_context

logger = logging.getLogger("sediment.prune_proxy")

PRUNED_ROUTES = frozenset({"/v1/chat/completions", "/v1/messages"})
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


def prune_body(method: str, path: str, body: bytes, enabled: bool) -> bytes:
    """Return the body to forward: pruned when it's a known chat request."""
    if not enabled or method != "POST" or urlsplit(path).path not in PRUNED_ROUTES:
        return body
    try:
        request = json.loads(body)
    except ValueError:
        return body
    if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
        return body
    try:
        messages, report = sediment_context.prune(
            request["messages"], sediment_context.PrunePolicy()
        )
    except Exception:  # noqa: BLE001 — pruning must never fail a model call
        logger.warning("sediment_context_prune reason=prune_failed")
        return body
    logger.info(
        "sediment_context_prune policy_version=%s stubbed_results=%d bytes_removed=%d",
        report["policy_version"],
        report["stubbed_results"],
        report["bytes_removed"],
    )
    if not report["stubbed_results"]:
        return body
    request["messages"] = messages
    return json.dumps(request, ensure_ascii=False).encode("utf-8")


class PruneProxyHandler(BaseHTTPRequestHandler):
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
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in HOP_BY_HOP
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
            logger.warning("sediment_prune_proxy reason=upstream_unreachable")
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
        for name, value in response.getheaders():
            if name.lower() not in HOP_BY_HOP:
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
        # read1 returns whatever has arrived, so server-sent events stream.
        while chunk := response.read1(65536):
            if chunked:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            else:
                self.wfile.write(chunk)
            self.wfile.flush()
        if chunked:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward


def make_server(
    upstream: str, host: str = "127.0.0.1", port: int = 8787, prune: bool = False
) -> ThreadingHTTPServer:
    parsed = urlsplit(upstream)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("upstream must be an http:// or https:// URL")
    handler = type(
        "ConfiguredPruneProxyHandler",
        (PruneProxyHandler,),
        {"upstream": parsed, "prune_enabled": prune},
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Forward model requests to one upstream, stubbing superseded tool output."
    )
    parser.add_argument(
        "--upstream",
        default=os.environ.get("SEDIMENT_PRUNE_UPSTREAM"),
        help="Gateway or provider base URL (default: $SEDIMENT_PRUNE_UPSTREAM)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8787, help="Bind port")
    args = parser.parse_args(argv)
    if not args.upstream:
        parser.error("--upstream or SEDIMENT_PRUNE_UPSTREAM is required")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    mode = os.environ.get("SEDIMENT_CONTEXT_PRUNE", "").strip()
    if mode and mode != "supersede":
        logger.warning("sediment_context_prune reason=unknown_mode mode=%s", mode)
    try:
        server = make_server(args.upstream, args.host, args.port, mode == "supersede")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    logger.info(
        "sediment_prune_proxy listening=%s:%d upstream=%s prune=%s",
        args.host,
        args.port,
        args.upstream,
        "supersede" if mode == "supersede" else "off",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

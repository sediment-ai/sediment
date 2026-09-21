# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checkout tools never forward credentials through redirects or unsafe URLs."""

import importlib.util
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(params=["scripts/smoke.py", "sim/driver/run.py"])
def operator(request):
    name = "security_" + Path(request.param).stem
    spec = importlib.util.spec_from_file_location(name, ROOT / request.param)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def endpoint(reply_code=200, location=None):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.respond()

        def do_GET(self):
            self.respond()

        def respond(self):
            received.append(dict(self.headers))
            self.send_response(reply_code)
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_never_reaches_other_origin(operator, code):
    with endpoint() as (destination, leaked):
        with endpoint(code, destination) as (source, received):
            status, _ = operator._post(
                source + "/ingest/gateway",
                b"{}",
                {"Authorization": "Bearer synthetic", "X-Hub-Signature-256": "signed"},
            )
    assert status == code
    assert len(received) == 1
    assert leaked == []


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/ingest/gateway",
        "https://user:secret@example.com/ingest/gateway",
        "https://example.com:0/ingest/gateway",
        "https://example.com:/ingest/gateway",
        "https://example.com:65536/ingest/gateway",
        "https://example.com/ingest/gateway?",
        "https://example.com/ingest/gateway#",
        "https://example.com/ingest/gateway\n",
        "https://exam_ple.com/ingest/gateway",
        "https://127.1/ingest/gateway",
        "https://example.com\\evil/ingest/gateway",
    ],
)
def test_unsafe_destination_is_rejected_before_request(operator, url, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("unsafe URL reached the HTTP request boundary")

    monkeypatch.setattr(operator.urllib.request, "Request", unexpected)
    with pytest.raises(ValueError, match="HTTPS"):
        operator._post(url, b"{}", {"Authorization": "Bearer synthetic"})


def test_smoke_validates_base_before_loading_secrets(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "smoke_security_main", ROOT / "scripts/smoke.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["smoke.py", "http://remote.example"])
    monkeypatch.setattr(
        module, "_env", lambda name: pytest.fail("loaded secret before URL validation")
    )
    assert module.main() == 2


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_https_redirect_cannot_downgrade_to_http(operator, code, monkeypatch):
    import io
    from email.message import Message

    # Supply the HTTPS response at urllib's handler seam; a real second
    # loopback endpoint proves that no downgraded request escapes the handler.
    with endpoint() as (destination, leaked):

        class RedirectingTLS(operator.urllib.request.HTTPSHandler):
            def https_open(self, req):
                headers = Message()
                headers["Location"] = destination
                response = operator.urllib.response.addinfourl(
                    io.BytesIO(b"{}"), headers, req.full_url, code
                )
                response.msg = "redirect"
                return response

        build = operator.urllib.request.build_opener
        monkeypatch.setattr(
            operator.urllib.request,
            "build_opener",
            lambda *handlers: build(RedirectingTLS(), *handlers),
        )
        status, _ = operator._post(
            "https://sediment.example/ingest/gateway",
            b"{}",
            {"Authorization": "Bearer synthetic"},
        )
    assert status == code
    assert leaked == []


def test_sim_validates_base_before_loading_credentials(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "sim_security_main", ROOT / "sim/driver/run.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    class Environment(dict):
        def get(self, key, default=None):
            if key in {"SEDIMENT_API_BEARER_TOKEN", "SEDIMENT_GITHUB_WEBHOOK_SECRET"}:
                pytest.fail("loaded credential before URL validation")
            return default

    monkeypatch.setattr(module.os, "environ", Environment())
    assert module.main(["--workdir", "/unused", "--api", "http://remote.example"]) == 2


def test_smoke_uses_the_checkout_wire_fixtures():
    spec = importlib.util.spec_from_file_location(
        "smoke_security_fixtures", ROOT / "scripts/smoke.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (module.FIXTURES / "litellm_standard_logging_object.json").is_file()

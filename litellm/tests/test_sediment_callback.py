# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke tests for the forwarder: POST shape, the fallback payload's
identity stamp, and the never-break-the-proxy contract. The
identity-extraction tests live in
``packages/capture/tests/test_session_identity.py`` with the logic."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

# The callback is deployment glue: the real litellm package only exists on
# the proxy host. Stub the one imported symbol so the forwarder is testable
# here; sender recovery tests use real HTTP and the shared transport.
custom_logger = types.ModuleType("litellm.integrations.custom_logger")


class _CustomLogger:
    pass


custom_logger.CustomLogger = _CustomLogger  # type: ignore[attr-defined]
integrations = types.ModuleType("litellm.integrations")
integrations.custom_logger = custom_logger  # type: ignore[attr-defined]
litellm_pkg = types.ModuleType("litellm")
litellm_pkg.integrations = integrations  # type: ignore[attr-defined]
sys.modules.setdefault("litellm", litellm_pkg)
sys.modules.setdefault("litellm.integrations", integrations)
sys.modules.setdefault("litellm.integrations.custom_logger", custom_logger)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sediment_callback  # noqa: E402
from sediment_capture import resolve_identity  # noqa: E402


def _fire(kwargs: dict, monkeypatch) -> list[tuple[str, dict, dict]]:
    calls = []
    monkeypatch.setenv("SEDIMENT_API_BEARER_TOKEN", "synthetic-token")

    def deliver(request, *, env):
        calls.append(
            (
                request.destination,
                json.loads(request.body),
                {"Authorization": "Bearer " + env["SEDIMENT_API_BEARER_TOKEN"]},
            )
        )
        return sediment_callback.delivery.Disposition(
            request.delivery_id, "acknowledged", "gateway_stored", "fact-id", True
        )

    monkeypatch.setattr(sediment_callback.delivery, "deliver", deliver)
    asyncio.run(
        sediment_callback.handler.async_log_success_event(kwargs, None, 0.0, 0.25)
    )
    return calls


def test_env_url_is_normalized() -> None:
    # Read at import from SEDIMENT_INGEST_URL; the trailing slash is
    # stripped so the /ingest/gateway join cannot double it.
    assert not sediment_callback.SEDIMENT_INGEST_URL.endswith("/")


def test_whitespace_buffer_setting_does_not_start_worker(monkeypatch):
    monkeypatch.setenv("SEDIMENT_DELIVERY_DIR", " \t\n ")
    callback = sediment_callback.SedimentCallback()
    try:
        assert callback._worker is None
    finally:
        callback.close_delivery()


def test_slo_forwarded_verbatim(monkeypatch) -> None:
    # No parsing, no identity logic — the SLO crosses the wire untouched
    # and the server does the rest.
    slo = {
        "litellm_call_id": "call-1",
        "model": "m",
        "metadata": {"requester_metadata": {"session_id": "sess-1"}},
    }
    calls = _fire({"standard_logging_object": slo}, monkeypatch)
    url, body, headers = calls[0]
    assert url.endswith("/ingest/gateway")
    assert body["provider"] == "litellm"
    assert body["payload"] == slo
    assert set(body) == {"provider", "payload", "capture"}
    assert set(body["capture"]) == {"id", "observed_at"}
    assert headers["Authorization"].startswith("Bearer ")


def test_no_session_payload_still_posted(monkeypatch) -> None:
    # The callback does not skip no-session completions — the server skips
    # them observably.
    calls = _fire({"standard_logging_object": {"litellm_call_id": "c2"}}, monkeypatch)
    assert len(calls) == 1


def test_fallback_preserves_identity_shape_for_server_validation(monkeypatch):
    calls = _fire(
        {
            "user": {"nested": True},
            "litellm_params": {"metadata": {"session_id": "session-real"}},
        },
        monkeypatch,
    )
    payload = calls[0][1]["payload"]
    assert payload["end_user"] == {"nested": True}
    identity = resolve_identity(payload)
    assert identity.session_id == "session-real"
    assert identity.user_id is None


def test_fallback_payload_carries_stamped_identity(monkeypatch) -> None:
    # SLO absent → the fallback payload must stamp litellm_params.metadata
    # (as metadata.requester_metadata) and kwargs["user"] (as end_user) — the
    # exact two spots resolve_identity reads. Cross-checked against the real
    # server-side resolver, not a mirror of it.
    kwargs = {
        "litellm_call_id": "c3",
        "model": "m",
        "messages": [],
        "litellm_params": {"metadata": {"session_id": "sess-fb"}},
        "user": "dev-fb",
    }
    calls = _fire(kwargs, monkeypatch)
    _, body, _ = calls[0]
    assert body["payload"]["litellm_call_id"] == "c3"
    identity = resolve_identity(body["payload"])
    assert identity is not None
    assert identity.session_id == "sess-fb"
    assert identity.user_id == "dev-fb"


def test_fallback_does_not_invent_a_provider_call_id() -> None:
    from sediment_capture.gateway import LiteLLMAdapter

    for response, expected in (({"id": "response-only"}, "response-only"), ({}, None)):
        payload = sediment_callback.handler._fallback_payload({}, response, 0.0, 0.25)
        call = LiteLLMAdapter().normalize(
            payload, session_id="session-real", user_id=None, org_id="test"
        )
        assert call.model_call_id == expected


def test_exceptions_are_swallowed(monkeypatch, caplog) -> None:
    # A logging callback must never break the proxy.
    def boom(*args, **kwargs):
        raise RuntimeError("private error with a credential")

    monkeypatch.setattr(sediment_callback.delivery, "deliver", boom)
    asyncio.run(
        sediment_callback.handler.async_log_success_event(
            {"standard_logging_object": {"litellm_call_id": "c4"}}, None, 0.0, 0.25
        )
    )
    assert "preparation_failed" in caplog.text
    assert "private error" not in caplog.text


def test_fallback_payload_carries_custom_headers(monkeypatch) -> None:
    # Codex/pi sessions ride requester_custom_headers, which the resolver
    # reads at the metadata's own top level — the fallback must lift them out
    # of litellm_params.metadata or a header-borne session dies on the
    # no-SLO path.
    session = "0190f5e0-0000-7000-8000-000000000001"
    kwargs = {
        "litellm_call_id": "c6",
        "model": "m",
        "messages": [],
        "litellm_params": {
            "metadata": {"requester_custom_headers": {"x-sediment-session": session}}
        },
    }
    calls = _fire(kwargs, monkeypatch)
    identity = resolve_identity(calls[0][1]["payload"])
    assert identity is not None
    assert identity.session_id == session
    assert identity.source == "sediment_header"


def test_prepared_callback_survives_sender_restart(monkeypatch, tmp_path) -> None:
    import json
    import os
    import shutil
    import subprocess
    import threading
    import time
    from datetime import UTC, datetime
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from sediment_capture.gateway import LiteLLMAdapter

    received = []
    healthy = threading.Event()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(body)
            self.send_response(200 if healthy.is_set() else 503)
            self.end_headers()
            self.wfile.write(b'{"fact_id":"retained-call","stored":true}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    directory = tmp_path / "pending"
    monkeypatch.setenv("SEDIMENT_DELIVERY_DIR", str(directory))
    monkeypatch.setenv("SEDIMENT_INGEST_URL", url)
    monkeypatch.setenv("SEDIMENT_API_BEARER_TOKEN", "original-token")
    monkeypatch.setattr(sediment_callback, "SEDIMENT_INGEST_URL", url)
    slo = {
        "litellm_call_id": "source-call",
        "model": "m",
        "messages": [{"role": "user", "content": "original source"}],
        "metadata": {"requester_metadata": {"session_id": "source-session"}},
    }
    callback = sediment_callback.SedimentCallback()
    before = datetime.now(UTC)
    try:
        asyncio.run(
            callback.async_log_success_event(
                {"standard_logging_object": slo}, None, 0.0, 0.25
            )
        )
        assert directory.exists(), "callback lost the payload when the API failed"
        callback.close_delivery()
        after = datetime.now(UTC)
        from sediment_cli import delivery

        assert delivery.status(directory)["pending"] == 1
        slo["messages"][0]["content"] = "changed after capture"
        healthy.set()
        # A different interpreter owns replay; it doesn't re-import the callback.
        copied = tmp_path / "sediment_delivery.py"
        shutil.copyfile(delivery.__file__, copied)
        time.sleep(2.1)  # permit the first persisted transport backoff to elapse
        result = subprocess.run(
            [sys.executable, str(copied), "replay"],
            env={**os.environ, "SEDIMENT_API_BEARER_TOKEN": "active-token"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["acknowledged"] == 1
        assert len(set(received)) == 1
        body = json.loads(received[-1])
        assert body["payload"]["messages"][0]["content"] == "original source"
        instant = datetime.fromisoformat(body["capture"]["observed_at"])
        assert before <= instant <= after
        from uuid import UUID

        call = LiteLLMAdapter().normalize(
            body["payload"],
            session_id="source-session",
            user_id=None,
            org_id="test",
            capture_id=UUID(body["capture"]["id"]),
            observed_at=instant,
        )
        assert call.model_call_id == "source-call"
        assert call.observed_at == instant
        assert delivery.status(directory)["pending"] == 0
    finally:
        if hasattr(callback, "close_delivery"):
            callback.close_delivery()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_worker_start_failure_keeps_callback_fail_soft(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SEDIMENT_DELIVERY_DIR", str(tmp_path / "pending"))

    def cannot_start(self):
        raise RuntimeError("private worker failure")

    monkeypatch.setattr(sediment_callback.threading.Thread, "start", cannot_start)
    callback = sediment_callback.SedimentCallback()
    callback.close_delivery()
    assert "worker_failed" in caplog.text
    assert "private worker failure" not in caplog.text


def test_running_worker_uses_rotated_credentials(monkeypatch, tmp_path):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received = []
    first = threading.Event()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.headers.get("Authorization"), body))
            self.send_response(503 if len(received) == 1 else 200)
            self.end_headers()
            self.wfile.write(b'{"fact_id":"retained-call","stored":true}')
            first.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    directory = tmp_path / "pending"
    monkeypatch.setenv("SEDIMENT_DELIVERY_DIR", str(directory))
    monkeypatch.setenv("SEDIMENT_INGEST_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("SEDIMENT_API_BEARER_TOKEN", "original-token")
    callback = sediment_callback.SedimentCallback()
    try:
        asyncio.run(callback.async_log_success_event({}, None, 0.0, 0.25))
        assert first.wait(5)
        monkeypatch.setenv("SEDIMENT_API_BEARER_TOKEN", "rotated-token")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if sediment_callback.delivery.status(directory)["pending"] == 0:
                break
            time.sleep(0.05)
        assert [token for token, _ in received] == [
            "Bearer original-token",
            "Bearer rotated-token",
        ]
        assert len({body for _, body in received}) == 1
        assert sediment_callback.delivery.status(directory)["pending"] == 0
    finally:
        callback.close_delivery()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_callback_does_not_keep_revoked_credential(monkeypatch):
    monkeypatch.setenv("SEDIMENT_API_BEARER_TOKEN", "temporary-token")
    environment = sediment_callback.handler._environment()
    monkeypatch.delenv("SEDIMENT_API_BEARER_TOKEN")
    assert not environment.get("SEDIMENT_API_BEARER_TOKEN")


def test_callback_ignores_checkout_command_shim_on_import_path(tmp_path):
    import os
    import subprocess

    root = Path(__file__).resolve().parents[2]
    probe = """
import sys, types
custom = types.ModuleType('litellm.integrations.custom_logger')
custom.CustomLogger = type('CustomLogger', (), {})
sys.modules['litellm.integrations.custom_logger'] = custom
import sediment_callback
from sediment_cli import delivery
assert sediment_callback.delivery is delivery
sediment_callback.handler.close_delivery()
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (str(root / "scripts"), str(root / "litellm"))
            ),
            "SEDIMENT_DELIVERY_DIR": "",
            "SEDIMENT_API_BEARER_TOKEN": "synthetic-token",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_raw_capture_is_private_at_creation(monkeypatch, tmp_path):
    import os

    directory = tmp_path / "fixtures"
    monkeypatch.setattr(sediment_callback, "CAPTURE_DIR", str(directory))
    previous = os.umask(0o022)
    try:
        assert _fire({"standard_logging_object": {"private": "source"}}, monkeypatch)
    finally:
        os.umask(previous)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert {p.name for p in directory.iterdir()} == {
        "standard_logging_object.json",
        "fallback_payload.json",
    }
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())


@pytest.mark.parametrize(
    "unsafe",
    [
        "directory_mode",
        "file_mode",
        "directory_link",
        "ancestor_link",
        "file_link",
        "hardlink",
    ],
)
def test_unsafe_raw_capture_never_writes_but_delivery_continues(
    monkeypatch, tmp_path, unsafe, caplog
):
    import os

    directory = tmp_path / "fixtures"
    directory.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    target = directory / "standard_logging_object.json"
    if unsafe == "directory_mode":
        directory.chmod(0o755)
    elif unsafe == "file_mode":
        target.write_text("unchanged")
        target.chmod(0o644)
    elif unsafe == "directory_link":
        link = tmp_path / "link"
        link.symlink_to(directory, target_is_directory=True)
        directory = link
    elif unsafe == "ancestor_link":
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        directory = link / "fixtures"
    elif unsafe == "file_link":
        target.symlink_to(outside)
    else:
        outside.chmod(0o600)
        os.link(outside, target)
    monkeypatch.setattr(sediment_callback, "CAPTURE_DIR", str(directory))
    assert _fire({"standard_logging_object": {"private": "source"}}, monkeypatch)
    assert outside.read_text() == "unchanged"
    assert "fixture_write_failed" in caplog.text
    assert "source" not in caplog.text
    assert not (directory / "fallback_payload.json").exists()
    if unsafe == "file_mode":
        assert target.read_text() == "unchanged"

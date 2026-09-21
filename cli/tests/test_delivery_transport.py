# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prepared HTTP bytes survive bounded private transport and process replay."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def test_shared_owner_exists():
    assert importlib.util.find_spec("sediment_cli.delivery") is not None


@pytest.fixture
def delivery():
    assert importlib.util.find_spec("sediment_cli.delivery") is not None, (
        "the shared delivery owner is missing"
    )
    return importlib.import_module("sediment_cli.delivery")


@contextmanager
def server(replies):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, body, self.headers.get("Authorization")))
            code, reply = replies[min(len(received) - 1, len(replies) - 1)]
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", received
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


def test_enqueue_publishes_private_exact_bytes_without_network(delivery, tmp_path):
    queue = tmp_path / "queue"
    body = b'{ "resourceLogs": [], "text":"private source" }\n'
    request = delivery.prepare_request("otlp", "http://127.0.0.1:9", body)
    receipt = delivery.enqueue(request, queue)
    assert receipt.as_dict() == {
        "format_version": 1,
        "delivery_id": request.delivery_id,
        "status": "queued",
        "reason": "buffered",
    }
    assert queue.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in queue.iterdir())
    assert delivery.status(queue)["pending"] == 1
    assert delivery.status(queue)["pending_bytes"] == len(body)
    assert delivery.status(queue)["worker_running"] is False
    assert sum(path.read_bytes().count(body) for path in queue.iterdir()) == 1


def test_status_never_enables_buffering(delivery, tmp_path):
    path = tmp_path / "missing"
    result = delivery.status(path)
    assert result["pending"] == result["blocked"] == result["pending_bytes"] == 0
    assert result["worker_running"] is False
    assert not path.exists()


def test_restart_replays_exact_bytes_using_active_credentials(delivery, tmp_path):
    queue = tmp_path / "queue"
    body = b'{"resourceLogs":[]}\n '
    with server([(503, b"private failure body"), (200, b"{}")]) as (url, received):
        request = delivery.prepare_request("otlp", url, body)
        assert delivery.enqueue(request, queue).status == "queued"
        first = delivery.replay(
            queue, env={"SEDIMENT_OTLP_ENDPOINT": url, "SEDIMENT_INGEST_TOKEN": "old"}
        )
        assert first["attempted"] == 1
        assert delivery.status(queue)["pending"] == 1
        env = {
            **os.environ,
            "SEDIMENT_DELIVERY_DIR": str(queue),
            "SEDIMENT_OTLP_ENDPOINT": url,
            "SEDIMENT_INGEST_TOKEN": "rotated",
        }
        # Backoff is persisted, not reset by restarting a worker.
        early = subprocess.run(
            [sys.executable, "-m", "sediment_cli.delivery", "replay"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert early.returncode == 0
        assert json.loads(early.stdout)["attempted"] == 0
        # Advance the scheduling clock, keeping the original record immutable.
        original = delivery.time.time
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(delivery.time, "time", lambda: original() + 65)
            result = delivery.replay(queue, env=env)
        assert result["attempted"] == 1
        assert received == [
            ("/v1/logs", body, "Bearer old"),
            ("/v1/logs", body, "Bearer rotated"),
        ]
    summary = delivery.status(queue)
    assert summary["pending"] == 0
    assert summary["receipts"] == {"otlp_delivered": 1}
    persisted = b"".join(path.read_bytes() for path in queue.iterdir())
    assert (
        body not in persisted
        and b"rotated" not in persisted
        and b"private failure" not in persisted
    )


@pytest.mark.parametrize(
    "channel,reply,status,reason",
    [
        (
            "gateway",
            b'{"fact_id":"retained","stored":true}',
            "acknowledged",
            "gateway_stored",
        ),
        (
            "gateway",
            b'{"fact_id":"retained","stored":false}',
            "acknowledged",
            "gateway_duplicate",
        ),
        ("gateway", b'{"skipped":true,"reason":"no_session"}', "skipped", "no_session"),
        ("otlp", b"{}", "acknowledged", "otlp_delivered"),
        ("gateway", b"{}", "pending", "invalid_acknowledgment"),
        ("gateway", b'{"fact_id":"x","stored":1}', "pending", "invalid_acknowledgment"),
        (
            "gateway",
            b'{"skipped":true,"reason":"unexpected"}',
            "pending",
            "invalid_acknowledgment",
        ),
        ("otlp", b'{"partialSuccess":{}}', "pending", "invalid_acknowledgment"),
        (
            "gateway",
            b'{"stored":true,"stored":false,"fact_id":"x"}',
            "pending",
            "invalid_acknowledgment",
        ),
    ],
)
def test_acknowledgments_do_not_overclaim_facts(
    delivery, channel, reply, status, reason
):
    with server([(200, reply)]) as (url, received):
        env = (
            {"SEDIMENT_INGEST_URL": url, "SEDIMENT_API_BEARER_TOKEN": "token"}
            if channel == "gateway"
            else {"SEDIMENT_OTLP_ENDPOINT": url, "SEDIMENT_INGEST_TOKEN": "token"}
        )
        result = delivery.send_once(
            delivery.prepare_request(channel, url, b"{}"), env=env
        )
        assert (result.status, result.reason) == (status, reason)
        assert len(received) == 1
        if reason.startswith("gateway_"):
            assert result.fact_id == "retained"
        else:
            assert "fact_id" not in result.as_dict()


def test_blocked_auth_needs_deliberate_replay(delivery, tmp_path):
    queue = tmp_path / "queue"
    with server([(401, b"secret response"), (200, b"{}")]) as (url, received):
        env = {"SEDIMENT_OTLP_ENDPOINT": url, "SEDIMENT_INGEST_TOKEN": "token"}
        delivery.enqueue(delivery.prepare_request("otlp", url, b"{}"), queue)
        assert delivery.replay(queue, env=env)["attempted"] == 1
        assert delivery.status(queue)["blocked"] == 1
        assert delivery.replay(queue, env=env)["attempted"] == 0
        assert delivery.replay(queue, env=env, retry_blocked=True)["attempted"] == 1
        assert len(received) == 2


def test_missing_or_changed_configuration_never_sends(delivery, tmp_path):
    queue = tmp_path / "queue"
    with server([(200, b"{}")]) as (url, received):
        request = delivery.prepare_request("otlp", url, b"{}")
        assert delivery.send_once(request, env={}).reason == "configuration_missing"
        delivery.enqueue(request, queue)
        result = delivery.replay(queue, env={"SEDIMENT_OTLP_ENDPOINT": url + "evil"})
        assert result["attempted"] == 0
        assert delivery.status(queue)["blocked"] == 1
        assert received == []


@pytest.mark.parametrize(
    "channel,path", [("otlp", "/v1/logs"), ("gateway", "/ingest/gateway")]
)
def test_replay_normalizes_supported_endpoint_formats(
    delivery, tmp_path, channel, path
):
    reply = b"{}" if channel == "otlp" else b'{"fact_id":"retained","stored":true}'
    with server([(200, reply)]) as (url, received):
        request = delivery.prepare_request(channel, url + path, b"{}")
        queue = tmp_path / "queue"
        delivery.enqueue(request, queue)
        configured = " \t" + url + path + "/ \n"
        env = (
            {"SEDIMENT_OTLP_ENDPOINT": configured, "SEDIMENT_INGEST_TOKEN": "active"}
            if channel == "otlp"
            else {
                "SEDIMENT_INGEST_URL": configured,
                "SEDIMENT_API_BEARER_TOKEN": "active",
            }
        )
        assert delivery.replay(queue, env=env)["acknowledged"] == 1
        assert received == [(path, b"{}", "Bearer active")]


def test_private_directory_and_entries_are_required(delivery, tmp_path):
    request = delivery.prepare_request("otlp", "http://127.0.0.1:9", b"{}")
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    assert delivery.enqueue(request, unsafe).reason == "unsafe_storage"
    safe = tmp_path / "safe"
    safe.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(safe, target_is_directory=True)
    assert delivery.enqueue(request, link).reason == "unsafe_storage"
    (safe / "foreign").symlink_to(tmp_path / "unrelated")
    assert delivery.enqueue(request, safe).reason == "unsafe_storage"
    assert not (tmp_path / "unrelated").exists()


def test_capacity_declines_without_evicting_entries(delivery, tmp_path, monkeypatch):
    queue = tmp_path / "queue"
    monkeypatch.setattr(delivery, "MAX_ACTIVE_ENTRIES", 1)
    first = delivery.prepare_request("otlp", "http://127.0.0.1:9", b"one")
    second = delivery.prepare_request("otlp", "http://127.0.0.1:9", b"two")
    assert delivery.enqueue(first, queue).status == "queued"
    assert delivery.enqueue(second, queue).reason == "buffer_full"
    assert delivery.status(queue)["pending_bytes"] == 3
    assert any(b"one" in path.read_bytes() for path in queue.iterdir())


def test_same_transport_id_cannot_replace_prepared_payload(delivery, tmp_path):
    request = delivery.prepare_request("otlp", "http://127.0.0.1:9", b"one")
    assert delivery.enqueue(request, tmp_path / "queue").reason == "buffered"
    assert delivery.enqueue(request, tmp_path / "queue").reason == "already_buffered"
    changed = delivery.prepare_request(
        "otlp",
        "http://127.0.0.1:9",
        b"two",
        delivery_id=request.delivery_id,
        captured_at=request.captured_at,
    )
    assert delivery.enqueue(changed, tmp_path / "queue").reason == "identity_conflict"


def test_expiration_removes_body_and_bounds_receipts(delivery, tmp_path, monkeypatch):
    queue = tmp_path / "queue"
    request = delivery.prepare_request(
        "otlp", "http://127.0.0.1:9", b"private expired body"
    )
    delivery.enqueue(request, queue)
    original = delivery.time.time
    monkeypatch.setattr(
        delivery.time, "time", lambda: original() + delivery.REPLAY_WINDOW_SECONDS + 1
    )
    assert delivery.replay(queue, env={})["attempted"] == 0
    assert delivery.status(queue)["receipts"] == {"expired": 1}
    assert b"private expired body" not in b"".join(
        p.read_bytes() for p in queue.iterdir()
    )
    monkeypatch.setattr(
        delivery.time,
        "time",
        lambda: (
            original()
            + delivery.REPLAY_WINDOW_SECONDS
            + delivery.RECEIPT_RETENTION_SECONDS
            + 2
        ),
    )
    delivery.replay(queue, env={})
    assert delivery.status(queue)["receipts"] == {}


def test_cli_request_roundtrip_and_bounded_stdin(delivery, tmp_path):
    queue = tmp_path / "queue"
    request = delivery.prepare_request("otlp", "http://127.0.0.1:9", b' {"x":1}\n')
    env = {**os.environ, "SEDIMENT_DELIVERY_DIR": str(queue)}
    result = subprocess.run(
        [sys.executable, "-m", "sediment_cli.delivery", "enqueue"],
        input=json.dumps(request.as_dict()),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "queued"
    invalid = {**request.as_dict(), "authorization": "secret"}
    result = subprocess.run(
        [sys.executable, "-m", "sediment_cli.delivery", "enqueue"],
        input=json.dumps(invalid),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 1
    assert "secret" not in result.stdout + result.stderr


def test_direct_mode_does_not_create_storage(delivery, tmp_path, caplog):
    with server([(200, b"{}")]) as (url, _):
        request = delivery.prepare_request("otlp", url, b"{}")
        result = delivery.deliver(
            request,
            env={"SEDIMENT_OTLP_ENDPOINT": url, "SEDIMENT_INGEST_TOKEN": "token"},
        )
        assert result.reason == "otlp_delivered"
    assert "best_effort" in caplog.text
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "fault", ["unsafe_storage", "storage_unavailable", "storage_busy"]
)
@pytest.mark.parametrize("channel", ["otlp", "gateway"])
def test_storage_fault_falls_back_once_with_original_request(
    delivery, tmp_path, monkeypatch, caplog, fault, channel
):
    import errno
    import fcntl

    queue = tmp_path / "queue"
    queue.mkdir(mode=0o755 if fault == "unsafe_storage" else 0o700)
    lock = None
    if fault == "storage_unavailable":

        def disk_full(*args):
            raise OSError(errno.ENOSPC, "synthetic private filesystem detail")

        monkeypatch.setattr(delivery._Queue, "atomic", disk_full)
    elif fault == "storage_busy":
        lock = os.open(queue / "enqueue.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    body = b'{ "source": "synthetic private payload", "timeUnixNano":"123" }\n'
    reply = b"{}" if channel == "otlp" else b'{"fact_id":"retained","stored":false}'
    try:
        with server([(200, reply)]) as (url, received):
            request = delivery.prepare_request(channel, url, body)
            env = {
                "SEDIMENT_DELIVERY_DIR": str(queue),
                "SEDIMENT_OTLP_ENDPOINT": url,
                "SEDIMENT_INGEST_URL": url,
                "SEDIMENT_INGEST_TOKEN": "synthetic-token",
                "SEDIMENT_API_BEARER_TOKEN": "synthetic-token",
            }
            result = delivery.deliver(request, env=env)
            assert result.status == "acknowledged"
            assert result.delivery_id == request.delivery_id
            assert result.as_dict()["fallback_reason"] == fault
            assert received == [
                (request.destination.removeprefix(url), body, "Bearer synthetic-token")
            ]
            assert (
                "mode=best_effort" in caplog.text and f"reason={fault}" in caplog.text
            )
            assert "synthetic private" not in caplog.text
            assert "synthetic-token" not in caplog.text
            assert not list(queue.glob("*.entry"))
            if channel == "gateway":
                assert result.fact_id == "retained" and result.stored is False
    finally:
        if lock is not None:
            os.close(lock)


@pytest.mark.parametrize(
    "fault", ["buffer_full", "entry_too_large", "identity_conflict"]
)
def test_terminal_enqueue_decline_never_falls_back(
    delivery, tmp_path, monkeypatch, fault
):
    queue = tmp_path / "queue"
    with server([(200, b"{}")]) as (url, received):
        request = delivery.prepare_request("otlp", url, b"original")
        if fault == "buffer_full":
            monkeypatch.setattr(delivery, "MAX_ACTIVE_ENTRIES", 0)
        elif fault == "entry_too_large":
            monkeypatch.setattr(delivery, "MAX_ENTRY_BYTES", 1)
        else:
            assert delivery.enqueue(request, queue).status == "queued"
            from dataclasses import replace

            request = replace(request, body=b"contradictory")
        result = delivery.deliver(
            request,
            env={
                "SEDIMENT_DELIVERY_DIR": str(queue),
                "SEDIMENT_OTLP_ENDPOINT": url,
                "SEDIMENT_INGEST_TOKEN": "token",
            },
        )
        assert (result.status, result.reason) == ("declined", fault)
        assert "fallback_reason" not in result.as_dict()
        assert received == []


@pytest.mark.parametrize("reply", [(503, b"{}"), (401, b"{}"), (200, b"[]")])
def test_failed_fallback_is_not_queued_or_acknowledged(delivery, tmp_path, reply):
    queue = tmp_path / "unsafe"
    queue.mkdir(mode=0o755)
    with server([reply]) as (url, received):
        result = delivery.deliver(
            delivery.prepare_request("otlp", url, b"{}"),
            env={
                "SEDIMENT_DELIVERY_DIR": str(queue),
                "SEDIMENT_OTLP_ENDPOINT": url,
                "SEDIMENT_INGEST_TOKEN": "token",
            },
        )
        assert result.status in {"pending", "blocked"}
        assert result.as_dict()["fallback_reason"] == "unsafe_storage"
        assert len(received) == 1
        assert list(queue.iterdir()) == []


def test_full_buffer_receipt_failure_preserves_terminal_decline(
    delivery, tmp_path, monkeypatch, caplog
):
    import errno

    monkeypatch.setattr(delivery, "MAX_ACTIVE_ENTRIES", 0)
    original = delivery._Queue.atomic

    def receipt_disk_full(queue, name, data):
        if name.endswith(".receipt"):
            raise OSError(errno.ENOSPC, "synthetic private filesystem detail")
        return original(queue, name, data)

    monkeypatch.setattr(delivery._Queue, "atomic", receipt_disk_full)
    with server([(200, b"{}")]) as (url, received):
        request = delivery.prepare_request("otlp", url, b"original")
        result = delivery.deliver(
            request,
            env={
                "SEDIMENT_DELIVERY_DIR": str(tmp_path / "queue"),
                "SEDIMENT_OTLP_ENDPOINT": url,
                "SEDIMENT_INGEST_TOKEN": "token",
            },
        )
        assert (result.status, result.reason) == ("declined", "buffer_full")
        assert "fallback_reason" not in result.as_dict()
        assert received == []
        assert "storage_unavailable" in caplog.text
        assert "synthetic private filesystem detail" not in caplog.text


def test_whitespace_buffer_setting_is_direct_without_files(
    delivery, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    with server([(200, b"{}")]) as (url, received):
        result = delivery.deliver(
            delivery.prepare_request("otlp", url, b"{}"),
            env={
                "SEDIMENT_DELIVERY_DIR": " \t\n ",
                "SEDIMENT_OTLP_ENDPOINT": url,
                "SEDIMENT_INGEST_TOKEN": "token",
            },
        )
        assert result.status == "acknowledged"
        assert len(received) == 1
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("allow_fallback", [False, True])
def test_cli_strict_enqueue_and_explicit_fallback(delivery, tmp_path, allow_fallback):
    queue = tmp_path / "unsafe"
    queue.mkdir(mode=0o755)
    with server([(200, b"{}")]) as (url, received):
        request = delivery.prepare_request("otlp", url, b"original bytes")
        result = subprocess.run(
            [sys.executable, delivery.__file__, "enqueue", "--directory", str(queue)]
            + (["--fallback-direct"] if allow_fallback else []),
            input=json.dumps(request.as_dict()),
            text=True,
            capture_output=True,
            timeout=10,
            env={"SEDIMENT_OTLP_ENDPOINT": url, "SEDIMENT_INGEST_TOKEN": "token"},
        )
        assert result.returncode == (0 if allow_fallback else 1)
        disposition = json.loads(result.stdout)
        assert disposition["delivery_id"] == request.delivery_id
        assert disposition["status"] == (
            "acknowledged" if allow_fallback else "declined"
        )
        assert len(received) == int(allow_fallback)
        if allow_fallback:
            assert disposition["fallback_reason"] == "unsafe_storage"
        assert list(queue.iterdir()) == []


def test_cli_whitespace_setting_is_disabled(delivery, tmp_path):
    result = subprocess.run(
        [sys.executable, delivery.__file__, "status"],
        cwd=tmp_path,
        env={"SEDIMENT_DELIVERY_DIR": " \t\n "},
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["reason"] == "buffer_disabled"
    assert list(tmp_path.iterdir()) == []


def test_status_existing_empty_directory_does_not_write(delivery, tmp_path):
    queue = tmp_path / "queue"
    queue.mkdir(mode=0o700)
    assert delivery.status(queue)["pending"] == 0
    assert list(queue.iterdir()) == []


def test_malformed_receipt_cannot_delete_pending_payload(delivery, tmp_path):
    queue = tmp_path / "queue"
    request = delivery.prepare_request("otlp", "http://127.0.0.1:9", b"preserve me")
    delivery.enqueue(request, queue)
    receipt = queue / (request.delivery_id + ".receipt")
    receipt.write_text("{}")
    receipt.chmod(0o600)
    with pytest.raises(delivery.DeliveryError, match="invalid_record"):
        delivery.replay(queue, env={})
    assert (queue / (request.delivery_id + ".entry")).exists()


def test_corrupt_state_is_declined_without_starving_other_entries(delivery, tmp_path):
    queue = tmp_path / "queue"
    with server([(200, b"{}")]) as (url, received):
        first = delivery.prepare_request("otlp", url, b"first")
        second = delivery.prepare_request("otlp", url, b"second")
        delivery.enqueue(first, queue)
        delivery.enqueue(second, queue)
        state = queue / (first.delivery_id + ".state")
        state.write_text('{"status":[]}')
        state.chmod(0o600)
        result = delivery.replay(
            queue, env={"SEDIMENT_OTLP_ENDPOINT": url, "SEDIMENT_INGEST_TOKEN": "t"}
        )
        assert result["attempted"] == 1
        assert received[0][1] == b"second"
        assert delivery.status(queue)["receipts"] == {
            "invalid_record": 1,
            "otlp_delivered": 1,
        }


def test_watch_owns_worker_between_batches_and_stops(delivery, tmp_path):
    queue = tmp_path / "queue"
    request = delivery.prepare_request("otlp", "http://127.0.0.1:9", b"{}")
    delivery.enqueue(request, queue)
    stopped = threading.Event()
    ready = threading.Event()
    results = []
    with pytest.MonkeyPatch.context() as patch:
        original = delivery._configuration

        def configuration(*args):
            ready.set()
            return original(*args)

        patch.setattr(delivery, "_configuration", configuration)
        worker = threading.Thread(
            target=lambda: results.append(
                delivery.watch(queue, env={}, stop_event=stopped)
            )
        )
        worker.start()
        try:
            assert ready.wait(3)
            assert delivery.status(queue)["worker_running"] is True
            assert delivery.replay(queue, env={})["worker_busy"] is True
            # Enqueue remains available while the worker owns its independent lock.
            another = delivery.prepare_request("otlp", "http://127.0.0.1:9", b"second")
            assert delivery.enqueue(another, queue).status == "queued"
        finally:
            stopped.set()
            worker.join(timeout=3)
        assert not worker.is_alive()
    assert delivery.status(queue)["worker_running"] is False


def test_response_header_drip_cannot_extend_total_timeout(delivery, monkeypatch):
    import socket
    import time

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stopped = threading.Event()

    def serve():
        with listener.accept()[0] as connection:
            connection.recv(65536)
            for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}":
                if stopped.wait(0.03):
                    return
                try:
                    connection.sendall(bytes([byte]))
                except OSError:
                    return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(delivery, "HTTP_TIMEOUT_SECONDS", 0.15)
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    started = time.monotonic()
    try:
        result = delivery.send_once(
            delivery.prepare_request("otlp", url, b"{}"),
            env={"SEDIMENT_OTLP_ENDPOINT": url, "SEDIMENT_INGEST_TOKEN": "t"},
        )
        assert result.reason == "transport_failure"
        assert time.monotonic() - started < 0.5
    finally:
        stopped.set()
        listener.close()
        thread.join(timeout=2)


def test_untrusted_nested_json_and_huge_numbers_are_declined(delivery, tmp_path):
    with server([(200, b"[" * 2000 + b"]" * 2000)]) as (url, _):
        result = delivery.send_once(
            delivery.prepare_request("otlp", url, b"{}"),
            env={"SEDIMENT_OTLP_ENDPOINT": url, "SEDIMENT_INGEST_TOKEN": "t"},
        )
        assert result.reason == "invalid_acknowledgment"
    assert delivery._number(10**1000) is False
    for field in ({"channel": []}, {"destination": []}, {"body_base64": []}):
        with pytest.raises(delivery.DeliveryError):
            delivery.request_from_dict(
                {
                    "channel": "otlp",
                    "destination": "http://localhost:9",
                    "body_base64": "e30=",
                    **field,
                }
            )


def _process(code, env, *, stdin=None):
    return subprocess.run(
        [sys.executable, "-c", code],
        input=stdin,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_process_death_before_publication_never_sends_partial_body(delivery, tmp_path):
    queue = tmp_path / "queue"
    with server([(200, b"{}")]) as (url, received):
        request = delivery.prepare_request("otlp", url, b"complete prepared body")
        code = """
import json, os, stat, sys
from sediment_cli import delivery as d
original = d.os.fsync
def interrupted(fd):
    original(fd)
    if stat.S_ISREG(os.fstat(fd).st_mode):
        os._exit(73)
d.os.fsync = interrupted
d.enqueue(d.request_from_dict(json.load(sys.stdin)), os.environ['SEDIMENT_DELIVERY_DIR'])
"""
        env = {
            "SEDIMENT_DELIVERY_DIR": str(queue),
            "SEDIMENT_OTLP_ENDPOINT": url,
            "SEDIMENT_INGEST_TOKEN": "t",
        }
        interrupted = _process(code, env, stdin=json.dumps(request.as_dict()))
        assert interrupted.returncode == 73
        assert list(queue.glob(".tmp-*"))
        assert delivery.status(queue)["pending"] == 0
        assert delivery.replay(queue, env=env)["attempted"] == 0
        assert received == []
        assert not list(queue.glob(".tmp-*"))
        assert delivery.enqueue(request, queue).status == "queued"
        assert delivery.replay(queue, env=env)["attempted"] == 1
        assert received[0][1] == request.body


@pytest.mark.parametrize("crash_after_receipt", [False, True])
def test_process_death_at_ack_boundary_preserves_transport_truth(
    delivery, tmp_path, crash_after_receipt
):
    queue = tmp_path / "queue"
    body = b'{ "capture_id":"unchanged", "time":123 }\n'
    with server(
        [
            (200, b'{"fact_id":"retained","stored":true}'),
            (200, b'{"fact_id":"retained","stored":false}'),
        ]
    ) as (url, received):
        request = delivery.prepare_request("gateway", url, body)
        delivery.enqueue(request, queue)
        setup = (
            """
original = d._Queue.remove
def stop(self, name):
    if name.endswith('.entry'):
        os._exit(73)
    return original(self, name)
d._Queue.remove = stop
"""
            if crash_after_receipt
            else """
def stop(*args):
    os._exit(73)
d._receipt = stop
"""
        )
        code = (
            "import os\nfrom sediment_cli import delivery as d\n"
            + setup
            + "d.replay(os.environ['SEDIMENT_DELIVERY_DIR'])\n"
        )
        env = {
            "SEDIMENT_DELIVERY_DIR": str(queue),
            "SEDIMENT_INGEST_URL": url,
            "SEDIMENT_API_BEARER_TOKEN": "t",
        }
        assert _process(code, env).returncode == 73
        assert (queue / (request.delivery_id + ".entry")).exists()
        resumed = subprocess.run(
            [sys.executable, "-m", "sediment_cli.delivery", "replay"],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert resumed.returncode == 0
        assert len(received) == (1 if crash_after_receipt else 2)
        assert all(row[1] == body for row in received)
        assert delivery.status(queue)["pending"] == 0
        receipt = json.loads((queue / (request.delivery_id + ".receipt")).read_text())
        assert receipt["delivery_id"] == request.delivery_id
        assert receipt["fact_id"] == "retained"
        assert receipt["stored"] is crash_after_receipt


def test_real_watch_process_owns_lock_and_sigterm_is_graceful(delivery, tmp_path):
    import time

    queue = tmp_path / "queue"
    env = {
        **os.environ,
        "SEDIMENT_DELIVERY_DIR": str(queue),
        "SEDIMENT_OTLP_ENDPOINT": "http://127.0.0.1:9",
        "SEDIMENT_INGEST_TOKEN": "t",
    }
    worker = subprocess.Popen(
        [sys.executable, "-m", "sediment_cli.delivery", "replay", "--watch"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 3
        while (
            not delivery.status(queue)["worker_running"] and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert delivery.status(queue)["worker_running"]
        assert delivery.replay(queue, env=env)["worker_busy"]
        worker.terminate()
        output, errors = worker.communicate(timeout=3)
        assert worker.returncode == 0, errors
        assert "attempted" in json.loads(output)
        assert not delivery.status(queue)["worker_running"]
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=3)


def test_batch_limit_and_retryable_failure_do_not_starve_siblings(delivery, tmp_path):
    queue = tmp_path / "queue"
    with server([(503, b"fail")] + [(200, b"{}")] * 33) as (url, received):
        env = {"SEDIMENT_OTLP_ENDPOINT": url, "SEDIMENT_INGEST_TOKEN": "t"}
        requests = [
            delivery.prepare_request("otlp", url, str(i).encode()) for i in range(34)
        ]
        for request in requests:
            assert delivery.enqueue(request, queue).status == "queued"
        result = delivery.replay(queue, env=env)
        assert result["attempted"] == 32
        assert result["pending"] == 1
        assert result["acknowledged"] == 31
        assert received[0][1] == b"0"
        assert delivery.status(queue)["pending"] == 3
        result = delivery.replay(queue, env=env)
        assert result["attempted"] == result["acknowledged"] == 2
        assert delivery.status(queue)["pending"] == 1


def test_byte_capacity_receipt_cap_and_fixed_defaults(delivery, tmp_path, monkeypatch):
    assert (
        delivery.MAX_ACTIVE_BYTES,
        delivery.MAX_ACTIVE_ENTRIES,
        delivery.MAX_ENTRY_BYTES,
    ) == (256 * 1024 * 1024, 2048, 8 * 1024 * 1024)
    assert (delivery.MAX_BATCH_ATTEMPTS, delivery.MAX_BACKOFF_SECONDS) == (32, 60)
    queue = tmp_path / "queue"
    monkeypatch.setattr(delivery, "MAX_ACTIVE_BYTES", 5)
    monkeypatch.setattr(delivery, "MAX_RECEIPTS", 2)
    assert (
        delivery.enqueue(
            delivery.prepare_request("otlp", "http://localhost:9", b"12345"), queue
        ).status
        == "queued"
    )
    for _ in range(4):
        assert (
            delivery.enqueue(
                delivery.prepare_request("otlp", "http://localhost:9", b"x"), queue
            ).reason
            == "buffer_full"
        )
    assert delivery.status(queue)["active_bytes"] == 5
    assert delivery.status(queue)["receipts"] == {"buffer_full": 2}


def test_standalone_file_and_public_cli_dispatch(delivery, tmp_path):
    standalone = tmp_path / "delivery.py"
    standalone.write_bytes(Path(delivery.__file__).read_bytes())
    queue = tmp_path / "queue"
    request = delivery.prepare_request("otlp", "http://localhost:9", b"{}")
    run = subprocess.run(
        [sys.executable, "-I", str(standalone), "enqueue", "--directory", str(queue)],
        input=json.dumps(request.as_dict()),
        capture_output=True,
        text=True,
        timeout=10,
        cwd=tmp_path,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["delivery_id"] == request.delivery_id
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "sediment_cli.cli",
            "delivery",
            "status",
            "--directory",
            str(queue),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["pending"] == 1


def test_ancestor_symlink_and_nonterminal_receipt_are_rejected(delivery, tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    request = delivery.prepare_request("otlp", "http://localhost:9", b"preserve")
    assert delivery.enqueue(request, link / "queue").reason == "unsafe_storage"
    assert not (target / "queue").exists()
    queue = tmp_path / "queue"
    delivery.enqueue(request, queue)
    receipt = queue / (request.delivery_id + ".receipt")
    receipt.write_text(
        json.dumps(
            {
                "format_version": 1,
                "delivery_id": request.delivery_id,
                "completed_at": delivery.time.time(),
                "status": "declined",
                "reason": "buffered",
            }
        )
    )
    receipt.chmod(0o600)
    with pytest.raises(delivery.DeliveryError, match="invalid_record"):
        delivery.replay(queue, env={})
    assert (queue / (request.delivery_id + ".entry")).exists()


def test_slow_failing_full_batch_cannot_starve_fresh_entry(
    delivery, tmp_path, monkeypatch
):
    queue = tmp_path / "queue"
    clock = [delivery.time.time()]
    monkeypatch.setattr(delivery.time, "time", lambda: clock[0])
    original = delivery.send_once

    def slow_failure(request, *, env):
        result = original(request, env=env)
        if result.status == "pending":
            clock[0] += 5
        return result

    monkeypatch.setattr(delivery, "send_once", slow_failure)
    # Model elapsed time for the actual HTTP failures; don't spend160s sleeping.
    with server([(503, b"{}")] * 32 + [(200, b"{}")]) as (url, received):
        env = {"SEDIMENT_OTLP_ENDPOINT": url, "SEDIMENT_INGEST_TOKEN": "t"}
        for index in range(33):
            delivery.enqueue(
                delivery.prepare_request("otlp", url, str(index).encode()), queue
            )
            clock[0] += 1
        first = delivery.replay(queue, env=env)
        assert first["attempted"] == first["pending"] == 32
        assert received[-1][1] == b"31"
        delivery.replay(queue, env=env)
        assert received[32][1] == b"32"


def test_expiration_is_rechecked_before_each_http_attempt(
    delivery, tmp_path, monkeypatch
):
    queue = tmp_path / "queue"
    clock = [delivery.time.time()]
    monkeypatch.setattr(delivery.time, "time", lambda: clock[0])
    original = delivery.send_once

    def cross_expiration(request, *, env):
        result = original(request, env=env)
        clock[0] += 5
        return result

    monkeypatch.setattr(delivery, "send_once", cross_expiration)
    with server([(200, b"{}")]) as (url, received):
        env = {"SEDIMENT_OTLP_ENDPOINT": url, "SEDIMENT_INGEST_TOKEN": "t"}
        for body in (b"first", b"second"):
            delivery.enqueue(delivery.prepare_request("otlp", url, body), queue)
            clock[0] += 0.01
        clock[0] += delivery.REPLAY_WINDOW_SECONDS - 2
        assert delivery.replay(queue, env=env)["attempted"] == 1
        assert len(received) == 1
        assert delivery.status(queue)["receipts"] == {"otlp_delivered": 1, "expired": 1}


def test_dns_timeout_has_no_late_payload_and_bounded_resolver(delivery, monkeypatch):
    import time

    release = threading.Event()
    entered = threading.Event()
    original = delivery.socket.getaddrinfo
    calls = []

    def blocked(*args, **kwargs):
        calls.append(args)
        entered.set()
        release.wait(3)
        return original(*args, **kwargs)

    with server([(200, b"{}")]) as (url, received):
        monkeypatch.setattr(delivery.socket, "getaddrinfo", blocked)
        monkeypatch.setattr(delivery, "HTTP_TIMEOUT_SECONDS", 0.1)
        request = delivery.prepare_request("otlp", url, b"{}")
        env = {"SEDIMENT_OTLP_ENDPOINT": url, "SEDIMENT_INGEST_TOKEN": "t"}
        try:
            started = time.monotonic()
            assert delivery.send_once(request, env=env).reason == "transport_failure"
            assert entered.is_set()
            assert delivery.send_once(request, env=env).reason == "transport_failure"
            assert time.monotonic() - started < 0.5
            assert len(calls) == 1
        finally:
            release.set()
            assert delivery._DNS_LOCK.acquire(timeout=3)
            delivery._DNS_LOCK.release()
        assert received == []


def test_maximum_body_fits_bounded_cli_request(delivery, tmp_path):
    queue = tmp_path / "queue"
    body = b"x" * delivery.MAX_ENTRY_BYTES
    request = delivery.prepare_request("otlp", "http://localhost:9", body)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sediment_cli.delivery",
            "enqueue",
            "--directory",
            str(queue),
        ],
        input=json.dumps(request.as_dict()),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert delivery.status(queue)["pending_bytes"] == delivery.MAX_ENTRY_BYTES
    with pytest.raises(delivery.DeliveryError, match="entry_too_large"):
        delivery.prepare_request("otlp", "http://localhost:9", body + b"x")


def test_corrupt_payload_is_declined_and_private_files_required(delivery, tmp_path):
    queue = tmp_path / "queue"
    request = delivery.prepare_request("otlp", "http://localhost:9", b"preserve")
    delivery.enqueue(request, queue)
    entry = queue / (request.delivery_id + ".entry")
    content = entry.read_bytes()
    entry.write_bytes(content[:-1] + b"X")
    assert delivery.replay(queue, env={})["attempted"] == 0
    assert delivery.status(queue)["receipts"] == {"invalid_record": 1}
    receipt = next(queue.glob("*.receipt"))
    receipt.chmod(0o640)
    assert (
        delivery.enqueue(
            delivery.prepare_request("otlp", "http://localhost:9", b"another"), queue
        ).reason
        == "unsafe_storage"
    )
    receipt.chmod(0o600)
    os.link(receipt, tmp_path / "linked-receipt")
    assert (
        delivery.enqueue(
            delivery.prepare_request("otlp", "http://localhost:9", b"another"), queue
        ).reason
        == "unsafe_storage"
    )


def test_fsync_failure_has_no_success_or_sensitive_diagnostic(
    delivery, tmp_path, monkeypatch, caplog
):
    queue = tmp_path / "queue"
    queue.mkdir(mode=0o700)

    def unavailable(fd):
        raise OSError("sensitive filesystem exception")

    monkeypatch.setattr(delivery.os, "fsync", unavailable)
    request = delivery.prepare_request("otlp", "http://localhost:9", b"secret payload")
    result = delivery.enqueue(request, queue)
    assert (result.status, result.reason) == ("declined", "storage_unavailable")
    assert request.delivery_id in caplog.text
    assert "sensitive" not in caplog.text and "secret payload" not in caplog.text
    assert delivery.status(queue)["pending"] == 0


@pytest.mark.parametrize(
    "code,expected",
    [
        (408, "pending"),
        (429, "pending"),
        (500, "pending"),
        (403, "blocked"),
        (422, "blocked"),
        (302, "blocked"),
    ],
)
def test_http_failure_statuses_are_classified(delivery, code, expected):
    with server([(code, b"private body")]) as (url, received):
        result = delivery.send_once(
            delivery.prepare_request("otlp", url, b"{}"),
            env={"SEDIMENT_OTLP_ENDPOINT": url, "SEDIMENT_INGEST_TOKEN": "t"},
        )
        assert result.status == expected
        assert len(received) == 1


def test_concurrent_publishers_preserve_every_complete_record(delivery, tmp_path):
    queue = tmp_path / "queue"
    children = []
    try:
        for index in range(8):
            request = delivery.prepare_request(
                "otlp", "http://localhost:9", str(index).encode()
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "sediment_cli.delivery",
                    "enqueue",
                    "--directory",
                    str(queue),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            process.stdin.write(json.dumps(request.as_dict()))
            process.stdin.close()
            process.stdin = None
            children.append(process)
        for process in children:
            output, errors = process.communicate(timeout=10)
            assert process.returncode == 0, errors
            assert json.loads(output)["status"] == "queued"
        assert delivery.status(queue)["pending"] == 8
        assert len(list(queue.glob("*.entry"))) == 8
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)


@pytest.mark.parametrize(
    "url", ["http://api:8000", "http://remote.example", "http://10.0.0.1:8000"]
)
def test_gateway_http_requires_explicit_origin_before_credentials(
    delivery, url, monkeypatch
):
    request = delivery.prepare_request("gateway", url, b"{}")
    monkeypatch.setattr(
        delivery, "_credential", lambda *args: pytest.fail("read credentials")
    )
    result = delivery.send_once(request, env={"SEDIMENT_INGEST_URL": url})
    assert (result.status, result.reason) == ("blocked", "configuration_invalid")


@pytest.mark.parametrize(
    "origin",
    [
        "http://api",
        "http://api:8001",
        "http://other:8000",
        "https://api:8000",
        "http://api:8000/path",
        "http://user@api:8000",
        "http://api:8000?",
        "http://api:8000#",
        "http://api:8000\n",
    ],
)
def test_gateway_exception_does_not_widen_destination(delivery, origin):
    with pytest.raises(delivery.DeliveryError, match="configuration_invalid"):
        delivery.configured_destination(
            "gateway",
            {
                "SEDIMENT_INGEST_URL": "http://api:8000",
                "SEDIMENT_GATEWAY_LOCAL_HTTP_ORIGIN": origin,
            },
        )


def test_exact_gateway_origin_exception_does_not_apply_to_otlp(delivery):
    env = {
        "SEDIMENT_INGEST_URL": "http://api:8000",
        "SEDIMENT_OTLP_ENDPOINT": "http://api:8000",
        "SEDIMENT_GATEWAY_LOCAL_HTTP_ORIGIN": "http://api:8000",
    }
    assert (
        delivery.configured_destination("gateway", env)
        == "http://api:8000/ingest/gateway"
    )
    with pytest.raises(delivery.DeliveryError, match="configuration_invalid"):
        delivery.configured_destination("otlp", env)


def test_gateway_replay_policy_removal_preserves_prepared_bytes(
    delivery, tmp_path, monkeypatch
):
    request = delivery.prepare_request(
        "gateway", "http://api:8000", b'{"private":"prepared"}'
    )
    queue = tmp_path / "queue"
    delivery.enqueue(request, queue)
    monkeypatch.setattr(
        delivery, "_credential", lambda *args: pytest.fail("read credentials")
    )
    result = delivery.replay(queue, env={"SEDIMENT_INGEST_URL": "http://api:8000"})
    assert result["attempted"] == 0
    assert delivery.status(queue)["blocked"] == 1
    assert any(request.body in p.read_bytes() for p in queue.iterdir())


def test_gateway_local_origin_direct_and_replay_use_live_policy(
    delivery, tmp_path, monkeypatch
):
    import socket

    resolve = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: resolve(
            "127.0.0.1" if host == "api" else host, *args, **kwargs
        ),
    )
    with server([(200, b'{"fact_id":"retained","stored":true}')]) as (
        loopback,
        received,
    ):
        origin = loopback.replace("127.0.0.1", "api")
        env = {
            "SEDIMENT_INGEST_URL": origin,
            "SEDIMENT_GATEWAY_LOCAL_HTTP_ORIGIN": origin,
            "SEDIMENT_API_BEARER_TOKEN": "synthetic",
        }
        request = delivery.prepare_request("gateway", origin, b'{"private":"prepared"}')
        assert delivery.send_once(request, env=env).status == "acknowledged"
        queue = tmp_path / "queue"
        delivery.enqueue(request, queue)
        removed = {
            k: v for k, v in env.items() if k != "SEDIMENT_GATEWAY_LOCAL_HTTP_ORIGIN"
        }
        assert delivery.replay(queue, env=removed)["attempted"] == 0
        assert len(received) == 1
        assert delivery.status(queue)["blocked"] == 1
        assert any(request.body in p.read_bytes() for p in queue.iterdir())
        assert delivery.replay(queue, env=env, retry_blocked=True)["acknowledged"] == 1
        assert len(received) == 2
        assert received[0] == received[1]

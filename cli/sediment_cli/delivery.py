# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded, opt-in transport of immutable prepared HTTP bodies (ADR 0017).

This module is Python 3.12 stdlib-only so a standalone copy is also a supported
gateway/fleet implementation. Transport receipts never establish OTLP Fact counts.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import hashlib
import http.client
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import signal
import socket
import ssl
import stat
import sys
import threading
import time
import urllib.parse
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

FORMAT_VERSION = 1
MAX_ACTIVE_BYTES = 256 * 1024 * 1024
MAX_ACTIVE_ENTRIES = 2048
MAX_ENTRY_BYTES = 8 * 1024 * 1024
MAX_STDIN_BYTES = ((MAX_ENTRY_BYTES + 2) // 3) * 4 + 16384
REPLAY_WINDOW_SECONDS = 24 * 60 * 60
RECEIPT_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_RECEIPTS = 2048
MAX_BATCH_ATTEMPTS = 32
HTTP_TIMEOUT_SECONDS = 5
MAX_BACKOFF_SECONDS = 60
MAX_RESPONSE_BYTES = 65536
_MAX_HEADER_BYTES = 16384
_TERMINAL = {"acknowledged", "skipped", "declined"}
_REASONS = {
    "buffered",
    "already_buffered",
    "gateway_stored",
    "gateway_duplicate",
    "otlp_delivered",
    "no_session",
    "invalid_acknowledgment",
    "transport_failure",
    "retryable_http",
    "authentication_rejected",
    "client_rejected",
    "redirect_rejected",
    "configuration_missing",
    "configuration_invalid",
    "destination_changed",
    "credentials_missing",
    "credentials_invalid",
    "buffer_full",
    "entry_too_large",
    "unsafe_storage",
    "storage_unavailable",
    "storage_busy",
    "worker_busy",
    "expired",
    "identity_conflict",
    "invalid_record",
    "invalid_request",
    "buffer_disabled",
}
logger = logging.getLogger("sediment.delivery")
# A timed-out OS resolver may finish later; only one resolver can remain alive.
_DNS_LOCK = threading.Lock()


class DeliveryError(ValueError):
    """A closed, content-free transport failure."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Disposition:
    delivery_id: str
    status: str
    reason: str
    fact_id: str | None = None
    stored: bool | None = None
    fallback_reason: str | None = None

    def as_dict(self) -> dict:
        result = {
            "format_version": FORMAT_VERSION,
            "delivery_id": self.delivery_id,
            "status": self.status,
            "reason": self.reason,
        }
        if self.fact_id is not None:
            result.update(fact_id=self.fact_id, stored=self.stored)
        if self.fallback_reason is not None:
            result["fallback_reason"] = self.fallback_reason
        return result


@dataclass(frozen=True)
class DeliveryRequest:
    channel: str
    destination: str
    body: bytes
    captured_at: str
    delivery_id: str

    def as_dict(self) -> dict:
        return {
            "channel": self.channel,
            "destination": self.destination,
            "body_base64": base64.b64encode(self.body).decode("ascii"),
            "captured_at": self.captured_at,
            "delivery_id": self.delivery_id,
        }


def _uuid(value: str) -> str:
    if not isinstance(value, str):
        raise DeliveryError("invalid_request")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise DeliveryError("invalid_request") from None


def _instant(value: str) -> str:
    if not isinstance(value, str):
        raise DeliveryError("invalid_request")
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError
        return instant.astimezone(UTC).isoformat()
    except (ValueError, OverflowError):
        raise DeliveryError("invalid_request") from None


def _destination(channel: str, value: str) -> str:
    if (
        not isinstance(channel, str)
        or channel not in {"gateway", "otlp"}
        or not isinstance(value, str)
    ):
        raise DeliveryError("invalid_request")
    if len(value) > 2048:
        raise DeliveryError("invalid_request")
    value = value.strip().rstrip("/")
    if not value or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise DeliveryError("invalid_request")
    try:
        parsed = urllib.parse.urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value
            or "#" in value
            or parsed.netloc.endswith(":")
            or "\\" in value
            or port == 0
        ):
            raise ValueError
        path = "/ingest/gateway" if channel == "gateway" else "/v1/logs"
        if parsed.path not in {"", "/", path}:
            raise ValueError
        if channel == "otlp" and parsed.scheme == "http" and host != "localhost":
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        host.encode("ascii")
        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            authority += f":{port}"
        return urllib.parse.urlunsplit((parsed.scheme, authority, path, "", ""))
    except (ValueError, UnicodeError):
        raise DeliveryError("invalid_request") from None


def prepare_request(
    channel: str,
    destination: str,
    body: bytes,
    *,
    captured_at: str | None = None,
    delivery_id: str | None = None,
) -> DeliveryRequest:
    if not isinstance(body, bytes):
        raise DeliveryError("invalid_request")
    if len(body) > MAX_ENTRY_BYTES:
        raise DeliveryError("entry_too_large")
    return DeliveryRequest(
        channel=channel,
        destination=_destination(channel, destination),
        body=body,
        captured_at=_instant(
            captured_at if captured_at is not None else datetime.now(UTC).isoformat()
        ),
        delivery_id=_uuid(delivery_id)
        if delivery_id is not None
        else str(uuid.uuid4()),
    )


def _validated(request: DeliveryRequest) -> DeliveryRequest:
    if not isinstance(request, DeliveryRequest):
        raise DeliveryError("invalid_request")
    return prepare_request(
        request.channel,
        request.destination,
        request.body,
        captured_at=request.captured_at,
        delivery_id=request.delivery_id,
    )


def _report(disposition: Disposition) -> Disposition:
    logger.warning(
        "delivery_disposition delivery_id=%s status=%s reason=%s",
        disposition.delivery_id,
        disposition.status,
        disposition.reason,
    )
    return disposition


def _json(data: bytes | str):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def constant(value):
        raise ValueError

    try:
        return json.loads(data, object_pairs_hook=object_pairs, parse_constant=constant)
    except RecursionError:
        raise ValueError from None


def _encoded(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode()


def configured_destination(channel: str, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    name = "SEDIMENT_INGEST_URL" if channel == "gateway" else "SEDIMENT_OTLP_ENDPOINT"
    value = env.get(name)
    if not value:
        raise DeliveryError("configuration_missing")
    try:
        destination = _destination(channel, value)
        parsed = urllib.parse.urlsplit(destination)
        if channel == "gateway" and parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = parsed.hostname == "localhost"
            if not loopback:
                origin = env.get("SEDIMENT_GATEWAY_LOCAL_HTTP_ORIGIN", "")
                # Only this active, exact origin authorizes plaintext gateway
                # delivery. Prepared records never store or enable the exception.
                if any(ord(c) < 33 or ord(c) == 127 for c in origin):
                    raise DeliveryError("configuration_invalid")
                allowed = urllib.parse.urlsplit(_destination("gateway", origin))
                raw = urllib.parse.urlsplit(origin)
                if (
                    raw.path not in {"", "/"}
                    or allowed.scheme != "http"
                    or (parsed.scheme, parsed.hostname, parsed.port)
                    != (allowed.scheme, allowed.hostname, allowed.port)
                ):
                    raise DeliveryError("configuration_invalid")
        return destination
    except DeliveryError:
        raise DeliveryError("configuration_invalid") from None


def _credential(channel: str, env: Mapping[str, str]) -> str:
    token = env.get(
        "SEDIMENT_API_BEARER_TOKEN"
        if channel == "gateway"
        else "SEDIMENT_INGEST_TOKEN",
        "",
    )
    if not token and channel == "otlp":
        for field in env.get("OTEL_EXPORTER_OTLP_HEADERS", "").split(","):
            key, separator, value = field.partition("=")
            if separator and key.strip().lower() == "authorization":
                value = urllib.parse.unquote(value).strip()
                if value.lower().startswith("bearer "):
                    token = value[7:]
                    break
    if not token:
        raise DeliveryError("credentials_missing")
    if not isinstance(token, str) or any(ord(c) < 32 or ord(c) > 126 for c in token):
        raise DeliveryError("credentials_invalid")
    return token


def _configuration(request: DeliveryRequest, env: Mapping[str, str]) -> str:
    if configured_destination(request.channel, env) != request.destination:
        raise DeliveryError("destination_changed")
    return _credential(request.channel, env)


def _acknowledgment(request: DeliveryRequest, body: bytes) -> Disposition:
    invalid = Disposition(request.delivery_id, "pending", "invalid_acknowledgment")
    try:
        result = _json(body)
    except (ValueError, UnicodeError):
        return invalid
    if not isinstance(result, dict):
        return invalid
    if request.channel == "otlp":
        return (
            Disposition(request.delivery_id, "acknowledged", "otlp_delivered")
            if result == {}
            else invalid
        )
    if set(result) == {"fact_id", "stored"}:
        fact_id = result["fact_id"]
        if (
            isinstance(fact_id, str)
            and fact_id.strip() == fact_id
            and fact_id
            and len(fact_id) <= 1024
            and not any(ord(c) < 32 for c in fact_id)
            and type(result["stored"]) is bool
        ):
            return Disposition(
                request.delivery_id,
                "acknowledged",
                "gateway_stored" if result["stored"] else "gateway_duplicate",
                fact_id,
                result["stored"],
            )
    if (
        set(result) == {"skipped", "reason"}
        and result["skipped"] is True
        and result["reason"] == "no_session"
    ):
        return Disposition(request.delivery_id, "skipped", "no_session")
    return invalid


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _connect(host: str, port: int, *, secure: bool, deadline: float):
    # socket's timeout excludes DNS. Bound the caller's wait and the number of
    # outstanding OS lookups; a late resolver never receives or sends a payload.
    if not _DNS_LOCK.acquire(timeout=_remaining(deadline)):
        raise TimeoutError
    done = threading.Event()
    addresses, errors = [], []

    def resolve():
        try:
            addresses.extend(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as exc:
            errors.append(exc)
        finally:
            _DNS_LOCK.release()
            done.set()

    try:
        threading.Thread(target=resolve, daemon=True).start()
    except RuntimeError:
        _DNS_LOCK.release()
        raise OSError from None
    if not done.wait(_remaining(deadline)):
        raise TimeoutError
    if errors:
        raise errors[0]
    for family, kind, protocol, _, address in addresses:
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(_remaining(deadline))
            connection.connect(address)
            if secure:
                context = ssl.create_default_context()
                connection.settimeout(_remaining(deadline))
                connection = context.wrap_socket(connection, server_hostname=host)
            return connection
        except OSError:
            connection.close()
    raise OSError


def send_once(
    request: DeliveryRequest, *, env: Mapping[str, str] | None = None
) -> Disposition:
    """One bounded HTTP attempt; no redirects, retries, or stored credentials."""
    env = os.environ if env is None else env
    delivery_id = str(uuid.uuid4())
    try:
        request = _validated(request)
        delivery_id = request.delivery_id
        token = _configuration(request, env)
    except DeliveryError as exc:
        return _report(Disposition(delivery_id, "blocked", exc.reason))
    parsed = urllib.parse.urlsplit(request.destination)
    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(
        parsed.hostname, parsed.port, timeout=HTTP_TIMEOUT_SECONDS
    )
    timer = None
    try:
        deadline = time.monotonic() + HTTP_TIMEOUT_SECONDS
        transport = _connect(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            secure=parsed.scheme == "https",
            deadline=deadline,
        )
        connection.sock = transport

        def interrupt():
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        # An inactivity timeout alone permits an endless slow response/header.
        timer = threading.Timer(_remaining(deadline), interrupt)
        timer.daemon = True
        timer.start()
        connection.request(
            "POST",
            parsed.path,
            body=request.body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "sediment-delivery/1",
                "Authorization": f"Bearer {token}",
            },
        )
        response = connection.getresponse()
        code = response.status
        if code in {408, 429} or 500 <= code <= 599:
            result = Disposition(delivery_id, "pending", "retryable_http")
        elif code in {401, 403}:
            result = Disposition(delivery_id, "blocked", "authentication_rejected")
        elif 300 <= code <= 399:
            result = Disposition(delivery_id, "blocked", "redirect_rejected")
        elif 400 <= code <= 499:
            result = Disposition(delivery_id, "blocked", "client_rejected")
        elif code != 200:
            result = Disposition(delivery_id, "pending", "invalid_acknowledgment")
        else:
            body = bytearray()
            while len(body) <= MAX_RESPONSE_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                part = response.read1(min(8192, MAX_RESPONSE_BYTES + 1 - len(body)))
                if not part:
                    break
                body.extend(part)
            _remaining(deadline)
            if response.length not in {None, 0} and len(body) <= MAX_RESPONSE_BYTES:
                raise http.client.IncompleteRead(bytes(body))
            result = (
                _acknowledgment(request, bytes(body))
                if len(body) <= MAX_RESPONSE_BYTES
                else Disposition(delivery_id, "pending", "invalid_acknowledgment")
            )
    except (OSError, http.client.HTTPException, ValueError):
        result = Disposition(delivery_id, "pending", "transport_failure")
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()
    return _report(result)


class _Queue:
    """An opened private directory; relative operations cannot follow record links."""

    def __init__(self, path: str | Path, *, create: bool):
        self.path = Path(path)
        self.fd = -1
        if not self.path.is_absolute():
            raise DeliveryError("unsafe_storage")
        try:
            self.fd = self._open_directory(create=create)
            self._check(os.fstat(self.fd), directory=True)
            for name in os.listdir(self.fd):
                try:
                    self._check(os.stat(name, dir_fd=self.fd, follow_symlinks=False))
                except FileNotFoundError:
                    # Another publisher/worker may have renamed or removed it.
                    pass
        except Exception:
            if self.fd != -1:
                os.close(self.fd)
                self.fd = -1
            raise

    def _open_directory(self, *, create: bool) -> int:
        parts = list(self.path.parts[1:])
        if ".." in parts:
            raise DeliveryError("unsafe_storage")
        # macOS exposes these root-owned OS aliases. All remaining components,
        # including user-created ancestor links, are opened without following links.
        if sys.platform == "darwin" and parts and parts[0] in {"tmp", "var"}:
            alias = Path("/") / parts[0]
            info = alias.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                and info.st_uid == 0
                and os.readlink(alias) == "private/" + parts[0]
            ):
                parts.insert(0, "private")
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for index, part in enumerate(parts):
                try:
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, 0o700, dir_fd=directory)
                        os.fsync(directory)
                    except FileExistsError:
                        pass
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory,
                    )
                os.close(directory)
                directory = child
                info = os.fstat(directory)
                if index != len(parts) - 1 and (
                    info.st_uid not in {0, os.getuid()}
                    or (
                        stat.S_IMODE(info.st_mode) & 0o022
                        and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
                    )
                ):
                    raise DeliveryError("unsafe_storage")
            return directory
        except Exception:
            os.close(directory)
            raise

    @staticmethod
    def _check(info, *, directory=False):
        correct_type = (
            stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        )
        mode = 0o700 if directory else 0o600
        if (
            not correct_type
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != mode
            or (not directory and info.st_nlink != 1)
        ):
            raise DeliveryError("unsafe_storage")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        os.close(self.fd)

    def read(self, name: str, limit: int) -> bytes:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as stream:
            self._check(os.fstat(stream.fileno()))
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise DeliveryError("invalid_record")
        return data

    def atomic(self, name: str, data: bytes):
        temporary = f".tmp-{uuid.uuid4()}"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fd,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            self.remove(temporary)

    def remove(self, name):
        try:
            os.unlink(name, dir_fd=self.fd)
        except FileNotFoundError:
            pass

    @contextmanager
    def lock(self, name: str, *, wait: bool = True, create: bool = True):
        flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
        fd = os.open(name, flags, 0o600, dir_fd=self.fd)
        try:
            self._check(os.fstat(fd))
            deadline = time.monotonic() + 1
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not wait or time.monotonic() >= deadline:
                        raise DeliveryError(
                            "worker_busy" if name == "worker.lock" else "storage_busy"
                        ) from None
                    time.sleep(0.01)
            yield
        finally:
            os.close(fd)

    def names(self, suffix):
        return sorted(name for name in os.listdir(self.fd) if name.endswith(suffix))


def _failure(exc: Exception) -> str:
    if isinstance(exc, DeliveryError):
        return exc.reason
    if isinstance(exc, OSError) and exc.errno in {40, 62, 20}:
        return "unsafe_storage"
    return "storage_unavailable"


def _header(queue: _Queue, name: str) -> dict:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=queue.fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        queue._check(info)
        line = stream.readline(_MAX_HEADER_BYTES + 1)
    try:
        header = _json(line)
        required = {
            "format_version",
            "delivery_id",
            "channel",
            "destination",
            "captured_at",
            "enqueued_at",
            "bytes",
            "sha256",
        }
        if (
            not line.endswith(b"\n")
            or len(line) > _MAX_HEADER_BYTES
            or not isinstance(header, dict)
            or set(header) != required
        ):
            raise ValueError
        if (
            type(header["format_version"]) is not int
            or header["format_version"] != FORMAT_VERSION
        ):
            raise ValueError
        if (
            _uuid(header["delivery_id"]) + ".entry" != name
            or _destination(header["channel"], header["destination"])
            != header["destination"]
        ):
            raise ValueError
        _instant(header["captured_at"])
        if (
            not _number(header["enqueued_at"])
            or type(header["bytes"]) is not int
            or not 0 <= header["bytes"] <= MAX_ENTRY_BYTES
        ):
            raise ValueError
        if (
            info.st_size != len(line) + header["bytes"]
            or not isinstance(header["sha256"], str)
            or not re.fullmatch("[0-9a-f]{64}", header["sha256"])
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise DeliveryError("invalid_record") from None
    return header


def _number(value):
    try:
        return type(value) in {int, float} and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _request(queue: _Queue, header: dict) -> DeliveryRequest:
    data = queue.read(
        header["delivery_id"] + ".entry", MAX_ENTRY_BYTES + _MAX_HEADER_BYTES
    )
    _, _, body = data.partition(b"\n")
    if (
        len(body) != header["bytes"]
        or hashlib.sha256(body).hexdigest() != header["sha256"]
    ):
        raise DeliveryError("invalid_record")
    return prepare_request(
        header["channel"],
        header["destination"],
        body,
        captured_at=header["captured_at"],
        delivery_id=header["delivery_id"],
    )


def _state(queue: _Queue, delivery_id: str) -> dict:
    try:
        state = _json(queue.read(delivery_id + ".state", _MAX_HEADER_BYTES))
    except FileNotFoundError:
        return {
            "status": "pending",
            "attempts": 0,
            "next_attempt": 0,
            "reason": "buffered",
        }
    except (ValueError, UnicodeError):
        raise DeliveryError("invalid_record") from None
    if (
        not isinstance(state, dict)
        or set(state) != {"status", "attempts", "next_attempt", "reason"}
        or not isinstance(state["status"], str)
        or state["status"] not in {"pending", "blocked"}
        or type(state["attempts"]) is not int
        or state["attempts"] < 0
        or not _number(state["next_attempt"])
        or not isinstance(state["reason"], str)
        or state["reason"] not in _REASONS
    ):
        raise DeliveryError("invalid_record")
    return state


def _receipt(queue: _Queue, disposition: Disposition, now: float):
    queue.atomic(
        disposition.delivery_id + ".receipt",
        _encoded({**disposition.as_dict(), "completed_at": now}),
    )
    queue.remove(disposition.delivery_id + ".entry")
    queue.remove(disposition.delivery_id + ".state")
    os.fsync(queue.fd)


def _read_receipt(queue: _Queue, name: str) -> dict:
    try:
        receipt = _json(queue.read(name, _MAX_HEADER_BYTES))
        required = {"format_version", "delivery_id", "status", "reason", "completed_at"}
        if not isinstance(receipt, dict) or set(receipt) not in (
            required,
            required | {"fact_id", "stored"},
        ):
            raise ValueError
        if (
            type(receipt["format_version"]) is not int
            or receipt["format_version"] != FORMAT_VERSION
        ):
            raise ValueError
        if _uuid(receipt["delivery_id"]) + ".receipt" != name or not _number(
            receipt["completed_at"]
        ):
            raise ValueError
        status, reason = receipt["status"], receipt["reason"]
        if (
            not isinstance(status, str)
            or status not in _TERMINAL
            or not isinstance(reason, str)
            or reason not in _REASONS
        ):
            raise ValueError
        if status == "acknowledged" and reason in {
            "gateway_stored",
            "gateway_duplicate",
        }:
            fact_id = receipt.get("fact_id")
            if (
                not isinstance(fact_id, str)
                or not fact_id
                or fact_id.strip() != fact_id
                or len(fact_id) > 1024
                or any(ord(c) < 32 for c in fact_id)
                or type(receipt.get("stored")) is not bool
                or receipt["stored"] != (reason == "gateway_stored")
            ):
                raise ValueError
        elif (
            set(receipt) != required
            or (status == "acknowledged" and reason != "otlp_delivered")
            or (status == "skipped" and reason != "no_session")
            or (
                status == "declined"
                and reason not in {"buffer_full", "expired", "invalid_record"}
            )
        ):
            raise ValueError
        return receipt
    except (ValueError, TypeError, KeyError):
        raise DeliveryError("invalid_record") from None


def _maintenance(queue: _Queue, now: float):
    # Validate before any deletion: a malformed receipt cannot erase its payload.
    receipts = {name: _read_receipt(queue, name) for name in queue.names(".receipt")}
    # Enqueue lock excludes unfinished publishers. Only our temporary names go.
    for name in os.listdir(queue.fd):
        if re.fullmatch(r"\.tmp-[0-9a-f-]{36}", name):
            queue.remove(name)
    for name in queue.names(".entry"):
        delivery_id = name.removesuffix(".entry")
        if (
            not re.fullmatch(r"[0-9a-f-]{36}", delivery_id)
            or _uuid(delivery_id) != delivery_id
        ):
            raise DeliveryError("invalid_record")
        if delivery_id + ".receipt" in receipts:
            # A crash after receipt publication can leave the acknowledged body.
            queue.remove(name)
            queue.remove(delivery_id + ".state")
            continue
        try:
            header = _header(queue, name)
            if now - header["enqueued_at"] < REPLAY_WINDOW_SECONDS:
                continue
            reason = "expired"
        except DeliveryError as exc:
            if exc.reason != "invalid_record":
                raise
            reason = "invalid_record"
        _receipt(queue, _report(Disposition(delivery_id, "declined", reason)), now)
    retained = []
    for name in queue.names(".receipt"):
        completed = _read_receipt(queue, name)["completed_at"]
        if now - completed >= RECEIPT_RETENTION_SECONDS:
            queue.remove(name)
        else:
            retained.append((completed, name))
    for _, name in sorted(retained)[: max(0, len(retained) - MAX_RECEIPTS)]:
        queue.remove(name)
    os.fsync(queue.fd)


def enqueue(request: DeliveryRequest, directory: str | Path) -> Disposition:
    """Publish exact bytes and fsync before returning queued; never perform HTTP."""
    delivery_id = str(uuid.uuid4())
    try:
        request = _validated(request)
        delivery_id = request.delivery_id
        with _Queue(directory, create=True) as queue, queue.lock("enqueue.lock"):
            now = time.time()
            _maintenance(queue, now)
            names = queue.names(".entry")
            if delivery_id + ".entry" in names:
                original = _request(queue, _header(queue, delivery_id + ".entry"))
                if original != request:
                    return _report(
                        Disposition(delivery_id, "declined", "identity_conflict")
                    )
                return _report(Disposition(delivery_id, "queued", "already_buffered"))
            if delivery_id + ".receipt" in queue.names(".receipt"):
                return _report(
                    Disposition(delivery_id, "declined", "identity_conflict")
                )
            used = sum(_header(queue, name)["bytes"] for name in names)
            if (
                len(names) >= MAX_ACTIVE_ENTRIES
                or used + len(request.body) > MAX_ACTIVE_BYTES
            ):
                result = Disposition(delivery_id, "declined", "buffer_full")
                try:
                    _receipt(queue, result, now)
                    _maintenance(queue, now)
                except (OSError, ValueError) as exc:
                    # A failed diagnostic must not erase the capacity decision
                    # and make the caller treat this as eligible for fallback.
                    logger.warning(
                        "delivery_receipt_failed delivery_id=%s reason=%s disposition=%s",
                        delivery_id,
                        _failure(exc),
                        result.reason,
                    )
                return _report(result)
            header = {
                "format_version": FORMAT_VERSION,
                "delivery_id": delivery_id,
                "channel": request.channel,
                "destination": request.destination,
                "captured_at": request.captured_at,
                "enqueued_at": now,
                "bytes": len(request.body),
                "sha256": hashlib.sha256(request.body).hexdigest(),
            }
            queue.atomic(
                delivery_id + ".entry", _encoded(header) + b"\n" + request.body
            )
        return _report(Disposition(delivery_id, "queued", "buffered"))
    except (OSError, ValueError) as exc:
        return _report(Disposition(delivery_id, "declined", _failure(exc)))


def deliver(
    request: DeliveryRequest, *, env: Mapping[str, str] | None = None
) -> Disposition:
    """Enqueue when enabled; storage faults degrade to one best-effort attempt."""
    env = os.environ if env is None else env
    directory = env.get("SEDIMENT_DELIVERY_DIR", "").strip()
    if directory:
        result = enqueue(request, directory)
        if result.status != "declined" or result.reason not in {
            "unsafe_storage",
            "storage_unavailable",
            "storage_busy",
        }:
            return result
        logger.warning(
            "delivery_mode delivery_id=%s mode=best_effort reason=%s",
            result.delivery_id,
            result.reason,
        )
        return replace(send_once(request, env=env), fallback_reason=result.reason)
    logger.warning("delivery_mode best_effort reason=buffer_disabled")
    return send_once(request, env=env)


def _summary() -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "attempted": 0,
        "acknowledged": 0,
        "skipped": 0,
        "pending": 0,
        "blocked": 0,
        "worker_busy": False,
    }


def _batch(
    queue: _Queue,
    env: Mapping[str, str],
    *,
    retry_blocked: bool,
    stop_event: threading.Event | None = None,
) -> dict:
    summary = _summary()
    with queue.lock("enqueue.lock"):
        _maintenance(queue, time.time())
        candidates = []
        for name in queue.names(".entry"):
            header = _header(queue, name)
            delivery_id = header["delivery_id"]
            try:
                due = (
                    _state(queue, delivery_id)["next_attempt"] or header["enqueued_at"]
                )
            except DeliveryError as exc:
                if exc.reason != "invalid_record":
                    raise
                _receipt(
                    queue,
                    _report(Disposition(delivery_id, "declined", "invalid_record")),
                    time.time(),
                )
                continue
            candidates.append((due, header))
        # Schedule by due time, including enqueue time for untouched entries.
        # Slow failures cannot reclaim every slot, nor can new arrivals starve retries.
        headers = [
            header
            for _, header in sorted(
                candidates,
                key=lambda pair: (
                    pair[0],
                    pair[1]["enqueued_at"],
                    pair[1]["delivery_id"],
                ),
            )
        ]
    for header in headers:
        if summary["attempted"] >= MAX_BATCH_ATTEMPTS or (
            stop_event is not None and stop_event.is_set()
        ):
            break
        delivery_id = header["delivery_id"]
        with queue.lock("enqueue.lock"):
            if delivery_id + ".entry" not in queue.names(".entry"):
                continue
            if time.time() - header["enqueued_at"] >= REPLAY_WINDOW_SECONDS:
                _receipt(
                    queue,
                    _report(Disposition(delivery_id, "declined", "expired")),
                    time.time(),
                )
                continue
            try:
                state = _state(queue, delivery_id)
                if state["status"] == "blocked" and not retry_blocked:
                    continue
                if state["status"] != "blocked" and state["next_attempt"] > time.time():
                    continue
                request = _request(queue, header)
            except DeliveryError as exc:
                if exc.reason != "invalid_record":
                    raise
                _receipt(
                    queue,
                    _report(Disposition(delivery_id, "declined", "invalid_record")),
                    time.time(),
                )
                continue
        try:
            _configuration(request, env)
        except DeliveryError as exc:
            result = _report(Disposition(delivery_id, "blocked", exc.reason))
        else:
            result = send_once(request, env=env)
            summary["attempted"] += 1
        with queue.lock("enqueue.lock"):
            now = time.time()
            # An enqueue can expire this entry while HTTP runs outside the lock.
            # Its terminal receipt wins; do not resurrect a deleted transport copy.
            if delivery_id + ".entry" not in queue.names(".entry"):
                continue
            if result.status in _TERMINAL:
                _receipt(queue, result, now)
            else:
                attempts = state["attempts"] + 1
                ceiling = min(MAX_BACKOFF_SECONDS, 2 ** min(attempts, 6))
                delay = ceiling / 2 + secrets.randbelow(1001) / 1000 * ceiling / 2
                queue.atomic(
                    delivery_id + ".state",
                    _encoded(
                        {
                            "status": result.status,
                            "attempts": attempts,
                            "next_attempt": now + delay,
                            "reason": result.reason,
                        }
                    ),
                )
            summary[result.status] += 1
    with queue.lock("enqueue.lock"):
        _maintenance(queue, time.time())
    return summary


def replay(
    directory: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    retry_blocked: bool = False,
) -> dict:
    """Drain up to 32 due entries once each; a failed entry never monopolizes work."""
    env = os.environ if env is None else env
    with _Queue(directory, create=False) as queue:
        try:
            with queue.lock("worker.lock", wait=False):
                return _batch(queue, env, retry_blocked=retry_blocked)
        except DeliveryError as exc:
            if exc.reason != "worker_busy":
                raise
            return {**_summary(), "worker_busy": True}


def watch(
    directory: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    stop_event: threading.Event | None = None,
    retry_blocked: bool = False,
) -> dict:
    """Own one worker until stopped, finishing at most its in-flight HTTP attempt.

    An explicitly enrolled worker initializes its private directory and locks
    before the first enqueue. Status never initializes storage.
    """
    env = os.environ if env is None else env
    stopped = threading.Event() if stop_event is None else stop_event
    result = _summary()
    while not stopped.is_set():
        try:
            queue = _Queue(directory, create=True)
        except (OSError, ValueError) as exc:
            logger.warning("delivery_worker reason=%s", _failure(exc))
            return {**result, "error": _failure(exc)}
        with queue:
            try:
                with queue.lock("worker.lock", wait=False):
                    while not stopped.is_set():
                        try:
                            result = _batch(
                                queue,
                                env,
                                retry_blocked=retry_blocked,
                                stop_event=stopped,
                            )
                            retry_blocked = False
                        except (OSError, ValueError) as exc:
                            logger.warning("delivery_worker reason=%s", _failure(exc))
                            result = {**_summary(), "error": _failure(exc)}
                        stopped.wait(1)
            except (OSError, ValueError) as exc:
                if isinstance(exc, DeliveryError) and exc.reason == "worker_busy":
                    return {**result, "worker_busy": True}
                logger.warning("delivery_worker reason=%s", _failure(exc))
                return {**result, "error": _failure(exc)}
    return result


def status(directory: str | Path) -> dict:
    """Inspect without creating directories, records, or worker enrollment."""
    result = {
        "format_version": FORMAT_VERSION,
        "pending": 0,
        "pending_bytes": 0,
        "oldest_pending_age_seconds": None,
        "blocked": 0,
        "blocked_bytes": 0,
        "active_bytes": 0,
        "receipts": {},
        "worker_running": False,
    }
    try:
        queue = _Queue(directory, create=False)
    except FileNotFoundError:
        return result
    with queue:
        try:
            with queue.lock("worker.lock", wait=False, create=False):
                pass
        except FileNotFoundError:
            pass
        except DeliveryError as exc:
            if exc.reason != "worker_busy":
                raise
            result["worker_running"] = True
        if "enqueue.lock" not in os.listdir(queue.fd):
            if (
                queue.names(".entry")
                or queue.names(".receipt")
                or queue.names(".state")
            ):
                raise DeliveryError("invalid_record")
            return result
        with queue.lock("enqueue.lock", create=False):
            now, ages = time.time(), []
            receipt_names = {
                name: _read_receipt(queue, name) for name in queue.names(".receipt")
            }
            for name in queue.names(".entry"):
                header = _header(queue, name)
                if header["delivery_id"] + ".receipt" in receipt_names:
                    continue
                state = _state(queue, header["delivery_id"])
                category = "blocked" if state["status"] == "blocked" else "pending"
                result[category] += 1
                result[category + "_bytes"] += header["bytes"]
                result["active_bytes"] += header["bytes"]
                if category == "pending":
                    ages.append(max(0, now - header["enqueued_at"]))
            result["oldest_pending_age_seconds"] = max(ages) if ages else None
            counts = Counter(receipt["reason"] for receipt in receipt_names.values())
            result["receipts"] = dict(sorted(counts.items()))
    return result


def build_parser(*, prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Deliver prepared capture payloads; OTLP acknowledgment is not a Fact count.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("enqueue", "durably enqueue a bounded JSON request from stdin"),
        ("status", "inspect private buffer and worker state"),
        ("replay", "replay a bounded batch of due payloads"),
    ):
        child = sub.add_parser(command, help=help_text)
        child.add_argument(
            "--directory",
            default=None,
            help="private buffer (default: SEDIMENT_DELIVERY_DIR)",
        )
        if command == "enqueue":
            child.add_argument(
                "--fallback-direct",
                action="store_true",
                help="allow one best-effort send when buffering is disabled or storage fails",
            )
        if command == "replay":
            child.add_argument(
                "--watch", action="store_true", help="supervise replay until stopped"
            )
            child.add_argument(
                "--retry-blocked",
                action="store_true",
                help="retry blocked payloads once; stop the existing worker first",
            )
    return parser


def request_from_dict(value: dict) -> DeliveryRequest:
    if (
        not isinstance(value, dict)
        or not {"channel", "destination", "body_base64"} <= set(value)
        or set(value)
        - {"channel", "destination", "body_base64", "captured_at", "delivery_id"}
    ):
        raise DeliveryError("invalid_request")
    if (
        not isinstance(value["body_base64"], str)
        or len(value["body_base64"]) > ((MAX_ENTRY_BYTES + 2) // 3) * 4
    ):
        raise DeliveryError("entry_too_large")
    try:
        body = base64.b64decode(value["body_base64"], validate=True)
    except (ValueError, binascii.Error):
        raise DeliveryError("invalid_request") from None
    return prepare_request(
        value["channel"],
        value["destination"],
        body,
        captured_at=value.get("captured_at"),
        delivery_id=value.get("delivery_id"),
    )


def main(argv: list[str] | None = None, *, prog: str | None = None) -> int:
    args = build_parser(prog=prog).parse_args(argv)
    directory = (
        args.directory
        if args.directory is not None
        else os.environ.get("SEDIMENT_DELIVERY_DIR", "")
    ).strip()
    fallback = args.command == "enqueue" and args.fallback_direct
    try:
        if not directory and not fallback:
            raise DeliveryError("buffer_disabled")
        if args.command == "enqueue":
            data = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
            if len(data) > MAX_STDIN_BYTES:
                raise DeliveryError("entry_too_large")
            try:
                request = request_from_dict(_json(data))
            except (ValueError, TypeError, RecursionError) as exc:
                raise DeliveryError(
                    exc.reason if isinstance(exc, DeliveryError) else "invalid_request"
                ) from None
            result = (
                deliver(request, env={**os.environ, "SEDIMENT_DELIVERY_DIR": directory})
                if fallback
                else enqueue(request, directory)
            )
            print(json.dumps(result.as_dict(), sort_keys=True))
            return (
                0
                if result.status == "queued"
                or (fallback and result.status in {"acknowledged", "skipped"})
                else 1
            )
        if args.command == "status":
            print(json.dumps(status(directory), sort_keys=True))
            return 0
        stopped = threading.Event()
        prior = {}
        if args.watch:
            for signum in (signal.SIGINT, signal.SIGTERM):
                prior[signum] = signal.signal(signum, lambda *_: stopped.set())
        try:
            result = (
                watch(directory, stop_event=stopped, retry_blocked=args.retry_blocked)
                if args.watch
                else replay(directory, retry_blocked=args.retry_blocked)
            )
            print(json.dumps(result, sort_keys=True), flush=True)
            return 1 if "error" in result else 0
        finally:
            for signum, handler in prior.items():
                signal.signal(signum, handler)
    except (OSError, ValueError) as exc:
        reason = _failure(exc)
        result = _report(Disposition(str(uuid.uuid4()), "declined", reason))
        print(json.dumps(result.as_dict(), sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())

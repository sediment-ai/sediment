# SPDX-License-Identifier: AGPL-3.0-or-later
"""
LiteLLM proxy → Sediment forwarder.

Deployment glue, not an installed package: copy this file next to the proxy
and register it in config.yaml via:

    litellm_settings:
      callbacks: sediment_callback.handler

On every successful completion the proxy forwards LiteLLM's native
``StandardLoggingPayload`` verbatim as
``{"provider": "litellm", "payload": <slo>}`` to ``POST /ingest/gateway``.
All Sediment-domain logic is server-side: ``LiteLLMAdapter`` normalizes the
payload and ``session_identity.resolve_identity`` (``packages/capture``)
extracts session/user identity — upgrading the server upgrades every
deployment's extraction, and this file stays boring.
Completions with no resolvable session are POSTed anyway; the server
answers 200 ``{"skipped": true, "reason": "no_session"}``, observable per
call id. Upgrade order for fleets: server before this file
(``docs/agents/capture-clients.md`` §5).

A logging callback must never break the proxy: everything is wrapped in a
broad try/except and failures are logged, never raised.
"""

from __future__ import annotations

import json
import logging
import os
import asyncio
import atexit
import threading
import stat
import sys
from collections import ChainMap
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("sediment.litellm_callback")

try:
    import sediment_delivery as delivery

    # A checkout can also expose scripts/sediment_delivery.py, which is only
    # a command entry point. The mounted standalone implementation owns deliver.
    if not callable(getattr(delivery, "deliver", None)):
        raise ImportError
except ImportError:
    try:
        from sediment_cli import delivery
    except ImportError:
        delivery = None
        logger.warning("sediment_delivery reason=helper_unavailable")

SEDIMENT_INGEST_URL = os.environ.get(
    "SEDIMENT_INGEST_URL", "http://localhost:8000"
).rstrip("/")
# No placeholder default: an unset token surfaces as a warning at proxy start
# plus a blocked delivery, never a credential that might match a placeholder.
if not os.environ.get("SEDIMENT_API_BEARER_TOKEN"):
    logger.warning("SEDIMENT_API_BEARER_TOKEN not set; delivery requires credentials")
# When set, dump the raw payloads to this dir for fixture building.
CAPTURE_DIR = os.environ.get("SEDIMENT_CAPTURE_DIR")


def _capture_directory(path: Path) -> int:
    """Open a private directory without following user-controlled path links."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe capture directory")
    parts = list(path.parts[1:])
    # macOS exposes /tmp and /var as root-owned aliases. No user-created
    # alias, including a link further down either tree, receives this exception.
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
        if not parts:
            raise ValueError("unsafe capture directory")
        for index, part in enumerate(parts):
            try:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            final = index == len(parts) - 1
            if final:
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError("unsafe capture directory")
            elif info.st_uid not in {0, os.getuid()} or (
                info.st_mode & 0o022
                and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
            ):
                raise ValueError("unsafe capture ancestor")
        return directory
    except Exception:
        os.close(directory)
        raise


def _write_capture_files(path: Path, payloads: dict[str, bytes]) -> None:
    directory = _capture_directory(path)
    try:
        # Check both destinations before publishing either payload. Never repair
        # an existing permissive file after sensitive bytes have reached it.
        for name in payloads:
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("unsafe capture file")
        for name, data in payloads.items():
            temporary = ".capture-" + str(uuid4())
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    finally:
        os.close(directory)


class SedimentCallback(CustomLogger):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._start_delivery()
        atexit.register(self.close_delivery)

    @staticmethod
    def _environment() -> Mapping[str, str]:
        # A lifetime worker reads active configuration on every attempt.
        # Only the legacy endpoint has a default; revoked tokens stay absent.
        return ChainMap(os.environ, {"SEDIMENT_INGEST_URL": SEDIMENT_INGEST_URL})

    def _start_delivery(self) -> None:
        directory = os.environ.get("SEDIMENT_DELIVERY_DIR", "").strip()
        if not directory:
            logger.info("sediment_delivery mode=best_effort reason=buffer_disabled")
            return
        if delivery is None or self._stop.is_set():
            return
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._replay,
                args=(directory,),
                daemon=True,
                name="sediment-delivery",
            )
            try:
                self._worker.start()
            except Exception:
                self._worker = None
                logger.warning("sediment_delivery reason=worker_failed")

    def _replay(self, directory: str) -> None:
        while not self._stop.is_set():
            try:
                delivery.watch(
                    directory, env=self._environment(), stop_event=self._stop
                )
            except Exception:  # never expose payloads or transport exceptions
                logger.warning("sediment_delivery reason=worker_failed")
            if self._stop.wait(1):
                return

    def close_delivery(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=6)
        atexit.unregister(self.close_delivery)

    async def async_log_success_event(
        self, kwargs, response_obj, start_time, end_time
    ) -> None:
        capture_id = str(uuid4())
        observed_at = datetime.now(UTC).isoformat()
        try:
            self._start_delivery()
            if delivery is None:
                logger.warning(
                    "sediment_delivery delivery_id=%s reason=helper_unavailable",
                    capture_id,
                )
                return
            # SLO is a dict in current LiteLLM; coerce so a version that hands
            # us an object (or something else) degrades to the fallback
            # payload instead of dropping the completion.
            slo = kwargs.get("standard_logging_object")
            if hasattr(slo, "model_dump"):
                slo = slo.model_dump()
            slo = slo if isinstance(slo, dict) else {}
            if CAPTURE_DIR:
                self._capture(slo, kwargs, response_obj, start_time, end_time)
            payload = slo or self._fallback_payload(
                kwargs, response_obj, start_time, end_time
            )
            environment = self._environment()
            request = delivery.prepare_request(
                "gateway",
                environment["SEDIMENT_INGEST_URL"],
                json.dumps(
                    {
                        "provider": "litellm",
                        "payload": payload,
                        "capture": {"id": capture_id, "observed_at": observed_at},
                    },
                    allow_nan=False,
                ).encode("utf-8"),
                captured_at=observed_at,
                delivery_id=capture_id,
            )
            await asyncio.to_thread(delivery.deliver, request, env=environment)
        except Exception:  # noqa: BLE001 — never break the proxy on logging
            logger.warning(
                "sediment_delivery delivery_id=%s reason=preparation_failed",
                capture_id,
            )

    def _fallback_payload(
        self, kwargs, response_obj, start_time, end_time
    ) -> dict[str, Any]:
        """Shape a payload when no SLO is present — stamped with the
        identity carriers ``resolve_identity`` reads
        (``metadata.requester_metadata``, ``metadata.requester_custom_headers``,
        ``end_user``), so a completion whose identity arrived only via
        ``litellm_params.metadata``, request headers, or ``kwargs["user"]``
        still captures. Forwarding fields, not parsing them."""
        if hasattr(response_obj, "model_dump"):
            resp = response_obj.model_dump()
        else:
            resp = dict(response_obj or {})
        delta = end_time - start_time  # datetimes normally; floats on some paths
        seconds = (
            delta.total_seconds() if hasattr(delta, "total_seconds") else float(delta)
        )
        meta = (kwargs.get("litellm_params") or {}).get("metadata")
        meta = meta if isinstance(meta, dict) else {}
        metadata: dict[str, Any] = {"requester_metadata": meta}
        # The resolver reads Codex/pi headers at the metadata's own
        # requester_custom_headers key — lift them out of litellm_params so
        # header-borne sessions survive the no-SLO path too.
        headers = meta.get("requester_custom_headers")
        if isinstance(headers, dict):
            metadata["requester_custom_headers"] = headers
        return {
            "litellm_call_id": kwargs.get("litellm_call_id"),
            "model": kwargs.get("model", "unknown"),
            "messages": kwargs.get("messages", []),
            "response": resp,
            "usage": resp.get("usage", {}),
            "response_time_ms": seconds * 1000,
            "metadata": metadata,
            "end_user": kwargs.get("user"),
        }

    def _capture(self, slo, kwargs, response_obj, start_time, end_time) -> None:
        try:
            _write_capture_files(
                Path(CAPTURE_DIR),
                {
                    "standard_logging_object.json": json.dumps(
                        slo, default=str, indent=2
                    ).encode(),
                    "fallback_payload.json": json.dumps(
                        self._fallback_payload(
                            kwargs, response_obj, start_time, end_time
                        ),
                        default=str,
                        indent=2,
                    ).encode(),
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning("sediment_capture reason=fixture_write_failed")


handler = SedimentCallback()

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded, disposable processes for the API's six expensive operations."""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import sys

from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response
from sediment_core import Push, normalize_repo_slug
from sediment_derive.repository_identity import REPOSITORY_IDENTITY_SKIP_REASONS

from .config import settings

logger = logging.getLogger("sediment.api.workers")

QUERY_SLOTS = 2
MIRROR_SLOTS = 2
MIRROR_QUEUE_SIZE = 16
QUERY_BUDGET_SECONDS = 30.0
MIRROR_BUDGET_SECONDS = 120.0
TERMINATION_GRACE_SECONDS = 1.0
MAX_RESULT_BYTES = 64 * 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MIRROR_FREE_BYTES = 1024 * 1024 * 1024
_WORKER_COMMAND = (sys.executable, "-m", "sediment_api.worker")
_QUERY_KINDS = frozenset({"commit", "session", "model-report", "lifecycle-report"})
_MIRROR_KINDS = frozenset({"push", "rename"})


def _unavailable(reason: str) -> Response:
    return JSONResponse(status_code=503, content={"detail": reason})


def _group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin can report EPERM while the last member exits. This still
        # means "possibly present": keep checking instead of releasing a slot.
        return True


def _signal_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        # Cleanup keeps checking/reaping the group, including this exit race.
        pass


def _reap_descendants(pid: int) -> None:
    # The direct child belongs to asyncio's watcher; call this only after wait().
    while True:
        try:
            child, _ = os.waitpid(-pid, os.WNOHANG)
        except ChildProcessError:
            return
        if child == 0:
            return


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Stop the complete process group, including descendants left on success."""
    _signal_group(process.pid, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + TERMINATION_GRACE_SECONDS
    while _group_exists(process.pid):
        if process.returncode is not None:
            await process.wait()
            _reap_descendants(process.pid)
        if asyncio.get_running_loop().time() >= deadline:
            _signal_group(process.pid, signal.SIGKILL)
            break
        await asyncio.sleep(0.01)
    await process.wait()
    # Linux reparents owned grandchildren here because the supervisor is a
    # subreaper. On macOS the system reaper owns them. Never reap another group.
    while _group_exists(process.pid):
        _reap_descendants(process.pid)
        await asyncio.sleep(0.01)


async def _finish_cleanup(task: asyncio.Task) -> None:
    """Repeated caller cancellation must not abandon process cleanup."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    task.result()


class _OutputLimit(Exception):
    pass


class _WorkerFailure(Exception):
    pass


async def _read_result(stream: asyncio.StreamReader) -> Response:
    # Status line precedes the raw response body, avoiding a base64 copy of a
    # 64 MiB report. No worker-controlled response headers cross this boundary.
    status = await stream.readline()
    if status not in {b"200\n", b"409\n", b"422\n", b"500\n", b"503\n"}:
        raise _WorkerFailure
    body = bytearray()
    while chunk := await stream.read(64 * 1024):
        if len(body) + len(chunk) > MAX_RESULT_BYTES:
            raise _OutputLimit
        body.extend(chunk)
    try:
        # Decline malformed IPC rather than forwarding arbitrary worker stdout.
        json.loads(body)
    except (ValueError, RecursionError):
        raise _WorkerFailure from None
    return Response(bytes(body), status_code=int(status), media_type="application/json")


async def _read_diagnostics(stream: asyncio.StreamReader) -> None:
    received = 0
    while chunk := await stream.readline():
        received += len(chunk)
        if received > MAX_DIAGNOSTIC_BYTES:
            raise _WorkerFailure
        # Only the two closed count records cross into parent logs. Raw stderr,
        # exception text, and driver/Git diagnostics stay outside the API log.
        try:
            value = json.loads(chunk)
            event = value.get("event")
            repo = normalize_repo_slug(value.get("repo", ""))
            if event == "attributions_derived" and all(
                type(value.get(key)) is int and value[key] >= 0
                for key in ("attributed", "notes", "jaccard")
            ):
                logging.getLogger("sediment.api.forge").info(
                    "attributions_derived repo=%s attributed=%d notes=%d jaccard=%d",
                    repo,
                    value["attributed"],
                    value["notes"],
                    value["jaccard"],
                )
            elif event == "session_commit_observations_captured" and all(
                type(value.get(key)) is int and value[key] >= 0
                for key in ("stored", "duplicates")
            ):
                logging.getLogger("sediment.api.forge").info(
                    "session_commit_observations_captured repo=%s stored=%d duplicates=%d",
                    repo,
                    value["stored"],
                    value["duplicates"],
                )
            elif (
                event == "repository_identity_declined"
                and value.get("reason") in REPOSITORY_IDENTITY_SKIP_REASONS
                and type(value.get("count")) is int
                and value["count"] == 1
            ):
                logging.getLogger("sediment.api.forge").warning(
                    "repository_identity_declined repo=%s reason=%s count=1",
                    repo,
                    value["reason"],
                )
            elif event == "worker_operation_warning":
                logger.warning("worker_operation_warning")
        except (AttributeError, TypeError, ValueError):
            continue


async def _discard(stream: asyncio.StreamReader) -> None:
    while await stream.read(64 * 1024):
        pass


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    # Capture validated parent settings for each job. Database credentials never
    # enter command arguments, the pipe envelope, or diagnostic messages.
    environment.update(
        SEDIMENT_DATABASE_URL=settings.database_url.get_secret_value(),
        SEDIMENT_ORG_ID=settings.org_id,
        SEDIMENT_MIRROR_PATH=settings.mirror_path or "",
        SEDIMENT_DEV_MODE=str(settings.dev_mode).lower(),
        SEDIMENT_ALLOWED_CLONE_HOSTS=json.dumps(settings.allowed_clone_hosts),
    )
    return environment


def _request_data(kind: str, payload: dict) -> bytes:
    request = json.dumps({"kind": kind, "payload": payload}).encode()
    if len(request) > MAX_REQUEST_BYTES:
        raise HTTPException(
            status_code=503, detail="work request exceeds the fixed limit"
        )
    return request


async def _execute(request: bytes) -> Response:
    process = None
    readers: list[asyncio.Task] = []
    started = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *_WORKER_COMMAND,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=64 * 1024,
            env=_child_environment(),
        )
    )
    try:
        # Cancellation during spawn still owns the process that spawn creates.
        process = await asyncio.shield(started)
        readers = [
            asyncio.create_task(_read_result(process.stdout)),
            asyncio.create_task(_read_diagnostics(process.stderr)),
        ]
        process.stdin.write(request)
        await process.stdin.drain()
        process.stdin.close()
        result, _ = await asyncio.gather(*readers)
        if await process.wait() != 0:
            raise _WorkerFailure
        return result
    finally:

        async def cleanup() -> None:
            nonlocal process
            if process is None:
                # Shielded spawn may finish after its caller was cancelled.
                process = await started
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            # Drain pipes during termination even after a cap/failure stopped
            # their readers. A paused full pipe otherwise prevents EOF and can
            # keep asyncio's subprocess transport alive after wait().
            process.stdin.close()
            await asyncio.gather(
                _terminate(process),
                _discard(process.stdout),
                _discard(process.stderr),
            )

        await _finish_cleanup(asyncio.create_task(cleanup()))


class WorkerSupervisor:
    """Own transient admission and child lifetimes for one API process."""

    def __init__(self) -> None:
        if sys.platform == "linux":
            # PR_SET_CHILD_SUBREAPER: adopt orphaned Git descendants so they
            # cannot accumulate under a container PID 1 that does not reap them.
            if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
                raise RuntimeError("cannot initialize worker process cleanup")
        self._query_tasks: set[asyncio.Task] = set()
        self._mirror_tasks: set[asyncio.Task] = set()
        self._push_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._mirror_slots = asyncio.Semaphore(MIRROR_SLOTS)
        self._closed = False

    async def _run(self, kind: str, request: bytes, *, mirror: bool) -> Response:
        budget = MIRROR_BUDGET_SECONDS if mirror else QUERY_BUDGET_SECONDS
        try:
            async with asyncio.timeout(budget):
                if mirror:
                    async with self._mirror_slots:
                        return await _execute(request)
                return await _execute(request)
        except TimeoutError:
            logger.warning("worker_budget_exceeded kind=%s", kind)
            return _unavailable("work budget exceeded")
        except _OutputLimit:
            logger.warning("worker_output_limit_exceeded kind=%s", kind)
            return JSONResponse(
                status_code=409, content={"detail": "response exceeds the fixed limit"}
            )
        except (OSError, ValueError, _WorkerFailure) as error:
            logger.error("worker_failed kind=%s reason=%s", kind, type(error).__name__)
            return _unavailable("work unavailable")

    async def run(self, kind: str, payload: dict) -> Response:
        if kind not in _QUERY_KINDS:
            raise ValueError("unknown query job")
        if self._closed or len(self._query_tasks) >= QUERY_SLOTS:
            return _unavailable("work capacity exceeded")
        task = asyncio.create_task(
            self._run(kind, _request_data(kind, payload), mirror=False)
        )
        self._query_tasks.add(task)
        try:
            return await task
        finally:
            self._query_tasks.discard(task)

    def _submit_mirror(self, kind: str, payload: dict) -> asyncio.Task:
        if kind not in _MIRROR_KINDS:
            raise ValueError("unknown mirror job")
        if self._closed or len(self._mirror_tasks) >= MIRROR_SLOTS + MIRROR_QUEUE_SIZE:
            raise HTTPException(status_code=503, detail="mirror capacity exceeded")
        request = _request_data(kind, payload)
        path = Path(settings.mirror_path or ".").absolute()
        while not path.exists():
            path = path.parent
        try:
            free = shutil.disk_usage(path).free
        except OSError:
            free = 0
        if free < MIRROR_FREE_BYTES:
            logger.warning("mirror_storage_reserve_exhausted")
            raise HTTPException(status_code=503, detail="mirror storage unavailable")
        task = asyncio.create_task(self._run(kind, request, mirror=True))
        self._mirror_tasks.add(task)
        task.add_done_callback(self._mirror_tasks.discard)
        return task

    def submit_push(
        self,
        push: Push,
        *,
        fetch_repo: str | None = None,
        fetch_clone_url: str | None = None,
    ) -> asyncio.Task:
        key = (push.org_id, push.push_id)
        active = self._push_tasks.get(key)
        if active is not None and not active.done():
            return active
        task = self._submit_mirror(
            "push",
            {
                "push": push.model_dump(mode="json"),
                "fetch_repo": fetch_repo,
                "fetch_clone_url": fetch_clone_url,
            },
        )
        self._push_tasks[key] = task

        def completed(done: asyncio.Task) -> None:
            if self._push_tasks.get(key) is done:
                self._push_tasks.pop(key)

        task.add_done_callback(completed)
        return task

    async def rename(self, old_repo: str, new_repo: str) -> Response:
        task = self._submit_mirror(
            "rename", {"old_repo": old_repo, "new_repo": new_repo}
        )
        return await task

    async def wait_background(self, task: asyncio.Task) -> None:
        # A duplicate delivery observes the same work; its disconnect must not
        # cancel work another accepted Push delivery also depends on.
        response = await asyncio.shield(task)
        if response.status_code != 200:
            logger.warning(
                "mirror_background_work_failed status=%s", response.status_code
            )

    async def close(self) -> None:
        self._closed = True
        tasks = self._query_tasks | self._mirror_tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._query_tasks.clear()
        self._mirror_tasks.clear()
        self._push_tasks.clear()

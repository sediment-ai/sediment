# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real subprocess checks for API admission, deadlines, and process cleanup."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import pytest


def _command(monkeypatch, source: str) -> None:
    from sediment_api import workers

    monkeypatch.setattr(workers, "_WORKER_COMMAND", (sys.executable, "-c", source))


def _exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _file(path: Path) -> str:
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)
    return path.read_text()


def test_deadline_kills_process_group_before_slot_reuse(monkeypatch, tmp_path):
    from sediment_api import workers

    marker = tmp_path / "processes"
    _command(
        monkeypatch,
        "import os, subprocess, sys, time, signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(60)\n",
    )
    monkeypatch.setattr(workers, "QUERY_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(workers, "TERMINATION_GRACE_SECONDS", 0.1)

    async def check():
        supervisor = workers.WorkerSupervisor()
        try:
            response = await supervisor.run("commit", {"sha": "a" * 40})
            assert response.status_code == 503
            parent, child = map(int, marker.read_text().split())
            assert not _exists(parent)
            # A killed orphan can briefly remain a zombie until its OS reaper runs.
            async with asyncio.timeout(5):
                while _exists(child):
                    await asyncio.sleep(0.01)
            _command(monkeypatch, "print('200'); print('{}')")
            assert (
                await supervisor.run("commit", {"sha": "a" * 40})
            ).status_code == 200
        finally:
            await supervisor.close()

    asyncio.run(check())


def test_query_admission_has_no_waiting_queue(monkeypatch, tmp_path):
    from sediment_api import workers

    _command(monkeypatch, "import time; time.sleep(60)")

    async def check():
        supervisor = workers.WorkerSupervisor()
        first = asyncio.create_task(supervisor.run("commit", {"sha": "a" * 40}))
        second = asyncio.create_task(supervisor.run("session", {"session_id": "s"}))
        try:
            await asyncio.sleep(0.05)
            response = await supervisor.run("model-report", {})
            assert response.status_code == 503
            assert len(supervisor._query_tasks) == 2
        finally:
            await supervisor.close()
            await asyncio.gather(first, second, return_exceptions=True)
        assert not supervisor._query_tasks

    asyncio.run(check())


@pytest.mark.parametrize("operation", ["cancel", "shutdown"])
def test_cancellation_and_shutdown_reap_child(monkeypatch, tmp_path, operation):
    from sediment_api import workers

    marker = tmp_path / "pid"
    _command(
        monkeypatch,
        f"import os,time; open({str(marker)!r},'w').write(str(os.getpid())); time.sleep(60)",
    )

    async def check():
        supervisor = workers.WorkerSupervisor()
        pending = asyncio.create_task(supervisor.run("commit", {"sha": "a" * 40}))
        pid = int(await _file(marker))
        if operation == "cancel":
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await supervisor.close()
        await asyncio.gather(pending, return_exceptions=True)
        assert not _exists(pid)
        assert not supervisor._query_tasks

    asyncio.run(check())


@pytest.mark.parametrize(
    ("source", "status"),
    [
        ("print('not-a-status'); print('{}')", 503),
        ("print('200'); print('malformed JSON')", 503),
        ("print('200'); print('x' * 2000)", 409),
        (
            "import sys; sys.stderr.write('secret' * 50000); print('200'); print('{}')",
            503,
        ),
        ("import sys; sys.stderr.write('private password'); sys.exit(7)", 503),
    ],
)
def test_pipe_limits_and_malformed_failure_are_bounded(monkeypatch, source, status):
    from sediment_api import workers

    _command(monkeypatch, source)
    monkeypatch.setattr(workers, "MAX_RESULT_BYTES", 1024)
    monkeypatch.setattr(workers, "MAX_DIAGNOSTIC_BYTES", 1024)

    async def check():
        supervisor = workers.WorkerSupervisor()
        try:
            response = await supervisor.run("commit", {"sha": "a" * 40})
            assert response.status_code == status
            assert b"secret" not in response.body
            assert b"password" not in response.body
            assert not supervisor._query_tasks
        finally:
            await supervisor.close()

    asyncio.run(check())


def _push(index: int):
    from sediment_core import ForgeProvider, Push

    return Push(
        push_id=f"push-{index}",
        org_id="testorg",
        provider=ForgeProvider.GITHUB,
        repo="owner/repo",
        clone_url="https://github.com/owner/repo.git",
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=f"{index + 1:040x}",
    )


def test_mirrors_bound_processes_queue_and_only_coalesce_active_identity(
    monkeypatch, tmp_path
):
    from fastapi import HTTPException
    from sediment_api import workers

    marker = tmp_path / "started"
    _command(
        monkeypatch,
        f"import os,time; fd=os.open({str(marker)!r},os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600); os.write(fd,b'process\\n'); os.close(fd); time.sleep(60)",
    )

    async def check():
        supervisor = workers.WorkerSupervisor()
        try:
            first = supervisor.submit_push(_push(0))
            assert supervisor.submit_push(_push(0)) is first
            distinct = [supervisor.submit_push(_push(i)) for i in range(1, 18)]
            assert len({first, *distinct}) == 18
            with pytest.raises(HTTPException) as error:
                supervisor.submit_push(_push(18))
            assert error.value.status_code == 503
            async with asyncio.timeout(5):
                while not marker.exists() or len(marker.read_text().splitlines()) < 2:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            assert marker.read_text().splitlines() == ["process", "process"]
            # Cancelling one waiter does not cancel another delivery's work.
            waiter = asyncio.create_task(supervisor.wait_background(first))
            await asyncio.sleep(0)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            assert not first.done()
        finally:
            await supervisor.close()
        assert not supervisor._mirror_tasks
        assert not supervisor._push_tasks

    asyncio.run(check())


def test_failed_push_can_retry_same_identity(monkeypatch):
    from sediment_api import workers

    _command(monkeypatch, "raise SystemExit(1)")

    async def check():
        supervisor = workers.WorkerSupervisor()
        try:
            first = supervisor.submit_push(_push(0))
            assert (await first).status_code == 503
            _command(monkeypatch, "print('200'); print('{}')")
            retry = supervisor.submit_push(_push(0))
            assert retry is not first
            assert (await retry).status_code == 200
        finally:
            await supervisor.close()

    asyncio.run(check())


def test_cancellation_during_spawn_does_not_orphan_the_started_process(
    monkeypatch, tmp_path
):
    from sediment_api import workers

    marker = tmp_path / "pid"
    _command(
        monkeypatch,
        f"import os,time; open({str(marker)!r},'w').write(str(os.getpid())); time.sleep(60)",
    )
    spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        await asyncio.sleep(0.2)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)

    async def check():
        supervisor = workers.WorkerSupervisor()
        pending = asyncio.create_task(supervisor.run("commit", {"sha": "a" * 40}))
        pid = int(await _file(marker))
        pending.cancel()
        await asyncio.sleep(0.01)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        assert not _exists(pid)
        await supervisor.close()

    asyncio.run(check())


def test_successful_child_cannot_leave_a_live_descendant(monkeypatch, tmp_path):
    from sediment_api import workers

    marker = tmp_path / "pid"
    _command(
        monkeypatch,
        "import subprocess,sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(marker)!r},'w').write(str(child.pid))\n"
        "print('200'); print('{}')",
    )

    async def check():
        supervisor = workers.WorkerSupervisor()
        response = await supervisor.run("commit", {"sha": "a" * 40})
        assert response.status_code == 200
        assert not _exists(int(marker.read_text()))
        await supervisor.close()

    asyncio.run(check())


def test_mirror_queue_rejects_oversized_job_before_retaining_it(monkeypatch):
    from fastapi import HTTPException
    from sediment_api import workers

    monkeypatch.setattr(workers, "MAX_REQUEST_BYTES", 128, raising=False)

    async def check():
        supervisor = workers.WorkerSupervisor()
        try:
            with pytest.raises(HTTPException) as error:
                supervisor.submit_push(_push(0))
            assert error.value.status_code == 503
            assert not supervisor._mirror_tasks
        finally:
            await supervisor.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "envelope",
    [
        b"not-json",
        b'{"kind":"arbitrary-command","payload":{}}',
        b'{"kind":"commit","payload":{},"extra":"private"}',
        b'{"kind":"commit","payload":[]}',
    ],
)
def test_child_rejects_malformed_or_unknown_request_envelopes(envelope):
    from sediment_api import workers

    async def check():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "sediment_api.worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=workers._child_environment(),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(envelope), 5)
        assert process.returncode == 1
        assert stdout == stderr == b""

    asyncio.run(check())


def test_dying_process_group_permission_race_is_rechecked(monkeypatch, tmp_path):
    from sediment_api import workers

    marker = tmp_path / "pid"
    _command(
        monkeypatch,
        f"import os,time; open({str(marker)!r},'w').write(str(os.getpid())); time.sleep(60)",
    )
    monkeypatch.setattr(workers, "QUERY_BUDGET_SECONDS", 0.1)
    signal_group = os.killpg
    raced = False

    def dying_group(pid, sig):
        nonlocal raced
        if sig == 0 and not raced:
            raced = True
            raise PermissionError("last group member is exiting")
        return signal_group(pid, sig)

    monkeypatch.setattr(os, "killpg", dying_group)

    async def check():
        supervisor = workers.WorkerSupervisor()
        try:
            response = await supervisor.run("commit", {"sha": "a" * 40})
            assert b"budget exceeded" in response.body
            assert not _exists(int(marker.read_text()))
            assert raced
        finally:
            await supervisor.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    ("reason", "visible"),
    [("repository_mirror_identity_unresolved", True), ("private-token", False)],
)
def test_identity_refusal_crosses_worker_boundary_without_raw_diagnostics(
    monkeypatch, caplog, reason, visible
):
    from sediment_api import workers

    _command(
        monkeypatch,
        "import logging\n"
        "from sediment_api.worker import _Diagnostics\n"
        "logging.basicConfig(handlers=[_Diagnostics()], force=True)\n"
        "logging.warning('mirror_refresh_skipped repo=%s reason=%s count=1', "
        f"'owner/repo', ValueError({reason!r}))\n"
        "print('200'); print('{}')\n",
    )

    async def check():
        supervisor = workers.WorkerSupervisor()
        try:
            assert (
                await supervisor.run("commit", {"sha": "a" * 40})
            ).status_code == 200
        finally:
            await supervisor.close()

    asyncio.run(check())
    assert (reason in caplog.text) is visible
    if visible:
        assert "count=1" in caplog.text

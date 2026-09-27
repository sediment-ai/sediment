# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evidence shares both admission slots with other queries and reports."""

import asyncio
import json
import sys
from textwrap import dedent

import pytest

from test_worker_processes import _command, _exists, _file

EVIDENCE_KINDS = (
    "evidence-inventory",
    "evidence-manifest",
    "evidence-read",
    "context-retrieve",
    "context-discover",
    "context-selected",
    "context-evidence-inventory",
    "context-evidence-manifest",
    "context-evidence-read",
)


def _barrier_command(monkeypatch, root, *, descendants=False):
    spawned = []
    spawn = asyncio.create_subprocess_exec

    async def record_spawn(*args, **kwargs):
        spawned.append(args)
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_spawn)
    child = dedent("""\
        import signal, sys, time
        from pathlib import Path
        path = Path(sys.argv[1])
        def stop(*_):
            (path / 'terminating').touch()
            while not (path / 'reap').exists():
                time.sleep(0.01)
            sys.exit(0)
        signal.signal(signal.SIGTERM, stop)
        (path / 'child-ready').touch()
        while True:
            time.sleep(0.01)
        """)
    _command(
        monkeypatch,
        dedent(f"""\
            import json, os, subprocess, sys, time
            from pathlib import Path
            payload = json.load(sys.stdin)['payload']
            path = Path({str(root)!r}) / payload['session_id']
            path.mkdir()
            pids = [os.getpid()]
            if {descendants!r}:
                child = subprocess.Popen(
                    [sys.executable, '-c', {child!r}, str(path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                pids.append(child.pid)
                while not (path / 'child-ready').exists():
                    time.sleep(0.01)
            (path / 'starting').write_text(' '.join(map(str, pids)))
            (path / 'starting').rename(path / 'started')
            while not (path / 'release').exists():
                time.sleep(0.01)
            if payload.get('finish') == 'failure':
                sys.exit(7)
            if payload.get('finish') == 'representation':
                print('409')
                print('{{"detail":{{"reason":"non_finite_number"}}}}')
            else:
                print('200')
                print('{{}}')
            """),
    )
    return spawned


async def _started(root, name, pending):
    ready = asyncio.create_task(_file(root / name / "started"))
    try:
        done, _ = await asyncio.wait(
            {ready, pending}, return_when=asyncio.FIRST_COMPLETED
        )
        if pending in done:
            pytest.fail(f"worker exited before its barrier: {(await pending).body!r}")
        return tuple(map(int, (await ready).split()))
    finally:
        ready.cancel()
        await asyncio.gather(ready, return_exceptions=True)


@pytest.mark.parametrize(
    ("first_kind", "second_kind"),
    [("evidence-inventory", kind) for kind in EVIDENCE_KINDS]
    + [
        ("model-report", "lifecycle-report"),
        ("model-report", "evidence-read"),
        ("evidence-read", "lifecycle-report"),
        ("commit", "session"),
    ],
)
def test_read_kinds_share_two_slots(monkeypatch, tmp_path, first_kind, second_kind):
    from sediment_api import workers

    spawned = _barrier_command(monkeypatch, tmp_path)

    async def check():
        supervisor = workers.WorkerSupervisor()
        pending = []
        try:
            first = asyncio.create_task(supervisor.run(first_kind, {"session_id": "a"}))
            pending.append(first)
            first_pids = await _started(tmp_path, "a", first)
            second = asyncio.create_task(
                supervisor.run(second_kind, {"session_id": "b"})
            )
            pending.append(second)
            second_pids = await _started(tmp_path, "b", second)
            assert all(_exists(pid) for pid in first_pids + second_pids)
            for kind in (
                *EVIDENCE_KINDS,
                "commit",
                "session",
                "model-report",
                "lifecycle-report",
            ):
                result = await supervisor.run(kind, {"session_id": "refused"})
                assert result.status_code == 503
                assert json.loads(result.body) == {"detail": "work capacity exceeded"}
            assert len(spawned) == 2
            assert sorted(path.name for path in tmp_path.iterdir()) == ["a", "b"]
            assert len(supervisor._query_tasks) == 2

            (tmp_path / "a" / "release").touch()
            assert (await first).status_code == 200
            assert all(not _exists(pid) for pid in first_pids)
            report = asyncio.create_task(
                supervisor.run("model-report", {"session_id": "report"})
            )
            pending.append(report)
            await _started(tmp_path, "report", report)
            assert len(spawned) == 3
            assert not second.done()
            assert all(_exists(pid) for pid in second_pids)
            assert len(supervisor._query_tasks) == 2
            (tmp_path / "report" / "release").touch()
            (tmp_path / "b" / "release").touch()
            assert (await report).status_code == (await second).status_code == 200
        finally:
            await supervisor.close()
            await asyncio.gather(*pending, return_exceptions=True)
        assert not supervisor._query_tasks

    asyncio.run(check())


@pytest.mark.parametrize("kind", EVIDENCE_KINDS)
@pytest.mark.parametrize("finish", ["cancel", "deadline", "failure", "representation"])
def test_evidence_cleanup_holds_capacity_and_preserves_sibling(
    monkeypatch, tmp_path, finish, kind
):
    from sediment_api import workers

    spawned = _barrier_command(monkeypatch, tmp_path, descendants=True)
    monkeypatch.setattr(workers, "TERMINATION_GRACE_SECONDS", 10)
    timeout = asyncio.timeout
    budgets = []

    def capture_budget(seconds):
        scope = timeout(seconds)
        if seconds == workers.QUERY_BUDGET_SECONDS:
            budgets.append(scope)
        return scope

    monkeypatch.setattr(asyncio, "timeout", capture_budget)

    async def check():
        supervisor = workers.WorkerSupervisor()
        pending = []
        try:
            first = asyncio.create_task(
                supervisor.run(kind, {"session_id": "a", "finish": finish})
            )
            pending.append(first)
            first_pids = await _started(tmp_path, "a", first)
            sibling = asyncio.create_task(
                supervisor.run("evidence-inventory", {"session_id": "b"})
            )
            pending.append(sibling)
            sibling_pids = await _started(tmp_path, "b", sibling)
            if finish == "cancel":
                first.cancel()
            elif finish == "deadline":
                budgets[0].reschedule(asyncio.get_running_loop().time())
            else:
                (tmp_path / "a" / "release").touch()
            await _file(tmp_path / "a" / "terminating")
            assert _exists(first_pids[1])
            assert not first.done()
            assert not sibling.done()
            assert all(_exists(pid) for pid in sibling_pids)
            assert len(supervisor._query_tasks) == 2
            refused = await supervisor.run("model-report", {"session_id": "refused"})
            assert refused.status_code == 503
            assert json.loads(refused.body) == {"detail": "work capacity exceeded"}
            assert len(spawned) == 2
            assert not (tmp_path / "refused").exists()
            (tmp_path / "a" / "reap").touch()
            if finish == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await first
            else:
                result = await first
                assert result.status_code == (
                    409 if finish == "representation" else 503
                )
                if finish == "representation":
                    assert json.loads(result.body) == {
                        "detail": {"reason": "non_finite_number"}
                    }
                else:
                    assert json.loads(result.body) == {
                        "detail": "work budget exceeded"
                        if finish == "deadline"
                        else "work unavailable"
                    }
            assert all(not _exists(pid) for pid in first_pids)
            assert all(_exists(pid) for pid in sibling_pids)
            assert not sibling.done()
            assert len(supervisor._query_tasks) == 1
            _command(monkeypatch, "print('200'); print('{}')")
            assert (await supervisor.run("model-report", {})).status_code == 200
            assert len(supervisor._query_tasks) == 1
            assert all(_exists(pid) for pid in sibling_pids)
        finally:
            for path in tmp_path.iterdir():
                (path / "reap").touch()
            await supervisor.close()
            await asyncio.gather(*pending, return_exceptions=True)
        assert all(not _exists(pid) for pid in sibling_pids)
        assert not supervisor._query_tasks

    asyncio.run(check())


def test_close_reaps_both_evidence_groups_and_refuses_admission(monkeypatch, tmp_path):
    from sediment_api import workers

    spawned = _barrier_command(monkeypatch, tmp_path, descendants=True)
    monkeypatch.setattr(workers, "TERMINATION_GRACE_SECONDS", 10)

    async def check():
        supervisor = workers.WorkerSupervisor()
        pending = []
        pids = []
        closing = None
        try:
            for name, kind in zip(("a", "b"), ("evidence-read", "context-retrieve")):
                task = asyncio.create_task(supervisor.run(kind, {"session_id": name}))
                pending.append(task)
                pids.extend(await _started(tmp_path, name, task))
            closing = asyncio.create_task(supervisor.close())
            for name in ("a", "b"):
                await _file(tmp_path / name / "terminating")
            assert not closing.done()
            assert len(supervisor._query_tasks) == 2
            for name in ("a", "b"):
                (tmp_path / name / "reap").touch()
            await closing
            assert all(not _exists(pid) for pid in pids)
            assert not supervisor._query_tasks
            for kind in ("evidence-read", "model-report"):
                result = await supervisor.run(kind, {"session_id": "refused"})
                assert result.status_code == 503
                assert json.loads(result.body) == {"detail": "work capacity exceeded"}
            assert len(spawned) == 2
            assert not (tmp_path / "refused").exists()
        finally:
            for path in tmp_path.iterdir():
                (path / "reap").touch()
            await supervisor.close()
            await asyncio.gather(*pending, return_exceptions=True)
            if closing is not None:
                await closing

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["inventory", "manifest", "read"])
def test_evidence_public_routes_obey_worker_deadline(client, monkeypatch, kind):
    from sediment_api import workers

    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (sys.executable, "-c", "import time; time.sleep(60)"),
    )
    monkeypatch.setattr(workers, "QUERY_BUDGET_SECONDS", 0.05)
    auth = {"Authorization": "Bearer test-operator-token-3a7e-2f6c"}
    if kind == "read":
        response = client.post(
            "/query/evidence/read",
            headers=auth,
            json={
                "schema_version": 1,
                "session_id": "s",
                "references": [
                    {
                        "inference_call_id": "call",
                        "side": "input",
                        "message_index": 0,
                        "part_index": 0,
                    }
                ],
            },
        )
    else:
        response = client.get(
            "/query/evidence" + ("/manifest" if kind == "manifest" else ""),
            headers=auth,
            params={"session_id": "s", "inference_call_id": "call"},
        )
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert not client.app.state.workers._query_tasks
    assert client.get("/health").status_code == 200

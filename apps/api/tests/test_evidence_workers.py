# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evidence has one admission slot inside the shared query/report pool."""

import asyncio
import sys

import pytest

from test_worker_processes import _command, _exists, _file


def test_evidence_cannot_consume_the_second_query_report_slot(monkeypatch, tmp_path):
    from sediment_api import workers

    marker = tmp_path / "pid"
    _command(
        monkeypatch,
        f"import os,time; open({str(marker)!r},'w').write(str(os.getpid())); time.sleep(60)",
    )

    async def check():
        supervisor = workers.WorkerSupervisor()
        first = asyncio.create_task(
            supervisor.run("evidence-inventory", {"session_id": "s"})
        )
        try:
            await _file(marker)
            for kind in (
                "evidence-inventory",
                "evidence-manifest",
                "evidence-read",
                "context-retrieve",
                "context-discover",
                "context-selected",
            ):
                result = await supervisor.run(kind, {})
                assert result.status_code == 503
            assert len(supervisor._query_tasks) == 1
            _command(monkeypatch, "print('200'); print('{}')")
            assert (await supervisor.run("model-report", {})).status_code == 200
            assert len(supervisor._query_tasks) == 1
        finally:
            await supervisor.close()
            await asyncio.gather(first, return_exceptions=True)
        assert not supervisor._query_tasks
        assert not supervisor._evidence_tasks

    asyncio.run(check())


@pytest.mark.parametrize(
    "kind",
    ["evidence-read", "context-retrieve", "context-discover", "context-selected"],
)
@pytest.mark.parametrize("finish", ["cancel", "deadline", "failure", "representation"])
def test_evidence_releases_both_admission_slots(monkeypatch, tmp_path, finish, kind):
    from sediment_api import workers

    marker = tmp_path / "pid"
    tail = {
        "cancel": "time.sleep(60)",
        "deadline": "time.sleep(60)",
        "failure": "sys.exit(7)",
        "representation": 'print(\'409\'); print(\'{"detail":{"reason":"non_finite_number"}}\')',
    }[finish]
    _command(
        monkeypatch,
        f"import os,time,sys; open({str(marker)!r},'w').write(str(os.getpid())); {tail}",
    )
    if finish == "deadline":
        monkeypatch.setattr(workers, "QUERY_BUDGET_SECONDS", 0.3)

    async def check():
        supervisor = workers.WorkerSupervisor()
        try:
            pending = asyncio.create_task(supervisor.run(kind, {}))
            pid = int(await _file(marker))
            if finish == "cancel":
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            else:
                result = await pending
                assert result.status_code == (
                    409 if finish == "representation" else 503
                )
            assert not _exists(pid)
            assert not supervisor._query_tasks
            assert not supervisor._evidence_tasks
            _command(monkeypatch, "print('200'); print('{}')")
            assert (await supervisor.run("evidence-inventory", {})).status_code == 200
        finally:
            await supervisor.close()

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
    assert not client.app.state.workers._evidence_tasks
    assert client.get("/health").status_code == 200

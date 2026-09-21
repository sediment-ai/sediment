# SPDX-License-Identifier: AGPL-3.0-or-later
"""API routes use the bounded process boundary, including failure cases."""

from __future__ import annotations

import sys
import asyncio
import threading

import pytest


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/query/commit/" + "a" * 40, {}),
        ("/query/session/absent", {}),
        (
            "/v1/reports/model-outcomes",
            {
                "cohort_start": "2026-09-01T00:00:00Z",
                "cohort_end": "2026-09-02T00:00:00Z",
                "as_of": "2026-09-02T00:00:00Z",
            },
        ),
        (
            "/v1/reports/accepted-work-lifecycle",
            {
                "cohort_start": "2026-09-01T00:00:00Z",
                "cohort_end": "2026-09-02T00:00:00Z",
                "as_of": "2026-09-02T00:00:00Z",
            },
        ),
    ],
)
def test_every_expensive_read_uses_process_deadline(client, monkeypatch, path, params):
    from sediment_api import workers

    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (sys.executable, "-c", "import time; time.sleep(60)"),
    )
    monkeypatch.setattr(workers, "QUERY_BUDGET_SECONDS", 0.05)
    response = client.get(
        path,
        params=params,
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "work budget exceeded"}
    assert client.get("/health").status_code == 200


def test_worker_creates_an_independent_database_engine(client, monkeypatch):

    # The main engine cannot be borrowed by this worker. A worker reconstructs
    # its own engine using the validated database URL supplied by the parent.
    client.app.state.database_engine.dispose()
    response = client.get(
        "/query/session/absent",
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )
    assert response.status_code == 200
    assert response.json() == {"found": False}
    assert hasattr(client.app.state, "workers")
    assert not client.app.state.workers._query_tasks


def test_push_storage_survives_admission_failure_and_redelivery_retries(
    client, monkeypatch, tmp_path
):
    from sediment_api import workers
    from sediment_api.config import settings
    from test_push_mirror import _make_remote, _note, _post_push, _push_payload

    remote, head = _make_remote(tmp_path, note_body=_note("bounded-session"))
    monkeypatch.setattr(settings, "dev_mode", True)
    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    monkeypatch.setattr(workers, "MIRROR_QUEUE_SIZE", -workers.MIRROR_SLOTS)
    payload = _push_payload(str(remote), head)

    rejected = _post_push(client, payload)
    assert rejected.status_code == 503
    pushes = client.app.state.fact_store.read_pushes(settings.org_id)
    assert len(pushes) == 1

    monkeypatch.setattr(workers, "MIRROR_QUEUE_SIZE", 16)
    delivered = _post_push(client, payload)
    assert delivered.status_code == 200
    assert delivered.json()["stored"] is False
    observed = client.app.state.fact_store.read_session_commit_observations(
        settings.org_id
    )
    assert len(observed) == 1
    assert observed[0].source_push_id == pushes[0].push_id
    assert len(client.app.state.fact_store.read_pushes(settings.org_id)) == 1


def test_mirror_free_space_reserve_declines_after_storing_push(
    client, monkeypatch, tmp_path
):
    from sediment_api import workers
    from sediment_api.config import settings
    from test_push_mirror import _post_push, _push_payload

    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    monkeypatch.setattr(workers, "MIRROR_FREE_BYTES", 2**63)
    response = _post_push(
        client, _push_payload("https://github.com/owner/repo.git", "a" * 40)
    )
    assert response.status_code == 503
    assert len(client.app.state.fact_store.read_pushes(settings.org_id)) == 1


def test_overloaded_read_workers_leave_health_and_authenticated_ingest_responsive(
    client, monkeypatch
):
    from sediment_api import workers
    from test_push_mirror import _post_push, _push_payload

    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (sys.executable, "-c", "import time; time.sleep(60)"),
    )
    supervisor = client.app.state.workers

    def start():
        return [
            asyncio.create_task(supervisor.run("commit", {"sha": "a" * 40}))
            for _ in range(2)
        ]

    tasks = client.portal.call(start)
    try:
        assert client.get("/health").status_code == 200
        response = _post_push(
            client,
            _push_payload("https://github.com/owner/repo.git", "b" * 40, repo=""),
        )
        assert response.status_code == 200
        assert response.json()["stored"] is True
        for _ in range(8):
            assert (
                client.get(
                    "/query/session/absent",
                    headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
                ).status_code
                == 503
            )
        assert len(supervisor._query_tasks) == 2
    finally:

        async def finish():
            await supervisor.close()
            await asyncio.gather(*tasks, return_exceptions=True)

        client.portal.call(finish)


def test_repository_rename_receipt_does_not_wait_for_mirror_lock(
    client, monkeypatch, tmp_path
):
    import fcntl
    from sediment_api.config import settings
    from sediment_derive import MirrorManager
    from test_push_mirror import _post_repository, _repo_rename_payload

    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    manager = MirrorManager(settings.mirror_path)
    lock = manager._lock_file(
        manager._mirror_path(settings.org_id, "acme-corp/old-service").name
    )
    responses = []
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        pending = threading.Thread(
            target=lambda: responses.append(
                _post_repository(
                    client, _repo_rename_payload("old-service", "acme-corp/new-service")
                )
            )
        )
        pending.start()
        try:
            pending.join(timeout=5)
            assert not pending.is_alive()
            [response] = responses
            assert response.status_code == 200
            assert response.json()["stored"] is True
            [rename] = client.app.state.fact_store.read_repository_renames(
                settings.org_id
            )
            assert rename.rename_id == response.json()["fact_id"]
            assert rename.old_repo == "acme-corp/old-service"
            assert rename.new_repo == "acme-corp/new-service"
            assert not client.app.state.workers._mirror_tasks
            assert client.get("/health").status_code == 200
            assert (
                client.get(
                    "/v1/facts",
                    headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
                ).status_code
                == 200
            )
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            pending.join(timeout=5)


def test_distinct_concurrent_pushes_preserve_both_git_observation_sets(
    client, monkeypatch, tmp_path
):
    from sediment_api.config import settings
    from test_push_mirror import _make_three_commit_remote, _post_push, _push_payload

    remote, shas = _make_three_commit_remote(tmp_path)
    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    monkeypatch.setattr(settings, "dev_mode", True)
    first = _push_payload(str(remote), shas[0])
    second = _push_payload(str(remote), shas[2]) | {"before": shas[0]}
    responses = []
    threads = [
        threading.Thread(
            target=lambda payload=p: responses.append(_post_push(client, payload))
        )
        for p in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert [response.status_code for response in responses] == [200, 200]
    store = client.app.state.fact_store
    pushes = {
        push.after_sha: push.push_id for push in store.read_pushes(settings.org_id)
    }
    observations = store.read_session_commit_observations(settings.org_id)
    assert {
        (row.commit_sha, row.session_id, row.source_push_id) for row in observations
    } == {
        (shas[0], "s-0", pushes[shas[0]]),
        (shas[1], "s-1", pushes[shas[2]]),
        (shas[2], "s-2", pushes[shas[2]]),
    }


def test_stalled_git_remote_expires_without_blocking_ingest(
    client, monkeypatch, tmp_path
):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from sediment_api import workers
    from sediment_api.config import settings
    from test_push_mirror import _post_push, _push_payload

    entered, release = threading.Event(), threading.Event()

    class StalledRemote(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            release.wait(timeout=10)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), StalledRemote)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    monkeypatch.setattr(settings, "dev_mode", True)
    monkeypatch.setattr(workers, "MIRROR_BUDGET_SECONDS", 2)
    responses = []
    pending = threading.Thread(
        target=lambda: responses.append(
            _post_push(
                client,
                _push_payload(
                    f"http://127.0.0.1:{server.server_port}/repo.git", "a" * 40
                ),
            )
        )
    )
    pending.start()
    try:
        assert entered.wait(timeout=5)
        assert client.get("/health").status_code == 200
        assert (
            _post_push(
                client,
                _push_payload("https://github.com/owner/repo.git", "b" * 40, repo=""),
            ).status_code
            == 200
        )
        pending.join(timeout=5)
        assert not pending.is_alive()
        assert responses[0].status_code == 200
        assert not client.app.state.workers._mirror_tasks
        assert len(client.app.state.fact_store.read_pushes(settings.org_id)) == 2
        assert (
            client.app.state.fact_store.read_session_commit_observations(
                settings.org_id
            )
            == []
        )
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        pending.join(timeout=5)
        serving.join(timeout=5)

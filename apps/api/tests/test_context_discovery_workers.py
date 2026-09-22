# SPDX-License-Identifier: AGPL-3.0-or-later
"""The subprocess repeats the Session boundary independently of HTTP admission."""

import asyncio
import json

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from sediment_api.config import settings
from sediment_api.worker import WorkerRequest, _dispatch
from sediment_api.workers import WorkerSupervisor

from test_context_query import TOKEN


class NoStorage:
    def read_snapshot(self):
        pytest.fail("unauthorized worker opened a Fact snapshot")


@pytest.fixture
def worker_grant(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "retrieval_session_id", None)
    monkeypatch.setattr(settings, "retrieval_session_ids", ["allowed"])


def test_worker_rechecks_membership_before_storage(worker_grant):
    with pytest.raises(HTTPException) as failure:
        _dispatch(
            WorkerRequest(
                "context-selected",
                {
                    "schema_version": 1,
                    "session_id": "forbidden-session",
                    "query": "goal",
                },
            ),
            NoStorage(),
        )
    assert failure.value.status_code == 403
    assert "forbidden-session" not in failure.value.detail


@pytest.mark.parametrize(
    "kind", ["context-retrieve", "context-discover", "context-selected"]
)
def test_worker_rechecks_disabled_grant_before_storage(worker_grant, monkeypatch, kind):
    monkeypatch.setattr(settings, "retrieval_token", None)
    monkeypatch.setattr(settings, "retrieval_session_ids", None)
    body = {"schema_version": 1, "query": "goal"}
    if kind == "context-selected":
        body["session_id"] = "allowed"
    with pytest.raises(HTTPException) as failure:
        _dispatch(WorkerRequest(kind, body), NoStorage())
    assert failure.value.status_code == 404


def test_worker_never_defaults_plural_grant_for_legacy(worker_grant):
    with pytest.raises(HTTPException) as failure:
        _dispatch(
            WorkerRequest("context-retrieve", {"schema_version": 1, "query": "goal"}),
            NoStorage(),
        )
    assert failure.value.status_code == 404


def test_child_refusal_survives_ipc_without_lookup(client, worker_grant):
    async def check():
        supervisor = WorkerSupervisor()
        try:
            response = await supervisor.run(
                "context-selected",
                {
                    "schema_version": 1,
                    "session_id": "forbidden-session",
                    "query": "goal",
                },
            )
            assert response.status_code == 403
            assert "forbidden-session" not in response.body.decode()
            assert json.loads(response.body)["detail"]
            response = await supervisor.run(
                "context-retrieve", {"schema_version": 1, "query": "goal"}
            )
            assert response.status_code == 404
            assert not supervisor._query_tasks
            assert not supervisor._evidence_tasks
        finally:
            await supervisor.close()

    asyncio.run(check())

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compute allocation ingest and Session dossier exposure — real FactStore."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sediment_api.config import settings
from sediment_api.main import app
from sediment_core import ComputeAllocationSource, FactTable

INGEST = {"Authorization": "Bearer test-ingest-token-3a7e-9d21"}
OPERATOR = {"Authorization": "Bearer test-operator-token-3a7e-2f6c"}


def _payload(**changes) -> dict:
    body = {
        "session_id": "sandbox-session",
        "allocation_source": "agent_request",
        "cpu_millicores": 8000,
        "memory_bytes": 16 * 2**30,
        "occurred_at": "2026-10-01T12:00:00Z",
    }
    body.update(changes)
    return {key: value for key, value in body.items() if value is not None}


def test_compute_ingest_stores_and_collapses_redelivery(client: TestClient) -> None:
    first = client.post("/ingest/compute", json=_payload(), headers=INGEST)
    assert first.status_code == 200
    assert first.json()["stored"] is True

    again = client.post("/ingest/compute", json=_payload(), headers=INGEST)
    assert again.status_code == 200
    assert again.json() == {"fact_id": first.json()["fact_id"], "stored": False}

    [stored] = app.state.fact_store.read_compute_allocations(settings.org_id)
    assert stored.allocation_id == first.json()["fact_id"]
    assert stored.org_id == settings.org_id
    assert stored.allocation_source is ComputeAllocationSource.AGENT_REQUEST
    assert (stored.cpu_millicores, stored.memory_bytes) == (8000, 16 * 2**30)


def test_compute_ingest_accepts_one_limit(client: TestClient) -> None:
    response = client.post(
        "/ingest/compute",
        json=_payload(memory_bytes=None, allocation_source="default"),
        headers=INGEST,
    )
    assert response.status_code == 200
    [stored] = app.state.fact_store.read_compute_allocations(settings.org_id)
    assert stored.memory_bytes is None


def test_compute_ingest_rejects_malformed_envelopes(client: TestClient) -> None:
    for payload in (
        _payload(cpu_millicores=None, memory_bytes=None),
        _payload(cpu_millicores=0),
        _payload(cpu_millicores=1.5),
        _payload(cpu_millicores="8000"),
        _payload(memory_bytes=True),
        _payload(memory_bytes=2**63),
        _payload(allocation_source="autoscaler"),
        _payload(occurred_at="2026-10-01T12:00:00"),
        _payload(occurred_at="9999-12-31T23:59:59-14:00"),
        _payload(occurred_at="0001-01-01T00:00:00+14:00"),
        _payload(session_id="   "),
        {**_payload(), "org_id": "another-org"},
    ):
        response = client.post("/ingest/compute", json=payload, headers=INGEST)
        assert response.status_code == 422, payload
    assert app.state.fact_store.read_compute_allocations(settings.org_id) == []


def test_session_dossier_shows_compute_allocations(client: TestClient) -> None:
    client.post(
        "/ingest/compute",
        json=_payload(allocation_source="default", cpu_millicores=2000),
        headers=INGEST,
    )
    client.post(
        "/ingest/compute",
        json=_payload(occurred_at="2026-10-01T12:05:00Z"),
        headers=INGEST,
    )

    response = client.get("/query/session/sandbox-session", headers=OPERATOR)

    assert response.status_code == 200
    body = response.json()
    assert body["coverage"]["compute_allocations"] == {
        "total": 2,
        "visible": 2,
        "quarantined": 0,
    }
    # Installed `sediment doctor` copies reject unknown timeline event types.
    assert body["timeline"] == []
    assert [
        (item["allocation_source"], item["cpu_millicores"])
        for item in body["compute_allocations"]
    ] == [("default", 2000), ("agent_request", 8000)]
    assert all(
        item["memory_bytes"] == 16 * 2**30 for item in body["compute_allocations"]
    )
    assert "no_inference_calls_observed" in body["gaps"]

    first = body["compute_allocations"][0]["allocation_id"]
    app.state.fact_store.quarantine_fact(
        settings.org_id, FactTable.COMPUTE_ALLOCATIONS, first, reason="test"
    )
    quarantined = client.get("/query/session/sandbox-session", headers=OPERATOR)
    body = quarantined.json()
    # A quarantined allocation never marks decision evidence incomplete.
    assert body["omitted_events"] == 0
    assert body["coverage"]["compute_allocations"]["quarantined"] == 1
    assert [item["allocation_id"] for item in body["compute_allocations"]] != [first]
    assert len(body["compute_allocations"]) == 1

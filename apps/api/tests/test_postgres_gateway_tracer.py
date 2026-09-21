# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from sediment_api.config import settings
from sediment_api.deps import get_store
from sediment_api.main import app
from sediment_core import InferenceCall, InferenceCallReceipt
from sediment_core import FactStore
from sediment_core.redaction import REDACTION_MARKER, redact_fact

FIXTURE = (
    Path(__file__).parents[3]
    / "packages"
    / "capture"
    / "tests"
    / "fixtures"
    / "litellm_standard_logging_object.json"
)


def test_authenticated_gateway_tracer_round_trips_through_postgres(
    postgres_engine,
    postgres_database_url,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "database_url", SecretStr(postgres_database_url))
    monkeypatch.setattr(settings, "dev_mode", True)
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    credential = "sk-proj-postgres-tracer-abcdefghijklmnopqrstuvwxyz"
    payload["metadata"]["requester_metadata"]["difficult_text"] = (
        "lone=\ud800 null=\x00"
    )
    payload["metadata"]["requester_metadata"]["authorization"] = f"Bearer {credential}"
    body = {
        "provider": "litellm",
        "session_id": "postgres-tracer-session",
        "user_id": "postgres-tracer-user",
        "payload": payload,
    }
    store = FactStore(postgres_engine)
    translated: list[InferenceCall] = []

    class RecordingStore:
        def store_inference_call_receipt(
            self, call: InferenceCall
        ) -> InferenceCallReceipt:
            translated.append(call)
            return store.store_inference_call_receipt(call)

    async def postgres_store_override():
        yield RecordingStore()

    app.dependency_overrides[get_store] = postgres_store_override
    try:
        with TestClient(app) as client:
            rejected = client.post(
                "/ingest/gateway",
                content=json.dumps(body, ensure_ascii=True),
                headers={
                    "Authorization": "Bearer wrong-token",
                    "Content-Type": "application/json",
                },
            )
            assert rejected.status_code == 401

            headers = {
                "Authorization": f"Bearer {settings.api_bearer_token}",
                "Content-Type": "application/json",
            }
            response = client.post(
                "/ingest/gateway",
                content=json.dumps(body, ensure_ascii=True),
                headers=headers,
            )
            redelivery = client.post(
                "/ingest/gateway",
                content=json.dumps(body, ensure_ascii=True),
                headers=headers,
            )
    finally:
        app.dependency_overrides.pop(get_store, None)

    assert response.status_code == 200
    assert response.json()["stored"] is True
    assert redelivery.status_code == 200
    assert redelivery.json()["stored"] is False
    assert redelivery.json()["fact_id"] == response.json()["fact_id"]

    [stored] = store.read_inference_calls(settings.org_id)
    assert len(translated) == 2
    expected, _ = redact_fact(translated[0])
    differences = {
        field: (getattr(stored, field), getattr(expected, field))
        for field in type(stored).model_fields
        if getattr(stored, field) != getattr(expected, field)
    }
    assert differences == {}
    assert stored.raw["metadata"]["requester_metadata"]["difficult_text"] == (
        "lone=\ud800 null=\x00"
    )
    assert stored.raw["metadata"]["requester_metadata"]["authorization"] == (
        f"Bearer {REDACTION_MARKER}"
    )

    with postgres_engine.connect() as connection:
        raw, raw_type, session_count = connection.execute(
            text(
                "SELECT i.raw, pg_typeof(i.raw)::text, count(s.session_id) "
                "FROM inference_calls i "
                "JOIN sessions s USING (org_id, session_id) "
                "GROUP BY i.raw"
            )
        ).one()
    assert raw_type == "text"
    assert "\\ud800" in raw
    assert "\\u0000" in raw
    assert credential not in raw
    assert REDACTION_MARKER in raw
    assert session_count == 1

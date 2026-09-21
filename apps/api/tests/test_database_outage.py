# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import logging

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from sediment_api.config import settings
from sediment_api.main import app

AUTH = {"Authorization": "Bearer test-operator-token-3a7e-2f6c"}


def test_runtime_database_outage_preserves_http_contracts_and_credentials(
    postgres_database_factory,
    monkeypatch,
    caplog,
) -> None:
    postgres_database_url = postgres_database_factory()
    url = make_url(postgres_database_url)
    database_name = url.database
    assert database_name is not None
    admin_url = url.set(database="postgres")
    admin_engine = create_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    monkeypatch.setattr(settings, "database_url", SecretStr(postgres_database_url))
    monkeypatch.setattr(settings, "dev_mode", True)
    caplog.set_level(logging.INFO)

    gateway_payload = {
        "provider": "litellm",
        "session_id": "outage-session",
        "user_id": "outage-user",
        "payload": {
            "model": "test-model",
            "messages": [],
            "response": {
                "id": "outage-call",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
            "litellm_call_id": "outage-call",
        },
    }
    otlp_payload = {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "1783542925478460000",
                                "body": {"stringValue": "claude_code.tool_decision"},
                                "attributes": [
                                    {"key": key, "value": {"stringValue": value}}
                                    for (key, value) in {
                                        "tool_name": "Edit",
                                        "decision": "accept",
                                        "source": "user_temporary",
                                        "session.id": "outage-session",
                                        "tool_use_id": "outage-call",
                                    }.items()
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    records = otlp_payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    sibling = json.loads(json.dumps(records[0]))
    for attribute in sibling["attributes"]:
        if attribute["key"] == "tool_use_id":
            attribute["value"]["stringValue"] = "outage-call-2"
    records.append(sibling)
    gateway_bytes = json.dumps(gateway_payload).encode()
    otlp_bytes = json.dumps(otlp_payload).encode()
    headers = {**AUTH, "Content-Type": "application/json"}

    with TestClient(app) as client:
        store = app.state.fact_store
        try:
            with admin_engine.connect() as connection:
                connection.exec_driver_sql(
                    f'ALTER DATABASE "{database_name}" WITH ALLOW_CONNECTIONS false'
                )
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                    ),
                    {"database_name": database_name},
                )

            bad_auth = client.post(
                "/ingest/gateway",
                json={},
                headers={"Authorization": "Bearer wrong"},
            )
            malformed = client.post("/ingest/gateway", json={}, headers=AUTH)
            valid = client.post(
                "/ingest/gateway", headers=headers, content=gateway_bytes
            )
            otlp = client.post("/v1/logs", headers=headers, content=otlp_bytes)

        finally:
            try:
                with admin_engine.connect() as connection:
                    connection.exec_driver_sql(
                        f'ALTER DATABASE "{database_name}" WITH ALLOW_CONNECTIONS true'
                    )
            finally:
                admin_engine.dispose()

        # The same service and pool recover after the owned database is restored.
        assert app.state.fact_store is store
        assert store.read_inference_calls(settings.org_id) == []
        assert store.read_decisions(settings.org_id) == []
        assert store.read_sessions(settings.org_id) == []
        restored_gateway = client.post(
            "/ingest/gateway", headers=headers, content=gateway_bytes
        )
        restored_otlp = client.post("/v1/logs", headers=headers, content=otlp_bytes)
        assert restored_gateway.status_code == restored_otlp.status_code == 200
        assert restored_gateway.json()["stored"] is True
        assert restored_otlp.json() == {}
        [call] = store.read_inference_calls(settings.org_id)
        assert call.inference_call_id == restored_gateway.json()["fact_id"]
        assert call.raw == gateway_payload["payload"]
        assert call.model == "test-model"
        assert call.output_messages[0].parts[0].content == "ok"
        decisions = store.read_decisions(settings.org_id)
        assert {decision.call_id for decision in decisions} == {
            "outage-call",
            "outage-call-2",
        }
        assert len(decisions) == 2
        assert all(decision.accepted and decision.explicit for decision in decisions)
        assert {json.dumps(decision.raw, sort_keys=True) for decision in decisions} == {
            json.dumps({"decision": record, "result": None}, sort_keys=True)
            for record in records
        }
        sessions = store.read_sessions(settings.org_id)
        assert len(sessions) == 1
        assert sessions[0].session_id == "outage-session"
        assert sessions[0].user_id == "outage-user"
        assert sessions[0].user_id_conflict is False
        replay_gateway = client.post(
            "/ingest/gateway", headers=headers, content=gateway_bytes
        )
        replay_otlp = client.post("/v1/logs", headers=headers, content=otlp_bytes)
        assert replay_gateway.status_code == replay_otlp.status_code == 200
        assert replay_gateway.json()["stored"] is False
        assert replay_otlp.json() == {}
        assert store.read_inference_calls(settings.org_id) == [call]
        assert store.read_decisions(settings.org_id) == decisions
        assert store.read_sessions(settings.org_id) == sessions

    assert bad_auth.status_code == 401
    assert malformed.status_code == 422
    assert otlp.status_code == 503
    assert otlp.json()["detail"]["code"] == "database_unavailable"
    assert valid.status_code == 503
    assert valid.json() == {
        "detail": {
            "code": "database_unavailable",
            "message": "PostgreSQL fact store unavailable",
        }
    }
    rendered_logs = caplog.text
    assert url.password not in rendered_logs
    assert str(admin_url) not in rendered_logs

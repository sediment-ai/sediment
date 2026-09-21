# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public evidence reads cross the HTTP, worker, and PostgreSQL boundaries."""

from datetime import UTC, datetime, timedelta
import json

import pytest
from sediment_core import (
    EVIDENCE_REQUEST_BYTES_LIMIT,
    FactTable,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)

AUTH = {"Authorization": "Bearer test-operator-token-3a7e-2f6c"}
SESSION = "session/%?#"
CALL = "call/%?#"
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _call(identifier=CALL, session=SESSION, org="testorg", **overrides):
    values = dict(
        inference_call_id=identifier,
        session_id=session,
        org_id=org,
        gateway_provider=GatewayProvider.LITELLM,
        model="model/%?#",
        model_provider="provider",
        observed_at=T0,
        user_id="private-user",
        raw={"secret": "private-raw"},
        input_messages=[
            InferenceMessage(role="user\x00\ud800", parts=[]),
            InferenceMessage(
                role="user",
                parts=[
                    TextPart(content="goal\x00\ud800"),
                    ReasoningPart(content="plan"),
                ],
            ),
        ],
        output_messages=[
            InferenceMessage(
                role="assistant",
                finish_reason="tool_calls",
                parts=[
                    ToolCallPart(id="alias", name="read", arguments={"huge": 2**100}),
                    ToolCallResponsePart(id="alias", result={"value": 0.25}),
                    TextPart(content="goal\x00\ud800"),
                ],
            )
        ],
    )
    return InferenceCall(**(values | overrides))


def _ref(identifier=CALL, side="output", message=0, part=0):
    return dict(
        inference_call_id=identifier,
        side=side,
        message_index=message,
        part_index=part,
    )


def _fetch(client, refs=None, session=SESSION, **kwargs):
    return client.post(
        "/query/evidence/read",
        headers=AUTH,
        json={
            "schema_version": 1,
            "session_id": session,
            "references": refs or [_ref()],
        },
        **kwargs,
    )


def _inventory(client, session=SESSION):
    return client.get("/query/evidence", params={"session_id": session}, headers=AUTH)


def _manifest(client, identifier=CALL, session=SESSION):
    return client.get(
        "/query/evidence/manifest",
        params={"session_id": session, "inference_call_id": identifier},
        headers=AUTH,
    )


def test_evidence_routes_preserve_exact_occurrences_and_values(client):
    call = _call()
    client.app.state.fact_store.store_inference_call(call)
    inventory = _inventory(client)
    assert inventory.status_code == 200
    assert inventory.headers["cache-control"] == "no-store"
    assert inventory.json() == {
        "schema_version": 1,
        "session_id": SESSION,
        "quarantine_revision": 0,
        "found": True,
        "capture_completeness": "unknown",
        "visible_inference_calls": 1,
        "quarantined_inference_calls": 0,
        "calls": [
            {
                "inference_call_id": CALL,
                "observed_at": "2026-09-01T00:00:00Z",
                "model_provider": "provider",
                "model": "model/%?#",
            }
        ],
    }
    manifest = _manifest(client)
    assert manifest.status_code == 200
    messages = manifest.json()["messages"]
    assert [(m["side"], m["message_index"]) for m in messages] == [
        ("input", 0),
        ("input", 1),
        ("output", 0),
    ]
    assert messages[0] == {
        "side": "input",
        "message_index": 0,
        "role": "user\x00\ud800",
        "finish_reason": None,
        "parts": [],
    }
    refs = [
        _ref(part=1),
        _ref(side="input", message=1),
        _ref(side="input", message=1, part=1),
        _ref(),
        _ref(part=2),
    ]
    response = _fetch(client, refs)
    assert response.status_code == 200
    assert [item["reference"] for item in response.json()["items"]] == refs
    assert [item["part"] for item in response.json()["items"]] == [
        call.output_messages[0].parts[1].model_dump(),
        call.input_messages[1].parts[0].model_dump(),
        call.input_messages[1].parts[1].model_dump(),
        call.output_messages[0].parts[0].model_dump(),
        call.output_messages[0].parts[2].model_dump(),
    ]
    for result in (inventory, manifest, response):
        assert result.headers["cache-control"] == "no-store"
        assert result.content.isascii()
        assert b"private-user" not in result.content
        assert b"private-raw" not in result.content
    assert b"goal" not in manifest.content
    assert b"arguments" not in manifest.content


@pytest.mark.parametrize("kind", ["inventory", "manifest", "read"])
@pytest.mark.parametrize(
    "token,status", [(None, 401), ("bad", 401), ("test-ingest-token-3a7e-9d21", 403)]
)
def test_evidence_routes_require_operator_authority(client, kind, token, status):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    if kind == "read":
        response = client.post("/query/evidence/read", headers=headers, json={})
    else:
        response = client.get(
            "/query/evidence" + ("/manifest" if kind == "manifest" else ""),
            headers=headers,
            params={"session_id": SESSION, "inference_call_id": CALL},
        )
    assert response.status_code == status


def test_evidence_unknown_inventory_is_explicit(client):
    result = _inventory(client)
    assert result.status_code == 200
    assert result.json()["found"] is False
    assert result.json()["calls"] == []
    assert result.json()["visible_inference_calls"] == 0


@pytest.mark.parametrize(
    "condition", ["missing", "foreign", "other-session", "quarantine"]
)
def test_evidence_unavailability_does_not_disclose_cause(client, condition):
    store = client.app.state.fact_store
    if condition != "missing":
        store.store_inference_call(
            _call(
                org="foreign" if condition == "foreign" else "testorg",
                session="other" if condition == "other-session" else SESSION,
            )
        )
    if condition == "quarantine":
        store.quarantine_fact("testorg", FactTable.INFERENCE_CALLS, CALL, reason="test")
    result = _fetch(client)
    assert result.status_code == 409
    assert result.json() == {
        "detail": {"reason": "evidence_unavailable", "reference_index": 0}
    }
    result = _manifest(client)
    assert result.status_code == 409
    assert result.json() == {"detail": {"reason": "evidence_unavailable"}}


def test_evidence_live_reads_observe_backdated_insert_then_quarantine(client):
    store = client.app.state.fact_store
    store.store_inference_call(_call())
    assert _inventory(client).json()["visible_inference_calls"] == 1
    store.store_inference_call(_call("late", observed_at=T0 - timedelta(days=1)))
    assert [x["inference_call_id"] for x in _inventory(client).json()["calls"]] == [
        "late",
        CALL,
    ]
    store.quarantine_fact("testorg", FactTable.INFERENCE_CALLS, CALL, reason="test")
    result = _inventory(client).json()
    assert result["visible_inference_calls"] == 1
    assert result["quarantined_inference_calls"] == 1
    assert result["quarantine_revision"] == 1
    assert _fetch(client).status_code == 409


def test_evidence_selected_nonfinite_refuses_complete_packet_only(client):
    store = client.app.state.fact_store
    store.store_inference_call(
        _call(
            output_messages=[
                InferenceMessage(
                    role="assistant",
                    parts=[
                        TextPart(content="safe"),
                        ToolCallResponsePart(
                            id="alias", result={"value": float("nan")}
                        ),
                    ],
                )
            ]
        )
    )
    assert _manifest(client).status_code == 200
    assert _fetch(client).status_code == 200
    result = _fetch(client, [_ref(), _ref(part=1)])
    assert result.status_code == 409
    assert result.json() == {"detail": {"reason": "non_finite_number"}}
    assert b"safe" not in result.content
    assert not client.app.state.workers._query_tasks
    assert _inventory(client).status_code == 200


@pytest.mark.parametrize(
    "bad",
    [
        {"schema_version": True},
        {"schema_version": 1.0},
        {"schema_version": "1"},
        {"schema_version": 2},
        {"references": []},
        {"references": [_ref(), _ref()]},
        {"references": [_ref(part=i) for i in range(33)]},
        {"org_id": "foreign"},
        {"references": [_ref(part=True)]},
        {"references": [_ref(part=1.0)]},
        {"references": [_ref(part=-1)]},
        {"references": [_ref() | {"secret": "private"}]},
    ],
)
def test_evidence_fetch_strict_envelope_rejects_malformed_inputs(client, bad):
    body = {"schema_version": 1, "session_id": SESSION, "references": [_ref()]} | bad
    response = client.post("/query/evidence/read", headers=AUTH, json=body)
    assert response.status_code == 422
    assert "input" not in response.json()["detail"][0]
    assert b"private" not in response.content


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("extra,status", [(0, 200), (1, 413)])
def test_evidence_body_limit_precedes_json_decoding(client, streamed, extra, status):
    client.app.state.fact_store.store_inference_call(_call())
    body = json.dumps(
        {"schema_version": 1, "session_id": SESSION, "references": [_ref()]}
    ).encode()
    body += b" " * (EVIDENCE_REQUEST_BYTES_LIMIT + extra - len(body))
    response = client.post(
        "/query/evidence/read",
        headers=AUTH | {"Content-Type": "application/json"},
        content=iter([body[:32768], body[32768:]]) if streamed else body,
    )
    assert response.status_code == status


@pytest.mark.parametrize("body", [b"{bad-json", b"[1]", b"\xff", b"[" * 2000])
def test_evidence_invalid_json_is_content_free_client_error(client, body):
    response = client.post("/query/evidence/read", headers=AUTH, content=body)
    assert response.status_code in (400, 422)
    assert b"bad-json" not in response.content


def test_query_response_optional_bound_declines_before_publication():
    from fastapi import HTTPException
    from sediment_api.routers.query import _query_response

    payload = {"value": "\x00\ud800", "count": 2**100}
    expected = _query_response(payload, dict).body
    assert _query_response(payload, dict, max_bytes=len(expected)).body == expected
    with pytest.raises(HTTPException) as error:
        _query_response(payload, dict, max_bytes=len(expected) - 1)
    assert error.value.status_code == 409
    assert error.value.detail == {
        "reason": "evidence_response_limit",
        "limit": len(expected) - 1,
    }


def test_evidence_encoded_response_limit_refuses_escaping_expansion(client):
    client.app.state.fact_store.store_inference_call(
        _call(
            output_messages=[
                InferenceMessage(
                    role="assistant", parts=[TextPart(content="\x00" * 180_000)]
                )
            ]
        )
    )
    result = _fetch(client)
    assert result.status_code == 409
    assert result.json() == {
        "detail": {"reason": "evidence_response_limit", "limit": 1024 * 1024}
    }
    assert result.headers["cache-control"] == "no-store"
    assert not client.app.state.workers._query_tasks
    assert _manifest(client).status_code == 200


def test_evidence_fetch_body_bound_survives_mounted_prefix():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sediment_api.main import app

    mounted = FastAPI()
    mounted.mount("/sediment", app)
    with TestClient(mounted) as client:
        response = client.post(
            "/sediment/query/evidence/read",
            headers=AUTH,
            content=b" " * (EVIDENCE_REQUEST_BYTES_LIMIT + 1),
        )
    assert response.status_code == 413


def test_evidence_fetch_retains_smaller_global_pre_auth_body_bound(client, monkeypatch):
    from sediment_api import deps

    monkeypatch.setattr(deps, "MAX_BODY_BYTES", 32)
    response = client.post("/query/evidence/read", content=b" " * 33)
    assert response.status_code == 413


def test_evidence_response_exact_byte_limit_and_one_byte_over(client):
    store = client.app.state.fact_store

    def text_call(identifier, size):
        return _call(
            identifier,
            output_messages=[
                InferenceMessage(role="assistant", parts=[TextPart(content="x" * size)])
            ],
        )

    store.store_inference_call(text_call("small", 0))
    overhead = len(_fetch(client, [_ref("small")]).content)
    size = 1024 * 1024 - overhead
    store.store_inference_call(text_call("exact", size))
    store.store_inference_call(text_call("above", size + 1))
    exact = _fetch(client, [_ref("exact")])
    assert exact.status_code == 200
    assert len(exact.content) == 1024 * 1024
    above = _fetch(client, [_ref("above")])
    assert above.status_code == 409
    assert above.json() == {
        "detail": {"reason": "evidence_response_limit", "limit": 1024 * 1024}
    }


@pytest.mark.parametrize("column", ["metadata", "messages"])
def test_evidence_source_refusal_reaches_http_without_content(client, column):
    overrides = (
        {"model": "s" * (8 * 1024 * 1024)}
        if column == "metadata"
        else {
            "output_messages": [
                InferenceMessage(
                    role="assistant", parts=[TextPart(content="s" * (8 * 1024 * 1024))]
                )
            ]
        }
    )
    client.app.state.fact_store.store_inference_call(_call(**overrides))
    operations = (
        [_inventory, _manifest] if column == "metadata" else [_manifest, _fetch]
    )
    for operation in operations:
        result = operation(client)
        assert result.status_code == 409
        detail = result.json()["detail"]
        assert detail["reason"] == "evidence_source_limit"
        assert detail["bytes"] > detail["limit"] == 8 * 1024 * 1024
        assert len(result.content) < 200
        assert result.headers["cache-control"] == "no-store"
    assert not client.app.state.workers._query_tasks


def test_evidence_fetch_exact_reference_count_and_ordered_failures(client):
    store = client.app.state.fact_store
    store.store_inference_call(
        _call(
            output_messages=[
                InferenceMessage(
                    role="assistant",
                    parts=[TextPart(content=str(i)) for i in range(32)],
                )
            ]
        )
    )
    refs = [_ref(part=i) for i in reversed(range(32))]
    response = _fetch(client, refs)
    assert response.status_code == 200
    assert [item["part"]["content"] for item in response.json()["items"]] == [
        str(i) for i in reversed(range(32))
    ]
    response = _fetch(client, [_ref(), _ref(part=32), _ref("absent")])
    assert response.json() == {
        "detail": {"reason": "evidence_part_absent", "reference_index": 1}
    }
    assert response.status_code == 409
    response = _fetch(client, [_ref(), _ref("absent"), _ref(part=32)])
    assert response.json() == {
        "detail": {"reason": "evidence_unavailable", "reference_index": 1}
    }
    assert response.status_code == 409


def test_evidence_corrupt_source_returns_controlled_failure_and_releases_capacity(
    client, caplog
):
    from sediment_core.postgres_schema import inference_calls
    from sqlalchemy import update

    store = client.app.state.fact_store
    store.store_inference_call(_call())
    with client.app.state.database_engine.begin() as connection:
        connection.execute(
            update(inference_calls)
            .where(inference_calls.c.inference_call_id == CALL)
            .values(output_messages="private-corrupt-message-content")
        )
    response = _fetch(client)
    assert response.status_code == 500
    assert response.json() == {"detail": "work failed"}
    assert b"private-corrupt" not in response.content
    assert "private-corrupt" not in caplog.text
    assert not client.app.state.workers._query_tasks
    assert not client.app.state.workers._evidence_tasks
    # Inventory never decodes corrupt content and gets a fresh worker/connection.
    assert _inventory(client).status_code == 200


@pytest.mark.parametrize("unknown", ["\ud800", "\x00", "private-unknown-field"])
def test_evidence_invalid_field_names_do_not_break_validation_response(client, unknown):
    body = {
        "schema_version": 1,
        "session_id": SESSION,
        "references": [_ref() | {unknown: 1}],
    }
    response = client.post(
        "/query/evidence/read",
        headers=AUTH | {"Content-Type": "application/json"},
        content=json.dumps(body),
    )
    assert response.status_code == 422
    assert response.json()["detail"]


def test_evidence_inventory_exact_count_limit_and_no_partial_overflow(client):
    store = client.app.state.fact_store
    for i in range(1000):
        store.store_inference_call(
            _call(
                f"count-{i:04}",
                model=None,
                model_provider=None,
                input_messages=[],
                output_messages=[],
                raw={},
                user_id=None,
            )
        )
    response = _inventory(client)
    assert response.status_code == 200
    assert len(response.json()["calls"]) == 1000
    store.store_inference_call(_call("count-1000"))
    response = _inventory(client)
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "reason": "evidence_inventory_limit",
            "count": 1001,
            "limit": 1000,
        }
    }


def test_evidence_inventory_known_empty_and_quarantined_only(client):
    from sediment_core.postgres_schema import sessions

    with client.app.state.database_engine.begin() as connection:
        connection.execute(
            sessions.insert().values(
                org_id="testorg",
                session_id=SESSION,
                first_observed_at=T0,
                last_observed_at=T0,
                user_id_conflict=False,
            )
        )
    result = _inventory(client).json()
    assert result["found"] is True
    assert result["calls"] == []
    store = client.app.state.fact_store
    store.store_inference_call(_call())
    store.quarantine_fact("testorg", FactTable.INFERENCE_CALLS, CALL, reason="test")
    result = _inventory(client).json()
    assert result["found"] is True
    assert result["visible_inference_calls"] == 0
    assert result["quarantined_inference_calls"] == 1
    assert result["calls"] == []

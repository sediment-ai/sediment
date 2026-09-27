# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixed-Session retrieval crosses real HTTP, worker, and PostgreSQL boundaries."""

import json
from datetime import timedelta

import pytest
from pydantic import SecretStr
from sediment_core import FactTable, InferenceMessage, TextPart, ToolCallResponsePart

from test_evidence_query import AUTH, CALL, SESSION, T0, _call

TOKEN = "retrieval-test-token-long-enough"
RETRIEVAL = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def retrieval(client, monkeypatch):
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "retrieval_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "retrieval_session_id", SESSION)
    return client


def post(client, **values):
    return client.post(
        "/query/context",
        headers=RETRIEVAL,
        json={"schema_version": 1, "query": "goal huge value", **values},
    )


def test_context_returns_exact_evidence_only_from_configured_session(retrieval):
    store = retrieval.app.state.fact_store
    call = _call()
    store.store_inference_call(call)
    store.store_inference_call(_call("other", session="other-session"))
    store.store_inference_call(_call("foreign", org="another-org"))
    result = post(retrieval)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.content.isascii()
    assert len(result.content) <= 16384
    value = result.json()
    assert value["source_session_id"] == SESSION
    assert value["capture_completeness"] == "unknown"
    assert value["status"] == "matched"
    assert value["coverage"] == dict(
        visible_inference_calls=1,
        quarantined_inference_calls=0,
        scanned_parts=5,
        complete_visible_scan=True,
    )
    items = value["items"]
    assert all(
        item["evidence"]["reference"]["inference_call_id"] == CALL for item in items
    )
    assert any(
        item["evidence"]["part"].get("arguments", {}).get("huge") == 2**100
        for item in items
    )
    assert any(
        item["evidence"]["part"].get("content") == "goal\x00\ud800" for item in items
    )
    assert sum(value["skipped"].values()) + len(items) == 5
    assert b"private-user" not in result.content
    assert b"private-raw" not in result.content
    assert TOKEN.encode() not in result.content
    operator = retrieval.post(
        "/query/context",
        headers=AUTH,
        json={"schema_version": 1, "query": "goal huge value"},
    )
    assert operator.content == result.content


def test_context_observes_backdated_insert_and_quarantine(retrieval):
    store = retrieval.app.state.fact_store
    store.store_inference_call(_call())
    assert post(retrieval).json()["coverage"]["visible_inference_calls"] == 1
    store.store_inference_call(_call("late", observed_at=T0 - timedelta(days=1)))
    assert post(retrieval).json()["coverage"]["visible_inference_calls"] == 2
    store.quarantine_fact("testorg", FactTable.INFERENCE_CALLS, CALL, reason="test")
    value = post(retrieval).json()
    assert value["quarantine_revision"] == 1
    assert value["coverage"]["quarantined_inference_calls"] == 1
    assert {
        item["evidence"]["reference"]["inference_call_id"] for item in value["items"]
    } == {"late"}


def test_context_missing_and_known_empty_are_distinct(retrieval):
    assert post(retrieval).json() == {"detail": {"reason": "evidence_unavailable"}}
    retrieval.app.state.fact_store.store_inference_call(
        _call(input_messages=[], output_messages=[])
    )
    value = post(retrieval).json()
    assert value["status"] == "no_match"
    assert value["items"] == []
    assert value["coverage"]["scanned_parts"] == 0


@pytest.mark.parametrize(
    "token,status",
    [(None, 401), ("unknown", 401), ("test-ingest-token-3a7e-9d21", 403)],
)
def test_context_checks_authority_before_envelope_errors(retrieval, token, status):
    result = retrieval.post(
        "/query/context",
        json={},
        headers={} if token is None else {"Authorization": f"Bearer {token}"},
    )
    assert result.status_code == status


def test_context_disabled_for_operator_and_unknown_for_retrieval(client):
    body = {"schema_version": 1, "query": "goal"}
    assert client.post("/query/context", headers=AUTH, json=body).status_code == 404
    assert (
        client.post("/query/context", headers=RETRIEVAL, json=body).status_code == 401
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"schema_version": True},
        {"schema_version": 1.0},
        {"schema_version": 2},
        {"query": ""},
        {"query": "  "},
        {"query": "the and what"},
        {"query": "x" * 2049},
        {"query": "é" * 1025},
        {"query": "\ud800"},
        {"query": ["sentinel-private-query"]},
        {"max_bytes": True},
        {"max_bytes": 4095},
        {"max_bytes": 65537},
        {"max_bytes": 4096.0},
        {"max_bytes": "4096"},
        {"session_id": "sentinel-private-query"},
        {"org_id": "another-org"},
        {"endpoint": "https://example.com"},
        {"sentinel-private-query": "x"},
    ],
)
def test_context_rejects_strict_envelope_without_echo(retrieval, bad):
    result = retrieval.post(
        "/query/context",
        headers=RETRIEVAL | {"Content-Type": "application/json"},
        content=json.dumps({"schema_version": 1, "query": "goal", **bad}),
    )
    assert result.status_code == 422
    assert b"sentinel-private-query" not in result.content


@pytest.mark.parametrize("extra,status", [(0, 200), (1, 413)])
def test_context_streaming_body_limit_before_decode(retrieval, extra, status):
    retrieval.app.state.fact_store.store_inference_call(_call())
    body = json.dumps({"schema_version": 1, "query": "goal"}).encode()
    body += b" " * (16384 + extra - len(body))
    result = retrieval.post(
        "/query/context",
        headers=RETRIEVAL | {"Content-Type": "application/json"},
        content=iter([body[:8192], body[8192:]]),
    )
    assert result.status_code == status


def test_context_counts_nonfinite_without_refusing_finite_evidence(retrieval):
    retrieval.app.state.fact_store.store_inference_call(
        _call(
            output_messages=[
                InferenceMessage(
                    role="tool",
                    parts=[
                        ToolCallResponsePart(id="t", result={"goal": float("nan")}),
                        TextPart(content="goal finite"),
                    ],
                )
            ]
        )
    )
    value = post(retrieval).json()
    assert value["skipped"]["non_finite_number"] == 1
    assert value["status"] == "matched"


def test_context_part_overflow_is_complete_refusal(retrieval):
    retrieval.app.state.fact_store.store_inference_call(
        _call(
            input_messages=[],
            output_messages=[
                InferenceMessage(
                    role="assistant", parts=[TextPart(content="goal")] * 16_385
                )
            ],
        )
    )
    result = post(retrieval)
    assert result.status_code == 409
    assert result.json() == {
        "detail": {"reason": "retrieval_part_limit", "count": 16_385, "limit": 16_384}
    }
    assert result.headers["cache-control"] == "no-store"


def test_context_stream_accepts_more_than_materialized_part_limit(retrieval):
    retrieval.app.state.fact_store.store_inference_call(
        _call(
            input_messages=[],
            output_messages=[
                InferenceMessage(
                    role="assistant", parts=[TextPart(content="goal")] * 2049
                )
            ],
        )
    )
    result = post(retrieval)
    assert result.status_code == 200
    value = result.json()
    assert value["status"] == "matched"
    assert len(value["items"]) == 1
    assert value["skipped"]["repeated_content"] == 2048
    assert result.headers["cache-control"] == "no-store"


def test_context_deadline_releases_worker_slots(retrieval, monkeypatch):
    import sys
    from sediment_api import workers

    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (sys.executable, "-c", "import time; time.sleep(60)"),
    )
    monkeypatch.setattr(workers, "QUERY_BUDGET_SECONDS", 0.05)
    result = post(retrieval)
    assert result.status_code == 503
    assert result.headers["cache-control"] == "no-store"
    assert not retrieval.app.state.workers._query_tasks
    assert retrieval.get("/health").status_code == 200


def test_context_state_overflow_returns_no_partial_counts(retrieval):
    retrieval.app.state.fact_store.store_inference_call(
        _call(
            input_messages=[],
            output_messages=[
                InferenceMessage(
                    role="x" * (7 * 1024 * 1024),
                    parts=[TextPart(content=f"goal {number}") for number in range(3)],
                )
            ],
        )
    )
    result = post(retrieval)
    assert result.status_code == 409
    assert result.json() == {
        "detail": {"reason": "retrieval_state_limit", "limit_bytes": 32 * 1024 * 1024}
    }
    assert result.headers["cache-control"] == "no-store"
    assert not retrieval.app.state.workers._query_tasks


def test_context_worker_failures_do_not_echo_diagnostics(
    retrieval, monkeypatch, caplog
):
    import sys
    from sediment_api import workers

    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('sentinel-evidence-secret'); sys.exit(1)",
        ),
    )
    result = post(retrieval)
    assert result.status_code == 503
    assert "sentinel-evidence-secret" not in result.text
    assert "sentinel-evidence-secret" not in caplog.text


@pytest.mark.parametrize("body", [b"{bad-json", b"[1]", b"\xff", b"[" * 2000])
def test_context_invalid_json_is_content_free(retrieval, body):
    result = retrieval.post("/query/context", headers=RETRIEVAL, content=body)
    assert result.status_code in (400, 422)
    assert b"bad-json" not in result.content


def test_context_global_and_mounted_body_limits(client, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sediment_api import deps
    from sediment_api.main import app

    mounted = FastAPI()
    mounted.mount("/sediment", app)
    with TestClient(mounted) as transport:
        result = transport.post(
            "/sediment/query/context", headers=AUTH, content=b" " * 16385
        )
    assert result.status_code == 413
    monkeypatch.setattr(deps, "MAX_BODY_BYTES", 32)
    assert client.post("/query/context", content=b" " * 33).status_code == 413

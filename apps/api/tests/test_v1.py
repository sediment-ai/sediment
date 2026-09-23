# SPDX-License-Identifier: AGPL-3.0-or-later
"""v1 endpoint tests — auth probe and fact counts against the real
FactStore (never mocked, per AGENTS.md)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sediment_core import (
    AgentHarness,
    DeveloperDecision,
    EditObservation,
    FactTable,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    InteractionMode,
    TextPart,
    ToolCallPart,
)

from sediment_api.main import app

ORG = "testorg"
AUTH = {"Authorization": "Bearer test-operator-token-3a7e-2f6c"}


def _seed_inference_call(
    inference_call_id: str = "inference-v1-1", *, session_id: str = "sess-v1-1"
) -> None:
    store = app.state.fact_store
    store.store_inference_call(
        InferenceCall(
            inference_call_id=inference_call_id,
            org_id=ORG,
            session_id=session_id,
            user_id="agent:api",
            gateway_provider=GatewayProvider.LITELLM,
            model_provider="anthropic",
            model="claude-sonnet-5",
            input_messages=[
                InferenceMessage(role="user", parts=[TextPart(content="hi")])
            ],
            output_messages=[
                InferenceMessage(
                    role="assistant",
                    parts=[
                        TextPart(content="hello"),
                        ToolCallPart(
                            id=f"tool-{inference_call_id}",
                            name="Edit",
                            arguments={},
                        ),
                    ],
                )
            ],
            input_tokens=1,
            output_tokens=1,
            duration_ms=1,
            model_call_id=f"model-{inference_call_id}",
            observed_at=datetime.now(UTC),
        )
    )


def _seed_compatibility_join() -> None:
    store = app.state.fact_store
    observed_at = datetime.now(UTC)
    store.store_decision(
        DeveloperDecision(
            decision_id="decision-v1-1",
            org_id=ORG,
            session_id="sess-v1-1",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="module.py",
            accepted=True,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            call_id="tool-inference-v1-1",
            occurred_at=observed_at,
        )
    )
    store.store_edit_observation(
        EditObservation(
            observation_id="observation-v1-1",
            org_id=ORG,
            session_id="sess-v1-1",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="module.py",
            call_id="tool-inference-v1-1",
            applied_text="after",
            observed_file_text="after",
            occurred_at=observed_at,
        )
    )


def test_me_happy_path(client: TestClient) -> None:
    resp = client.get("/v1/me", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {
        "org_id": ORG,
        "version": "0.2.0",
        "authority": "operator",
        "client_id": "operator",
    }


def test_me_matches_health_version(client: TestClient) -> None:
    """The version /v1/me reports must equal the one /health reports — the
    CLI compares the two to warn on version skew."""
    me = client.get("/v1/me", headers=AUTH).json()
    health = client.get("/health").json()
    assert me["version"] == health["version"]


def test_me_requires_auth(client: TestClient) -> None:
    """Missing or wrong bearer → 401, never 422 or 500 (API conventions)."""
    assert client.get("/v1/me").status_code == 401
    assert (
        client.get("/v1/me", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )


def test_facts_happy_path(client: TestClient) -> None:
    _seed_inference_call()
    resp = client.get("/v1/facts", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sessions"] == 1
    tables = body["tables"]
    assert set(tables) == {t.value for t in FactTable}
    assert tables["inference_calls"] == {"total": 1, "visible": 1}
    for counts in tables.values():
        assert counts["total"] >= counts["visible"]
    assert body["quarantine_revision"] == 0


def test_facts_empty_store(client: TestClient) -> None:
    resp = client.get("/v1/facts", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sessions"] == 0
    assert all(c == {"total": 0, "visible": 0} for c in body["tables"].values())
    assert body["quarantine_revision"] == 0


def test_facts_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/facts").status_code == 401


def test_facts_quarantine_visibility(client: TestClient) -> None:
    """Quarantine one fact and the visible count shifts while total holds —
    the endpoint reads FactStore's quarantine-excluding defaults."""
    _seed_inference_call()
    before = client.get("/v1/facts", headers=AUTH).json()
    assert before["tables"]["inference_calls"] == {"total": 1, "visible": 1}
    assert before["quarantine_revision"] == 0

    store = app.state.fact_store
    store.quarantine_fact(
        ORG, FactTable.INFERENCE_CALLS, "inference-v1-1", reason="test quarantine"
    )

    after = client.get("/v1/facts", headers=AUTH).json()
    assert after["tables"]["inference_calls"] == {"total": 1, "visible": 0}
    assert after["quarantine_revision"] == 1


def test_session_facts_counts_only_the_named_session(client: TestClient) -> None:
    _seed_inference_call("inference-target")
    _seed_inference_call("inference-other", session_id="sess-other")

    resp = client.get("/v1/facts/session/sess-v1-1", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json() == {
        "session_id": "sess-v1-1",
        "tables": {
            "inference_calls": {"total": 1, "visible": 1},
            "developer_decisions": {"total": 0, "visible": 0},
            "edit_observations": {"total": 0, "visible": 0},
            "rejected_edits": {"total": 0, "visible": 0},
            "retry_linkages": {"total": 0, "visible": 0},
        },
    }


def test_session_facts_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/facts/session/sess-v1-1").status_code == 401


def test_session_inference_calls_expose_reconciliation_fields(
    client: TestClient,
) -> None:
    _seed_inference_call()
    _seed_inference_call("inference-other", session_id="sess-other")

    resp = client.get("/v1/facts/session/sess-v1-1/inference-calls", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "sess-v1-1"
    assert len(body["inference_calls"]) == 1
    assert body["inference_calls"][0] == {
        "inference_call_id": "inference-v1-1",
        "model_call_id": "model-inference-v1-1",
        "gateway_provider": "litellm",
        "model_provider": "anthropic",
        "model": "claude-sonnet-5",
        "input_tokens": 1,
        "output_tokens": 1,
        "duration_ms": 1,
    }


def test_session_inference_calls_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/facts/session/sess-v1-1/inference-calls").status_code == 401


def test_session_compatibility_evidence_exposes_only_join_fields(
    client: TestClient,
) -> None:
    _seed_inference_call()
    _seed_compatibility_join()

    resp = client.get(
        "/v1/facts/session/sess-v1-1/compatibility-evidence", headers=AUTH
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "session_id": "sess-v1-1",
        "inference_calls": [
            {
                "tool_call_ids": ["tool-inference-v1-1"],
            }
        ],
        "developer_decisions": [
            {
                "agent_harness": "claude-code",
                "accepted": True,
                "explicit": True,
                "interaction_mode": "agent",
                "call_id": "tool-inference-v1-1",
            }
        ],
        "edit_observations": [
            {
                "agent_harness": "claude-code",
                "call_id": "tool-inference-v1-1",
            }
        ],
    }


def test_session_compatibility_evidence_requires_auth(client: TestClient) -> None:
    assert (
        client.get("/v1/facts/session/sess-v1-1/compatibility-evidence").status_code
        == 401
    )


def test_compatibility_evidence_accepts_lossless_canonical_content(
    client: TestClient,
) -> None:
    call = InferenceCall(
        org_id=ORG,
        session_id="opaque-session",
        gateway_provider=GatewayProvider.LITELLM,
        input_messages=[],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[
                    TextPart(content="nul\x00surrogate\ud800"),
                    ToolCallPart(
                        id="tool-opaque", name="Edit", arguments={"value": float("inf")}
                    ),
                ],
            )
        ],
        raw={"private": "omitted"},
    )
    assert app.state.fact_store.store_inference_call(call)
    response = client.get(
        "/v1/facts/session/opaque-session/compatibility-evidence", headers=AUTH
    )
    assert response.status_code == 200
    assert response.json()["inference_calls"] == [{"tool_call_ids": ["tool-opaque"]}]
    assert "private" not in response.text

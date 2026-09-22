# SPDX-License-Identifier: AGPL-3.0-or-later
"""Candidate discovery and selection cross the real HTTP worker boundary."""

import json
import sys

import pytest
from pydantic import SecretStr
from sediment_core import (
    FactTable,
    InferenceMessage,
    Push,
    SessionCommitObservation,
    TextPart,
)

from sediment_api.config import settings
from test_context_query import RETRIEVAL, TOKEN
from test_evidence_query import AUTH, SESSION, T0, _call

ROUTES = ("/query/context/discover", "/query/context/selected")
COMMIT = {
    "repository_provider": "github",
    "repository_host": "github.com",
    "repository_id": "123",
    "commit_sha": "a" * 40,
}


@pytest.fixture
def discovery(client, monkeypatch):
    monkeypatch.setattr(settings, "retrieval_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "retrieval_session_id", None)
    monkeypatch.setattr(settings, "retrieval_session_ids", [SESSION, "uncommitted"])
    return client


def request_body(route, **values):
    return {
        "schema_version": 1,
        "query": "goal",
        **({"session_id": SESSION} if route.endswith("selected") else {}),
        **values,
    }


def post(client, route=ROUTES[0], **values):
    return client.post(route, headers=RETRIEVAL, json=request_body(route, **values))


def test_discover_then_select_exact_authorized_evidence(discovery):
    store = discovery.app.state.fact_store
    store.store_inference_call(_call())
    store.store_inference_call(_call("uncommitted-call", session="uncommitted"))
    store.store_inference_call(_call("outside-call", session="outside"))
    store.store_inference_call(_call("foreign-call", org="foreign"))
    response = post(discovery)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    value = response.json()
    assert value["status"] == "matched"
    assert value["coverage"]["authorized_sessions"] == 2
    assert value["coverage"]["found_sessions"] == 2
    assert value["coverage"]["visible_inference_calls"] == 2
    assert {item["session_id"] for item in value["items"]} == {SESSION, "uncommitted"}
    for candidate in value["items"]:
        exact = discovery.post(
            "/query/evidence/read",
            headers=AUTH,
            json={
                "schema_version": 1,
                "session_id": candidate["session_id"],
                "references": [candidate["preview"]["reference"]],
            },
        )
        assert exact.status_code == 200
        assert exact.json()["items"] == [candidate["preview"]]
        selected = post(discovery, ROUTES[1], session_id=candidate["session_id"])
        assert selected.status_code == 200
        assert selected.json()["source_session_id"] == candidate["session_id"]
        assert candidate["preview"] in [
            item["evidence"] for item in selected.json()["items"]
        ]
    assert b"private-user" not in response.content
    assert b"private-raw" not in response.content
    assert b"outside-call" not in response.content
    assert b"foreign-call" not in response.content
    assert TOKEN.encode() not in response.content
    assert (
        discovery.post(ROUTES[0], headers=AUTH, json=request_body(ROUTES[0])).content
        == response.content
    )


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "token,status",
    [(None, 401), ("unknown", 401), ("test-ingest-token-3a7e-9d21", 403)],
)
def test_context_set_authority_precedes_semantic_validation(
    discovery, route, token, status
):
    response = discovery.post(
        route,
        headers={} if token is None else {"Authorization": f"Bearer {token}"},
        json={},
    )
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("route", ROUTES)
def test_context_set_disabled_for_operator_unknown_for_retrieval(client, route):
    assert client.post(route, headers=AUTH, json=request_body(route)).status_code == 404
    assert (
        client.post(route, headers=RETRIEVAL, json=request_body(route)).status_code
        == 401
    )


@pytest.mark.parametrize("route", ROUTES)
def test_singleton_also_supports_explicit_discovery_and_selection(
    discovery, monkeypatch, route
):
    monkeypatch.setattr(settings, "retrieval_session_ids", None)
    monkeypatch.setattr(settings, "retrieval_session_id", SESSION)
    discovery.app.state.fact_store.store_inference_call(_call())
    assert post(discovery, route).status_code == 200
    legacy = discovery.post(
        "/query/context", headers=RETRIEVAL, json={"schema_version": 1, "query": "goal"}
    )
    selected = post(discovery, ROUTES[1])
    assert selected.content == legacy.content


@pytest.mark.parametrize("token", [TOKEN, "test-operator-token-3a7e-2f6c"])
def test_out_of_grant_refusal_is_identical_before_worker(discovery, monkeypatch, token):
    store = discovery.app.state.fact_store
    store.store_inference_call(_call("outside", session="outside-sentinel"))
    store.store_inference_call(
        _call("foreign", session="foreign-sentinel", org="foreign")
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("out-of-grant selection reached the worker")

    monkeypatch.setattr(discovery.app.state.workers, "run", forbidden)
    responses = [
        discovery.post(
            ROUTES[1],
            headers={"Authorization": f"Bearer {token}"},
            json=request_body(ROUTES[1], session_id=session),
        )
        for session in ("outside-sentinel", "foreign-sentinel", "missing-sentinel")
    ]
    assert {response.status_code for response in responses} == {403}
    assert len({response.content for response in responses}) == 1
    assert all(
        response.headers["cache-control"] == "no-store" for response in responses
    )
    assert all(
        session not in responses[0].text
        for session in ("outside-sentinel", "foreign-sentinel", "missing-sentinel")
    )


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "bad",
    [
        {"schema_version": True},
        {"schema_version": 1.0},
        {"schema_version": 2},
        {"query": ""},
        {"query": "the and what"},
        {"query": "x" * 2049},
        {"query": "é" * 1025},
        {"query": "\ud800"},
        {"query": ["sentinel-private-query"]},
        {"max_bytes": True},
        {"max_bytes": 4095},
        {"max_bytes": 65537},
        {"max_bytes": "4096"},
        {"org_id": "private"},
        {"source_session_ids": ["private"]},
        {"endpoint": "private"},
        {"sentinel-private-query": "value"},
    ],
)
def test_context_set_strict_envelope_never_echoes_input(discovery, route, bad):
    response = discovery.post(
        route,
        headers=RETRIEVAL | {"Content-Type": "application/json"},
        content=json.dumps(request_body(route, **bad)),
    )
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert b"sentinel-private-query" not in response.content


@pytest.mark.parametrize(
    "commit",
    [
        "a" * 40,
        {},
        {"commit_sha": "a" * 40},
        COMMIT | {"repository_id": ""},
        COMMIT | {"repository_host": "https://github.com"},
        COMMIT | {"commit_sha": "a" * 39},
        COMMIT | {"repository_provider": "sentinel-private-anchor"},
        COMMIT | {"sentinel-private-anchor": "value"},
    ],
)
def test_commit_anchor_requires_complete_canonical_identity(discovery, commit):
    response = post(discovery, commit=commit)
    assert response.status_code == 422
    assert b"sentinel-private-anchor" not in response.content


@pytest.mark.parametrize("commit", [None, COMMIT])
def test_optional_commit_anchor_is_accepted_without_invented_matches(discovery, commit):
    discovery.app.state.fact_store.store_inference_call(_call())
    response = post(discovery, commit=commit)
    assert response.status_code == 200
    assert response.json()["commit"] == commit
    assert all(item["commit_match"] is None for item in response.json()["items"])


@pytest.mark.parametrize(
    "session", [None, "", "\x00", "\ud800", 1, True, ["sentinel-private-id"]]
)
def test_selected_requires_valid_session_id(discovery, session):
    response = discovery.post(
        ROUTES[1],
        headers=RETRIEVAL | {"Content-Type": "application/json"},
        content=json.dumps(request_body(ROUTES[1], session_id=session)),
    )
    assert response.status_code == 422
    assert b"sentinel-private-id" not in response.content


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("extra,status", [(0, 200), (1, 413)])
def test_context_set_streaming_body_limit(discovery, route, extra, status):
    discovery.app.state.fact_store.store_inference_call(_call())
    body = json.dumps(request_body(route)).encode()
    body += b" " * (16384 + extra - len(body))
    response = discovery.post(
        route,
        headers=RETRIEVAL | {"Content-Type": "application/json"},
        content=iter([body[:8192], body[8192:]]),
    )
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "body",
    [b"{bad-json", b"\xff", b"[" * 2000],
    ids=["malformed", "invalid-utf8", "nested"],
)
def test_context_set_malformed_json_is_content_free(discovery, route, body):
    response = discovery.post(
        route, headers=RETRIEVAL | {"Content-Type": "application/json"}, content=body
    )
    assert response.status_code == 400
    assert b"bad-json" not in response.content
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("route", ROUTES)
def test_context_set_deadline_releases_worker_slots(discovery, monkeypatch, route):
    from sediment_api import workers

    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (sys.executable, "-c", "import time; time.sleep(60)"),
    )
    monkeypatch.setattr(workers, "QUERY_BUDGET_SECONDS", 0.05)
    response = post(discovery, route)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert not discovery.app.state.workers._query_tasks
    assert discovery.get("/health").status_code == 200


def test_selected_rechecks_quarantine_after_candidate_discovery(discovery):
    store = discovery.app.state.fact_store
    call = _call()
    store.store_inference_call(call)
    assert post(discovery).json()["items"]
    store.quarantine_fact(
        "testorg", FactTable.INFERENCE_CALLS, call.inference_call_id, reason="test"
    )
    selected = post(discovery, ROUTES[1]).json()
    assert selected["items"] == []
    assert selected["quarantine_revision"] == 1
    assert selected["coverage"]["quarantined_inference_calls"] == 1


def test_discovery_aggregate_part_overflow_refuses_complete_grant(discovery):
    store = discovery.app.state.fact_store
    for session in (SESSION, "uncommitted"):
        store.store_inference_call(
            _call(
                session,
                session=session,
                input_messages=[],
                output_messages=[
                    InferenceMessage(
                        role="user", parts=[TextPart(content="goal")] * 1025
                    )
                ],
            )
        )
    response = post(discovery)
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "retrieval_part_limit"
    assert post(discovery, ROUTES[1]).status_code == 200


def test_discovery_aggregate_call_overflow_refuses_complete_grant(discovery):
    store = discovery.app.state.fact_store
    for number in range(1001):
        store.store_inference_call(
            _call(
                str(number),
                session=SESSION if number < 501 else "uncommitted",
                input_messages=[],
                output_messages=[],
            )
        )
    response = post(discovery)
    assert response.status_code == 409
    assert response.json() == {
        "detail": {"reason": "evidence_inventory_limit", "count": 1001, "limit": 1000}
    }
    assert post(discovery, ROUTES[1]).status_code == 200


def test_discovery_aggregate_byte_overflow_refuses_complete_grant(discovery):
    store = discovery.app.state.fact_store
    for session in (SESSION, "uncommitted"):
        store.store_inference_call(
            _call(
                session,
                session=session,
                input_messages=[],
                output_messages=[
                    InferenceMessage(
                        role="user",
                        parts=[TextPart(content="goal " + "x" * (4 * 1024 * 1024))],
                    )
                ],
            )
        )
    response = post(discovery)
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "evidence_source_limit"
    assert post(discovery, ROUTES[1]).status_code == 200


def test_discovery_commit_witness_survives_http_and_rechecks_quarantine(discovery):
    store = discovery.app.state.fact_store
    store.store_inference_call(_call("text-match", session="uncommitted"))
    identity = {name: value for name, value in COMMIT.items() if name != "commit_sha"}
    store.store_push(
        Push(
            push_id="source-push",
            org_id="testorg",
            provider="github",
            repo="acme/old",
            clone_url="https://private.invalid/repo",
            ref="refs/heads/main",
            before_sha="0" * 40,
            after_sha="b" * 40,
            captured_at=T0,
            **identity,
        )
    )
    store.store_session_commit_observation(
        SessionCommitObservation(
            observation_id="commit-observation",
            org_id="testorg",
            repo="acme/renamed",
            session_id=SESSION,
            source_push_id="source-push",
            captured_at=T0,
            **COMMIT,
        )
    )
    response = post(discovery, commit=COMMIT)
    assert response.status_code == 200
    candidates = response.json()["items"]
    assert [item["session_id"] for item in candidates] == [SESSION, "uncommitted"]
    assert candidates[0]["score"] == 0
    assert candidates[0]["preview"] is None
    assert candidates[0]["commit_match"] == {
        "observation_id": "commit-observation",
        "source_push_id": "source-push",
        "captured_at": T0.isoformat().replace("+00:00", "Z"),
    }
    assert b"private.invalid" not in response.content
    assert b"acme/renamed" not in response.content
    other_repo = post(discovery, commit=COMMIT | {"repository_id": "456"})
    assert [item["session_id"] for item in other_repo.json()["items"]] == [
        "uncommitted"
    ]
    store.quarantine_fact("testorg", FactTable.PUSHES, "source-push", reason="test")
    changed = post(discovery, commit=COMMIT).json()
    assert changed["quarantine_revision"] == 1
    assert [item["session_id"] for item in changed["items"]] == ["uncommitted"]


def test_discovery_reports_missing_and_known_empty_scope(discovery):
    missing = post(discovery).json()
    assert missing["status"] == "no_match"
    assert missing["items"] == []
    assert missing["coverage"]["authorized_sessions"] == 2
    assert missing["coverage"]["found_sessions"] == 0
    discovery.app.state.fact_store.store_inference_call(
        _call(input_messages=[], output_messages=[])
    )
    known = post(discovery).json()
    assert known["status"] == "no_match"
    assert known["items"] == []
    assert known["coverage"]["found_sessions"] == 1
    assert known["coverage"]["visible_inference_calls"] == 1
    assert known["coverage"]["scanned_parts"] == 0


@pytest.mark.parametrize("route", ROUTES)
def test_context_set_preserves_mounted_predecode_body_bound(client, monkeypatch, route):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sediment_api import deps
    from sediment_api.main import app

    mounted = FastAPI()
    mounted.mount("/sediment", app)
    with TestClient(mounted) as transport:
        response = transport.post(
            "/sediment" + route, headers=AUTH, content=b" " * 16385
        )
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    monkeypatch.setattr(deps, "MAX_BODY_BYTES", 32)
    assert client.post(route, content=b" " * 33).status_code == 413

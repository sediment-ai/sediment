# SPDX-License-Identifier: AGPL-3.0-or-later
"""Granted factual reads let a consumer select evidence without keyword ranking."""

import json
from datetime import timedelta

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sediment_core import (
    EVIDENCE_REQUEST_BYTES_LIMIT,
    FactTable,
    InferenceMessage,
    TextPart,
    ToolCallResponsePart,
)

from sediment_api.config import settings
from sediment_api.worker import WorkerRequest, _dispatch

from test_context_query import RETRIEVAL, TOKEN
from test_evidence_query import AUTH, CALL, SESSION, T0, _call, _ref

ROOT = "/query/context/evidence"
KINDS = ("inventory", "manifest", "read")


@pytest.fixture
def granted(client, monkeypatch):
    monkeypatch.setattr(settings, "retrieval_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "retrieval_session_id", None)
    monkeypatch.setattr(settings, "retrieval_session_ids", [SESSION, "uncommitted"])
    return client


def exact(
    client,
    kind="read",
    *,
    session=SESSION,
    identifier=CALL,
    refs=None,
    headers=None,
    root=ROOT,
):
    headers = RETRIEVAL if headers is None else headers
    if kind == "read":
        return client.post(
            root + "/read",
            headers=headers,
            json={
                "schema_version": 1,
                "session_id": session,
                "references": refs if refs is not None else [_ref(identifier)],
            },
        )
    return client.get(
        root + ("/manifest" if kind == "manifest" else ""),
        headers=headers,
        params={"session_id": session, "inference_call_id": identifier},
    )


@pytest.mark.parametrize("singleton", [False, True])
def test_scoped_exact_reads_preserve_operator_bytes_and_occurrences(
    granted, monkeypatch, singleton
):
    if singleton:
        monkeypatch.setattr(settings, "retrieval_session_ids", None)
        monkeypatch.setattr(settings, "retrieval_session_id", SESSION)
    granted.app.state.fact_store.store_inference_call(_call())
    refs = [
        _ref(part=2),
        _ref(side="input", message=1),
        _ref(side="input", message=1, part=1),
        _ref(),
    ]
    for kind in KINDS:
        response = exact(granted, kind, refs=refs)
        assert response.status_code == 200
        assert (
            response.content
            == exact(
                granted, kind, refs=refs, headers=AUTH, root="/query/evidence"
            ).content
        )
        assert response.content == exact(granted, kind, refs=refs, headers=AUTH).content
        assert response.headers["cache-control"] == "no-store"
        assert response.content.isascii()
        assert b"private-user" not in response.content
        assert b"private-raw" not in response.content
    items = exact(granted, refs=refs).json()["items"]
    assert [item["reference"] for item in items] == refs
    assert items[0]["part"] == items[1]["part"]  # Distinct repeated occurrences.
    assert items[2]["part"]["type"] == "reasoning"
    assert items[3]["part"]["arguments"]["huge"] == 2**100
    assert items[0]["part"]["content"] == "goal\x00\ud800"


def test_uncommitted_evidence_outside_keyword_candidates_remains_readable(granted):
    store = granted.app.state.fact_store
    store.store_inference_call(_call())
    store.store_inference_call(
        _call(
            "uncommitted-call",
            session="uncommitted",
            output_messages=[],
            input_messages=[
                InferenceMessage(
                    role="user",
                    parts=[
                        TextPart(
                            content="Keep the original requirements; the failed attempt lost state."
                        )
                    ],
                )
            ],
        )
    )
    candidates = granted.post(
        "/query/context/discover",
        headers=RETRIEVAL,
        json={"schema_version": 1, "query": "goal"},
    )
    assert candidates.status_code == 200
    assert [item["session_id"] for item in candidates.json()["items"]] == [SESSION]
    inventory = exact(granted, "inventory", session="uncommitted")
    assert inventory.status_code == 200
    identifier = inventory.json()["calls"][0]["inference_call_id"]
    manifest = exact(granted, "manifest", session="uncommitted", identifier=identifier)
    reference = manifest.json()["messages"][0]["parts"][0]["reference"]
    response = exact(granted, session="uncommitted", refs=[reference])
    assert response.status_code == 200
    assert "failed attempt" in response.json()["items"][0]["part"]["content"]
    assert "score" not in response.json()["items"][0]
    assert "commit_match" not in response.json()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 401),
        ({"Authorization": "Bearer bad"}, 401),
        ({"Authorization": "Bearer test-ingest-token-3a7e-9d21"}, 403),
    ],
)
def test_scoped_exact_authority_precedes_envelope_validation(
    granted, kind, headers, status
):
    if kind == "read":
        response = granted.post(ROOT + "/read", headers=headers, json={})
    else:
        response = exact(granted, kind, headers=headers)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("kind", KINDS)
def test_scoped_disabled_and_legacy_operator_authority(
    client, granted, kind, monkeypatch
):
    assert exact(granted, kind, root="/query/evidence").status_code == 403
    monkeypatch.setattr(settings, "retrieval_token", None)
    monkeypatch.setattr(settings, "retrieval_session_ids", None)
    assert exact(client, kind, headers=AUTH).status_code == 404
    assert exact(client, kind).status_code == 401


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("headers", [RETRIEVAL, AUTH])
def test_outside_grant_refuses_before_worker_without_existence_leak(
    granted, monkeypatch, kind, headers
):
    granted.app.state.fact_store.store_inference_call(
        _call("outside", session="outside-sentinel")
    )
    granted.app.state.fact_store.store_inference_call(
        _call("foreign", session="foreign-sentinel", org="foreign")
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("out-of-grant request reached worker")

    monkeypatch.setattr(granted.app.state.workers, "run", forbidden)
    responses = [
        exact(granted, kind, session=session, headers=headers)
        for session in ("outside-sentinel", "foreign-sentinel", "missing-sentinel")
    ]
    assert {response.status_code for response in responses} == {403}
    assert len({response.content for response in responses}) == 1
    assert all(
        response.headers["cache-control"] == "no-store" for response in responses
    )
    assert not any(
        value in responses[0].text
        for value in ("outside-sentinel", "foreign-sentinel", "missing-sentinel")
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("disabled", [False, True])
def test_scoped_worker_independently_checks_grant_before_storage(
    granted, monkeypatch, kind, disabled
):
    class NoStorage:
        def __getattr__(self, name):
            pytest.fail("unauthorized worker accessed storage")

    if disabled:
        monkeypatch.setattr(settings, "retrieval_session_ids", None)
        monkeypatch.setattr(settings, "retrieval_token", None)
    with pytest.raises(HTTPException) as error:
        _dispatch(
            WorkerRequest(
                "context-evidence-" + kind,
                {
                    "schema_version": 1,
                    "session_id": "forbidden",
                    "inference_call_id": CALL,
                    "references": [_ref()],
                },
            ),
            NoStorage(),
        )
    assert error.value.status_code == (404 if disabled else 403)
    assert "forbidden" not in error.value.detail


def test_scoped_exact_rechecks_quarantine_release_and_backdated_facts(granted):
    store = granted.app.state.fact_store
    store.store_inference_call(_call())
    assert exact(granted, "inventory").json()["quarantine_revision"] == 0
    assert exact(granted, "manifest").status_code == 200
    store.quarantine_fact("testorg", FactTable.INFERENCE_CALLS, CALL, reason="test")
    assert exact(granted).json() == {
        "detail": {
            "reason": "evidence_unavailable",
            "reference_index": 0,
        }
    }
    assert exact(granted, "manifest").json() == {
        "detail": {"reason": "evidence_unavailable"}
    }
    store.release_fact("testorg", FactTable.INFERENCE_CALLS, CALL, reason="test")
    response = exact(granted)
    assert response.status_code == 200
    assert response.json()["quarantine_revision"] == 2
    store.store_inference_call(_call("backdated", observed_at=T0 - timedelta(days=1)))
    assert [
        call["inference_call_id"]
        for call in exact(granted, "inventory").json()["calls"]
    ] == ["backdated", CALL]


@pytest.mark.parametrize(
    "condition", ["missing", "foreign", "cross-session", "quarantined"]
)
def test_scoped_unavailable_references_have_identical_complete_refusal(
    granted, condition
):
    store = granted.app.state.fact_store
    store.store_inference_call(_call("available"))
    if condition != "missing":
        store.store_inference_call(
            _call(
                org="foreign" if condition == "foreign" else "testorg",
                session="uncommitted" if condition == "cross-session" else SESSION,
            )
        )
    if condition == "quarantined":
        store.quarantine_fact("testorg", FactTable.INFERENCE_CALLS, CALL, reason="test")
    response = exact(granted, refs=[_ref("available"), _ref()])
    assert response.status_code == 409
    assert response.json() == {
        "detail": {"reason": "evidence_unavailable", "reference_index": 1}
    }
    assert b"available" not in response.content.replace(b"unavailable", b"")
    manifest = exact(granted, "manifest")
    assert manifest.status_code == 409
    assert manifest.json() == {"detail": {"reason": "evidence_unavailable"}}


def test_scoped_fetch_nonfinite_sibling_and_source_side_limits(granted):
    store = granted.app.state.fact_store
    store.store_inference_call(
        _call(
            output_messages=[
                InferenceMessage(
                    role="assistant",
                    parts=[
                        TextPart(content="finite"),
                        ToolCallResponsePart(id="call", result=float("inf")),
                    ],
                )
            ]
        )
    )
    assert exact(granted, "manifest").status_code == 200
    assert exact(granted).status_code == 200
    response = exact(granted, refs=[_ref(), _ref(part=1)])
    assert response.status_code == 409
    assert response.json() == {"detail": {"reason": "non_finite_number"}}
    oversized = [
        InferenceMessage(role="user", parts=[TextPart(content="x" * (8 * 1024 * 1024))])
    ]
    store.store_inference_call(_call("huge-input", input_messages=oversized))
    store.store_inference_call(_call("huge-unselected", output_messages=oversized))
    assert exact(granted).status_code == 200
    assert exact(granted, identifier="huge-input").status_code == 200
    response = exact(granted, refs=[_ref("huge-input", side="input")])
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "evidence_source_limit"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("extra,status", [(0, 200), (1, 413)])
def test_scoped_fetch_streaming_body_limit_precedes_decoding(granted, extra, status):
    granted.app.state.fact_store.store_inference_call(_call())
    body = json.dumps(
        {"schema_version": 1, "session_id": SESSION, "references": [_ref()]}
    ).encode()
    body += b" " * (EVIDENCE_REQUEST_BYTES_LIMIT + extra - len(body))
    response = granted.post(
        ROOT + "/read",
        headers=RETRIEVAL | {"Content-Type": "application/json"},
        content=iter([body[:32768], body[32768:]]),
    )
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "bad",
    [
        {"references": []},
        {"references": [_ref(), _ref()]},
        {"references": [_ref(part=i) for i in range(33)]},
        {"schema_version": True},
        {"references": [_ref(part=-1)]},
        {"session_id": None},
        {"query": "private-query"},
    ],
)
def test_scoped_fetch_reuses_strict_exact_envelope(granted, bad):
    response = granted.post(
        ROOT + "/read",
        headers=RETRIEVAL,
        json={
            "schema_version": 1,
            "session_id": SESSION,
            "references": [_ref()],
            **bad,
        },
    )
    assert response.status_code == 422
    assert b"private-query" not in response.content
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("body", [b"{private-invalid-json", b"\xff", b"[" * 2000])
def test_scoped_fetch_malformed_json_is_content_free(granted, body):
    response = granted.post(
        ROOT + "/read",
        headers=RETRIEVAL | {"Content-Type": "application/json"},
        content=body,
    )
    assert response.status_code == 400
    assert b"private-invalid" not in response.content
    assert response.headers["cache-control"] == "no-store"


def test_scoped_fetch_body_bound_survives_mount_and_smaller_global_limit(
    granted, monkeypatch
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sediment_api import deps

    mounted = FastAPI()
    mounted.mount("/sediment", granted.app)
    with TestClient(mounted) as client:
        response = client.post(
            "/sediment" + ROOT + "/read",
            headers=RETRIEVAL,
            content=b" " * (EVIDENCE_REQUEST_BYTES_LIMIT + 1),
        )
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    monkeypatch.setattr(deps, "MAX_BODY_BYTES", 32)
    assert granted.post(ROOT + "/read", content=b" " * 33).status_code == 413


def test_scoped_exact_inventory_is_complete_or_refuses(granted):
    store = granted.app.state.fact_store
    for index in range(1000):
        store.store_inference_call(
            _call(
                f"count-{index:04}",
                input_messages=[],
                output_messages=[],
                model=None,
                model_provider=None,
                raw={},
                user_id=None,
            )
        )
    response = exact(granted, "inventory")
    assert response.status_code == 200
    assert len(response.json()["calls"]) == 1000
    store.store_inference_call(_call("count-1000"))
    response = exact(granted, "inventory")
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "reason": "evidence_inventory_limit",
            "count": 1001,
            "limit": 1000,
        }
    }
    assert response.headers["cache-control"] == "no-store"


def test_scoped_fetch_reference_limit_and_ordered_refusals(granted):
    granted.app.state.fact_store.store_inference_call(
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
    response = exact(granted, refs=refs)
    assert response.status_code == 200
    assert [item["reference"] for item in response.json()["items"]] == refs
    response = exact(granted, refs=[_ref(), _ref(part=32), _ref("missing")])
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "reason": "evidence_part_absent",
            "reference_index": 1,
        }
    }


def test_scoped_fetch_encoded_response_limit_refuses_complete_packet(granted):
    granted.app.state.fact_store.store_inference_call(
        _call(
            output_messages=[
                InferenceMessage(
                    role="assistant", parts=[TextPart(content="\x00" * 180_000)]
                )
            ]
        )
    )
    response = exact(granted)
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "reason": "evidence_response_limit",
            "limit": 1024 * 1024,
        }
    }
    assert exact(granted, "manifest").status_code == 200
    assert not granted.app.state.workers._query_tasks
    assert not granted.app.state.workers._evidence_tasks


@pytest.mark.parametrize("kind", KINDS)
def test_scoped_exact_public_worker_deadline_releases_capacity(
    granted, monkeypatch, kind
):
    import sys
    from sediment_api import workers

    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (sys.executable, "-c", "import time; time.sleep(60)"),
    )
    monkeypatch.setattr(workers, "QUERY_BUDGET_SECONDS", 0.05)
    response = exact(granted, kind)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert not granted.app.state.workers._query_tasks
    assert not granted.app.state.workers._evidence_tasks
    assert granted.get("/health").status_code == 200


@pytest.fixture
def validation_client(monkeypatch):
    """Invalid requests need no lifespan, database, or admitted worker."""
    from fastapi.testclient import TestClient
    from sediment_api.main import app

    monkeypatch.setattr(settings, "retrieval_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "retrieval_session_id", SESSION)
    monkeypatch.setattr(settings, "retrieval_session_ids", None)

    class NoWorkers:
        async def run(self, *args, **kwargs):
            pytest.fail("invalid request reached worker")

    monkeypatch.setattr(app.state, "workers", NoWorkers(), raising=False)
    client = TestClient(app)
    yield client
    client.close()


@pytest.mark.parametrize("location", ["envelope", "reference"])
@pytest.mark.parametrize("key", ["private-field-sentinel", "\x00", "\ud800"])
def test_scoped_exact_validation_never_echoes_extra_keys(
    validation_client, location, key
):
    body = {"schema_version": 1, "session_id": SESSION, "references": [_ref()]}
    target = body if location == "envelope" else body["references"][0]
    target[key] = "private-value-sentinel"
    response = validation_client.post(
        ROOT + "/read",
        headers=RETRIEVAL | {"Content-Type": "application/json"},
        content=json.dumps(body),
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {"type": "value_error", "loc": [], "msg": "Invalid evidence request"}
        ]
    }
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "bad",
    [
        {"session_id": float("nan")},
        {"schema_version": float("inf")},
        {"references": [_ref(part=float("nan"))]},
        {"references": [_ref() | {"side": "private-side-sentinel"}]},
        {"references": {"private-shape-sentinel": 1}},
        {"references": ["private-reference-sentinel"]},
    ],
)
def test_scoped_exact_validation_closes_nonfinite_and_wrong_shape_errors(
    validation_client, bad
):
    body = {"schema_version": 1, "session_id": SESSION, "references": [_ref()], **bad}
    response = validation_client.post(
        ROOT + "/read",
        headers=RETRIEVAL | {"Content-Type": "application/json"},
        content=json.dumps(body),
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {"type": "value_error", "loc": [], "msg": "Invalid evidence request"}
        ]
    }
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("kind", ["inventory", "manifest"])
def test_scoped_exact_invalid_query_is_content_free(validation_client, kind):
    response = exact(validation_client, kind, session="\x00")
    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {"type": "value_error", "loc": [], "msg": "Invalid evidence request"}
        ]
    }
    assert response.headers["cache-control"] == "no-store"


def test_operator_evidence_validation_keeps_existing_wire_shape(validation_client):
    response = validation_client.post("/query/evidence/read", headers=AUTH, json={})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert {tuple(error["loc"]) for error in response.json()["detail"]} == {
        ("body", "schema_version"),
        ("body", "session_id"),
        ("body", "references"),
    }

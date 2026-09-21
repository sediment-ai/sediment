# SPDX-License-Identifier: AGPL-3.0-or-later
"""All four fact types flow from the committed
capture fixtures through HTTP into PostgreSQL; auth failures are
rejected at the door; redeliveries collapse on UNIQUE indexes (ADR 0003).

Fixtures are the wire-verified ones in ``packages/capture/tests/fixtures``
— the point of this suite is the same bytes traversing the full HTTP path,
so they are referenced, not copied.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sediment_capture import parse_otlp_decisions, sign_payload
from sediment_core import FactStore

from sediment_api.config import settings
from sediment_api.main import app

FIXTURES = Path(__file__).resolve().parents[3] / "packages/capture/tests/fixtures"
OTLP_FIXTURES = sorted(FIXTURES.glob("otlp/*/*.json"))


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_bearer_token}"}


def _post_webhook(
    client: TestClient,
    path: str,
    event: str,
    payload: dict[str, Any],
    secret: str | None = None,
) -> httpx.Response:
    body = json.dumps(payload).encode()
    return client.post(
        path,
        content=body,
        headers={
            "X-Hub-Signature-256": sign_payload(
                body, secret if secret is not None else settings.github_webhook_secret
            ),
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": "delivery-e2e",
            "Content-Type": "application/json",
        },
    )


def _store() -> FactStore:
    """Borrow the app's lifespan-owned PostgreSQL fact store."""
    return app.state.fact_store


def test_health(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_litellm_payload_to_inference_call_fact(client: TestClient) -> None:
    payload = _fixture("litellm_standard_logging_object.json")
    body = {
        "provider": "litellm",
        "session_id": "sess-e2e-1",
        "user_id": "dev@example.com",
        "payload": payload,
    }
    resp = client.post("/ingest/gateway", json=body, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["stored"] is True

    # Redelivery returns the first retained Fact identity, with no second row.
    redelivery = client.post("/ingest/gateway", json=body, headers=_auth())
    assert redelivery.status_code == 200
    assert redelivery.json()["stored"] is False
    assert redelivery.json()["fact_id"] == resp.json()["fact_id"]

    calls = _store().read_inference_calls(settings.org_id)
    assert len(calls) == 1
    c = calls[0]
    assert c.inference_call_id == resp.json()["fact_id"]
    assert c.org_id == settings.org_id
    assert c.session_id == "sess-e2e-1"
    assert c.model_call_id == payload["litellm_call_id"]
    assert c.model == payload["model"]
    assert c.output_messages


def test_gateway_hostile_numerics_degrade_not_500(client: TestClient) -> None:
    # json accepts NaN/Infinity and arbitrary-precision ints; int(NaN)
    # raises in the adapter and a >int64 value overflows at the PostgreSQL
    # INSERT. Both must degrade to a stored fact, never a 500.
    body = {
        "provider": "litellm",
        "session_id": "sess-hostile",
        "user_id": "u",
        "payload": {
            "model": "m",
            "messages": [],
            "response": {"choices": [{"message": {"content": "x"}}]},
            "usage": {"prompt_tokens": 10**30, "completion_tokens": float("nan")},
            "response_time_ms": float("inf"),
            "litellm_call_id": "hostile-1",
        },
    }
    # httpx's json= refuses NaN; stdlib dumps emits the NaN/Infinity
    # literals a real client can put on the wire.
    resp = client.post(
        "/ingest/gateway",
        content=json.dumps(body).encode(),
        headers={**_auth(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    c = _store().read_inference_calls(settings.org_id)[0]
    assert (c.input_tokens, c.output_tokens, c.duration_ms) == (None, None, None)


def test_gateway_unsupported_provider_is_400(client: TestClient) -> None:
    body = {
        "provider": "portkey",  # stub adapter: deliberately unregistered
        "session_id": "s",
        "user_id": "u",
        "payload": {},
    }
    assert client.post("/ingest/gateway", json=body, headers=_auth()).status_code == 400


def test_gateway_whitespace_ids_are_422_not_500(client: TestClient) -> None:
    # min_length=1 admitted whitespace-only ids, which then blew up as an
    # uncaught ValidationError (500) at InferenceCall construction. The
    # envelope now mirrors the core NonEmptyId validator: 422 at the door,
    # nothing stored.
    good = {
        "provider": "litellm",
        "session_id": "sess-ws",
        "user_id": "dev@example.com",
        "payload": {},
    }
    for field in ("session_id", "user_id"):
        for bad in ("", " ", "   ", "\t\n"):
            body = {**good, field: bad}
            resp = client.post("/ingest/gateway", json=body, headers=_auth())
            assert resp.status_code == 422, (field, bad, resp.status_code)
    assert _store().read_inference_calls(settings.org_id) == []


def test_otlp_fixtures_to_decision_facts(client: TestClient) -> None:
    assert OTLP_FIXTURES, "capture OTLP fixtures not found"
    # The translator (same function the route calls) defines how many
    # decisions each fixture yields; the HTTP path must lose none of them.
    expected = sum(
        len(parse_otlp_decisions(json.loads(p.read_text()), org_id=settings.org_id))
        for p in OTLP_FIXTURES
    )
    assert expected > 0
    for path in OTLP_FIXTURES:
        resp = client.post(
            "/v1/logs", json=json.loads(path.read_text()), headers=_auth()
        )
        assert resp.status_code == 200, path.name
        # Empty ExportLogsServiceResponse = full success (OTLP/HTTP JSON).
        assert resp.json() == {}, path.name

    decisions = _store().read_decisions(settings.org_id)
    assert len(decisions) == expected
    assert {d.agent_harness for d in decisions} == {
        "claude-code",
        "copilot",
        "codex",
        "pi",
    }
    assert all(d.org_id == settings.org_id for d in decisions)
    assert all(d.session_id for d in decisions)  # no placeholders (ADR 0002)
    assert {d.accepted for d in decisions} == {True, False}

    # Redelivery of the whole batch set: every record collapses.
    for path in OTLP_FIXTURES:
        assert (
            client.post(
                "/v1/logs", json=json.loads(path.read_text()), headers=_auth()
            ).status_code
            == 200
        )
    assert len(_store().read_decisions(settings.org_id)) == expected


def test_otlp_chunked_transfer_body(client: TestClient) -> None:
    # Real OTLP exporters stream chunked (no Content-Length); the route must
    # still assemble and parse the body.
    payload = json.loads(OTLP_FIXTURES[0].read_text())
    body = json.dumps(payload).encode()

    def chunks():
        yield body[: len(body) // 2]
        yield body[len(body) // 2 :]

    resp = client.post(
        "/v1/logs",
        content=chunks(),
        headers={**_auth(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    expected = len(parse_otlp_decisions(payload, org_id=settings.org_id))
    assert len(_store().read_decisions(settings.org_id)) == expected


def test_otlp_edit_observation_to_fact(client: TestClient) -> None:
    # The transcript extractor's wire: one sediment.edit_observation record
    # → one EditObservation fact through the same /v1/logs door; a refire of the
    # same call collapses (first write wins).
    record = {
        "body": {"stringValue": "sediment.edit_observation"},
        "timeUnixNano": "1782578510649000000",
        "attributes": [
            {"key": k, "value": {"stringValue": v}}
            for k, v in {
                "session.id": "sess-eo",
                "tool_use_id": "toolu-eo",
                "tool_name": "Edit",
                "file_path": "/repo/app.py",
                "applied_text": "x = 1",
                "observed_file_text": "x = 2",
                "agent": "claude-code",
            }.items()
        ],
    }
    payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": [record]}]}]}
    assert client.post("/v1/logs", json=payload, headers=_auth()).status_code == 200
    [observation] = _store().read_edit_observations(settings.org_id)
    assert (observation.session_id, observation.call_id) == ("sess-eo", "toolu-eo")
    assert (observation.applied_text, observation.observed_file_text) == (
        "x = 1",
        "x = 2",
    )
    assert client.post("/v1/logs", json=payload, headers=_auth()).status_code == 200
    assert len(_store().read_edit_observations(settings.org_id)) == 1


def test_otlp_rejected_edit_to_fact(client: TestClient) -> None:
    # The refused-edit wire: one sediment.rejected_edit record → one
    # RejectedEdit fact through the same door, riding the same batch as an
    # edit observation. A refire of the same call collapses (first write wins).
    def _attrs(mapping):
        return [{"key": k, "value": {"stringValue": v}} for k, v in mapping.items()]

    observation_record = {
        "body": {"stringValue": "sediment.edit_observation"},
        "timeUnixNano": "1782578510649000000",
        "attributes": _attrs(
            {
                "session.id": "sess-mix",
                "tool_use_id": "toolu-applied",
                "tool_name": "Edit",
                "file_path": "/repo/app.py",
                "applied_text": "x = 1",
                "observed_file_text": "x = 1",
                "agent": "claude-code",
            }
        ),
    }
    rejected_record = {
        "body": {"stringValue": "sediment.rejected_edit"},
        "timeUnixNano": "1782578510649000000",
        "attributes": _attrs(
            {
                "session.id": "sess-mix",
                "tool_use_id": "toolu-refused",
                "tool_name": "Edit",
                "file_path": "/repo/other.py",
                "proposed": "def worse(): ...",
                "agent": "claude-code",
            }
        ),
    }
    payload = {
        "resourceLogs": [
            {"scopeLogs": [{"logRecords": [observation_record, rejected_record]}]}
        ]
    }
    assert client.post("/v1/logs", json=payload, headers=_auth()).status_code == 200
    [rejected] = _store().read_rejected_edits(settings.org_id)
    assert (rejected.session_id, rejected.call_id) == ("sess-mix", "toolu-refused")
    assert rejected.proposed == "def worse(): ..."
    # Both record types landed from the one batch, neither claiming the other.
    [observation] = _store().read_edit_observations(settings.org_id)
    assert observation.call_id == "toolu-applied"

    assert client.post("/v1/logs", json=payload, headers=_auth()).status_code == 200
    assert len(_store().read_rejected_edits(settings.org_id)) == 1


def test_otlp_retry_linkage_to_fact_and_redelivery(client: TestClient) -> None:
    record = {
        "body": {"stringValue": "sediment.retry_linkage"},
        "timeUnixNano": "1782578510649000000",
        "attributes": [
            {"key": key, "value": {"stringValue": value}}
            for key, value in {
                "session.id": "sess-retry",
                "agent": "claude-code",
                "file_path": "/repo/app.py",
                "tool_name": "Edit",
                "rejected_call_id": "toolu-rejected",
                "accepted_call_id": "toolu-accepted",
            }.items()
        ],
    }
    payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": [record]}]}]}

    assert client.post("/v1/logs", json=payload, headers=_auth()).status_code == 200
    [linkage] = _store().read_retry_linkages(settings.org_id)
    assert linkage.session_id == "sess-retry"
    assert linkage.rejected_call_id == "toolu-rejected"
    assert linkage.accepted_call_id == "toolu-accepted"
    assert linkage.tool_name == "Edit"
    assert linkage.raw == {}

    assert client.post("/v1/logs", json=payload, headers=_auth()).status_code == 200
    assert len(_store().read_retry_linkages(settings.org_id)) == 1


def test_otlp_malformed_body_is_400(client: TestClient) -> None:
    resp = client.post(
        "/v1/logs",
        content=b"not json",
        headers={**_auth(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert (
        client.post("/v1/logs", json=["a", "list"], headers=_auth()).status_code == 400
    )


def test_push_fixture_to_push_fact(client: TestClient) -> None:
    payload = _fixture("github_push.json")
    resp = _post_webhook(client, "/ingest/github/push", "push", payload)
    assert resp.status_code == 200
    assert resp.json()["stored"] is True

    redelivery = _post_webhook(client, "/ingest/github/push", "push", payload)
    assert redelivery.json()["stored"] is False

    pushes = _store().read_pushes(settings.org_id)
    assert len(pushes) == 1
    p = pushes[0]
    assert p.org_id == settings.org_id
    assert p.repo == payload["repository"]["full_name"]
    assert p.ref == payload["ref"]
    assert p.after_sha == payload["after"]


def test_workflow_run_fixture_to_ci_outcome_fact(client: TestClient) -> None:
    payload = _fixture("github_workflow_run.json")
    resp = _post_webhook(client, "/ingest/github/ci", "workflow_run", payload)
    assert resp.status_code == 200
    assert resp.json()["stored"] is True

    redelivery = _post_webhook(client, "/ingest/github/ci", "workflow_run", payload)
    assert redelivery.json()["stored"] is False

    outcomes = _store().read_ci_outcomes(settings.org_id)
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.org_id == settings.org_id
    assert o.commit_sha == payload["workflow_run"]["head_sha"]
    # Check identity captured: the workflow definition, not just the run.
    assert o.workflow_name == payload["workflow_run"]["name"]
    assert o.workflow_path == payload["workflow_run"]["path"]
    assert o.run_url == payload["workflow_run"]["html_url"]
    assert o.source_event_id == "delivery-e2e"


def test_pull_request_merge_to_merge_boundary_fact(client: TestClient) -> None:
    payload = _fixture("github_pull_request.json")

    resp = _post_webhook(client, "/ingest/github/pull-request", "pull_request", payload)
    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    redelivery = _post_webhook(
        client, "/ingest/github/pull-request", "pull_request", payload
    )
    assert redelivery.json()["stored"] is False

    [merge] = _store().read_pull_request_merges(settings.org_id)
    assert merge.pr_number == 41
    assert merge.head_sha == "a" * 40
    assert merge.merge_commit_sha == "c" * 40
    assert merge.source_event_id == "delivery-e2e"
    assert _store().read_pull_request_revisions(settings.org_id) == []


def test_pull_request_opened_to_revision_fact(client: TestClient) -> None:
    payload = _fixture("github_pull_request.json")
    payload["action"] = "opened"

    resp = _post_webhook(client, "/ingest/github/pull-request", "pull_request", payload)
    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    redelivery = _post_webhook(
        client, "/ingest/github/pull-request", "pull_request", payload
    )
    assert redelivery.json()["stored"] is False

    [revision] = _store().read_pull_request_revisions(settings.org_id)
    assert revision.org_id == settings.org_id
    assert revision.pr_number == 41
    assert revision.head_sha == "a" * 40
    assert revision.previous_head_sha is None
    assert revision.source_event_id == "delivery-e2e"
    assert _store().read_pull_request_merges(settings.org_id) == []


def test_pull_request_synchronize_to_revision_fact(client: TestClient) -> None:
    payload = _fixture("github_pull_request.json")
    payload["action"] = "synchronize"
    payload["before"] = "d" * 40
    payload["after"] = "a" * 40

    resp = _post_webhook(client, "/ingest/github/pull-request", "pull_request", payload)

    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    [revision] = _store().read_pull_request_revisions(settings.org_id)
    assert revision.previous_head_sha == "d" * 40


def test_pull_request_synchronize_head_mismatch_is_skipped(client: TestClient) -> None:
    payload = _fixture("github_pull_request.json")
    payload["action"] = "synchronize"
    payload["before"] = "d" * 40
    payload["after"] = "e" * 40

    resp = _post_webhook(client, "/ingest/github/pull-request", "pull_request", payload)

    assert resp.status_code == 200
    assert resp.json() == {
        "skipped": True,
        "reason": "synchronize_head_mismatch",
    }
    assert _store().read_pull_request_revisions(settings.org_id) == []


def test_pull_request_unsupported_action_is_skipped(client: TestClient) -> None:
    payload = _fixture("github_pull_request.json")
    payload["action"] = "edited"

    resp = _post_webhook(client, "/ingest/github/pull-request", "pull_request", payload)

    assert resp.status_code == 200
    assert resp.json() == {
        "skipped": True,
        "reason": "unsupported_pull_request_action",
    }
    assert _store().read_pull_request_revisions(settings.org_id) == []
    assert _store().read_pull_request_merges(settings.org_id) == []


def test_wrong_event_type_is_skipped_not_stored(client: TestClient) -> None:
    resp = _post_webhook(
        client, "/ingest/github/push", "ping", {"zen": "Design for failure."}
    )
    assert resp.status_code == 200
    assert resp.json()["skipped"] is True
    resp = _post_webhook(
        client, "/ingest/github/ci", "push", _fixture("github_push.json")
    )
    assert resp.json()["skipped"] is True
    resp = _post_webhook(
        client,
        "/ingest/github/pull-request",
        "push",
        _fixture("github_push.json"),
    )
    assert resp.json()["skipped"] is True
    assert _store().read_pushes(settings.org_id) == []
    assert _store().read_ci_outcomes(settings.org_id) == []
    assert _store().read_pull_request_merges(settings.org_id) == []


def test_wrong_event_with_bad_signature_is_401(client: TestClient) -> None:
    # Signature is verified BEFORE event-type discrimination: GitHub's setup
    # ping against a misconfigured secret must show red in the webhook UI,
    # not a green 200 {skipped}.
    resp = _post_webhook(
        client,
        "/ingest/github/push",
        "ping",
        {"zen": "Anything added..."},
        secret="wrong",
    )
    assert resp.status_code == 401
    resp = _post_webhook(client, "/ingest/github/ci", "ping", {}, secret="wrong")
    assert resp.status_code == 401
    resp = _post_webhook(
        client, "/ingest/github/pull-request", "ping", {}, secret="wrong"
    )
    assert resp.status_code == 401


def test_webhook_malformed_body_with_valid_hmac_is_400(client: TestClient) -> None:
    # Signature-valid but not a JSON object: 400 (which stops GitHub's
    # redelivery loop), never 500.
    for path, event in (
        ("/ingest/github/push", "push"),
        ("/ingest/github/ci", "workflow_run"),
        ("/ingest/github/pull-request", "pull_request"),
    ):
        for body in (b"not json", b'["an", "array"]'):
            resp = client.post(
                path,
                content=body,
                headers={
                    "X-Hub-Signature-256": sign_payload(
                        body, settings.github_webhook_secret
                    ),
                    "X-GitHub-Event": event,
                    "Content-Type": "application/json",
                },
            )
            assert resp.status_code == 400, (path, body)


def test_bad_bearer_is_401_and_stores_nothing(client: TestClient) -> None:
    body = {
        "provider": "litellm",
        "session_id": "s",
        "user_id": "u",
        "payload": _fixture("litellm_standard_logging_object.json"),
    }
    bad = {"Authorization": "Bearer wrong-token"}
    assert client.post("/ingest/gateway", json=body, headers=bad).status_code == 401
    assert client.post("/v1/logs", json={}, headers=bad).status_code == 401
    # A non-ASCII header must be a 401, never a 500 (compare_digest raises
    # TypeError on non-ASCII str). Sent as latin-1 bytes — what a raw HTTP
    # client can put on the wire.
    evil = {b"Authorization": "Bearer wröng".encode("latin-1")}
    assert client.post("/v1/logs", json={}, headers=evil).status_code == 401
    # A missing header is an auth failure, not a schema error.
    assert client.post("/v1/logs", json={}).status_code == 401
    # A scheme-less header must not authenticate, even with the right token.
    schemeless = {"Authorization": settings.api_bearer_token}
    assert client.post("/v1/logs", json={}, headers=schemeless).status_code == 401
    # The scheme is case-insensitive (RFC 9110); some exporters send lowercase.
    lower = {"Authorization": f"bearer {settings.api_bearer_token}"}
    assert client.post("/v1/logs", json={}, headers=lower).status_code == 200
    assert _store().read_inference_calls(settings.org_id) == []


def test_bad_hmac_is_401_and_stores_nothing(client: TestClient) -> None:
    push = _fixture("github_push.json")
    run = _fixture("github_workflow_run.json")
    pull_request = _fixture("github_pull_request.json")
    assert (
        _post_webhook(
            client, "/ingest/github/push", "push", push, secret="wrong"
        ).status_code
        == 401
    )
    assert (
        _post_webhook(
            client, "/ingest/github/ci", "workflow_run", run, secret="wrong"
        ).status_code
        == 401
    )
    assert (
        _post_webhook(
            client,
            "/ingest/github/pull-request",
            "pull_request",
            pull_request,
            secret="wrong",
        ).status_code
        == 401
    )
    assert _store().read_pushes(settings.org_id) == []
    assert _store().read_ci_outcomes(settings.org_id) == []
    assert _store().read_pull_request_merges(settings.org_id) == []


def test_oversized_webhook_body_is_413_before_auth(
    client: TestClient, monkeypatch
) -> None:
    """The pre-auth body read is bounded. Cap is lowered rather than sending
    25 MB — the branch is what matters, not the constant."""
    from sediment_api import deps

    monkeypatch.setattr(deps, "MAX_BODY_BYTES", 1024)
    oversized = json.dumps({"pad": "x" * 4096}).encode()
    # Correctly signed: 413 must win over signature checking, proving the cap
    # is enforced during the read and not after the body is already buffered.
    resp = client.post(
        "/ingest/github/push",
        content=oversized,
        headers={
            "X-Hub-Signature-256": sign_payload(
                oversized, settings.github_webhook_secret
            ),
            "X-GitHub-Event": "push",
        },
    )
    assert resp.status_code == 413
    # An unsigned oversized body is rejected on size too — an unauthenticated
    # caller never gets to size the allocation.
    assert (
        client.post(
            "/ingest/github/push", content=oversized, headers={"X-GitHub-Event": "push"}
        ).status_code
        == 413
    )
    # A body under the cap still flows normally (the guard is not a blanket
    # reject): valid signature, wrong event type → the ordinary skip path.
    assert (
        _post_webhook(
            client, "/ingest/github/push", "ping", {"zen": "small"}
        ).status_code
        == 200
    )
    assert _store().read_pushes(settings.org_id) == []


def test_capped_read_peak_memory_stays_at_the_ceiling() -> None:
    """The cap is only a real ceiling if buffering does not overshoot it.

    A chunk list plus ``b"".join`` holds the chunks and the joined copy alive
    at once, peaking at ~2x the limit — so a 25 MB cap would admit 50 MB and
    the ceiling would not mean what it says. Exercises
    the real reader against a fake stream rather than re-implementing it.
    """
    import tracemalloc

    from sediment_api import deps

    limit = 8 * 1024 * 1024
    chunk_size = 64 * 1024

    class _FakeStreamRequest:
        """Yields distinct chunk objects, as an ASGI server does. Sharing one
        object would make the list-of-chunks approach look free."""

        async def stream(self):
            for _ in range(limit // chunk_size):
                yield bytes(chunk_size)

    tracemalloc.start()
    body = asyncio.run(deps._read_body_capped(_FakeStreamRequest(), limit))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(body) == limit
    # 1.25x leaves room for bytearray's amortized over-allocation while still
    # failing the ~2x that a join reintroduces.
    assert peak < limit * 1.25, f"peak {peak / limit:.2f}x the limit, expected ~1x"


def test_deeply_nested_body_never_500s_on_any_door(client: TestClient) -> None:
    """AGENTS.md §API Conventions: malformed bodies from authenticated
    callers are 400/422, never 500. The envelope doors used to 500:
    FastAPI's default RequestValidationError handler re-serializes the
    offending input through jsonable_encoder, which recurses to death on
    deeply nested JSON. One app-level handler covers every envelope door —
    all six doors are pinned here so a newly added envelope router cannot
    silently reintroduce the defect."""
    raw = TestClient(client.app, raise_server_exceptions=False)
    nested = ("[" * 3000 + "]" * 3000).encode()
    headers = {**_auth(), "Content-Type": "application/json"}

    resp = raw.post("/ingest/gateway", content=nested, headers=headers)
    assert resp.status_code == 422
    # The 422 still names the failing location — without echoing the input.
    detail = resp.json()["detail"]
    assert detail and all("loc" in e and "msg" in e for e in detail)
    assert len(resp.content) < 4096

    resp = raw.post("/ingest/ci", content=nested, headers=headers)
    assert resp.status_code == 422

    # The hand-parsed doors catch their own body parse: 400, not 500.
    assert raw.post("/v1/logs", content=nested, headers=headers).status_code == 400
    for path, event in (
        ("/ingest/github/push", "push"),
        ("/ingest/github/ci", "workflow_run"),
        ("/ingest/github/pull-request", "pull_request"),
    ):
        resp = raw.post(
            path,
            content=nested,
            headers={
                "X-Hub-Signature-256": sign_payload(
                    nested, settings.github_webhook_secret
                ),
                "X-GitHub-Event": event,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 400, path


def test_every_door_bounds_body_size(client: TestClient, monkeypatch) -> None:
    """The body ceiling holds on every door, not only the webhook one
    — the webhook door read before it could authenticate and grew the cap
    first, but the bearer doors were an accident of who reads the body, not
    a decision. Enforced app-wide by BodySizeLimitMiddleware during the
    read. Cap lowered rather than posting 25 MB — the branch matters, not
    the constant."""
    from sediment_api import deps

    monkeypatch.setattr(deps, "MAX_BODY_BYTES", 1024)
    big = json.dumps({"pad": "x" * 4096}).encode()
    headers = {**_auth(), "Content-Type": "application/json"}
    assert (
        client.post("/ingest/gateway", content=big, headers=headers).status_code == 413
    )
    assert client.post("/ingest/ci", content=big, headers=headers).status_code == 413
    assert client.post("/v1/logs", content=big, headers=headers).status_code == 413
    assert _store().read_inference_calls(settings.org_id) == []


def test_missing_webhook_headers(client: TestClient) -> None:
    body = json.dumps(_fixture("github_push.json")).encode()
    # No signature at all: an unsigned request is unauthenticated (401),
    # not a schema error (422).
    resp = client.post(
        "/ingest/github/push", content=body, headers={"X-GitHub-Event": "push"}
    )
    assert resp.status_code == 401
    # No event header: not a GitHub delivery at all (GitHub always sends
    # it) — the documented malformed-request response (422) applies.
    resp = client.post(
        "/ingest/github/push",
        content=body,
        headers={
            "X-Hub-Signature-256": sign_payload(body, settings.github_webhook_secret)
        },
    )
    assert resp.status_code == 422
    assert _store().read_pushes(settings.org_id) == []


def test_client_supplied_org_is_rejected_on_gateway(client: TestClient) -> None:
    body = {
        "provider": "litellm",
        "session_id": "s",
        "user_id": "u",
        "org_id": "evil-org",  # extra="forbid" → 422, not silently honored
        "payload": {},
    }
    assert client.post("/ingest/gateway", json=body, headers=_auth()).status_code == 422
    assert _store().read_inference_calls(settings.org_id) == []


def test_webhook_org_query_param_is_ignored(client: TestClient) -> None:
    # Requests cannot override tenancy with ?org_id=; every Fact carries
    # the deployment's configured organization.
    payload = _fixture("github_push.json")
    resp = _post_webhook(client, "/ingest/github/push?org_id=evil-org", "push", payload)
    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    store = _store()
    assert store.read_pushes("evil-org") == []
    assert len(store.read_pushes(settings.org_id)) == 1


def test_webhook_malformed_sha_skips_never_500s(client: TestClient) -> None:
    """You cannot 422 a webhook into correctness — a junk join key is
    skipped and logged (capture fail-soft), stored nowhere, and never 500s.
    Before the schema-level CommitSha, the junk head_sha stored a CIOutcome
    that could never join a commit; the identical bytes 422 on /ingest/ci."""
    run = _fixture("github_workflow_run.json")
    run["workflow_run"]["head_sha"] = "not-a-sha"
    resp = _post_webhook(client, "/ingest/github/ci", "workflow_run", run)
    assert resp.status_code == 200
    assert resp.json()["skipped"] is True
    assert _store().read_ci_outcomes(settings.org_id) == []

    push = _fixture("github_push.json")
    push["after"] = "abc1234"  # abbreviated: a join key is never an abbreviation
    resp = _post_webhook(client, "/ingest/github/push", "push", push)
    assert resp.status_code == 200
    assert resp.json()["skipped"] is True
    assert _store().read_pushes(settings.org_id) == []

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gateway envelope identity resolution: envelope ids optional,
server-side ``resolve_identity`` for the litellm provider only, and the
``no_session`` skip. The thinned callback forwards payloads without
extracting identity — these tests pin the server half of that contract.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sediment_core import REDACTION_MARKER, FactStore

from sediment_api.config import settings
from sediment_api.main import app


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_bearer_token}"}


def _store() -> FactStore:
    return app.state.fact_store


def _payload(call_id: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": "m",
        "messages": [],
        "response": {"choices": [{"message": {"content": "x"}}]},
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        "response_time_ms": 5,
        "litellm_call_id": call_id,
    }
    base.update(over)
    return base


_IDENTITY_META = {
    "requester_metadata": {"session_id": "sess-payload", "user_id": "dev-payload"}
}


def test_no_session_anywhere_skips_with_reason(client: TestClient) -> None:
    body = {"provider": "litellm", "payload": _payload("skip-1")}
    resp = client.post("/ingest/gateway", json=body, headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {"skipped": True, "reason": "no_session"}
    assert _store().read_inference_calls(settings.org_id) == []


def test_payload_identity_resolves_when_envelope_ids_absent(
    client: TestClient,
) -> None:
    body = {
        "provider": "litellm",
        "payload": _payload("resolve-1", metadata=_IDENTITY_META),
    }
    resp = client.post("/ingest/gateway", json=body, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    c = _store().read_inference_calls(settings.org_id)[0]
    assert c.session_id == "sess-payload"
    assert c.user_id == "dev-payload"


@pytest.mark.parametrize("agent", ["pi", "codex"])
def test_malformed_metadata_uses_native_session_through_ingest(client, agent):
    session_id = "11223344-5566-4778-899a-bbccddeeff00"
    headers = (
        {"x-sediment-session": session_id}
        if agent == "pi"
        else {"x-codex-turn-metadata": json.dumps({"session_id": session_id})}
    )
    body = {
        "provider": "litellm",
        "payload": _payload(
            f"shape-{agent}",
            metadata={
                "session_id": {"nested": True},
                "requester_metadata": {"session_id": ["wrong"]},
                "requester_custom_headers": headers,
            },
        ),
    }
    response = client.post("/ingest/gateway", json=body, headers=_auth())
    assert response.status_code == 200
    assert response.json()["stored"] is True
    [call] = _store().read_inference_calls(settings.org_id)
    assert call.session_id == session_id
    replay = client.post("/ingest/gateway", json=body, headers=_auth())
    assert replay.status_code == 200
    assert replay.json()["stored"] is False
    assert _store().read_inference_calls(settings.org_id) == [call]


def test_claude_api_key_identity_json_stores_under_its_real_session(
    client: TestClient,
) -> None:
    session_id = "11223344-5566-4778-899a-bbccddeeff00"
    identity = json.dumps(
        {
            "device_id": "d3adbeefcafe1234",
            "account_uuid": "",
            "session_id": session_id,
        }
    )
    body = {
        "provider": "litellm",
        "payload": _payload("claude-api-key-1", end_user=identity),
    }

    resp = client.post("/ingest/gateway", json=body, headers=_auth())

    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    [call] = _store().read_inference_calls(settings.org_id)
    assert call.session_id == session_id
    assert call.user_id == "device_d3adbeefcafe"


def test_gateway_credentials_never_reach_postgresql(client: TestClient) -> None:
    message_secret = "message-secret-1234567890"
    completion_secret = "completion-secret-1234567890"
    tool_secret = "short"
    authorization_secret = 'Digest username="Mufasa", realm="sediment-realm"'
    raw_secret = "raw-secret-12345678901234567"
    payload = _payload(
        "redact-1",
        messages=[{"role": "user", "content": f"api_key='{message_secret}'"}],
        response={
            "choices": [
                {
                    "message": {
                        "content": f"Authorization: Bearer {completion_secret}",
                        "tool_calls": [
                            {
                                "id": "toolu-redact",
                                "type": "function",
                                "function": {
                                    "name": "Write",
                                    "arguments": json.dumps(
                                        {
                                            "api_key": tool_secret,
                                            "authorization": authorization_secret,
                                        }
                                    ),
                                },
                            }
                        ],
                    }
                }
            ]
        },
        headers={"x-api-key": raw_secret},
    )
    body = {
        "provider": "litellm",
        "session_id": "sess-redact",
        "user_id": "dev-redact",
        "payload": payload,
    }

    resp = client.post("/ingest/gateway", json=body, headers=_auth())

    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    [stored] = _store().read_inference_calls(settings.org_id)
    blob = json.dumps(stored.model_dump(mode="json"), sort_keys=True)
    for secret in (
        message_secret,
        completion_secret,
        tool_secret,
        authorization_secret,
        "Mufasa",
        "sediment-realm",
        raw_secret,
    ):
        assert secret not in blob
    assert blob.count(REDACTION_MARKER) >= 4


def test_malformed_gateway_arguments_redact_unterminated_authorization(
    client: TestClient,
) -> None:
    secret = "malformed-authorization-secret"
    arguments = f'{{"authorization":"Digest username={secret}'
    payload = _payload(
        "redact-malformed-1",
        response={
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "toolu-redact-malformed",
                                "type": "function",
                                "function": {
                                    "name": "Write",
                                    "arguments": arguments,
                                },
                            }
                        ],
                    }
                }
            ]
        },
    )
    body = {
        "provider": "litellm",
        "session_id": "sess-redact-malformed",
        "user_id": "dev-redact-malformed",
        "payload": payload,
    }

    resp = client.post("/ingest/gateway", json=body, headers=_auth())

    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    [stored] = _store().read_inference_calls(settings.org_id)
    blob = json.dumps(stored.model_dump(mode="json"), sort_keys=True)
    assert secret not in blob
    assert REDACTION_MARKER in blob


def test_envelope_ids_win_over_payload_identity(client: TestClient) -> None:
    # Back-compat: already-deployed callbacks and shims post explicit ids.
    body = {
        "provider": "litellm",
        "session_id": "sess-env",
        "user_id": "dev-env",
        "payload": _payload("envelope-1", metadata=_IDENTITY_META),
    }
    resp = client.post("/ingest/gateway", json=body, headers=_auth())
    assert resp.status_code == 200
    c = _store().read_inference_calls(settings.org_id)[0]
    assert c.session_id == "sess-env"
    assert c.user_id == "dev-env"


def test_envelope_session_without_user_stores_user_as_absent(
    client: TestClient,
) -> None:
    # A session without a user still captures without fabricating identity.
    body = {
        "provider": "litellm",
        "session_id": "sess-solo",
        "payload": _payload("solo-1"),
    }
    resp = client.post("/ingest/gateway", json=body, headers=_auth())
    assert resp.status_code == 200
    c = _store().read_inference_calls(settings.org_id)[0]
    assert (c.session_id, c.user_id) == ("sess-solo", None)


def test_empty_string_ids_still_422_when_present(client: TestClient) -> None:
    # Optional does not mean lax: a present-but-empty id is a placeholder
    # (ADR 0002), rejected at the envelope as before.
    for field in ("session_id", "user_id"):
        for bad in ("", "   "):
            body = {
                "provider": "litellm",
                "session_id": "sess-ok",
                "user_id": "dev-ok",
                "payload": _payload("bad-1"),
                field: bad,
            }
            resp = client.post("/ingest/gateway", json=body, headers=_auth())
            assert resp.status_code == 422, (field, bad)
    assert _store().read_inference_calls(settings.org_id) == []


def test_non_litellm_provider_never_runs_litellm_heuristics(
    client: TestClient, monkeypatch
) -> None:
    # The heuristics are LiteLLM-SLO-specific: a future portkey/helicone
    # adapter must not inherit them. Register a stub adapter so the request
    # reaches identity handling, and make any call into resolve_identity
    # explode.
    from sediment_api.routers import gateway as gateway_module

    class _StubAdapter:
        def normalize(self, payload, **kw):  # pragma: no cover - never reached
            raise AssertionError("normalize must not run for a skipped request")

    def _boom(payload):  # pragma: no cover - the assertion is the test
        raise AssertionError("litellm heuristics ran for a non-litellm provider")

    monkeypatch.setitem(gateway_module.ADAPTERS, "portkey", _StubAdapter())
    monkeypatch.setattr(gateway_module, "resolve_identity", _boom)

    body = {
        "provider": "portkey",
        "payload": _payload("portkey-1", metadata=_IDENTITY_META),
    }
    resp = client.post("/ingest/gateway", json=body, headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {"skipped": True, "reason": "no_session"}
    assert _store().read_inference_calls(settings.org_id) == []


def test_whitespace_payload_identity_never_500s(client: TestClient) -> None:
    """A whitespace-only session or user inside the forwarded payload used
    to surface from resolve_identity and raise at InferenceCall construction —
    a 500 where the callback-era envelope path 422'd. Whitespace is absent
    (falls through the source chain), so a whitespace session skips and a
    whitespace end_user stays absent."""
    raw = TestClient(client.app, raise_server_exceptions=False)
    body = {
        "provider": "litellm",
        "payload": _payload(
            "ws-1", metadata={"requester_metadata": {"session_id": "   "}}
        ),
    }
    resp = raw.post("/ingest/gateway", json=body, headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {"skipped": True, "reason": "no_session"}

    body = {
        "provider": "litellm",
        "payload": _payload(
            "ws-2",
            metadata={"requester_metadata": {"session_id": "sess-ws-ok"}},
            end_user="   ",
        ),
    }
    resp = raw.post("/ingest/gateway", json=body, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    c = _store().read_inference_calls(settings.org_id)[0]
    assert (c.session_id, c.user_id) == ("sess-ws-ok", None)

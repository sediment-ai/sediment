# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prepared gateway capture preserves identity and chronology across replay."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from sediment_api.config import settings
from sediment_api.main import app
from sediment_capture import LiteLLMAdapter
from sediment_core import GatewayProvider, InferenceCall
from sediment_derive import MirrorManager, derive_rollouts

BOUNDARY = datetime(2026, 9, 11, tzinfo=UTC)
CAPTURE_ID = "7754fe46-50db-45f9-93ef-5b3e99ab70b3"


def _body(index=0, *, keyed=False):
    messages = [{"role": "user", "content": "start"}]
    for previous in range(index):
        messages.extend(
            [
                {"role": "assistant", "content": f"step {previous}"},
                {"role": "user", "content": f"continue {previous}"},
            ]
        )
    return {
        "provider": "litellm",
        "session_id": "capture-session",
        "capture": {
            "id": str(UUID(int=UUID(CAPTURE_ID).int + index)),
            "observed_at": (BOUNDARY + timedelta(minutes=index)).isoformat(),
        },
        "payload": {
            "litellm_call_id": f"provider-{index}" if keyed else None,
            "messages": messages,
            "response": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": f"step {index}",
                        }
                    }
                ]
            },
        },
    }


def _post(client, body):
    return client.post(
        "/ingest/gateway",
        json=body,
        headers={"Authorization": f"Bearer {settings.api_bearer_token}"},
    )


@pytest.mark.parametrize("keyed", [False, True])
@pytest.mark.parametrize("order", [(0, 1, 2), (2, 0, 1)])
def test_gateway_capture_replay_retains_call_and_rollout_chronology(
    client, tmp_path, keyed, order
):
    receipts = {}
    for index in order:
        response = _post(client, _body(index, keyed=keyed))
        assert response.status_code == 200
        receipts[index] = response.json()
        assert receipts[index]["stored"] is True
    store = app.state.fact_store
    calls = store.read_inference_calls(settings.org_id)
    assert [call.observed_at for call in calls] == [
        BOUNDARY + timedelta(minutes=index) for index in range(3)
    ]
    assert [call.model_call_id for call in calls] == (
        [f"provider-{index}" for index in range(3)] if keyed else [None] * 3
    )
    [session] = store.read_sessions(settings.org_id)
    assert session.first_observed_at == BOUNDARY
    assert session.last_observed_at == BOUNDARY + timedelta(minutes=2)
    mirrors = MirrorManager(tmp_path / "mirrors")
    [rollout] = derive_rollouts(store, mirrors, settings.org_id)
    assert len(rollout.segments) == 1
    assert [turn.inference_call_id for turn in rollout.segments[0]] == [
        receipts[index]["fact_id"] for index in range(3)
    ]
    for index in reversed(order):
        response = _post(client, _body(index, keyed=keyed))
        assert response.json() == {
            "fact_id": receipts[index]["fact_id"],
            "stored": False,
        }
    assert store.read_inference_calls(settings.org_id) == calls
    assert store.read_sessions(settings.org_id) == [session]
    assert derive_rollouts(store, mirrors, settings.org_id) == [rollout]


@pytest.mark.parametrize(
    "stamped,replay_stamped", [(False, False), (False, True), (True, True)]
)
def test_gateway_natural_duplicate_receipt_names_original_fact(
    client, stamped, replay_stamped
):
    body = _body(keyed=True)
    if not stamped:
        body.pop("capture")
    first = _post(client, body)
    assert first.status_code == 200
    if replay_stamped:
        body["capture"] = _body(1)["capture"]
    replay = _post(client, body)
    assert replay.status_code == 200
    assert replay.json() == {"fact_id": first.json()["fact_id"], "stored": False}


@pytest.mark.parametrize(
    "capture",
    [
        {},
        {"id": CAPTURE_ID},
        {"observed_at": BOUNDARY.isoformat()},
        {"id": "invalid", "observed_at": BOUNDARY.isoformat()},
        {"id": CAPTURE_ID, "observed_at": "2026-09-11T00:00:00"},
        {"id": CAPTURE_ID, "observed_at": "invalid"},
        {"id": CAPTURE_ID, "observed_at": BOUNDARY.isoformat(), "org_id": "foreign"},
        [],
        "invalid",
        42,
    ],
)
def test_gateway_declines_malformed_capture_before_storage(client, capture):
    body = _body()
    body["capture"] = capture
    assert _post(client, body).status_code == 422
    assert app.state.fact_store.read_inference_calls(settings.org_id) == []
    assert app.state.fact_store.read_sessions(settings.org_id) == []


def test_gateway_capture_uuid_is_scoped_to_deployment_org(client, monkeypatch):
    first = _post(client, _body())
    assert first.status_code == 200
    monkeypatch.setattr(settings, "org_id", "another-org")
    second = _post(client, _body())
    assert second.status_code == 200
    assert first.json()["fact_id"] != second.json()["fact_id"]
    assert second.json()["stored"] is True


@pytest.mark.parametrize("foreign", [False, True])
def test_gateway_capture_conflict_declines_without_exposing_retained_id(
    client, foreign
):
    body = _body(keyed=True)
    store = app.state.fact_store
    if foreign:
        # A legacy producer can have occupied the stamped primary key in
        # another organization. The caller must not receive that Fact's ID.
        candidate = LiteLLMAdapter().normalize(
            body["payload"],
            session_id=body["session_id"],
            user_id=None,
            org_id=settings.org_id,
            capture_id=UUID(CAPTURE_ID),
            observed_at=BOUNDARY,
        )
        first_id = candidate.inference_call_id
        store.store_inference_call(
            InferenceCall(
                inference_call_id=first_id,
                org_id="foreign",
                session_id="foreign-session",
                gateway_provider=GatewayProvider.LITELLM,
                model_call_id="foreign-provider",
                input_messages=[],
                output_messages=[],
            )
        )
    else:
        first = _post(client, body)
        assert first.status_code == 200
        first_id = first.json()["fact_id"]
        body = deepcopy(body)
        body["payload"]["litellm_call_id"] = "different-source-call"
    response = _post(client, body)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "inference_call_identity_conflict"
    assert first_id not in response.text
    assert "foreign" not in response.text

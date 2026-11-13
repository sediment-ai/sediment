# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ingest response envelopes: the committed OpenAPI names each outcome, and
representative stored, duplicate, and skipped bodies validate against it
while keeping their exact wire shapes (AGENTS.md §API Conventions)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from sediment_capture import sign_payload

from sediment_api.config import settings

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "packages/capture/tests/fixtures"
SPEC = yaml.safe_load((REPO_ROOT / "openapi.yaml").read_text(encoding="utf-8"))
RECEIPT = "#/components/schemas/IngestReceipt"
SKIPPED = "#/components/schemas/IngestSkipped"
OTLP = "#/components/schemas/OTLPExportLogsResponse"


def _ok_schema(path: str) -> dict[str, Any]:
    response = SPEC["paths"][path]["post"]["responses"]["200"]
    return response["content"]["application/json"]["schema"]


def _assert_matches_spec(path: str, response: httpx.Response) -> None:
    assert response.status_code == 200
    schema = {**_ok_schema(path), "components": SPEC["components"]}
    Draft202012Validator(schema).validate(response.json())


@pytest.mark.parametrize(
    ("path", "refs"),
    [
        ("/ingest/gateway", [RECEIPT, SKIPPED]),
        ("/ingest/github/push", [RECEIPT, SKIPPED]),
        ("/ingest/github/repository", [RECEIPT, SKIPPED]),
        ("/ingest/github/ci", [RECEIPT, SKIPPED]),
        ("/ingest/github/pull-request", [RECEIPT, SKIPPED]),
        ("/ingest/ci", [RECEIPT]),
        ("/ingest/compute", [RECEIPT]),
        ("/v1/logs", [OTLP]),
    ],
)
def test_each_ingest_route_names_its_envelopes(path: str, refs: list[str]) -> None:
    schema = _ok_schema(path)
    options = schema.get("anyOf", [schema])
    assert [option["$ref"] for option in options] == refs


def test_envelope_schemas_are_closed() -> None:
    schemas = SPEC["components"]["schemas"]
    receipt, skipped, otlp = (
        schemas[name]
        for name in ("IngestReceipt", "IngestSkipped", "OTLPExportLogsResponse")
    )
    assert receipt["required"] == ["fact_id", "stored"]
    assert receipt["properties"]["fact_id"]["type"] == "string"
    assert receipt["properties"]["stored"]["type"] == "boolean"
    assert skipped["required"] == ["skipped", "reason"]
    assert skipped["properties"]["skipped"]["const"] is True
    assert otlp["properties"] == {}
    for schema in (receipt, skipped, otlp):
        assert schema["additionalProperties"] is False


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_bearer_token}"}


def _webhook(
    client: TestClient, path: str, event: str, payload: dict[str, Any]
) -> httpx.Response:
    body = json.dumps(payload).encode()
    return client.post(
        path,
        content=body,
        headers={
            "X-Hub-Signature-256": sign_payload(body, settings.github_webhook_secret),
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": "delivery-envelopes",
            "Content-Type": "application/json",
        },
    )


def test_gateway_stored_duplicate_and_skipped_bodies(client: TestClient) -> None:
    payload = json.loads(
        (FIXTURES / "litellm_standard_logging_object.json").read_text()
    )
    body = {"provider": "litellm", "session_id": "sess-envelope", "payload": payload}

    first = client.post("/ingest/gateway", json=body, headers=_bearer())
    again = client.post("/ingest/gateway", json=body, headers=_bearer())
    skipped = client.post(
        "/ingest/gateway",
        json={"provider": "litellm", "payload": {"litellm_call_id": "no-session"}},
        headers=_bearer(),
    )

    for response in (first, again, skipped):
        _assert_matches_spec("/ingest/gateway", response)
    fact_id = first.json()["fact_id"]
    assert first.json() == {"fact_id": fact_id, "stored": True}
    assert again.json() == {"fact_id": fact_id, "stored": False}
    assert skipped.json() == {"skipped": True, "reason": "no_session"}


def test_webhook_stored_duplicate_and_skipped_bodies(client: TestClient) -> None:
    push = json.loads((FIXTURES / "github_push.json").read_text())

    first = _webhook(client, "/ingest/github/push", "push", push)
    again = _webhook(client, "/ingest/github/push", "push", push)
    skipped = _webhook(client, "/ingest/github/push", "ping", {"zen": "ok"})

    for response in (first, again, skipped):
        _assert_matches_spec("/ingest/github/push", response)
    fact_id = first.json()["fact_id"]
    assert first.json() == {"fact_id": fact_id, "stored": True}
    assert again.json() == {"fact_id": fact_id, "stored": False}
    assert skipped.json() == {"skipped": True, "reason": "not a push event"}


def test_otlp_success_stays_empty(client: TestClient) -> None:
    [fixture] = sorted(FIXTURES.glob("otlp/claude_code/tool_decision_accept_user.json"))
    payload = json.loads(fixture.read_text())

    for _ in range(2):
        response = client.post("/v1/logs", json=payload, headers=_bearer())
        _assert_matches_spec("/v1/logs", response)
        assert response.content == b"{}"

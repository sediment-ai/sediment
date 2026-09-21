# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public authenticated capture preserves repository identity and receipts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sediment_capture import sign_payload
from sediment_core import FactStore
from sqlalchemy.exc import OperationalError

FIXTURES = Path(__file__).resolve().parents[3] / "packages/capture/tests/fixtures"
IDENTITY = {
    "repository_provider": "github",
    "repository_host": "github.com",
    "repository_id": "186853002",
}


def _payload(kind):
    if kind == "repository":
        return {
            "action": "renamed",
            "repository": {"id": 186853002, "full_name": "acme/new"},
            "changes": {"repository": {"name": {"from": "old"}}},
        }
    name = "pull_request" if kind.startswith("pull_request") else kind
    payload = json.loads((FIXTURES / f"github_{name}.json").read_text())
    payload["repository"]["id"] = 186853002
    if kind.startswith("pull_request"):
        payload["pull_request"]["head"]["repo"]["id"] = 186853003
        if kind == "pull_request_revision":
            payload["action"] = "opened"
    return payload


def _post(client, kind, payload, *, delivery="delivery-1", valid=True):
    from sediment_api.config import settings

    event = "pull_request" if kind.startswith("pull_request") else kind
    route = {"workflow_run": "ci", "pull_request": "pull-request"}.get(event, event)
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "Host": "spoofed.example.test",
        "X-Forwarded-Host": "spoofed.example.test",
        "X-Hub-Signature-256": sign_payload(
            body, settings.github_webhook_secret if valid else "incorrect"
        ),
    }
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    return client.post(f"/ingest/github/{route}", content=body, headers=headers)


def _facts(kind):
    from sediment_api.config import settings
    from sediment_api.main import app

    method = {
        "push": "read_pushes",
        "workflow_run": "read_ci_outcomes",
        "pull_request_merge": "read_pull_request_merges",
        "pull_request_revision": "read_pull_request_revisions",
        "repository": "read_repository_renames",
    }[kind]
    return getattr(app.state.fact_store, method)(settings.org_id)


@pytest.mark.parametrize(
    "kind",
    [
        "push",
        "workflow_run",
        "pull_request_merge",
        "pull_request_revision",
        "repository",
    ],
)
def test_signed_capture_uses_configured_host_and_retained_receipt(
    client, monkeypatch, kind
):
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "mirror_path", None)
    monkeypatch.setattr(settings, "github_host", "github.example.test")
    payload = _payload(kind)
    first = _post(client, kind, payload)
    assert first.status_code == 200, first.text
    assert first.json()["stored"] is True
    [retained] = _facts(kind)
    second = _post(client, kind, payload)
    assert second.status_code == 200, second.text
    assert second.json() == {"fact_id": first.json()["fact_id"], "stored": False}
    assert _facts(kind) == [retained]
    assert retained.repository_host == "github.example.test"
    assert retained.repository_id == "186853002"
    if kind.startswith("pull_request"):
        assert retained.head_repository_id == "186853003"
    if kind == "repository":
        assert retained.occurred_at is None


def test_rename_storage_failure_is_retryable_without_mirror_work(
    client, monkeypatch, caplog
):
    from sediment_api.config import settings
    from sediment_api.routers import forge

    monkeypatch.setattr(settings, "mirror_path", "/private/mirror")

    def unavailable(*args, **kwargs):
        raise OperationalError(
            "private statement", {}, Exception("private database password")
        )

    def forbidden(*args, **kwargs):
        pytest.fail("mirror accessed before rename storage")

    monkeypatch.setattr(FactStore, "store_repository_rename_receipt", unavailable)
    monkeypatch.setattr(forge, "MirrorManager", forbidden)
    response = _post(client, "repository", _payload("repository"))
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "database_unavailable"
    assert "private database password" not in response.text + caplog.text
    assert _facts("repository") == []


@pytest.mark.parametrize(
    "kind",
    ["workflow_run", "pull_request_merge", "pull_request_revision", "repository"],
)
def test_conflicting_delivery_returns_closed_409(client, monkeypatch, kind):
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "mirror_path", None)
    payload = _payload(kind)
    first = _post(client, kind, payload)
    assert first.status_code == 200
    if kind == "repository":
        payload["repository"]["full_name"] = "acme/conflicting"
    elif kind.startswith("pull_request"):
        payload["pull_request"]["head"]["repo"]["id"] = 186853099
    else:
        payload["repository"]["id"] = 186853099
    conflict = _post(client, kind, payload)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "repository_identity_conflict"
    assert first.json()["fact_id"] not in conflict.text
    assert len(_facts(kind)) == 1


def test_repository_auth_precedes_skips_and_absent_delivery_stays_absent(client):
    for action in ("ping", {"private": "value"}):
        payload = _payload("repository")
        payload["action"] = action
        response = _post(client, "repository", payload, valid=False)
        assert response.status_code == 401
    first = _post(client, "repository", _payload("repository"), delivery=None)
    second = _post(client, "repository", _payload("repository"), delivery=None)
    assert first.json()["stored"] is second.json()["stored"] is True
    assert first.json()["fact_id"] != second.json()["fact_id"]
    assert all(f.source_event_id is None for f in _facts("repository"))


def _vendor_payload():
    return {
        "provider": "jenkins",
        "run_id": "run-42",
        "repo": "acme/new",
        "branch": "main",
        "commit_sha": "a" * 40,
        "result": "passed",
    }


def _vendor_post(client, payload):
    from sediment_api.config import settings

    return client.post(
        "/ingest/ci",
        json=payload,
        headers={"Authorization": f"Bearer {settings.api_bearer_token}"},
    )


def test_neutral_ci_identity_is_independent_authenticated_assertion(client):
    payload = {**_vendor_payload(), **IDENTITY}
    first = _vendor_post(client, payload)
    assert first.status_code == 200, first.text
    second = _vendor_post(client, payload)
    assert second.json() == {"fact_id": first.json()["fact_id"], "stored": False}
    [fact] = _facts("workflow_run")
    assert fact.provider.value == "jenkins"
    assert fact.repository_provider.value == "github"
    assert fact.repository_id == "186853002"


@pytest.mark.parametrize("missing", list(IDENTITY))
def test_neutral_ci_partial_identity_rejected_at_request_boundary(client, missing):
    payload = {**_vendor_payload(), **IDENTITY}
    payload.pop(missing)
    response = _vendor_post(client, payload)
    assert response.status_code == 422
    assert _facts("workflow_run") == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository_provider", "other"),
        ("repository_host", "https://github.com"),
        ("repository_id", True),
        ("repository_id", "01"),
        ("repository_id", "9" * 21),
    ],
)
def test_neutral_ci_invalid_complete_identity_is_rejected(client, field, value):
    payload = {**_vendor_payload(), **IDENTITY, field: value}
    response = _vendor_post(client, payload)
    assert response.status_code == 422
    assert _facts("workflow_run") == []


def test_neutral_ci_url_does_not_supply_absent_identity(client):
    response = _vendor_post(
        client,
        {**_vendor_payload(), "run_url": "https://github.com/acme/new/actions/runs/42"},
    )
    assert response.status_code == 200
    [fact] = _facts("workflow_run")
    assert (
        fact.repository_provider is fact.repository_host is fact.repository_id is None
    )


@pytest.mark.parametrize(
    "kind", ["push", "workflow_run", "pull_request_merge", "pull_request_revision"]
)
def test_signed_invalid_repository_id_retains_valid_fact(
    client, monkeypatch, kind, caplog
):
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "mirror_path", None)
    payload = _payload(kind)
    payload["repository"]["id"] = {"private": "malformed-id-content"}
    response = _post(client, kind, payload)
    assert response.status_code == 200
    assert response.json()["stored"] is True
    [fact] = _facts(kind)
    assert (
        fact.repository_provider is fact.repository_host is fact.repository_id is None
    )
    losses = [r for r in caplog.records if r.message == "repository_identity_declined"]
    assert len(losses) == 1
    assert losses[0].reason == "repository_identity_invalid"
    assert losses[0].count == 1
    assert "malformed-id-content" not in caplog.text


@pytest.mark.parametrize(
    "host",
    [
        "https://github.com",
        "github.com:443",
        "github.com.",
        "github..com",
        "",
        "github.com/path",
    ],
)
def test_github_host_is_validated_configuration(host):
    from sediment_api.config import Settings, settings

    with pytest.raises(ValidationError):
        Settings(**{**settings.model_dump(), "github_host": host})

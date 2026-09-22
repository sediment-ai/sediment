# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every bearer door enforces the configured credential authority."""

import json

import pytest
from types import SimpleNamespace
from pydantic import SecretStr

from sediment_api.config import Settings
from sediment_api.main import app

INGEST = {"/ingest/gateway", "/ingest/ci", "/v1/logs"}
BEARER_ROUTES = [
    (method, path)
    for path, item in app.openapi()["paths"].items()
    for method, operation in item.items()
    if any(p.get("name") == "authorization" for p in operation.get("parameters", []))
]


@pytest.fixture
def credentials(monkeypatch):
    import sediment_api.deps as deps

    config = SimpleNamespace(
        operator_token=SecretStr("operator-private-secret"),
        ingest_tokens={
            "alice": SecretStr("alice-capture-secret"),
            "bob": SecretStr("bob-capture-secret"),
        },
        api_bearer_token="legacy-capture-secret",
        retrieval_token=None,
        retrieval_session_id=None,
        context_session_ids=(),
    )
    monkeypatch.setattr(deps, "settings", config)
    return config


@pytest.mark.parametrize("route", BEARER_ROUTES, ids=lambda route: route[1])
@pytest.mark.parametrize(
    "token", ["alice-capture-secret", "operator-private-secret", "unknown-token"]
)
def test_every_bearer_route_enforces_authority(client, credentials, route, token):
    response = client.request(
        route[0], route[1], headers={"Authorization": f"Bearer {token}"}, json={}
    )
    if token == "unknown-token":
        assert response.status_code == 401
    elif token == "alice-capture-secret" and route[1] not in INGEST | {"/v1/me"}:
        assert response.status_code == 403
    else:
        assert response.status_code not in {401, 403}


def test_named_clients_revoke_independently_and_legacy_has_no_read_authority(
    client, credentials, monkeypatch
):
    assert (
        client.get(
            "/v1/me", headers={"Authorization": "Bearer alice-capture-secret"}
        ).json()["client_id"]
        == "alice"
    )
    monkeypatch.setattr(
        credentials, "ingest_tokens", {"bob": SecretStr("bob-capture-secret")}
    )
    assert (
        client.get(
            "/v1/me", headers={"Authorization": "Bearer alice-capture-secret"}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/v1/me", headers={"Authorization": "Bearer bob-capture-secret"}
        ).json()["authority"]
        == "ingest"
    )
    assert (
        client.get(
            "/v1/facts", headers={"Authorization": "Bearer legacy-capture-secret"}
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/v1/me", headers={"Authorization": "Bearer operator-private-secret"}
        ).json()["authority"]
        == "operator"
    )


@pytest.mark.parametrize(
    "values",
    [
        {"operator_token": ""},
        {"operator_token": "changeme"},
        {"operator_token": "capture-secret"},
        {"ingest_tokens": {}},
        {"ingest_tokens": {"a": "same-secret", "b": "same-secret"}},
        {"ingest_tokens": {"a": ""}},
        {"ingest_tokens": {"a": "changeme"}},
    ],
)
def test_production_rejects_missing_placeholder_or_overlapping_credentials(values):
    config = Settings(
        _env_file=None,
        org_id="acme",
        database_url="postgresql://user:secret@localhost/db",
        github_webhook_secret="webhook-secret",
        api_bearer_token="",
        operator_token="operator-secret",
        ingest_tokens={"capture": "capture-secret"},
    )
    for name, value in values.items():
        setattr(
            config,
            name,
            SecretStr(value)
            if name == "operator_token"
            else {k: SecretStr(v) for k, v in value.items()},
        )
    assert config.validate_production_security()
    assert "capture-secret" not in str(config)
    assert "operator-secret" not in str(config)


def test_ingest_map_validation_errors_never_echo_secret(monkeypatch):
    monkeypatch.setenv(
        "SEDIMENT_INGEST_TOKENS", json.dumps({"": "sentinel-secret-never-print"})
    )
    with pytest.raises(ValueError) as failure:
        Settings(
            _env_file=None, org_id="acme", database_url="postgresql://localhost/db"
        )
    assert "sentinel-secret-never-print" not in str(failure.value)


@pytest.mark.parametrize(
    "raw",
    [
        '{"a":"hidden-first","a":"hidden-second"}',
        '{"a":"hidden-first",',
        '["hidden-first"]',
    ],
)
def test_malformed_or_duplicate_client_map_has_sanitized_errors(monkeypatch, raw):
    monkeypatch.setenv("SEDIMENT_INGEST_TOKENS", raw)
    with pytest.raises(ValueError) as failure:
        Settings(
            _env_file=None, org_id="acme", database_url="postgresql://localhost/db"
        )
    assert "hidden-first" not in str(failure.value)
    assert "hidden-second" not in str(failure.value)


def test_production_lifespan_rejects_bootstrap_database_identity(
    postgres_database_url, monkeypatch
):
    from fastapi.testclient import TestClient
    from sediment_core.postgres_roles import DatabasePrivilegeError
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "database_url", SecretStr(postgres_database_url))
    monkeypatch.setattr(settings, "dev_mode", False)
    with pytest.raises(DatabasePrivilegeError, match="sediment_runtime"):
        with TestClient(app):
            pytest.fail("privileged database identity reached serving state")


@pytest.mark.parametrize("token", ["bad\x7fsecret", "bad\ud800secret", "badésecret"])
@pytest.mark.parametrize(
    "field", ["operator_token", "ingest_tokens", "api_bearer_token"]
)
def test_production_bearer_credentials_must_fit_http_headers(field, token):
    values = {
        "org_id": "acme",
        "database_url": "postgresql://localhost/db",
        "operator_token": "operator-secret",
        "ingest_tokens": {"capture": "capture-secret"},
        "api_bearer_token": "",
        "github_webhook_secret": "webhook-secret",
    }
    values[field] = {"capture": token} if field == "ingest_tokens" else token
    config = Settings(_env_file=None, **values)
    assert config.validate_production_security()

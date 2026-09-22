# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fixed Session credential cannot acquire ingest or operator authority."""

import pytest
from pydantic import SecretStr, ValidationError

from sediment_api.config import Settings, settings
from sediment_api.main import app

TOKEN = "retrieval-test-token-long-enough"


def configured(**values):
    options = dict(
        _env_file=None,
        org_id="acme",
        database_url="postgresql://localhost/db",
        operator_token="operator-test-token-long-enough",
        ingest_tokens={"capture": "capture-test-token-long-enough"},
        api_bearer_token="legacy-test-token-long-enough",
        github_webhook_secret="webhook-test-token-long-enough",
        retrieval_token=TOKEN,
        retrieval_session_id="source-session",
        dev_mode=True,
    )
    options.update(values)
    return Settings(**options)


@pytest.mark.parametrize("dev_mode", [True, False])
@pytest.mark.parametrize(
    "values",
    [
        {"retrieval_token": None},
        {"retrieval_session_id": None},
        {"retrieval_session_id": ""},
        {"retrieval_token": ""},
        {"retrieval_token": "changeme"},
        {"retrieval_token": "short"},
        {"retrieval_token": "x" * 24 + "\t" + "y"},
        {"retrieval_token": "x" * 24 + "é"},
        {"retrieval_token": "x" * 24 + "\ud800"},
        {"retrieval_token": "operator-test-token-long-enough"},
        {"retrieval_token": "capture-test-token-long-enough"},
        {"retrieval_token": "legacy-test-token-long-enough"},
        {"retrieval_token": "webhook-test-token-long-enough"},
    ],
)
def test_invalid_retrieval_configuration_fails_even_in_dev_mode(values, dev_mode):
    with pytest.raises(ValidationError) as failure:
        configured(dev_mode=dev_mode, **values)
    assert TOKEN not in str(failure.value)
    assert "test-token-long-enough" not in str(failure.value)


def test_retrieval_settings_are_optional_and_secret_bearing():
    config = configured()
    assert config.retrieval_token.get_secret_value() == TOKEN
    assert TOKEN not in repr(config)
    assert config.retrieval_session_id == "source-session"
    disabled = configured(retrieval_token=None, retrieval_session_id=None)
    assert disabled.retrieval_token is None
    assert disabled.retrieval_session_id is None


def test_retrieval_is_reserved_ingest_client_even_when_disabled():
    with pytest.raises(ValidationError, match="reserved client"):
        configured(
            retrieval_token=None,
            retrieval_session_id=None,
            ingest_tokens={"retrieval": "capture-test-token-long-enough"},
        )


@pytest.fixture
def retrieval_credential(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "retrieval_session_id", "source-session")


BEARER_ROUTES = [
    (method, path)
    for path, item in app.openapi()["paths"].items()
    for method, operation in item.items()
    if any(p.get("name") == "authorization" for p in operation.get("parameters", []))
    and path
    not in {
        "/v1/me",
        "/query/context",
        "/query/context/discover",
        "/query/context/selected",
        "/query/context/evidence",
        "/query/context/evidence/manifest",
        "/query/context/evidence/read",
    }
]


@pytest.mark.parametrize("route", BEARER_ROUTES, ids=lambda route: route[1])
@pytest.mark.parametrize("plural", [False, True])
def test_retrieval_cannot_use_existing_bearer_routes(
    client, retrieval_credential, route, plural, monkeypatch
):
    if plural:
        monkeypatch.setattr(settings, "retrieval_session_id", None)
        monkeypatch.setattr(
            settings, "retrieval_session_ids", ["source-session", "other"]
        )
    response = client.request(
        route[0], route[1], headers={"Authorization": f"Bearer {TOKEN}"}, json={}
    )
    assert response.status_code == 403
    assert TOKEN not in response.text
    assert "source-session" not in response.text


def test_identity_reports_fixed_source_only_for_retrieval(client, retrieval_credential):
    response = client.get("/v1/me", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json() == {
        "org_id": settings.org_id,
        "version": __import__("sediment_api").__version__,
        "authority": "retrieval",
        "client_id": "retrieval",
        "source_session_id": "source-session",
    }
    operator = client.get(
        "/v1/me",
        headers={
            "Authorization": f"Bearer {settings.operator_token.get_secret_value()}"
        },
    )
    assert "source_session_id" not in operator.json()


def test_disabled_retrieval_credential_is_unknown(client):
    response = client.get("/v1/me", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 401

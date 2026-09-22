# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded Session grants preserve credential and singleton contracts."""

import json

import pytest
from pydantic import SecretStr, ValidationError

from sediment_api.config import settings
from sediment_api.workers import _child_environment

from test_retrieval_authority import TOKEN, configured


def plural(**values):
    return configured(
        retrieval_session_id=None,
        retrieval_session_ids=values.pop(
            "retrieval_session_ids", ["second", " first "]
        ),
        **values,
    )


def test_plural_grant_normalizes_and_preserves_bounded_set():
    config = plural()
    assert config.retrieval_session_ids == ["second", "first"]
    assert config.context_session_ids == ("first", "second")
    assert configured().context_session_ids == ("source-session",)
    assert (
        configured(retrieval_token=None, retrieval_session_id=None).context_session_ids
        == ()
    )
    assert (
        len(
            plural(
                retrieval_session_ids=[f"s-{i}" for i in range(32)]
            ).context_session_ids
        )
        == 32
    )


@pytest.mark.parametrize(
    "value",
    [
        [],
        ["same", " same "],
        [""],
        ["  "],
        [None],
        [1],
        [True],
        ["bad\x00id"],
        ["bad\ud800id"],
        [f"s-{i}" for i in range(33)],
        {},
        "null",
        "[bad-json",
        '["sentinel-private-id"]' + " " * 16384,
        ["sentinel-private-id" * 1000],
    ],
)
def test_invalid_plural_grant_fails_without_echo(value):
    with pytest.raises(ValidationError) as failure:
        plural(retrieval_session_ids=value)
    assert "sentinel-private-id" not in str(failure.value)
    assert TOKEN not in str(failure.value)


@pytest.mark.parametrize(
    "values",
    [
        {"retrieval_session_ids": ["other"]},
        {
            "retrieval_session_id": None,
            "retrieval_session_ids": ["other"],
            "retrieval_token": None,
        },
        {"retrieval_session_id": None, "retrieval_session_ids": None},
    ],
)
def test_grant_requires_exactly_one_source_setting_and_token(values):
    with pytest.raises(ValidationError):
        configured(**values)


def test_plural_json_environment_limit_precedes_decode(monkeypatch):
    monkeypatch.setenv("SEDIMENT_RETRIEVAL_SESSION_IDS", '[" padded "]')
    assert configured(retrieval_session_id=None).context_session_ids == ("padded",)
    exact = '["' + "x" * (16384 - 4) + '"]'
    assert len(exact.encode()) == 16384
    assert len(plural(retrieval_session_ids=exact).context_session_ids) == 1
    with pytest.raises(ValidationError):
        plural(retrieval_session_ids=exact + " ")
    # Bound UTF-8 bytes, including whitespace the JSON decoder would discard.
    with pytest.raises(ValidationError):
        plural(retrieval_session_ids='["' + "é" * 8191 + '"]')


@pytest.mark.parametrize(
    "token",
    [
        "short",
        "operator-test-token-long-enough",
        "capture-test-token-long-enough",
        "legacy-test-token-long-enough",
        "webhook-test-token-long-enough",
    ],
)
@pytest.mark.parametrize("dev_mode", [True, False])
def test_plural_retains_strong_distinct_retrieval_secret(token, dev_mode):
    with pytest.raises(ValidationError):
        plural(retrieval_token=token, dev_mode=dev_mode)


@pytest.fixture
def plural_credential(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "retrieval_session_id", None)
    monkeypatch.setattr(settings, "retrieval_session_ids", ["second", "first"])


def test_plural_identity_is_sorted_without_singleton_default(client, plural_credential):
    response = client.get("/v1/me", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json() == {
        "org_id": settings.org_id,
        "version": __import__("sediment_api").__version__,
        "authority": "retrieval",
        "client_id": "retrieval",
        "source_session_ids": ["first", "second"],
    }


@pytest.mark.parametrize("ids", [["first"], ["second", "first"]])
def test_plural_never_gives_legacy_route_a_default(
    client, plural_credential, monkeypatch, ids
):
    monkeypatch.setattr(settings, "retrieval_session_ids", ids)
    for token in (TOKEN, settings.operator_token.get_secret_value()):
        response = client.post(
            "/query/context",
            headers={"Authorization": f"Bearer {token}"},
            json={"schema_version": 1, "query": "goal"},
        )
        assert response.status_code == 404


@pytest.mark.parametrize("mode", ["plural", "single", "disabled"])
def test_child_grant_uses_validated_parent_not_inherited_environment(monkeypatch, mode):
    monkeypatch.setenv("SEDIMENT_RETRIEVAL_SESSION_IDS", '["ambient-untrusted"]')
    monkeypatch.setenv("SEDIMENT_RETRIEVAL_SESSION_ID", "ambient-untrusted")
    monkeypatch.setenv("SEDIMENT_RETRIEVAL_TOKEN", "ambient-untrusted")
    monkeypatch.setattr(
        settings, "retrieval_token", None if mode == "disabled" else SecretStr(TOKEN)
    )
    monkeypatch.setattr(
        settings, "retrieval_session_id", "single" if mode == "single" else None
    )
    monkeypatch.setattr(
        settings,
        "retrieval_session_ids",
        ["second", "first"] if mode == "plural" else None,
    )
    env = _child_environment()
    assert "ambient-untrusted" not in repr(
        {k: v for k, v in env.items() if k.startswith("SEDIMENT_RETRIEVAL_")}
    )
    if mode == "plural":
        assert json.loads(env["SEDIMENT_RETRIEVAL_SESSION_IDS"]) == ["second", "first"]
        assert "SEDIMENT_RETRIEVAL_SESSION_ID" not in env
    else:
        assert "SEDIMENT_RETRIEVAL_SESSION_IDS" not in env


def test_child_keeps_valid_utf8_grant_inside_setting_bound(monkeypatch):
    ids = ["é" * 8000]
    config = plural(retrieval_session_ids=ids)
    monkeypatch.setattr(settings, "retrieval_token", config.retrieval_token)
    monkeypatch.setattr(settings, "retrieval_session_id", None)
    monkeypatch.setattr(settings, "retrieval_session_ids", config.retrieval_session_ids)
    serialized = _child_environment()["SEDIMENT_RETRIEVAL_SESSION_IDS"]
    assert plural(retrieval_session_ids=serialized).context_session_ids == tuple(ids)

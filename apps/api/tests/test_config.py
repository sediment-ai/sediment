# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings: org binding at construction + the fail-closed startup
posture.

Exercises ``Settings`` directly with explicit kwargs — never via the app
import, which would raise at module load under bad settings — so no test
depends on ambient env beyond what conftest pins.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sediment_api.config import Settings

STRONG = {
    "api_bearer_token": "s3cret-rotated-token-9f21",
    "github_webhook_secret": "another-strong-secret-4b7e",
    "database_url": (
        "postgresql+psycopg://sentinel-user:sentinel-password@localhost/sediment"
    ),
}


def test_org_id_is_normalized_at_construction() -> None:
    assert Settings(org_id="Acme", dev_mode=True).org_id == "acme"


def test_case_variant_configs_converge_on_one_tenant() -> None:
    a = Settings(org_id="ACME", dev_mode=True)
    b = Settings(org_id="acme", dev_mode=True)
    assert a.org_id == b.org_id


# One accept + one reject prove the field_validator is wired to
# normalize_org_id; the full accept/reject table is core's contract and
# lives in packages/core/tests/test_org.py.
@pytest.mark.parametrize("bad", ["", "../evil"])
def test_invalid_org_id_fails_the_boot(bad: str) -> None:
    with pytest.raises(ValidationError):
        Settings(org_id=bad, dev_mode=True)


def test_missing_org_id_fails_the_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEDIMENT_ORG_ID", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, dev_mode=True, **STRONG)


def test_missing_database_url_fails_the_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEDIMENT_DATABASE_URL", raising=False)
    values = {key: value for key, value in STRONG.items() if key != "database_url"}

    with pytest.raises(ValidationError):
        Settings(_env_file=None, org_id="acme", dev_mode=True, **values)


def test_database_url_is_secret_bearing() -> None:
    settings = Settings(org_id="acme", dev_mode=True, **STRONG)

    assert settings.database_url.get_secret_value().endswith("/sediment")
    assert "sentinel-user" not in repr(settings)
    assert "sentinel-password" not in repr(settings)


@pytest.mark.parametrize("suffix", ["\n", "\r\n", "\n\n"])
def test_database_url_strips_trailing_newline(suffix: str) -> None:
    # A mounted/piped secret often arrives with a trailing newline (the
    # shape Kubernetes envFrom / shell command substitution produce); the
    # URL consumed by lifespan must not carry it into the engine.
    settings = Settings(
        org_id="acme", dev_mode=True, database_url=STRONG["database_url"] + suffix
    )
    assert settings.database_url.get_secret_value() == STRONG["database_url"]


@pytest.mark.parametrize("bad", ["", "\n", "\r\n", "\n\n"])
def test_empty_or_newline_only_database_url_fails_the_boot(bad: str) -> None:
    # Fail fast at construction with a named-setting message instead of
    # after a ~30 s retry loop in lifespan against a newline-corrupted
    # target. "" is the cli.py falsiness arm; the newline-only values are
    # the trailing-newline arm trimmed to nothing by rstrip("\r\n").
    with pytest.raises(ValidationError):
        Settings(org_id="acme", dev_mode=True, database_url=bad)


def test_database_url_rejection_message_names_setting_not_value() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(org_id="acme", dev_mode=True, database_url="\n")
    blob = str(exc_info.value)
    # The boot must name the offending setting so an operator knows what
    # to fix. The rejected input is always empty/newline-only (a non-empty
    # URL passes the strip), so the message never echoes a credential;
    # pin that the static message names the setting and its env var.
    assert "database_url" in blob
    assert "SEDIMENT_DATABASE_URL" in blob


def test_strong_database_url_with_trailing_newline_passes_production() -> None:
    # The trailing-newline arm used to fail slowly in lifespan; after the
    # strip it constructs and passes the production security gate, so a
    # real secret sourced from a mounted file boots the app.
    settings = Settings(
        org_id="acme",
        dev_mode=False,
        database_url=STRONG["database_url"] + "\n",
        api_bearer_token="s3cret-rotated-token-9f21",
        github_webhook_secret="another-strong-secret-4b7e",
    )
    assert settings.validate_production_security() == []
    assert settings.database_url.get_secret_value() == STRONG["database_url"]


def test_default_token_is_flagged_in_production() -> None:
    settings = Settings(
        org_id="acme",
        dev_mode=False,
        api_bearer_token="changeme",
        github_webhook_secret="another-strong-secret-4b7e",
    )
    problems = settings.validate_production_security()
    assert any("api_bearer_token" in p for p in problems)


def test_empty_webhook_secret_is_flagged_in_production() -> None:
    settings = Settings(
        org_id="acme",
        dev_mode=False,
        api_bearer_token="s3cret-rotated-token-9f21",
        github_webhook_secret="",
    )
    problems = settings.validate_production_security()
    assert any("github_webhook_secret" in p for p in problems)


def test_all_strong_secrets_pass_in_production() -> None:
    settings = Settings(org_id="acme", dev_mode=False, **STRONG)
    assert settings.validate_production_security() == []


def test_short_operator_token_is_flagged_in_production() -> None:
    # ADR 0018: production rejects an operator token under the length floor
    # even when it is otherwise printable ASCII and not a placeholder.
    settings = Settings(
        org_id="acme",
        dev_mode=False,
        operator_token="a",
        api_bearer_token="s3cret-rotated-token-9f21",
        github_webhook_secret="another-strong-secret-4b7e",
    )
    problems = settings.validate_production_security()
    assert any("operator_token" in p and "24" in p for p in problems)


def test_short_ingest_secret_is_flagged_in_production() -> None:
    settings = Settings(
        org_id="acme",
        dev_mode=False,
        operator_token="operator-token-well-over-24-chars",
        api_bearer_token="b",
        github_webhook_secret="another-strong-secret-4b7e",
    )
    problems = settings.validate_production_security()
    assert any("ingest_tokens" in p and "24" in p for p in problems)


def test_short_webhook_secret_is_flagged_in_production() -> None:
    settings = Settings(
        org_id="acme",
        dev_mode=False,
        operator_token="operator-token-well-over-24-chars",
        api_bearer_token="s3cret-rotated-token-9f21",
        github_webhook_secret="c",
    )
    problems = settings.validate_production_security()
    assert any("github_webhook_secret" in p and "24" in p for p in problems)


def test_secret_exactly_at_the_length_floor_passes_in_production() -> None:
    # 24 characters exactly: the floor is a minimum, not an exclusive bound.
    exact = "x" * 24
    settings = Settings(
        org_id="acme",
        dev_mode=False,
        operator_token=exact,
        api_bearer_token="y" * 24,
        github_webhook_secret="z" * 24,
    )
    assert settings.validate_production_security() == []


def test_dev_mode_skips_secret_checks() -> None:
    settings = Settings(
        org_id="acme",
        dev_mode=True,
        api_bearer_token="changeme",
        github_webhook_secret="changeme",
    )
    assert settings.validate_production_security() == []


def test_secrets_are_stripped_at_construction() -> None:
    # A mounted/piped secret with a trailing newline must match what
    # clients actually send.
    settings = Settings(
        org_id="acme",
        dev_mode=False,
        api_bearer_token="s3cret-rotated-token-9f21\n",
        github_webhook_secret="  another-strong-secret-4b7e  ",
    )
    assert settings.api_bearer_token == "s3cret-rotated-token-9f21"
    assert settings.github_webhook_secret == "another-strong-secret-4b7e"
    assert settings.validate_production_security() == []


def test_whitespace_only_secret_is_flagged_in_production() -> None:
    settings = Settings(
        org_id="acme",
        dev_mode=False,
        api_bearer_token="   \n",
        github_webhook_secret="another-strong-secret-4b7e",
    )
    problems = settings.validate_production_security()
    assert any("api_bearer_token" in p for p in problems)


def test_open_mirror_allowlist_warns_in_production() -> None:
    # Mirror mode on, empty allowlist, production → one advisory warning.
    settings = Settings(
        org_id="acme", dev_mode=False, mirror_path="/srv/mirrors", **STRONG
    )
    warnings = settings.config_warnings()
    assert any("SEDIMENT_ALLOWED_CLONE_HOSTS" in w for w in warnings)


def test_mirror_with_allowlist_does_not_warn() -> None:
    settings = Settings(
        org_id="acme",
        dev_mode=False,
        mirror_path="/srv/mirrors",
        allowed_clone_hosts=["github.com"],
        **STRONG,
    )
    assert settings.config_warnings() == []


def test_no_mirror_path_does_not_warn() -> None:
    settings = Settings(org_id="acme", dev_mode=False, **STRONG)
    assert settings.config_warnings() == []


def test_dev_mode_skips_mirror_warning() -> None:
    # Dev opts out: the fixture file:// remotes need the open posture.
    settings = Settings(org_id="acme", dev_mode=True, mirror_path="/srv/mirrors")
    assert settings.config_warnings() == []


def test_problem_messages_never_echo_secret_values() -> None:
    settings = Settings(
        org_id="acme",
        dev_mode=False,
        api_bearer_token="changeme",
        github_webhook_secret="changeme",
    )
    blob = " ".join(settings.validate_production_security())
    # Each offending setting must be named so the operator knows what to
    # fix — but the configured *value* must never be echoed. "changeme" is
    # the only flaggable token value, so it doubles as the leak probe.
    assert "api_bearer_token" in blob
    assert "github_webhook_secret" in blob
    assert "changeme" not in blob

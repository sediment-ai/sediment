# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deployment secrets stay private from creation and never replace existing data."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def module():
    spec = importlib.util.spec_from_file_location(
        "create_deploy_env", ROOT / "scripts/create_deploy_env.py"
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_secrets_are_private_distinct_and_capture_authority_is_named(
    tmp_path, monkeypatch
):
    creator = module()
    path = tmp_path / ".env"
    original = os.fdopen

    def checked_fdopen(fd, *args, **kwargs):
        assert os.fstat(fd).st_mode & 0o777 == 0o600
        assert os.fstat(fd).st_size == 0
        return original(fd, *args, **kwargs)

    monkeypatch.setattr(creator.os, "fdopen", checked_fdopen)
    old = os.umask(0)
    try:
        creator.create_environment(path)
    finally:
        os.umask(old)
    assert path.stat().st_mode & 0o777 == 0o600
    values = dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )
    names = [
        "POSTGRES_PASSWORD",
        "SEDIMENT_MIGRATOR_PASSWORD",
        "SEDIMENT_RUNTIME_PASSWORD",
        "SEDIMENT_OPERATOR_PASSWORD",
        "SEDIMENT_OPERATOR_TOKEN",
        "SEDIMENT_GATEWAY_INGEST_TOKEN",
        "SEDIMENT_GITHUB_WEBHOOK_SECRET",
        "LITELLM_MASTER_KEY",
    ]
    secrets = [values[name] for name in names]
    assert len(set(secrets)) == len(secrets)
    assert all(len(secret) >= 64 for secret in secrets)
    assert json.loads(values["SEDIMENT_INGEST_TOKENS"]) == {
        "gateway": values["SEDIMENT_GATEWAY_INGEST_TOKEN"]
    }
    assert values["SEDIMENT_OPERATOR_TOKEN"] not in values["SEDIMENT_INGEST_TOKENS"]
    assert values["SEDIMENT_API_BEARER_TOKEN"] == ""


def test_existing_file_and_symlink_are_never_overwritten(tmp_path):
    creator = module()
    target = tmp_path / "existing"
    target.write_text("retained")
    with pytest.raises(FileExistsError):
        creator.create_environment(target)
    link = tmp_path / ".env"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        creator.create_environment(link)
    assert target.read_text() == "retained"


def test_shared_writable_parent_is_rejected(tmp_path):
    creator = module()
    tmp_path.chmod(0o777)
    try:
        with pytest.raises(PermissionError, match="chmod go-w"):
            creator.create_environment(tmp_path / ".env")
        assert not (tmp_path / ".env").exists()
    finally:
        tmp_path.chmod(0o700)


def test_named_clients_can_enroll_from_a_generated_environment(tmp_path):
    path = tmp_path / ".env"
    result = subprocess.run(
        [
            sys.executable,
            "-S",  # Deployment generation must work without workspace dependencies.
            str(ROOT / "scripts/create_deploy_env.py"),
            "--output",
            str(path),
            "--ingest-client",
            "alice-laptop",
            "--ingest-client",
            "bob.desktop",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    values = dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )
    clients = json.loads(values["SEDIMENT_INGEST_TOKENS"])
    assert set(clients) == {"gateway", "alice-laptop", "bob.desktop"}
    tokens = list(clients.values())
    assert len(set(tokens)) == 3
    assert all(len(token) == 64 for token in tokens)
    assert values["SEDIMENT_OPERATOR_TOKEN"] not in tokens
    assert all(token not in result.stdout + result.stderr for token in tokens)
    assert path.stat().st_mode & 0o777 == 0o600
    # Import the server's eager Settings singleton in a separate process so
    # this generated deployment cannot become another test's configuration.
    validation = subprocess.run(
        [
            sys.executable,
            "-c",
            "from sediment_api.config import settings; "
            "settings.validate_production_security()",
        ],
        cwd=tmp_path,
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("SEDIMENT_")},
            "SEDIMENT_DATABASE_URL": "postgresql+psycopg://runtime@localhost/sediment",
        },
        capture_output=True,
        text=True,
    )
    assert validation.returncode == 0, validation.stderr


@pytest.mark.parametrize(
    "clients",
    [
        ("operator",),
        ("legacy",),
        ("retrieval",),
        ("gateway",),
        ("alice", "alice"),
        ("",),
        ("a" * 65,),
        ("alice\nSEDIMENT_DEV_MODE=true",),
        ("${TOKEN}",),
    ],
)
def test_invalid_client_names_leave_no_environment_file(tmp_path, clients):
    path = tmp_path / ".env"
    with pytest.raises(ValueError, match="client"):
        module().create_environment(path, ingest_clients=clients)
    assert not path.exists()


def test_pilot_environment_selects_complete_stack_and_records_source(tmp_path):
    path = tmp_path / ".env"
    module().create_environment(
        path,
        domain="sediment.example.com",
        email="pilot+ops@example.com",
        provider_key="sk-ant-test-only-12345678901234567890",
        ingest_clients=("alice-laptop",),
    )
    values = dotenv_values(path)
    assert set(json.loads(values["SEDIMENT_INGEST_TOKENS"])) == {
        "gateway",
        "alice-laptop",
    }
    assert values["COMPOSE_PROFILES"] == "https"
    assert values["SEDIMENT_DOMAIN"] == "sediment.example.com"
    assert values["SEDIMENT_ACME_EMAIL"] == "pilot+ops@example.com"
    assert values["ANTHROPIC_API_KEY"] == "sk-ant-test-only-12345678901234567890"
    assert len(values["SEDIMENT_SOURCE_REVISION"]) == 40
    assert len(values["SEDIMENT_SOURCE_DIGEST"]) == 64
    assert values["SEDIMENT_DELIVERY_DIR"] == ""
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "domain",
    [
        "localhost",
        "127.0.0.1",
        "https://sediment.example.com",
        "sediment.example.com/path",
        "*.example.com",
        "bad_name.example.com",
        "-bad.example.com",
        "example.com:443",
        "example.com\nOTHER=value",
        "{{env}}.example.com",
        "example.local",
        "a" * 64 + ".com",
    ],
)
def test_pilot_rejects_invalid_domain_without_creating_file(tmp_path, domain):
    path = tmp_path / ".env"
    with pytest.raises(ValueError, match="hostname"):
        module().create_environment(
            path,
            domain=domain,
            email="ops@example.com",
            provider_key="sk-ant-test-only-12345678901234567890",
        )
    assert not path.exists()


@pytest.mark.parametrize(
    ("email", "provider_key"),
    [
        ("", "sk-ant-test-only-12345678901234567890"),
        ("ops@example.com\nOTHER=value", "sk-ant-test-only-12345678901234567890"),
        ("ops@example.com", ""),
        ("ops@example.com", "secret\nOTHER=value"),
        ("ops@example.com", "sk-ant-${EXPANDED}-12345678901234567890"),
    ],
)
def test_pilot_rejects_invalid_inputs_without_exposing_them(
    tmp_path, email, provider_key
):
    with pytest.raises(ValueError) as exc:
        module().create_environment(
            tmp_path / ".env",
            domain="sediment.example.com",
            email=email,
            provider_key=provider_key,
        )
    assert "OTHER=value" not in str(exc.value)
    if provider_key:
        assert provider_key not in str(exc.value)
    assert not (tmp_path / ".env").exists()


def test_pilot_noninteractive_setup_does_not_echo_a_secret(tmp_path):
    secret = "sk-ant-noninteractive-12345678901234567890"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/create_deploy_env.py"),
            "--domain",
            "sediment.example.com",
            "--output",
            str(tmp_path / ".env"),
        ],
        input="",
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ANTHROPIC_API_KEY": secret,
            "SEDIMENT_ACME_EMAIL": "ops@example.com",
        },
    )
    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout + result.stderr
    assert "https://sediment.example.com/health" in result.stdout
    assert "docker compose --env-file " in result.stdout
    assert " up -d --build --wait" in result.stdout

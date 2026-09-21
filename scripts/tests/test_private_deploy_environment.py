# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deployment secrets stay private from creation and never replace existing data."""

import importlib.util
import json
import os
from pathlib import Path

import pytest

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
        with pytest.raises(PermissionError):
            creator.create_environment(tmp_path / ".env")
        assert not (tmp_path / ".env").exists()
    finally:
        tmp_path.chmod(0o700)

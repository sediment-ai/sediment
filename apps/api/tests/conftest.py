# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Environment before app import: ``settings`` is a module singleton constructed
when ``sediment_api.config`` is imported, so org/secrets must be in the env at
collection time. setdefault, so an explicit env override wins. The secrets
are real-looking on purpose — the suite runs the fail-closed production
posture, not dev mode.
"""

from __future__ import annotations

import os

os.environ.setdefault("SEDIMENT_ORG_ID", "testorg")
os.environ.setdefault("SEDIMENT_API_BEARER_TOKEN", "test-ingest-token-3a7e-9d21")
os.environ.setdefault("SEDIMENT_OPERATOR_TOKEN", "test-operator-token-3a7e-2f6c")
os.environ.setdefault("SEDIMENT_GITHUB_WEBHOOK_SECRET", "test-webhook-secret-91bd-7c3a")
os.environ.setdefault(
    "SEDIMENT_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres",
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import SecretStr  # noqa: E402


@pytest.fixture()
def client(postgres_database_url, postgres_engine, monkeypatch):
    """Run the app against the worker's migrated PostgreSQL database."""
    from sediment_api.config import settings
    from sediment_api.main import app

    monkeypatch.setattr(settings, "database_url", SecretStr(postgres_database_url))
    # Shared store fixtures own schemas; production identity is tested separately.
    monkeypatch.setattr(settings, "dev_mode", True)
    with TestClient(app) as test_client:
        yield test_client

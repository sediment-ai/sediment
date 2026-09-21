# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Environment before app import (same contract as apps/api/tests):
``settings`` is a module singleton constructed when ``sediment_api.config``
is imported, so org/secrets must be in the env at collection time.
setdefault, so an explicit env override wins. The remote-verb tests drive
the real app in process via the ``client`` fixture.
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

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

# The attribution-seeding test reuses derive's git fixture helpers
# (`gitfixtures`, the documented cross-package fixture pattern). Full-suite
# runs get the path from pytest's collection order; this keeps a standalone
# `uv run pytest cli` working too.
sys.path.insert(0, str(Path(__file__).parents[2] / "packages" / "derive" / "tests"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import SecretStr  # noqa: E402


def pytest_addoption(parser):
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="rewrite cli/tests/testdata/help/*.txt from current --help output",
    )


@pytest.fixture()
def client(postgres_database_url, postgres_engine, monkeypatch):
    """Run the API against the worker's migrated PostgreSQL database."""
    from sediment_api.config import settings
    from sediment_api.main import app

    monkeypatch.setattr(settings, "database_url", SecretStr(postgres_database_url))
    monkeypatch.setattr(settings, "dev_mode", True)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def app_transport(tmp_path, client, monkeypatch):
    """Point the client seam at the in-process app; config lands in tmp.

    The seam's ``_http()`` returns a fresh ``httpx.Client`` per call; here it
    returns the in-process ``TestClient`` (an ``httpx.Client`` subclass) so
    the remote verbs exercise the real app without a socket."""
    import sediment_cli.client as api_client

    monkeypatch.setattr(api_client, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(api_client, "_http", lambda: client)
    monkeypatch.delenv("SEDIMENT_SESSION_TOKEN", raising=False)
    return tmp_path


@pytest.fixture()
def remote(app_transport, monkeypatch):
    """app_transport plus CI-style env credentials and remote ``facts``."""
    monkeypatch.setenv("SEDIMENT_URL", "https://testserver")
    monkeypatch.setenv("SEDIMENT_SESSION_TOKEN", "test-operator-token-3a7e-2f6c")
    monkeypatch.delenv("SEDIMENT_DATABASE_URL", raising=False)
    return app_transport

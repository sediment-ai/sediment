# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import make_url

import pytest

from sediment_api.config import settings
from sediment_api.main import app
from sediment_core import FactStore
from sediment_core.postgres_engine import DatabaseOperationError


def test_api_lifespan_owns_one_postgres_engine_and_disposes_it(
    postgres_database_url: str,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "database_url", SecretStr(postgres_database_url))
    monkeypatch.setattr(settings, "dev_mode", True)
    closed_connections = 0

    with TestClient(app) as client:
        engine = app.state.database_engine
        store = app.state.fact_store
        pool = engine.pool

        def record_close(*_args) -> None:
            nonlocal closed_connections
            closed_connections += 1

        event.listen(pool, "close", record_close)
        assert isinstance(store, FactStore)
        assert store._engine is engine
        assert client.get("/health").status_code == 200

    assert pool.checkedout() == 0
    assert closed_connections >= 1


def test_api_rejects_an_absent_schema_without_mutating_it(
    postgres_database_factory,
    monkeypatch,
) -> None:
    database_url = postgres_database_factory(migrated=False)
    monkeypatch.setattr(settings, "database_url", SecretStr(database_url))

    with pytest.raises(DatabaseOperationError, match="found absent"):
        with TestClient(app):
            pass

    engine = create_engine(database_url)
    try:
        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


def test_api_rejects_a_behind_schema(
    postgres_database_factory,
    monkeypatch,
) -> None:
    database_url = postgres_database_factory(migrated=False)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            )
    finally:
        engine.dispose()
    monkeypatch.setattr(settings, "database_url", SecretStr(database_url))

    with pytest.raises(DatabaseOperationError, match="found behind"):
        with TestClient(app):
            pass


def test_api_rejects_an_ahead_schema(
    postgres_database_factory,
    monkeypatch,
) -> None:
    database_url = postgres_database_factory(migrated=False)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO alembic_version (version_num) VALUES ('future_9999')"
            )
    finally:
        engine.dispose()
    monkeypatch.setattr(settings, "database_url", SecretStr(database_url))

    with pytest.raises(DatabaseOperationError, match="found ahead"):
        with TestClient(app):
            pass


def test_api_rejects_an_unreachable_database_without_leaking_credentials(
    monkeypatch,
) -> None:
    import sediment_core.postgres_engine as postgres_engine_module

    database_url = (
        "postgresql+psycopg://sentinel-user:sentinel-password@127.0.0.1:1/"
        "sediment?token=sentinel-query"
    )
    monkeypatch.setattr(settings, "database_url", SecretStr(database_url))
    # The bounded startup wait retries an unreachable database; keep the
    # test's window short while proving the retry path is the one that fails.
    monkeypatch.setattr(postgres_engine_module, "STARTUP_WAIT_DEADLINE_SECONDS", 0.3)
    monkeypatch.setattr(postgres_engine_module, "STARTUP_WAIT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(
        postgres_engine_module, "DATABASE_CONNECT_TIMEOUT_SECONDS", 0.05
    )

    with pytest.raises(DatabaseOperationError) as captured:
        with TestClient(app):
            pass

    rendered = str(captured.value)
    assert rendered == "connect to database failed for 127.0.0.1:1/sediment"
    for sentinel in (
        "sentinel-user",
        "sentinel-password",
        "sentinel-query",
        "psycopg",
    ):
        assert sentinel not in rendered


def test_api_invalid_url_preserves_safe_password_escape_hint(monkeypatch) -> None:
    database_url = "postgresql://user:sentinel-p@ss!word@localhost/sediment"
    monkeypatch.setattr(settings, "database_url", SecretStr(database_url))

    with pytest.raises(ValueError) as captured:
        with TestClient(app):
            pass

    rendered = str(captured.value)
    assert rendered == (
        "invalid PostgreSQL URL; if the password contains special "
        "characters, percent-encode them"
    )
    assert "sentinel-p" not in rendered
    assert "ss!word" not in rendered


def test_api_rejects_unauthorized_database_credentials_without_leaking_them(
    postgres_database_url: str,
    monkeypatch,
) -> None:
    database_url = (
        make_url(postgres_database_url)
        .set(
            username="sentinel-unauthorized-user",
            password="sentinel-unauthorized-password",
        )
        .render_as_string(hide_password=False)
    )
    monkeypatch.setattr(settings, "database_url", SecretStr(database_url))
    import sediment_core.postgres_engine as postgres_engine_module

    monkeypatch.setattr(postgres_engine_module, "STARTUP_WAIT_DEADLINE_SECONDS", 0.3)
    monkeypatch.setattr(postgres_engine_module, "STARTUP_WAIT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(
        postgres_engine_module, "DATABASE_CONNECT_TIMEOUT_SECONDS", 0.05
    )

    with pytest.raises(DatabaseOperationError) as captured:
        with TestClient(app):
            pass

    rendered = str(captured.value)
    assert rendered.startswith("connect to database failed for ")
    assert "sentinel-unauthorized-user" not in rendered
    assert "sentinel-unauthorized-password" not in rendered


def test_api_disposes_its_engine_when_schema_verification_fails(
    postgres_database_factory,
    monkeypatch,
) -> None:
    from sediment_api import main as main_module

    database_url = postgres_database_factory(migrated=False)
    engine = create_engine(database_url)
    monkeypatch.setattr(settings, "database_url", SecretStr(database_url))
    monkeypatch.setattr(main_module, "create_postgres_engine", lambda _url, **_: engine)

    with pytest.raises(DatabaseOperationError):
        with TestClient(app):
            pass

    assert engine.pool.checkedout() == 0

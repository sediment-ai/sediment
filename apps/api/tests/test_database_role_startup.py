# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production startup against operator-named roles provisioned without Sediment."""

from __future__ import annotations

from uuid import uuid4

import pytest
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from sediment_core.postgres_engine import DatabaseOperationError
from sediment_core.postgres_roles import (
    DatabasePrivilegeError,
    RoleNames,
    administrator_sql,
    migrate_database,
)

# Creates cluster-wide roles under names no other module uses; see
# packages/core/tests/test_postgres_roles.py for the default-name contract.
pytestmark = pytest.mark.cluster_roles
ROLES = RoleNames("sediment_test_api_m", "sediment_test_api_r", "sediment_test_api_o")
ADMIN, ADMIN_PASSWORD = "sediment_test_api_admin", "api-admin-test-secret"
PASSWORDS = {role: f"{role}-secret" for role in ROLES}


@pytest.fixture
def provisioned(postgres_admin_url):
    """The administrator's printed SQL applied by a non-superuser owner."""
    superuser = create_engine(
        postgres_admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    database = f"sediment_api_role_test_{uuid4().hex}"
    engines = []

    def url(role, password=None):
        return (
            make_url(postgres_admin_url)
            .set(
                database=database,
                username=role,
                password=password or PASSWORDS.get(role, ADMIN_PASSWORD),
            )
            .render_as_string(hide_password=False)
        )

    def connect(role, password=None):
        engine = create_engine(url(role, password), poolclass=NullPool)
        engines.append(engine)
        return engine

    with superuser.connect() as connection:
        connection.exec_driver_sql(
            f"CREATE ROLE {ADMIN} LOGIN CREATEROLE CREATEDB PASSWORD '{ADMIN_PASSWORD}'"
        )
        connection.exec_driver_sql(f'CREATE DATABASE "{database}" OWNER {ADMIN}')
    try:
        with connect(ADMIN).connect() as connection:
            driver = connection.connection.driver_connection
            driver.execute(administrator_sql(database, ROLES))
            for role, secret in PASSWORDS.items():
                driver.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(secret)
                    )
                )
            driver.commit()
        yield url, connect
    finally:
        for engine in engines:
            engine.dispose()
        with superuser.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
            for role in (*ROLES, ADMIN):
                connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')
        superuser.dispose()


def start(monkeypatch, database_url):
    from fastapi.testclient import TestClient

    from sediment_api.config import settings
    from sediment_api.main import app

    monkeypatch.setattr(settings, "database_url", SecretStr(database_url))
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "migrator_role", ROLES.migrator)
    monkeypatch.setattr(settings, "runtime_role", ROLES.runtime)
    monkeypatch.setattr(settings, "operator_role", ROLES.operator)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_startup_refuses_until_the_migrate_step_grants_then_serves(
    provisioned, monkeypatch
):
    url, connect = provisioned
    with pytest.raises(DatabaseOperationError, match="found absent"):
        start(monkeypatch, url(ROLES.runtime))
    assert migrate_database(url(ROLES.migrator), ROLES)
    start(monkeypatch, url(ROLES.runtime))
    # A table that Alembic created without grants blocks startup again.
    with connect(ROLES.migrator).begin() as connection:
        connection.exec_driver_sql(
            f"REVOKE ALL ON inference_call_aliases FROM {ROLES.runtime}"
        )
    with pytest.raises(DatabasePrivilegeError, match="inference_call_aliases"):
        start(monkeypatch, url(ROLES.runtime))
    assert migrate_database(url(ROLES.migrator), ROLES)
    start(monkeypatch, url(ROLES.runtime))


def test_startup_uses_a_runtime_password_the_role_rotated_itself(
    provisioned, monkeypatch
):
    url, connect = provisioned
    assert migrate_database(url(ROLES.migrator), ROLES)
    with connect(ROLES.runtime).connect() as connection:
        driver = connection.connection.driver_connection
        driver.execute("ALTER ROLE CURRENT_USER PASSWORD 'rotated-runtime-secret'")
        driver.commit()
    start(monkeypatch, url(ROLES.runtime, "rotated-runtime-secret"))


def test_startup_refuses_the_migrator_and_the_administrator(provisioned, monkeypatch):
    url, _ = provisioned
    assert migrate_database(url(ROLES.migrator), ROLES)
    with pytest.raises(DatabasePrivilegeError, match=ROLES.runtime):
        start(monkeypatch, url(ROLES.migrator))
    # The administrator can't even read the schema revision.
    with pytest.raises(DatabaseOperationError, match="verify database startup"):
        start(monkeypatch, url(ADMIN))

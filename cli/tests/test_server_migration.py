# SPDX-License-Identifier: AGPL-3.0-or-later
"""`sediment server` migrates an external database at start, then serves."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import types
from uuid import uuid4

import httpx
import pytest
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from sediment_core import postgres_migrations
from sediment_core.postgres_migrations import (
    MIGRATION_LOCK_KEY,
    RevisionState,
    inspect_revision,
)
from sediment_core.postgres_roles import RoleNames, administrator_sql

# Creates cluster-wide roles under names no other module uses.
pytestmark = pytest.mark.cluster_roles
ROLES = RoleNames("sediment_test_srv_m", "sediment_test_srv_r", "sediment_test_srv_o")
PASSWORDS = {role: f"{role}-secret" for role in ROLES}


@pytest.fixture
def unmigrated(postgres_admin_url):
    """An administrator-provisioned database that no migration has touched."""
    superuser = create_engine(
        postgres_admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    database = f"sediment_server_test_{uuid4().hex}"

    def url(role=None):
        target = make_url(postgres_admin_url).set(database=database)
        if role:
            target = target.set(username=role, password=PASSWORDS[role])
        return target.render_as_string(hide_password=False)

    with superuser.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
    try:
        with create_engine(url(), poolclass=NullPool).connect() as connection:
            driver = connection.connection.driver_connection
            driver.execute(administrator_sql(database, ROLES))
            for role, secret in PASSWORDS.items():
                driver.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(secret)
                    )
                )
            driver.commit()
        yield database, url
    finally:
        with superuser.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
            for role in ROLES:
                connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')
        superuser.dispose()


def server_environment(url, home):
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith("SEDIMENT_")},
        "HOME": str(home),
        "SEDIMENT_ORG_ID": "acme",
        "SEDIMENT_DEV_MODE": "false",
        "SEDIMENT_ALLOWED_CLONE_HOSTS": '["github.com"]',
        "SEDIMENT_MIGRATOR_ROLE": ROLES.migrator,
        "SEDIMENT_RUNTIME_ROLE": ROLES.runtime,
        "SEDIMENT_OPERATOR_ROLE": ROLES.operator,
        "SEDIMENT_MIGRATOR_DATABASE_URL": url(ROLES.migrator),
        "SEDIMENT_DATABASE_URL": url(ROLES.runtime),
    }


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_two_replicas_migrate_once_and_serve_with_the_runtime_role(
    unmigrated, tmp_path
):
    database, url = unmigrated
    replicas = []
    # Hold the migration lock while both start, so both must wait for it.
    holder = create_engine(url(ROLES.migrator), poolclass=NullPool)
    lock = holder.connect()
    lock.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
    try:
        for index in range(2):
            port = free_port()
            log = (tmp_path / f"replica-{index}.log").open("w")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "sediment_cli.cli",
                    "server",
                    "--port",
                    str(port),
                    "--root",
                    str(tmp_path / f"root-{index}"),
                ],
                cwd=tmp_path,
                env=server_environment(url, tmp_path),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            replicas.append((process, port, log))
        # Both replicas reach the lock and wait; neither serves yet.
        waited = time.monotonic() + 30
        while time.monotonic() < waited:
            with create_engine(url(), poolclass=NullPool).connect() as connection:
                waiting = connection.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                        "AND NOT granted"
                    )
                ).scalar_one()
            if waiting == 2:
                break
            time.sleep(0.2)
        assert waiting == 2, "replicas did not wait on the migration lock"
        lock.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY}
        )
        lock.close()
        deadline = time.monotonic() + 90
        for index, (process, port, _) in enumerate(replicas):
            while True:
                try:
                    if httpx.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None or time.monotonic() > deadline:
                    log = (tmp_path / f"replica-{index}.log").read_text()
                    pytest.fail(f"replica {index} did not serve:\n{log}")
                time.sleep(0.2)
        assert inspect_revision(url()).state is RevisionState.AT_HEAD
        with create_engine(url(), poolclass=NullPool).connect() as connection:
            identities = set(
                connection.execute(
                    text(
                        "SELECT usename FROM pg_stat_activity "
                        "WHERE datname=:database AND pid<>pg_backend_pid()"
                    ),
                    {"database": database},
                ).scalars()
            )
        assert identities == {ROLES.runtime}
        for index in range(2):
            output = (tmp_path / f"replica-{index}.log").read_text()
            assert "secret" not in output
    finally:
        lock.close()
        holder.dispose()
        for process, _, log in replicas:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            log.close()


def test_server_fails_when_the_migration_lock_outlasts_its_wait(
    unmigrated, tmp_path, monkeypatch, capsys
):
    from sediment_cli.cli import main

    _, url = unmigrated
    monkeypatch.setattr(os, "environ", server_environment(url, tmp_path), raising=True)
    monkeypatch.setattr(postgres_migrations, "MIGRATION_LOCK_WAIT_SECONDS", 1.0)
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        types.SimpleNamespace(run=lambda *a, **k: pytest.fail("served")),
    )
    holder = create_engine(url(ROLES.migrator), poolclass=NullPool)
    try:
        with holder.connect() as connection:
            connection.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY}
            )
            assert main(["server", "--root", str(tmp_path / "root")]) == 1
    finally:
        holder.dispose()
    captured = capsys.readouterr()
    assert captured.err == (
        "error: database migration lock is held by another process after "
        "waiting 1 seconds\n"
    )
    assert inspect_revision(url()).state is RevisionState.ABSENT

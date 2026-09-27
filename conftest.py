# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared real-PostgreSQL fixtures for store and migration contract tests."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterator
from hashlib import sha256
from pathlib import Path
from threading import Lock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool

_DEFAULT_ADMIN_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres"
_DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_TEMPLATE_DATABASE = re.compile(r"^sediment_tpl_[0-9a-f]{48}$")
_TEMPLATE_BUILD_DATABASE = re.compile(r"^sediment_tpl_build_[0-9a-f]{16}$")
_PYTEST_DATABASE = re.compile(
    r"^sediment_pytest_(?P<started>[0-9a-f]{8})_"
    r"(?:master|gw[0-9]+|w[0-9a-f]{8})_[0-9a-f]{8}$"
)
_TEMPLATE_COMMENT = re.compile(r"^sediment-pytest-template:(?P<used>[0-9]+)$")
_TEMPLATE_LOCK_KEY = 7_315_324_899_385_579_001
_STALE_AFTER_SECONDS = 24 * 60 * 60

PostgresDatabaseFactory = Callable[..., str]
PostgresStoreFactory = Callable[[], tuple[str, object]]


def pytest_sessionstart(session: pytest.Session) -> None:
    """Match runtime library discovery before fixtures import the driver."""
    from sediment_core.postgres_engine import configure_libpq

    configure_libpq()


def _migration_digest(paths: list[Path] | None = None) -> str:
    """Hash ordered Alembic revision names and bytes without import side effects."""
    if paths is None:
        versions = (
            Path(__file__).parent
            / "packages"
            / "core"
            / "sediment_core"
            / "alembic"
            / "versions"
        )
        paths = [path for path in versions.glob("*.py") if path.name != "__init__.py"]
    if not paths:
        raise RuntimeError("no Alembic revision scripts found")
    digest = sha256()
    for path in sorted(paths, key=lambda candidate: candidate.name):
        name = path.name.encode()
        content = path.read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _template_database_name(digest: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("migration digest must be 64 lowercase hexadecimal digits")
    return f"sediment_tpl_{digest[:48]}"


def _worker_tag(worker_id: str) -> str:
    if worker_id == "master" or re.fullmatch(r"gw[0-9]+", worker_id):
        return worker_id
    return f"w{sha256(worker_id.encode()).hexdigest()[:8]}"


def _pytest_database_name(worker_id: str, started_at: int) -> str:
    name = (
        f"sediment_pytest_{started_at:08x}_{_worker_tag(worker_id)}_{uuid4().hex[:8]}"
    )
    if not _PYTEST_DATABASE.fullmatch(name):  # pragma: no cover - generated invariant
        raise RuntimeError("generated unsafe PostgreSQL test database name")
    return name


def _database_url(admin_url: str, name: str) -> str:
    return make_url(admin_url).set(database=name).render_as_string(hide_password=False)


def _database_inventory(connection) -> dict[str, str | None]:
    rows = connection.execute(
        text(
            "SELECT datname, shobj_description(oid, 'pg_database') AS comment "
            "FROM pg_database"
        )
    ).mappings()
    return {row["datname"]: row["comment"] for row in rows}


def _cleanup_candidates(
    inventory: dict[str, str | None],
    *,
    current_template: str,
    cutoff: int,
) -> list[str]:
    candidates = []
    for name, comment in inventory.items():
        if _TEMPLATE_BUILD_DATABASE.fullmatch(name):
            candidates.append(name)
            continue
        if _TEMPLATE_DATABASE.fullmatch(name) and name != current_template:
            match = _TEMPLATE_COMMENT.fullmatch(comment or "")
            if match is None or int(match.group("used")) < cutoff:
                candidates.append(name)
            continue
        match = _PYTEST_DATABASE.fullmatch(name)
        if match is not None and int(match.group("started"), 16) < cutoff:
            candidates.append(name)
    return sorted(candidates)


def _drop_database(connection, name: str) -> None:
    if not _DATABASE_NAME.fullmatch(name):
        raise RuntimeError("refusing unsafe PostgreSQL test database name")
    connection.execute(
        text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = :database_name AND pid <> pg_backend_pid()"
        ),
        {"database_name": name},
    )
    connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')


def _ensure_template_database(
    admin_url: str,
    *,
    digest: str,
    clean_stale: bool = False,
) -> str:
    """Publish one migrated template under an administrative-database lock."""
    from sediment_core.postgres_migrations import upgrade_database

    template_name = _template_database_name(digest)
    admin_engine = create_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    try:
        with admin_engine.connect() as connection:
            connection.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": _TEMPLATE_LOCK_KEY}
            ).scalar_one()
            try:
                inventory = _database_inventory(connection)
                if clean_stale:
                    cutoff = int(time.time()) - _STALE_AFTER_SECONDS
                    for name in _cleanup_candidates(
                        inventory,
                        current_template=template_name,
                        cutoff=cutoff,
                    ):
                        _drop_database(connection, name)
                    inventory = _database_inventory(connection)
                if template_name not in inventory:
                    build_name = f"sediment_tpl_build_{uuid4().hex[:16]}"
                    connection.exec_driver_sql(f'CREATE DATABASE "{build_name}"')
                    try:
                        upgrade_database(_database_url(admin_url, build_name))
                        connection.exec_driver_sql(
                            f'ALTER DATABASE "{build_name}" WITH ALLOW_CONNECTIONS false'
                        )
                        connection.exec_driver_sql(
                            f'ALTER DATABASE "{build_name}" RENAME TO "{template_name}"'
                        )
                    except BaseException:
                        _drop_database(connection, build_name)
                        raise
                connection.exec_driver_sql(
                    f'COMMENT ON DATABASE "{template_name}" IS '
                    f"'sediment-pytest-template:{int(time.time())}'"
                )
            finally:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _TEMPLATE_LOCK_KEY},
                ).scalar_one()
    finally:
        admin_engine.dispose()
    return template_name


@pytest.fixture(scope="session")
def postgres_admin_url() -> str:
    """The administrative URL every PostgreSQL-backed test resolves the same way."""
    return os.environ.get("SEDIMENT_TEST_DATABASE_URL", _DEFAULT_ADMIN_URL)


@pytest.fixture(scope="session")
def postgres_database_factory(
    request: pytest.FixtureRequest,
    postgres_admin_url: str,
) -> Iterator[PostgresDatabaseFactory]:
    """Create and remove exact-name isolated PostgreSQL test databases."""
    admin_url = postgres_admin_url
    worker_input = getattr(request.config, "workerinput", {})
    worker_id = worker_input.get("workerid", "master")
    started_at = int(time.time())
    created: list[str] = []
    created_lock = Lock()
    admin_engine = create_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    migration_digest = _migration_digest()
    template_name = _ensure_template_database(
        admin_url,
        digest=migration_digest,
        clean_stale=True,
    )

    def create_database(*, migrated: bool = True) -> str:
        name = _pytest_database_name(worker_id, started_at)
        with admin_engine.connect() as connection:
            if migrated:
                connection.exec_driver_sql(
                    f'CREATE DATABASE "{name}" WITH TEMPLATE "{template_name}"'
                )
            else:
                connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
        with created_lock:
            created.append(name)
        return _database_url(admin_url, name)

    yield create_database

    with created_lock:
        cleanup_names = list(reversed(created))
    for name in cleanup_names:
        with admin_engine.connect() as connection:
            _drop_database(connection, name)
    admin_engine.dispose()


@pytest.fixture(scope="session")
def postgres_database_url(
    postgres_database_factory: PostgresDatabaseFactory,
) -> str:
    """One migrated PostgreSQL database isolated to this pytest worker."""
    return postgres_database_factory()


@pytest.fixture()
def postgres_engine(postgres_database_url: str):
    """A pooled engine over the worker database, emptied after each test."""
    from sediment_core.postgres_engine import create_postgres_engine

    engine = create_postgres_engine(postgres_database_url)
    try:
        yield engine
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "TRUNCATE TABLE fact_quarantine, inference_calls, "
                "developer_decisions, edit_observations, rejected_edits, "
                "retry_linkages, ci_outcomes, pushes, pull_request_merges, "
                "pull_request_revisions, session_commit_observations, repository_renames, sessions "
                "RESTART IDENTITY CASCADE"
            )
        engine.dispose()


@pytest.fixture()
def postgres_store(postgres_engine: Engine):
    """Return the public fact store over the test's emptied database."""
    from sediment_core import FactStore

    return FactStore(postgres_engine)


@pytest.fixture()
def postgres_store_factory(
    postgres_database_factory: PostgresDatabaseFactory,
) -> Iterator[PostgresStoreFactory]:
    """Create isolated public fact stores and dispose their engines."""
    from sediment_core import FactStore
    from sediment_core.postgres_engine import create_postgres_engine

    engines: list[Engine] = []

    def create_store() -> tuple[str, object]:
        database_url = postgres_database_factory()
        engine = create_postgres_engine(database_url)
        engines.append(engine)
        return database_url, FactStore(engine)

    yield create_store

    for engine in engines:
        engine.dispose()

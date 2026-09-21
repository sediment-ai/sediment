# SPDX-License-Identifier: AGPL-3.0-or-later
"""PostgreSQL template-database contract for the shared test substrate."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from sediment_core.postgres_migrations import RevisionState, inspect_revision


def _load_test_database_module() -> ModuleType:
    path = Path(__file__).parents[2] / "conftest.py"
    spec = spec_from_file_location("sediment_root_conftest", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise RuntimeError("could not load root conftest.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


test_databases = _load_test_database_module()


def _admin_url() -> str:
    return os.environ.get(
        "SEDIMENT_TEST_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres",
    )


def _database_url(admin_url: str, name: str) -> str:
    return make_url(admin_url).set(database=name).render_as_string(hide_password=False)


def _database_names(admin_url: str) -> set[str]:
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with engine.connect() as connection:
            return set(
                connection.execute(text("SELECT datname FROM pg_database")).scalars()
            )
    finally:
        engine.dispose()


def _drop_databases(admin_url: str, names: set[str]) -> None:
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with engine.connect() as connection:
            for name in names:
                connection.exec_driver_sql(
                    f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'
                )
    finally:
        engine.dispose()


def _freeze_schema_value(value: object) -> object:
    if isinstance(value, dict):
        return tuple(
            sorted((key, _freeze_schema_value(item)) for key, item in value.items())
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_schema_value(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _schema_dump(database_url: str) -> tuple[object, ...]:
    """Return the schema contract needed to compare two migrated databases."""
    engine = create_engine(database_url, poolclass=NullPool)
    try:
        database = inspect(engine)
        tables = []
        for table_name in sorted(database.get_table_names()):
            tables.append(
                (
                    table_name,
                    tuple(
                        sorted(
                            (
                                column["name"],
                                str(column["type"]),
                                column["nullable"],
                                column["default"],
                                tuple(sorted((column.get("identity") or {}).items())),
                                tuple(sorted((column.get("computed") or {}).items())),
                            )
                            for column in database.get_columns(table_name)
                        )
                    ),
                    tuple(
                        sorted(
                            (
                                index["name"],
                                tuple(index["column_names"] or ()),
                                index["unique"],
                                tuple(index.get("expressions") or ()),
                                _freeze_schema_value(
                                    index.get("dialect_options") or {}
                                ),
                            )
                            for index in database.get_indexes(table_name)
                        )
                    ),
                    tuple(
                        sorted(
                            (constraint["name"], constraint["sqltext"])
                            for constraint in database.get_check_constraints(table_name)
                        )
                    ),
                    tuple(sorted(database.get_pk_constraint(table_name).items())),
                    tuple(
                        sorted(
                            (
                                constraint["name"],
                                tuple(constraint["column_names"] or ()),
                            )
                            for constraint in database.get_unique_constraints(
                                table_name
                            )
                        )
                    ),
                    tuple(
                        sorted(
                            (
                                constraint["name"],
                                tuple(constraint["constrained_columns"] or ()),
                                constraint["referred_schema"],
                                constraint["referred_table"],
                                tuple(constraint["referred_columns"] or ()),
                                tuple(
                                    sorted((constraint.get("options") or {}).items())
                                ),
                            )
                            for constraint in database.get_foreign_keys(table_name)
                        )
                    ),
                )
            )
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        views = tuple(
            (name, database.get_view_definition(name))
            for name in sorted(database.get_view_names())
        )
        return (
            tuple(tables),
            tuple(sorted(database.get_sequence_names())),
            views,
            revision,
        )
    finally:
        engine.dispose()


def test_migration_digest_is_ordered_content_sensitive_and_identifier_safe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    digest_migrations = getattr(test_databases, "_migration_digest", None)
    template_name = getattr(test_databases, "_template_database_name", None)
    assert callable(digest_migrations)
    assert callable(template_name)
    with pytest.raises(RuntimeError, match="no Alembic revision scripts"):
        digest_migrations([])

    first = tmp_path / "0001_first.py"
    second = tmp_path / "0002_second.py"
    first.write_text("revision = 'one'\n")
    second.write_text("revision = 'two'\n")

    digest = digest_migrations([second, first])
    assert digest == digest_migrations([first, second])
    first.write_text("revision = 'changed'\n")
    assert digest != digest_migrations([first, second])
    assert template_name(digest).startswith("sediment_tpl_")
    assert len(template_name(digest).encode()) <= 63

    versions = tmp_path / "packages" / "core" / "sediment_core" / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "0001_numeric.py").write_text("revision = 'numeric'\n")
    alphabetic = versions / "a123_alphabetic.py"
    alphabetic.write_text("revision = 'alphabetic'\n")
    (versions / "__init__.py").write_text("")
    monkeypatch.setattr(test_databases, "__file__", str(tmp_path / "conftest.py"))

    default_digest = digest_migrations()
    alphabetic.write_text("revision = 'changed'\n")
    assert default_digest != digest_migrations()


def test_migrated_factory_copy_matches_a_fresh_upgrade_schema(
    postgres_database_factory,
) -> None:
    digest_migrations = getattr(test_databases, "_migration_digest", None)
    template_name = getattr(test_databases, "_template_database_name", None)
    assert callable(digest_migrations)
    assert callable(template_name)

    clone_url = postgres_database_factory()
    fresh_url = postgres_database_factory(migrated=False)
    from sediment_core.postgres_migrations import upgrade_database

    upgrade_database(fresh_url)

    expected_template = template_name(digest_migrations())
    admin_engine = create_engine(
        _admin_url(), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        with admin_engine.connect() as connection:
            row = connection.execute(
                text("SELECT datallowconn FROM pg_database WHERE datname = :name"),
                {"name": expected_template},
            ).one()
        assert row.datallowconn is False
    finally:
        admin_engine.dispose()

    assert _schema_dump(clone_url) == _schema_dump(fresh_url)


def test_schema_dump_detects_constraint_and_index_drift(
    postgres_database_factory,
) -> None:
    identity_clone = postgres_database_factory()
    identity_fresh = postgres_database_factory()
    primary_key_clone = postgres_database_factory()
    primary_key_fresh = postgres_database_factory()
    partial_index_clone = postgres_database_factory()
    partial_index_fresh = postgres_database_factory()

    identity_engine = create_engine(identity_clone, poolclass=NullPool)
    try:
        with identity_engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE fact_quarantine "
                    "ALTER COLUMN quarantine_revision DROP IDENTITY"
                )
            )
    finally:
        identity_engine.dispose()

    primary_key_engine = create_engine(primary_key_clone, poolclass=NullPool)
    try:
        with primary_key_engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE inference_calls DROP CONSTRAINT inference_calls_pkey")
            )
    finally:
        primary_key_engine.dispose()

    partial_index_engine = create_engine(partial_index_clone, poolclass=NullPool)
    try:
        with partial_index_engine.begin() as connection:
            connection.execute(text("DROP INDEX uq_decisions_keyed"))
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX uq_decisions_keyed ON developer_decisions "
                    "(org_id, agent_harness, session_id, accepted, explicit, "
                    "interaction_mode, occurred_at, call_id)"
                )
            )
    finally:
        partial_index_engine.dispose()

    assert _schema_dump(identity_clone) != _schema_dump(identity_fresh)
    assert _schema_dump(primary_key_clone) != _schema_dump(primary_key_fresh)
    assert _schema_dump(partial_index_clone) != _schema_dump(partial_index_fresh)


def test_concurrent_template_initialization_publishes_one_valid_template() -> None:
    digest_migrations = getattr(test_databases, "_migration_digest", None)
    ensure_template = getattr(test_databases, "_ensure_template_database", None)
    template_name = getattr(test_databases, "_template_database_name", None)
    assert callable(digest_migrations)
    assert callable(ensure_template)
    assert callable(template_name)

    digest = uuid4().hex + uuid4().hex
    expected = template_name(digest)
    clone = "sediment_concurrency_" + uuid4().hex[:8]
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(
                    lambda _index: ensure_template(_admin_url(), digest=digest),
                    range(4),
                )
            )
        assert results == [expected] * 4
        assert expected in _database_names(_admin_url())
        admin_engine = create_engine(
            _admin_url(), isolation_level="AUTOCOMMIT", poolclass=NullPool
        )
        try:
            with admin_engine.connect() as connection:
                connection.exec_driver_sql(
                    f'CREATE DATABASE "{clone}" WITH TEMPLATE "{expected}"'
                )
        finally:
            admin_engine.dispose()
        assert (
            inspect_revision(_database_url(_admin_url(), clone)).state
            is RevisionState.AT_HEAD
        )
    finally:
        _drop_databases(_admin_url(), {clone, expected})


def test_cleanup_drops_only_stale_sediment_test_databases() -> None:
    digest_migrations = getattr(test_databases, "_migration_digest", None)
    ensure_template = getattr(test_databases, "_ensure_template_database", None)
    assert callable(digest_migrations)
    assert callable(ensure_template)

    stale_template = "sediment_tpl_" + "0" * 48
    stale_build = "sediment_tpl_build_" + "1" * 16
    stale_test = "sediment_pytest_00000001_master_" + "2" * 8
    recent_test = f"sediment_pytest_{int(time.time()):08x}_master_" + "3" * 8
    unrelated = "sediment_keep_" + uuid4().hex[:8]
    candidates = {stale_template, stale_build, stale_test, recent_test, unrelated}
    admin_engine = create_engine(
        _admin_url(), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        with admin_engine.connect() as connection:
            for name in candidates:
                connection.exec_driver_sql(f'CREATE DATABASE "{name}"')

        ensure_template(
            _admin_url(),
            digest=digest_migrations(),
            clean_stale=True,
        )

        remaining = _database_names(_admin_url())
        assert stale_template not in remaining
        assert stale_build not in remaining
        assert stale_test not in remaining
        assert recent_test in remaining
        assert unrelated in remaining
    finally:
        _drop_databases(_admin_url(), candidates)

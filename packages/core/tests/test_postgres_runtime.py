# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from sediment_core import FactStore
import sediment_core.postgres_engine as postgres_engine_module
import sediment_core.postgres_migrations as postgres_migrations_module
import sediment_core.store as store_module


def test_public_fact_store_is_the_postgres_implementation(postgres_engine) -> None:
    assert FactStore is getattr(store_module, "FactStore", None)
    store = FactStore(postgres_engine)

    assert store.health_check() is True


def test_api_engine_limits_do_not_change_operator_engines(postgres_database_url):
    engine = postgres_engine_module.create_postgres_engine(
        postgres_database_url, api_work=True, single_connection=True
    )
    operator = postgres_engine_module.create_postgres_engine(postgres_database_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SHOW statement_timeout").scalar_one()
                == "30s"
            )
            assert connection.exec_driver_sql("SHOW lock_timeout").scalar_one() == "5s"
            assert (
                connection.exec_driver_sql(
                    "SHOW idle_in_transaction_session_timeout"
                ).scalar_one()
                == "30s"
            )
        assert engine.pool.size() == 1
        assert engine.pool._max_overflow == 0
        with operator.connect() as connection:
            assert (
                connection.exec_driver_sql("SHOW statement_timeout").scalar_one() == "0"
            )
    finally:
        engine.dispose()
        operator.dispose()


def test_api_statement_and_lock_timeouts_cancel_work_and_release_connection(
    postgres_database_url, postgres_engine, monkeypatch
):
    monkeypatch.setattr(postgres_engine_module, "API_STATEMENT_TIMEOUT_MS", 200)
    monkeypatch.setattr(postgres_engine_module, "API_LOCK_TIMEOUT_MS", 50)
    engine = postgres_engine_module.create_postgres_engine(
        postgres_database_url, api_work=True, single_connection=True
    )
    try:
        with engine.connect() as connection:
            with pytest.raises(SQLAlchemyError) as error:
                connection.exec_driver_sql("SELECT pg_sleep(10)")
            assert error.value.orig.sqlstate == "57014"
            connection.rollback()
            assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
        with postgres_engine.begin() as holder:
            holder.exec_driver_sql(
                "LOCK TABLE inference_calls IN ACCESS EXCLUSIVE MODE"
            )
            with engine.connect() as blocked:
                with pytest.raises(SQLAlchemyError) as error:
                    blocked.exec_driver_sql("SELECT count(*) FROM inference_calls")
                assert error.value.orig.sqlstate == "55P03"
                blocked.rollback()
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM inference_calls"
                ).scalar_one()
                == 0
            )
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("database_url", "target"),
    [
        (
            "postgresql+psycopg://sentinel-user:sentinel-password@"
            "db.example.test:6543/sediment?sslmode=require&token=sentinel-query",
            "db.example.test:6543/sediment",
        ),
        (
            "postgresql+psycopg://user%40example.test:pass%2Fword@"
            "localhost/sediment_eval?application_name=sentinel-app",
            "localhost/sediment_eval",
        ),
        ("not a database URL with sentinel-invalid", "invalid PostgreSQL target"),
    ],
)
def test_sanitized_database_target_omits_credentials_and_query_parameters(
    database_url: str,
    target: str,
) -> None:
    sanitize = getattr(postgres_engine_module, "sanitized_database_target", None)

    assert callable(sanitize)
    rendered = sanitize(database_url)
    assert rendered == target
    for sentinel in (
        "sentinel-user",
        "sentinel-password",
        "sentinel-query",
        "user%40example.test",
        "pass%2Fword",
        "sentinel-app",
        "sentinel-invalid",
    ):
        assert sentinel not in rendered


def test_database_operation_error_contains_only_the_safe_operation_and_target() -> None:
    database_url = (
        "postgresql+psycopg://sentinel-user:sentinel-password@"
        "db.example.test:6543/sediment?token=sentinel-query"
    )
    build_error = getattr(postgres_engine_module, "database_operation_error", None)

    assert callable(build_error)
    error = build_error("inspect database revision", database_url)
    assert isinstance(error, RuntimeError)
    assert str(error) == (
        "inspect database revision failed for db.example.test:6543/sediment"
    )
    assert repr(error) == (
        "DatabaseOperationError('inspect database revision failed for "
        "db.example.test:6543/sediment')"
    )
    for sentinel in (
        "sentinel-user",
        "sentinel-password",
        "sentinel-query",
    ):
        assert sentinel not in str(error)
        assert sentinel not in repr(error)


def test_engine_revision_inspection_borrows_the_process_engine(
    postgres_engine,
) -> None:
    inspect_engine_revision = getattr(
        postgres_migrations_module, "inspect_engine_revision", None
    )

    assert callable(inspect_engine_revision)
    inspection = inspect_engine_revision(postgres_engine)
    assert inspection.state is postgres_migrations_module.RevisionState.AT_HEAD
    with postgres_engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


def test_invalid_url_hints_at_percent_encoding_without_echoing_input() -> None:
    with pytest.raises(ValueError) as captured:
        postgres_engine_module.create_postgres_engine(
            "postgresql://user:sentinel-p@ss!word@localhost/sediment"
        )

    rendered = str(captured.value)
    assert "percent-encode" in rendered
    assert "sentinel-p" not in rendered
    assert "ss!word" not in rendered


def test_wait_for_database_returns_when_the_server_is_reachable(
    postgres_engine,
) -> None:
    postgres_engine_module.wait_for_database(
        postgres_engine, deadline_seconds=6.0, interval_seconds=0.05
    )


def test_wait_for_database_fails_bounded_with_only_the_safe_target() -> None:
    engine = postgres_engine_module.create_postgres_engine(
        "postgresql+psycopg://sentinel-user:sentinel-password@127.0.0.1:1/sediment"
    )
    try:
        with pytest.raises(postgres_engine_module.DatabaseOperationError) as captured:
            postgres_engine_module.wait_for_database(
                engine, deadline_seconds=0.3, interval_seconds=0.05
            )
    finally:
        engine.dispose()

    rendered = str(captured.value)
    assert rendered == "connect to database failed for 127.0.0.1:1/sediment"
    assert "sentinel-user" not in rendered
    assert "sentinel-password" not in rendered


def test_wait_for_database_does_not_start_an_attempt_past_the_deadline(
    monkeypatch,
) -> None:
    clock = [0.0]

    class _Engine:
        url = make_url("postgresql+psycopg://user:password@db.example.test/sediment")

        def connect(self):
            clock[0] += 5.0
            raise SQLAlchemyError("simulated five-second connection timeout")

    monkeypatch.setattr(postgres_engine_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        postgres_engine_module.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    with pytest.raises(postgres_engine_module.DatabaseOperationError):
        postgres_engine_module.wait_for_database(
            _Engine(), deadline_seconds=30.0, interval_seconds=1.0
        )

    assert clock[0] <= 30.0


def test_minimum_server_version_accepts_the_real_server(postgres_engine) -> None:
    with postgres_engine.connect() as connection:
        postgres_engine_module.verify_minimum_server_version(connection)


def test_minimum_server_version_refuses_an_older_server() -> None:
    class _Result:
        def scalar_one(self) -> str:
            return "150004"

    class _Connection:
        def exec_driver_sql(self, _sql: str) -> _Result:
            return _Result()

    with pytest.raises(postgres_engine_module.DatabaseOperationError) as captured:
        postgres_engine_module.verify_minimum_server_version(_Connection())

    assert "PostgreSQL 15" in str(captured.value)
    assert "minimum PostgreSQL 16" in str(captured.value)


def test_sanitized_target_refuses_a_multi_at_authority() -> None:
    rendered = postgres_engine_module.sanitized_database_target(
        "postgresql://user:sentinel-p@ss!word@localhost/sediment"
    )
    assert rendered == "invalid PostgreSQL target"
    assert "ss!word" not in rendered


def test_invalid_port_gets_the_same_hint_without_parser_text() -> None:
    with pytest.raises(ValueError) as captured:
        postgres_engine_module.create_postgres_engine(
            "postgresql://user:pa]ss:@localhost:abc/sediment"
        )
    rendered = str(captured.value)
    assert "percent-encode" in rendered
    assert "abc" not in rendered and "pa]ss" not in rendered

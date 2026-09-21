# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real PostgreSQL deployment role boundaries; no shared deployment roles."""

from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from sediment_core import FactStore
from sediment_core.models import FactTable
from sediment_core.postgres_migrations import upgrade_database
from sediment_core.postgres_schema import metadata

ROLES = ("sediment_migrator", "sediment_runtime", "sediment_operator")
PASSWORDS = dict(
    zip(ROLES, ("migration-'special@secret", "runtime-secret", "operator-secret"))
)


def role_module():
    from sediment_core import postgres_roles

    return postgres_roles


def role_url(url, role):
    return (
        make_url(url)
        .set(username=role, password=PASSWORDS[role])
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="module")
def role_admin(postgres_admin_url):
    """Serialize this fixed-role contract and refuse to touch existing identities."""
    engine = create_engine(
        postgres_admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT pg_try_advisory_lock(731532489938557902)"
        ).scalar_one(), "role contract already running"
        existing = (
            connection.execute(
                text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:roles)"),
                {"roles": list(ROLES)},
            )
            .scalars()
            .all()
        )
        assert not existing, (
            "role tests need a disposable cluster without managed deployment identities"
        )
        try:
            yield engine
        finally:
            for role in ROLES:
                connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')
            connection.exec_driver_sql("SELECT pg_advisory_unlock(731532489938557902)")
    engine.dispose()


@pytest.fixture
def role_database(role_admin):
    name = f"sediment_role_test_{uuid4().hex}"
    with role_admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    url = role_admin.url.set(database=name).render_as_string(hide_password=False)
    engines = []

    def connect(role=None):
        engine = create_engine(role_url(url, role) if role else url, poolclass=NullPool)
        engines.append(engine)
        return engine

    try:
        yield url, connect
    finally:
        for engine in engines:
            engine.dispose()
        with role_admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')


def provision(url):
    role_module().provision_database(
        url,
        migrator_password=PASSWORDS[ROLES[0]],
        runtime_password=PASSWORDS[ROLES[1]],
        operator_password=PASSWORDS[ROLES[2]],
    )


def seed_every_fact(engine):
    # Reuse the canonical store contract builders; instantiate real validated Facts.
    samples = importlib.import_module("test_store")
    store = FactStore(engine)
    calls = [
        ("store_inference_call", "_inference_call"),
        ("store_decision", "_decision"),
        ("store_edit_observation", "_observation"),
        ("store_rejected_edit", "_rejected_edit"),
        ("store_retry_linkage", "_retry_linkage"),
        ("store_ci_outcome", "_ci_outcome"),
        ("store_push", "_push"),
        ("store_repository_rename", "_repository_rename"),
        ("store_session_commit_observation", "_session_commit_observation"),
        ("store_pull_request_merge", "_pull_request_merge"),
        ("store_pull_request_revision", "_pull_request_revision"),
    ]
    assert len(calls) == len(FactTable)
    for method, factory in calls:
        fact = getattr(samples, factory)()
        assert getattr(store, method)(fact)
        assert not getattr(store, method)(fact)
    assert all(store.count_facts("acme", table) == 1 for table in FactTable)
    return store


def snapshot(engine):
    with engine.connect() as connection:
        return {
            name: sorted(
                (dict(row) for row in connection.execute(table.select()).mappings()),
                key=repr,
            )
            for name, table in metadata.tables.items()
        }


def denied(engine, statement):
    with (
        engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection,
        pytest.raises(DBAPIError) as failure,
    ):
        connection.exec_driver_sql(statement)
    assert failure.value.orig.sqlstate == "42501"


def test_fresh_runtime_ingests_every_fact_and_operator_appends_quarantine(
    role_database,
):
    url, connect = role_database
    provision(url)
    runtime = connect(ROLES[1])
    operator = connect(ROLES[2])
    role_module().validate_runtime_privileges(runtime)
    store = seed_every_fact(runtime)
    op = FactStore(operator)
    op.quarantine_fact(
        "acme", FactTable.INFERENCE_CALLS, "inference-1", reason="review"
    )
    assert store.count_facts("acme", FactTable.INFERENCE_CALLS) == 0
    op.release_fact(
        "acme", FactTable.INFERENCE_CALLS, "inference-1", reason="review complete"
    )
    assert store.count_facts("acme", FactTable.INFERENCE_CALLS) == 1
    assert op.quarantine_revision("acme") == 2
    before = snapshot(operator)
    provision(url)
    assert snapshot(operator) == before
    upgrade_database(role_url(url, ROLES[0]))
    role_module().validate_runtime_privileges(runtime)


def test_legacy_transfer_preserves_every_fact_and_unrelated_object(role_database):
    url, connect = role_database
    upgrade_database(url)
    bootstrap = connect()
    seed_every_fact(bootstrap).quarantine_fact(
        "acme", FactTable.PUSHES, "push-1", reason="retain history"
    )
    before = snapshot(bootstrap)
    with bootstrap.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE unrelated (id int)")
    provision(url)
    assert snapshot(connect(ROLES[2])) == before
    with bootstrap.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT tableowner = current_user FROM pg_tables WHERE tablename = 'unrelated'"
        ).scalar_one()
        owners = (
            connection.execute(
                text(
                    "SELECT tableowner FROM pg_tables WHERE schemaname='public' AND tablename = ANY(:tables)"
                ),
                {"tables": [*metadata.tables, "alembic_version"]},
            )
            .scalars()
            .all()
        )
        assert set(owners) == {ROLES[0]}
    role_module().validate_runtime_privileges(connect(ROLES[1]))


@pytest.mark.parametrize("role", ROLES[1:])
def test_fact_mutation_and_administration_fail(role_database, role):
    url, connect = role_database
    provision(url)
    engine = connect(role)
    statements = [
        *(f'UPDATE "{table}" SET org_id=org_id' for table in FactTable),
        *(f'DELETE FROM "{table}"' for table in FactTable),
        *(f'TRUNCATE "{table}"' for table in FactTable),
        "UPDATE sessions SET session_id=session_id",
        "UPDATE sessions SET org_id=org_id",
        "DELETE FROM sessions",
        "TRUNCATE sessions",
        "UPDATE fact_quarantine SET reason=reason",
        "DELETE FROM fact_quarantine",
        "TRUNCATE fact_quarantine",
        "INSERT INTO alembic_version VALUES ('bad')",
        "UPDATE alembic_version SET version_num='bad'",
        "DELETE FROM alembic_version",
        "CREATE TABLE forbidden (id int)",
        "CREATE TEMP TABLE forbidden (id int)",
        "ALTER TABLE inference_calls ADD COLUMN forbidden int",
        "DROP TABLE inference_calls",
        "CREATE SCHEMA forbidden",
        "CREATE ROLE forbidden",
        "CREATE DATABASE forbidden",
        "SET ROLE sediment_migrator",
        "SET ROLE pg_execute_server_program",
        "SET ROLE pg_read_server_files",
        "SET ROLE pg_write_server_files",
        f'SET ROLE "{make_url(url).username}"',
    ]
    if role == ROLES[1]:
        statements += [
            "INSERT INTO fact_quarantine DEFAULT VALUES",
            "SELECT nextval('fact_quarantine_quarantine_revision_seq')",
        ]
    else:
        statements += [
            f'INSERT INTO "{table}" DEFAULT VALUES'
            for table in [*FactTable, "sessions"]
        ]
    for statement in statements:
        denied(engine, statement)


def test_concurrent_runtime_session_upsert_updates_only_metadata(role_database):
    url, connect = role_database
    provision(url)
    runtime = connect(ROLES[1])
    samples = importlib.import_module("test_store")
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def write(index):
        return FactStore(runtime).store_inference_call(
            samples._inference_call(
                inference_call_id=f"call-{index}",
                model_call_id=f"model-{index}",
                observed_at=start + timedelta(seconds=index),
                user_id=f"user-{index % 2}",
            )
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(write, [3, 1, 2, 0]))
    session = FactStore(runtime).read_sessions("acme")[0]
    assert session.first_observed_at == start
    assert session.last_observed_at == start + timedelta(seconds=3)
    assert session.user_id_conflict


@pytest.mark.parametrize(
    "grant",
    [
        "ALTER ROLE sediment_runtime SUPERUSER",
        "ALTER ROLE sediment_runtime CREATEROLE",
        "ALTER ROLE sediment_runtime CREATEDB",
        "ALTER ROLE sediment_runtime REPLICATION",
        "ALTER ROLE sediment_runtime BYPASSRLS",
        "GRANT sediment_migrator TO sediment_runtime WITH INHERIT FALSE",
        "GRANT pg_read_all_data TO sediment_runtime",
        "GRANT UPDATE ON inference_calls TO sediment_runtime",
        "GRANT UPDATE (raw) ON inference_calls TO sediment_runtime",
        "GRANT DELETE ON inference_calls TO PUBLIC",
        "GRANT INSERT ON fact_quarantine TO sediment_runtime",
        "GRANT UPDATE (org_id) ON sessions TO sediment_runtime",
        "GRANT CREATE ON SCHEMA public TO sediment_runtime",
        "ALTER TABLE inference_calls OWNER TO sediment_runtime",
        "GRANT SELECT ON inference_calls TO sediment_runtime WITH GRANT OPTION",
        "CREATE FUNCTION public.unexpected() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'",
        "CREATE TABLE unexpected (id int); GRANT SELECT ON unexpected TO sediment_runtime",
    ],
)
def test_runtime_validator_rejects_effective_excess_authority(role_database, grant):
    url, connect = role_database
    provision(url)
    with connect().begin() as connection:
        connection.exec_driver_sql(grant)
    with pytest.raises(role_module().DatabasePrivilegeError):
        role_module().validate_runtime_privileges(connect(ROLES[1]))
    # Provision reconciles direct managed-role drift; dropped DB removes object drift.
    with connect().begin() as connection:
        connection.exec_driver_sql(
            "ALTER ROLE sediment_runtime NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS"
        )
        connection.exec_driver_sql(
            "REVOKE sediment_migrator, pg_read_all_data FROM sediment_runtime"
        )


def test_cli_provision_requires_private_environment_and_sanitizes_failures(
    monkeypatch, capsys
):
    from sediment_cli.cli import main

    monkeypatch.delenv("SEDIMENT_ORG_ID", raising=False)
    for variable in [
        "SEDIMENT_BOOTSTRAP_DATABASE_URL",
        "SEDIMENT_MIGRATOR_PASSWORD",
        "SEDIMENT_RUNTIME_PASSWORD",
        "SEDIMENT_OPERATOR_PASSWORD",
    ]:
        monkeypatch.delenv(variable, raising=False)
    assert main(["db", "provision"]) == 1
    assert (
        capsys.readouterr().err
        == "error: set SEDIMENT_BOOTSTRAP_DATABASE_URL for database provisioning\n"
    )
    monkeypatch.setenv(
        "SEDIMENT_BOOTSTRAP_DATABASE_URL",
        "postgresql://user:sentinel@secret@localhost/db",
    )
    for role in ROLES:
        monkeypatch.setenv(role.upper() + "_PASSWORD", PASSWORDS[role])
    assert main(["db", "provision"]) == 1
    captured = capsys.readouterr()
    assert "sentinel" not in captured.err
    assert "invalid PostgreSQL URL" in captured.err


@pytest.mark.parametrize(
    "grant",
    [
        "CREATE TABLE unexpected (id int); GRANT SELECT (id) ON unexpected TO sediment_runtime",
        "GRANT MAINTAIN ON inference_calls TO sediment_runtime",
        "ALTER TABLE inference_calls OWNER TO sediment_operator",
    ],
)
def test_runtime_validator_covers_column_reads_maintenance_and_foreign_ownership(
    role_database, grant
):
    url, connect = role_database
    provision(url)
    with connect().begin() as connection:
        if "MAINTAIN" in grant and connection.dialect.server_version_info < (17,):
            # MAINTAIN is a PostgreSQL 17 privilege; _validate_privileges only
            # checks it starting at that version (see postgres_roles.py), and
            # the declared minimum server is 16.
            pytest.skip("MAINTAIN requires PostgreSQL 17")
        connection.exec_driver_sql(grant)
    with pytest.raises(role_module().DatabasePrivilegeError):
        role_module().validate_runtime_privileges(connect(ROLES[1]))


def test_repeat_provision_reconciles_privileged_role_and_column_drift(role_database):
    url, connect = role_database
    provision(url)
    with connect().begin() as connection:
        connection.exec_driver_sql("ALTER ROLE sediment_runtime CREATEROLE")
        connection.exec_driver_sql(
            "GRANT sediment_migrator TO sediment_runtime WITH INHERIT FALSE"
        )
        connection.exec_driver_sql(
            "GRANT UPDATE (raw) ON inference_calls TO sediment_runtime"
        )
        connection.exec_driver_sql("GRANT DELETE ON pushes TO PUBLIC")
    provision(url)
    runtime = connect(ROLES[1])
    role_module().validate_runtime_privileges(runtime)
    denied(runtime, "UPDATE inference_calls SET raw=raw")
    denied(runtime, "SET ROLE sediment_migrator")


def test_legacy_behind_migration_preserves_logical_values(role_database):
    from alembic import command
    from sediment_core.postgres_migrations import (
        _alembic_config,
        inspect_revision,
        RevisionState,
    )

    url, connect = role_database
    bootstrap = connect()
    with bootstrap.connect() as connection:
        config = _alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0008_session_commit_observations")
    # Pre-0009 storage keeps these descriptive fields as literal text.
    with bootstrap.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO fact_quarantine (quarantine_id, org_id, fact_table, fact_id, action, reason, recorded_at) VALUES ('retained', 'acme', 'inference_calls', 'call', 'quarantine', 'legacy reason', '2026-01-01T00:00:00Z')"
        )
    provision(url)
    assert inspect_revision(role_url(url, ROLES[0])).state is RevisionState.AT_HEAD
    assert (
        FactStore(connect(ROLES[2])).read_quarantine_log("acme")[0].reason
        == "legacy reason"
    )
    assert FactStore(connect(ROLES[2])).quarantine_revision("acme") == 1


def test_transfer_tolerates_column_drift_behind_a_tracked_revision(role_database):
    """A future migration that adds or drops a column must not block every
    deployment that is behind head: a tracked but
    behind-head table's column mismatch is upgrade_database()'s job to
    reconcile, not _transfer_known_tables()'s job to reject."""
    from alembic import command
    from sediment_core.postgres_migrations import _alembic_config

    url, connect = role_database
    bootstrap = connect()
    with bootstrap.connect() as connection:
        config = _alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0008_session_commit_observations")
    with bootstrap.begin() as connection:
        # Simulate the column a future migration between 0008 and head would
        # add: this deployment's live "pushes" table lacks it.
        connection.exec_driver_sql("ALTER TABLE pushes DROP COLUMN forced")
    with bootstrap.connect() as connection:
        role_module()._transfer_known_tables(connection)  # must not raise
        connection.commit()


def test_untracked_table_still_gets_the_strict_column_check(role_database):
    """Without any alembic_version row, nothing will reconcile a column
    mismatch, so the pre-migration check stays strict for that case —
    the one covered separately by
    test_provisioning_does_not_adopt_an_unrecognized_table_shape."""
    url, connect = role_database
    with connect().begin() as connection:
        connection.exec_driver_sql("CREATE TABLE pushes (id int)")
    with connect().connect() as connection:
        with pytest.raises(
            role_module().DatabasePrivilegeError, match="unexpected columns"
        ):
            role_module()._transfer_known_tables(connection)


def test_provisioning_catches_unreconciled_column_drift_after_migration(
    role_database,
):
    """Column drift that migration does not fix is still caught — with a
    named error instead of a raw grant-time SQL error — just after
    upgrade_database() runs instead of before it."""
    from alembic import command
    from sediment_core.postgres_migrations import _alembic_config

    url, connect = role_database
    bootstrap = connect()
    with bootstrap.connect() as connection:
        config = _alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0008_session_commit_observations")
    with bootstrap.begin() as connection:
        # No migration between 0008 and head restores this column, so
        # provisioning must still fail — now from _verify_known_columns()
        # after migration, not from the premature pre-migration check.
        connection.exec_driver_sql("ALTER TABLE pushes DROP COLUMN forced")
    with pytest.raises(
        role_module().DatabasePrivilegeError, match="unexpected columns"
    ):
        provision(url)


def test_operator_bulk_quarantine_dry_run_and_apply(role_database):
    url, connect = role_database
    provision(url)
    seed_every_fact(connect(ROLES[1]))
    store = FactStore(connect(ROLES[2]))
    assert (
        store.quarantine_inference_calls_where("acme", reason="review", dry_run=True)
        == 1
    )
    assert store.quarantine_revision("acme") == 0
    assert (
        store.quarantine_inference_calls_where("acme", reason="review", dry_run=False)
        == 1
    )
    assert store.quarantine_revision("acme") == 1


def test_cli_provision_success_and_unprivileged_migrator(
    role_database, monkeypatch, capsys
):
    from sediment_cli.cli import main

    url, connect = role_database
    monkeypatch.delenv("SEDIMENT_ORG_ID", raising=False)
    monkeypatch.setenv("SEDIMENT_BOOTSTRAP_DATABASE_URL", url)
    for role in ROLES:
        monkeypatch.setenv(role.upper() + "_PASSWORD", PASSWORDS[role])
    assert main(["db", "provision"]) == 0
    assert capsys.readouterr().out == "database roles provisioned and schema upgraded\n"
    migrator = connect(ROLES[0])
    denied(migrator, "CREATE ROLE forbidden")
    denied(migrator, "CREATE DATABASE forbidden")
    denied(migrator, f'SET ROLE "{make_url(url).username}"')


@pytest.mark.parametrize(
    "passwords",
    [
        ("", "valid-two", "valid-three"),
        ("same", "same", "different"),
        ("valid-one", "nul\x00value", "valid-three"),
    ],
)
def test_invalid_role_secrets_are_rejected_without_exposure(role_database, passwords):
    url, _ = role_database
    with pytest.raises(
        role_module().DatabasePrivilegeError, match="distinct nonempty"
    ) as failure:
        role_module().provision_database(
            url,
            migrator_password=passwords[0],
            runtime_password=passwords[1],
            operator_password=passwords[2],
        )
    assert all(not secret or secret not in str(failure.value) for secret in passwords)


@pytest.mark.parametrize("suffix", ["", "?options=-c%20role%3Dsediment"])
def test_provisioning_refuses_implicit_database_or_identity_overrides(
    role_database, suffix
):
    url, _ = role_database
    parsed = make_url(url)
    target = (
        parsed._replace(database=None).render_as_string(hide_password=False)
        if not suffix
        else url + suffix
    )
    with pytest.raises(
        role_module().DatabasePrivilegeError,
        match="explicit host and database" if not suffix else "URL options",
    ):
        provision(target)


def test_provisioning_does_not_adopt_an_unrecognized_table_shape(role_database):
    url, connect = role_database
    with connect().begin() as connection:
        connection.exec_driver_sql("CREATE TABLE inference_calls (unrelated int)")
    with pytest.raises(role_module().DatabasePrivilegeError):
        provision(url)
    with connect().connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT tableowner=current_user FROM pg_tables WHERE tablename='inference_calls'"
        ).scalar_one()


def test_validator_does_not_exclude_user_schemas_with_pg_prefix(role_database):
    url, connect = role_database
    provision(url)
    with connect().begin() as connection:
        connection.exec_driver_sql(
            "CREATE SCHEMA pgcustom; GRANT CREATE ON SCHEMA pgcustom TO sediment_runtime"
        )
    with pytest.raises(role_module().DatabasePrivilegeError):
        role_module().validate_runtime_privileges(connect(ROLES[1]))

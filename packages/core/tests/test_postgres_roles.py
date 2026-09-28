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
# The role contract needs these cluster-wide roles absent, and any real
# `sediment server` test provisions them. CI runs `cluster_roles` alone before
# every other pass.
pytestmark = pytest.mark.cluster_roles
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


def mutation_statements(administrator, role):
    statements = [
        *(f'UPDATE "{table}" SET org_id=org_id' for table in FactTable),
        *(f'DELETE FROM "{table}"' for table in FactTable),
        *(f'TRUNCATE "{table}"' for table in FactTable),
        "UPDATE sessions SET session_id=session_id",
        "UPDATE sessions SET org_id=org_id",
        "DELETE FROM sessions",
        "UPDATE inference_call_aliases SET call_id=call_id",
        "DELETE FROM inference_call_aliases",
        "TRUNCATE inference_call_aliases",
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
        f'SET ROLE "{administrator}"',
    ]
    if role == ROLES[1]:
        statements += [
            "INSERT INTO fact_quarantine DEFAULT VALUES",
            "SELECT nextval('fact_quarantine_quarantine_revision_seq')",
        ]
    else:
        statements += [
            f'INSERT INTO "{table}" DEFAULT VALUES'
            for table in [*FactTable, "sessions", "inference_call_aliases"]
        ]
    return statements


@pytest.mark.parametrize("role", ROLES[1:])
def test_fact_mutation_and_administration_fail(role_database, role):
    url, connect = role_database
    provision(url)
    engine = connect(role)
    for statement in mutation_statements(make_url(url).username, role):
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


ADMIN = "sediment_test_admin"
ADMIN_PASSWORD = "admin-test-secret"


@pytest.fixture
def managed_admin(role_admin):
    """A LOGIN CREATEROLE CREATEDB administrator that isn't a superuser.

    Earlier tests leave superuser-created roles behind, which this
    administrator could not manage; start without them.
    """
    with role_admin.connect() as connection:
        for role in ROLES:
            connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')
        connection.exec_driver_sql(
            f"CREATE ROLE {ADMIN} LOGIN CREATEROLE CREATEDB PASSWORD '{ADMIN_PASSWORD}'"
        )
    try:
        yield ADMIN
    finally:
        with role_admin.connect() as connection:
            for database in (
                connection.exec_driver_sql(
                    "SELECT datname FROM pg_database "
                    f"WHERE datdba=(SELECT oid FROM pg_roles WHERE rolname='{ADMIN}')"
                )
                .scalars()
                .all()
            ):
                connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
            connection.exec_driver_sql(f"DROP OWNED BY {ADMIN}")
            connection.exec_driver_sql(f"DROP ROLE {ADMIN}")


def check(url):
    return role_module().check_database(url)


def failures(url):
    return [str(failure) for failure in check(url).failures]


def test_check_passes_for_every_identity_after_provisioning(role_database):
    url, _ = role_database
    before = check(url)
    assert before.identity == make_url(url).username
    assert before.revision.value == "absent"
    # A fresh database grants PUBLIC CONNECT and TEMPORARY until provisioning.
    assert any(
        str(failure).startswith(f'database "{make_url(url).database}" grants PUBLIC')
        for failure in before.failures
    )
    provision(url)
    for target in (url, *(role_url(url, role) for role in ROLES)):
        result = check(target)
        assert result.failures == (), result.failures
        assert result.revision.value == "at_head"


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        (
            'REVOKE CONNECT ON DATABASE "{database}" FROM sediment_runtime',
            'doesn\'t grant "sediment_runtime" CONNECT; fix: GRANT CONNECT ON '
            'DATABASE "{database}" TO "sediment_runtime"',
        ),
        (
            "REVOKE CREATE ON SCHEMA public FROM sediment_migrator",
            'schema public doesn\'t grant "sediment_migrator" CREATE; fix: GRANT '
            'CREATE ON SCHEMA public TO "sediment_migrator"',
        ),
        (
            'GRANT TEMPORARY ON DATABASE "{database}" TO PUBLIC',
            'database "{database}" grants PUBLIC TEMPORARY; fix: REVOKE ALL ON '
            'DATABASE "{database}" FROM PUBLIC',
        ),
        (
            "GRANT pg_read_all_data TO sediment_runtime",
            'role "sediment_runtime" is a member of "pg_read_all_data"; fix: REVOKE '
            '"pg_read_all_data" FROM "sediment_runtime" GRANTED BY',
        ),
        (
            "ALTER ROLE sediment_migrator CREATEDB",
            'role "sediment_migrator" holds CREATEDB; fix: ALTER ROLE '
            '"sediment_migrator" NOCREATEDB',
        ),
        (
            "ALTER TABLE pushes OWNER TO {administrator}",
            'table "public"."pushes" is owned by "{administrator}", not '
            '"sediment_migrator"; fix: ALTER TABLE "public"."pushes" OWNER TO '
            '"sediment_migrator"',
        ),
    ],
    ids=["connect", "schema-create", "public-temp", "membership", "createdb", "owner"],
)
def test_check_names_each_missing_required_state_and_its_fix(
    role_database, fault, expected
):
    url, connect = role_database
    provision(url)
    names = {
        "database": make_url(url).database,
        "administrator": make_url(url).username,
    }
    with connect().begin() as connection:
        connection.exec_driver_sql(fault.format(**names))
    try:
        for target in (url, role_url(url, ROLES[0])):
            assert any(expected.format(**names) in line for line in failures(target))
        # Read-only: the check reports the fault and leaves it in place.
        assert any(expected.format(**names) in line for line in failures(url))
    finally:
        with connect().begin() as connection:
            connection.exec_driver_sql(
                "ALTER ROLE sediment_migrator NOCREATEDB; "
                "REVOKE pg_read_all_data FROM sediment_runtime"
            )


def test_check_reports_every_failure_including_a_provider_membership(role_database):
    url, connect = role_database
    provision(url)
    provider = "sediment_test_rds_iam"
    with connect().begin() as connection:
        connection.exec_driver_sql(
            f"CREATE ROLE {provider}; GRANT {provider} TO sediment_runtime; "
            "ALTER ROLE sediment_operator CREATEROLE; "
            "GRANT UPDATE (raw) ON inference_calls TO sediment_runtime"
        )
    try:
        lines = failures(role_url(url, ROLES[0]))
        assert any(f'is a member of "{provider}"' in line for line in lines)
        assert any(
            line.startswith('role "sediment_operator" holds CREATEROLE')
            for line in lines
        )
        assert any(
            'holds UPDATE on columns ("raw") of "public"."inference_calls"' in line
            for line in lines
        )
        with pytest.raises(role_module().DatabasePrivilegeError, match=provider):
            role_module().validate_runtime_privileges(connect(ROLES[1]))
    finally:
        with connect().begin() as connection:
            connection.exec_driver_sql(
                f"DROP ROLE {provider}; ALTER ROLE sediment_operator NOCREATEROLE"
            )


def test_check_reports_provisioning_preconditions_for_an_administrator(
    role_database, managed_admin
):
    url, connect = role_database
    provision(url)
    with connect().begin() as connection:
        connection.exec_driver_sql(
            f'GRANT CONNECT ON DATABASE "{make_url(url).database}" TO {managed_admin}'
        )
    target = (
        make_url(url)
        .set(username=managed_admin, password=ADMIN_PASSWORD)
        .render_as_string(hide_password=False)
    )
    lines = failures(target)
    subject = f'administrator "{managed_admin}"'
    assert any(
        line.startswith(f"{subject} lacks the privileges of the owner of database")
        for line in lines
    )
    for role in ROLES:
        assert any(
            line.startswith(
                f'role "{role}" doesn\'t grant {subject} the ADMIN option; fix: '
                f'GRANT "{role}" TO "{managed_admin}" WITH ADMIN OPTION'
            )
            and "administrator-provisioned roles" in line
            for line in lines
        )


def test_cli_check_reports_failures_without_credentials(role_database, capsys):
    from sediment_cli.cli import main

    url, connect = role_database
    provision(url)
    migrator = role_url(url, ROLES[0])
    assert main(["db", "check", "--database-url", migrator]) == 0
    assert capsys.readouterr().out == (
        'database check as "sediment_migrator": passed (schema at_head)\n'
    )
    with connect().begin() as connection:
        connection.exec_driver_sql("GRANT DELETE ON pushes TO sediment_operator")
    assert main(["db", "check", "--database-url", migrator]) == 1
    captured = capsys.readouterr()
    assert (
        '  role "sediment_operator" holds DELETE on "public"."pushes"; '
        "fix: run `sediment db upgrade` as the migrator\n"
    ) in captured.out
    assert captured.err.endswith("1 database check failed\n")
    assert PASSWORDS[ROLES[0]] not in captured.out + captured.err


def test_check_before_migration_reports_reachable_unrelated_relations(role_database):
    url, connect = role_database
    provision(url)
    with connect().begin() as connection:
        # Back to an unmigrated database whose roles and grants exist.
        for table in [*metadata.tables, "alembic_version"]:
            connection.exec_driver_sql(f'DROP TABLE "{table}" CASCADE')
        connection.exec_driver_sql(
            "CREATE TABLE stray (id int); GRANT SELECT ON stray TO PUBLIC"
        )
    result = check(role_url(url, ROLES[0]))
    assert result.revision.value == "absent"
    assert [str(failure) for failure in result.failures] == [
        line
        for role in ROLES[1:]
        for line in (
            f'role "{role}" holds SELECT on "public"."stray"; fix: REVOKE ALL ON '
            f'TABLE "public"."stray" FROM PUBLIC, "{role}"',
            f'role "{role}" holds SELECT on columns ("id") of "public"."stray"; '
            f'fix: REVOKE ALL ("id") ON TABLE "public"."stray" FROM PUBLIC, "{role}"',
        )
    ]


def test_check_on_a_behind_schema_names_only_the_revision(role_database):
    url, connect = role_database
    provision(url)
    with connect().begin() as connection:
        connection.exec_driver_sql(
            "UPDATE alembic_version SET version_num='0010_repository_identity'"
        )
    result = check(role_url(url, ROLES[0]))
    assert result.revision.value == "behind"
    assert [str(failure) for failure in result.failures] == [
        "database schema revision is behind; the supported head is "
        "0011_inference_call_aliases; "
        "fix: run `sediment db upgrade` with the release that supports this schema"
    ]


def test_check_as_the_owning_administrator_reports_an_unreadable_schema(
    role_database, managed_admin, capsys
):
    from sediment_cli.cli import main

    url, connect = role_database
    provision(url)
    with connect().begin() as connection:
        connection.exec_driver_sql(
            f'ALTER DATABASE "{make_url(url).database}" OWNER TO {managed_admin}'
        )
    target = (
        make_url(url)
        .set(username=managed_admin, password=ADMIN_PASSWORD)
        .render_as_string(hide_password=False)
    )
    try:
        result = check(target)
        # Only Sediment's roles may read alembic_version; the migrator's check
        # covers table grants.
        assert result.revision is None
        assert all(
            str(failure).startswith((f'administrator "{managed_admin}"', "role "))
            for failure in result.failures
        ), result.failures
        main(["db", "check", "--database-url", target])
        assert "schema unreadable as this identity" in capsys.readouterr().out
    finally:
        # The administrator fixture drops databases it owns; this one isn't.
        with connect().begin() as connection:
            connection.exec_driver_sql(
                f'ALTER DATABASE "{make_url(url).database}" OWNER TO CURRENT_USER'
            )


def test_upgrade_as_migrator_grants_a_table_that_alembic_left_ungranted(
    role_database, capsys
):
    """Alembic creates a table without grants; the API refuses until the
    migrate step grants it. Revoking as the owner stands in for that new table,
    because the head revision is forward-only."""
    from sediment_cli.cli import main

    url, connect = role_database
    provision(url)
    migrator = role_url(url, ROLES[0])
    with connect(ROLES[0]).begin() as connection:
        connection.exec_driver_sql(
            "REVOKE ALL ON inference_call_aliases "
            "FROM sediment_runtime, sediment_operator"
        )
    with pytest.raises(
        role_module().DatabasePrivilegeError, match="inference_call_aliases"
    ):
        role_module().validate_runtime_privileges(connect(ROLES[1]))
    assert main(["db", "upgrade", "--database-url", migrator]) == 0
    assert capsys.readouterr().out.endswith("grants applied and roles validated\n")
    role_module().validate_runtime_privileges(connect(ROLES[1]))
    assert check(migrator).failures == ()


def test_upgrade_as_migrator_names_every_state_that_grants_cannot_fix(
    role_database, capsys
):
    from sediment_cli.cli import main

    url, connect = role_database
    provision(url)
    database = make_url(url).database
    with connect().begin() as connection:
        connection.exec_driver_sql(
            "GRANT pg_read_all_data TO sediment_runtime; "
            f'REVOKE CONNECT ON DATABASE "{database}" FROM sediment_operator'
        )
    try:
        with pytest.raises(role_module().DatabasePrivilegeError) as failure:
            role_module().migrate_database(role_url(url, ROLES[0]))
        message = str(failure.value)
        assert 'role "sediment_runtime" is a member of "pg_read_all_data"' in message
        assert f'GRANT CONNECT ON DATABASE "{database}" TO "sediment_operator"' in (
            message
        )
        assert main(["db", "upgrade", "--database-url", role_url(url, ROLES[0])]) == 1
        assert "pg_read_all_data" in capsys.readouterr().err
    finally:
        with connect().begin() as connection:
            connection.exec_driver_sql("REVOKE pg_read_all_data FROM sediment_runtime")


def test_upgrade_refuses_another_identity_on_a_provisioned_database(role_database):
    url, connect = role_database
    provision(url)
    with pytest.raises(
        role_module().DatabasePrivilegeError,
        match='run `sediment db upgrade` as "sediment_migrator"',
    ):
        role_module().migrate_database(url)
    # The refusal comes before Alembic takes the lock or changes anything.
    assert check(role_url(url, ROLES[0])).failures == ()


@pytest.fixture
def admin_database(role_admin, managed_admin):
    """A dedicated database that the non-superuser administrator owns."""
    name = f"sediment_admin_test_{uuid4().hex}"
    with role_admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}" OWNER {managed_admin}')
    url = role_admin.url.set(database=name).render_as_string(hide_password=False)
    admin_url = (
        make_url(url)
        .set(username=managed_admin, password=ADMIN_PASSWORD)
        .render_as_string(hide_password=False)
    )
    engines = []

    def connect(role=None, password=None):
        target = url
        if role:
            target = (
                make_url(url)
                .set(username=role, password=password or PASSWORDS[role])
                .render_as_string(hide_password=False)
            )
        engine = create_engine(target, poolclass=NullPool)
        engines.append(engine)
        return engine

    try:
        yield admin_url, connect
    finally:
        for engine in engines:
            engine.dispose()
        with role_admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')


def rotated(passwords):
    return {role: f"rotated-{secret}" for role, secret in passwords.items()}


def logs_in(engine):
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except DBAPIError:
        return False
    return True


def test_non_superuser_administrator_provisions_migrates_and_rotates(
    admin_database, managed_admin
):
    admin_url, connect = admin_database
    provision(admin_url)
    assert check(role_url(admin_url, ROLES[0])).failures == ()
    assert check(admin_url).failures == ()
    runtime = connect(ROLES[1])
    role_module().validate_runtime_privileges(runtime)
    seed_every_fact(runtime)
    for role in ROLES[1:]:
        for statement in mutation_statements(managed_admin, role):
            denied(connect(role), statement)
    before = snapshot(connect(ROLES[2]))

    fresh = rotated(PASSWORDS)
    role_module().provision_database(
        admin_url,
        migrator_password=fresh[ROLES[0]],
        runtime_password=fresh[ROLES[1]],
        operator_password=fresh[ROLES[2]],
    )
    assert snapshot(connect(ROLES[2], fresh[ROLES[2]])) == before
    for role in ROLES:
        assert not logs_in(connect(role))
        assert logs_in(connect(role, fresh[role]))
    role_module().validate_runtime_privileges(connect(ROLES[1], fresh[ROLES[1]]))


def test_provisioning_revokes_a_membership_granted_by_the_administrator(
    admin_database, managed_admin
):
    admin_url, connect = admin_database
    provision(admin_url)
    with connect(managed_admin, ADMIN_PASSWORD).begin() as connection:
        connection.exec_driver_sql(
            "CREATE ROLE sediment_test_parent; "
            "GRANT sediment_test_parent TO sediment_runtime"
        )
    try:
        provision(admin_url)
        assert check(admin_url).failures == ()
    finally:
        with connect(managed_admin, ADMIN_PASSWORD).begin() as connection:
            connection.exec_driver_sql("DROP ROLE sediment_test_parent")


def test_provisioning_refuses_a_role_created_by_another_administrator(
    admin_database, managed_admin, role_admin
):
    admin_url, connect = admin_database
    with role_admin.connect() as connection:
        connection.exec_driver_sql("CREATE ROLE sediment_operator")
    with pytest.raises(role_module().DatabasePrivilegeError) as failure:
        provision(admin_url)
    message = str(failure.value)
    assert (
        f'role "sediment_operator" doesn\'t grant administrator "{managed_admin}" '
        f'the ADMIN option; fix: GRANT "sediment_operator" TO "{managed_admin}" '
        "WITH ADMIN OPTION"
    ) in message
    assert "administrator-provisioned roles" in message
    with role_admin.connect() as connection:
        created = connection.execute(
            text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:roles)"),
            {"roles": list(ROLES)},
        ).scalars()
        assert set(created) == {"sediment_operator"}, "changed roles before failing"


def test_provisioning_refuses_a_membership_it_cannot_revoke(
    admin_database, managed_admin, role_admin
):
    admin_url, connect = admin_database
    provision(admin_url)
    with role_admin.connect() as connection:
        connection.exec_driver_sql("GRANT pg_read_all_data TO sediment_runtime")
    with pytest.raises(role_module().DatabasePrivilegeError) as failure:
        role_module().provision_database(
            admin_url,
            **{
                f"{role.removeprefix('sediment_')}_password": secret
                for role, secret in rotated(PASSWORDS).items()
            },
        )
    assert (
        'role "sediment_runtime" is a member of "pg_read_all_data" through a grant '
        f'by "postgres" that administrator "{managed_admin}" can\'t revoke'
    ) in str(failure.value)
    # Nothing changed: the old passwords still log in.
    assert all(logs_in(connect(role)) for role in ROLES)


def test_provisioning_refuses_a_database_the_administrator_does_not_own(
    role_database, managed_admin
):
    url, connect = role_database
    database = make_url(url).database
    with connect().begin() as connection:
        connection.exec_driver_sql(
            f'GRANT CONNECT ON DATABASE "{database}" TO {managed_admin}'
        )
    target = (
        make_url(url)
        .set(username=managed_admin, password=ADMIN_PASSWORD)
        .render_as_string(hide_password=False)
    )
    with pytest.raises(
        role_module().DatabasePrivilegeError,
        match=(
            f'lacks the privileges of the owner of database "{database}"; fix: '
            f'ALTER DATABASE "{database}" OWNER TO "{managed_admin}"'
        ),
    ):
        provision(target)
    with connect().connect() as connection:
        assert not connection.execute(
            text("SELECT count(*) FROM pg_roles WHERE rolname = ANY(:roles)"),
            {"roles": list(ROLES)},
        ).scalar_one()

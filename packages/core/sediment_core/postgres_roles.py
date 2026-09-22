# SPDX-License-Identifier: AGPL-3.0-or-later
"""Provision and verify the fixed roles of one dedicated Sediment database."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from .models import FactTable
from .postgres_engine import DatabaseOperationError, create_postgres_engine
from .postgres_migrations import upgrade_database
from .postgres_schema import metadata

if TYPE_CHECKING:
    from psycopg import sql

MIGRATOR_ROLE = "sediment_migrator"
RUNTIME_ROLE = "sediment_runtime"
OPERATOR_ROLE = "sediment_operator"
SESSION_UPDATE_COLUMNS = frozenset(
    {"first_observed_at", "last_observed_at", "user_id", "user_id_conflict"}
)
_PROVISION_LOCK = 7_315_324_899_385_581_413


class DatabasePrivilegeError(DatabaseOperationError):
    """A credential-free deployment permission failure."""


def _tables() -> dict[str, tuple[str, ...]]:
    if set(metadata.tables) != {
        "sessions",
        "fact_quarantine",
        "inference_call_aliases",
        *FactTable,
    }:
        raise DatabasePrivilegeError("database permission coverage differs from schema")
    return {
        **{name: tuple(table.c.keys()) for name, table in metadata.tables.items()},
        "alembic_version": ("version_num",),
    }


def _ddl(connection: Connection, template: str, *parts: sql.Composable) -> None:
    from psycopg import sql

    # psycopg quotes identifiers and SCRAM verifiers without SQLAlchemy SQL logging.
    connection.connection.driver_connection.execute(sql.SQL(template).format(*parts))


def _sequence(connection: Connection) -> str:
    value = connection.exec_driver_sql(
        "SELECT pg_get_serial_sequence('public.fact_quarantine', 'quarantine_revision')"
    ).scalar_one()
    if value is None:
        raise DatabasePrivilegeError("quarantine identity sequence is missing")
    return value


def _reconcile_roles(connection: Connection, passwords: dict[str, str]) -> None:
    from psycopg import sql

    for role, password in passwords.items():
        identifier = sql.Identifier(role)
        exists = connection.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
        ).scalar_one_or_none()
        if exists is None:
            _ddl(connection, "CREATE ROLE {}", identifier)
        driver = connection.connection.driver_connection
        verifier = driver.pgconn.encrypt_password(
            password.encode("utf-8"), role.encode("ascii"), b"scram-sha-256"
        ).decode("ascii")
        _ddl(
            connection,
            "ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {} VALID UNTIL 'infinity'",
            identifier,
            sql.Literal(verifier),
        )
        _ddl(connection, "ALTER ROLE {} RESET ALL", identifier)
        database = connection.exec_driver_sql("SELECT current_database()").scalar_one()
        _ddl(
            connection,
            "ALTER ROLE {} IN DATABASE {} RESET ALL",
            identifier,
            sql.Identifier(database),
        )
        _ddl(connection, "ALTER ROLE {} SET search_path = public", identifier)
        memberships = (
            connection.execute(
                text(
                    "SELECT parent.rolname FROM pg_auth_members m "
                    "JOIN pg_roles parent ON parent.oid=m.roleid "
                    "JOIN pg_roles member ON member.oid=m.member "
                    "WHERE member.rolname=:role"
                ),
                {"role": role},
            )
            .scalars()
            .all()
        )
        for parent in memberships:
            _ddl(
                connection,
                "REVOKE {} FROM {} CASCADE",
                sql.Identifier(parent),
                identifier,
            )


def _verify_known_columns(connection: Connection) -> None:
    """Every known table's live columns match head exactly.

    Callers run this after ``upgrade_database()``, once migration has had a
    chance to reconcile a table that was behind head. Called too early, an
    ordinary future column-adding migration would raise here on every
    deployment that has not yet applied it.
    """
    for name, columns in _tables().items():
        actual = {
            column["name"]
            for column in inspect(connection).get_columns(name, schema="public")
        }
        if actual != set(columns):
            raise DatabasePrivilegeError("known database object has unexpected columns")


def _transfer_known_tables(connection: Connection) -> None:
    from psycopg import sql

    bootstrap = connection.exec_driver_sql("SELECT current_user").scalar_one()
    # A table with no alembic_version at all predates migration tracking, so
    # nothing will reconcile its shape — hold it to the head shape exactly.
    # A tracked table may legitimately be behind head; upgrade_database()
    # (run by the caller right after this) reconciles it, and
    # _verify_known_columns() re-checks the exact shape once it has.
    tracked = inspect(connection).has_table("alembic_version")
    for name, columns in _tables().items():
        owner = connection.execute(
            text(
                "SELECT pg_get_userbyid(c.relowner) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' AND c.relname=:name AND c.relkind='r'"
            ),
            {"name": name},
        ).scalar_one_or_none()
        if owner is None:
            continue
        if owner not in {bootstrap, MIGRATOR_ROLE}:
            raise DatabasePrivilegeError(
                "known database object has unexpected ownership"
            )
        if not tracked:
            actual = {
                column["name"]
                for column in inspect(connection).get_columns(name, schema="public")
            }
            if actual != set(columns):
                raise DatabasePrivilegeError(
                    "known database object has unexpected columns"
                )
        # ALTER TABLE also transfers its indexes and owned identity sequences.
        _ddl(
            connection,
            "ALTER TABLE {} OWNER TO {}",
            sql.Identifier("public", name),
            sql.Identifier(MIGRATOR_ROLE),
        )


def _apply_grants(connection: Connection) -> None:
    from psycopg import sql

    for name, columns in _tables().items():
        table = sql.Identifier("public", name)
        column_list = sql.SQL(", ").join(map(sql.Identifier, columns))
        for role in ("PUBLIC", RUNTIME_ROLE, OPERATOR_ROLE):
            grantee = sql.SQL("PUBLIC") if role == "PUBLIC" else sql.Identifier(role)
            _ddl(connection, "REVOKE ALL ON TABLE {} FROM {} CASCADE", table, grantee)
            # Table revocation does not revoke older column-level privileges.
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                _ddl(
                    connection,
                    "REVOKE {} ({}) ON {} FROM {} CASCADE",
                    sql.SQL(privilege),
                    column_list,
                    table,
                    grantee,
                )
        for role in (RUNTIME_ROLE, OPERATOR_ROLE):
            _ddl(connection, "GRANT SELECT ON {} TO {}", table, sql.Identifier(role))
        if name in {*FactTable, "sessions", "inference_call_aliases"}:
            _ddl(
                connection,
                "GRANT INSERT ON {} TO {}",
                table,
                sql.Identifier(RUNTIME_ROLE),
            )
        if name == "fact_quarantine":
            _ddl(
                connection,
                "GRANT INSERT ON {} TO {}",
                table,
                sql.Identifier(OPERATOR_ROLE),
            )
    _ddl(
        connection,
        "GRANT UPDATE ({}) ON public.sessions TO {}",
        sql.SQL(", ").join(map(sql.Identifier, sorted(SESSION_UPDATE_COLUMNS))),
        sql.Identifier(RUNTIME_ROLE),
    )
    sequence = _sequence(connection)
    # Resolve the catalog result as regclass, then quote its actual namespace/name.
    schema, name = connection.execute(
        text(
            "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=CAST(:sequence AS regclass)"
        ),
        {"sequence": sequence},
    ).one()
    identifier = sql.Identifier(schema, name)
    _ddl(
        connection,
        "REVOKE ALL ON SEQUENCE {} FROM PUBLIC, {}, {} CASCADE",
        identifier,
        sql.Identifier(RUNTIME_ROLE),
        sql.Identifier(OPERATOR_ROLE),
    )
    _ddl(
        connection,
        "GRANT USAGE ON SEQUENCE {} TO {}",
        identifier,
        sql.Identifier(OPERATOR_ROLE),
    )


def provision_database(
    bootstrap_database_url: str,
    *,
    migrator_password: str,
    runtime_password: str,
    operator_password: str,
) -> None:
    """Reconcile roles, adopt known legacy tables, migrate, then grant access.

    The caller stops services before provisioning. Only this one-shot operation
    receives the bootstrap credential. Repeating it also rotates role passwords.
    Unknown objects are never transferred or granted access.
    """
    engine = create_postgres_engine(bootstrap_database_url)
    from psycopg import sql

    passwords = {
        MIGRATOR_ROLE: migrator_password,
        RUNTIME_ROLE: runtime_password,
        OPERATOR_ROLE: operator_password,
    }
    try:
        if not engine.url.host or not engine.url.database:
            raise DatabasePrivilegeError(
                "database provisioning requires an explicit host and database"
            )
        if (
            any(not value or "\x00" in value for value in passwords.values())
            or len(set(passwords.values())) != 3
            or engine.url.password in passwords.values()
        ):
            raise DatabasePrivilegeError(
                "database roles require distinct nonempty passwords"
            )
        # Driver URL parameters must not override the target or managed identity.
        if set(engine.url.query) - {
            "sslmode",
            "sslrootcert",
            "sslcert",
            "sslkey",
            "sslcrl",
            "sslcrldir",
            "channel_binding",
        }:
            raise DatabasePrivilegeError(
                "unsupported database provisioning URL options"
            )
        with engine.connect() as connection:
            admin = connection.exec_driver_sql(
                "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
            ).scalar_one()
            if (
                not admin
                or connection.exec_driver_sql("SELECT current_user").scalar_one()
                in passwords
            ):
                raise DatabasePrivilegeError(
                    "database provisioning requires the bootstrap administrator"
                )
            if not connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": _PROVISION_LOCK}
            ).scalar_one():
                raise DatabasePrivilegeError("database provisioning is already running")
            connection.commit()
            try:
                _reconcile_roles(connection, passwords)
                database = connection.exec_driver_sql(
                    "SELECT current_database()"
                ).scalar_one()
                _ddl(
                    connection,
                    "REVOKE ALL ON DATABASE {} FROM PUBLIC, {}, {}, {}",
                    sql.Identifier(database),
                    *map(sql.Identifier, passwords),
                )
                for role in passwords:
                    _ddl(
                        connection,
                        "GRANT CONNECT ON DATABASE {} TO {}",
                        sql.Identifier(database),
                        sql.Identifier(role),
                    )
                _ddl(
                    connection,
                    "REVOKE ALL ON SCHEMA public FROM PUBLIC, {}, {}, {}",
                    *map(sql.Identifier, passwords),
                )
                for role in passwords:
                    _ddl(
                        connection,
                        "GRANT USAGE ON SCHEMA public TO {}",
                        sql.Identifier(role),
                    )
                _ddl(
                    connection,
                    "GRANT CREATE ON SCHEMA public TO {}",
                    sql.Identifier(MIGRATOR_ROLE),
                )
                _transfer_known_tables(connection)
                connection.commit()
                migration_url = engine.url.set(
                    username=MIGRATOR_ROLE, password=migrator_password
                ).render_as_string(hide_password=False)
                upgrade_database(migration_url)
                _verify_known_columns(connection)
                _apply_grants(connection)
                _validate_privileges(connection, RUNTIME_ROLE)
                _validate_privileges(connection, OPERATOR_ROLE)
                connection.commit()
            finally:
                connection.rollback()
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": _PROVISION_LOCK}
                )
                connection.commit()
    except DatabasePrivilegeError:
        raise
    except Exception:
        raise DatabasePrivilegeError("could not provision database roles") from None
    finally:
        engine.dispose()


def _validate_privileges(connection: Connection, role: str) -> None:
    expected = _tables()
    params = {"role": role}
    dangerous_role = connection.execute(
        text(
            "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls "
            "FROM pg_roles WHERE rolname=:role"
        ),
        params,
    ).scalar_one_or_none()
    membership = connection.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname<>:role "
            "AND pg_has_role(:role, oid, 'MEMBER'))"
        ),
        params,
    ).scalar_one()
    if dangerous_role is not False or membership:
        raise DatabasePrivilegeError(
            "database role has privileged attributes or membership"
        )
    if connection.execute(
        text("SELECT has_database_privilege(:role, current_database(), 'CREATE,TEMP')"),
        params,
    ).scalar_one():
        raise DatabasePrivilegeError("database role can create database objects")
    if connection.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname<>'information_schema' AND (has_schema_privilege(:role, oid, 'CREATE') OR pg_has_role(:role, nspowner, 'MEMBER')))"
        ),
        params,
    ).scalar_one():
        raise DatabasePrivilegeError("database role owns or can create in a schema")
    if connection.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND has_function_privilege(:role, p.oid, 'EXECUTE'))"
        ),
        params,
    ).scalar_one():
        raise DatabasePrivilegeError("database role can execute an unexpected function")
    relations = (
        connection.execute(
            text(
                "SELECT c.oid, n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner) AS owner, pg_has_role(:role, c.relowner, 'MEMBER') AS owns FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND c.relkind IN ('r','p','v','m','f','S')"
            ),
            params,
        )
        .mappings()
        .all()
    )
    found = set()
    maintenance = (
        ("MAINTAIN",) if connection.dialect.server_version_info >= (17,) else ()
    )
    sequence = connection.execute(
        text("SELECT CAST(CAST(:name AS regclass) AS oid)"),
        {"name": _sequence(connection)},
    ).scalar_one()
    for relation in relations:
        params = {"role": role, "oid": relation["oid"]}
        if relation["owns"]:
            raise DatabasePrivilegeError("database role owns a relation")
        if relation["relkind"] == "S":
            for permission in ("USAGE", "SELECT", "UPDATE", "USAGE WITH GRANT OPTION"):
                allowed = (
                    role == OPERATOR_ROLE
                    and relation["oid"] == sequence
                    and permission == "USAGE"
                )
                actual = connection.execute(
                    text("SELECT has_sequence_privilege(:role, :oid, :permission)"),
                    {**params, "permission": permission},
                ).scalar_one()
                if actual != allowed:
                    raise DatabasePrivilegeError(
                        "database sequence permissions differ from policy"
                    )
            continue
        name = relation["relname"]
        known = (
            relation["nspname"] == "public"
            and name in expected
            and relation["relkind"] == "r"
        )
        columns = tuple(
            column["name"]
            for column in inspect(connection).get_columns(
                name, schema=relation["nspname"]
            )
        )
        if known:
            if relation["owner"] != MIGRATOR_ROLE:
                raise DatabasePrivilegeError(
                    "application table is not owned by the migrator"
                )
            found.add(name)
            if set(columns) != set(expected[name]):
                raise DatabasePrivilegeError(
                    "database columns differ from permission coverage"
                )
        insert = known and (
            (
                role == RUNTIME_ROLE
                and name in {*FactTable, "sessions", "inference_call_aliases"}
            )
            or (role == OPERATOR_ROLE and name == "fact_quarantine")
        )
        for permission in (
            "SELECT",
            "INSERT",
            "DELETE",
            "TRUNCATE",
            "TRIGGER",
            "REFERENCES",
            "SELECT WITH GRANT OPTION",
            "INSERT WITH GRANT OPTION",
            *maintenance,
        ):
            allowed = (known and permission == "SELECT") or (
                insert and permission == "INSERT"
            )
            actual = connection.execute(
                text("SELECT has_table_privilege(:role, :oid, :permission)"),
                {**params, "permission": permission},
            ).scalar_one()
            if actual != allowed:
                raise DatabasePrivilegeError(
                    "database table permissions differ from policy"
                )
        for column in columns:
            for permission in (
                "SELECT",
                "UPDATE",
                "UPDATE WITH GRANT OPTION",
                "INSERT",
                "INSERT WITH GRANT OPTION",
                "REFERENCES",
                "SELECT WITH GRANT OPTION",
            ):
                allowed = (
                    (known and permission == "SELECT")
                    or (insert and permission == "INSERT")
                    or (
                        known
                        and role == RUNTIME_ROLE
                        and name == "sessions"
                        and column in SESSION_UPDATE_COLUMNS
                        and permission == "UPDATE"
                    )
                )
                actual = connection.execute(
                    text(
                        "SELECT has_column_privilege(:role, :oid, :column, :permission)"
                    ),
                    {**params, "column": column, "permission": permission},
                ).scalar_one()
                if actual != allowed:
                    raise DatabasePrivilegeError(
                        "database column permissions differ from policy"
                    )
    if found != set(expected):
        raise DatabasePrivilegeError("database tables differ from permission coverage")


def validate_runtime_privileges(engine: Engine) -> None:
    """Read-only startup gate; callers explicitly bypass it in development only."""
    try:
        with engine.connect() as connection:
            current, session = connection.exec_driver_sql(
                "SELECT current_user, session_user"
            ).one()
            if current != RUNTIME_ROLE or session != RUNTIME_ROLE:
                raise DatabasePrivilegeError(
                    "API requires the sediment_runtime database role"
                )
            _validate_privileges(connection, RUNTIME_ROLE)
    except DatabasePrivilegeError:
        raise
    except Exception:
        raise DatabasePrivilegeError(
            "could not validate database runtime privileges"
        ) from None

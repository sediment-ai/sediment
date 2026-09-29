# SPDX-License-Identifier: AGPL-3.0-or-later
"""Provision, check, and verify the fixed roles of one dedicated Sediment database."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from .models import FactTable
from .postgres_engine import (
    DatabaseOperationError,
    DatabaseURLValidationError,
    create_postgres_engine,
    database_operation_error,
    verify_minimum_server_version,
)
from .postgres_migrations import (
    HEAD_REVISION,
    RevisionState,
    inspect_connection_revision,
    upgrade_database,
)
from .postgres_schema import metadata

if TYPE_CHECKING:
    from psycopg import sql

MIGRATOR_ROLE = "sediment_migrator"
RUNTIME_ROLE = "sediment_runtime"
OPERATOR_ROLE = "sediment_operator"
ROLES = (MIGRATOR_ROLE, RUNTIME_ROLE, OPERATOR_ROLE)
SESSION_UPDATE_COLUMNS = frozenset(
    {"first_observed_at", "last_observed_at", "user_id", "user_id_conflict"}
)
_PROVISION_LOCK = 7_315_324_899_385_581_413


class DatabasePrivilegeError(DatabaseOperationError):
    """A credential-free deployment permission failure."""


@dataclass(frozen=True)
class PrivilegeFailure:
    """One failed deployment check and the statement that corrects it."""

    subject: str
    problem: str
    fix: str

    def __str__(self) -> str:
        return f"{self.subject} {self.problem}; fix: {self.fix}"


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


def _quote(name: str) -> str:
    """Quote an identifier for a diagnostic's corrective statement."""
    return '"' + name.replace('"', '""') + '"'


def _relation(schema: str, name: str) -> str:
    return f"{_quote(schema)}.{_quote(name)}"


_ROLE_ATTRIBUTES = (
    ("rolsuper", "SUPERUSER"),
    ("rolcreatedb", "CREATEDB"),
    ("rolcreaterole", "CREATEROLE"),
    ("rolreplication", "REPLICATION"),
    ("rolbypassrls", "BYPASSRLS"),
)
# Grants on migrator-owned objects come only from the owner's grant pass.
_REGRANT = "run `sediment db provision`"


def _role_failures(connection: Connection, role: str) -> Iterator[PrivilegeFailure]:
    """Elevated attributes and memberships; a missing role ends every check."""
    subject = f"role {_quote(role)}"
    attributes = (
        connection.execute(
            text(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                "rolbypassrls FROM pg_roles WHERE rolname=:role"
            ),
            {"role": role},
        )
        .mappings()
        .one_or_none()
    )
    if attributes is None:
        yield PrivilegeFailure(subject, "does not exist", _REGRANT)
        return
    held = [name for column, name in _ROLE_ATTRIBUTES if attributes[column]]
    if held:
        yield PrivilegeFailure(
            subject,
            f"holds {', '.join(held)}",
            f"ALTER ROLE {_quote(role)} " + " ".join(f"NO{name}" for name in held),
        )
    if connection.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname<>:role "
            "AND pg_has_role(:role, oid, 'MEMBER'))"
        ),
        {"role": role},
    ).scalar_one():
        grants = connection.execute(
            text(
                "SELECT parent.rolname, grantor.rolname FROM pg_auth_members m "
                "JOIN pg_roles parent ON parent.oid=m.roleid "
                "JOIN pg_roles member ON member.oid=m.member "
                "JOIN pg_roles grantor ON grantor.oid=m.grantor "
                "WHERE member.rolname=:role ORDER BY 1, 2"
            ),
            {"role": role},
        ).all()
        for parent, grantor in grants:
            yield PrivilegeFailure(
                subject,
                f"is a member of {_quote(parent)}",
                f"REVOKE {_quote(parent)} FROM {_quote(role)} "
                f"GRANTED BY {_quote(grantor)}",
            )
        if not grants:
            yield PrivilegeFailure(
                subject, "has the privileges of other roles", "revoke its memberships"
            )


def _privilege_failures(
    connection: Connection, role: str, *, coverage: bool = True
) -> Iterator[PrivilegeFailure]:
    """Yield each departure from the runtime or operator policy, in check order.

    ``_validate_privileges`` raises the first; ``check_database`` reports all.
    ``coverage=False`` checks a schema that isn't at head: Sediment's own
    tables and quarantine sequence wait for the migration, but every other
    reachable relation is still checked.
    """
    expected = _tables()
    params = {"role": role}
    quoted = _quote(role)
    subject = f"role {quoted}"
    missing = False
    for failure in _role_failures(connection, role):
        missing = failure.problem == "does not exist"
        yield failure
    if missing:
        return
    database = connection.exec_driver_sql("SELECT current_database()").scalar_one()
    if connection.execute(
        text("SELECT has_database_privilege(:role, current_database(), 'CREATE,TEMP')"),
        params,
    ).scalar_one():
        yield PrivilegeFailure(
            subject,
            f"can create objects or temporary tables in database {_quote(database)}",
            f"REVOKE CREATE, TEMPORARY ON DATABASE {_quote(database)} "
            f"FROM PUBLIC, {quoted}",
        )
    schemas = connection.execute(
        text(
            "SELECT nspname, has_schema_privilege(:role, oid, 'CREATE') FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname<>'information_schema' AND (has_schema_privilege(:role, oid, 'CREATE') OR pg_has_role(:role, nspowner, 'MEMBER')) ORDER BY 1"
        ),
        params,
    ).all()
    for schema, can_create in schemas:
        yield (
            PrivilegeFailure(
                subject,
                f"can create objects in schema {_quote(schema)}",
                f"REVOKE CREATE ON SCHEMA {_quote(schema)} FROM PUBLIC, {quoted}",
            )
            if can_create
            else PrivilegeFailure(
                subject,
                f"owns schema {_quote(schema)}",
                f"ALTER SCHEMA {_quote(schema)} OWNER TO pg_database_owner",
            )
        )
    functions = (
        connection.execute(
            text(
                "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND has_function_privilege(:role, p.oid, 'EXECUTE') ORDER BY 1"
            ),
            params,
        )
        .scalars()
        .all()
    )
    for function in functions:
        yield PrivilegeFailure(
            subject,
            f"can execute function {function}",
            f"REVOKE EXECUTE ON ROUTINE {function} FROM PUBLIC, {quoted}",
        )
    relations_found = (
        connection.execute(
            text(
                "SELECT c.oid, n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner) AS owner, pg_has_role(:role, c.relowner, 'MEMBER') AS owns FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND c.relkind IN ('r','p','v','m','f','S') ORDER BY n.nspname, c.relname"
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
    sequence = (
        connection.execute(
            text("SELECT CAST(CAST(:name AS regclass) AS oid)"),
            {"name": _sequence(connection)},
        ).scalar_one()
        if coverage
        # Catalog lookup: name resolution would need USAGE on public.
        else connection.exec_driver_sql(
            "SELECT d.objid FROM pg_depend d "
            "JOIN pg_class s ON s.oid=d.objid AND s.relkind='S' "
            "JOIN pg_class t ON t.oid=d.refobjid "
            "JOIN pg_namespace n ON n.oid=t.relnamespace "
            "WHERE d.classid='pg_class'::regclass "
            "AND d.refclassid='pg_class'::regclass AND d.deptype='i' "
            "AND n.nspname='public' AND t.relname='fact_quarantine'"
        ).scalar()
    )
    for relation in relations_found:
        params = {"role": role, "oid": relation["oid"]}
        if not coverage and (
            relation["oid"] == sequence
            or (relation["nspname"] == "public" and relation["relname"] in expected)
        ):
            continue
        target = _relation(relation["nspname"], relation["relname"])
        if relation["owns"]:
            yield PrivilegeFailure(
                subject,
                f"owns relation {target}",
                f"ALTER TABLE {target} OWNER TO {_quote(MIGRATOR_ROLE)}",
            )
            continue
        if relation["relkind"] == "S":
            known_sequence = relation["oid"] == sequence
            drift = []
            for permission in ("USAGE", "SELECT", "UPDATE", "USAGE WITH GRANT OPTION"):
                allowed = (
                    role == OPERATOR_ROLE and known_sequence and permission == "USAGE"
                )
                actual = connection.execute(
                    text("SELECT has_sequence_privilege(:role, :oid, :permission)"),
                    {**params, "permission": permission},
                ).scalar_one()
                if actual != allowed:
                    drift.append(f"{'holds' if actual else 'lacks'} {permission}")
            if drift:
                yield PrivilegeFailure(
                    subject,
                    f"{', '.join(drift)} on sequence {target}",
                    _REGRANT
                    if known_sequence
                    else f"REVOKE ALL ON SEQUENCE {target} FROM PUBLIC, {quoted}",
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
                yield PrivilegeFailure(
                    f"table {target}",
                    f"is owned by {_quote(relation['owner'])}, "
                    f"not {_quote(MIGRATOR_ROLE)}",
                    f"ALTER TABLE {target} OWNER TO {_quote(MIGRATOR_ROLE)}",
                )
            found.add(name)
            if set(columns) != set(expected[name]):
                yield PrivilegeFailure(
                    f"table {target}",
                    "has columns that differ from the supported schema",
                    "run `sediment db upgrade`; if the columns still differ, "
                    "restore the table from a backup",
                )
        insert = known and (
            (
                role == RUNTIME_ROLE
                and name in {*FactTable, "sessions", "inference_call_aliases"}
            )
            or (role == OPERATOR_ROLE and name == "fact_quarantine")
        )
        drift = []
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
                drift.append(f"{'holds' if actual else 'lacks'} {permission}")
        if drift:
            yield PrivilegeFailure(
                subject,
                f"{', '.join(drift)} on {target}",
                _REGRANT
                if known
                else f"REVOKE ALL ON TABLE {target} FROM PUBLIC, {quoted}",
            )
        column_drift: dict[str, list[str]] = {}
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
                    key = f"{'holds' if actual else 'lacks'} {permission}"
                    column_drift.setdefault(key, []).append(column)
        for drift_kind, drifted in column_drift.items():
            listed = ", ".join(map(_quote, drifted))
            yield PrivilegeFailure(
                subject,
                f"{drift_kind} on columns ({listed}) of {target}",
                _REGRANT
                if known
                else f"REVOKE ALL ({listed}) ON TABLE {target} FROM PUBLIC, {quoted}",
            )
    if coverage and found != set(expected):
        yield PrivilegeFailure(
            "database schema",
            "lacks tables " + ", ".join(sorted(set(expected) - found)),
            _REGRANT,
        )


def _validate_privileges(connection: Connection, role: str) -> None:
    failure = next(_privilege_failures(connection, role), None)
    if failure is not None:
        raise DatabasePrivilegeError(str(failure))


def _sediment_roles(connection: Connection) -> set[str]:
    return set(
        connection.execute(
            text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:roles)"),
            {"roles": list(ROLES)},
        )
        .scalars()
        .all()
    )


def _database_failures(connection: Connection) -> Iterator[PrivilegeFailure]:
    """The dedicated database and ``public`` schema grant only the required access."""
    existing = _sediment_roles(connection)
    database, owner = connection.exec_driver_sql(
        "SELECT datname, pg_get_userbyid(datdba) FROM pg_database "
        "WHERE datname=current_database()"
    ).one()
    subject = f"database {_quote(database)}"
    if owner in ROLES:
        yield PrivilegeFailure(
            subject,
            f"is owned by Sediment role {_quote(owner)}",
            f"ALTER DATABASE {_quote(database)} OWNER TO <administrator>",
        )
    grants = connection.exec_driver_sql(
        "SELECT CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) "
        "END, a.privilege_type FROM pg_database d CROSS JOIN LATERAL "
        "aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a "
        "WHERE d.datname=current_database() ORDER BY 1, 2"
    ).all()
    public = [privilege for grantee, privilege in grants if grantee == "PUBLIC"]
    if public:
        yield PrivilegeFailure(
            subject,
            f"grants PUBLIC {', '.join(public)}",
            f"REVOKE ALL ON DATABASE {_quote(database)} FROM PUBLIC",
        )
    for role in ROLES:
        held = {privilege for grantee, privilege in grants if grantee == role}
        if role not in existing:
            continue
        if "CONNECT" not in held:
            yield PrivilegeFailure(
                subject,
                f"doesn't grant {_quote(role)} CONNECT",
                f"GRANT CONNECT ON DATABASE {_quote(database)} TO {_quote(role)}",
            )
        if role == MIGRATOR_ROLE and held - {"CONNECT"}:
            yield PrivilegeFailure(
                subject,
                f"grants {_quote(role)} {', '.join(sorted(held - {'CONNECT'}))}",
                f"REVOKE CREATE, TEMPORARY ON DATABASE {_quote(database)} "
                f"FROM {_quote(role)}",
            )
    grants = connection.exec_driver_sql(
        "SELECT CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) "
        "END, a.privilege_type FROM pg_namespace n CROSS JOIN LATERAL "
        "aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a "
        "WHERE n.nspname='public' ORDER BY 1, 2"
    ).all()
    subject = "schema public"
    if not connection.exec_driver_sql(
        "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='public')"
    ).scalar_one():
        yield PrivilegeFailure(subject, "does not exist", "CREATE SCHEMA public")
        return
    public = [privilege for grantee, privilege in grants if grantee == "PUBLIC"]
    if public:
        yield PrivilegeFailure(
            subject,
            f"grants PUBLIC {', '.join(public)}",
            "REVOKE ALL ON SCHEMA public FROM PUBLIC",
        )
    for role in ROLES:
        held = {privilege for grantee, privilege in grants if grantee == role}
        if role not in existing:
            continue
        needed = ["USAGE", "CREATE"] if role == MIGRATOR_ROLE else ["USAGE"]
        for privilege in needed:
            if privilege not in held:
                yield PrivilegeFailure(
                    subject,
                    f"doesn't grant {_quote(role)} {privilege}",
                    f"GRANT {privilege} ON SCHEMA public TO {_quote(role)}",
                )


def _ownership_failures(connection: Connection) -> Iterator[PrivilegeFailure]:
    """Every existing Sediment table belongs to the migrator."""
    owners = connection.execute(
        text(
            "SELECT c.relname, pg_get_userbyid(c.relowner) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' "
            "AND c.relkind='r' AND c.relname = ANY(:tables) ORDER BY 1"
        ),
        {"tables": list(_tables())},
    ).all()
    for name, owner in owners:
        if owner != MIGRATOR_ROLE:
            target = _relation("public", name)
            yield PrivilegeFailure(
                f"table {target}",
                f"is owned by {_quote(owner)}, not {_quote(MIGRATOR_ROLE)}",
                f"ALTER TABLE {target} OWNER TO {_quote(MIGRATOR_ROLE)}",
            )


def _migrator_failures(connection: Connection) -> Iterator[PrivilegeFailure]:
    """The migrator logs in, holds nothing elevated, and owns Sediment's tables."""
    missing = False
    for failure in _role_failures(connection, MIGRATOR_ROLE):
        missing = failure.problem == "does not exist"
        yield failure
    if missing:
        return
    if not connection.execute(
        text("SELECT rolcanlogin FROM pg_roles WHERE rolname=:role"),
        {"role": MIGRATOR_ROLE},
    ).scalar_one():
        yield PrivilegeFailure(
            f"role {_quote(MIGRATOR_ROLE)}",
            "can't log in",
            f"ALTER ROLE {_quote(MIGRATOR_ROLE)} LOGIN",
        )
    yield from _ownership_failures(connection)


def _provisioning_failures(connection: Connection) -> Iterator[PrivilegeFailure]:
    """What the connected administrator needs before provisioning changes anything.

    ADR 0027: a superuser, or ``CREATEROLE`` with the database owner's
    privileges; ``ADMIN`` on each existing Sediment role; the ability to clear
    each existing role's attributes and memberships.
    """
    administrator = (
        connection.execute(
            text(
                "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                "rolbypassrls FROM pg_roles WHERE rolname=current_user"
            )
        )
        .mappings()
        .one()
    )
    name = administrator["rolname"]
    subject = f"administrator {_quote(name)}"
    database = connection.exec_driver_sql("SELECT current_database()").scalar_one()
    if administrator["rolsuper"]:
        return
    # ponytail: the rolsuper gate stays until capability-based provisioning
    # lands (ADR 0027, step 3); the checks after it are that step's contract.
    yield PrivilegeFailure(
        subject,
        "isn't a superuser, which `sediment db provision` requires",
        "provision as a superuser",
    )
    if not administrator["rolcreaterole"]:
        yield PrivilegeFailure(
            subject,
            "lacks CREATEROLE",
            f"ALTER ROLE {_quote(name)} CREATEROLE",
        )
    if not connection.exec_driver_sql(
        "SELECT pg_has_role(current_user, datdba, 'USAGE') FROM pg_database "
        "WHERE datname=current_database()"
    ).scalar_one():
        yield PrivilegeFailure(
            subject,
            f"lacks the privileges of the owner of database {_quote(database)}",
            f"ALTER DATABASE {_quote(database)} OWNER TO {_quote(name)}",
        )
    if connection.exec_driver_sql(
        "SELECT NOT pg_has_role(current_user, nspowner, 'USAGE') FROM pg_namespace "
        "WHERE nspname='public'"
    ).scalar():
        yield PrivilegeFailure(
            subject,
            "lacks the privileges of the owner of schema public",
            "ALTER SCHEMA public OWNER TO pg_database_owner",
        )
    for role in sorted(_sediment_roles(connection)):
        quoted = _quote(role)
        if not connection.execute(
            text("SELECT pg_has_role(current_user, :role, 'MEMBER WITH ADMIN OPTION')"),
            {"role": role},
        ).scalar_one():
            yield PrivilegeFailure(
                f"role {quoted}",
                f"doesn't grant {subject} the ADMIN option",
                f"GRANT {quoted} TO {_quote(name)} WITH ADMIN OPTION, run by a role "
                f"that holds ADMIN on {quoted}; or switch to administrator-"
                f"provisioned roles with the credentials of {quoted}",
            )
        attributes = (
            connection.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname=:role"
                ),
                {"role": role},
            )
            .mappings()
            .one()
        )
        for column, attribute in _ROLE_ATTRIBUTES:
            if (
                column in attributes
                and attributes[column]
                and not administrator[column]
            ):
                yield PrivilegeFailure(
                    f"role {quoted}",
                    f"holds {attribute}, which only a role with {attribute} can remove",
                    f"ALTER ROLE {quoted} NO{attribute}, run by a superuser",
                )
        grants = connection.execute(
            text(
                "SELECT parent.rolname, grantor.rolname, "
                "pg_has_role(current_user, m.roleid, 'MEMBER WITH ADMIN OPTION') "
                "AND pg_has_role(current_user, m.grantor, 'USAGE') "
                "FROM pg_auth_members m "
                "JOIN pg_roles parent ON parent.oid=m.roleid "
                "JOIN pg_roles member ON member.oid=m.member "
                "JOIN pg_roles grantor ON grantor.oid=m.grantor "
                "WHERE member.rolname=:role ORDER BY 1, 2"
            ),
            {"role": role},
        ).all()
        for parent, grantor, revocable in grants:
            if not revocable:
                yield PrivilegeFailure(
                    f"role {quoted}",
                    f"is a member of {_quote(parent)} through a grant by "
                    f"{_quote(grantor)} that {subject} can't revoke",
                    f"REVOKE {_quote(parent)} FROM {quoted} GRANTED BY "
                    f"{_quote(grantor)}, run by a superuser",
                )
    yield from _ownership_failures(connection)


def _readable_revision(connection: Connection) -> RevisionState | None:
    """The schema revision, or None when this identity can't read it.

    The catalogs answer whether ``public.alembic_version`` exists without
    the search path or ``USAGE`` on ``public``, which an administrator can
    lack; only the migrator, runtime, and operator roles may read the table.
    """
    readable = connection.exec_driver_sql(
        "SELECT has_schema_privilege(n.oid, 'USAGE') "
        "AND has_table_privilege(c.oid, 'SELECT') FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relname='alembic_version'"
    ).scalar()
    if readable is None:
        return RevisionState.ABSENT
    return inspect_connection_revision(connection).state if readable else None


@dataclass(frozen=True)
class DatabaseCheck:
    """One read-only check of a dedicated database, as the connected identity.

    ``revision`` is None when the identity can't read the schema revision;
    the check then skips Sediment's table coverage, which the migrator's
    check covers.
    """

    identity: str
    revision: RevisionState | None
    failures: tuple[PrivilegeFailure, ...]


def check_database(database_url: str) -> DatabaseCheck:
    """Report every failed deployment check without changing the database.

    An identity other than a Sediment role is checked as the administrator
    that would provision this database. An absent schema is the expected
    state before the first migration, so table coverage waits for it.
    """
    engine = create_postgres_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            verify_minimum_server_version(connection)
            identity = connection.exec_driver_sql("SELECT current_user").scalar_one()
            failures: list[PrivilegeFailure] = []
            if identity not in ROLES:
                failures += _provisioning_failures(connection)
            failures += _database_failures(connection)
            failures += _migrator_failures(connection)
            revision = _readable_revision(connection)
            if revision in {RevisionState.BEHIND, RevisionState.AHEAD}:
                failures.append(
                    PrivilegeFailure(
                        "database schema",
                        f"revision is {revision.value}; the supported head is "
                        f"{HEAD_REVISION}",
                        "run `sediment db upgrade` with the release that "
                        "supports this schema",
                    )
                )
            for role in (RUNTIME_ROLE, OPERATOR_ROLE):
                failures += _privilege_failures(
                    connection, role, coverage=revision is RevisionState.AT_HEAD
                )
            connection.rollback()
    except (DatabasePrivilegeError, DatabaseOperationError, DatabaseURLValidationError):
        raise
    except Exception:
        raise database_operation_error("check database", database_url) from None
    finally:
        engine.dispose()
    return DatabaseCheck(identity, revision, tuple(dict.fromkeys(failures)))


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

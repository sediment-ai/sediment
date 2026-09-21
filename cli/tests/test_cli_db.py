# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import pytest

from sediment_cli.cli import main
from sediment_core import postgres_migrations
from sediment_core.postgres_migrations import HEAD_REVISION, MIGRATION_LOCK_KEY


def test_db_status_is_read_only_and_upgrade_is_idempotent(
    postgres_database_factory,
    capsys,
) -> None:
    database_url = postgres_database_factory(migrated=False)

    assert main(["db", "status", "--database-url", database_url]) == 0
    assert capsys.readouterr().out == (
        f"database schema: absent (supported head {HEAD_REVISION})\n"
    )

    assert main(["db", "upgrade", "--database-url", database_url]) == 0
    assert capsys.readouterr().out == f"database schema upgraded to {HEAD_REVISION}\n"
    assert main(["db", "upgrade", "--database-url", database_url]) == 0
    assert capsys.readouterr().out == f"database schema upgraded to {HEAD_REVISION}\n"

    assert main(["db", "status", "--database-url", database_url]) == 0
    assert capsys.readouterr().out == (f"database schema: at_head ({HEAD_REVISION})\n")


def test_db_status_corrupt_script_directory_exits_nonzero(
    postgres_database_factory,
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """`db status` surfaces a broken script directory, not a success exit code.

    Regression for the over-broad ``except Exception:`` around
    ``ScriptDirectory.get_revision``: with a known-behind stored revision and an
    unimportable version file, the CLI must exit 1 (via ``MigrationError``)
    instead of printing ``ahead`` and exiting 0.
    """
    from alembic.config import Config

    database_url = postgres_database_factory(migrated=False)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            connection.execute(
                text(
                    "INSERT INTO alembic_version (version_num) "
                    "VALUES ('0002_retry_linkages')"
                )
            )
    finally:
        engine.dispose()

    script_dir = tmp_path / "alembic"
    versions = script_dir / "versions"
    versions.mkdir(parents=True)
    real_versions = Path(postgres_migrations.__file__).with_name("alembic") / "versions"
    for path in real_versions.glob("*.py"):
        if path.name == "__init__.py":
            continue
        (versions / path.name).write_bytes(path.read_bytes())
    (versions / "0009_corrupt.py").write_text(
        'raise RuntimeError("boom during import of version file")\n'
    )

    def _corrupt_alembic_config() -> Config:
        config = Config()
        config.set_main_option("script_location", str(script_dir))
        return config

    monkeypatch.setattr(postgres_migrations, "_alembic_config", _corrupt_alembic_config)

    assert main(["db", "status", "--database-url", database_url]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: could not inspect database revision\n"


def test_db_upgrade_reads_database_url_from_environment(
    postgres_database_factory,
    monkeypatch,
    capsys,
) -> None:
    database_url = postgres_database_factory(migrated=False)
    monkeypatch.setenv("SEDIMENT_DATABASE_URL", database_url)

    assert main(["db", "upgrade"]) == 0
    assert capsys.readouterr().out == f"database schema upgraded to {HEAD_REVISION}\n"


def test_db_command_validates_database_url_before_application_settings(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("SEDIMENT_DATABASE_URL", raising=False)
    monkeypatch.delenv("SEDIMENT_ORG_ID", raising=False)

    assert main(["db", "upgrade"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("error: set SEDIMENT_DATABASE_URL or pass --database-url\n")


@pytest.mark.parametrize("command", ["status", "upgrade"])
def test_db_invalid_url_preserves_safe_password_escape_hint(
    command: str,
    capsys,
) -> None:
    database_url = "postgresql://user:sentinel-p@ss!word@localhost/sediment"

    assert main(["db", command, "--database-url", database_url]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error: invalid PostgreSQL URL; if the password contains special "
        "characters, percent-encode them\n"
    )
    assert "sentinel-p" not in captured.err
    assert "ss!word" not in captured.err


def test_db_upgrade_connection_failure_never_exposes_credentials(
    postgres_database_factory,
    capsys,
) -> None:
    database_url = postgres_database_factory(migrated=False)
    secret = "sentinel-password-558"
    bad_url = (
        make_url(database_url)
        .set(password=secret)
        .render_as_string(hide_password=False)
    )

    assert main(["db", "upgrade", "--database-url", bad_url]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: could not upgrade database schema\n"
    assert secret not in captured.err
    assert bad_url not in captured.err


def test_db_upgrade_lock_failure_exits_nonzero(
    postgres_database_factory,
    capsys,
) -> None:
    database_url = postgres_database_factory(migrated=False)
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": MIGRATION_LOCK_KEY},
            ).scalar_one()
            assert main(["db", "upgrade", "--database-url", database_url]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == (
            "error: database migration lock is held by another process\n"
        )
    finally:
        engine.dispose()


def test_db_upgrade_migration_failure_exits_nonzero(
    postgres_database_factory,
    capsys,
) -> None:
    database_url = postgres_database_factory(migrated=False)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            connection.execute(
                text("INSERT INTO alembic_version VALUES ('future_9999')")
            )
        assert main(["db", "upgrade", "--database-url", database_url]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "error: could not upgrade database schema\n"
    finally:
        engine.dispose()


def test_db_upgrade_permission_failure_exits_nonzero_without_credentials(
    postgres_database_factory,
    capsys,
) -> None:
    database_url = postgres_database_factory(migrated=False)
    admin_engine = create_engine(database_url)
    role = f"sediment_readonly_{uuid4().hex[:12]}"
    password = f"permission-secret-{uuid4().hex}"
    database_name = make_url(database_url).database
    assert database_name is not None
    role_created = False
    try:
        with admin_engine.begin() as connection:
            connection.connection.driver_connection.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role),
                    sql.Literal(password),
                )
            )
            role_created = True
            connection.exec_driver_sql(
                f'GRANT CONNECT ON DATABASE "{database_name}" TO "{role}"'
            )
        restricted_url = (
            make_url(database_url)
            .set(
                username=role,
                password=password,
            )
            .render_as_string(hide_password=False)
        )

        assert main(["db", "upgrade", "--database-url", restricted_url]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "error: could not upgrade database schema\n"
        assert password not in captured.err
        assert restricted_url not in captured.err
    finally:
        admin_engine.dispose()
        if role_created:
            cleanup_engine = create_engine(database_url)
            try:
                with cleanup_engine.begin() as connection:
                    connection.exec_driver_sql(
                        f'REVOKE CONNECT ON DATABASE "{database_name}" FROM "{role}"'
                    )
                    connection.connection.driver_connection.execute(
                        sql.SQL("DROP ROLE {}").format(sql.Identifier(role))
                    )
            finally:
                cleanup_engine.dispose()


def _behind_database_url(postgres_database_factory) -> str:
    """Create a database migrated to 0008 (one revision below head).

    The CLI one-shot quarantine verbs write ``fact_quarantine.reason`` through
    ``_SerializedText``; against a pre-0009 database the 0009 migration would
    later re-encode those rows. A behind database is the double-encoding
    window the writer-side AT_HEAD gate closes.
    """
    from alembic import command

    from sediment_core.postgres_migrations import _alembic_config

    database_url = postgres_database_factory(migrated=False)
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            config = _alembic_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "0008_session_commit_observations")
    finally:
        engine.dispose()
    return database_url


@pytest.mark.parametrize(
    ("argv", "operation"),
    [
        (
            ["quarantine", "inference_calls", "call-1", "--reason", "review"],
            "quarantine",
        ),
        (
            ["release", "inference_calls", "call-1", "--reason", "review"],
            "release",
        ),
        (
            [
                "quarantine-inference-calls",
                "--session-id",
                "sess-1",
                "--reason",
                "review",
                "--apply",
            ],
            "quarantine-inference-calls",
        ),
    ],
)
def test_store_write_verbs_refuse_behind_database_with_actionable_error(
    postgres_database_factory, monkeypatch, capsys, argv: list[str], operation: str
) -> None:
    """The one-shot quarantine verbs exit 1 against a behind database.

    Regression for the CLI write path: with no AT_HEAD gate, a
    ``sediment quarantine`` run before ``sediment db upgrade`` would write a
    ``fact_quarantine.reason`` value the 0009 migration later
    double-encodes. The writer-side gate turns that into a clean
    ``error: <verb> requires the database schema at head; run
    `sediment db upgrade`` line and exit code 1, matching the API lifespan
    gate on the same contract.
    """
    from pydantic import SecretStr

    from sediment_api.config import settings

    database_url = _behind_database_url(postgres_database_factory)
    monkeypatch.setattr(settings, "database_url", SecretStr(database_url))
    monkeypatch.setattr(settings, "org_id", "testorg")

    assert main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"error: {operation} requires the database schema at head; "
        "run `sediment db upgrade`\n"
    )


def test_store_read_verbs_still_work_against_behind_database(
    postgres_database_factory, monkeypatch, capsys
) -> None:
    """Read-only store verbs stay usable against a behind database.

    The gate scopes to writes; ``facts`` (count) and ``quarantine-log``
    must keep working during a staged rollout so an operator can compare
    counts before running ``sediment db upgrade``.
    """
    from pydantic import SecretStr

    from sediment_api.config import settings

    database_url = _behind_database_url(postgres_database_factory)
    monkeypatch.setenv("SEDIMENT_DATABASE_URL", database_url)
    monkeypatch.setenv("SEDIMENT_ORG_ID", "testorg")
    monkeypatch.setattr(settings, "database_url", SecretStr(database_url))
    monkeypatch.setattr(settings, "org_id", "testorg")

    assert main(["facts"]) == 0
    facts_out = capsys.readouterr().out
    assert "quarantine_revision: 0" in facts_out
    assert "repository_renames   unavailable unavailable" in facts_out

    assert main(["quarantine-log"]) == 0
    log_out = capsys.readouterr().out
    assert "0 rows" in log_out


def test_preupgrade_facts_preserve_real_counts_and_show_missing_tables(
    postgres_database_factory, monkeypatch, capsys
) -> None:
    from sediment_core import (
        FactStore,
        GatewayProvider,
        InferenceCall,
        RepositoryRename,
    )

    database_url = _behind_database_url(postgres_database_factory)
    monkeypatch.setenv("SEDIMENT_DATABASE_URL", database_url)
    monkeypatch.setenv("SEDIMENT_ORG_ID", "testorg")
    engine = create_engine(database_url)
    try:
        store = FactStore(engine)
        for fact_id in ("visible", "hidden"):
            store.store_inference_call(
                InferenceCall(
                    inference_call_id=fact_id,
                    org_id="testorg",
                    session_id="old-session",
                    gateway_provider=GatewayProvider.LITELLM,
                    input_messages=[],
                    output_messages=[],
                    observed_at=datetime(2026, 9, 1, tzinfo=UTC),
                )
            )
        # Seed the pre-0009 representation directly, as the prior writer did.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO fact_quarantine "
                    "(quarantine_id, org_id, fact_table, fact_id, action, reason, recorded_at) "
                    "VALUES ('old-quarantine', 'testorg', 'inference_calls', 'hidden', "
                    "'quarantine', 'legacy review', :at)"
                ),
                {"at": datetime(2026, 9, 1, tzinfo=UTC)},
            )

        def counts():
            assert main(["facts"]) == 0
            captured = capsys.readouterr()
            assert captured.err == ""
            assert "quarantine_revision: 1" in captured.out
            rows = {
                row[0]: row[1:]
                for line in captured.out.splitlines()
                if (row := line.split())
            }
            assert rows["sessions"] == ["1", "-"]
            assert rows["inference_calls"] == ["2", "1"]
            assert rows["pushes"] == ["0", "0"]
            return rows

        assert counts()["repository_renames"] == ["unavailable", "unavailable"]
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == "0008_session_commit_observations"
            )
            assert (
                connection.execute(
                    text("SELECT reason FROM fact_quarantine")
                ).scalar_one()
                == "legacy review"
            )
        assert main(["db", "upgrade", "--database-url", database_url]) == 0
        capsys.readouterr()
        assert counts()["repository_renames"] == ["0", "0"]
        store.store_repository_rename(
            RepositoryRename(
                org_id="testorg",
                repository_provider="github",
                repository_host="github.com",
                repository_id="101",
                old_repo="testorg/old",
                new_repo="testorg/new",
            )
        )
        assert counts()["repository_renames"] == ["1", "1"]
    finally:
        engine.dispose()


def test_store_write_verb_succeeds_after_upgrade_recovers_the_gate(
    postgres_database_factory, monkeypatch, capsys
) -> None:
    """The gate's error is actionable: ``db upgrade`` unblocks the verb.

    The operator path the error message names (run ``sediment db upgrade``,
    then retry) end-to-end: a behind database refuses the quarantine verb,
    the upgrade brings the schema to head, and the same verb then succeeds
    and the reason reads back unchanged.
    """
    from pydantic import SecretStr

    from sediment_api.config import settings

    database_url = _behind_database_url(postgres_database_factory)
    monkeypatch.setattr(settings, "database_url", SecretStr(database_url))
    monkeypatch.setattr(settings, "org_id", "testorg")

    assert main(["quarantine", "inference_calls", "call-1", "--reason", "review"]) == 1
    assert "run `sediment db upgrade`" in capsys.readouterr().err

    assert main(["db", "upgrade", "--database-url", database_url]) == 0
    assert capsys.readouterr().out == f"database schema upgraded to {HEAD_REVISION}\n"

    assert main(["quarantine", "inference_calls", "call-1", "--reason", "review"]) == 0
    quarantine_out = capsys.readouterr().out
    assert "quarantined inference_calls/call-1" in quarantine_out

    assert main(["quarantine-log"]) == 0
    log_out = capsys.readouterr().out
    assert "review" in log_out
    assert '"review"' not in log_out

# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import io
import runpy
from pathlib import Path

import sediment_core.postgres_migrations as postgres_migrations
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from sediment_core.postgres_migrations import (
    HEAD_REVISION,
    MIGRATION_LOCK_KEY,
    MigrationError,
    RevisionState,
    inspect_revision,
    upgrade_database,
)
from sediment_core.postgres_schema import metadata


def _cursor_agent_harness_migration_sql(function: str) -> str:
    path = (
        Path(postgres_migrations.__file__).with_name("alembic")
        / "versions"
        / "0003_cursor_agent_harness.py"
    )
    assert path.exists(), "Cursor AgentHarness migration is missing"
    namespace = runpy.run_path(str(path))
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    migration = namespace[function]
    migration.__globals__["op"] = Operations(context)
    migration()
    return " ".join(output.getvalue().split())


def test_postgres_upgrade_creates_complete_schema_and_is_idempotent(
    postgres_database_factory,
) -> None:
    database_url = postgres_database_factory(migrated=False)
    assert inspect_revision(database_url).state is RevisionState.ABSENT

    upgrade_database(database_url)
    assert inspect_revision(database_url).state is RevisionState.AT_HEAD
    upgrade_database(database_url)
    assert inspect_revision(database_url).state is RevisionState.AT_HEAD

    engine = create_engine(database_url)
    try:
        assert set(inspect(engine).get_table_names()) == {
            "alembic_version",
            "sessions",
            "inference_calls",
            "developer_decisions",
            "edit_observations",
            "rejected_edits",
            "retry_linkages",
            "ci_outcomes",
            "pushes",
            "pull_request_merges",
            "pull_request_revisions",
            "session_commit_observations",
            "fact_quarantine",
            "repository_renames",
        }
    finally:
        engine.dispose()


def test_postgres_baseline_matches_live_metadata(postgres_database_url) -> None:
    engine = create_engine(postgres_database_url)
    try:
        database = inspect(engine)
        for table_name, table in metadata.tables.items():
            assert {column["name"] for column in database.get_columns(table_name)} == {
                column.name for column in table.columns
            }
            assert set(
                database.get_pk_constraint(table_name)["constrained_columns"]
            ) == {column.name for column in table.primary_key.columns}
            expected_indexes = {index.name for index in table.indexes} | {
                constraint.name
                for constraint in table.constraints
                if constraint.__visit_name__ == "unique_constraint"
            }
            assert {
                index["name"] for index in database.get_indexes(table_name)
            } == expected_indexes
            assert {
                check["name"] for check in database.get_check_constraints(table_name)
            } == {
                constraint.name
                for constraint in table.constraints
                if constraint.__visit_name__ == "table_or_column_check_constraint"
            }
    finally:
        engine.dispose()


def test_postgres_autogenerate_reports_no_metadata_drift(
    postgres_database_url,
) -> None:
    engine = create_engine(postgres_database_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True},
            )
            assert compare_metadata(context, metadata) == []
    finally:
        engine.dispose()


def test_postgres_revision_inspection_distinguishes_behind_and_ahead(
    postgres_database_factory,
) -> None:
    behind_url = postgres_database_factory(migrated=False)
    behind_engine = create_engine(behind_url)
    try:
        with behind_engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
        result = inspect_revision(behind_url)
        assert result.state is RevisionState.BEHIND
        assert result.database_revision is None
        assert result.head_revision == HEAD_REVISION
    finally:
        behind_engine.dispose()

    ahead_url = postgres_database_factory(migrated=False)
    ahead_engine = create_engine(ahead_url)
    try:
        with ahead_engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES ('future_9999')")
            )
        result = inspect_revision(ahead_url)
        assert result.state is RevisionState.AHEAD
        assert result.database_revision == "future_9999"
        assert result.head_revision == HEAD_REVISION
    finally:
        ahead_engine.dispose()


def test_postgres_revision_inspection_known_behind_revision(
    postgres_database_factory,
) -> None:
    """A known, non-head stored revision classifies as BEHIND with that revision.

    Exercises the ``known_revision is not None`` -> BEHIND branch, which no
    existing test covered with a real revision string.
    """
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
        inspection = inspect_revision(database_url)
        assert inspection.state is RevisionState.BEHIND
        assert inspection.database_revision == "0002_retry_linkages"
        assert inspection.head_revision == HEAD_REVISION
    finally:
        engine.dispose()


def test_postgres_revision_inspection_propagates_corrupt_script_directory(
    postgres_database_factory,
    monkeypatch,
    tmp_path,
) -> None:
    """A broken script directory propagates instead of reporting AHEAD.

    A stored known-behind revision plus an unimportable version file must
    surface as ``MigrationError``, not be silently reclassified as AHEAD.
    """
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

    try:
        inspect_revision(database_url)
    except MigrationError as exc:
        assert str(exc) == "could not inspect database revision"
        assert isinstance(exc.__cause__, RuntimeError)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("corrupt script directory did not raise MigrationError")


def test_postgres_revision_inspection_does_not_mutate_absent_database(
    postgres_database_factory,
) -> None:
    database_url = postgres_database_factory(migrated=False)
    assert inspect_revision(database_url).state is RevisionState.ABSENT

    engine = create_engine(database_url)
    try:
        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


def test_postgres_upgrade_fails_when_advisory_lock_is_held(
    postgres_database_factory,
) -> None:
    database_url = postgres_database_factory(migrated=False)
    engine = create_engine(database_url)
    try:
        with engine.connect() as lock_connection:
            assert lock_connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY}
            ).scalar_one()
            try:
                upgrade_database(database_url)
            except MigrationError as exc:
                assert str(exc) == "database migration lock is held by another process"
            else:  # pragma: no cover - assertion branch
                raise AssertionError("upgrade acquired an advisory lock twice")
    finally:
        engine.dispose()


def test_postgres_baseline_revision_is_frozen_from_live_metadata() -> None:
    revision = (
        Path(postgres_migrations.__file__).with_name("alembic")
        / "versions"
        / "0001_postgresql_baseline.py"
    ).read_text(encoding="utf-8")
    assert "postgres_schema" not in revision
    assert "metadata.create_all" not in revision


def test_cursor_agent_harness_migration_replaces_all_four_checks() -> None:
    assert postgres_migrations.HEAD_REVISION == "0010_repository_identity"
    constraints = {
        "developer_decisions": "ck_decisions_agent_harness",
        "edit_observations": "ck_edit_observations_agent_harness",
        "rejected_edits": "ck_rejected_edits_agent_harness",
        "retry_linkages": "ck_retry_linkages_agent_harness",
    }

    upgrade_sql = _cursor_agent_harness_migration_sql("upgrade")
    downgrade_sql = _cursor_agent_harness_migration_sql("downgrade")

    for table, constraint in constraints.items():
        assert f"ALTER TABLE {table} DROP CONSTRAINT {constraint}" in upgrade_sql
        assert f"ALTER TABLE {table} DROP CONSTRAINT {constraint}" in downgrade_sql
    upgrade_check = (
        "CHECK (agent_harness IN ('claude-code', 'copilot', 'codex', 'cursor', 'pi'))"
    )
    downgrade_check = (
        "CHECK (agent_harness IN ('claude-code', 'copilot', 'codex', 'pi'))"
    )
    assert upgrade_sql.count(upgrade_check) == len(constraints)
    assert downgrade_sql.count(downgrade_check) == len(constraints)


def test_postgres_baseline_accepts_empty_branch_sentinel(
    postgres_database_url,
) -> None:
    engine = create_engine(postgres_database_url)
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    "INSERT INTO ci_outcomes "
                    "(schema_version, outcome_id, org_id, provider, run_id, repo, "
                    "commit_sha, branch, result, workflow_name, captured_at, raw) "
                    "VALUES (1, 'outcome-absent-branch', 'acme', 'github_actions', "
                    "'run-1', '', :sha, '', 'passed', '', now(), '{}')"
                ),
                {"sha": "a" * 40},
            )
    finally:
        engine.dispose()

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explicit PostgreSQL schema migration and revision operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .postgres_engine import (
    DatabaseOperationError,
    DatabaseURLValidationError,
    create_postgres_engine,
    verify_minimum_server_version,
)

HEAD_REVISION = "0010_repository_identity"
MIGRATION_LOCK_KEY = 7_315_324_899_385_581_412


class MigrationError(RuntimeError):
    """An expected public database migration failure."""


class RevisionState(StrEnum):
    ABSENT = "absent"
    BEHIND = "behind"
    AT_HEAD = "at_head"
    AHEAD = "ahead"


@dataclass(frozen=True)
class RevisionInspection:
    state: RevisionState
    database_revision: str | None
    head_revision: str = HEAD_REVISION


def inspect_revision(database_url: str) -> RevisionInspection:
    engine = None
    try:
        engine = create_postgres_engine(database_url)
        return inspect_engine_revision(engine)
    except (DatabaseOperationError, DatabaseURLValidationError, MigrationError):
        raise
    except Exception as exc:
        raise MigrationError("could not inspect database revision") from exc
    finally:
        if engine is not None:
            engine.dispose()


def inspect_engine_revision(engine: Engine) -> RevisionInspection:
    """Inspect the revision through a borrowed process-owned engine."""
    with engine.connect() as connection:
        if not inspect(connection).has_table("alembic_version"):
            return RevisionInspection(RevisionState.ABSENT, None)
        database_revision = MigrationContext.configure(
            connection
        ).get_current_revision()
    if database_revision is None:
        return RevisionInspection(RevisionState.BEHIND, None)
    if database_revision == HEAD_REVISION:
        return RevisionInspection(RevisionState.AT_HEAD, database_revision)

    script = ScriptDirectory.from_config(_alembic_config())
    try:
        known_revision = script.get_revision(database_revision)
    except CommandError:
        known_revision = None
    state = RevisionState.BEHIND if known_revision is not None else RevisionState.AHEAD
    return RevisionInspection(state, database_revision)


def upgrade_database(database_url: str) -> None:
    engine = None
    try:
        engine = create_postgres_engine(database_url)
        with engine.connect() as connection:
            verify_minimum_server_version(connection)
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": MIGRATION_LOCK_KEY},
                ).scalar_one()
            )
            connection.commit()
            if not acquired:
                raise MigrationError(
                    "database migration lock is held by another process"
                )
            try:
                config = _alembic_config()
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
            finally:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": MIGRATION_LOCK_KEY},
                )
                connection.commit()
    except (DatabaseOperationError, DatabaseURLValidationError, MigrationError):
        raise
    except Exception as exc:
        raise MigrationError("could not upgrade database schema") from exc
    finally:
        if engine is not None:
            engine.dispose()


def _alembic_config() -> Config:
    config = Config()
    script_location = Path(__file__).with_name("alembic")
    config.set_main_option("script_location", str(script_location))
    return config

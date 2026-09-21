# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pooled synchronous PostgreSQL engine lifecycle."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

logger = logging.getLogger(__name__)

#: Refuse servers older than PostgreSQL 16, the oldest release Sediment
#: validates. Compose and CI pin the supported PostgreSQL 17 deployment.
MINIMUM_SERVER_VERSION_NUM = 160000

#: How long a long-lived process waits for PostgreSQL to accept connections
#: before failing startup. One-shot operator commands fail fast instead.
STARTUP_WAIT_DEADLINE_SECONDS = 30.0
STARTUP_WAIT_INTERVAL_SECONDS = 1.0
DATABASE_CONNECT_TIMEOUT_SECONDS = 5.0
API_STATEMENT_TIMEOUT_MS = 30_000
API_LOCK_TIMEOUT_MS = 5_000
API_IDLE_TRANSACTION_TIMEOUT_MS = 30_000


class DatabaseOperationError(RuntimeError):
    """A credential-safe public PostgreSQL operation failure."""


class DatabaseURLValidationError(ValueError):
    """A credential-safe public PostgreSQL URL validation failure."""


_PASSWORD_ESCAPE_HINT = (
    "invalid PostgreSQL URL; if the password contains special "
    "characters, percent-encode them"
)


def _authority_has_unescaped_userinfo(database_url: str) -> bool:
    """Detect a raw password whose ``@`` splits the authority.

    The URL parser is lenient: ``user:p@ss@host`` parses without error but
    moves part of the password into the host, so a later diagnostic built
    from that host would echo secret material. More than one ``@`` before
    the first path segment means the userinfo needs percent-encoding.
    """
    remainder = database_url.split("://", 1)[-1]
    return remainder.split("/", 1)[0].count("@") > 1


def sanitized_database_target(database_url: str) -> str:
    """Return a credential-free PostgreSQL host, port, and database name."""
    try:
        if _authority_has_unescaped_userinfo(database_url):
            raise ValueError
        url = make_url(database_url)
        if url.get_backend_name() != "postgresql" or not url.host or not url.database:
            raise ValueError
        authority = url.host
        if url.port is not None:
            authority = f"{authority}:{url.port}"
        return f"{authority}/{url.database}"
    except (ArgumentError, AttributeError, TypeError, ValueError):
        return "invalid PostgreSQL target"


def database_operation_error(
    operation: str, database_url: str
) -> DatabaseOperationError:
    """Build a public failure without retaining secret-bearing input."""
    return DatabaseOperationError(
        f"{operation} failed for {sanitized_database_target(database_url)}"
    )


def engine_target(engine: Engine) -> str:
    """Return the engine's credential-free host, port, and database name."""
    authority = engine.url.host or "invalid PostgreSQL target"
    if engine.url.port is not None:
        authority = f"{authority}:{engine.url.port}"
    return f"{authority}/{engine.url.database}"


def configure_libpq() -> None:
    """Let Psycopg discover Homebrew's keg-only client without shell setup."""
    if sys.platform != "darwin" or shutil.which("pg_config"):
        return
    try:
        prefix = subprocess.run(
            ["brew", "--prefix", "libpq"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        binaries = Path(prefix) / "bin"
        if binaries.is_absolute() and (binaries / "pg_config").is_file():
            # Append so explicitly configured tools retain precedence.
            os.environ["PATH"] = os.pathsep.join(
                filter(None, (os.environ.get("PATH"), str(binaries)))
            )
    except (OSError, subprocess.SubprocessError):
        # The driver supplies the existing credential-safe missing-library error.
        pass


def create_postgres_engine(
    database_url: str, *, api_work: bool = False, single_connection: bool = False
) -> Engine:
    """Create Sediment's bounded synchronous PostgreSQL connection pool."""
    if _authority_has_unescaped_userinfo(database_url):
        raise DatabaseURLValidationError(_PASSWORD_ESCAPE_HINT)
    try:
        url = make_url(database_url)
    except (ArgumentError, ValueError):
        # The raw URL never enters the message: an unescaped password is the
        # common cause, and echoing the parser's own text could echo the
        # credential or its fragments.
        raise DatabaseURLValidationError(_PASSWORD_ESCAPE_HINT) from None
    if url.get_backend_name() != "postgresql":
        raise DatabaseURLValidationError("database URL must use PostgreSQL")
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    elif url.drivername != "postgresql+psycopg":
        raise DatabaseURLValidationError("database URL must use the psycopg 3 driver")
    connect_args = {"connect_timeout": DATABASE_CONNECT_TIMEOUT_SECONDS}
    if api_work:
        connect_args["options"] = (
            f"-c statement_timeout={API_STATEMENT_TIMEOUT_MS} "
            f"-c lock_timeout={API_LOCK_TIMEOUT_MS} "
            f"-c idle_in_transaction_session_timeout={API_IDLE_TRANSACTION_TIMEOUT_MS}"
        )
    try:
        configure_libpq()
        return create_engine(
            url,
            pool_size=1 if single_connection else 5,
            max_overflow=0 if single_connection else 5,
            pool_timeout=30,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
    except ImportError:
        raise DatabaseOperationError(
            "PostgreSQL driver unavailable; install a maintained system libpq library "
            "(libpq5 on Debian/Ubuntu, libpq on macOS) and make it discoverable "
            "by the system dynamic loader, then retry"
        ) from None


def wait_for_database(
    engine: Engine,
    *,
    deadline_seconds: float | None = None,
    interval_seconds: float | None = None,
) -> None:
    """Ping until the database accepts connections or the deadline passes.

    An orchestrator can start API replicas before PostgreSQL finishes booting,
    so a long-lived process retries inside one bounded window instead of
    failing on the first refused connection. The failure message carries only
    the credential-free target; per-attempt detail goes to the log.
    """
    if deadline_seconds is None:
        deadline_seconds = STARTUP_WAIT_DEADLINE_SECONDS
    if interval_seconds is None:
        interval_seconds = STARTUP_WAIT_INTERVAL_SECONDS
    deadline = time.monotonic() + deadline_seconds
    attempts = 0
    while True:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds < DATABASE_CONNECT_TIMEOUT_SECONDS:
            raise DatabaseOperationError(
                f"connect to database failed for {engine_target(engine)}"
            ) from None
        attempts += 1
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
            return
        except SQLAlchemyError:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds < DATABASE_CONNECT_TIMEOUT_SECONDS:
                raise DatabaseOperationError(
                    f"connect to database failed for {engine_target(engine)}"
                ) from None
            logger.warning(
                "database_not_ready target=%s attempt=%d",
                engine_target(engine),
                attempts,
            )
            time.sleep(
                min(
                    interval_seconds,
                    remaining_seconds - DATABASE_CONNECT_TIMEOUT_SECONDS,
                )
            )


def verify_minimum_server_version(connection: Connection) -> None:
    """Refuse a PostgreSQL server older than the supported minimum."""
    version_num = int(
        connection.exec_driver_sql("SHOW server_version_num").scalar_one()
    )
    if version_num < MINIMUM_SERVER_VERSION_NUM:
        raise DatabaseOperationError(
            f"PostgreSQL {version_num // 10000} is older than the supported "
            f"minimum PostgreSQL {MINIMUM_SERVER_VERSION_NUM // 10000}"
        )

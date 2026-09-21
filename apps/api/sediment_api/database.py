# SPDX-License-Identifier: AGPL-3.0-or-later
"""PostgreSQL composition helpers for one-shot operator processes."""

from __future__ import annotations

import argparse
import os
from collections.abc import Iterator
from contextlib import contextmanager

from sediment_core import FactStore
from sediment_core.postgres_engine import (
    create_postgres_engine,
    database_operation_error,
)
from sqlalchemy.exc import SQLAlchemyError


def add_database_url_argument(parser: argparse.ArgumentParser) -> None:
    """Add the direct-store PostgreSQL setting to an operator parser."""
    parser.add_argument(
        "--database-url",
        default=os.environ.get("SEDIMENT_DATABASE_URL"),
        help="PostgreSQL URL (default: $SEDIMENT_DATABASE_URL)",
    )


@contextmanager
def one_shot_fact_store(
    database_url: str | None, *, operation: str
) -> Iterator[FactStore]:
    """Own and dispose one bounded engine for a one-shot process."""
    if not database_url:
        raise ValueError("set SEDIMENT_DATABASE_URL or pass --database-url")
    engine = None
    try:
        engine = create_postgres_engine(database_url)
        yield FactStore(engine)
    except SQLAlchemyError:
        raise database_operation_error(operation, database_url) from None
    finally:
        if engine is not None:
            engine.dispose()

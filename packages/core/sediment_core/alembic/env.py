# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run Alembic migrations on the connection supplied by Sediment."""

from __future__ import annotations

from alembic import context

from sediment_core.postgres_schema import metadata


def run_migrations() -> None:
    connection = context.config.attributes.get("connection")
    if connection is None:
        raise RuntimeError("Sediment must supply an Alembic connection")
    context.configure(
        connection=connection,
        target_metadata=metadata,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations()

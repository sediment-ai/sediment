# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preserve descriptive scalar content in ASCII-escaped TEXT (ADR 0015)."""

import json

from alembic import op
import sqlalchemy as sa

revision = "0009_descriptive_text_encoding"
down_revision = "0008_session_commit_observations"
branch_labels = None
depends_on = None

# Frozen physical inventory: migrations never import live models or metadata.
_COLUMNS = {
    "ci_outcomes": (
        "outcome_id",
        (
            "workflow_name",
            "run_url",
            "provider_result",
            "error_type",
            "reason",
            "source_event_type",
            "source_spec_version",
        ),
    ),
    "pushes": ("push_id", ("clone_url",)),
    "fact_quarantine": ("quarantine_revision", ("reason",)),
}


def upgrade() -> None:
    for name in (
        "run_url",
        "provider_result",
        "error_type",
        "reason",
        "source_event_type",
        "source_spec_version",
    ):
        op.drop_constraint(f"ck_ci_outcomes_{name}", "ci_outcomes", type_="check")
    op.drop_constraint("ck_quarantine_reason", "fact_quarantine", type_="check")
    connection = op.get_bind()
    for name, (primary_key, columns) in _COLUMNS.items():
        table = sa.table(name, sa.column(primary_key), *(sa.column(c) for c in columns))
        statement = table.update().where(table.c[primary_key] == sa.bindparam("_key"))
        statement = statement.values(
            {column: sa.bindparam(column) for column in columns}
        )
        # Stream the original rows so migration memory is bounded. Every write
        # changes physical encoding only; SQL NULL and logical values stay intact.
        with connection.execute(
            table.select().execution_options(yield_per=1000)
        ).mappings() as rows:
            for batch in rows.partitions(1000):
                connection.execute(
                    statement,
                    [
                        {
                            "_key": row[primary_key],
                            **{
                                column: None
                                if row[column] is None
                                else json.dumps(row[column], ensure_ascii=True)
                                for column in columns
                            },
                        }
                        for row in batch
                    ],
                )


def downgrade() -> None:
    raise RuntimeError("descriptive content encoding is forward-only (ADR 0015)")

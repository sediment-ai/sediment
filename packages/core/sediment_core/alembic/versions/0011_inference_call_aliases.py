# SPDX-License-Identifier: AGPL-3.0-or-later
"""Index exact captured identifiers without parsing content in PostgreSQL."""

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import array

revision = "0011_inference_call_aliases"
down_revision = "0010_repository_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Frozen physical representation and decoder: no mutable model imports.
    connection = op.get_bind()
    connection.exec_driver_sql("LOCK TABLE inference_calls IN ACCESS EXCLUSIVE MODE")
    op.add_column("inference_calls", sa.Column("call_alias_count", sa.BigInteger()))
    aliases = op.create_table(
        "inference_call_aliases",
        sa.Column(
            "inference_call_id",
            sa.Text(),
            sa.ForeignKey("inference_calls.inference_call_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "inference_call_id", "ordinal", name="pk_inference_call_aliases"
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_inference_call_aliases_ordinal"),
        sa.CheckConstraint(
            "org_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name="ck_inference_call_aliases_org_id",
        ),
        sa.CheckConstraint(
            "call_id <> '' AND call_id = btrim(call_id, E' \\t\\n\\r')",
            name="ck_inference_call_aliases_call_id",
        ),
    )
    calls = sa.table(
        "inference_calls",
        *(
            sa.column(name)
            for name in (
                "inference_call_id",
                "org_id",
                "model_call_id",
                "output_messages",
                "call_alias_count",
            )
        ),
    )
    source = sa.select(
        calls.c.inference_call_id,
        calls.c.org_id,
        calls.c.model_call_id,
        calls.c.output_messages,
    ).execution_options(yield_per=1)
    update_counts = (
        calls.update()
        .where(calls.c.inference_call_id == sa.bindparam("_fact_id"))
        .values(call_alias_count=sa.bindparam("_count"))
    )
    alias_batch = []
    count_batch = []
    with connection.execute(source).mappings() as rows:
        for row in rows:
            identifiers = {
                part["id"]
                for message in json.loads(row["output_messages"])
                for part in message["parts"]
                if part["type"] == "tool_call"
            }
            if row["model_call_id"] is not None:
                identifiers.add(row["model_call_id"])
            identifiers = sorted(identifiers)
            for ordinal, identifier in enumerate(identifiers):
                alias_batch.append(
                    {
                        "inference_call_id": row["inference_call_id"],
                        "org_id": row["org_id"],
                        "ordinal": ordinal,
                        "call_id": identifier,
                    }
                )
                if len(alias_batch) == 1000:
                    connection.execute(aliases.insert(), alias_batch)
                    alias_batch.clear()
            count_batch.append(
                {"_fact_id": row["inference_call_id"], "_count": len(identifiers)}
            )
            if len(count_batch) == 1000:
                connection.execute(update_counts, count_batch)
                count_batch.clear()
    if alias_batch:
        connection.execute(aliases.insert(), alias_batch)
    if count_batch:
        connection.execute(update_counts, count_batch)
    op.alter_column("inference_calls", "call_alias_count", nullable=False)
    op.create_check_constraint(
        "ck_inference_calls_call_alias_count",
        "inference_calls",
        "call_alias_count >= 0",
    )
    op.create_index(
        "ix_inference_call_aliases_lookup",
        "inference_call_aliases",
        [sa.sql.expression.Grouping(array([aliases.c.org_id, aliases.c.call_id]))],
        postgresql_using="hash",
    )


def downgrade() -> None:
    raise RuntimeError("Inference call alias representation is forward-only")

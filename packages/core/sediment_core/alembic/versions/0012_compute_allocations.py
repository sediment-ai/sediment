# SPDX-License-Identifier: AGPL-3.0-or-later
"""Add immutable Session-scoped Compute allocation Facts.

Revision ID: 0012_compute_allocations
Revises: 0011_inference_call_aliases
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0012_compute_allocations"
down_revision = "0011_inference_call_aliases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_quarantine_table", "fact_quarantine", type_="check")
    op.create_check_constraint(
        "ck_quarantine_table",
        "fact_quarantine",
        "fact_table IN ('inference_calls', 'developer_decisions', "
        "'ci_outcomes', 'pushes', 'pull_request_merges', "
        "'pull_request_revisions', 'edit_observations', 'rejected_edits', "
        "'retry_linkages', 'session_commit_observations', "
        "'repository_renames', 'compute_allocations')",
    )
    op.create_table(
        "compute_allocations",
        sa.Column("allocation_id", sa.Text(), nullable=False),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("allocation_source", sa.Text(), nullable=False),
        sa.Column("cpu_millicores", sa.BigInteger(), nullable=True),
        sa.Column("memory_bytes", sa.BigInteger(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "allocation_id <> '' AND allocation_id = "
            "btrim(allocation_id, E' \\t\\n\\r')",
            name="ck_compute_allocations_allocation_id",
        ),
        sa.CheckConstraint(
            "org_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name="ck_compute_allocations_org_id",
        ),
        sa.CheckConstraint(
            "session_id <> '' AND session_id = btrim(session_id, E' \\t\\n\\r')",
            name="ck_compute_allocations_session_id",
        ),
        sa.CheckConstraint(
            "allocation_source IN ('default', 'agent_request', 'operator', "
            "'randomized')",
            name="ck_compute_allocations_allocation_source",
        ),
        sa.CheckConstraint(
            "cpu_millicores IS NULL OR cpu_millicores >= 1",
            name="ck_compute_allocations_cpu_millicores",
        ),
        sa.CheckConstraint(
            "memory_bytes IS NULL OR memory_bytes >= 1",
            name="ck_compute_allocations_memory_bytes",
        ),
        sa.CheckConstraint(
            "cpu_millicores IS NOT NULL OR memory_bytes IS NOT NULL",
            name="ck_compute_allocations_limit_present",
        ),
        sa.PrimaryKeyConstraint("allocation_id"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_compute_allocations_natural ON compute_allocations "
        "(org_id, session_id, occurred_at, allocation_source, "
        "coalesce(cpu_millicores, 0), coalesce(memory_bytes, 0))"
    )
    op.create_index(
        "ix_compute_allocations_session_time",
        "compute_allocations",
        ["org_id", "session_id", "occurred_at", "allocation_id"],
        unique=False,
    )


def downgrade() -> None:
    # Dropping the table would delete Facts, which are never deleted (ADR 0001).
    raise RuntimeError("Compute allocation Facts are forward-only")

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Add immutable retry-linkage facts.

Revision ID: 0002_retry_linkages
Revises: 0001_postgresql_baseline
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_retry_linkages"
down_revision = "0001_postgresql_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_quarantine_table", "fact_quarantine", type_="check")
    op.create_check_constraint(
        "ck_quarantine_table",
        "fact_quarantine",
        "fact_table IN ('inference_calls', 'developer_decisions', "
        "'ci_outcomes', 'pushes', 'edit_observations', 'rejected_edits', "
        "'retry_linkages')",
    )
    op.create_table(
        "retry_linkages",
        sa.Column("retry_linkage_id", sa.Text(), nullable=False),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("agent_harness", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("rejected_call_id", sa.Text(), nullable=False),
        sa.Column("accepted_call_id", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "accepted_call_id <> '' AND "
            "accepted_call_id = btrim(accepted_call_id, E' \\t\\n\\r')",
            name="ck_retry_linkages_accepted_call_id",
        ),
        sa.CheckConstraint(
            "agent_harness IN ('claude-code', 'copilot', 'codex', 'pi')",
            name="ck_retry_linkages_agent_harness",
        ),
        sa.CheckConstraint(
            "file_path <> '' AND file_path = btrim(file_path, E' \\t\\n\\r')",
            name="ck_retry_linkages_file_path",
        ),
        sa.CheckConstraint(
            "org_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name="ck_retry_linkages_org_id",
        ),
        sa.CheckConstraint(
            "rejected_call_id <> '' AND "
            "rejected_call_id = btrim(rejected_call_id, E' \\t\\n\\r')",
            name="ck_retry_linkages_rejected_call_id",
        ),
        sa.CheckConstraint(
            "rejected_call_id <> accepted_call_id",
            name="ck_retry_linkages_distinct_calls",
        ),
        sa.CheckConstraint(
            "retry_linkage_id <> '' AND "
            "retry_linkage_id = btrim(retry_linkage_id, E' \\t\\n\\r')",
            name="ck_retry_linkages_retry_linkage_id",
        ),
        sa.CheckConstraint(
            "session_id <> '' AND session_id = btrim(session_id, E' \\t\\n\\r')",
            name="ck_retry_linkages_session_id",
        ),
        sa.CheckConstraint(
            "tool_name IN ('Edit', 'Write')",
            name="ck_retry_linkages_tool_name",
        ),
        sa.CheckConstraint(
            "user_id IS NULL OR "
            "(user_id <> '' AND user_id = btrim(user_id, E' \\t\\n\\r'))",
            name="ck_retry_linkages_user_id",
        ),
        sa.PrimaryKeyConstraint("retry_linkage_id"),
    )
    op.create_index(
        "uq_retry_linkages_calls",
        "retry_linkages",
        [
            "org_id",
            "agent_harness",
            "session_id",
            "tool_name",
            "file_path",
            "rejected_call_id",
            "accepted_call_id",
        ],
        unique=True,
    )

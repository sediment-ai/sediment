# SPDX-License-Identifier: AGPL-3.0-or-later
"""Add immutable Git-note Session-to-commit observation Facts.

Revision ID: 0008_session_commit_observations
Revises: 0007_session_dossier_lookup
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0008_session_commit_observations"
down_revision = "0007_session_dossier_lookup"
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
        "'retry_linkages', 'session_commit_observations')",
    )
    op.create_table(
        "session_commit_observations",
        sa.Column("schema_version", sa.BigInteger(), nullable=False),
        sa.Column("observation_id", sa.Text(), nullable=False),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("source_push_id", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 1",
            name="ck_session_commit_observations_schema_version",
        ),
        sa.CheckConstraint(
            "observation_id <> '' AND observation_id = "
            "btrim(observation_id, E' \\t\\n\\r')",
            name="ck_session_commit_observations_observation_id",
        ),
        sa.CheckConstraint(
            "org_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name="ck_session_commit_observations_org_id",
        ),
        sa.CheckConstraint(
            "repo <> '' AND repo = lower(repo) AND repo ~ '^[^/]+/[^/]+$'",
            name="ck_session_commit_observations_repo",
        ),
        sa.CheckConstraint(
            "commit_sha ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'",
            name="ck_session_commit_observations_commit_sha",
        ),
        sa.CheckConstraint(
            "session_id <> '' AND session_id = btrim(session_id, E' \\t\\n\\r')",
            name="ck_session_commit_observations_session_id",
        ),
        sa.CheckConstraint(
            "source_push_id <> '' AND source_push_id = "
            "btrim(source_push_id, E' \\t\\n\\r')",
            name="ck_session_commit_observations_source_push_id",
        ),
        sa.PrimaryKeyConstraint("observation_id"),
    )
    op.create_index(
        "uq_session_commit_observations_edge",
        "session_commit_observations",
        ["org_id", "repo", "commit_sha", "session_id"],
        unique=True,
    )
    op.create_index(
        "ix_session_commit_observations_as_of",
        "session_commit_observations",
        ["org_id", "captured_at", "observation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("session_commit_observations")
    op.drop_constraint("ck_quarantine_table", "fact_quarantine", type_="check")
    op.create_check_constraint(
        "ck_quarantine_table",
        "fact_quarantine",
        "fact_table IN ('inference_calls', 'developer_decisions', "
        "'ci_outcomes', 'pushes', 'pull_request_merges', "
        "'pull_request_revisions', 'edit_observations', 'rejected_edits', "
        "'retry_linkages')",
    )

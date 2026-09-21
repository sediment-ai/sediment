# SPDX-License-Identifier: AGPL-3.0-or-later
"""Add immutable pull request revision facts.

Revision ID: 0005_pull_request_revisions
Revises: 0004_pull_request_merges
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_pull_request_revisions"
down_revision = "0004_pull_request_merges"
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
        "'retry_linkages')",
    )
    op.create_table(
        "pull_request_revisions",
        sa.Column("revision_id", sa.Text(), nullable=False),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("pr_number", sa.BigInteger(), nullable=False),
        sa.Column("head_repo", sa.Text(), nullable=False),
        sa.Column("head_ref", sa.Text(), nullable=False),
        sa.Column("head_sha", sa.Text(), nullable=False),
        sa.Column("base_ref", sa.Text(), nullable=False),
        sa.Column("base_sha", sa.Text(), nullable=False),
        sa.Column("previous_head_sha", sa.Text(), nullable=True),
        sa.Column("source_event_id", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision_id <> '' AND revision_id = btrim(revision_id, E' \\t\\n\\r')",
            name="ck_pr_revisions_revision_id",
        ),
        sa.CheckConstraint(
            "org_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name="ck_pr_revisions_org_id",
        ),
        sa.CheckConstraint("provider IN ('github')", name="ck_pr_revisions_provider"),
        sa.CheckConstraint(
            "repo <> '' AND repo = lower(repo) AND repo ~ '^[^/]+/[^/]+$'",
            name="ck_pr_revisions_repo",
        ),
        sa.CheckConstraint(
            "pr_number BETWEEN 1 AND 9223372036854775807",
            name="ck_pr_revisions_pr_number",
        ),
        sa.CheckConstraint(
            "head_repo <> '' AND head_repo = lower(head_repo) "
            "AND head_repo ~ '^[^/]+/[^/]+$'",
            name="ck_pr_revisions_head_repo",
        ),
        sa.CheckConstraint(
            "head_ref <> '' AND head_ref NOT LIKE 'refs/heads/%' "
            "AND head_ref = btrim(head_ref, E' \\t\\n\\r')",
            name="ck_pr_revisions_head_ref",
        ),
        sa.CheckConstraint(
            "head_sha ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'",
            name="ck_pr_revisions_head_sha",
        ),
        sa.CheckConstraint(
            "base_ref <> '' AND base_ref NOT LIKE 'refs/heads/%' "
            "AND base_ref = btrim(base_ref, E' \\t\\n\\r')",
            name="ck_pr_revisions_base_ref",
        ),
        sa.CheckConstraint(
            "base_sha ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'",
            name="ck_pr_revisions_base_sha",
        ),
        sa.CheckConstraint(
            "previous_head_sha IS NULL OR "
            "previous_head_sha ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'",
            name="ck_pr_revisions_previous_head_sha",
        ),
        sa.CheckConstraint(
            "source_event_id IS NULL OR "
            "(source_event_id <> '' AND "
            "source_event_id = btrim(source_event_id, E' \\t\\n\\r'))",
            name="ck_pr_revisions_source_event_id",
        ),
        sa.PrimaryKeyConstraint("revision_id"),
    )
    op.create_index(
        "ix_pull_request_revisions_org_repo_pr",
        "pull_request_revisions",
        ["org_id", "repo", "pr_number"],
        unique=False,
    )
    op.create_index(
        "uq_pull_request_revisions_natural",
        "pull_request_revisions",
        ["org_id", "provider", "repo", "pr_number", "head_sha", "base_sha"],
        unique=True,
    )

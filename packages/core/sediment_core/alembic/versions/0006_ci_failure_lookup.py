# SPDX-License-Identifier: AGPL-3.0-or-later
"""Index bounded CI outcome investigation queries.

Revision ID: 0006_ci_failure_lookup
Revises: 0005_pull_request_revisions
"""

from __future__ import annotations

from alembic import op

revision = "0006_ci_failure_lookup"
down_revision = "0005_pull_request_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_ci_failure_lookup",
        "ci_outcomes",
        [
            "org_id",
            "repo",
            "result",
            "captured_at",
            "outcome_id",
        ],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ci_failure_lookup", table_name="ci_outcomes")

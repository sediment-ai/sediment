# SPDX-License-Identifier: AGPL-3.0-or-later
"""Index bounded Session dossier queries.

Revision ID: 0007_session_dossier_lookup
Revises: 0006_ci_failure_lookup
"""

from __future__ import annotations

from alembic import op

revision = "0007_session_dossier_lookup"
down_revision = "0006_ci_failure_lookup"
branch_labels = None
depends_on = None

_INDEXES = (
    (
        "ix_inference_calls_session_time",
        "inference_calls",
        ["org_id", "session_id", "observed_at", "inference_call_id"],
    ),
    (
        "ix_decisions_session_time",
        "developer_decisions",
        ["org_id", "session_id", "occurred_at", "decision_id"],
    ),
    (
        "ix_edit_observations_session_time",
        "edit_observations",
        ["org_id", "session_id", "occurred_at", "observation_id"],
    ),
    (
        "ix_rejected_edits_session_time",
        "rejected_edits",
        ["org_id", "session_id", "occurred_at", "rejection_id"],
    ),
    (
        "ix_retry_linkages_session_time",
        "retry_linkages",
        ["org_id", "session_id", "occurred_at", "retry_linkage_id"],
    ),
    (
        "ix_ci_repo_commit",
        "ci_outcomes",
        ["org_id", "repo", "commit_sha", "captured_at", "outcome_id"],
    ),
    (
        "ix_pushes_repo_after",
        "pushes",
        ["org_id", "repo", "after_sha", "captured_at", "push_id"],
    ),
)


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns, unique=False)


def downgrade() -> None:
    for name, table, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)

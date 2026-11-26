# SPDX-License-Identifier: AGPL-3.0-or-later
"""Include file_path in the EditObservation natural key.

A Codex apply_patch call can touch several files under one parent tool-call
id. Developer decisions already fan out per file_path under that call_id;
Edit observations need the same composite identity so each file's pair is
stored and joinable without inventing child call ids.

Revision ID: 0013_edit_obs_file_path
Revises: 0012_compute_allocations
"""

from __future__ import annotations

from alembic import op

revision = "0013_edit_obs_file_path"
down_revision = "0012_compute_allocations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_edit_observations_call", table_name="edit_observations")
    op.create_index(
        "uq_edit_observations_call",
        "edit_observations",
        ["org_id", "agent_harness", "session_id", "call_id", "file_path"],
        unique=True,
    )


def downgrade() -> None:
    raise RuntimeError("Edit observation file-path identity is forward-only")

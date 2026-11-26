# SPDX-License-Identifier: AGPL-3.0-or-later
"""Include edit_part_index in the EditObservation natural key.

A Claude Code MultiEdit call can touch several edits[] entries under one
parent tool-call id and one file_path. Codex multi-file patches already
distinguish members by file_path (0013); same-file MultiEdit parts need the
remaining discriminator so each part is stored and joinable without
inventing child call ids.

Revision ID: 0014_edit_obs_part_index
Revises: 0013_edit_obs_file_path
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0014_edit_obs_part_index"
down_revision = "0013_edit_obs_file_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "edit_observations",
        sa.Column("edit_part_index", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_edit_observations_edit_part_index",
        "edit_observations",
        "edit_part_index IS NULL OR edit_part_index >= 0",
    )
    op.drop_index("uq_edit_observations_call", table_name="edit_observations")
    op.execute(
        "CREATE UNIQUE INDEX uq_edit_observations_call ON edit_observations "
        "(org_id, agent_harness, session_id, call_id, file_path, "
        "coalesce(edit_part_index, -1))"
    )


def downgrade() -> None:
    raise RuntimeError("Edit observation part-index identity is forward-only")

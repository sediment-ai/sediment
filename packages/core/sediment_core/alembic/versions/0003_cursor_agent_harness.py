# SPDX-License-Identifier: AGPL-3.0-or-later
"""Allow Cursor in developer-side Fact harness constraints.

Revision ID: 0003_cursor_agent_harness
Revises: 0002_retry_linkages
"""

from __future__ import annotations

from alembic import op

revision = "0003_cursor_agent_harness"
down_revision = "0002_retry_linkages"
branch_labels = None
depends_on = None

_AGENT_HARNESS_CONSTRAINTS = (
    ("developer_decisions", "ck_decisions_agent_harness"),
    ("edit_observations", "ck_edit_observations_agent_harness"),
    ("rejected_edits", "ck_rejected_edits_agent_harness"),
    ("retry_linkages", "ck_retry_linkages_agent_harness"),
)
_V1_AGENT_HARNESSES = ("claude-code", "copilot", "codex", "pi")
_V2_AGENT_HARNESSES = ("claude-code", "copilot", "codex", "cursor", "pi")


def _replace_agent_harness_checks(values: tuple[str, ...]) -> None:
    allowed = ", ".join(repr(value) for value in values)
    for table, constraint in _AGENT_HARNESS_CONSTRAINTS:
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(
            constraint,
            table,
            f"agent_harness IN ({allowed})",
        )


def upgrade() -> None:
    _replace_agent_harness_checks(_V2_AGENT_HARNESSES)


def downgrade() -> None:
    _replace_agent_harness_checks(_V1_AGENT_HARNESSES)

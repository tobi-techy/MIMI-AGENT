"""Helper inboxes (4 Nov 2026): one row per piece of work one helper hands to
another (``ask_helper`` / ``delegate_task``).

Revision ID: 0018
Revises: 0015

``0016`` is claimed by the computer-tasks branch and ``0017`` by the returns
branch, so this id is ``0018`` and chains after ``0015`` while this branch
stands alone. The only change at merge time is one line: ``down_revision``
becomes ``"0017"`` once returns (renumbered to 0017) lands. Nothing here
renumbers another branch.
Create Date: 2026-11-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0018"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "helper_tasks",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("source_key", sa.String(100), nullable=False, server_default=""),
        sa.Column("parent_task_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("from_helper", sa.String(32), nullable=False),
        sa.Column("to_helper", sa.String(32), nullable=False),
        sa.Column("skill", sa.String(64), nullable=False, server_default=""),
        sa.Column("shop_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("thread_id", sa.String(200), nullable=False, server_default=""),
        sa.Column("turn_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("ask", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("outcome", sa.Text(), nullable=False, server_default=""),
        sa.Column("result", sa.Text(), nullable=False, server_default=""),
        sa.Column("chain", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("created_ts", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.String(40), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("tenant_id", "task_id"),
    )
    op.create_index("ix_helper_tasks_tenant_to", "helper_tasks",
                    ["tenant_id", "to_helper", "created_ts"])


def downgrade() -> None:
    op.drop_index("ix_helper_tasks_tenant_to", table_name="helper_tasks")
    op.drop_table("helper_tasks")

"""Always-on runtime: durable helper jobs and the owner inbox.

``runtime_jobs`` holds one row per helper job: the JSON body is the whole
resumable state, ``version`` is the compare-and-set guard, and ``state`` /
``lease_owner`` exist for the claim query and the console. ``runtime_owner_inbox``
holds owner messages until a step boundary takes them (acked in the same
transaction as the job save).

This is the first revision: the chain starts here.

Revision ID: 0001
Revises:
Create Date: 2026-10-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_jobs",
        sa.Column("job_id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("helper", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
    )
    op.create_index("ix_runtime_jobs_tenant_id", "runtime_jobs", ["tenant_id"])
    op.create_index("ix_runtime_jobs_state", "runtime_jobs", ["state"])

    op.create_table(
        "runtime_owner_inbox",
        sa.Column("message_id", sa.String(), primary_key=True),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("received_at", sa.String(), nullable=False),
        sa.Column("acked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_runtime_owner_inbox_job_id", "runtime_owner_inbox", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_runtime_owner_inbox_job_id", table_name="runtime_owner_inbox")
    op.drop_table("runtime_owner_inbox")
    op.drop_index("ix_runtime_jobs_state", table_name="runtime_jobs")
    op.drop_index("ix_runtime_jobs_tenant_id", table_name="runtime_jobs")
    op.drop_table("runtime_jobs")

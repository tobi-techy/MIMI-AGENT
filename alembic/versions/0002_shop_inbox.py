"""Shop replies waiting for their delivery receipt job.

One row per inbound shop WhatsApp message routed to a receipt job, keyed on
the channel message id (so a webhook retry is a no-op). Read by the console;
written by ``mimi.receipts.inbox.SqlShopInbox``.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shop_inbox",
        sa.Column("message_id", sa.String(), primary_key=True),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("shop_id", sa.String(), nullable=False),
        sa.Column("sender", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("received_at", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_shop_inbox_job_id", "shop_inbox", ["job_id"]
    )
    op.create_index(
        "ix_shop_inbox_shop_id", "shop_inbox", ["shop_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_shop_inbox_shop_id", table_name="shop_inbox")
    op.drop_index("ix_shop_inbox_job_id", table_name="shop_inbox")
    op.drop_table("shop_inbox")

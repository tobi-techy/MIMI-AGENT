"""Sales tracking (3 Nov 2026): batch and expiry on delivery lines, and the
weekly check-in's state in the store.

Revision ID: 0015
Revises: 0014

0015 is the next free number after 0014 (policy rules) on main. RAI-324's
branch also claims 0011 and is to be renumbered after this one.
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("delivery_lines") as b:
        b.add_column(sa.Column("expires_on", sa.String(10), nullable=True))
        b.add_column(sa.Column("batch", sa.String(64), nullable=True))
    op.create_table(
        "checkin_weeks",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("week", sa.String(8), nullable=False),
        sa.Column("event_id", sa.String(200), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("low_stock_sku", sa.String(64), nullable=False, server_default=""),
        sa.Column("answered_at", sa.String(40), nullable=False, server_default=""),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "shop_id", "week"),
    )
    op.create_index("ix_checkin_weeks_event", "checkin_weeks", ["tenant_id", "event_id"])
    op.create_table(
        "checkin_shops",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("skips", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("connect_offered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "shop_id"),
    )


def downgrade() -> None:
    op.drop_table("checkin_shops")
    op.drop_index("ix_checkin_weeks_event", table_name="checkin_weeks")
    op.drop_table("checkin_weeks")
    with op.batch_alter_table("delivery_lines") as b:
        b.drop_column("batch")
        b.drop_column("expires_on")

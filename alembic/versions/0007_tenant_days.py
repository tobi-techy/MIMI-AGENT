"""Tenant day numbers for the stock and credit helpers (RAI-272).

``tenants.live_day`` is the world's day number of ``live_date`` and
``tenants.price_rise_day`` the day some catalogue prices rose. The Stock and
Credit helpers on the API read deliveries and prices by day number, as their
in-process stand-ins (``CreditGo``, the stock flow) read the world.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tenants") as b:
        b.add_column(sa.Column("live_day", sa.Integer(), nullable=False, server_default="0"))
        b.add_column(sa.Column("price_rise_day", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tenants") as b:
        b.drop_column("price_rise_day")
        b.drop_column("live_day")

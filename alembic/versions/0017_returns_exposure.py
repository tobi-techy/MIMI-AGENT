"""Returns ledger and exposure (feat/returns-and-exposure).

Revision ID: 0017
Revises: 0016

A return opens from an approved slow-stock pickup option, then drafted,
approved, collected, inspected, settled (or cancelled); its settlement is a
``return_credit`` approval applied to the shop's open invoices (undoable),
and ``returns_settings`` holds the owner's configured pickup cost.

Renumbered from 0016 to 0017 when the computer-tasks branch (RAI-326) landed
on main first with its own ``0016`` on ``0015``, so the chain is
``0015`` -> ``0016`` (computer tasks) -> ``0017`` (returns) with one head.
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "returns",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("return_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("approval_id", sa.String(64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="drafted"),
        sa.Column("value_drafted_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("value_collected_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("value_returned_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("recovered_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("pickup_cost_kobo", sa.BigInteger(), nullable=True),
        sa.Column("saving_kobo", sa.BigInteger(), nullable=True),
        sa.Column("credit_kobo", sa.BigInteger(), nullable=True),
        sa.Column("credit_approval_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("created_ts", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "return_id"),
    )
    op.create_index("ix_returns_tenant_status", "returns", ["tenant_id", "status"])
    op.create_index("ix_returns_tenant_shop", "returns", ["tenant_id", "shop_id"])
    op.create_table(
        "return_lines",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("return_id", sa.String(64), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("item", sa.String(200), nullable=False, server_default=""),
        sa.Column("unit", sa.String(32), nullable=False, server_default=""),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("quantity_drafted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quantity_collected", sa.Integer(), nullable=True),
        sa.Column("quantity_returned", sa.Integer(), nullable=True),
        sa.Column("unit_price_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("value_drafted_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("value_returned_kobo", sa.BigInteger(), nullable=True),
        sa.Column("condition", sa.String(16), nullable=False, server_default=""),
        sa.Column("disposition", sa.String(24), nullable=False, server_default=""),
        sa.Column("expires_on", sa.String(10), nullable=True),
        sa.Column("batch", sa.String(64), nullable=False, server_default=""),
        sa.Column("expiry_source", sa.String(16), nullable=False, server_default="estimated"),
        sa.PrimaryKeyConstraint("tenant_id", "return_id", "line_no"),
    )
    op.create_table(
        "return_events",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("return_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("by", sa.String(100), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "event_id"),
    )
    op.create_index("ix_return_events_return", "return_events",
                    ["tenant_id", "return_id", "seq"])
    op.create_table(
        "credit_notes",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("credit_id", sa.String(64), nullable=False),
        sa.Column("return_id", sa.String(64), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("approval_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("amount_kobo", sa.BigInteger(), nullable=False),
        sa.Column("applied_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("on_account_kobo", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("applications", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("applied_at", sa.String(40), nullable=False, server_default=""),
        sa.Column("undone_at", sa.String(40), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "credit_id"),
    )
    op.create_index("ix_credit_notes_return", "credit_notes", ["tenant_id", "return_id"])
    op.create_table(
        "returns_settings",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("pickup_cost_kobo", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id"),
    )


def downgrade() -> None:
    op.drop_table("returns_settings")
    op.drop_index("ix_credit_notes_return", table_name="credit_notes")
    op.drop_table("credit_notes")
    op.drop_index("ix_return_events_return", table_name="return_events")
    op.drop_table("return_events")
    op.drop_table("return_lines")
    op.drop_index("ix_returns_tenant_shop", table_name="returns")
    op.drop_index("ix_returns_tenant_status", table_name="returns")
    op.drop_table("returns")

"""Shops' choices about voice replies, each kept with the shop's consent.

One row per choice ("VOICE ON", "VOICE OFF"): the shop's message id, its
words, who sent it and when. The newest row is the shop's current choice;
with no row, voice replies are off. No audio is stored here or anywhere.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shop_voice_consent",
        sa.Column("consent_message_id", sa.String(), primary_key=True),
        sa.Column("shop_id", sa.String(), nullable=False),
        sa.Column("voice_replies", sa.Boolean(), nullable=False),
        sa.Column("language", sa.String(), nullable=False),
        sa.Column("consent_text", sa.Text(), nullable=False),
        sa.Column("consent_sender", sa.String(), nullable=False),
        sa.Column("decided_at", sa.String(), nullable=False),
    )
    op.create_index("ix_shop_voice_consent_shop_id", "shop_voice_consent", ["shop_id"])


def downgrade() -> None:
    op.drop_index("ix_shop_voice_consent_shop_id", table_name="shop_voice_consent")
    op.drop_table("shop_voice_consent")

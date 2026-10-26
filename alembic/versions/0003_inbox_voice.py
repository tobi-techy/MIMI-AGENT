"""Voice notes in the shop inbox: the transcript, never the audio.

A voice note is stored as its transcript in ``text`` with ``kind='voice'``,
the transcript's status (ok, low, failed), the provider that produced it,
any confidence the API gave, the clip's length and its SHA-256. Text
replies keep the defaults.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("kind", sa.String(), False, "text"),
    ("transcript_status", sa.String(), False, ""),
    ("transcript_provider", sa.String(), False, ""),
    ("transcript_confidence", sa.Float(), True, None),
    ("audio_seconds", sa.Float(), True, None),
    ("audio_sha256", sa.String(), False, ""),
)


def upgrade() -> None:
    with op.batch_alter_table("shop_inbox") as batch:
        for name, type_, nullable, default in _COLUMNS:
            batch.add_column(
                sa.Column(name, type_, nullable=nullable, server_default=default)
            )


def downgrade() -> None:
    with op.batch_alter_table("shop_inbox") as batch:
        for name, *_ in reversed(_COLUMNS):
            batch.drop_column(name)

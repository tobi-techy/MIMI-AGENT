"""A voice note's shape: where each sentence sat, and its loudness envelope.

``shop_inbox`` already keeps a voice note's transcript and the clip's length
and SHA-256, never its audio. These two columns are the facts about the audio
that can be kept without it, both as JSON text:

* ``segments``: ``[[start_s, end_s, text], ...]`` exactly as the provider
  timed each sentence (empty when it sent no timings);
* ``envelope``: about 130 integers 0 to 100, the RMS loudness of the clip
  start to end, computed once while the clip was in hand (empty when the
  audio could not be decoded in code).

Additive: two columns on an existing table, both with an empty default, so
transcripts stored before them read back as no positions and no envelope.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_COLUMNS = ("segments", "envelope")


def upgrade() -> None:
    with op.batch_alter_table("shop_inbox") as batch:
        for name in _COLUMNS:
            batch.add_column(sa.Column(name, sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("shop_inbox") as batch:
        for name in reversed(_COLUMNS):
            batch.drop_column(name)

"""Returns as a record the book itself can write (21 Nov 2026).

The returns ledger (0017) knew a return Mimi proposed from a pickup. An
imported book's returns were not a record at all, which is why a distributor's
own export could not carry them. This revision gives the row the facts an
export has and the ledger did not keep:

* ``rep_id``: who carries the goods back;
* ``raised_on`` and ``raised_day``: the day the return was raised, and its date
  ordinal, so "days open" is the same subtraction as every other day in the
  book;
* ``delivery_id``: the delivery the goods came in on, when the book names it;
* ``reason`` holds one of the closed list in ``mimi.types.RETURN_REASONS`` on
  an imported return, word for word, so a reason can be counted;
* the three proof columns every business record carries
  (``mimi.provenance``): a return is the distributor's own record, so
  ``estimated`` until something else witnesses it.

Additive and reversible: seven columns, all with a server default, so existing
rows keep a value and no table rewrite is needed. Nothing here changes an
existing column, so no expand/contract sequence is involved.

``status`` gains the book's own states (``pending``, ``in_transit``,
``received``, ``credited``, ``rejected``, ``mimi.types.RETURN_STATUSES``) beside
the ledger's (``drafted`` ... ``settled``): one column, one journey, and the
transition table in ``mimi.returns.ledger`` is what keeps the order.

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


def upgrade() -> None:
    op.add_column("returns", sa.Column("delivery_id", sa.String(64), nullable=False,
                                       server_default=""))
    op.add_column("returns", sa.Column("rep_id", sa.String(64), nullable=False,
                                       server_default=""))
    op.add_column("returns", sa.Column("raised_on", sa.String(10), nullable=False,
                                       server_default=""))
    op.add_column("returns", sa.Column("raised_day", sa.Integer(), nullable=False,
                                       server_default="0"))
    op.add_column("returns", sa.Column("proof_level", sa.String(16), nullable=False,
                                       server_default="estimated"))
    op.add_column("returns", sa.Column("witnesses", sa.Text(), nullable=False,
                                       server_default="[]"))
    op.add_column("returns", sa.Column("proof_policy_version", sa.String(16),
                                       nullable=False, server_default="v2"))


def downgrade() -> None:
    for column in ("proof_policy_version", "witnesses", "proof_level", "raised_day",
                   "raised_on", "rep_id", "delivery_id"):
        op.drop_column("returns", column)

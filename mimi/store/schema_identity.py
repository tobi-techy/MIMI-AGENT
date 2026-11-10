"""Identity tables: the distributor's people and the areas they cover.

Phase 1 slice 1 (10 Nov 2026, ``PHASE1-COMMAND.md`` section 4). The
command layer needs to know *who* is asking before it can say what they may
see, so these two tables come first and the reference tables watch them.

Defined on the same ``metadata`` as the rest of the store (imported once at
the bottom of ``mimi/store/schema.py``), so ``ensure_schema`` and alembic see
one schema. Alembic ``0021_identity`` creates exactly these tables;
``tests/test_store.py`` compares the two.

Not in here, on purpose:

* An agent is never a ``users`` row. Mimi and its helpers are participants
  with their own ``member_kind`` (``mimi/channels`` later); giving them a
  ``users`` row would let a role check treat a model as a person.
* A shop is never a ``users`` row either. A shop has a thread, not a seat.
"""

from __future__ import annotations

import sqlalchemy as sa

from mimi.store.schema import metadata

ROLES = ("owner", "supervisor", "rep", "brand_viewer")
STATUSES = ("invited", "active", "disabled")


users = sa.Table(
    "users",
    metadata,
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("user_id", sa.String(64), nullable=False),  # u_...
    # The identity provider's subject. Null until the person's first sign-in
    # links it; the seam (``MIMI_AUTH_MODE``) looks people up by it, so
    # it is unique per tenant when set. Two NULLs are allowed (not linked yet).
    sa.Column("external_subject", sa.String(200), nullable=True),
    # Phone OTP is a first-class identifier: a pilot user may have no email.
    sa.Column("email", sa.String(320), nullable=True),
    sa.Column("phone_e164", sa.String(32), nullable=True),
    sa.Column("name", sa.String(200), nullable=False),
    # owner | supervisor | rep | brand_viewer. Never read from a token claim:
    # this column is the only source of a role (``mimi.identity.roles``).
    sa.Column("role", sa.String(16), nullable=False),
    # invited | active | disabled. Only ``active`` is a caller.
    sa.Column("status", sa.String(16), nullable=False, default="invited"),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("last_seen_at", sa.String(40), nullable=True),
    # Who gave this person an account. NULL is the bootstrap owner (the first
    # row of a tenant, written by code, not by a person). Everyone else names
    # an owner or supervisor of the same tenant, so "who invited this rep" is
    # a row, not an investigation. Enforced in ``mimi.identity.store``.
    sa.Column("created_by_user_id", sa.String(64), nullable=True),
    sa.PrimaryKeyConstraint("tenant_id", "user_id"),
    sa.UniqueConstraint("tenant_id", "phone_e164", name="uq_users_tenant_phone"),
    sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    sa.UniqueConstraint("tenant_id", "external_subject", name="uq_users_tenant_subject"),
    # Phone only, email only, or both: never neither. A row nobody can sign in
    # as only looks like an account. ``store.create_user`` says the same thing
    # in a sentence; this is the last line.
    sa.CheckConstraint("phone_e164 IS NOT NULL OR email IS NOT NULL",
                       name="ck_users_has_identifier"),
)


# Which existing areas a supervisor covers. ``area`` is the book's own area
# value, stored trimmed and casefolded (``mimi.identity.areas.area_key``) so
# "Ikeja", "ikeja" and "Ikeja " are one territory: a typo must never hide a
# whole territory from the person who owns it. The display spelling comes from
# the shops and reps in that area, never from this column. The book has no
# supervisor edge at all (``reps`` is name, phone, areas; shops carry
# ``rep_id``), which is why the link has to live here. See docs/IDENTITY.md.
user_territories = sa.Table(
    "user_territories",
    metadata,
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("area", sa.String(200), nullable=False),  # normalised: trim + casefold
    sa.Column("added_at", sa.String(40), nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "user_id", "area"),
    # Slice 2: territory channels are found by area, not by person.
    sa.Index("ix_user_territories_tenant_area", "tenant_id", "area"),
)

IDENTITY_TABLES = (users, user_territories)

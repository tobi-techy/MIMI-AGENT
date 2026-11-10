"""The ``users`` rows: create, find, change, and the rules that need a query.

Three rules live here because a table cannot hold them:

* **One owner per tenant.** A unique index on ``role`` would also ban two
  disabled owners and would still allow a second ``active`` owner after a
  rename, so the check is in code, on every create and every role change.
* **Somebody created everyone else.** ``created_by_user_id`` names an owner or
  supervisor of the same tenant; only the bootstrap row may have none.
* **An identifier is required.** Phone OTP is first-class, so a user may have
  no email; email and Google sign-in is the other way round. One of the two,
  never neither.

Times come from ``mimi.clock.wall()`` (the one wall clock) through
``mimi.store.util.now_iso``.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from mimi.identity.areas import area_key
from mimi.identity.roles import ROLES, Role
from mimi.store import schema as s
from mimi.store.util import now_iso

STATUSES = ("invited", "active", "disabled")


class IdentityError(Exception):
    """A refusal the API turns into a 4xx with the sentence as written."""


class OwnerExists(IdentityError):
    pass


class DuplicateUser(IdentityError):
    pass


class InvalidUser(IdentityError):
    pass


@dataclass(frozen=True)
class User:
    tenant_id: str
    user_id: str
    role: str
    name: str
    status: str
    email: str | None = None
    phone_e164: str | None = None
    external_subject: str | None = None
    created_at: str = ""
    last_seen_at: str | None = None
    created_by_user_id: str | None = None

    @property
    def active(self) -> bool:
        return self.status == "active"

    def to_dict(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "role": self.role, "name": self.name,
                "status": self.status, "email": self.email, "phone": self.phone_e164,
                "last_seen_at": self.last_seen_at}


def _user(row: Any) -> User:
    return User(tenant_id=row.tenant_id, user_id=row.user_id, role=row.role, name=row.name,
                status=row.status, email=row.email, phone_e164=row.phone_e164,
                external_subject=row.external_subject, created_at=row.created_at,
                last_seen_at=row.last_seen_at, created_by_user_id=row.created_by_user_id)


def new_user_id() -> str:
    return f"u_{uuid4().hex[:16]}"


def normal_email(email: str | None) -> str | None:
    """Trimmed, lowercased: the same person typing their address differently
    must hit the same unique row."""
    if not email:
        return None
    text = email.strip().lower()
    return text or None


def to_e164(phone: str | None) -> str | None:
    """A Nigerian number in E.164, from the way people actually type it.

    ``08000000001`` -> ``+2348000000001``; an already-international number
    keeps its digits. Nothing else is guessed: a number too short to be a
    line is refused rather than stored as a key nobody can match."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if not digits:
        return None
    if digits.startswith("0") and len(digits) == 11:
        digits = "234" + digits[1:]
    if len(digits) < 10:
        raise InvalidUser("a phone number needs at least 10 digits")
    return "+" + digits


def _owners(conn: Connection, tenant: str) -> list[str]:
    return [r.user_id for r in conn.execute(
        sa.select(s.users.c.user_id)
        .where(s.users.c.tenant_id == tenant, s.users.c.role == "owner")).all()]


def create_user(conn: Connection, tenant_id: str, *, role: Role, name: str,
                email: str | None = None, phone: str | None = None,
                status: str = "invited", created_by_user_id: str | None = None,
                external_subject: str | None = None,
                user_id: str | None = None) -> User:
    """One new person in the distributor. Raises ``IdentityError``, never a
    bare database error, for the cases a caller can fix."""
    role = (role or "").strip().lower()  # type: ignore[assignment]
    if role not in ROLES:
        raise InvalidUser(f"unknown role {role!r}")
    if status not in STATUSES:
        raise InvalidUser(f"unknown status {status!r}")
    name = (name or "").strip()
    if not name:
        raise InvalidUser("a user needs a name")
    email_n = normal_email(email)
    phone_n = to_e164(phone)
    if not email_n and not phone_n:
        raise InvalidUser("a user needs a phone number or an email address")
    if role == "owner" and _owners(conn, tenant_id):
        raise OwnerExists("this distributor already has an owner")
    if created_by_user_id:
        maker = get_user(conn, tenant_id, created_by_user_id)
        if maker is None or maker.role not in ("owner", "supervisor"):
            raise InvalidUser("only an owner or a supervisor can create a user")
    uid = user_id or new_user_id()
    row = {"tenant_id": tenant_id, "user_id": uid, "external_subject": external_subject,
           "email": email_n, "phone_e164": phone_n, "name": name, "role": role,
           "status": status, "created_at": now_iso(), "last_seen_at": None,
           "created_by_user_id": created_by_user_id or None}
    try:
        conn.execute(s.users.insert().values(**row))
    except sa.exc.IntegrityError as exc:  # the unique constraints, in words
        raise DuplicateUser("that phone number or email is already in use here") from exc
    return _user(SimpleNamespace(**row))


def get_user(conn: Connection, tenant_id: str, user_id: str) -> User | None:
    row = conn.execute(sa.select(s.users)
                       .where(s.users.c.tenant_id == tenant_id,
                              s.users.c.user_id == user_id)).first()
    return None if row is None else _user(row)


def find_by_subject(conn: Connection, tenant_id: str, subject: str) -> User | None:
    if not subject:
        return None
    row = conn.execute(sa.select(s.users)
                       .where(s.users.c.tenant_id == tenant_id,
                              s.users.c.external_subject == subject)).first()
    return None if row is None else _user(row)


def find_subject_anywhere(conn: Connection, subject: str) -> list[User]:
    """Every row with this provider subject, across tenants.

    Used only when a token carries no tenant claim: the caller is accepted
    when the subject names exactly one row, and refused when it names more
    than one rather than picking a distributor."""
    if not subject:
        return []
    return [_user(r) for r in conn.execute(
        sa.select(s.users).where(s.users.c.external_subject == subject)).all()]


def find_by_phone(conn: Connection, tenant_id: str, phone: str | None) -> User | None:
    key = to_e164(phone) if phone else None
    if not key:
        return None
    row = conn.execute(sa.select(s.users)
                       .where(s.users.c.tenant_id == tenant_id,
                              s.users.c.phone_e164 == key)).first()
    return None if row is None else _user(row)


def find_by_email(conn: Connection, tenant_id: str, email: str | None) -> User | None:
    key = normal_email(email)
    if not key:
        return None
    row = conn.execute(sa.select(s.users)
                       .where(s.users.c.tenant_id == tenant_id,
                              s.users.c.email == key)).first()
    return None if row is None else _user(row)


def find_by_email_anywhere(conn: Connection, email: str | None) -> list[User]:
    """Every invited row with this address, across tenants.

    Used by the first-login link when the session names no distributor: one
    match is linked, more than one is refused rather than guessed."""
    key = normal_email(email)
    if not key:
        return []
    return [_user(r) for r in conn.execute(
        sa.select(s.users).where(s.users.c.email == key)).all()]


def ensure_owner(conn: Connection, tenant_id: str, *, phone: str | None,
                 name: str = "Owner", email: str | None = None,
                 external_subject: str | None = None) -> User:
    """The bootstrap owner: the first row of a tenant, written by code.

    Idempotent by phone, so seeding twice or a first sign-in after the seed
    lands on the same row. Links an IdP subject the first time one is known."""
    owners = _owners(conn, tenant_id)
    if owners:
        owner = get_user(conn, tenant_id, owners[0])
        assert owner is not None
        return owner
    existing = find_by_phone(conn, tenant_id, phone) if phone else None
    if existing is not None:
        set_role(conn, tenant_id, existing.user_id, "owner")
        set_status(conn, tenant_id, existing.user_id, "active")
        return get_user(conn, tenant_id, existing.user_id) or existing
    return create_user(conn, tenant_id, role="owner", name=name, email=email, phone=phone,
                       status="active", external_subject=external_subject)


def set_role(conn: Connection, tenant_id: str, user_id: str, role: Role) -> User:
    role = (role or "").strip().lower()  # type: ignore[assignment]
    if role not in ROLES:
        raise InvalidUser(f"unknown role {role!r}")
    user = get_user(conn, tenant_id, user_id)
    if user is None:
        raise InvalidUser("no such user")
    if role == "owner" and user.role != "owner":
        others = [u for u in _owners(conn, tenant_id) if u != user_id]
        if others:
            raise OwnerExists("this distributor already has an owner")
    conn.execute(s.users.update()
                 .where(s.users.c.tenant_id == tenant_id, s.users.c.user_id == user_id)
                 .values(role=role))
    return get_user(conn, tenant_id, user_id) or user


def set_status(conn: Connection, tenant_id: str, user_id: str, status: str) -> User:
    if status not in STATUSES:
        raise InvalidUser(f"unknown status {status!r}")
    user = get_user(conn, tenant_id, user_id)
    if user is None:
        raise InvalidUser("no such user")
    if user.role == "owner" and status != "active":
        raise IdentityError("the owner's own account cannot be disabled here: "
                            "hand the owner role to somebody else first")
    conn.execute(s.users.update()
                 .where(s.users.c.tenant_id == tenant_id, s.users.c.user_id == user_id)
                 .values(status=status))
    return get_user(conn, tenant_id, user_id) or user


def link_subject(conn: Connection, tenant_id: str, user_id: str, subject: str) -> User:
    """Attach the identity provider's subject on first sign-in."""
    if not subject:
        raise InvalidUser("an identity subject is required")
    user = get_user(conn, tenant_id, user_id)
    if user is None:
        raise InvalidUser("no such user")
    try:
        conn.execute(s.users.update()
                     .where(s.users.c.tenant_id == tenant_id,
                            s.users.c.user_id == user_id)
                     .values(external_subject=subject))
    except sa.exc.IntegrityError as exc:
        raise DuplicateUser("that identity is already linked to another user") from exc
    return get_user(conn, tenant_id, user_id) or user


def touch_last_seen(conn: Connection, tenant_id: str, user_id: str) -> None:
    """One write per request is too much; the caller batches it per request."""
    conn.execute(s.users.update()
                 .where(s.users.c.tenant_id == tenant_id, s.users.c.user_id == user_id)
                 .values(last_seen_at=now_iso()))


def list_users(conn: Connection, tenant_id: str) -> list[User]:
    return [_user(r) for r in conn.execute(
        sa.select(s.users).where(s.users.c.tenant_id == tenant_id)
        .order_by(s.users.c.created_at, s.users.c.user_id)).all()]


# ------------------------------------------------------------- territories

def set_territories(conn: Connection, tenant_id: str, user_id: str,
                    areas: list[str]) -> list[str]:
    """Replace a supervisor's territories. ``area`` values are normalised on
    the way in (``area_key``) so a typo cannot hide a whole territory."""
    user = get_user(conn, tenant_id, user_id)
    if user is None:
        raise InvalidUser("no such user")
    if user.role != "supervisor":
        raise IdentityError("only a supervisor has territories; "
                            "an owner sees everything and a rep has their beat")
    keys = {area_key(a) for a in areas or [] if area_key(a)}
    conn.execute(s.user_territories.delete().where(
        s.user_territories.c.tenant_id == tenant_id,
        s.user_territories.c.user_id == user_id))
    at = now_iso()
    if keys:
        conn.execute(s.user_territories.insert(), [
            {"tenant_id": tenant_id, "user_id": user_id, "area": k, "added_at": at}
            for k in sorted(keys)])
    return sorted(keys)


def territories(conn: Connection, tenant_id: str, user_id: str) -> list[str]:
    return sorted(r.area for r in conn.execute(
        sa.select(s.user_territories.c.area)
        .where(s.user_territories.c.tenant_id == tenant_id,
               s.user_territories.c.user_id == user_id)).all())

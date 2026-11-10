"""A caller's ground: the shops and reps they may see.

Resolved from the book as it already is, never invented:

* ``shops.area`` and ``shops.rep_id`` say where a shop is and who serves it.
* ``reps.areas`` says which areas a rep works (in this book it disagrees with
  the shops they serve, so shop coverage never reads it).
* ``user_territories`` says which areas a supervisor covers.

* owner: everything in the tenant.
* supervisor: the areas in their ``user_territories`` rows decide which shops
  (by ``shops.area``); the reps serving those areas decide "their reps", which
  is what a money approval for one of their own reps is checked against.
* rep: the single ``reps`` row whose phone is theirs, so their shops are the
  ones on their beat. A rep is not scoped by area: areas are shared, so an
  area match would show them another rep's shops.
* brand_viewer: nothing. The role is deferred and no user has it in the pilot.
* any other value: nothing. An unknown role fails safe.

An empty scope is a real answer, not a missing one: a supervisor with no
territories sees no shops and ``Scope.empty`` is true, so the console can say
"no territory assigned" instead of drawing an empty shop list. There is no
fallback to "everything" anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from mimi.identity.areas import area_key, area_keys
from mimi.identity.phones import phone_key, same_phone
from mimi.store import schema as s
from mimi.store.util import loads


@dataclass(frozen=True)
class Scope:
    """What a caller may see.

    ``cover_by`` says which axis decides a shop, and only one does:

    * ``all``: the owner's whole tenant.
    * ``area``: a supervisor's ground is the areas the owner gave them. Their
      ``rep_ids`` are the reps who serve shops there, and that set is only
      asked "is this rep theirs" (money approvals), never "which shops".
    * ``rep``: a rep's ground is the shops on their own beat.

    Two axes ORed together would let a supervisor's area grant them the other
    shops of every rep who happens to serve one shop in it. This world's
    ``reps.areas`` and the areas of the shops they serve do not agree, so that
    difference is not hypothetical.
    """

    global_: bool = False
    areas: frozenset[str] = frozenset()  # normalised area keys
    rep_ids: frozenset[str] = frozenset()
    cover_by: str = "area"  # "all" | "area" | "rep"

    @property
    def empty(self) -> bool:
        """True when this caller may see no shops at all (an unassigned
        supervisor, a rep with no book row, a deferred role)."""
        if self.global_:
            return False
        return not (self.areas if self.cover_by == "area" else self.rep_ids)

    def covers_shop(self, area: str | None, rep_id: str | None) -> bool:
        if self.global_:
            return True
        if self.cover_by == "rep":
            return bool(rep_id) and rep_id in self.rep_ids
        key = area_key(area)
        return bool(key) and key in self.areas

    def covers_rep(self, rep_id: str | None) -> bool:
        if self.global_:
            return True
        return bool(rep_id) and rep_id in self.rep_ids

    def to_dict(self) -> dict[str, Any]:
        """What ``GET /v1/me`` shows. The area display spelling is the
        console's to look up; these are the keys it matches on."""
        return {"all": self.global_, "empty": self.empty, "cover_by": self.cover_by,
                "areas": sorted(self.areas), "rep_ids": sorted(self.rep_ids)}


ALL = Scope(global_=True, cover_by="all")
NONE = Scope()


def scopes_for(conn: Connection, user: Any) -> Scope:
    """The scope of one ``users`` row. Never raises for a missing book row."""
    role = (user.role or "").strip().lower()
    tenant = user.tenant_id
    if role == "owner":
        return ALL
    if role == "supervisor":
        return _supervisor(conn, tenant, str(user.user_id))
    if role == "rep":
        return _rep(conn, tenant, user.phone_e164)
    return NONE


def _supervisor(conn: Connection, tenant: str, user_id: str) -> Scope:
    """The areas the owner gave this supervisor, and the reps who serve them.

    The reps are worked out from `shops.rep_id` in those areas, not from
    ``reps.areas``: in this book a rep's recorded areas and the areas of the
    shops they actually serve disagree (rep-1 lists Basiri and serves none of
    it), so ``shops`` is the ground truth. A rep serving one shop in the
    territory counts as theirs; it never widens which *shops* they see,
    because shop coverage is by area only."""
    rows = conn.execute(
        sa.select(s.user_territories.c.area)
        .where(s.user_territories.c.tenant_id == tenant,
               s.user_territories.c.user_id == user_id)).all()
    areas = frozenset(area_key(r.area) for r in rows if area_key(r.area))
    if not areas:
        return NONE
    rep_ids = {r.rep_id for r in conn.execute(
        sa.select(s.shops.c.rep_id, s.shops.c.area)
        .where(s.shops.c.tenant_id == tenant).distinct()).all()
        if r.rep_id and area_key(r.area) in areas}
    for r in conn.execute(sa.select(s.reps.c.rep_id, s.reps.c.areas)
                          .where(s.reps.c.tenant_id == tenant)).all():
        if area_keys(loads(r.areas, [])) & areas:
            rep_ids.add(r.rep_id)
    return Scope(areas=areas, rep_ids=frozenset(rep_ids), cover_by="area")


def _rep(conn: Connection, tenant: str, phone: str | None) -> Scope:
    """The caller's own ``reps`` row, by phone. No row: an empty scope."""
    if not phone_key(phone):
        return NONE
    rows = conn.execute(sa.select(s.reps.c.rep_id, s.reps.c.phone)
                        .where(s.reps.c.tenant_id == tenant)).all()
    mine = [r.rep_id for r in rows if same_phone(r.phone, phone)]
    if len(mine) != 1:  # none, or an ambiguous book: never guess
        return NONE
    return Scope(rep_ids=frozenset(mine), cover_by="rep")

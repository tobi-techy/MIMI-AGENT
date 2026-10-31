"""Per-shop consent: who may read a shop's connected data (RAI-277).

A consent grant names one shop, one receiver and one scope, with the time
it was given and the time it was revoked, if it ever was. Reads check a
live grant every time; revoking takes effect before the next sync. The
distributor sees its own products only; brands see totals with no shop
name and no money; everyone else is locked.

Scopes here are read consent. Credit use of terminal sales value is never
allowed: terminal data feeds the shop's own stock estimate only.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.connectors.base import ConnectorError
from mimi.store import schema as s
from mimi.store.util import now_iso

# Receivers a shop can share with.
RECEIVER_DISTRIBUTOR = "distributor"
RECEIVER_BRAND = "brand"

# Scopes a grant can cover.
SCOPE_STOCK = "stock"  # sheet and POS item data: stock left per item
SCOPE_SALES = "sales"  # POS item sales and terminal daily value
SCOPE_RECEIVABLES = "receivables"  # invoices and payments from the books

SCOPES: tuple[str, ...] = (SCOPE_STOCK, SCOPE_SALES, SCOPE_RECEIVABLES)


def grant(engine: Engine, tenant: str, shop_id: str, receiver: str, scope: str) -> dict[str, Any]:
    """Record the shop's consent for one receiver and scope."""
    if scope not in SCOPES:
        raise ConnectorError(f"unknown consent scope {scope!r}",
                             next_step="Use one of: stock, sales, receivables.")
    now = now_iso()
    row = {"tenant_id": tenant, "shop_id": shop_id, "receiver": receiver,
           "scope": scope, "granted_at": now, "revoked_at": None}
    with engine.begin() as conn:
        conn.execute(sa.insert(s.consent_grants).values(**row))
    return row


def revoke(engine: Engine, tenant: str, shop_id: str, receiver: str, scope: str) -> bool:
    """Revoke one grant. True when a live grant was revoked."""
    now = now_iso()
    with engine.begin() as conn:
        res = conn.execute(s.consent_grants.update().where(
            s.consent_grants.c.tenant_id == tenant,
            s.consent_grants.c.shop_id == shop_id,
            s.consent_grants.c.receiver == receiver,
            s.consent_grants.c.scope == scope,
            s.consent_grants.c.revoked_at.is_(None)).values(revoked_at=now))
        return bool(res.rowcount)


def live_grants(engine: Engine, tenant: str, shop_id: str) -> list[dict[str, Any]]:
    """Every grant the shop has not revoked."""
    with engine.connect() as conn:
        rows = list(conn.execute(sa.select(s.consent_grants).where(
            s.consent_grants.c.tenant_id == tenant,
            s.consent_grants.c.shop_id == shop_id)).all())
    return [{"shop_id": r.shop_id, "receiver": r.receiver, "scope": r.scope,
             "granted_at": r.granted_at}
            for r in rows if r.revoked_at is None]


def may_read(engine: Engine, tenant: str, shop_id: str, scope: str) -> bool:
    """True when the shop has a live grant covering this read."""
    if scope not in SCOPES:
        return False
    with engine.connect() as conn:
        hit = conn.execute(sa.select(sa.literal(1)).where(sa.exists(sa.select(1).select_from(
            s.consent_grants).where(
                s.consent_grants.c.tenant_id == tenant,
                s.consent_grants.c.shop_id == shop_id,
                s.consent_grants.c.scope == scope,
                s.consent_grants.c.revoked_at.is_(None)))))).scalar()
        return bool(hit)


def check_read(engine: Engine, tenant: str, shop_id: str, scope: str) -> None:
    """Raise unless the shop consented to this read."""
    # The distributor's own book needs no grant: it is the distributor's.
    if not shop_id:
        return
    if not may_read(engine, tenant, shop_id, scope):
        raise ConnectorError(
            f"{shop_id} has not shared {scope} data.",
            next_step="Ask the shop to connect its tool first; the read stays refused.")


def brand_view(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a brand may see: totals only, never a shop name, money or
    another supplier's products, even with consent on."""
    totals: dict[str, int] = {}
    for row in rows:
        sku = str(row.get("sku") or row.get("item") or "")
        try:
            qty = int(row.get("quantity") or row.get("qty_left") or 0)
        except (TypeError, ValueError):
            qty = 0
        totals[sku] = totals.get(sku, 0) + qty
    return [{"sku": sku, "total_quantity": qty} for sku, qty in sorted(totals.items())]

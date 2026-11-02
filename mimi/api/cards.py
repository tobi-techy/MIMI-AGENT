"""Answer cards for the owner console, read from the book by code (docs/HANDOFFS.md).

A helper's answer to the owner comes as one short sentence plus a card:

* ``shop_row``: one shop (the highest debtor, or the shop the owner named):
  owed, overdue, days late, credit limit, with ``remind`` and ``open_shop``.
* ``list``: open orders (``order/open-orders``), shops running low
  (``stock/low-stock-report``) or debtors (``collector/chase``).

Every figure here is summed in integer kobo from the book (receivables,
approvals, deliveries); never from model text. A card is built only for the
owner's own console messages: a shop never sees another shop's row.

``code_answer`` answers the lookups that need no model at all (open orders,
low stock): the sentence is written by code from the same rows as the card.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.harness import lexicon
from mimi.harness.handoff import Card, CardAction, ChipIntent, ListItem, ShopRow
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.store.util import loads

# Skills a card is built for, and the lookups code answers on its own.
CARD_SKILLS: frozenset[str] = frozenset({
    "collector/chase", "collector/collections", "collector/payment-check",
    "credit/credit-review", "order/open-orders", "stock/low-stock-report"})
CODE_LOOKUPS: frozenset[str] = frozenset({"order/open-orders", "stock/low-stock-report"})
MAX_ITEMS = 8
ORDER_TOOLS = ("submit_order", "revise_order")

_TOP = re.compile(r"\b(?:highest|biggest|largest|top|most|worst|major)\b|\bowes? (?:me )?the most\b",
                  re.I)


def naira(kobo: int) -> str:
    return f"N{kobo / 100:,.0f}" if kobo % 100 == 0 else f"N{kobo / 100:,.2f}"


@dataclass(frozen=True)
class Answer:
    card: Card | None
    sentence: str  # one sentence written by code ("" when the helper's own is kept)


# ---------------------------------------------------------------- debtors


def _debtors(go: SqlGo, tenant: str) -> list[ShopRow]:
    """Every shop that owes, highest first, summed in code."""
    owed: dict[str, int] = defaultdict(int)
    over: dict[str, int] = defaultdict(int)
    late: dict[str, int] = defaultdict(int)
    n: dict[str, int] = defaultdict(int)
    names: dict[str, str] = {}
    for r in go.receivables(tenant, None):
        sid = str(r["shop_id"])
        owed[sid] += int(r["outstanding_kobo"])
        n[sid] += 1
        names[sid] = str(r["shop_name"])
        if int(r["days_overdue"]) > 0:
            over[sid] += int(r["outstanding_kobo"])
            late[sid] = max(late[sid], int(r["days_overdue"]))
    if not owed:
        return []
    with go.engine.connect() as conn:
        limits = {row.shop_id: int(row.credit_limit_kobo) for row in conn.execute(
            sa.select(s.shops.c.shop_id, s.shops.c.credit_limit_kobo).where(
                s.shops.c.tenant_id == tenant, s.shops.c.shop_id.in_(list(owed))))}
    rows = [ShopRow(shop_id=sid, name=names[sid], owed_kobo=owed[sid],
                    owed_naira=naira(owed[sid]), overdue_kobo=over[sid],
                    overdue_naira=naira(over[sid]), days_late=late[sid],
                    credit_limit_kobo=limits.get(sid, 0),
                    credit_limit_naira=naira(limits.get(sid, 0)), open_invoices=n[sid])
            for sid in owed if owed[sid] > 0]
    return sorted(rows, key=lambda r: (-r.owed_kobo, -r.overdue_kobo, r.shop_id))


def _named_shop(go: SqlGo, tenant: str, text: str) -> str:
    """The one shop the owner's words name (an id or a book name), or ""."""
    m = re.search(r"\bshop-\d{3}\b", text, re.I)
    if m:
        return m.group(0).lower()
    folded = lexicon.fold(text)
    hits = []
    for name, sid in sorted(go.known_shops(tenant).items()):
        key = lexicon.fold(name)
        words = [w for w in re.findall(r"[a-z0-9&']+", key) if len(w) > 2]
        head = " ".join(words[:2])
        if key and (key in folded or (head and len(words) >= 2 and head in folded)):
            hits.append(sid)
    return hits[0] if len(set(hits)) == 1 else ""


def _shop_actions(row: ShopRow) -> list[CardAction]:
    return [
        CardAction(action="remind", label=f"Remind {row.name}",
                   intent=ChipIntent(kind="turn", assign_to="collector",
                                     text=f"Send {row.shop_id} a soft reminder about the "
                                          "overdue balance")),
        CardAction(action="open_shop", label="Open shop",
                   intent=ChipIntent(kind="open", target=f"shop:{row.shop_id}")),
    ]


def shop_row_card(row: ShopRow, skill: str, title: str, as_of: str) -> Card:
    return Card(kind="shop_row", title=title, skill=skill, shop_row=row, total=1,
                actions=_shop_actions(row), as_of=as_of)


def _late(row: ShopRow) -> str:
    if row.days_late <= 0:
        return "not overdue"
    return f"{row.days_late} day{'s' if row.days_late != 1 else ''} late"


def debtor_answer(go: SqlGo, tenant: str, skill: str, text: str, as_of: str) -> Answer:
    rows = _debtors(go, tenant)
    named = _named_shop(go, tenant, text)
    if named:
        row = next((r for r in rows if r.shop_id == named), None)
        if row is None:
            return Answer(None, "")
        return Answer(shop_row_card(row, skill, row.name, as_of),
                      f"{row.name} owes {row.owed_naira}, {_late(row)}.")
    if not rows:
        return Answer(Card(kind="list", title="Shops that owe", skill=skill, total=0,
                           empty_text="No shop owes anything in the book.", as_of=as_of),
                      "No shop owes anything in the book.")
    if skill == "collector/chase" and _TOP.search(text):
        top = rows[0]
        return Answer(shop_row_card(top, skill, "Highest debtor", as_of),
                      f"{top.name} owes the most: {top.owed_naira}, {_late(top)}.")
    if skill != "collector/chase":
        return Answer(None, "")
    items = [ListItem(item_id=r.shop_id, label=r.name, shop_id=r.shop_id,
                      amount_kobo=r.owed_kobo, amount_naira=r.owed_naira, status=_late(r))
             for r in rows[:MAX_ITEMS]]
    total = sum(r.owed_kobo for r in rows)
    return Answer(Card(kind="list", title="Shops that owe", skill=skill, items=items,
                       total=len(rows), total_kobo=total, total_naira=naira(total),
                       as_of=as_of),
                  f"{len(rows)} shop{'s' if len(rows) != 1 else ''} owe {naira(total)} in all; "
                  f"{rows[0].name} owes the most.")


# ----------------------------------------------------------------- orders


def open_orders(engine: Engine, tenant: str) -> list[dict[str, Any]]:
    """Orders waiting in approvals, newest first (the newest version of each)."""
    with engine.connect() as conn:
        rows = conn.execute(sa.select(s.approvals).where(
            s.approvals.c.tenant_id == tenant, s.approvals.c.tool.in_(ORDER_TOOLS),
            s.approvals.c.status == "pending").order_by(
            s.approvals.c.created_ts.desc(), s.approvals.c.approval_id)).all()
        names = {r.shop_id: r.name for r in conn.execute(sa.select(
            s.shops.c.shop_id, s.shops.c.name).where(s.shops.c.tenant_id == tenant))}
    out = []
    for a in rows:
        p = loads(a.payload, {})
        lines = list(p.get("lines") or [])
        qty = sum(int(ln.get("quantity") or 0) for ln in lines)
        total = sum(int(ln.get("line_total_kobo") or 0) for ln in lines)
        out.append({"approval_id": a.approval_id, "order_id": str(p.get("order_id") or ""),
                    "shop_id": a.shop_id, "shop_name": names.get(a.shop_id, a.shop_id),
                    "lines": len(lines), "qty": qty, "total_kobo": total,
                    "over_credit": bool(p.get("over_credit"))})
    return out


def orders_answer(engine: Engine, tenant: str, as_of: str) -> Answer:
    orders = open_orders(engine, tenant)
    skill = "order/open-orders"
    if not orders:
        return Answer(Card(kind="list", title="Open orders", skill=skill, total=0,
                           empty_text="No shop order is waiting right now.", as_of=as_of,
                           actions=[]),
                      "No shop order is waiting for you right now.")
    items = [ListItem(item_id=o["order_id"] or o["approval_id"],
                      label=f"{o['shop_name']}: {o['lines']} line{'s' if o['lines'] != 1 else ''}",
                      qty=o["qty"], unit="units", shop_id=o["shop_id"],
                      status="over credit, waiting for you" if o["over_credit"]
                      else "waiting for you",
                      amount_kobo=o["total_kobo"], amount_naira=naira(o["total_kobo"]))
             for o in orders[:MAX_ITEMS]]
    actions = [CardAction(action="open_approvals", label="Open approvals",
                          intent=ChipIntent(kind="open", target="approvals"))]
    total = sum(o["total_kobo"] for o in orders)
    n = len(orders)
    return Answer(Card(kind="list", title="Open orders", skill=skill, items=items, total=n,
                       total_kobo=total, total_naira=naira(total), actions=actions,
                       as_of=as_of),
                  f"{n} order{'s are' if n != 1 else ' is'} waiting for your approval, "
                  f"{naira(total)} in all.")


# ------------------------------------------------------------------ stock


def low_stock(go: SqlGo, tenant: str) -> list[dict[str, Any]]:
    """Items code estimates will run out within 3 days, soonest first.

    The same estimator Stock uses (``mimi.stock``): from deliveries and the
    shops' own taps, never a shop's count shown as such."""
    from mimi.harness.registry import ToolContext
    from mimi.stock.estimator import estimate
    from mimi.stock.intents import band_for
    from mimi.stock.items import display_name
    from mimi.stock.tools import counts, histories

    ctx = cast(ToolContext, SimpleNamespace(book=go, tenant_id=tenant))
    with go.engine.connect() as conn:
        shops = [(r.shop_id, r.name) for r in conn.execute(sa.select(
            s.shops.c.shop_id, s.shops.c.name).where(s.shops.c.tenant_id == tenant)
            .order_by(s.shops.c.shop_id))]
    out = []
    for sid, name in shops:
        hist, day = histories(ctx, sid)
        cs = counts(ctx, sid)
        for sku, h in hist.items():
            e = estimate(h, cs.get(sku, []), day)
            if e is None or e.days_to_run_out is None:
                continue
            band = band_for(e.days_to_run_out)
            if band != "under_3_days":
                continue
            out.append({"shop_id": sid, "shop_name": name, "sku": sku,
                        "item": display_name(h.name), "unit": h.unit,
                        "days": float(e.days_to_run_out), "band": band})
    return sorted(out, key=lambda x: (x["days"], x["shop_id"], x["sku"]))


def stock_answer(go: SqlGo, tenant: str, as_of: str) -> Answer:
    rows = low_stock(go, tenant)
    skill = "stock/low-stock-report"
    if not rows:
        return Answer(Card(kind="list", title="Running low", skill=skill, total=0,
                           empty_text="No shop looks low in the next 3 days.", as_of=as_of),
                      "No shop looks likely to run out in the next 3 days.")
    items = [ListItem(item_id=f"{r['shop_id']}:{r['sku']}", label=f"{r['item']}, {r['shop_name']}",
                      unit=r["unit"], shop_id=r["shop_id"], status="may run out within 3 days")
             for r in rows[:MAX_ITEMS]]
    shops = len({r["shop_id"] for r in rows})
    return Answer(Card(kind="list", title="Running low", skill=skill, items=items,
                       total=len(rows), as_of=as_of),
                  f"{len(rows)} item{'s' if len(rows) != 1 else ''} at {shops} "
                  f"shop{'s' if shops != 1 else ''} may run out within 3 days.")


# ------------------------------------------------------------------- entry


def answer_for(engine: Engine, tenant: str, skill: str, text: str, *,
               as_of: str = "") -> Answer:
    """The card (and code sentence) for an owner's answer on ``skill``; ``Answer(None, "")``
    when the skill has none. Never raises: a read error gives no card."""
    if skill not in CARD_SKILLS:
        return Answer(None, "")
    go = SqlGo(engine)
    try:
        if not as_of:
            as_of = go.today_for(tenant).isoformat()
        if skill == "order/open-orders":
            return orders_answer(engine, tenant, as_of)
        if skill == "stock/low-stock-report":
            return stock_answer(go, tenant, as_of)
        return debtor_answer(go, tenant, skill, text, as_of)
    except Exception:  # a card is extra: the answer still goes out without it
        import logging

        logging.getLogger(__name__).warning("card for %s failed", skill, exc_info=True)
        return Answer(None, "")


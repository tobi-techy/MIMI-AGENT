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
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi import owed
from mimi.harness import lexicon
from mimi.harness.handoff import Card, CardAction, ChipIntent, ListItem, ShopRow
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.store.util import loads

# Skills a card is built for, and the lookups code answers on its own.
CARD_SKILLS: frozenset[str] = frozenset({
    "collector/chase", "collector/collections", "collector/payment-check",
    "credit/credit-review", "order/open-orders", "stock/low-stock-report",
    "mimi/owner-brief"})
CODE_LOOKUPS: frozenset[str] = frozenset({"order/open-orders", "stock/low-stock-report",
                                          "mimi/owner-brief"})
MAX_ITEMS = 8
ORDER_TOOLS = ("submit_order", "revise_order")

# "top 5", "which stores owe the most": the owner asked for several shops, so a
# list, not the one highest debtor.
_MANY = re.compile(r"\btop\s+\d+\b|\b\d+\s+(?:shops|stores|debtors|customers)\b"
                   r"|\b(?:shops|stores|debtors|customers|outlets|people)\b", re.I)
_TOP = re.compile(r"\b(?:highest|biggest|largest|top|most|worst|major)\b|\bowes? (?:me )?the most\b",
                  re.I)


def naira(kobo: int) -> str:
    return owed.naira(kobo)


@dataclass(frozen=True)
class Answer:
    card: Card | None
    sentence: str  # one sentence written by code ("" when the helper's own is kept)


# ---------------------------------------------------------------- debtors


def _debtors(go: SqlGo, tenant: str) -> list[ShopRow]:
    """Every shop that owes, most overdue first, summed once in code (``mimi.owed``)."""
    book = owed.totals(go.receivables(tenant, None))
    if not book.shops:
        return []
    with go.engine.connect() as conn:
        limits = {row.shop_id: int(row.credit_limit_kobo) for row in conn.execute(
            sa.select(s.shops.c.shop_id, s.shops.c.credit_limit_kobo).where(
                s.shops.c.tenant_id == tenant, s.shops.c.shop_id.in_(list(book.shops))))}
    rows = [ShopRow(shop_id=b.shop_id, name=b.shop_name, owed_kobo=b.owed_kobo,
                    owed_naira=naira(b.owed_kobo), overdue_kobo=b.overdue_kobo,
                    overdue_naira=naira(b.overdue_kobo), days_late=b.oldest_days_overdue,
                    credit_limit_kobo=limits.get(b.shop_id, 0),
                    credit_limit_naira=naira(limits.get(b.shop_id, 0)),
                    open_invoices=b.open_invoices)
            for b in book.shops.values() if b.owed_kobo > 0]
    return sorted(rows, key=lambda r: (-r.overdue_kobo, -r.owed_kobo, r.shop_id))


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


def _overdue_line(rows: list[ShopRow]) -> str:
    over = sum(r.overdue_kobo for r in rows)
    n = sum(1 for r in rows if r.overdue_kobo > 0)
    return f"{naira(over)} overdue across {n} shop{'s' if n != 1 else ''}"


def debtor_answer(go: SqlGo, tenant: str, skill: str, text: str, as_of: str) -> Answer:
    rows = _debtors(go, tenant)
    named = _named_shop(go, tenant, text)
    if named:
        row = next((r for r in rows if r.shop_id == named), None)
        if row is None:
            return Answer(None, "")
        return Answer(shop_row_card(row, skill, row.name, as_of),
                      f"{row.name} owes {row.owed_naira}, {row.overdue_naira} of it overdue, "
                      f"{_late(row)}.")
    if not rows:
        return Answer(Card(kind="list", title="Shops that owe", skill=skill, total=0,
                           empty_text="No shop owes anything in the book.", as_of=as_of),
                      "No shop owes anything in the book.")
    if skill == "collector/chase" and _TOP.search(text) and not _MANY.search(text):
        top = rows[0]
        return Answer(shop_row_card(top, skill, "Highest debtor", as_of),
                      f"{top.name} owes the most: {top.overdue_naira} overdue "
                      f"({top.owed_naira} owed in all, {_late(top)}), out of "
                      f"{_overdue_line(rows)}.")
    if skill != "collector/chase":
        return Answer(None, "")
    m = re.search(r"\btop\s+(\d+)\b|\b(\d+)\s+(?:shops|stores|debtors|customers)\b", text, re.I)
    want = int(next(g for g in m.groups() if g)) if m else MAX_ITEMS
    want = max(1, min(want, MAX_ITEMS))
    items = [ListItem(item_id=r.shop_id, label=r.name, shop_id=r.shop_id,
                      amount_kobo=r.overdue_kobo, amount_naira=r.overdue_naira,
                      status=(f"overdue, {_late(r)}; {r.owed_naira} owed in all"
                              if r.overdue_kobo else f"not overdue; {r.owed_naira} owed"))
             for r in rows[:want]]
    over = sum(r.overdue_kobo for r in rows)
    return Answer(Card(kind="list", title="Overdue by shop", skill=skill, items=items,
                       total=len(rows), total_kobo=over, total_naira=naira(over),
                       as_of=as_of),
                  f"{_overdue_line(rows)}; {rows[0].name} owes the most, "
                  f"{rows[0].overdue_naira} overdue.")


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


def stock_answer(go: SqlGo, tenant: str, as_of: str, text: str = "") -> Answer:
    rows = low_stock(go, tenant)
    skill = "stock/low-stock-report"
    named = _named_shop(go, tenant, text) if text else ""
    if named:
        # "What's running out at Mama Sola Provisions?": that shop only.
        rows = [r for r in rows if r["shop_id"] == named]
        name = go.known_shops(tenant)
        shop = next((n for n, sid in name.items() if sid == named), named)
        if not rows:
            return Answer(Card(kind="list", title=f"Running low: {shop}", skill=skill,
                               total=0, as_of=as_of,
                               empty_text=f"Nothing at {shop} looks low in the next 3 days."),
                          f"Nothing at {shop} looks likely to run out in the next 3 days.")
        items = [ListItem(item_id=f"{r['shop_id']}:{r['sku']}", label=r["item"],
                          unit=r["unit"], shop_id=r["shop_id"],
                          status="may run out within 3 days") for r in rows[:MAX_ITEMS]]
        return Answer(Card(kind="list", title=f"Running low: {shop}", skill=skill,
                           items=items, total=len(rows), as_of=as_of),
                      f"{len(rows)} item{'s' if len(rows) != 1 else ''} at {shop} may run "
                      "out within 3 days.")
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


# ------------------------------------------------------------------ brief

BRIEF_TOP = 3
FLAG_WINDOW_DAYS = 7


def brief_facts(engine: Engine, go: SqlGo, tenant: str) -> dict[str, Any]:
    """Everything the morning brief says, read and summed from the book in code.

    * who owes: total overdue and the top debtors (``mimi.owed``, the same sum
      as Collector, the cards and ``GET /v1/shops``);
    * what came in yesterday (the book's day before today): payments and
      their total (a fake alert counts as nothing), how many the bank feed
      shows, and shop orders that reached approvals that day;
    * what needs the owner today: approvals waiting, credit asks waiting,
      payments flagged in the last 7 days (fake alerts, unmatched), open
      disputes, and items the stock estimator says may run out within 3 days.
    """
    from datetime import UTC, datetime, timedelta

    from mimi.tz import lagos_day

    today = go.today_for(tenant)
    yday = (today - timedelta(days=1)).isoformat()
    rows = _debtors(go, tenant)
    with engine.connect() as conn:
        live_day = int(conn.execute(sa.select(s.tenants.c.live_day).where(
            s.tenants.c.tenant_id == tenant)).scalar() or 0)
        feed = {r.payment_id for r in conn.execute(sa.select(s.bank_feed.c.payment_id).where(
            s.bank_feed.c.tenant_id == tenant))}
        pays = conn.execute(sa.select(s.payments).where(
            s.payments.c.tenant_id == tenant,
            s.payments.c.day >= live_day - FLAG_WINDOW_DAYS)).all()
        apr = conn.execute(sa.select(s.approvals.c.tool, s.approvals.c.status,
                                     s.approvals.c.created_ts, s.approvals.c.helper).where(
            s.approvals.c.tenant_id == tenant)).all()
        credit_asks = int(conn.execute(sa.select(sa.func.count()).select_from(
            s.credit_requests).where(s.credit_requests.c.tenant_id == tenant,
                                     s.credit_requests.c.status == "pending")).scalar() or 0)
        disputes = int(conn.execute(sa.select(sa.func.count()).select_from(s.disputes).where(
            s.disputes.c.tenant_id == tenant, s.disputes.c.status == "open")).scalar() or 0)
    came = [p for p in pays if p.paid_on == yday and not p.fake_alert]
    flagged = [p for p in pays if p.fake_alert or p.status == "unmatched"]

    def day_of(ts: float) -> str:
        return lagos_day(datetime.fromtimestamp(float(ts), UTC)).isoformat()

    orders_yday = [a for a in apr if a.tool in ORDER_TOOLS and day_of(a.created_ts) == yday]
    pending = [a for a in apr if a.status == "pending"]
    low = low_stock(go, tenant)
    return {
        "as_of": today.isoformat(), "yesterday": yday,
        "overdue_kobo": sum(r.overdue_kobo for r in rows),
        "overdue_shops": sum(1 for r in rows if r.overdue_kobo > 0),
        "owed_kobo": sum(r.owed_kobo for r in rows),
        "top": [r for r in rows if r.overdue_kobo > 0][:BRIEF_TOP],
        "paid_n": len(came), "paid_kobo": sum(int(p.amount_kobo) for p in came),
        "paid_in_bank_n": sum(1 for p in came if p.payment_id in feed),
        "orders_n": len(orders_yday),
        "approvals_n": len(pending),
        "orders_waiting_n": sum(1 for a in pending if a.tool in ORDER_TOOLS),
        "credit_asks_n": credit_asks,
        "flagged_n": len(flagged), "disputes_n": disputes,
        "low_items_n": len(low), "low_shops_n": len({r["shop_id"] for r in low}),
    }


def _n(k: int, one: str, many: str = "") -> str:
    return f"{k} {one if k == 1 else (many or one + 's')}"


def brief_answer(engine: Engine, go: SqlGo, tenant: str, as_of: str) -> Answer:
    f = brief_facts(engine, go, tenant)
    skill = "mimi/owner-brief"
    items: list[ListItem] = []
    if f["overdue_kobo"]:
        items.append(ListItem(item_id="owes:total", label="Overdue, all shops",
                              amount_kobo=f["overdue_kobo"],
                              amount_naira=naira(f["overdue_kobo"]),
                              status=f"who owes: {_n(f['overdue_shops'], 'shop')}"))
    for r in f["top"]:
        items.append(ListItem(item_id=f"owes:{r.shop_id}", label=r.name, shop_id=r.shop_id,
                              amount_kobo=r.overdue_kobo, amount_naira=r.overdue_naira,
                              status=f"who owes: {_late(r)}"))
    items.append(ListItem(item_id="in:payments", label=f"Payments yesterday ({f['paid_n']})",
                          qty=f["paid_n"], amount_kobo=f["paid_kobo"],
                          amount_naira=naira(f["paid_kobo"]),
                          status=f"came in: {f['paid_in_bank_n']} seen in the bank feed"))
    items.append(ListItem(item_id="in:orders", label=f"New shop orders yesterday ({f['orders_n']})",
                          qty=f["orders_n"], status="came in"))
    needs = [("needs:approvals", "Approvals waiting", f["approvals_n"]),
             ("needs:credit", "Credit asks waiting", f["credit_asks_n"]),
             ("needs:flagged", "Payments flagged (last 7 days)", f["flagged_n"]),
             ("needs:disputes", "Open delivery disputes", f["disputes_n"]),
             ("needs:stock", f"Items that may run out in 3 days ({_n(f['low_shops_n'], 'shop')})",
              f["low_items_n"])]
    for iid, label, k in needs:
        items.append(ListItem(item_id=iid, label=label, qty=k, status="needs you today"))
    actions = [CardAction(action="open_approvals", label="Open approvals",
                          intent=ChipIntent(kind="open", target="approvals")),
               CardAction(action="ask", label="Who owes the most?",
                          intent=ChipIntent(kind="turn", assign_to="collector",
                                            text="Who is the highest debtor?"))]
    card = Card(kind="list", title="Morning brief", skill=skill, items=items,
                total=len(items), actions=actions, as_of=f["as_of"] or as_of)
    owes = (f"{naira(f['overdue_kobo'])} is overdue across {_n(f['overdue_shops'], 'shop')}"
            + (f"; {f['top'][0].name} owes the most ({f['top'][0].overdue_naira})"
               if f["top"] else "")) if f["overdue_kobo"] else "No shop is overdue"
    came = (f"Yesterday {naira(f['paid_kobo'])} came in from {_n(f['paid_n'], 'payment')}"
            if f["paid_n"] else "No payment came in yesterday")
    came += (f" and {_n(f['orders_n'], 'new shop order')}" if f["orders_n"]
             else " and no new shop orders")
    todo = [x for x in (
        _n(f["approvals_n"], "approval") + " waiting" if f["approvals_n"] else "",
        _n(f["credit_asks_n"], "credit ask") if f["credit_asks_n"] else "",
        _n(f["flagged_n"], "flagged payment") if f["flagged_n"] else "",
        _n(f["disputes_n"], "open dispute") if f["disputes_n"] else "",
        (_n(f["low_items_n"], "item") + " may run out within 3 days")
        if f["low_items_n"] else "") if x]
    need = ("Needs you today: " + ", ".join(todo) + ".") if todo else (
        "Nothing needs you today.")
    return Answer(card, f"Good morning. {owes}. {came}. {need}")



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
            return stock_answer(go, tenant, as_of, text)
        if skill == "mimi/owner-brief":
            return brief_answer(engine, go, tenant, as_of)
        return debtor_answer(go, tenant, skill, text, as_of)
    except Exception:  # a card is extra: the answer still goes out without it
        import logging

        logging.getLogger(__name__).warning("card for %s failed", skill, exc_info=True)
        return Answer(None, "")


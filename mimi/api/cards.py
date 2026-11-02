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
from mimi.harness.handoff import Card, CardAction, ChipIntent, LimitChange, ListItem, ShopRow
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.store.util import loads

# Skills a card is built for, and the lookups code answers on its own.
CARD_SKILLS: frozenset[str] = frozenset({
    "collector/chase", "collector/collections", "collector/payment-check",
    "credit/credit-review", "order/open-orders", "stock/low-stock-report",
    "mimi/owner-brief", "order/reorder", "proof/proofs-today", "collector/rep-visit"})
CODE_LOOKUPS: frozenset[str] = frozenset({"order/open-orders", "stock/low-stock-report",
                                          "mimi/owner-brief", "order/reorder",
                                          "proof/proofs-today"})
# Owner reads a helper used to answer with a model run (3 to 6 Muse Spark
# calls, 30 to 60 s on the demo, 30 Oct 2026 run 2). With no action asked
# for, code answers them from the book in well under a second (``code_read``).
READ_SKILLS: frozenset[str] = frozenset({"collector/chase", "collector/collections",
                                         "credit/credit-review"})
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
    rows = [_row(b.shop_id, b.shop_name, b.owed_kobo, b.overdue_kobo, b.oldest_days_overdue,
                 limits.get(b.shop_id, 0), b.open_invoices)
            for b in book.shops.values() if b.owed_kobo > 0]
    return sorted(rows, key=lambda r: (-r.overdue_kobo, -r.owed_kobo, r.shop_id))


def _row(shop_id: str, name: str, owed_kobo: int, overdue_kobo: int, days_late: int,
         limit_kobo: int, open_invoices: int = 0) -> ShopRow:
    over = max(0, owed_kobo - limit_kobo) if limit_kobo > 0 else 0
    return ShopRow(shop_id=shop_id, name=name, owed_kobo=owed_kobo, owed_naira=naira(owed_kobo),
                   overdue_kobo=overdue_kobo, overdue_naira=naira(overdue_kobo),
                   days_late=days_late, credit_limit_kobo=limit_kobo,
                   credit_limit_naira=naira(limit_kobo), open_invoices=open_invoices,
                   over_limit=over > 0, over_limit_by_kobo=over,
                   over_limit_by_naira=naira(over) if over else "")


def shop_row_for(go: SqlGo, tenant: str, shop_id: str) -> ShopRow | None:
    """One shop's row, whether or not it owes (a limit question names any shop)."""
    book = owed.totals(go.receivables(tenant, shop_id))
    with go.engine.connect() as conn:
        r = conn.execute(sa.select(s.shops.c.name, s.shops.c.credit_limit_kobo).where(
            s.shops.c.tenant_id == tenant, s.shops.c.shop_id == shop_id)).first()
    if r is None:
        return None
    b = book.shops.get(shop_id)
    return _row(shop_id, r.name, b.owed_kobo if b else 0, b.overdue_kobo if b else 0,
                b.oldest_days_overdue if b else 0, int(r.credit_limit_kobo),
                b.open_invoices if b else 0)


def _over(row: ShopRow) -> str:
    """", or the clause that flags a shop owing more than its limit."""
    if not row.over_limit:
        return ""
    return f"; that is {row.over_limit_by_naira} over its {row.credit_limit_naira} credit limit"


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
                                     text=f"Send {row.name} a soft reminder about the "
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
                      f"{_late(row)}{_over(row)}.")
    if not rows:
        # No card: an empty card's own line said the same thing twice
        # ("No shop order is waiting ..." under "No shop order is waiting ...").
        return Answer(None, "No shop owes anything in the book.")
    if skill == "collector/chase" and _TOP.search(text) and not _MANY.search(text):
        top = rows[0]
        return Answer(shop_row_card(top, skill, "Highest debtor", as_of),
                      f"{top.name} owes the most: {top.overdue_naira} overdue "
                      f"({top.owed_naira} owed in all, {_late(top)}{_over(top)}), out of "
                      f"{_overdue_line(rows)}.")
    if skill != "collector/chase":
        return Answer(None, "")
    m = re.search(r"\btop\s+(\d+)\b|\b(\d+)\s+(?:shops|stores|debtors|customers)\b", text, re.I)
    want = int(next(g for g in m.groups() if g)) if m else MAX_ITEMS
    want = max(1, min(want, MAX_ITEMS))
    items = [ListItem(item_id=r.shop_id, label=r.name, shop_id=r.shop_id,
                      amount_kobo=r.overdue_kobo, amount_naira=r.overdue_naira,
                      status=(f"overdue, {_late(r)}; {r.owed_naira} owed in all"
                              if r.overdue_kobo else f"not overdue; {r.owed_naira} owed")
                      + (f"; {r.over_limit_by_naira} over limit" if r.over_limit else ""))
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
                    "over_credit": bool(p.get("over_credit")),
                    "over_by_kobo": int(p.get("over_by_kobo") or 0)})
    return out


def orders_answer(engine: Engine, tenant: str, as_of: str) -> Answer:
    orders = open_orders(engine, tenant)
    skill = "order/open-orders"
    if not orders:
        # One line, no card: the empty card's text repeated the sentence (the
        # "duplicate Order reply" of 30 Oct 2026 run 2).
        return Answer(None, "No shop order is waiting for you right now.")
    items = [ListItem(item_id=o["order_id"] or o["approval_id"],
                      label=f"{o['shop_name']}: {o['lines']} line{'s' if o['lines'] != 1 else ''}",
                      qty=o["qty"], unit="units", shop_id=o["shop_id"],
                      status=(f"over credit limit by {naira(o['over_by_kobo'])}, waiting for you"
                              if o["over_credit"] and o["over_by_kobo"] else
                              "over credit limit, waiting for you" if o["over_credit"]
                              else "waiting for you"),
                      amount_kobo=o["total_kobo"], amount_naira=naira(o["total_kobo"]))
             for o in orders[:MAX_ITEMS]]
    actions = [CardAction(action="open_approvals", label="Open approvals",
                          intent=ChipIntent(kind="open", target="approvals"))]
    total = sum(o["total_kobo"] for o in orders)
    n = len(orders)
    over = sum(1 for o in orders if o["over_credit"])
    flag = (f" {over} would push {'shops' if over != 1 else 'a shop'} past "
            f"{'their credit limits' if over != 1 else 'its credit limit'}." if over else "")
    return Answer(Card(kind="list", title="Open orders", skill=skill, items=items, total=n,
                       total_kobo=total, total_naira=naira(total), actions=actions,
                       as_of=as_of),
                  f"{n} order{'s are' if n != 1 else ' is'} waiting for your approval, "
                  f"{naira(total)} in all.{flag}")


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
    out.sort(key=lambda x: (x["days"], x["shop_id"], x["sku"]))
    # Two skus can share a display name ("Okin Sachet Milk 15g" twice for one
    # shop on 30 Oct 2026): one row per shop and item name, the soonest kept.
    seen: set[tuple[str, str]] = set()
    uniq = []
    for x in out:
        k = (x["shop_id"], x["item"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    return uniq


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
            return Answer(None, f"Nothing at {shop} looks likely to run out in the next 3 days.")
        items = [ListItem(item_id=f"{r['shop_id']}:{r['sku']}", label=r["item"],
                          unit=r["unit"], shop_id=r["shop_id"],
                          status="may run out within 3 days") for r in rows[:MAX_ITEMS]]
        return Answer(Card(kind="list", title=f"Running low: {shop}", skill=skill,
                           items=items, total=len(rows), as_of=as_of),
                      f"{len(rows)} item{'s' if len(rows) != 1 else ''} at {shop} may run "
                      "out within 3 days.")
    if not rows:
        return Answer(None, "No shop looks likely to run out in the next 3 days.")
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
                              status=f"overdue: {_n(f['overdue_shops'], 'shop')}"))
    for r in f["top"]:
        # The amount is what is overdue, so the row says overdue (30 Oct 2026
        # run 2: "who owes" beside an overdue figure read as the whole debt).
        items.append(ListItem(item_id=f"owes:{r.shop_id}", label=r.name, shop_id=r.shop_id,
                              amount_kobo=r.overdue_kobo, amount_naira=r.overdue_naira,
                              status=f"overdue, {_late(r)}"
                              + (f"; {r.over_limit_by_naira} over limit" if r.over_limit
                                 else "")))
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
            + (f"; {f['top'][0].name} is the most overdue ({f['top'][0].overdue_naira})"
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



# ------------------------------------------------------------- owner reads

# An owner ask that wants something done, not only read: the helper runs.
_ACTION = re.compile(r"\b(?:chase|remind|reminders?|send|draft|message|text|whatsapp|follow ?up|"
                     r"call|tell|warn|notify|plan|schedule|collect|visit|assign|email|write|"
                     r"should i|is (?:it|this shop|they) safe|risk|recommend|review|advise|"
                     r"why)\b", re.I)
_WHO_OWES = re.compile(r"\bwho (?:owes|is owing|is overdue|has not paid|hasn'?t paid)\b"
                       r"|\b(?:highest|biggest|largest|top|worst)\s+(?:\d+\s+)?"
                       r"(?:debtors?|owers?|balances?)\b"
                       r"|\bowes? (?:me )?the most\b|\bhow much (?:is|am i) owed\b"
                       r"|\b(?:total|all) (?:that is )?overdue\b", re.I)
_SHOP_OWES = re.compile(r"\bhow much (?:does|do|is)\b.{0,60}\b(?:owe|owing)\b"
                        r"|\bwhat (?:does|do)\b.{0,60}\bowe\b|\b(?:balance|owed|owes)\b", re.I)
_LIMIT_READ = re.compile(r"\b(?:credit )?limit\b|\bcredit line\b", re.I)
# "Raise it by ₦100,000", "increase Funke's limit to 2.6m", "cut it by 50k".
_LIMIT_CHANGE = re.compile(
    r"\b(?P<verb>raise|increase|bump|up|lift|top up|add|lower|reduce|cut|drop|decrease|"
    r"set|change|make|move)\b[^.?!]{0,60}?\b(?P<how>by|to)\s+"
    r"(?P<amt>(?:n|₦|ngn)?\s?\d[\d,]*(?:\.\d+)?\s?(?:k|m|million|thousand)?)\b", re.I)
_DOWN = frozenset({"lower", "reduce", "cut", "drop", "decrease"})


def parse_naira(words: str) -> int:
    """``₦100,000``, ``100k``, ``2.6m``, ``N50000`` in kobo (0 if none)."""
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s?(k|m|million|thousand)?\b", words or "", re.I)
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    mult = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000}.get(
        (m.group(2) or "").lower(), 1)
    return int(round(n * mult * 100))


def limit_change(text: str, row: ShopRow) -> LimitChange | None:
    """The limit change the owner asked for in ``text``, worked out by code."""
    m = _LIMIT_CHANGE.search(text or "")
    if not m:
        return None
    amt = parse_naira(m.group("amt"))
    if amt <= 0:
        return None
    verb, how = m.group("verb").lower(), m.group("how").lower()
    cur = row.credit_limit_kobo
    if how == "to":
        new = amt
    elif verb in _DOWN:
        new = max(0, cur - amt)
    else:
        new = cur + amt
    if new == cur:
        return None
    return LimitChange(shop_id=row.shop_id, current_limit_kobo=cur,
                       current_limit_naira=naira(cur), proposed_limit_kobo=new,
                       proposed_limit_naira=naira(new), change_kobo=new - cur,
                       change_naira=naira(abs(new - cur)),
                       would_be_over_limit=row.owed_kobo > new, asked=m.group(0)[:120])


def wants_limit_change(text: str) -> bool:
    return bool(_LIMIT_CHANGE.search(text or ""))


def code_read(engine: Engine, tenant: str, skill: str, text: str) -> bool:
    """Whether code answers this owner message on ``skill`` from the book, no model.

    Collector and Credit reads only (who owes the most, what one shop owes,
    one shop's limit, a limit change the owner asks for), with no other
    action asked for. Anything else (chase, remind, should I give them
    more, is this shop safe) runs the helper as before."""
    if skill not in READ_SKILLS:
        return False
    t = text or ""
    if _ACTION.search(t):
        return False
    if skill == "collector/chase":
        return bool(_WHO_OWES.search(t))
    named = _named_shop(SqlGo(engine), tenant, t)
    if not named:
        return False
    if skill == "credit/credit-review":
        return bool(_LIMIT_READ.search(t) or wants_limit_change(t))
    return bool(_SHOP_OWES.search(t))


def credit_answer(go: SqlGo, tenant: str, text: str, as_of: str) -> Answer:
    """One shop's credit limit, what it owes against it, and any change the
    owner asked for (shown for them to confirm; Mimi never sets a limit)."""
    named = _named_shop(go, tenant, text)
    row = shop_row_for(go, tenant, named) if named else None
    if row is None:
        return Answer(None, "")
    card = shop_row_card(row, "credit/credit-review", row.name, as_of)
    change = limit_change(text, row)
    head = (f"{row.name}'s credit limit is {row.credit_limit_naira}; it owes "
            f"{row.owed_naira}" + (f", {row.over_limit_by_naira} over the limit"
                                    if row.over_limit else "") + ".")
    if change is None:
        if wants_limit_change(text):
            return Answer(card, head + " How much should the new limit be?")
        return Answer(card, head)
    card = card.model_copy(update={
        "limit_change": change,
        "actions": [CardAction(action="set_limit", label="Set credit limit",
                               intent=ChipIntent(kind="open",
                                                 target=f"credit_limit:{row.shop_id}"))]
        + list(card.actions)})
    up = change.change_kobo > 0
    still = (f" It would still owe {naira(row.owed_kobo - change.proposed_limit_kobo)} over "
             "the new limit." if change.would_be_over_limit else "")
    return Answer(card, f"{head} {'Raising' if up else 'Lowering'} it by {change.change_naira} "
                        f"makes it {change.proposed_limit_naira}: confirm with Set credit limit; "
                        f"I don't change limits myself.{still}")


# ------------------------------------------------------------------ proofs

PROOF_TOOLS = ("record_payment_proof", "flag_payment_proof")


def proofs_answer(engine: Engine, go: SqlGo, tenant: str, as_of: str) -> Answer:
    """Payment screenshots Proof checked today (Lagos day), each beside what the
    shop owes in the book. Likely or flagged, never "paid": only the bank feed
    verifies a payment."""
    from datetime import datetime

    from mimi.clock import wall_utc
    from mimi.tz import lagos_day

    today = lagos_day(wall_utc()).isoformat()
    with engine.connect() as conn:
        rows = conn.execute(sa.select(s.intents).where(
            s.intents.c.tenant_id == tenant, s.intents.c.tool.in_(PROOF_TOOLS))
            .order_by(s.intents.c.created_at.desc())).all()
    picked = []
    for r in rows:
        try:
            day = lagos_day(datetime.fromisoformat(r.created_at)).isoformat()
        except ValueError:
            continue
        if day == today:
            picked.append(r)
    skill = "proof/proofs-today"
    if not picked:
        return Answer(None, "No shop has sent a payment proof today.")
    debt = {r.shop_id: r for r in _debtors(go, tenant)}
    names = go.known_shops(tenant)
    by_id = {sid: n for n, sid in names.items()}
    items = []
    for r in picked[:MAX_ITEMS]:
        p = loads(r.payload, {})
        flagged = r.tool == "flag_payment_proof"
        amt = int((p.get("amount_kobo_as_shown") if flagged else p.get("amount_kobo")) or 0)
        d = debt.get(r.shop_id)
        owes = (f"owes {d.owed_naira}, {d.overdue_naira} overdue" if d else "owes nothing")
        status = ("flagged: looks fake, waiting for you" if flagged
                  else "likely; the bank feed has not confirmed it") + f"; {owes}"
        items.append(ListItem(item_id=str(p.get("proof_id") or r.idempotency_key),
                              label=by_id.get(r.shop_id, "a shop"), shop_id=r.shop_id,
                              amount_kobo=amt, amount_naira=naira(amt), status=status))
    n, bad = len(picked), sum(1 for r in picked if r.tool == "flag_payment_proof")
    actions = [CardAction(action="open_approvals", label="Open approvals",
                          intent=ChipIntent(kind="open", target="approvals"))]
    return Answer(Card(kind="list", title="Payment proofs today", skill=skill, items=items,
                       total=n, as_of=as_of, actions=actions),
                  f"{_n(n, 'shop')} sent a payment proof today"
                  + (f"; {bad} looks fake and waits for you" if bad else "")
                  + ". None counts as paid until the bank feed shows it.")


# ----------------------------------------------------------------- reorder


def reorder_answer(engine: Engine, go: SqlGo, tenant: str, text: str, as_of: str) -> Answer:
    """The reorder card: with a shop named, its last delivered order priced at
    today's prices and checked against its credit limit; with none, what to
    restock first (the stock estimator's soonest run-outs, quantity by code).
    Nothing is ordered or sent from here."""
    skill = "order/reorder"
    named = _named_shop(go, tenant, text)
    if not named:
        rows = low_stock(go, tenant)
        if not rows:
            return Answer(None, "Nothing looks likely to run out in the next 3 days, so "
                                "nothing needs restocking first.")
        items = [ListItem(item_id=f"{r['shop_id']}:{r['sku']}",
                          label=f"{r['item']}, {r['shop_name']}", unit=r["unit"],
                          shop_id=r["shop_id"],
                          status=f"may run out in {max(r['days'], 0):.0f} day"
                                 f"{'s' if round(r['days']) != 1 else ''}")
                 for r in rows[:MAX_ITEMS]]
        first = rows[0]
        return Answer(Card(kind="list", title="Restock first", skill=skill, items=items,
                           total=len(rows), as_of=as_of,
                           actions=[CardAction(action="open_shop",
                                               label=f"Open {first['shop_name']}",
                                               intent=ChipIntent(kind="open",
                                                                 target=f"shop:{first['shop_id']}"))]),
                      f"Restock {first['item']} at {first['shop_name']} first: it may run out "
                      f"soonest. {len(rows)} item{'s' if len(rows) != 1 else ''} may run out "
                      "within 3 days.")
    ds = go.deliveries(tenant, named)
    row = shop_row_for(go, tenant, named)
    if row is None:
        return Answer(None, "")
    if not ds or not ds[-1]["lines"]:
        return Answer(None, f"{row.name} has no earlier delivery to repeat.")
    last = ds[-1]
    prices = go.price_list(tenant)
    items, total = [], 0
    for ln in last["lines"]:
        unit_price = prices.get(ln["sku"], (ln["unit_price_kobo"],))[0]
        amt = int(unit_price) * int(ln["quantity"])
        total += amt
        items.append(ListItem(item_id=f"{named}:{ln['sku']}", label=ln["name"],
                              qty=int(ln["quantity"]), unit=ln["unit"], shop_id=named,
                              amount_kobo=amt, amount_naira=naira(amt),
                              status=f"{naira(int(unit_price))} each"))
    available = row.credit_limit_kobo - row.owed_kobo
    over_by = max(0, total - max(available, 0))
    flag = (f" On credit it would put {row.name} {naira(over_by)} over its "
            f"{row.credit_limit_naira} limit." if over_by else
            f" It fits in the {naira(available)} of credit left.")
    card = Card(kind="list", title=f"Reorder: {row.name}", skill=skill, shop_row=row,
                items=items, total=len(items), total_kobo=total, total_naira=naira(total),
                as_of=as_of, actions=[CardAction(action="open_shop", label=f"Open {row.name}",
                                                 intent=ChipIntent(kind="open",
                                                                   target=f"shop:{named}"))])
    return Answer(card, f"{row.name}'s last order ({last['delivered_on']}) at today's prices "
                        f"comes to {naira(total)}.{flag}")


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
        if skill == "order/reorder":
            return reorder_answer(engine, go, tenant, text, as_of)
        if skill == "proof/proofs-today":
            return proofs_answer(engine, go, tenant, as_of)
        if skill == "credit/credit-review":
            ans = credit_answer(go, tenant, text, as_of)
            if ans.card is not None:
                return ans
        if skill == "collector/rep-visit":
            named = _named_shop(go, tenant, text)
            row = shop_row_for(go, tenant, named) if named else None
            return Answer(shop_row_card(row, skill, row.name, as_of), "") if row else \
                Answer(None, "")
        return debtor_answer(go, tenant, skill, text, as_of)
    except Exception:  # a card is extra: the answer still goes out without it
        import logging

        logging.getLogger(__name__).warning("card for %s failed", skill, exc_info=True)
        return Answer(None, "")


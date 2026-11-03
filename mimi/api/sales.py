"""Sales tracking on the API (3 Nov 2026): the sell-through read, the weekly
slow-stock check and its swap, promo or pickup approvals. Code only, no model.

* ``sell_through(engine, tenant)`` backs ``GET /v1/stock/sell-through``: per
  shop and product, sold over the period (counts when there are two, else the
  estimate, with its proof level), days of cover, days to expiry and a status
  (``mimi.stock.selling``). Names and ``₦`` only in display strings.
* ``run_slow_check(engine, tenant)`` backs ``POST /v1/stock/slow-check`` and
  the ``stock.slow_check`` routine: every gated slow or near-expiry line
  (``selling.alerts``), within the caps (``slow.pick_slow``: once per item per
  week, ``SHOP_WEEKLY_CAP`` per shop per week), becomes one ``slow_stock``
  approval with its options worked out and checked in code (``swap.plan``,
  ``swap.verify``). The approval key is fixed per shop, item and week, so a
  second run opens nothing new (idempotent). The owner thread gets one line.
* ``on_approved`` records the owner's chosen option and tells the shop in
  plain words; ``undo`` puts it back and tells the shop the plan changed.
  Mimi never moves stock or money: the rep does the move or pickup, and the
  distributor settles any credit.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi import owed
from mimi.api.agent import Conflict, NotFound
from mimi.clock import wall
from mimi.stock import swap
from mimi.stock.estimator import estimate
from mimi.stock.history import ItemHistory, StockCount, StockIn, bucket_of
from mimi.stock.items import display_name, unit_word
from mimi.stock.selling import STATUS_WORD, SellThrough, alerts
from mimi.stock.selling import sell_through as row_for
from mimi.stock.slow import SlowAssessment, SlowBook, pick_slow
from mimi.stock.trigger import iso_week
from mimi.store import schema as s
from mimi.store.book import approval_id_for
from mimi.store.util import dumps, insert_ignore, loads, now_iso
from mimi.world.model import Sku

SLOW_KIND = "slow_stock"
STATUS_ORDER = {"near_expiry": 0, "slow": 1, "low": 2, "ok": 3}
PROOF_GRADE = {"verified": "verified", "likely": "estimated", "estimated": "unverified"}


@dataclass
class Book:
    """What the sell-through and slow check read from the store."""

    live_day: int
    start: date
    rise_day: int
    distributor: str
    shops: dict[str, Any]
    skus: dict[str, Sku]
    histories: dict[tuple[str, str], ItemHistory]
    counts: dict[tuple[str, str], list[StockCount]]

    def date_of(self, day: int) -> date:
        return self.start + timedelta(days=day)

    def day_of(self, iso: str) -> int:
        return (date.fromisoformat(iso) - self.start).days


def ngn(kobo: int) -> int:
    return int((int(kobo) + 50) // 100)


def load_book(engine: Engine, tenant: str) -> Book:
    with engine.connect() as conn:
        t = conn.execute(sa.select(s.tenants).where(s.tenants.c.tenant_id == tenant)).first()
        if t is None:
            raise NotFound(tenant)
        live = int(t.live_day)
        start = date.fromisoformat(t.live_date) - timedelta(days=live)
        shops = {r.shop_id: r for r in conn.execute(
            sa.select(s.shops).where(s.shops.c.tenant_id == tenant))}
        skus = {r.sku: Sku(r.sku, r.name, r.brand, r.unit, int(r.price_kobo),
                           int(r.risen_price_kobo) if r.risen_price_kobo is not None else None)
                for r in conn.execute(sa.select(s.skus).where(s.skus.c.tenant_id == tenant))}
        rows = conn.execute(
            sa.select(s.deliveries.c.delivery_id, s.deliveries.c.shop_id, s.deliveries.c.day,
                      s.delivery_lines.c.sku, s.delivery_lines.c.name, s.delivery_lines.c.unit,
                      s.delivery_lines.c.quantity, s.delivery_lines.c.expires_on,
                      s.delivery_lines.c.batch)
            .join(s.delivery_lines, sa.and_(
                s.delivery_lines.c.tenant_id == s.deliveries.c.tenant_id,
                s.delivery_lines.c.delivery_id == s.deliveries.c.delivery_id))
            .where(s.deliveries.c.tenant_id == tenant, s.deliveries.c.day < live)).all()
        stock_rows = conn.execute(sa.select(s.stock_counts).where(
            s.stock_counts.c.tenant_id == tenant)).all()
        count_intents = conn.execute(sa.select(s.intents.c.shop_id, s.intents.c.payload).where(
            s.intents.c.tenant_id == tenant, s.intents.c.tool == "record_stock_count",
            s.intents.c.outcome.not_in(("refused", "shadowed")))).all()
    book = Book(live, start, int(t.price_rise_day if t.price_rise_day is not None else 10**6),
                str(t.name).removesuffix(" (demo copy)"), shops, skus, {}, {})
    ins: dict[tuple[str, str], list[StockIn]] = defaultdict(list)
    names: dict[str, tuple[str, str]] = {}
    for r in rows:
        exp = book.day_of(r.expires_on) if r.expires_on else None
        ins[(r.shop_id, r.sku)].append(StockIn(int(r.day), int(r.quantity), r.delivery_id,
                                               expires_day=exp, batch=r.batch or ""))
        names[r.sku] = (r.name, r.unit)
    for key, v in sorted(ins.items()):
        shop = shops.get(key[0])
        book.histories[key] = ItemHistory(
            key[0], key[1], *names[key[1]], tuple(sorted(v, key=lambda x: (x.day, x.delivery_id))),
            visit_days=int(shop.restock_days) if shop is not None else None)
    counts: dict[tuple[str, str], list[StockCount]] = defaultdict(list)
    for c in stock_rows:
        day = book.day_of(c.counted_on)
        if day > live:
            continue
        units = max(0, int(c.quantity))
        counts[(c.shop_id, c.sku)].append(StockCount(
            day, bucket_of(units), units,
            source="connector" if c.source == "connector" else "typed"))
    for ci in count_intents:
        p = loads(ci.payload, {})
        try:
            counts[(ci.shop_id, str(p["sku"]))].append(StockCount(
                int(p["counted_day"]), p["bucket"], int(p["units_assumed"]),
                source="typed" if p.get("exact") else "tap"))
        except (KeyError, TypeError, ValueError):
            continue
    book.counts = {k: sorted(v, key=lambda c: (c.day, c.after_delivery)) for k, v in counts.items()}
    return book


# ------------------------------------------------------------ the read


def _date_text(d: date) -> str:
    return f"{d.day} {d.strftime('%b')}"


def _note(r: SellThrough, book: Book, price: int) -> str:
    """One plain line for the console: names, ₦, 'about' on estimates."""
    unit = r.unit
    if r.status in ("near_expiry", "slow") and r.slow is not None:
        n = r.slow.excess_units
        what = f"About {n} {unit_word(unit, n)}"
        worth = owed.naira(n * price)
        if r.status == "near_expiry" and r.expires_day is not None:
            return (f"{what} may not sell before {_date_text(book.date_of(r.expires_day))} "
                    f"({worth}).")
        return f"{what} more than the shop usually needs ({worth})."
    if r.status == "low":
        return "Likely to run low within 3 days."
    if r.watch:
        return "Selling slowly, maybe. A count from the shop would tell."
    return "Selling as usual."


def row_dict(r: SellThrough, book: Book) -> dict[str, Any]:
    shop = book.shops.get(r.shop_id)
    sku = book.skus.get(r.sku)
    price = sku.price_on(book.live_day, book.rise_day) if sku else 0
    at_risk = r.slow.excess_units if (r.slow is not None and alerts(r.slow)) else 0
    return {
        "shop_id": r.shop_id, "shop_name": shop.name if shop is not None else "",
        "area": shop.area if shop is not None else "",
        "sku": r.sku, "item": display_name(r.item), "unit": unit_word(r.unit),
        "period_days": r.period_days, "sold_units": r.sold_units,
        "sold_over_days": r.sold_over_days, "sold_method": r.sold_method,
        "sold_value_ngn": ngn(r.sold_units * price),
        "proof_level": r.proof_level, "proof_grade": PROOF_GRADE.get(r.proof_level, "unverified"),
        "proof": r.proof, "units_left": r.units_left,
        "days_of_cover": int(r.days_of_cover) if r.days_of_cover is not None else None,
        "days_to_expiry": r.days_to_expiry,
        "expires_on": book.date_of(r.expires_day).isoformat() if r.expires_day is not None else None,
        "status": r.status, "status_word": STATUS_WORD[r.status], "watch": r.watch,
        "confidence": r.confidence,
        "at_risk_units": at_risk, "at_risk_ngn": ngn(at_risk * price),
        "last_count_on": (book.date_of(r.last_count_day).isoformat()
                          if r.last_count_day is not None else None),
        "note": _note(r, book, price),
    }


def sell_through(engine: Engine, tenant: str, *, days: int = 28, shop_id: str = "",
                 status: str = "") -> dict[str, Any]:
    book = load_book(engine, tenant)
    if shop_id and shop_id not in book.shops:
        raise NotFound(shop_id)
    rows = []
    for (sid, _sku), h in book.histories.items():
        if shop_id and sid != shop_id:
            continue
        r = row_for(h, book.counts.get((sid, h.sku), []), book.live_day, days)
        if r is not None:
            rows.append(row_dict(r, book))
    totals = {k: sum(1 for r in rows if r["status"] == k) for k in STATUS_ORDER}
    at_risk = sum(r["at_risk_ngn"] for r in rows)
    if status:
        rows = [r for r in rows if r["status"] == status]
    rows.sort(key=lambda r: (STATUS_ORDER[r["status"]], -r["at_risk_ngn"], r["shop_name"],
                             r["item"]))
    return {"as_of": book.date_of(book.live_day).isoformat(), "period_days": days,
            "rows": rows, "total": len(rows),
            "counts": totals, "at_risk_ngn": at_risk,
            "at_risk_text": f"{owed.naira(at_risk * 100)} of stock may not sell in time"}


# ------------------------------------------------------------ the weekly check


def _assessments(book: Book) -> list[tuple[SlowAssessment, SellThrough]]:
    out = []
    for key, h in book.histories.items():
        r = row_for(h, book.counts.get(key, []), book.live_day)
        if r is not None and r.slow is not None and alerts(r.slow):
            out.append((r.slow, r))
    return out


def _peers(book: Book, sku: str) -> list[swap.Peer]:
    out = []
    for (sid, k), h in book.histories.items():
        if k != sku or sid not in book.shops:
            continue
        e = estimate(h, book.counts.get((sid, k), []), book.live_day)
        if e is None:
            continue
        shop = book.shops[sid]
        out.append(swap.Peer(sid, shop.name, shop.area, shop.rep_id, e))
    return out


def _book_this_week(conn: sa.Connection, tenant: str, week: str) -> SlowBook:
    book = SlowBook()
    for a in conn.execute(sa.select(s.approvals.c.payload).where(
            s.approvals.c.tenant_id == tenant, s.approvals.c.tool == SLOW_KIND)):
        p = loads(a.payload, {})
        if p.get("week") == week:
            book.record(str(p.get("shop_id")), str(p.get("sku")), week)
    return book


def _payload(d: Any, a: SlowAssessment, book: Book, week: str) -> dict[str, Any]:
    shop = book.shops[a.shop_id]
    options = []
    for o in d.options:
        options.append({
            "action": o.action, "quantity": o.quantity, "unit": unit_word(a.unit, o.quantity),
            "unit_price_kobo": o.unit_price_kobo, "value_kobo": o.value_kobo,
            "value_ngn": ngn(o.value_kobo), "to_shop_id": o.to_shop_id,
            "to_shop_name": o.to_shop_name, "why": o.why,
            "text": swap.option_text(o, owed.naira(o.value_kobo)),
            "shop_message": swap.shop_text(o, a.item, a.unit, book.distributor, d.expires_on)})
    return {
        "kind": SLOW_KIND, "draft_id": d.draft_id, "week": week, "shop_id": a.shop_id,
        "shop_name": shop.name, "sku": a.sku, "item": display_name(a.item),
        "slow_kind": a.kind, "expires_on": d.expires_on, "confidence": a.confidence,
        "expiry_band": a.expiry_band, "cover_band": a.cover_band,
        "reason": swap.reason_text(a, d.expires_on), "options": options,
        "recommended": d.recommended, "chosen": None, "policy_version": d.policy_version,
        "moves_stock": False, "moves_money": False, "result": None}


def preview(p: dict[str, Any]) -> str:
    opts = "; ".join(o["text"] for o in p["options"])
    return f"{p['shop_name']}: {p['reason']} Options: {opts}."


def run_slow_check(engine: Engine, tenant: str, *, turn_id: str = "",
                   now: float | None = None) -> dict[str, Any]:
    """Open this week's slow-stock approvals. Returns what was opened."""
    from mimi.api.demo_wiring import _owner_thread, _say

    now = wall() if now is None else now
    book = load_book(engine, tenant)
    week = iso_week(book.date_of(book.live_day))
    found = _assessments(book)
    prices = {k: v.price_on(book.live_day, book.rise_day) for k, v in book.skus.items()}
    opened: list[str] = []
    skipped: list[dict[str, str]] = []
    with engine.begin() as conn:
        caps = _book_this_week(conn, tenant, week)
        picks = pick_slow([a for a, _ in found], caps, week, prices)
        owner = _owner_thread(conn, tenant)
        for a in picks:
            shop = book.shops.get(a.shop_id)
            sku = book.skus.get(a.sku)
            if shop is None or sku is None:
                continue
            exp = (book.date_of(a.as_of_day + a.days_to_expiry).isoformat()
                   if a.kind == "near_expiry" and a.days_to_expiry is not None else "")
            peers = _peers(book, a.sku)
            d = swap.plan(a, area=shop.area, rep_id=shop.rep_id, peers=peers, sku=sku,
                          day=book.live_day, rise_day=book.rise_day, tenant_id=tenant,
                          week=week, as_of=book.date_of(book.live_day).isoformat(),
                          expires_on=exp)
            if d is None:
                skipped.append({"shop_id": a.shop_id, "sku": a.sku, "why": "no_option"})
                continue
            nearby = {p.shop_id for p in peers
                      if p.area == shop.area or p.rep_id == shop.rep_id}
            problems = swap.verify(d, a, sku=sku, day=book.live_day, rise_day=book.rise_day,
                                   nearby=nearby)
            if problems:
                skipped.append({"shop_id": a.shop_id, "sku": a.sku,
                                "why": "verifier: " + "; ".join(problems)})
                continue
            key = f"{SLOW_KIND}:{a.shop_id}:{a.sku}:{week}"
            apr = approval_id_for(tenant, key)
            p = _payload(d, a, book, week)
            fresh = insert_ignore(conn, s.approvals, {
                "approval_id": apr, "tenant_id": tenant, "idempotency_key": key,
                "turn_id": turn_id, "thread_id": owner, "helper": "", "skill": "stock/slow-check",
                "shop_id": a.shop_id, "tool": SLOW_KIND, "intent_kind": d.kind,
                "preview": preview(p), "payload": dumps(p), "status": "pending",
                "created_at": now_iso(now), "created_ts": now, "handling": ""})
            if not fresh:
                continue
            opened.append(apr)
        if opened:
            n = len(opened)
            _say(conn, tenant, owner, key=f"slow:{week}:{len(opened)}:{opened[0]}",
                 channel="owner_console", shop_id="", turn_id=turn_id, kind="slow_stock_check",
                 now=now,
                 text=(f"Slow stock check: {n} {'item needs' if n == 1 else 'items need'} a "
                       "decision before it expires or sits too long. They are in your "
                       "approvals with options to move, push or collect. Nothing moves until "
                       "you approve."))
    return {"week": week, "opened": opened, "skipped": skipped, "checked": len(found)}


# ------------------------------------------------------------ approve and undo


def _approval(conn: sa.Connection, tenant: str, approval_id: str) -> Any:
    a = conn.execute(sa.select(s.approvals).where(
        s.approvals.c.tenant_id == tenant, s.approvals.c.approval_id == approval_id)).first()
    if a is None:
        raise NotFound(approval_id)
    return a


def is_slow(engine: Engine, tenant: str, approval_id: str) -> bool:
    with engine.connect() as conn:
        a = conn.execute(sa.select(s.approvals.c.tool).where(
            s.approvals.c.tenant_id == tenant, s.approvals.c.approval_id == approval_id)).first()
    return a is not None and a.tool == SLOW_KIND


def check_option(engine: Engine, tenant: str, approval_id: str, option: str | None) -> str:
    """The option an approval would carry out: the one asked for, else the
    recommended one. 409 when it is not one of the draft's options."""
    with engine.connect() as conn:
        p = loads(_approval(conn, tenant, approval_id).payload, {})
    want = option or str(p.get("recommended", ""))
    if want not in {o["action"] for o in p.get("options", [])}:
        raise Conflict(f"{want or 'that'} is not one of this draft's options")
    return want


def _set(conn: sa.Connection, tenant: str, approval_id: str, p: dict[str, Any]) -> None:
    conn.execute(s.approvals.update().where(
        s.approvals.c.tenant_id == tenant, s.approvals.c.approval_id == approval_id)
        .values(payload=dumps(p)))


def on_approved(engine: Engine, tenant: str, approval_id: str, option: str) -> None:
    """Record the chosen option and tell the shop (once). Mimi moves nothing."""
    from mimi.api.demo_wiring import _ensure_thread, _owner_thread, _say

    now = wall()
    with engine.begin() as conn:
        a = _approval(conn, tenant, approval_id)
        p = loads(a.payload, {})
        if p.get("chosen"):
            return
        o = next(x for x in p["options"] if x["action"] == option)
        shop_thread = f"{tenant}:shop:{a.shop_id}"
        _ensure_thread(conn, tenant, shop_thread, "shop", a.shop_id, now)
        _say(conn, tenant, shop_thread, key=f"slow:{approval_id}:shop", channel="shop_whatsapp",
             shop_id=a.shop_id, kind="slow_stock_shop", now=now, text=o["shop_message"])
        who = {"move": f"Your rep moves {o['quantity']} {o['unit']} to {o['to_shop_name']}",
               "pickup": f"Your rep collects {o['quantity']} {o['unit']}",
               "promo": "No stock moves; the shop has been asked to push it"}[option]
        _say(conn, tenant, _owner_thread(conn, tenant), key=f"slow:{approval_id}:owner",
             channel="owner_console", shop_id="", kind="slow_stock_approved", now=now,
             text=(f"Approved for {p['shop_name']}, {p['item']}: {o['text']}. {who}. "
                   f"{p['shop_name']} has been told. You can undo it."))
        p["chosen"] = option
        p["result"] = {"option": option, "shop_told": True, "undo_available": True,
                       "decided_at": now_iso(now)}
        _set(conn, tenant, approval_id, p)
    if option == "pickup":
        # A pickup opens a returns draft (the ledger), so the collected goods
        # have somewhere to land. Mimi moves nothing; the rep collects.
        from mimi.api import returns as returns_ledger

        with engine.begin() as conn:
            returns_ledger.draft_from_pickup(conn, tenant, approval_id, now)


def undo(engine: Engine, tenant: str, approval_id: str) -> None:
    """Put an approved slow-stock plan back: the shop is told it changed."""
    from mimi.api.demo_wiring import _owner_thread, _say

    now = wall()
    with engine.begin() as conn:
        a = _approval(conn, tenant, approval_id)
        p = loads(a.payload, {})
        res = p.get("result") or {}
        if a.status != "approved" or not res.get("undo_available"):
            raise Conflict("only an approved slow-stock plan can be undone, once")
        _say(conn, tenant, f"{tenant}:shop:{a.shop_id}", key=f"slow:{approval_id}:shop-undo",
             channel="shop_whatsapp", shop_id=a.shop_id, kind="slow_stock_shop_undo", now=now,
             text=(f"Hello. Please ignore the earlier message about {p['item']}. "
                   f"{book_name(conn, tenant)} will be in touch if anything changes."))
        _say(conn, tenant, _owner_thread(conn, tenant), key=f"slow:{approval_id}:undo",
             channel="owner_console", shop_id="", kind="slow_stock_undone", now=now,
             text=f"Undone: the plan for {p['shop_name']}'s {p['item']} is off. The shop has been told.")
        p["result"] = {**res, "undo_available": False, "undone_at": now_iso(now)}
        _set(conn, tenant, approval_id, p)


def book_name(conn: sa.Connection, tenant: str) -> str:
    name = conn.execute(sa.select(s.tenants.c.name).where(s.tenants.c.tenant_id == tenant)).scalar()
    return str(name or "Your distributor").removesuffix(" (demo copy)")

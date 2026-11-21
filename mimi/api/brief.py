"""The daily owner brief: one message, five sections, eight items at most.

The first thing Mimi delivers that is worth paying for. Once a day, before the
gate meeting, one message that attacks the four problems a distributor owner
names: he cannot see his trade, cannot see who owes what and how old it is, has
no daily pressure on collections, and treats returns as an afterthought.

The shape, in his order of worry:

1. **What changed** since the last brief: the movement in money at risk, what
   came in, how many returns closed. One line, or an honest note that this is
   the first brief and there is nothing to compare yet.
2. **Money at risk**: the shops that owe, with the shop that has gone quiet
   while owing first, because that is the debt you lose, then oldest debt first
   and amount second; the account over its limit and still taking deliveries is
   called out in words.
3. **Collections today**: the biggest exposure by amount times age, each with an
   expected amount and a named owner. Where the rep is holding cash he did not
   bank, the item escalates.
4. **Returns**: what is still open, anything past two days escalated in words,
   and a product returned by three or more shops flagged as the product's
   problem rather than three shops' bad luck.
5. **Coverage**: the shops past their own restock rhythm by cycles missed, with
   the rows the book cannot judge grouped apart and never scored.

The rules this module keeps:

* **Every figure is computed here from the book.** No number is typed into a
  template and no model is called anywhere in this path. A section with nothing
  to say says so in one line, because a missing section reads as an all-clear.
* **Every item names an owner and a next action**, and the whole brief holds at
  most ``ITEM_CAP`` items; the rest are counted, never listed (an owner handed
  forty rows reads none of them).
* **The same book on the same day is the same message.** Every list has an
  explicit sort key and nothing reads the wall clock: the day is the book's own.
* **Nothing here writes to the book.** The one row this module writes is the
  brief's own numbers (``brief_snapshots``), so tomorrow can say what changed.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi import owed
from mimi.credit.exposure import bucket_of
from mimi.identity.areas import area_key
from mimi.store import schema as s
from mimi.store.attention import AttentionRow, attention
from mimi.store.returns import AgedReturn, aged_returns
from mimi.store.util import insert_ignore, now_iso

#: How many items the whole brief may list. Rule 10 of the instinct model: never
#: hand the owner forty rows.
ITEM_CAP = 8
#: What each section may spend of that budget, in the owner's order of worry.
SECTION_ITEMS: dict[str, int] = {
    "money": 3, "collections": 3, "returns": 1, "coverage": 1}
#: A return open longer than this is a credit note the shop will dispute (rule 6).
ESCALATE_AFTER_DAYS = 2
#: One beat, in days: the window in which a product returned by three shops is
#: one problem rather than three coincidences (rule 7). A default, like every
#: threshold here; the first distributor moves it.
BEAT_DAYS = 14

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

#: A return's stage in the owner's words: the stored statuses are the ledger's
#: and a person reading the brief should not have to translate them.
RETURN_WORDS: dict[str, str] = {
    "drafted": "waiting on the pickup", "approved": "approved, waiting on the rep",
    "collected": "with the rep", "inspected": "counted at the depot",
    "settled": "credited", "cancelled": "cancelled",
}

#: The ageing bands, largest first: (bucket key, words a person reads).
BUCKET_WORDS = (("days_over_60", "past 60 days"), ("days_31_60", "at 31 to 60 days"),
                ("days_1_30", "at 1 to 30 days"))


@dataclass(frozen=True)
class Item:
    """One thing to do this morning: whose it is and what it is."""

    section: str
    shop_id: str
    text: str
    #: For a collection item: the amount targeted, and the rep whose shop it is.
    expected_kobo: int = 0
    rep_id: str = ""


@dataclass
class Brief:
    """The message, the numbers behind it, and what tomorrow compares against."""

    day: date
    book_day: date
    text: str = ""
    line: str = ""
    items: list[Item] = field(default_factory=list)
    items_total: int = 0
    shop_ids: frozenset[str] = frozenset()
    snapshot: dict[str, int] = field(default_factory=dict)
    #: The morning's collection targets, for the end-of-day variance.
    targets: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Shop:
    shop_id: str
    name: str
    area: str
    rep_id: str
    rep_name: str
    limit_kobo: int
    restock_days: int
    outstanding_kobo: int = 0
    overdue_kobo: int = 0
    oldest_days_overdue: int = 0
    latest_delivery_on: str = ""
    cycles_missed: int | None = None
    over_since_on: str = ""
    median_payment_kobo: int | None = None
    unbanked_kobo: int = 0
    quiet: bool = False
    supervisor: str = ""

    @property
    def over_limit_kobo(self) -> int:
        return max(0, self.outstanding_kobo - self.limit_kobo)

    @property
    def over_limit_and_ordering(self) -> bool:
        return bool(self.over_since_on and self.latest_delivery_on
                    and self.latest_delivery_on > self.over_since_on)


@dataclass
class _Book:
    """One read of the book for one day: what every section counts from."""

    day: date
    book_day: date
    shops: dict[str, _Shop]
    invoices: list[tuple[str, int, int]]  # (shop id, days overdue, kobo)
    returns: list[AgedReturn]
    return_lines: list[tuple[str, str, str, str, int, int]]  # return, shop, sku, item, qty, kobo
    closed_returns: int
    due: list[AttentionRow]
    previous: dict[str, Any] | None
    collected_kobo: int


# ------------------------------------------------------------------ the build


def build(engine: Engine, tenant_id: str, day: date | None = None) -> Brief:
    """The whole brief for ``tenant_id``, counted from the book as of ``day``."""
    book = _read(engine, tenant_id, day)
    b = Brief(day=book.day, book_day=book.book_day)
    debtors = [one for one in book.shops.values() if one.outstanding_kobo > 0]
    b.snapshot = {
        "at_risk_kobo": sum(one.outstanding_kobo for one in book.shops.values()),
        "overdue_kobo": sum(one.overdue_kobo for one in book.shops.values()),
        "collected_kobo": book.collected_kobo,
        "returns_open": len(book.returns),
        "returns_value_kobo": sum(one.value_kobo for one in book.returns),
        "returns_closed": book.closed_returns,
        "shops_missed": sum(1 for row in book.due if row.kind == "overdue"),
    }
    sections = {
        "money": _money_items(book),
        "collections": _collection_items(book),
        "returns": _return_items(book),
        "coverage": _coverage_items(book.due, book.shops),
    }
    b.items, b.items_total = _allocate(sections)
    b.targets = [{"shop_id": item.shop_id, "rep_id": item.rep_id,
                  "expected_kobo": item.expected_kobo}
                 for item in b.items if item.section == "collections"]
    b.shop_ids = frozenset(item.shop_id for item in b.items if item.shop_id)
    b.text = _render(b, book, sections)
    b.line = (f"{owed.naira(b.snapshot['at_risk_kobo'])} at risk, "
              f"{owed.naira(b.snapshot['overdue_kobo'])} of it overdue; "
              f"{len([one for one in debtors if one.overdue_kobo])} to collect; "
              f"{len(book.returns)} return{'s' if len(book.returns) != 1 else ''} open.")
    return b


def _read(engine: Engine, tenant_id: str, day: date | None) -> _Book:
    with engine.connect() as conn:
        live_date = conn.execute(sa.select(s.tenants.c.live_date).where(
            s.tenants.c.tenant_id == tenant_id)).scalar()
        if live_date is None:
            raise LookupError(f"no tenant {tenant_id!r} in this book")
        book_day = date.fromisoformat(str(live_date))
        counted_on = day if day is not None else book_day
        shops = _shops(conn, tenant_id)
        invoices = _invoices(conn, tenant_id, counted_on, shops)
        _deliveries_and_payments(conn, tenant_id, shops)
        _supervisors(conn, tenant_id, shops)
        closed = int(conn.execute(
            sa.select(sa.func.count()).select_from(s.returns).where(
                s.returns.c.tenant_id == tenant_id,
                s.returns.c.status.in_(("credited", "settled")))).scalar() or 0)
        previous_row = conn.execute(
            sa.select(s.brief_snapshots).where(
                s.brief_snapshots.c.tenant_id == tenant_id,
                s.brief_snapshots.c.day < counted_on.isoformat())
            .order_by(s.brief_snapshots.c.day.desc()).limit(1)).first()
        previous = dict(previous_row._mapping) if previous_row is not None else None
        collected = _collected(conn, tenant_id, previous, book_day)
        return_lines = _return_lines(conn, tenant_id, counted_on)
    # The attention read is the one that says how far past its own rhythm a shop
    # is; a shop past it and owing money is the quiet debt rule 3 names.
    due = attention(engine, tenant_id, counted_on)
    for row in due:
        one = shops.get(row.shop_id)
        if one is None:
            continue
        one.cycles_missed = row.cycles_missed
        one.quiet = row.kind == "overdue" and row.outstanding_kobo > 0
    return _Book(day=counted_on, book_day=book_day, shops=shops, invoices=invoices,
                 returns=aged_returns(engine, tenant_id, counted_on),
                 return_lines=return_lines, closed_returns=closed, due=due,
                 previous=previous, collected_kobo=collected)


def _shops(conn: sa.Connection, tenant_id: str) -> dict[str, _Shop]:
    out: dict[str, _Shop] = {}
    rows = conn.execute(
        sa.select(s.shops.c.shop_id, s.shops.c.name, s.shops.c.area, s.shops.c.rep_id,
                  s.shops.c.credit_limit_kobo, s.shops.c.restock_days,
                  s.reps.c.name.label("rep_name"))
        .outerjoin(s.reps, sa.and_(s.reps.c.tenant_id == s.shops.c.tenant_id,
                                   s.reps.c.rep_id == s.shops.c.rep_id))
        .where(s.shops.c.tenant_id == tenant_id)
        .order_by(s.shops.c.shop_id)).all()
    for r in rows:
        out[str(r.shop_id)] = _Shop(
            shop_id=str(r.shop_id), name=str(r.name), area=str(r.area or ""),
            rep_id=str(r.rep_id or ""), rep_name=str(r.rep_name or ""),
            limit_kobo=int(r.credit_limit_kobo or 0),
            restock_days=int(r.restock_days or 0))
    return out


def _invoices(conn: sa.Connection, tenant_id: str, day: date,
              shops: dict[str, _Shop]) -> list[tuple[str, int, int]]:
    """Each shop's open money, and every open invoice's age for the bands."""
    out: list[tuple[str, int, int]] = []
    for r in conn.execute(
            sa.select(s.receivables.c.shop_id, s.receivables.c.outstanding_kobo,
                      s.receivables.c.due_on).where(
                s.receivables.c.tenant_id == tenant_id,
                s.receivables.c.outstanding_kobo > 0)).all():
        one = shops.get(str(r.shop_id))
        if one is None:
            continue
        kobo = int(r.outstanding_kobo)
        days = max(0, (day - date.fromisoformat(str(r.due_on))).days)
        one.outstanding_kobo += kobo
        if days > 0:
            one.overdue_kobo += kobo
            one.oldest_days_overdue = max(one.oldest_days_overdue, days)
        out.append((one.shop_id, days, kobo))
    return out


def _deliveries_and_payments(conn: sa.Connection, tenant_id: str,
                             shops: dict[str, _Shop]) -> None:
    """The last delivery to each shop, the day it passed its limit, and any cash
    a rep is holding.

    The crossing is computed in code: the shop's deliveries and payments walked
    in day order, payments on a day first, and the first day the running balance
    ends above the credit limit. A delivery on a later day is the danger the
    second rule names: overspending is not the risk, overspending with fresh
    deliveries on the van is.
    """
    deliveries: dict[str, list[tuple[str, int]]] = {}
    for r in conn.execute(sa.select(
            s.deliveries.c.shop_id, s.deliveries.c.delivered_on,
            s.deliveries.c.total_kobo).where(
                s.deliveries.c.tenant_id == tenant_id)).all():
        deliveries.setdefault(str(r.shop_id), []).append(
            (str(r.delivered_on), int(r.total_kobo)))
    banked = {str(r.payment_id) for r in conn.execute(sa.select(s.bank_feed.c.payment_id)
                                                     .where(s.bank_feed.c.tenant_id
                                                            == tenant_id))}
    payments: dict[str, list[tuple[str, int, str, str]]] = {}
    for pay in conn.execute(sa.select(
            s.payments.c.shop_id, s.payments.c.paid_on, s.payments.c.amount_kobo,
            s.payments.c.method, s.payments.c.payment_id).where(
                s.payments.c.tenant_id == tenant_id)).all():
        if int(pay.amount_kobo) > 0:
            payments.setdefault(str(pay.shop_id), []).append(
                (str(pay.paid_on), int(pay.amount_kobo), str(pay.method),
                 str(pay.payment_id)))
    for shop_id, one in shops.items():
        ds = sorted(deliveries.get(shop_id, []))
        ps = sorted(payments.get(shop_id, []))
        one.latest_delivery_on = ds[-1][0] if ds else ""
        one.over_since_on = _over_since(ds, ps, one.limit_kobo)
        amounts = sorted(amount for _day, amount, _method, _pid in ps)
        # The middle of what the shop actually pays: a collection target from the
        # book, not a round number someone chose.
        one.median_payment_kobo = amounts[len(amounts) // 2] if amounts else None
        # Cash the rep took and nobody banked: the newest cash payment with no
        # bank credit behind it. One party's word is not money in the account.
        cash = [(d, amount) for d, amount, method, pid in ps
                if method == "cash_to_rep" and pid not in banked]
        one.unbanked_kobo = cash[-1][1] if cash else 0


def _over_since(deliveries: Sequence[tuple[str, int]],
                payments: Sequence[tuple[str, int, str, str]],
                limit_kobo: int) -> str:
    """The first day a delivery left the shop's balance above its credit limit.

    The day is a date the book carries, and the order is that date: payments on
    a day are applied before deliveries, so a shop that paid on the day it took
    goods is not called over its limit by a payment that had already landed.
    """
    movements = [(0, paid_on, amount) for paid_on, amount, _method, _pid in payments]
    movements += [(1, delivered_on, total) for delivered_on, total in deliveries]
    balance = 0
    for payment_first, day, amount in sorted(movements):
        if payment_first == 0:
            balance -= amount
        else:
            balance += amount
            if balance > limit_kobo:
                return day
    return ""


def _supervisors(conn: sa.Connection, tenant_id: str,
                 shops: dict[str, _Shop]) -> None:
    """Label each shop with the supervisor who covers its area, where there is one.

    An imported book carries no supervisors, so usually there is none and an
    escalation names the owner of the business himself.
    """
    rows = conn.execute(
        sa.select(s.users.c.name, s.user_territories.c.area)
        .join(s.user_territories, sa.and_(
            s.user_territories.c.tenant_id == s.users.c.tenant_id,
            s.user_territories.c.user_id == s.users.c.user_id))
        .where(s.users.c.tenant_id == tenant_id, s.users.c.role == "supervisor",
               s.users.c.status != "disabled")
        .order_by(s.user_territories.c.area, s.users.c.user_id)).all()
    by_area: dict[str, str] = {}
    for r in rows:
        by_area.setdefault(area_key(str(r.area)), str(r.name))
    for one in shops.values():
        one.supervisor = by_area.get(area_key(one.area), "")


def _collected(conn: sa.Connection, tenant_id: str, previous: dict[str, Any] | None,
               book_day: date) -> int:
    """What came in since the last brief: payments after the day it counted from.

    A first brief has nothing to compare, so this is zero and the message says so
    rather than inventing a window.
    """
    if previous is None:
        return 0
    since = str(previous.get("book_day") or previous.get("day") or "")
    if not since:
        return 0
    total = conn.execute(
        sa.select(sa.func.coalesce(sa.func.sum(s.payments.c.amount_kobo), 0)).where(
            s.payments.c.tenant_id == tenant_id,
            s.payments.c.fake_alert.is_(False),
            s.payments.c.paid_on > since,
            s.payments.c.paid_on <= book_day.isoformat())).scalar()
    return int(total or 0)


def _return_lines(conn: sa.Connection, tenant_id: str,
                  day: date) -> list[tuple[str, str, str, str, int, int]]:
    """(return, shop, sku, item, quantity, kobo) for returns raised inside one beat."""
    since = day.toordinal() - BEAT_DAYS
    rows = conn.execute(
        sa.select(s.returns.c.return_id, s.returns.c.shop_id, s.return_lines.c.sku,
                  s.return_lines.c.item, s.return_lines.c.quantity_drafted,
                  s.return_lines.c.value_drafted_kobo)
        .join(s.return_lines, sa.and_(
            s.return_lines.c.tenant_id == s.returns.c.tenant_id,
            s.return_lines.c.return_id == s.returns.c.return_id))
        .where(s.returns.c.tenant_id == tenant_id,
               s.returns.c.raised_day >= since,
               s.returns.c.raised_day <= day.toordinal())
        .order_by(s.returns.c.return_id, s.return_lines.c.line_no)).all()
    return [(str(r.return_id), str(r.shop_id), str(r.sku), str(r.item or r.sku),
             int(r.quantity_drafted or 0), int(r.value_drafted_kobo or 0)) for r in rows]


# ---------------------------------------------------------------- the sections


def _money_items(book: _Book) -> list[Item]:
    """The debts worth a morning: the quiet ones first, then oldest first.

    Two judgements decide the order and neither is about size: a shop that has
    stopped ordering while owing money is the debt you lose (rule 3), and a small
    debt at seventy days is worse than a large one at ten (rule 1).
    """
    debtors = [one for one in book.shops.values() if one.outstanding_kobo > 0]
    quiet = sorted((one for one in debtors if one.quiet),
                   key=lambda one: (-one.oldest_days_overdue, -one.outstanding_kobo,
                                    one.shop_id))
    ranked = sorted((one for one in debtors if not one.quiet),
                    key=lambda one: (-one.oldest_days_overdue, -one.outstanding_kobo,
                                     one.shop_id))
    return [_money_item(one) for one in quiet + ranked]


def _money_item(one: _Shop) -> Item:
    parts = [f"{one.name}{_area(one)}: {owed.naira(one.outstanding_kobo)} owed"]
    if one.oldest_days_overdue:
        days = one.oldest_days_overdue
        parts.append(f", oldest part {days} day{'s' if days != 1 else ''} overdue")
    else:
        parts.append(", nothing overdue yet")
    if one.quiet:
        # Only the fact that it has gone quiet: how far past its own rhythm it is
        # belongs to the coverage line, so the same shop is not told twice.
        parts.append(", and it has stopped ordering")
    if one.over_limit_and_ordering:
        parts.append(f", over the {owed.naira(one.limit_kobo)} limit and still taking "
                     f"deliveries on {_day_words(one.latest_delivery_on)}")
    elif one.over_limit_kobo and one.limit_kobo:
        parts.append(f", over its {owed.naira(one.limit_kobo)} limit")
    if one.quiet:
        # The facts already say it stopped ordering, so the action adds what the
        # rep should find out rather than repeating the warning.
        action = "call today and find out why"
    elif one.over_limit_and_ordering:
        action = "stop the van until it pays"
    else:
        action = "collect or agree a date"
    if one.rep_name:
        tail = f". Rep {one.rep_name}: {action}."
    else:
        tail = f". You: give it to a rep and {action.replace('collect', 'collect it')}."
    return Item("money", one.shop_id, "".join(parts) + tail)


def _collection_items(book: _Book) -> list[Item]:
    """Today's collection target: amount times age, with an expected amount.

    The expected amount is the smaller of what the shop owes and what it usually
    pays per collection (the middle of its own past payments), so the target is
    evidence from the book rather than a round number. Where the rep is holding
    last time's cash and did not bank it, the item goes to the supervisor of the
    area, or to the owner where the book has none.
    """
    candidates = sorted((one for one in book.shops.values() if one.overdue_kobo > 0),
                        key=lambda one: (-(one.overdue_kobo * one.oldest_days_overdue),
                                         -one.overdue_kobo, one.shop_id))
    out: list[Item] = []
    for one in candidates:
        expected = one.outstanding_kobo
        if one.median_payment_kobo:
            expected = min(expected, one.median_payment_kobo)
        target = (f"all {owed.naira(expected)} owed" if expected >= one.outstanding_kobo
                  else f"{owed.naira(expected)} of {owed.naira(one.outstanding_kobo)} owed")
        base = f"{one.name}{_area(one)}: collect {target}, oldest part {one.oldest_days_overdue} days"
        if one.unbanked_kobo:
            holder = one.rep_name or "your rep"
            boss = f"{one.supervisor} (supervisor)" if one.supervisor else "You"
            text = (f"{base}. {boss}: bank the {owed.naira(one.unbanked_kobo)} {holder} is "
                    "holding, then collect it today.")
            out.append(Item("collections", one.shop_id, text, expected, one.rep_id))
            continue
        if one.rep_name:
            text = f"{base}. Rep {one.rep_name}: collect it today."
        else:
            text = f"{base}. You: give it to a rep and collect it today."
        out.append(Item("collections", one.shop_id, text, expected, one.rep_id))
    return out


def _return_items(book: _Book) -> list[Item]:
    """Open returns aged, escalated past two days, and a product coming back twice."""
    out: list[Item] = []
    repeat = _repeat_sku(book)
    if repeat is not None:
        item, shops_count, kobo = repeat
        out.append(Item("returns", "", (
            f"Product problem: {item} came back from {shops_count} shops in the last "
            f"{BEAT_DAYS} days, {owed.naira(kobo)} of it. You: check it with the supplier "
            "before the next order.")))
    for one in sorted(book.returns, key=lambda r: (-r.days_open, -r.value_kobo, r.return_id)):
        where = one.shop_name or one.shop_id
        days = f"{one.days_open} day{'s' if one.days_open != 1 else ''} open"
        reason = f", {one.reason}" if one.reason else ""
        what = (f"{where}: {owed.naira(one.value_kobo)} returned, "
                f"{RETURN_WORDS.get(one.status, one.status)}{reason}, {days}")
        if one.days_open > ESCALATE_AFTER_DAYS:
            what += ". Escalated: the shop will dispute a credit note this old"
        if one.rep_name:
            text = f"{what}. Rep {one.rep_name}: pick it up today."
        else:
            text = f"{what}. You: send a rep to pick it up today."
        out.append(Item("returns", one.shop_id, text))
    return out


def _repeat_sku(book: _Book) -> tuple[str, int, int] | None:
    """The SKU three or more shops sent back inside one beat, if there is one."""
    open_ids = {one.return_id for one in book.returns}
    shops_by_sku: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    totals: dict[str, int] = {}
    for return_id, shop_id, sku, item, _quantity, kobo in book.return_lines:
        if return_id not in open_ids:
            continue
        shops_by_sku.setdefault(sku, set()).add(shop_id)
        names[sku] = item
        totals[sku] = totals.get(sku, 0) + kobo
    hits = sorted(((sku, len(shops)) for sku, shops in shops_by_sku.items() if len(shops) >= 3),
                  key=lambda pair: (-pair[1], pair[0]))
    if not hits:
        return None
    sku, count = hits[0]
    return names[sku], count, totals[sku]


def _coverage_items(due: Sequence[AttentionRow], shops: dict[str, _Shop]) -> list[Item]:
    """The shops past their own rhythm, in ``attention``'s order.

    A row the book cannot judge (no delivery on record, no restock interval) is
    never scored here: it is named apart by ``_render`` and counted, so a hole
    never reads as calm.
    """
    out: list[Item] = []
    for row in due:
        if row.kind != "overdue":
            continue
        cycles = row.cycles_missed or 0
        where = f"{row.shop_name}{f' ({row.area})' if row.area else ''}"
        rep = shops.get(row.shop_id)
        rep_name = rep.rep_name if rep is not None else ""
        if rep_name:
            tail = f" Rep {rep_name}: book a delivery this week."
        else:
            tail = " You: give it to a rep and book a delivery this week."
        out.append(Item("coverage", row.shop_id, (
            f"{where}: {cycles} restock cycle{'s' if cycles != 1 else ''} missed, nothing "
            f"in {row.days_since_last_delivery} days.{tail}")))
    return out


# ----------------------------------------------------------------- the cap


def _allocate(sections: dict[str, list[Item]]) -> tuple[list[Item], int]:
    """Fill the eight-item budget section by section, in the owner's order.

    Each section may spend up to its own share; an unused share is not carried
    over, because the sections are ordered by what the owner worries about first.
    """
    items: list[Item] = []
    for section, share in SECTION_ITEMS.items():
        room = ITEM_CAP - len(items)
        items.extend(sections.get(section, [])[:max(0, min(share, room))])
    return items, sum(len(candidates) for candidates in sections.values())


# ----------------------------------------------------------------- the words


def _render(b: Brief, book: _Book, sections: dict[str, list[Item]]) -> str:
    lines = [f"Mimi morning brief, {_date_words(book.day)}", "",
             _change_line(b, book),
             _money_head(b, book)]
    lines.extend(_lines_for(b, "money"))
    lines.append(_collections_head(b, book))
    lines.extend(_lines_for(b, "collections"))
    lines.append(_returns_head(b, book))
    lines.extend(_lines_for(b, "returns"))
    lines.append(_coverage_head(book))
    lines.extend(_lines_for(b, "coverage"))
    unknown = [row for row in book.due if row.kind == "unknown"]
    if unknown:
        names = ", ".join(row.shop_name for row in unknown[:6])
        more = "" if len(unknown) <= 6 else f" and {len(unknown) - 6} more"
        lines.append(f"The book has no delivery on record for {len(unknown)} shop"
                     f"{'s' if len(unknown) != 1 else ''}: {names}{more}. They are not "
                     "ranked.")
    if b.items_total > len(b.items):
        lines.append(f"{len(b.items)} items listed, {b.items_total - len(b.items)} more in "
                     "the book.")
    return "\n".join(line for line in lines if line)


def _lines_for(b: Brief, section: str) -> list[str]:
    return [f"{b.items.index(item) + 1}. {item.text}" for item in b.items
            if item.section == section]


def _change_line(b: Brief, book: _Book) -> str:
    snap = b.snapshot
    if book.previous is None:
        return (f"What changed: this is the first brief, so there is nothing to compare. "
                f"{owed.naira(snap['at_risk_kobo'])} at risk, "
                f"{owed.naira(snap['overdue_kobo'])} of it overdue, "
                f"{snap['returns_open']} return{'s' if snap['returns_open'] != 1 else ''} "
                "open. Tomorrow says what moved.")
    was = int(book.previous.get("at_risk_kobo") or 0)
    movement = snap["at_risk_kobo"] - was
    moved = (f"up {owed.naira(movement)}" if movement > 0 else
             f"down {owed.naira(-movement)}" if movement < 0 else "unchanged")
    closed = snap["returns_closed"] - int(book.previous.get("returns_closed") or 0)
    parts = [f"money at risk is {owed.naira(snap['at_risk_kobo'])}, {moved} on the last "
             f"brief", f"{owed.naira(snap['collected_kobo'])} came in"]
    parts.append(f"{closed} more return{'s' if closed != 1 else ''} closed" if closed > 0
                 else f"{snap['returns_open']} returns still open")
    return "What changed: " + ", ".join(parts) + "."


def _money_head(b: Brief, book: _Book) -> str:
    debtors = [one for one in book.shops.values() if one.outstanding_kobo > 0]
    if not debtors:
        return "Money at risk: nothing is owed today."
    bands: dict[str, int] = {key: 0 for key, _word in BUCKET_WORDS}
    shops_in: dict[str, set[str]] = {key: set() for key, _word in BUCKET_WORDS}
    for shop_id, days, kobo in book.invoices:
        key = bucket_of(days)
        if key in bands and days > 0:
            bands[key] += kobo
            shops_in[key].add(shop_id)
    aged = [f"{owed.naira(bands[key])} {word} ({len(shops_in[key])} shop"
            f"{'s' if len(shops_in[key]) != 1 else ''})"
            for key, word in BUCKET_WORDS if bands[key]]
    return (f"Money at risk: {owed.naira(b.snapshot['at_risk_kobo'])} open across "
            f"{len(debtors)} shop{'s' if len(debtors) != 1 else ''}."
            + (f" {', '.join(aged)}." if aged else ""))


def _collections_head(b: Brief, book: _Book) -> str:
    due = [one for one in book.shops.values() if one.overdue_kobo > 0]
    if not due:
        return "Collections today: nothing is overdue for collection."
    return (f"Collections today: {len(due)} shop{'s' if len(due) != 1 else ''} overdue on "
            f"{owed.naira(sum(one.overdue_kobo for one in due))}.")


def _returns_head(b: Brief, book: _Book) -> str:
    if not book.returns:
        return "Returns: nothing is open."
    value = sum(one.value_kobo for one in book.returns)
    escalated = sum(1 for one in book.returns if one.days_open > ESCALATE_AFTER_DAYS)
    tail = f", {escalated} escalated" if escalated else ""
    return (f"Returns: {len(book.returns)} open, {owed.naira(value)}{tail}.")


def _coverage_head(book: _Book) -> str:
    measured = [row for row in book.due if row.kind == "overdue"]
    if not measured:
        return "Coverage: every shop is inside its own restock rhythm."
    return (f"Coverage: {len(measured)} shop{'s' if len(measured) != 1 else ''} past "
            f"{'their' if len(measured) != 1 else 'its'} own restock rhythm.")


def variance(engine: Engine, tenant_id: str, day: date) -> str:
    """What the morning targeted against what came in, per rep (the 8:30 slot).

    The targets are the collection items the brief filed this morning; the
    collection is every payment on the book's day from the shop each one named.
    A rep with no target and no payment is not named, because there is nothing
    to say about him.
    """
    with engine.connect() as conn:
        row = conn.execute(sa.select(s.brief_snapshots).where(
            s.brief_snapshots.c.tenant_id == tenant_id,
            s.brief_snapshots.c.day == day.isoformat())).first()
        if row is None:
            return "No collection target was set this morning, so the day cannot be measured."
        targets = json.loads(str(row.collection_targets or "[]"))
        book_day = str(row.book_day or "")
        reps = {str(r.rep_id): str(r.name) for r in conn.execute(
            sa.select(s.reps.c.rep_id, s.reps.c.name).where(
                s.reps.c.tenant_id == tenant_id))}
        paid: dict[str, int] = {}
        for r in conn.execute(sa.select(s.payments.c.shop_id, s.payments.c.amount_kobo)
                              .where(s.payments.c.tenant_id == tenant_id,
                                     s.payments.c.fake_alert.is_(False),
                                     s.payments.c.paid_on == book_day)):
            if targets:
                paid[str(r.shop_id)] = paid.get(str(r.shop_id), 0) + int(r.amount_kobo)
    if not targets:
        return ("No collection target was set this morning: nothing was far enough overdue "
                "to chase.")
    per_rep: dict[str, dict[str, int]] = {}
    for target in targets:
        rep_id = str(target.get("rep_id") or "")
        entry = per_rep.setdefault(rep_id, {"targeted": 0, "collected": 0})
        entry["targeted"] += int(target.get("expected_kobo") or 0)
        entry["collected"] += paid.get(str(target.get("shop_id") or ""), 0)
    parts: list[str] = []
    for rep_id, entry in sorted(per_rep.items(), key=lambda pair: reps.get(pair[0], "")):
        who = reps.get(rep_id) or "a rep not on the book"
        targeted, collected = entry["targeted"], entry["collected"]
        if collected >= targeted:
            parts.append(f"Rep {who} was set {owed.naira(targeted)} and "
                         f"{owed.naira(collected)} came in.")
        else:
            parts.append(f"Rep {who} was set {owed.naira(targeted)} and "
                         f"{owed.naira(collected)} came in, {owed.naira(targeted - collected)} "
                         "short.")
    return "Collections: " + " ".join(parts)


def _area(one: _Shop) -> str:
    return f" ({one.area})" if one.area else ""


def _date_words(day: date) -> str:
    return f"{DAYS[day.weekday()]} {day.day} {MONTHS[day.month - 1]} {day.year}"


def _day_words(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {MONTHS[d.month - 1]}"


# ----------------------------------------------------------------- the record


def record(engine: Engine, tenant_id: str, brief: Brief, at: datetime) -> None:
    """Keep the brief's own numbers, so tomorrow says what changed.

    One row per tenant and Lagos day (the routine's claim already guarantees one
    run a day, and the primary key makes a second impossible). Nothing in the
    book changes, and no model is called.
    """
    with engine.begin() as conn:
        insert_ignore(conn, s.brief_snapshots, {
            "tenant_id": tenant_id, "day": brief.day.isoformat(),
            "book_day": brief.book_day.isoformat(), **brief.snapshot,
            "items_listed": len(brief.items),
            "collection_targets": json.dumps(brief.targets, sort_keys=True),
            "created_at": now_iso(at.timestamp())})

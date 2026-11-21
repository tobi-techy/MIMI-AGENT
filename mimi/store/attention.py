"""The daily attention list: which shops need a call today, and why (MIAG-9).

The first read of an imported book that a distributor pays for. For one tenant
and one day it answers, per shop, how far past its own restock rhythm it is, and
the money behind it. Pure code: no model, no writes, no new tables.

Two rules are what make the order believable:

* **The row is ranked by cycles missed, never by days.** ``restock_days`` is per
  shop, so nine days is four missed cycles for a two-day shop and one for a
  weekly shop, and the two-day shop is the emergency. Days are not comparable
  across shops; cycles are. Outstanding kobo breaks a tie and never starts a row.
* **A hole is not a zero.** A shop with nothing delivered on or before the day,
  or no restock interval in the book, cannot be ranked at all and must not sink
  to the bottom as though it were calm. Those rows are ``unknown``: they sort
  above every measured row and carry no cycles number, because there is nothing
  to divide by (``plans/mimi-mvp/rep-surface-contract.md``, the exception zone).

Money is integer kobo, summed here from ``receivables``; no figure is read from
a file total. The day is the book's own (``tenants.live_date``), the same day
every other surface reads, so an imported book counted on its export day agrees
with the shop page; ``mimi.clock`` on the Lagos calendar (``mimi.tz``) only
answers when the book names no day at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from typing import Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.engine import Engine

from mimi import owed
from mimi.clock import wall
from mimi.store import schema as s
from mimi.store.db import database_url, make_engine
from mimi.tz import LAGOS

#: How many rows the owner-facing message lists before it says how many are
#: left. The list is for a person to act on this morning, not a report.
LISTED = 10

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class AttentionRow(BaseModel):
    """One shop that needs a call today, or one the book cannot judge yet."""

    model_config = ConfigDict(extra="forbid")

    shop_id: str
    shop_name: str
    area: str
    rep_id: str
    #: ``overdue`` is a measurement; ``unknown`` is a hole in the book.
    kind: Literal["overdue", "unknown"]
    #: Days since the last delivery divided by the shop's own rhythm, floored.
    #: ``None`` on an unknown row: a hole has no interval to divide by.
    cycles_missed: int | None
    restock_days: int | None
    days_since_last_delivery: int | None
    outstanding_kobo: int
    overdue_kobo: int
    #: True when the credit position is negative: a limit of zero is no credit
    #: granted, so any debt at all is over it.
    over_limit: bool
    #: The credit limit less what the shop owes; negative is over the limit.
    credit_position_kobo: int


def today() -> date:
    """Today on the Lagos clock. Only for a book that names no day of its own."""
    return datetime.fromtimestamp(wall(), LAGOS).date()


def attention(engine: Engine, tenant_id: str, day: date | None = None) -> list[AttentionRow]:
    """Every shop that needs a call on ``day``, unknown rows first.

    ``day`` defaults to the book's own day (``tenants.live_date``), which is the
    day every other surface counts overdue from: an imported book read without
    an argument then agrees with the shop page instead of measuring from the
    machine's calendar. A shop is listed when it has missed at least one restock
    cycle, or when the book cannot say how far past it is; a shop inside its
    cycle is not an exception and is left out.
    """
    with engine.connect() as conn:
        live_date = conn.execute(sa.select(s.tenants.c.live_date).where(
            s.tenants.c.tenant_id == tenant_id)).scalar()
        if live_date is None:
            return []
        counted_on = day if day is not None else _book_day(str(live_date))
        shops = conn.execute(sa.select(
            s.shops.c.shop_id, s.shops.c.name, s.shops.c.area, s.shops.c.rep_id,
            s.shops.c.restock_days, s.shops.c.credit_limit_kobo).where(
            s.shops.c.tenant_id == tenant_id).order_by(s.shops.c.shop_id)).all()
        if not shops:
            return []
        # The newest delivery in the book whatever its date, so a delivery dated
        # after the day being counted is a fact about the shop rather than a
        # shop that looks like it has none. The date string is what is read, not
        # the stored day number: the synthetic world numbers its days from its
        # own epoch while an imported book stores date ordinals, and a calendar
        # day is the one thing both hold.
        last = {r.shop_id: date.fromisoformat(str(r.last_on)) for r in conn.execute(
            sa.select(s.deliveries.c.shop_id,
                      sa.func.max(s.deliveries.c.delivered_on).label("last_on")).where(
                s.deliveries.c.tenant_id == tenant_id).group_by(s.deliveries.c.shop_id))}
        money = {r.shop_id: (int(r.outstanding or 0), int(r.overdue or 0))
                 for r in conn.execute(sa.select(
                     s.receivables.c.shop_id,
                     sa.func.sum(s.receivables.c.outstanding_kobo).label("outstanding"),
                     sa.func.sum(sa.case(
                         (s.receivables.c.due_on < counted_on.isoformat(),
                          s.receivables.c.outstanding_kobo), else_=0)).label("overdue")).where(
                     s.receivables.c.tenant_id == tenant_id,
                     s.receivables.c.outstanding_kobo > 0).group_by(
                     s.receivables.c.shop_id))}

    rows: list[AttentionRow] = []
    for shop in shops:
        outstanding, overdue = money.get(shop.shop_id, (0, 0))
        rhythm = int(shop.restock_days or 0)
        restock = rhythm if rhythm > 0 else None
        delivered = last.get(shop.shop_id)
        # A delivery dated after the day being counted has not happened as of
        # that day, so the days since cannot be measured from it. The shop is a
        # hole for this day (unknown, no cycles) rather than calm or absent: an
        # as-of view of a book cannot claim to know a delivery it has not seen.
        days = (None if delivered is None or delivered > counted_on
                else (counted_on - delivered).days)
        position = int(shop.credit_limit_kobo) - outstanding
        cycles = days // restock if (days is not None and restock) else None
        if cycles is not None and cycles < 1:
            continue
        rows.append(AttentionRow(
            shop_id=shop.shop_id, shop_name=shop.name, area=shop.area,
            rep_id=shop.rep_id, kind="unknown" if cycles is None else "overdue",
            cycles_missed=cycles, restock_days=restock,
            days_since_last_delivery=days, outstanding_kobo=outstanding,
            overdue_kobo=overdue, over_limit=position < 0, credit_position_kobo=position))
    # Unknown first, then most cycles missed, then most owed, then shop id so a
    # tie never reorders between two reads of the same book.
    rows.sort(key=lambda r: (r.kind != "unknown", -(r.cycles_missed or 0),
                             -r.outstanding_kobo, r.shop_id))
    return rows


def _book_day(live_date: str) -> date:
    """The book's own day, or today when the book names none."""
    return date.fromisoformat(live_date) if live_date else today()


def render(rows: list[AttentionRow], day: date) -> str:
    """The list as the owner reads it: what is wrong and the numbers behind it."""
    label = f"{day.day} {_MONTHS[day.month - 1]} {day.year}"
    if not rows:
        return f"No shop needs attention today ({label})."
    shown = rows[:LISTED]
    unknown = [r for r in shown if r.kind == "unknown"]
    measured = [r for r in shown if r.kind == "overdue"]
    count = len(rows)
    head = f"1 shop needs attention today ({label})." if count == 1 else (
        f"{count} shops need attention today ({label}).")
    out = [head]
    if unknown:
        out.append("")
        out.append("The book cannot judge these, so they are not ranked:")
        out.extend(f"  {_line(r)}" for r in unknown)
    if measured:
        out.append("")
        out.append("Past their own restock cycle, most cycles missed first:")
        out.extend(f"  {_line(r)}" for r in measured)
    if count > len(shown):
        out.append("")
        out.append(f"{count - len(shown)} more not shown; --json prints every row.")
    return "\n".join(out)


def _line(row: AttentionRow) -> str:
    who = ", ".join(p for p in (row.shop_name, row.area, row.rep_id) if p)
    parts: list[str] = []
    if row.days_since_last_delivery is None:
        # Either nothing on record or only a delivery dated after this day: the
        # line says what is true of the day being counted, never "no delivery"
        # for a shop the book has one for.
        parts.append("nothing delivered by this day")
    elif row.restock_days is None:
        parts.append("no restock interval in the book")
        parts.append(_days(row.days_since_last_delivery))
    else:
        every = "every day" if row.restock_days == 1 else f"every {row.restock_days} days"
        parts.append(f"restocks {every}")
        parts.append(_days(row.days_since_last_delivery))
        if row.cycles_missed is not None:
            one = row.cycles_missed == 1
            parts.append(f"{row.cycles_missed} cycle{'s' if not one else ''} missed")
    parts.append(f"{owed.naira(row.outstanding_kobo)} outstanding")
    if row.overdue_kobo:
        parts.append(f"{owed.naira(row.overdue_kobo)} of it overdue")
    if row.over_limit:
        limit = row.credit_position_kobo + row.outstanding_kobo
        parts.append(f"{owed.naira(-row.credit_position_kobo)} over its "
                     f"{owed.naira(limit)} limit")
    return f"{who}: {', '.join(parts)}."


def _days(days: int) -> str:
    if days == 0:
        return "delivered today"
    return f"nothing in {days} day" if days == 1 else f"nothing in {days} days"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m mimi.store.attention",
        description="Which shops need a call today, by cycles missed on their own "
                    "restock rhythm, and what they owe.")
    ap.add_argument("--tenant-id", required=True, help="the distributor's tenant id")
    ap.add_argument("--db", default="", help="SQLAlchemy URL (default: DATABASE_URL)")
    ap.add_argument("--day", default="",
                    help="the day to count from, ISO (default: the book's own day, "
                         "tenants.live_date; use it to count a book from another day)")
    ap.add_argument("--json", action="store_true", help="print the typed rows")
    args = ap.parse_args(argv)
    try:
        asked = date.fromisoformat(args.day) if args.day else None
    except ValueError:
        print(f"--day {args.day!r} is not an ISO date (2026-10-27)", file=sys.stderr)
        return 2
    engine = make_engine(args.db or database_url())
    with engine.connect() as conn:
        live_date = conn.execute(sa.select(s.tenants.c.live_date).where(
            s.tenants.c.tenant_id == args.tenant_id)).scalar()
    if live_date is None:
        # An empty book is an empty list, but a mistyped tenant id must not read
        # as one: the operator would believe the book is calm.
        print(f"no tenant {args.tenant_id!r} in this book", file=sys.stderr)
        return 2
    day = asked if asked is not None else _book_day(str(live_date))
    rows = attention(engine, args.tenant_id, day)
    if args.json:
        print(json.dumps([r.model_dump() for r in rows], indent=2))
    else:
        print(render(rows, day))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

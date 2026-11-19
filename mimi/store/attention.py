"""The daily attention list: which shops need a call today, and why (MIAG-9).

The first read of an imported book that a distributor pays for. For one tenant
and one day it answers, per shop, how far past its own restock rhythm it is, and
the money behind it. Pure code: no model, no writes, no new tables.

Two rules are what make the order believable:

* **The row is ranked by cycles missed, never by days.** ``restock_days`` is per
  shop, so nine days is four missed cycles for a two-day shop and one for a
  weekly shop, and the two-day shop is the emergency. Days are not comparable
  across shops; cycles are. Outstanding kobo breaks a tie and never starts a row.
* **A hole is not a zero.** A shop with no delivery on record, or no restock
  interval in the book, cannot be ranked at all and must not sink to the bottom
  as though it were calm. Those rows are ``unknown``: they sort above every
  measured row and carry no cycles number, because there is no interval to
  divide by (``plans/mimi-mvp/rep-surface-contract.md``, the exception zone).

Money is integer kobo, summed here from ``receivables``; no figure is read from
a file total. Days come from the one wall clock (``mimi.clock``) on the Lagos
calendar day (``mimi.tz``).
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
    """The day the list counts from: today on the Lagos clock."""
    return datetime.fromtimestamp(wall(), LAGOS).date()


def attention(engine: Engine, tenant_id: str, day: date | None = None) -> list[AttentionRow]:
    """Every shop that needs a call on ``day``, unknown rows first (default: today).

    A shop is listed when it has missed at least one restock cycle, or when the
    book cannot say how far past it is. A shop inside its cycle is not an
    exception and is left out.
    """
    counted_on = today() if day is None else day
    ordinal = counted_on.toordinal()
    with engine.connect() as conn:
        shops = conn.execute(sa.select(
            s.shops.c.shop_id, s.shops.c.name, s.shops.c.area, s.shops.c.rep_id,
            s.shops.c.restock_days, s.shops.c.credit_limit_kobo).where(
            s.shops.c.tenant_id == tenant_id).order_by(s.shops.c.shop_id)).all()
        if not shops:
            return []
        # Deliveries after the run day are not a visit yet: a book imported with
        # a later date must not read as having delivered before it happened.
        last = {r.shop_id: int(r.last_day or 0) for r in conn.execute(
            sa.select(s.deliveries.c.shop_id,
                      sa.func.max(s.deliveries.c.day).label("last_day")).where(
                s.deliveries.c.tenant_id == tenant_id,
                s.deliveries.c.day <= ordinal).group_by(s.deliveries.c.shop_id))}
        money = {r.shop_id: (int(r.outstanding or 0), int(r.overdue or 0))
                 for r in conn.execute(sa.select(
                     s.receivables.c.shop_id,
                     sa.func.sum(s.receivables.c.outstanding_kobo).label("outstanding"),
                     sa.func.sum(sa.case(
                         (s.receivables.c.due_day < ordinal,
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
        days = None if delivered is None else max(0, ordinal - delivered)
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
    if row.kind == "unknown" and row.days_since_last_delivery is None:
        parts.append("no delivery on record")
    elif row.restock_days is None:
        parts.append("no restock interval in the book")
        if row.days_since_last_delivery is not None:
            parts.append(_days(row.days_since_last_delivery))
    else:
        every = "every day" if row.restock_days == 1 else f"every {row.restock_days} days"
        parts.append(f"restocks {every}")
        if row.days_since_last_delivery is not None:
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
    return "delivered today" if days == 0 else f"nothing in {days} days"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m mimi.store.attention",
        description="Which shops need a call today, by cycles missed on their own "
                    "restock rhythm, and what they owe.")
    ap.add_argument("--tenant-id", required=True, help="the distributor's tenant id")
    ap.add_argument("--db", default="", help="SQLAlchemy URL (default: DATABASE_URL)")
    ap.add_argument("--day", default="",
                    help="the day to count from, ISO (default: today in Lagos; use it "
                         "for a book exported on an earlier day)")
    ap.add_argument("--json", action="store_true", help="print the typed rows")
    args = ap.parse_args(argv)
    try:
        day = date.fromisoformat(args.day) if args.day else today()
    except ValueError:
        print(f"--day {args.day!r} is not an ISO date (2026-10-27)", file=sys.stderr)
        return 2
    engine = make_engine(args.db or database_url())
    with engine.connect() as conn:
        known = conn.execute(sa.select(s.tenants.c.tenant_id).where(
            s.tenants.c.tenant_id == args.tenant_id)).scalar()
    if known is None:
        # An empty book is an empty list, but a mistyped tenant id must not read
        # as one: the operator would believe the book is calm.
        print(f"no tenant {args.tenant_id!r} in this book", file=sys.stderr)
        return 2
    rows = attention(engine, args.tenant_id, day)
    if args.json:
        print(json.dumps([r.model_dump() for r in rows], indent=2))
    else:
        print(render(rows, day))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

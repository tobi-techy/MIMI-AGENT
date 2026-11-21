"""The returns a distributor is still carrying, aged (MIAG-10).

The daily-value read behind the brief's returns section: for one tenant and one
day, every return that is still open, how long it has been open, what it is
worth and who carries it. Pure code: no model, no writes, no new table.

Two rules make the list worth reading:

* **Open is one definition** (``mimi.types.RETURN_OPEN_STATUSES``, read through
  ``mimi.returns.ledger``): a return is open while the goods or the credit are
  still in flight, whichever path opened it. A credited or rejected return is
  closed and never ages.
* **Age is days since it was raised**, on the book's own day, from the day the
  return records. A return raised with no day in the book (a row written before
  the column existed) ages from the day it was created, so nothing hides.

Money is integer kobo, read from the return's own value column; the value was
summed from its lines when the row was written.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.engine import Engine

from mimi import owed
from mimi.store import schema as s
from mimi.store.db import database_url, make_engine
from mimi.types import RETURN_OPEN_STATUSES

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def at_stake_kobo(status: str, *, drafted_kobo: int, collected_kobo: int | None,
                  returned_kobo: int | None) -> int:
    """What a return is worth *at its stage*, in kobo: one rule for every reader.

    A return's value moves as the goods move, and reporting the drafted figure
    for goods that came back short is a number nobody owes:

    * ``drafted`` or ``approved``: what the return was raised for;
    * ``collected`` or later: what the rep actually took;
    * ``inspected`` or later: what inspection accepted, which is the figure a
      credit note rests on.

    A stage with no figure recorded falls back to the one before it, because a
    missing count is not a zero. The aged read and the daily brief both call
    this, so the two can never disagree about what is at stake.
    """
    taken = int(collected_kobo or 0)
    back = int(returned_kobo or 0)
    raised = int(drafted_kobo or 0)
    if status in ("inspected", "settled"):
        return back or taken or raised
    if status == "collected":
        return taken or raised
    return raised


class AgedReturn(BaseModel):
    """One return still open, and what it costs to leave it there."""

    model_config = ConfigDict(extra="forbid")

    return_id: str
    shop_id: str
    shop_name: str
    rep_id: str
    rep_name: str
    status: str
    raised_on: str
    days_open: int
    value_kobo: int
    reason: str


def aged_returns(engine: Engine, tenant_id: str, day: date | None = None) -> list[AgedReturn]:
    """Every open return for ``tenant_id``, oldest first.

    ``day`` defaults to the book's own day (``tenants.live_date``), the same day
    every other surface counts from, so an imported book read on its export day
    agrees with the shop page.
    """
    with engine.connect() as conn:
        live_date = conn.execute(sa.select(s.tenants.c.live_date).where(
            s.tenants.c.tenant_id == tenant_id)).scalar()
        if live_date is None:
            return []
        counted_on = day if day is not None else date.fromisoformat(str(live_date))
        ordinal = counted_on.toordinal()
        rows = conn.execute(
            sa.select(s.returns, s.shops.c.name.label("shop_name"),
                      s.reps.c.name.label("rep_name"))
            .join(s.shops, sa.and_(s.shops.c.tenant_id == s.returns.c.tenant_id,
                                   s.shops.c.shop_id == s.returns.c.shop_id))
            .outerjoin(s.reps, sa.and_(s.reps.c.tenant_id == s.returns.c.tenant_id,
                                       s.reps.c.rep_id == s.returns.c.rep_id))
            .where(s.returns.c.tenant_id == tenant_id,
                   s.returns.c.status.in_(tuple(sorted(RETURN_OPEN_STATUSES))))
            .order_by(s.returns.c.raised_day, s.returns.c.return_id)).all()
    out: list[AgedReturn] = []
    for r in rows:
        raised = int(r.raised_day or 0) or _created_day(str(r.created_at or ""))
        out.append(AgedReturn(
            return_id=str(r.return_id), shop_id=str(r.shop_id),
            shop_name=str(r.shop_name), rep_id=str(r.rep_id or ""),
            rep_name=str(r.rep_name or ""), status=str(r.status),
            raised_on=str(r.raised_on or ""),
            days_open=max(0, ordinal - raised) if raised else 0,
            value_kobo=at_stake_kobo(str(r.status), drafted_kobo=int(r.value_drafted_kobo or 0),
                                     collected_kobo=int(r.value_collected_kobo or 0),
                                     returned_kobo=int(r.value_returned_kobo or 0)),
            reason=str(r.reason or "")))
    out.sort(key=lambda r: (-r.days_open, -r.value_kobo, r.return_id))
    return out


def _created_day(created_at: str) -> int:
    """The day a row with no recorded raised day was created, as an ordinal."""
    try:
        return date.fromisoformat(created_at[:10]).toordinal()
    except (TypeError, ValueError):
        return 0


def render(rows: list[AgedReturn], day: date) -> str:
    """The list as a person reads it, oldest return first."""
    label = f"{day.day} {_MONTHS[day.month - 1]} {day.year}"
    if not rows:
        return f"No return is open on {label}."
    total = sum(r.value_kobo for r in rows)
    head = (f"1 return is open on {label}, worth {owed.naira(total)}." if len(rows) == 1
            else f"{len(rows)} returns are open on {label}, worth {owed.naira(total)}.")
    lines = [head, ""]
    lines.extend(f"  {_line(r)}" for r in rows)
    return "\n".join(lines)


def _line(row: AgedReturn) -> str:
    who = ", ".join(p for p in (row.shop_name, row.rep_name) if p)
    days = ("raised today" if row.days_open == 0 else
            "raised 1 day ago" if row.days_open == 1 else
            f"open {row.days_open} days")
    return (f"{who}: {owed.naira(row.value_kobo)}, {days}, {row.status.replace('_', ' ')}"
            f"{', ' + row.reason if row.reason else ''}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m mimi.store.returns",
        description="The returns still open, aged: days since raised, what each is "
                    "worth, the shop and the rep.")
    ap.add_argument("--tenant-id", required=True, help="the distributor's tenant id")
    ap.add_argument("--db", default="", help="SQLAlchemy URL (default: DATABASE_URL)")
    ap.add_argument("--day", default="",
                    help="the day to count from, ISO (default: the book's own day, "
                         "tenants.live_date)")
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
        # A mistyped tenant id must not read as a book with no returns.
        print(f"no tenant {args.tenant_id!r} in this book", file=sys.stderr)
        return 2
    day = asked if asked is not None else date.fromisoformat(str(live_date))
    rows = aged_returns(engine, args.tenant_id, day)
    if args.json:
        print(json.dumps([r.model_dump() for r in rows], indent=2))
    else:
        print(render(rows, day))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

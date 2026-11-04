"""The one place that says how invoice rows are read before they are written.

Three things write a shop's ``paid_kobo``, ``outstanding_kobo`` and ``status``:
a return credit (``mimi.api.returns``), Go's own writer for a verified bank
payment (``mimi.api.demo_wiring._apply_to_invoices`` and
``mimi.api.week.go.GoSim._pay``), and the routine. Each of them has to read the
invoice it is about to change, work out the new figures from that read, and
write those figures back, so a writer whose read is not holding anything can
have its figures overwritten by another writer that read first and committed
later. That is a lost update, and it is a wrong balance rather than a failed
call: nobody sees an error.

Every one of those reads goes through this module, and that is what makes the
lock order a single order: ``position`` then ``delivery_id``, the same order
``SqlGo.receivables`` reads and ``returns.ledger.credit_plan`` allocates in. Two
writers that both take the rows that way queue behind each other instead of
taking the same two rows in opposite orders, which is what deadlocks.

The lock is taken by the read rather than left to the write for two reasons.
The write needs figures only the read has (the plan of where a credit comes off
the invoices, and the status the invoice is left with), so the read has to
happen first and has to be the thing that holds the row. And a write that
locks without reading, such as ``UPDATE ... SET outstanding = outstanding - :x``,
cannot be asked for at all here: the amount each invoice takes is decided by a
plan in code (``ledger.credit_plan``), it is written down on the ``credit_notes``
row so an undo can subtract exactly it, and it is shown to the owner before the
credit is applied.

SQLite has one writer at a time and no row locks, and SQLAlchemy drops the
clause there, so this is safe offline and only observable on Postgres. The
tests that prove the blocking are Postgres only and say so.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa

from mimi.store import schema as s

#: The order invoice rows are taken in: oldest first, by position within the
#: tenant, with the delivery id breaking a tie. One definition, so a new writer
#: calls the read below instead of writing its own query and inventing a second
#: order a deadlock could cycle on.
LOCK_ORDER: tuple[str, ...] = ("position", "delivery_id")


def _ordered(stmt: Any, *, lock: bool) -> Any:
    q = stmt.order_by(*(s.receivables.c[name] for name in LOCK_ORDER))
    return q.with_for_update() if lock else q


def open_invoices(conn: sa.Connection, tenant_id: str, shop_id: str, *,
                  lock: bool = True) -> list[Any]:
    """A shop's open invoices, oldest first, taken ``FOR UPDATE`` by default.

    With ``lock=True`` the rows are held until the caller's transaction ends, so
    a writer that plans against them and then writes them cannot be overtaken.
    A caller that only looks, such as drafting a credit for the owner to
    approve, passes ``lock=False``: the plan it shows is a preview, and the
    figures are planned again from the book under the lock when the credit is
    applied.
    """
    return list(conn.execute(_ordered(
        sa.select(s.receivables).where(
            s.receivables.c.tenant_id == tenant_id,
            s.receivables.c.shop_id == shop_id,
            s.receivables.c.outstanding_kobo > 0), lock=lock)))


def invoices_by_id(conn: sa.Connection, tenant_id: str, delivery_ids: Iterable[str], *,
                   lock: bool = True) -> dict[str, Any]:
    """The named invoices by delivery id, taken in the same order when locked.

    The undo reads the invoices a credit came off by id, so it takes them the
    same way the apply did: all of them, in one order, before it writes any.
    """
    ids = [str(d) for d in dict.fromkeys(delivery_ids) if str(d)]
    if not ids:
        return {}
    rows = conn.execute(_ordered(
        sa.select(s.receivables).where(
            s.receivables.c.tenant_id == tenant_id,
            s.receivables.c.delivery_id.in_(ids)), lock=lock))
    return {str(r.delivery_id): r for r in rows}


def overdue_ids(conn: sa.Connection, tenant_id: str, day_no: int, *,
                lock: bool = True) -> list[str]:
    """The invoices past their due day and still owing, taken in the same order.

    The roll marks these overdue with one bulk ``UPDATE``. A bulk update takes
    the rows it touches itself, in whatever order the planner picks, so a
    transaction that is holding an invoice further down the order can deadlock
    against it. The roll reads them through here first, holding them, so it
    takes them the way every other writer does.
    """
    rows = conn.execute(_ordered(
        sa.select(s.receivables.c.delivery_id).where(
            s.receivables.c.tenant_id == tenant_id,
            s.receivables.c.outstanding_kobo > 0,
            s.receivables.c.due_day < day_no), lock=lock))
    return [str(r.delivery_id) for r in rows]

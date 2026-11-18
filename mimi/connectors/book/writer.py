"""Write a reading into the store: the dry run, the rejects, then the rows.

This is the half of ring 2 that knows the store. It does four things a source
cannot:

* **Checks each row against the book it lands in.** A delivery for a shop this
  distributor does not have, a line for a SKU nobody prices, a receivable whose
  total disagrees with its delivery: each is a reject with a reason, because the
  figure it would produce is wrong rather than missing.
* **Writes nothing twice.** Every entity is keyed by the distributor's own id.
  A row already in the book with the same content is *unchanged* and writes
  nothing; a row whose content differs is refused, never overwritten. An import
  repairs a gap and adds new rows; it does not edit a book it does not own.
* **Stamps provenance on every row.** The deliveries and payments carry the
  book's own proof columns, filled by ``mimi.provenance``: the distributor's
  record is one party's word, so those rows are ``estimated`` until a shop or the
  bank confirms them. Every imported row, including shops and SKUs, also writes
  one ``proof_events`` line naming the import run that created it, so a figure
  can be traced back to the file it came from.
* **Leaves a ledger row.** ``connector_sync_runs`` records what arrived, from
  where, when, how many rows and every reject: the one place that answers "where
  did this book come from".

A dry run is this module with the writes left out: the same plan, the same
rejects, nothing in the store.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.connectors.base import ConnectorError
from mimi.connectors.book.csv_source import read_book
from mimi.connectors.book.rows import (
    DUP_IN_BOOK,
    MIXES_TWO_SHOPS,
    TOTAL_MISMATCH,
    UNKNOWN_DELIVERY,
    UNKNOWN_REP,
    UNKNOWN_SHOP,
    UNKNOWN_SKU,
    PaymentRow,
    Reading,
    Reject,
)
from mimi.provenance import (
    POLICY_VERSION,
    ProofEvent,
    delivery_stamp,
    payment_type,
    stamp,
    witness,
)
from mimi.store import schema as s
from mimi.store.proofbook import event_id
from mimi.store.util import dumps, loads, now_iso
from mimi.tz import now_lagos

#: The store table each entity writes, and the order that respects what points
#: at what: a line needs its delivery, a delivery needs its shop and SKU.
STORE_TABLES: dict[str, sa.Table] = {
    "reps": s.reps, "shops": s.shops, "skus": s.skus, "deliveries": s.deliveries,
    "delivery_lines": s.delivery_lines, "receivables": s.receivables,
    "payments": s.payments,
}
WRITE_ORDER: tuple[str, ...] = ("reps", "shops", "skus", "deliveries", "delivery_lines",
                               "receivables", "payments")

#: The record type each entity's provenance line carries. Shops, reps, SKUs,
#: lines and receivables have no proof columns of their own, so their provenance
#: is the append-only event; deliveries and payments get both.
RECORD_TYPE = {"reps": "rep", "shops": "shop", "skus": "sku", "deliveries": "delivery",
               "delivery_lines": "delivery_line", "receivables": "receivable",
               "payments": "payment"}

#: A store row as it will be written, and the proof level it earns.
_Row = dict[str, Any]

#: Columns the book changes on its own after a row lands: a receipt confirms a
#: delivery, a bank credit lifts a payment's proof level, a dispute changes a
#: status. A re-import must not read those as "the file disagrees".
_OWNS_ITSELF: tuple[str, ...] = ("status", "witnessed_at", "live", "fake_alert", "partial",
                                "split_of", "proof_level", "witnesses",
                                "proof_policy_version")


@dataclass
class Plan:
    """What an import would do, worked out without writing anything."""

    tenant_id: str
    source: str
    label: str
    digest: str
    at: str
    live_date: str
    tenant: _Row | None = None
    tenant_new: bool = False
    #: True when this import moves the book's day, so the stored overdue word on
    #: invoices that were not yet due has to be recomputed.
    day_moved: bool = False
    creates: dict[str, list[_Row]] = field(default_factory=lambda: {k: [] for k in STORE_TABLES})
    unchanged: dict[str, int] = field(default_factory=lambda: {k: 0 for k in STORE_TABLES})
    rejects: list[Reject] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> dict[str, int]:
        out: dict[str, int] = {k: 0 for k in STORE_TABLES}
        for reject in self.rejects:
            out[reject.entity] = out.get(reject.entity, 0) + 1
        return out

    def counts(self) -> dict[str, dict[str, int]]:
        """Rows to add, rows already there, rows refused, per table."""
        rejected = self.rejected
        return {table: {"new": len(self.creates[table]), "unchanged": self.unchanged[table],
                        "rejected": rejected.get(table, 0)} for table in STORE_TABLES}

    def rows_written(self) -> int:
        return sum(len(rows) for rows in self.creates.values())


@dataclass(frozen=True)
class Report:
    """What an import did (or, for a dry run, would do)."""

    tenant_id: str
    source: str
    label: str
    digest: str
    at: str
    dry_run: bool
    live_date: str
    counts: dict[str, dict[str, int]]
    rejects: tuple[Reject, ...]
    notes: tuple[str, ...]

    def summary(self) -> str:
        """The preview an owner reads: counts a line each, then the rejects."""
        head = (f"{'dry run' if self.dry_run else 'imported'} {self.label} "
                f"({self.source}, {self.digest[:12]}) for {self.tenant_id}, "
                f"book day {self.live_date}")
        lines = [head]
        for table, count in self.counts.items():
            lines.append(f"  {table:<15} {count['new']:>5} added "
                         f"{count['unchanged']:>5} unchanged {count['rejected']:>5} rejected")
        for note in self.notes:
            lines.append(f"note: {note}")
        if self.rejects:
            lines.append(f"{len(self.rejects)} rows refused:")
            lines.extend(f"  {r.where}: {r.detail} [{r.reason}]" for r in self.rejects)
        return "\n".join(lines)


def import_book(engine: Engine, path: str | Path, *, tenant_id: str, dry_run: bool = False,
                as_of: str | None = None, at: str | None = None) -> Report:
    """Read a book file and write it into ``tenant_id``.

    ``dry_run`` works out every count and every reject and writes nothing.
    ``as_of`` names the book's day (the day overdue is counted from); without it
    the day is the last delivery or payment the book holds.
    """
    stamp = at or now_iso()  # one moment for the read, the rows and the ledger
    reading = read_book(path, tenant_id=tenant_id, read_at=stamp)
    plan = build_plan(engine, reading, tenant_id=tenant_id, at=stamp, as_of=as_of)
    if not dry_run:
        write_plan(engine, plan)
    return Report(tenant_id=tenant_id, source=plan.source, label=plan.label,
                  digest=plan.digest, at=plan.at, dry_run=dry_run, live_date=plan.live_date,
                  counts=plan.counts(), rejects=tuple(plan.rejects), notes=tuple(plan.notes))


# ------------------------------------------------------------------ the plan


def build_plan(engine: Engine, reading: Reading, *, tenant_id: str, at: str,
               as_of: str | None = None) -> Plan:
    """Decide what each row of ``reading`` becomes. Reads the book; writes nothing."""
    book = _existing(engine, tenant_id)
    if book.tenant is not None and bool(book.tenant.simulated):
        raise ConnectorError(
            f"Tenant {tenant_id!r} is the synthetic demo world, not a distributor.",
            next_step="Import a real book into a tenant of its own: pass --tenant-id "
                      "with the distributor's own id.")
    plan = Plan(tenant_id=tenant_id, source=reading.source, label=reading.label,
                digest=reading.digest, at=at, live_date="",
                # A row the source could not read is refused before anything else
                # is checked; the report is one list, in file order.
                rejects=list(reading.rejects))
    if book.tenant is None and reading.distributor is None:
        raise ConnectorError(
            "This book names no distributor, so there is no tenant to create.",
            next_step="Add the distributor row: tenant_id, name and owner_phones.")
    _plan_reps(plan, reading, book)
    _plan_shops(plan, reading, book)
    _plan_skus(plan, reading, book)
    _plan_deliveries(plan, reading, book)
    _plan_payments(plan, reading, book)
    # The book's day has to be known before receivables are planned: whether an
    # invoice is overdue is that day compared with its due date.
    plan.live_date = _live_date(plan, book, as_of)
    _plan_receivables(plan, reading, book)
    _plan_tenant(plan, reading, book)
    return plan


@dataclass
class _Book:
    """What the tenant already holds, read once."""

    tenant: Any | None = None
    reps: dict[str, Any] = field(default_factory=dict)
    shops: dict[str, Any] = field(default_factory=dict)
    skus: dict[str, Any] = field(default_factory=dict)
    deliveries: dict[str, Any] = field(default_factory=dict)
    lines: dict[str, dict[int, Any]] = field(default_factory=dict)
    receivables: dict[str, Any] = field(default_factory=dict)
    payments: dict[str, Any] = field(default_factory=dict)
    last_day: str = ""


def _existing(engine: Engine, tenant_id: str) -> _Book:
    book = _Book()
    with engine.connect() as conn:
        book.tenant = conn.execute(sa.select(s.tenants).where(
            s.tenants.c.tenant_id == tenant_id)).first()
        for name, table, key in (("reps", s.reps, "rep_id"), ("shops", s.shops, "shop_id"),
                                 ("skus", s.skus, "sku"), ("deliveries", s.deliveries,
                                                           "delivery_id"),
                                 ("receivables", s.receivables, "delivery_id"),
                                 ("payments", s.payments, "payment_id")):
            rows = conn.execute(sa.select(table).where(table.c.tenant_id == tenant_id)).all()
            setattr(book, name, {getattr(row, key): row for row in rows})
        for line in conn.execute(sa.select(s.delivery_lines).where(
                s.delivery_lines.c.tenant_id == tenant_id)):
            book.lines.setdefault(line.delivery_id, {})[int(line.line_no)] = line
        days = [str(d.delivered_on) for d in book.deliveries.values()]
        days += [str(p.paid_on) for p in book.payments.values()]
    book.last_day = max(days) if days else ""
    return book


def _plan_tenant(plan: Plan, reading: Reading, book: _Book) -> None:
    """The tenants row: created by the first import, moved on by later ones."""
    distributor = reading.distributor
    if book.tenant is not None:
        changes: dict[str, Any] = {}
        if distributor is not None:
            if book.tenant.name != distributor.name:
                changes["name"] = distributor.name
            if list(loads(book.tenant.owner_phones, [])) != list(distributor.owner_phones):
                changes["owner_phones"] = dumps(sorted(distributor.owner_phones))
        # The book's day follows the book: a later delivery means the book has
        # moved on, and overdue is counted from the newer day. Same file, same
        # day, no change.
        if plan.live_date > str(book.tenant.live_date):
            changes["live_date"] = plan.live_date
            changes["live_day"] = date.fromisoformat(plan.live_date).toordinal()
            plan.day_moved = True
        if changes:
            plan.tenant = {"tenant_id": plan.tenant_id, **changes}
            plan.notes.append("the distributor's own row changed: "
                              + ", ".join(sorted(changes)))
        return
    assert distributor is not None  # build_plan refused a book that names none
    plan.tenant_new = True
    plan.tenant = {
        "tenant_id": plan.tenant_id, "name": distributor.name,
        "owner_phones": dumps(sorted(distributor.owner_phones)),
        "owner_thread": f"{plan.tenant_id}:owner",
        "live_date": plan.live_date,
        "live_day": date.fromisoformat(plan.live_date).toordinal(),
        "price_rise_day": None, "daily_cap_kobo": 50_000,
        "collection_account": "{}",
        # A tenant made from a real distributor's own book is not synthetic, and
        # every read of it says so.
        "simulated": False, "created_at": plan.at,
    }


def _plan_reps(plan: Plan, reading: Reading, book: _Book) -> None:
    for rep in reading.reps:
        values = {"tenant_id": plan.tenant_id, "rep_id": rep.rep_id, "name": rep.name,
                  "phone": rep.phone, "areas": dumps(list(rep.areas))}
        row = book.reps.get(rep.rep_id)
        if row is None:
            plan.creates["reps"].append(values)
        elif _same(row, values, json_fields=("areas",)):
            plan.unchanged["reps"] += 1
        else:
            plan.rejects.append(_dup("reps", rep.rep_id, rep.provenance.where, "rep"))


def _plan_shops(plan: Plan, reading: Reading, book: _Book) -> None:
    reps = set(book.reps) | {row["rep_id"] for row in plan.creates["reps"]}
    for shop in reading.shops:
        if shop.rep_id and shop.rep_id not in reps:
            plan.rejects.append(Reject(
                "shops", shop.shop_id, shop.provenance.where, UNKNOWN_REP,
                f"shop {shop.shop_id} is covered by rep {shop.rep_id!r}, whom the book "
                "does not have"))
            continue
        values = {"tenant_id": plan.tenant_id, "shop_id": shop.shop_id, "name": shop.name,
                  "shop_type": "", "size": "", "owner": shop.owner, "area": shop.area,
                  "street": shop.street, "phone": shop.phone, "rep_id": shop.rep_id,
                  "credit_limit_kobo": shop.credit_limit_kobo, "bank": "",
                  "restock_days": shop.restock_days, "simulated": False}
        row = book.shops.get(shop.shop_id)
        if row is None:
            plan.creates["shops"].append(values)
        elif _same(row, values):
            plan.unchanged["shops"] += 1
        else:
            plan.rejects.append(_dup("shops", shop.shop_id, shop.provenance.where, "shop"))


def _plan_skus(plan: Plan, reading: Reading, book: _Book) -> None:
    for sku in reading.skus:
        values = {"tenant_id": plan.tenant_id, "sku": sku.sku, "name": sku.name,
                  "brand": sku.brand, "unit": sku.unit, "price_kobo": sku.price_kobo,
                  "risen_price_kobo": None}
        row = book.skus.get(sku.sku)
        if row is None:
            plan.creates["skus"].append(values)
        elif _same(row, values):
            plan.unchanged["skus"] += 1
        else:
            plan.rejects.append(_dup("skus", sku.sku, sku.provenance.where, "SKU"))


def _plan_deliveries(plan: Plan, reading: Reading, book: _Book) -> None:
    shops = set(book.shops) | {row["shop_id"] for row in plan.creates["shops"]}
    reps = set(book.reps) | {row["rep_id"] for row in plan.creates["reps"]}
    # What each SKU is called, from the book and from this file: a line takes its
    # name and unit from the product, never from itself.
    skus = {sku: (row.name, row.unit) for sku, row in book.skus.items()}
    skus.update({row["sku"]: (row["name"], row["unit"]) for row in plan.creates["skus"]})
    for delivery in reading.deliveries:
        where = delivery.provenance.where
        if delivery.shop_id not in shops:
            plan.rejects.append(Reject(
                "deliveries", delivery.delivery_id, where, UNKNOWN_SHOP,
                f"delivery {delivery.delivery_id} is for shop {delivery.shop_id!r}, "
                "which this book does not have"))
            continue
        if delivery.rep_id and delivery.rep_id not in reps:
            plan.rejects.append(Reject(
                "deliveries", delivery.delivery_id, where, UNKNOWN_REP,
                f"delivery {delivery.delivery_id} is carried by rep {delivery.rep_id!r}, "
                "whom the book does not have"))
            continue
        unknown = [line.sku for line in delivery.lines if line.sku not in skus]
        if unknown:
            plan.rejects.append(Reject(
                "deliveries", delivery.delivery_id, where, UNKNOWN_SKU,
                f"delivery {delivery.delivery_id} carries {', '.join(sorted(set(unknown)))}, "
                "which the book does not price"))
            continue
        values = {
            "tenant_id": plan.tenant_id, "delivery_id": delivery.delivery_id,
            "shop_id": delivery.shop_id, "rep_id": delivery.rep_id,
            # Day numbers are date ordinals, so "days since" arithmetic
            # (overdue, aging, the credit window) works on an imported book
            # exactly as it does on the synthetic one.
            "day": delivery.delivered_on.toordinal(),
            "delivered_on": delivery.delivered_on.isoformat(),
            "minute": delivery.minute, "total_kobo": delivery.total_kobo,
            "status": "delivered",
            # No shop has confirmed this delivery yet: that is what a receipt is
            # for, and an import must not claim one.
            "witnessed_at": None, "live": False,
            **delivery_stamp(delivery.delivery_id).columns(),
        }
        line_values = [_line_values(plan, delivery.delivery_id, line, skus[line.sku])
                       for line in delivery.lines]
        row = book.deliveries.get(delivery.delivery_id)
        if row is None:
            plan.creates["deliveries"].append(values)
            plan.creates["delivery_lines"].extend(line_values)
        elif _same(row, values, ignore=_OWNS_ITSELF) and _same_lines(
                book.lines.get(delivery.delivery_id, {}), line_values):
            plan.unchanged["deliveries"] += 1
            plan.unchanged["delivery_lines"] += len(line_values)
        else:
            plan.rejects.append(_dup("deliveries", delivery.delivery_id, where, "delivery"))


def _line_values(plan: Plan, delivery_id: str, line: Any,
                 sku: tuple[str, str]) -> _Row:
    """One delivery line as the store holds it."""
    name, unit = sku
    return {
        "tenant_id": plan.tenant_id, "delivery_id": delivery_id, "line_no": line.line_no,
        "sku": line.sku, "name": name, "unit": unit, "quantity": line.quantity,
        "unit_price_kobo": line.unit_price_kobo,
        "line_total_kobo": line.quantity * line.unit_price_kobo,
        "expires_on": line.expires_on.isoformat() if line.expires_on else None,
        "batch": line.batch or None,
    }


def _plan_receivables(plan: Plan, reading: Reading, book: _Book) -> None:
    deliveries = _delivery_totals(plan, reading, book)
    for receivable in reading.receivables:
        where = receivable.provenance.where
        known = deliveries.get(receivable.delivery_id)
        if known is None:
            plan.rejects.append(Reject(
                "receivables", receivable.delivery_id, where, UNKNOWN_DELIVERY,
                f"the receivable names delivery {receivable.delivery_id!r}, which this "
                "book does not have"))
            continue
        shop_id, total = known
        if receivable.shop_id != shop_id:
            plan.rejects.append(Reject(
                "receivables", receivable.delivery_id, where, MIXES_TWO_SHOPS,
                f"the receivable is for shop {receivable.shop_id!r} and its delivery "
                f"{receivable.delivery_id} went to shop {shop_id!r}"))
            continue
        if receivable.total_kobo != total:
            plan.rejects.append(Reject(
                "receivables", receivable.delivery_id, where, TOTAL_MISMATCH,
                f"delivery {receivable.delivery_id} owes {receivable.total_kobo} kobo "
                f"according to the receivable and its lines add up to {total} kobo"))
            continue
        values = {
            "tenant_id": plan.tenant_id, "delivery_id": receivable.delivery_id,
            "shop_id": receivable.shop_id,
            "due_day": receivable.due_on.toordinal(),
            "due_on": receivable.due_on.isoformat(),
            "total_kobo": receivable.total_kobo, "paid_kobo": receivable.paid_kobo,
            # Outstanding is computed here, never read from the file.
            "outstanding_kobo": receivable.total_kobo - receivable.paid_kobo,
            "status": _receivable_status(receivable, plan),
            # The book's own order, oldest due first, so every list reads the
            # same way; the ordinal of the due date is that order and survives a
            # re-import.
            "position": receivable.due_on.toordinal(),
        }
        row = book.receivables.get(receivable.delivery_id)
        if row is None:
            plan.creates["receivables"].append(values)
        elif _same(row, values, ignore=("paid_kobo", "outstanding_kobo", "status")):
            # What has been paid is the book's own record (a payment that landed
            # after this file was exported), so a re-import leaves it alone.
            plan.unchanged["receivables"] += 1
        else:
            plan.rejects.append(_dup("receivables", receivable.delivery_id, where,
                                     "receivable"))


def _delivery_totals(plan: Plan, reading: Reading,
                     book: _Book) -> dict[str, tuple[str, int]]:
    """(shop_id, total_kobo) for every delivery this import can see."""
    out = {d: (row.shop_id, int(row.total_kobo)) for d, row in book.deliveries.items()}
    for row in plan.creates["deliveries"]:
        out[row["delivery_id"]] = (row["shop_id"], int(row["total_kobo"]))
    return out


def _receivable_status(receivable: Any, plan: Plan) -> str:
    """Settled, overdue or open, as of the book's day. Computed, not supplied."""
    if receivable.total_kobo - receivable.paid_kobo <= 0:
        return "settled"
    return "overdue" if receivable.due_on.isoformat() < plan.live_date else "open"


def _plan_payments(plan: Plan, reading: Reading, book: _Book) -> None:
    shops = set(book.shops) | {row["shop_id"] for row in plan.creates["shops"]}
    deliveries = _delivery_totals(plan, reading, book)
    for payment in reading.payments:
        where = payment.provenance.where
        if payment.shop_id not in shops:
            plan.rejects.append(Reject(
                "payments", payment.payment_id, where, UNKNOWN_SHOP,
                f"payment {payment.payment_id} is from shop {payment.shop_id!r}, which "
                "this book does not have"))
            continue
        if payment.delivery_id:
            known = deliveries.get(payment.delivery_id)
            if known is None:
                plan.rejects.append(Reject(
                    "payments", payment.payment_id, where, UNKNOWN_DELIVERY,
                    f"payment {payment.payment_id} names delivery "
                    f"{payment.delivery_id!r}, which this book does not have"))
                continue
            if known[0] != payment.shop_id:
                plan.rejects.append(Reject(
                    "payments", payment.payment_id, where, MIXES_TWO_SHOPS,
                    f"payment {payment.payment_id} is from shop {payment.shop_id!r} and "
                    f"settles delivery {payment.delivery_id} of shop {known[0]!r}"))
                continue
        values = {
            "tenant_id": plan.tenant_id, "payment_id": payment.payment_id,
            "shop_id": payment.shop_id, "delivery_id": payment.delivery_id,
            "day": payment.paid_on.toordinal(), "paid_on": payment.paid_on.isoformat(),
            "amount_kobo": payment.amount_kobo, "method": payment.method,
            "reference": payment.reference, "status": "reported",
            "partial": False, "split_of": None, "fake_alert": False,
            **_payment_proof(payment),
        }
        row = book.payments.get(payment.payment_id)
        if row is None:
            plan.creates["payments"].append(values)
        elif _same(row, values, ignore=_OWNS_ITSELF):
            plan.unchanged["payments"] += 1
        else:
            plan.rejects.append(_dup("payments", payment.payment_id, where, "payment"))


def _payment_proof(payment: PaymentRow) -> dict[str, str]:
    """The distributor's own record of a payment is one party's word, so the
    level is whatever ``mimi.provenance`` says one witness earns: estimated,
    until the bank feed or the shop confirms it."""
    return stamp(payment_type(payment.method),
                 [witness("distributor_record", payment.payment_id)]).columns()


def _live_date(plan: Plan, book: _Book, as_of: str | None) -> str:
    """The book's day: the day overdue is counted from.

    ``as_of`` wins when the operator knows it (a book exported on a Sunday still
    describes Friday). Otherwise it is the last thing that happened in the book:
    the newest delivery or payment, the file's or the book's.
    """
    if as_of:
        try:
            return date.fromisoformat(as_of).isoformat()
        except ValueError:
            raise ConnectorError(f"{as_of!r} is not a date the book can use.",
                                 next_step="Pass --as-of as 2026-10-26.") from None
    days = [book.last_day] if book.last_day else []
    days += [str(row["delivered_on"]) for row in plan.creates["deliveries"]]
    days += [str(row["paid_on"]) for row in plan.creates["payments"]]
    return max(days) if days else now_lagos().date().isoformat()


# ------------------------------------------------------------------ the writes


def write_plan(engine: Engine, plan: Plan) -> None:
    """Write the plan: the tenant, its rows, their provenance, and the ledger.

    One transaction. A failure halfway leaves the book as it was, which is the
    only behaviour an owner can reason about: an import either happened or it
    did not.
    """
    run_id = "imp_" + hashlib.sha256(
        f"{plan.tenant_id}|{plan.source}|{plan.digest}|{plan.at}".encode()).hexdigest()[:20]
    with engine.begin() as conn:
        _write_tenant(conn, plan)
        _refresh_overdue(conn, plan)
        for table in WRITE_ORDER:
            if plan.creates[table]:
                conn.execute(STORE_TABLES[table].insert(), plan.creates[table])
        _write_provenance(conn, plan, run_id)
        conn.execute(sa.insert(s.connector_sync_runs).values(
            tenant_id=plan.tenant_id, run_id=run_id,
            # No connector account is behind a file: the label names the file and
            # its content, which is what makes this run findable again.
            connection_id=f"{plan.source}:{plan.label}#{plan.digest[:12]}",
            shop_id="", scope="book",
            rows_read=plan.rows_written() + len(plan.rejects),
            rows_mapped=plan.rows_written(),
            errors=dumps([f"{r.where}: {r.detail}" for r in plan.rejects]),
            started_at=plan.at, finished_at=plan.at))


def _write_tenant(conn: sa.Connection, plan: Plan) -> None:
    if plan.tenant is None:
        return
    if plan.tenant_new:
        conn.execute(s.tenants.insert().values(**plan.tenant))
        # The person who signs in as this distributor. Written by the import so a
        # tenant made from a file is a tenant someone can actually use.
        from mimi.identity import store as ident

        ident.ensure_owner(conn, plan.tenant_id, phone=loads(
            plan.tenant["owner_phones"], [])[0], name=str(plan.tenant["name"]))
        return
    conn.execute(s.tenants.update().where(
        s.tenants.c.tenant_id == plan.tenant_id).values(**plan.tenant))


def _refresh_overdue(conn: sa.Connection, plan: Plan) -> None:
    """Restate which open invoices are late, after the book's day moved.

    ``status`` is overdue or open, which is the due date compared with the book's
    day: a fact about the book, not about the file. It is stored because the
    helpers read it as a word ("overdue"), so it has to be true after an import
    that moved the day. Outstanding and paid are untouched: money only moves
    through payments.
    """
    if not plan.day_moved:
        return
    open_rows = sa.and_(s.receivables.c.tenant_id == plan.tenant_id,
                        s.receivables.c.outstanding_kobo > 0)
    conn.execute(s.receivables.update().where(
        open_rows, s.receivables.c.due_on < plan.live_date,
        s.receivables.c.status != "overdue").values(status="overdue"))
    conn.execute(s.receivables.update().where(
        open_rows, s.receivables.c.due_on >= plan.live_date,
        s.receivables.c.status != "open").values(status="open"))


def _write_provenance(conn: sa.Connection, plan: Plan, run_id: str) -> None:
    """One append-only event per imported row: which run, which record, when.

    Deliveries and payments carry a witness of their own (in their proof
    columns, written above); this is the line the other tables do not have room
    for, and it is the whole provenance record for shops, reps and SKUs.
    """
    for table in WRITE_ORDER:
        for row in plan.creates[table]:
            record_id = _record_id(table, row)
            level = row.get("proof_level", "estimated")
            event = ProofEvent(
                record_type=RECORD_TYPE[table], record_id=record_id, old_level="none",
                new_level=level, witness_kind="distributor_record",  # type: ignore[arg-type]
                witness_source=run_id, policy_version=POLICY_VERSION, at=plan.at)
            conn.execute(s.proof_events.insert().values(
                tenant_id=plan.tenant_id, event_id=event_id(event), **event.as_row()))


def _record_id(table: str, row: _Row) -> str:
    """The distributor's own id for the row: the key a figure is traced by."""
    if table == "delivery_lines":
        return f"{row['delivery_id']}:{row['line_no']}"
    if table == "receivables":
        return str(row["delivery_id"])
    if table == "skus":
        return str(row["sku"])
    for key in ("delivery_id", "payment_id", "shop_id", "rep_id"):
        if key in row:
            return str(row[key])
    raise KeyError(f"no external id on a {table} row")


# ------------------------------------------------------------------- helpers


def _dup(entity: str, external_id: str, where: str, what: str) -> Reject:
    return Reject(
        entity, external_id, where, DUP_IN_BOOK,
        f"{what} {external_id} is already in the book with different content. An "
        "import adds rows and never overwrites one, so this row is refused: correct "
        "the book's own record, or import under the id the book already knows.")


def _same(row: Any, values: _Row, *, json_fields: tuple[str, ...] = (),
          ignore: tuple[str, ...] = ()) -> bool:
    """Whether the book already holds exactly this row."""
    for key, want in values.items():
        if key == "tenant_id" or key in ignore:
            continue
        have = getattr(row, key)
        if key in json_fields:
            have = loads(have, [])
            want = loads(want, [])
        if have != want:
            return False
    return True


def _same_lines(have: dict[int, Any], wanted: list[_Row]) -> bool:
    if len(have) != len(wanted):
        return False
    for values in wanted:
        row = have.get(int(values["line_no"]))
        if row is None or not _same(row, values, ignore=("name", "unit")):
            return False
    return True


__all__ = ["Plan", "Report", "build_plan", "import_book", "write_plan"]

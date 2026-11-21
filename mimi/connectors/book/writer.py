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
from datetime import date, datetime
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
    ProofStamp,
    add_witness,
    delivery_stamp,
    parse_witnesses,
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
    "payments": s.payments, "returns": s.returns, "return_lines": s.return_lines,
}
WRITE_ORDER: tuple[str, ...] = ("reps", "shops", "skus", "deliveries", "delivery_lines",
                               "receivables", "payments", "returns", "return_lines")

#: The two tables whose rows the proof policy reasons about, and so the only two
#: that write a provenance event here. A delivery's type is ``delivery``; a
#: payment's is its own (``payment_transfer``, ``payment_cash``, ``payment_pos``),
#: because that distinction decides which witnesses can lift a figure at all.
#: Shops, reps, SKUs, lines and receivables are reference rows the policy has no
#: type for: their provenance is the import run in ``connector_sync_runs``, which
#: names the file, its digest and the moment it was read.
PROVENANCED = ("deliveries", "returns", "payments")

#: A store row as it will be written, and the proof level it earns.
_Row = dict[str, Any]

#: Columns the book changes on its own after a row lands: a receipt confirms a
#: delivery, a bank credit lifts a payment's proof level, a dispute changes a
#: status. A re-import must not read those as "the file disagrees".
_OWNS_ITSELF: tuple[str, ...] = ("status", "witnessed_at", "live", "fake_alert", "partial",
                                "split_of", "proof_level", "witnesses",
                                "proof_policy_version")

#: A return's status is the book's own word and part of its content (a book that
#: advances a return is a different row, refused like any other change). The
#: proof columns and the row's own stamps are the writer's, not the file's, so a
#: re-import of the same file is unchanged rather than a disagreement.
_RETURN_OWNED: tuple[str, ...] = ("proof_level", "witnesses", "proof_policy_version",
                                  "created_at", "created_ts", "updated_at")


@dataclass
class Plan:
    """What an import would do, worked out without writing anything."""

    tenant_id: str
    source: str
    label: str
    digest: str
    at: str
    live_date: str
    rows_read: int = 0
    tenant: _Row | None = None
    tenant_new: bool = False
    creates: dict[str, list[_Row]] = field(default_factory=lambda: {k: [] for k in STORE_TABLES})
    unchanged: dict[str, int] = field(default_factory=lambda: {k: 0 for k in STORE_TABLES})
    rejects: list[Reject] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: (row, as written, as stored) for every phone the import normalised.
    phones: list[tuple[str, str, str]] = field(default_factory=list)

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
                digest=reading.digest, at=at, live_date="", rows_read=reading.rows_read,
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
    _plan_returns(plan, reading, book)
    # The book's day has to be known before receivables are planned: whether an
    # invoice is overdue is that day compared with its due date.
    plan.live_date = _live_date(plan, book, as_of)
    _plan_receivables(plan, reading, book)
    _plan_tenant(plan, reading, book)
    _describe_phones(plan)
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
    returns: dict[str, Any] = field(default_factory=dict)
    return_lines: dict[str, dict[int, Any]] = field(default_factory=dict)
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
                                 ("payments", s.payments, "payment_id"),
                                 ("returns", s.returns, "return_id")):
            rows = conn.execute(sa.select(table).where(table.c.tenant_id == tenant_id)).all()
            setattr(book, name, {getattr(row, key): row for row in rows})
        for line in conn.execute(sa.select(s.delivery_lines).where(
                s.delivery_lines.c.tenant_id == tenant_id)):
            book.lines.setdefault(line.delivery_id, {})[int(line.line_no)] = line
        for line in conn.execute(sa.select(s.return_lines).where(
                s.return_lines.c.tenant_id == tenant_id)):
            book.return_lines.setdefault(line.return_id, {})[int(line.line_no)] = line
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
                for raw, canonical in zip(distributor.raw_owner_phones,
                                          distributor.owner_phones, strict=True):
                    _note_phone(plan, plan.tenant_id, raw, canonical)
        if changes:
            plan.tenant = {"tenant_id": plan.tenant_id, **changes}
            plan.notes.append("the distributor's own row changed: "
                              + ", ".join(sorted(changes)))
        return
    assert distributor is not None  # build_plan refused a book that names none
    plan.tenant_new = True
    for raw, canonical in zip(distributor.raw_owner_phones, distributor.owner_phones,
                              strict=True):
        _note_phone(plan, plan.tenant_id, raw, canonical)
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


def _note_phone(plan: Plan, row_id: str, raw: str, canonical: str) -> None:
    """Only for a row this import writes: a re-import changes no phone, so it
    says nothing about one."""
    if raw != canonical:
        plan.phones.append((row_id, raw, canonical))


def _describe_phones(plan: Plan) -> None:
    """Say in the report which numbers were rewritten, before anyone wonders.

    One line, the count and a few examples: the file still holds what the
    distributor typed, and the second import of it reports nothing because the
    store already has the canonical form.
    """
    if not plan.phones:
        return
    count = len(plan.phones)
    examples = ", ".join(f"{row} {raw!r} as {canonical}"
                         for row, raw, canonical in plan.phones[:3])
    more = "" if count <= 3 else f" and {count - 3} more"
    plan.notes.append(
        f"{count} {'phone number' if count == 1 else 'phone numbers'} stored in E.164 "
        f"(the form the channel sends): {examples}{more}")


def _plan_reps(plan: Plan, reading: Reading, book: _Book) -> None:
    for rep in reading.reps:
        values = {"tenant_id": plan.tenant_id, "rep_id": rep.rep_id, "name": rep.name,
                  "phone": rep.phone, "areas": dumps(list(rep.areas))}
        row = book.reps.get(rep.rep_id)
        if row is None:
            plan.creates["reps"].append(values)
            _note_phone(plan, rep.rep_id, rep.raw_phone, rep.phone)
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
            _note_phone(plan, shop.shop_id, shop.raw_phone, shop.phone)
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


def _plan_returns(plan: Plan, reading: Reading, book: _Book) -> None:
    """Imported returns: the shop, the delivery and the SKUs checked, the value summed.

    A return names the shop it came from, the rep carrying it back and the
    delivery the goods came in on. A row that points at something the book does
    not have is refused by name, never guessed at, exactly as a delivery is.
    """
    shops = set(book.shops) | {row["shop_id"] for row in plan.creates["shops"]}
    reps = set(book.reps) | {row["rep_id"] for row in plan.creates["reps"]}
    skus = {sku: (row.name, row.unit) for sku, row in book.skus.items()}
    skus.update({row["sku"]: (row["name"], row["unit"]) for row in plan.creates["skus"]})
    deliveries = _delivery_totals(plan, reading, book)
    for one in reading.returns:
        where = one.provenance.where
        if one.shop_id not in shops:
            plan.rejects.append(Reject(
                "returns", one.return_id, where, UNKNOWN_SHOP,
                f"return {one.return_id} is from shop {one.shop_id!r}, which this book "
                "does not have"))
            continue
        if one.rep_id and one.rep_id not in reps:
            plan.rejects.append(Reject(
                "returns", one.return_id, where, UNKNOWN_REP,
                f"return {one.return_id} is carried by rep {one.rep_id!r}, whom the "
                "book does not have"))
            continue
        if one.delivery_id:
            known = deliveries.get(one.delivery_id)
            if known is None:
                plan.rejects.append(Reject(
                    "returns", one.return_id, where, UNKNOWN_DELIVERY,
                    f"return {one.return_id} names delivery {one.delivery_id!r}, which "
                    "this book does not have"))
                continue
            if known[0] != one.shop_id:
                plan.rejects.append(Reject(
                    "returns", one.return_id, where, MIXES_TWO_SHOPS,
                    f"return {one.return_id} is from shop {one.shop_id!r} and takes goods "
                    f"off delivery {one.delivery_id} of shop {known[0]!r}"))
                continue
        unknown = [line.sku for line in one.lines if line.sku not in skus]
        if unknown:
            plan.rejects.append(Reject(
                "returns", one.return_id, where, UNKNOWN_SKU,
                f"return {one.return_id} carries {', '.join(sorted(set(unknown)))}, which "
                "the book does not price"))
            continue
        counted = one.status in ("received", "credited")
        values = {
            "tenant_id": plan.tenant_id, "return_id": one.return_id,
            "shop_id": one.shop_id, "source": "imported", "approval_id": None,
            "delivery_id": one.delivery_id, "rep_id": one.rep_id,
            "raised_on": one.raised_on.isoformat(),
            "raised_day": one.raised_on.toordinal(),
            "reason": one.reason, "status": one.status,
            # The raised value is the sum of the lines, and only a return the book
            # says came back counts the same figure as returned.
            "value_drafted_kobo": one.value_kobo,
            "value_collected_kobo": 0,
            "value_returned_kobo": one.value_kobo if counted else 0,
            "recovered_kobo": 0, "pickup_cost_kobo": None, "saving_kobo": None,
            "credit_kobo": None, "credit_approval_id": None,
            "created_at": plan.at,
            "created_ts": datetime.fromisoformat(plan.at).timestamp(),
            "updated_at": plan.at,
            **_return_proof(one.return_id),
        }
        line_values = [_return_line_values(plan, one.return_id, line, skus[line.sku],
                                           counted=counted) for line in one.lines]
        row = book.returns.get(one.return_id)
        if row is None:
            plan.creates["returns"].append(values)
            plan.creates["return_lines"].extend(line_values)
        elif _same(row, values, ignore=_RETURN_OWNED) and _same_lines(
                book.return_lines.get(one.return_id, {}), line_values):
            plan.unchanged["returns"] += 1
            plan.unchanged["return_lines"] += len(line_values)
        else:
            plan.rejects.append(_dup("returns", one.return_id, where, "return"))


def _return_line_values(plan: Plan, return_id: str, line: Any, sku: tuple[str, str],
                        *, counted: bool) -> _Row:
    """One return line as the store holds it: the product's own name and unit,
    the value computed from quantity and price."""
    name, unit = sku
    value = line.quantity * line.unit_price_kobo
    return {
        "tenant_id": plan.tenant_id, "return_id": return_id, "line_no": line.line_no,
        "sku": line.sku, "item": name, "unit": unit, "reason": line.reason,
        "quantity_drafted": line.quantity,
        "quantity_collected": line.quantity if counted else None,
        "quantity_returned": line.quantity if counted else None,
        "unit_price_kobo": line.unit_price_kobo,
        "value_drafted_kobo": value,
        "value_returned_kobo": value if counted else None,
        "condition": "", "disposition": "", "expires_on": None, "batch": "",
        "expiry_source": "estimated",
    }


def _return_proof(return_id: str) -> dict[str, str]:
    """A return is the distributor's own record: one party's word, so estimated
    (``mimi.provenance``, policy v2)."""
    return stamp("return", [witness("distributor_record", return_id)]).columns()


def _payment_proof(payment: PaymentRow) -> dict[str, str]:
    """The distributor's own record of a payment is one party's word, so the
    level is whatever ``mimi.provenance`` says one witness earns: estimated,
    until the bank feed or the shop confirms it."""
    return stamp(payment_type(payment.method),
                 [witness("distributor_record", payment.payment_id)]).columns()


def _live_date(plan: Plan, book: _Book, as_of: str | None) -> str:
    """The book's day: the day overdue is counted from.

    ``as_of`` wins when the operator knows it (a book exported on a Sunday still
    describes Friday), but never an earlier day than the book is already on: that
    half applies, and half applying a day is how a status and a sum come to
    disagree. Otherwise the day is the last thing that happened in the book: the
    newest delivery or payment, the file's or the book's.
    """
    days = [book.last_day] if book.last_day else []
    days += [str(row["delivered_on"]) for row in plan.creates["deliveries"]]
    days += [str(row["paid_on"]) for row in plan.creates["payments"]]
    own = max(days) if days else ""
    if as_of:
        try:
            wanted = date.fromisoformat(as_of).isoformat()
        except ValueError:
            raise ConnectorError(f"{as_of!r} is not a date the book can use.",
                                 next_step="Pass --as-of as 2026-10-26.") from None
        if own and wanted < own:
            # Half applying it is worse than refusing it: the invoices would be
            # stored with a status computed for one day while overdue is counted
            # from another, and the word and the sum would disagree.
            raise ConnectorError(
                f"The book's day is already {own} and --as-of {as_of} is earlier.",
                next_step=f"Pass --as-of on or after {own}, or leave it out: the book "
                          "day follows the newest delivery or payment in it.")
        return wanted
    return own or now_lagos().date().isoformat()


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
        for table in WRITE_ORDER:
            if plan.creates[table]:
                conn.execute(STORE_TABLES[table].insert(), plan.creates[table])
        # After the rows, because the day and the words it decides are read from
        # the book as it now is, not as it was when the plan was made.
        _refresh_overdue(conn, plan, _apply_day(conn, plan))
        _write_provenance(conn, plan, run_id)
        conn.execute(sa.insert(s.connector_sync_runs).values(
            tenant_id=plan.tenant_id, run_id=run_id,
            # No connector account is behind a file: the label names the file and
            # its content, which is what makes this run findable again.
            connection_id=f"{plan.source}:{plan.label}#{plan.digest[:12]}",
            shop_id="", scope="book",
            rows_read=plan.rows_read,
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


def _apply_day(conn: sa.Connection, plan: Plan) -> str:
    """Move the tenant's day forward if this book is newer; never backward.

    The comparison is in the statement, not in Python, so two imports racing on
    one tenant cannot lose the later day: the older plan's update matches no row
    and the stored day stays where the newer book put it.
    """
    def stored() -> str:
        raw = conn.execute(sa.select(s.tenants.c.live_date).where(
            s.tenants.c.tenant_id == plan.tenant_id)).scalar()
        return str(raw or plan.live_date)

    if plan.live_date > stored():
        conn.execute(s.tenants.update().where(
            s.tenants.c.tenant_id == plan.tenant_id,
            s.tenants.c.live_date < plan.live_date).values(
            live_date=plan.live_date,
            live_day=date.fromisoformat(plan.live_date).toordinal()))
    return stored()


def _refresh_overdue(conn: sa.Connection, plan: Plan, day: str) -> None:
    """Restate which open invoices are late, as of the day the book is now on.

    ``status`` is overdue or open, the due date compared with the book's day: a
    fact about the book, not about the file. The helpers read it as a word
    ("overdue"), so it has to be true after any import that could have moved the
    day or added invoices. It is written from the day in effect rather than the
    planned one for the same reason. Outstanding and paid are untouched: money
    only moves through payments.
    """
    open_rows = sa.and_(s.receivables.c.tenant_id == plan.tenant_id,
                        s.receivables.c.outstanding_kobo > 0)
    conn.execute(s.receivables.update().where(
        open_rows, s.receivables.c.due_on < day,
        s.receivables.c.status != "overdue").values(status="overdue"))
    conn.execute(s.receivables.update().where(
        open_rows, s.receivables.c.due_on >= day,
        s.receivables.c.status != "open").values(status="open"))


def _write_provenance(conn: sa.Connection, plan: Plan, run_id: str) -> None:
    """One append-only event per imported delivery and payment: which run, which
    record, when.

    The event is built by ``mimi.provenance.add_witness``, the same function that
    stamps a record when a witness lands, so the record type is checked against
    the policy's closed list and the level is computed by the rules rather than
    asserted here. The witness it records is the import run, which is what makes
    the event traceable back to a file; the record's own witness (the
    distributor's row) is already in its proof columns, written above.
    """
    for table in PROVENANCED:
        for row in plan.creates[table]:
            record_type, record_id = _provenanced_record(table, row)
            current = ProofStamp(record_type, row["proof_level"],  # type: ignore[arg-type]
                                 parse_witnesses(row["witnesses"]),
                                 row["proof_policy_version"])
            _, event = add_witness(current, record_type, record_id,
                                   witness("distributor_record", run_id), plan.at)
            assert event is not None  # the run is a new witness, so one always lands
            conn.execute(s.proof_events.insert().values(
                tenant_id=plan.tenant_id, event_id=event_id(event), **event.as_row()))


def _provenanced_record(table: str, row: _Row) -> tuple[str, str]:
    """The policy's record type and the row's own id for a provenanced table."""
    if table == "deliveries":
        return "delivery", str(row["delivery_id"])
    if table == "returns":
        return "return", str(row["return_id"])
    return payment_type(str(row["method"])), str(row["payment_id"])


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

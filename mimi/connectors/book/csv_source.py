"""A distributor's book from a folder of CSV files or one workbook.

Two file shapes, one parser per table, one set of rules:

* **A folder** with one CSV per table: ``distributor.csv``, ``reps.csv``,
  ``shops.csv``, ``skus.csv``, ``deliveries.csv``, ``delivery_lines.csv``,
  ``receivables.csv``, ``payments.csv``.
* **A workbook** (.xlsx/.xlsm) with one sheet per table under those names.

Both go through the grid readers in ``mimi.connectors.csv``, so a quoted comma, a
byte-order mark or an Excel date cell is understood in one place. Columns are
matched by name, not position: ``Shop ID``, ``shop_id`` and ``SHOP-ID`` are the
same column.

What a file may not do, and gets a reject for instead:

* write money as anything but a whole naira amount or a whole kobo figure
  (``1250.555`` naira is 125055.5 kobo, which is not a figure this book holds);
* write a date this reader cannot read: ISO ``2026-10-26`` or the Nigerian
  day-first ``26/10/2026``, or a real sheet date cell;
* list a delivery with no lines, or a line for a delivery it does not list;
* state a total that disagrees with its own lines (the lines are summed; the
  file's figure is only ever compared against that sum).

A table's *columns* are checked once, before any row. A missing id, date, money
or quantity column refuses the whole file with a plain sentence, because there is
no honest per-row answer to "this file has no price column".
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from mimi.connectors.base import ConnectorError
from mimi.connectors.book.rows import (
    BAD_DATE,
    BAD_METHOD,
    BAD_MONEY,
    BAD_NUMBER,
    DUP_IN_FILE,
    MISSING_FIELD,
    NO_LINES,
    PAYMENT_METHODS,
    TOTAL_MISMATCH,
    UNKNOWN_DELIVERY,
    DeliveryRow,
    DistributorRow,
    LineRow,
    PaymentRow,
    Provenance,
    Reading,
    ReceivableRow,
    Reject,
    RepRow,
    ShopRow,
    SkuRow,
)
from mimi.connectors.csv.connector import EXCEL_EXTENSIONS, csv_grid, excel_sheets
from mimi.connectors.sheets.mapping import MAX_ROWS

#: The tables inside a book, and the file name or sheet name each one lives under.
TABLES: tuple[str, ...] = (
    "distributor", "reps", "shops", "skus",
    "deliveries", "delivery_lines", "receivables", "payments",
)

#: Columns a table must have before any row can be read. Everything else is
#: optional and reads as empty rather than as a guessed value.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "distributor": ("tenant_id", "name", "owner_phones"),
    "reps": ("rep_id", "name", "phone"),
    "shops": ("shop_id", "phone"),
    "skus": ("sku", "name", "price_naira"),
    "deliveries": ("delivery_id", "shop_id", "delivered_on"),
    "delivery_lines": ("delivery_id", "sku", "quantity", "unit_price_naira"),
    "receivables": ("delivery_id", "shop_id", "due_on", "total_naira", "paid_naira"),
    "payments": ("payment_id", "shop_id", "paid_on", "amount_naira", "method"),
}

#: ``delivery_lines.line_no`` is optional: a file that does not number its lines
#: gets them numbered in the order it lists them.
_DEFAULT_RESTOCK_DAYS = 7  # the store's own default for shops.restock_days

#: ``prov(table, external_id, row_no)``: where a row came from, as it parses.
_Where = Callable[[str, str, int], Provenance]


@dataclass
class _Raw:
    """One table as read: rows by their file line number, columns by name."""

    columns: dict[str, int]
    rows: list[tuple[int, list[Any]]]
    name: str


class _Bad(Exception):
    """A row that cannot be read. Carries the reject reason and its sentence."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def read_book(path: str | Path, *, tenant_id: str, read_at: str) -> Reading:
    """Read a book at ``path`` (a folder of CSVs or a workbook) as canonical rows.

    Raises ``ConnectorError`` when the file cannot be a book at all: not found,
    one lone CSV, a table missing, a table without its key columns, a distributor
    row that names another tenant.
    """
    where = Path(path)
    if where.is_dir():
        return _parse("csv", label=where.name, grids=_tables_from_folder(where),
                      names={table: f"{table}.csv" for table in TABLES},
                      digest=_folder_digest(where), tenant_id=tenant_id, read_at=read_at)
    if where.suffix.lower() in EXCEL_EXTENSIONS:
        return _parse("xlsx", label=where.name, grids=_tables_from_workbook(where),
                      names={table: f"sheet {table}" for table in TABLES},
                      digest=hashlib.sha256(where.read_bytes()).hexdigest(),
                      tenant_id=tenant_id, read_at=read_at)
    if not where.exists():
        raise ConnectorError(f"There is no file or folder at {where}.",
                             next_step="Point the import at a book folder or .xlsx file.")
    raise ConnectorError(
        f"A book is not one CSV file: {where.name} holds one table.",
        next_step="Send a folder with one CSV per table, or one .xlsx workbook with "
                  "one sheet per table.")


# ------------------------------------------------------------------ the shapes


def _tables_from_folder(folder: Path) -> dict[str, list[list[Any]]]:
    """One CSV file per table. A missing table is refused by name."""
    grids: dict[str, list[list[Any]]] = {}
    missing: list[str] = []
    for table in TABLES:
        path = folder / f"{table}.csv"
        if not path.is_file():
            missing.append(path.name)
            continue
        try:
            grids[table] = csv_grid(path.read_text(encoding="utf-8-sig"))
        except ConnectorError as exc:
            raise ConnectorError(f"{path.name}: {exc}", next_step=exc.next_step) from exc
    if missing:
        raise ConnectorError(
            f"The folder {folder.name} has no {', '.join(missing)}.",
            next_step="A book folder holds one file per table, named after the table.")
    return grids


def _folder_digest(folder: Path) -> str:
    """One SHA-256 over the folder's tables by name and content, so the same
    book read twice is one digest however the folder was copied."""
    digest = hashlib.sha256()
    for table in TABLES:
        digest.update(table.encode())
        digest.update((folder / f"{table}.csv").read_bytes())
    return digest.hexdigest()


def _tables_from_workbook(path: Path) -> dict[str, list[list[Any]]]:
    """One sheet per table, matched by name however it is spelled."""
    sheets = excel_sheets(path.read_bytes(), path.name)
    by_name = {_key(name): grid for name, grid in sheets.items()}
    missing = [table for table in TABLES if table not in by_name]
    if missing:
        raise ConnectorError(
            f"{path.name} has no sheet named {', '.join(missing)}.",
            next_step="A book workbook holds one sheet per table, named after the table.")
    return {table: by_name[table] for table in TABLES}


# ------------------------------------------------------------------- the parse


def _parse(source: str, label: str, grids: dict[str, list[list[Any]]],
           names: dict[str, str], *, digest: str, tenant_id: str,
           read_at: str) -> Reading:
    def prov(table: str, external_id: str, row_no: int) -> Provenance:
        return Provenance(source=source, external_id=external_id, read_at=read_at,
                          where=f"{names[table]} row {row_no}")

    rejects: list[Reject] = []
    tables = {name: _table(grids[name], name, names[name]) for name in TABLES}
    return Reading(
        source=source,
        label=label,
        digest=digest,
        distributor=_distributor(tables["distributor"], prov, rejects, tenant_id),
        reps=_unique(_reps(tables["reps"], prov, rejects), "reps", "rep_id", rejects),
        shops=_unique(_shops(tables["shops"], prov, rejects), "shops", "shop_id", rejects),
        skus=_unique(_skus(tables["skus"], prov, rejects), "skus", "sku", rejects),
        deliveries=_deliveries(tables["deliveries"], tables["delivery_lines"], prov, rejects),
        receivables=_receivables(tables["receivables"], prov, rejects),
        payments=_payments(tables["payments"], prov, rejects),
        rejects=tuple(rejects))


def _unique(rows: list[Any], table: str, key: str, rejects: list[Reject]) -> tuple[Any, ...]:
    """One row per id, and the copies reported.

    An identical repeat is harmless: the first copy is the row. A repeat that
    *disagrees* is refused entirely, first copy included: the file contradicts
    itself, and picking one would be a guess about which of the two is the
    distributor's book.
    """
    kept: dict[str, Any] = {}
    poisoned: set[str] = set()
    out: list[Any] = []
    for row in rows:
        external_id = getattr(row, key)
        first = kept.get(external_id)
        if first is None:
            kept[external_id] = row
            out.append(row)
            continue
        same = _signature(first) == _signature(row)
        if not same and external_id not in poisoned:
            poisoned.add(external_id)
            out = [r for r in out if getattr(r, key) != external_id]
            rejects.append(Reject(
                table, external_id, first.provenance.where, DUP_IN_FILE,
                f"{key} {external_id} is listed twice in this file with different "
                "content, so no copy of it is imported"))
        rejects.append(Reject(
            table, external_id, row.provenance.where, DUP_IN_FILE,
            f"{key} {external_id} is listed twice in this file: "
            + ("the two rows are identical, so the first is used" if same
               else "the two rows disagree, so no copy of it is imported")))
    return tuple(out)


def _signature(row: Any) -> tuple[Any, ...]:
    """A row's content without where it was read from: what "the same row" means."""
    return tuple(v for k, v in vars(row).items() if k != "provenance")


def _distributor(table: _Raw, prov: _Where, rejects: list[Reject],
                 tenant_id: str) -> DistributorRow | None:
    if len(table.rows) != 1:
        raise ConnectorError(f"{table.name} holds {len(table.rows)} rows.",
                             next_step="A book names one distributor: one row with "
                                       "tenant_id, name and owner_phones.")
    row_no, row = table.rows[0]
    tenant = _text(row, table.columns, "tenant_id")
    place = prov("distributor", tenant, row_no)
    name = _text(row, table.columns, "name")
    phones = _phones(_text(row, table.columns, "owner_phones"))
    if not tenant:
        rejects.append(Reject("distributor", "", place.where, MISSING_FIELD,
                              "the distributor row has no tenant_id"))
        return None
    if not name:
        rejects.append(Reject("distributor", tenant, place.where, MISSING_FIELD,
                              "the distributor row has no name"))
        return None
    if not phones:
        rejects.append(Reject("distributor", tenant, place.where, MISSING_FIELD,
                              "the distributor row has no owner_phones: an owner is "
                              "reached by phone, so there is nobody to sign in"))
        return None
    if tenant != tenant_id:
        raise ConnectorError(f"That book belongs to {tenant!r}, not to {tenant_id!r}.",
                             next_step="Import it into its own tenant, or fix the "
                                       "tenant_id column in the book.")
    return DistributorRow(tenant_id=tenant, name=name, owner_phones=phones, provenance=place)


def _reps(table: _Raw, prov: _Where, rejects: list[Reject]) -> list[RepRow]:
    out: list[RepRow] = []
    for row_no, row in table.rows:
        rep_id = _text(row, table.columns, "rep_id")
        place = prov("reps", rep_id, row_no)
        try:
            if not rep_id:
                raise _Bad(MISSING_FIELD, "a rep row has no rep_id")
            name = _text(row, table.columns, "name")
            phone = _text(row, table.columns, "phone")
            if not name:
                raise _Bad(MISSING_FIELD, f"rep {rep_id} has no name")
            if not phone:
                raise _Bad(MISSING_FIELD, f"rep {rep_id} has no phone: a rep is a "
                                          "person Mimi has to be able to reach")
            out.append(RepRow(rep_id=rep_id, name=name, phone=phone,
                              areas=_list(_text(row, table.columns, "areas")),
                              provenance=place))
        except _Bad as bad:
            rejects.append(Reject("reps", rep_id, place.where, bad.reason, bad.detail))
    return out


def _shops(table: _Raw, prov: _Where, rejects: list[Reject]) -> list[ShopRow]:
    out: list[ShopRow] = []
    for row_no, row in table.rows:
        shop_id = _text(row, table.columns, "shop_id")
        place = prov("shops", shop_id, row_no)
        try:
            if not shop_id:
                raise _Bad(MISSING_FIELD, "a shop row has no shop_id")
            phone = _text(row, table.columns, "phone")
            if not phone:
                raise _Bad(MISSING_FIELD, f"shop {shop_id} has no phone: the shop's own "
                                          "number is the whole channel to it")
            name = _text(row, table.columns, "name")
            owner = _text(row, table.columns, "owner")
            if not name and not owner:
                raise _Bad(MISSING_FIELD, f"shop {shop_id} has no name and no owner")
            out.append(ShopRow(
                shop_id=shop_id, name=name, owner=owner,
                area=_text(row, table.columns, "area"),
                street=_text(row, table.columns, "street"),
                phone=phone, rep_id=_text(row, table.columns, "rep_id"),
                credit_limit_kobo=_money(row, table.columns, "credit_limit_naira", 0),
                restock_days=_whole(row, table.columns, "restock_days",
                                    _DEFAULT_RESTOCK_DAYS),
                provenance=place))
        except _Bad as bad:
            rejects.append(Reject("shops", shop_id, place.where, bad.reason, bad.detail))
    return out


def _skus(table: _Raw, prov: _Where, rejects: list[Reject]) -> list[SkuRow]:
    out: list[SkuRow] = []
    for row_no, row in table.rows:
        sku = _text(row, table.columns, "sku")
        place = prov("skus", sku, row_no)
        try:
            if not sku:
                raise _Bad(MISSING_FIELD, "a product row has no sku")
            name = _text(row, table.columns, "name")
            if not name:
                raise _Bad(MISSING_FIELD, f"sku {sku} has no name")
            price = _money(row, table.columns, "price_naira")
            if price <= 0:
                raise _Bad(MISSING_FIELD, f"sku {sku} has no price: a price of zero is "
                                          "a missing price, not a free product")
            out.append(SkuRow(sku=sku, name=name,
                              brand=_text(row, table.columns, "brand"),
                              unit=_text(row, table.columns, "unit"),
                              price_kobo=price, provenance=place))
        except _Bad as bad:
            rejects.append(Reject("skus", sku, place.where, bad.reason, bad.detail))
    return out


def _deliveries(table: _Raw, lines: _Raw, prov: _Where,
                rejects: list[Reject]) -> tuple[DeliveryRow, ...]:
    """Deliveries and their lines, summed here in code.

    A delivery is written whole or not at all: one unreadable line would make the
    total a sum over the lines that happened to read, which is the wrong number
    in the shape of a right one.
    """
    by_delivery: dict[str, list[tuple[int, list[Any]]]] = {}
    for row_no, row in lines.rows:
        by_delivery.setdefault(_text(row, lines.columns, "delivery_id"), []).append(
            (row_no, row))

    out: list[DeliveryRow] = []
    placed: set[str] = set()
    kept: dict[str, DeliveryRow] = {}
    rejected_copies: set[str] = set()
    for row_no, row in table.rows:
        delivery_id = _text(row, table.columns, "delivery_id")
        place = prov("deliveries", delivery_id, row_no)
        try:
            if not delivery_id:
                raise _Bad(MISSING_FIELD, "a delivery row has no delivery_id")
            shop_id = _text(row, table.columns, "shop_id")
            if not shop_id:
                raise _Bad(MISSING_FIELD, f"delivery {delivery_id} names no shop")
            placed.add(delivery_id)
            delivery = _delivery(
                delivery_id, shop_id, place, row, table.columns,
                by_delivery.get(delivery_id, []), lines.columns, prov, rejects)
            first = kept.get(delivery_id)
            if first is None:
                kept[delivery_id] = delivery
                out.append(delivery)
                continue
            same = _signature(first) == _signature(delivery)
            if not same and delivery_id not in rejected_copies:
                rejected_copies.add(delivery_id)
                out = [d for d in out if d.delivery_id != delivery_id]
                rejects.append(Reject(
                    "deliveries", delivery_id, first.provenance.where, DUP_IN_FILE,
                    f"delivery {delivery_id} is listed twice in this file with "
                    "different content, so no copy of it is imported"))
            rejects.append(Reject(
                "deliveries", delivery_id, place.where, DUP_IN_FILE,
                f"delivery {delivery_id} is listed twice in this file: "
                + ("the two rows are identical, so the first is used" if same
                   else "the two rows disagree, so no copy of it is imported")))
        except _Bad as bad:
            rejects.append(Reject("deliveries", delivery_id, place.where, bad.reason,
                                  bad.detail))

    for delivery_id, rows in by_delivery.items():
        if delivery_id in placed:
            continue
        for row_no, _row in rows:
            rejects.append(Reject(
                "delivery_lines", delivery_id,
                prov("delivery_lines", delivery_id, row_no).where, UNKNOWN_DELIVERY,
                f"a line for delivery {delivery_id!r}, which this file does not list"))
    return tuple(out)


def _delivery(delivery_id: str, shop_id: str, place: Provenance, row: list[Any],
              columns: dict[str, int], raw_lines: list[tuple[int, list[Any]]],
              line_columns: dict[str, int], prov: _Where,
              rejects: list[Reject]) -> DeliveryRow:
    if not raw_lines:
        raise _Bad(NO_LINES, f"delivery {delivery_id} lists no lines, so there is "
                             "nothing to add up")
    parsed: list[LineRow] = []
    for order, (row_no, line_row) in enumerate(raw_lines, start=1):
        line = _line(delivery_id, order, line_row, line_columns,
                     prov("delivery_lines", delivery_id, row_no), rejects)
        if line is not None:
            parsed.append(line)
    if len(parsed) != len(raw_lines):
        raise _Bad(NO_LINES, f"delivery {delivery_id} has a line that cannot be read, "
                             "so its total would be wrong")
    total = sum(line.quantity * line.unit_price_kobo for line in parsed)
    stated = _optional_money(row, columns, "total_naira")
    if stated is not None and stated != total:
        raise _Bad(TOTAL_MISMATCH,
                   f"delivery {delivery_id}: the file says {stated} kobo in total and "
                   f"its lines add up to {total} kobo")
    return DeliveryRow(
        delivery_id=delivery_id, shop_id=shop_id,
        rep_id=_text(row, columns, "rep_id"),
        delivered_on=_day(row, columns, "delivered_on", f"delivery {delivery_id}"),
        minute=_whole(row, columns, "minute", 0),
        lines=tuple(parsed), total_kobo=total, provenance=place)


def _line(delivery_id: str, order: int, row: list[Any], columns: dict[str, int],
          place: Provenance, rejects: list[Reject]) -> LineRow | None:
    """One delivery line, or a reject naming the line that could not be read."""
    try:
        sku = _text(row, columns, "sku")
        if not sku:
            raise _Bad(MISSING_FIELD, f"delivery {delivery_id}, line {order}: no sku")
        quantity = _whole(row, columns, "quantity")
        if quantity <= 0:
            raise _Bad(BAD_NUMBER, f"delivery {delivery_id}, line {order}, sku {sku}: "
                                   f"quantity {quantity} is not units delivered")
        price = _money(row, columns, "unit_price_naira")
        if price <= 0:
            raise _Bad(MISSING_FIELD, f"delivery {delivery_id}, line {order}, sku {sku}: "
                                      "the line has no price")
        return LineRow(
            line_no=_whole(row, columns, "line_no", order), sku=sku, quantity=quantity,
            unit_price_kobo=price, batch=_text(row, columns, "batch"),
            expires_on=_day_or_none(row, columns, "expires_on",
                                    f"delivery {delivery_id}, line {order}, sku {sku}"),
            provenance=place)
    except _Bad as bad:
        rejects.append(Reject("delivery_lines", delivery_id, place.where, bad.reason,
                              bad.detail))
        return None


def _receivables(table: _Raw, prov: _Where, rejects: list[Reject]) -> tuple[ReceivableRow, ...]:
    out: list[ReceivableRow] = []
    for row_no, row in table.rows:
        delivery_id = _text(row, table.columns, "delivery_id")
        place = prov("receivables", delivery_id, row_no)
        try:
            shop_id = _text(row, table.columns, "shop_id")
            if not delivery_id:
                raise _Bad(MISSING_FIELD, "a receivable row has no delivery_id")
            if not shop_id:
                raise _Bad(MISSING_FIELD, f"the receivable for {delivery_id} names no shop")
            total = _money(row, table.columns, "total_naira")
            paid = _money(row, table.columns, "paid_naira")
            if total <= 0:
                raise _Bad(MISSING_FIELD, f"the receivable for {delivery_id} has no total")
            if paid > total:
                raise _Bad(BAD_MONEY, f"the receivable for {delivery_id} is paid {paid} "
                                      f"kobo of a {total} kobo total: more than it owes")
            out.append(ReceivableRow(
                delivery_id=delivery_id, shop_id=shop_id,
                due_on=_day(row, table.columns, "due_on",
                            f"the receivable for {delivery_id}"),
                total_kobo=total, paid_kobo=paid, provenance=place))
        except _Bad as bad:
            rejects.append(Reject("receivables", delivery_id, place.where, bad.reason,
                                  bad.detail))
    return tuple(out)


def _payments(table: _Raw, prov: _Where, rejects: list[Reject]) -> tuple[PaymentRow, ...]:
    out: list[PaymentRow] = []
    for row_no, row in table.rows:
        payment_id = _text(row, table.columns, "payment_id")
        place = prov("payments", payment_id, row_no)
        try:
            if not payment_id:
                raise _Bad(MISSING_FIELD, "a payment row has no payment_id")
            shop_id = _text(row, table.columns, "shop_id")
            if not shop_id:
                raise _Bad(MISSING_FIELD, f"payment {payment_id} names no shop")
            method = _method(_text(row, table.columns, "method"))
            amount = _money(row, table.columns, "amount_naira")
            if amount <= 0:
                raise _Bad(MISSING_FIELD, f"payment {payment_id} has no amount")
            out.append(PaymentRow(
                payment_id=payment_id, shop_id=shop_id,
                delivery_id=_text(row, table.columns, "delivery_id"),
                paid_on=_day(row, table.columns, "paid_on", f"payment {payment_id}"),
                amount_kobo=amount, method=method,
                reference=_text(row, table.columns, "reference"), provenance=place))
        except _Bad as bad:
            rejects.append(Reject("payments", payment_id, place.where, bad.reason,
                                  bad.detail))
    return tuple(out)


# ----------------------------------------------------------------- table reads


def _table(grid: list[list[Any]], name: str, where: str) -> _Raw:
    """A grid's header and data rows, with the required columns checked once."""
    header = [_key(cell) for cell in (grid[0] if grid else [])]
    columns = {col: i for i, col in enumerate(header) if col}
    missing = [col for col in REQUIRED_COLUMNS[name] if col not in columns]
    if missing:
        raise ConnectorError(
            f"{where} has no {', '.join(missing)} column.",
            next_step=f"Its header reads: {', '.join(header) or 'nothing'}. Add the "
                      "missing column and import again.")
    rows = [(no, row) for no, row in enumerate(grid[1:], start=2)
            if any(str(cell).strip() for cell in row)]
    if len(rows) > MAX_ROWS:
        raise ConnectorError(f"{where} has more than {MAX_ROWS} rows.",
                             next_step="Split the file and import it in parts.")
    return _Raw(columns=columns, rows=rows, name=where)


def _key(cell: Any) -> str:
    """``Shop ID`` / ``shop-id`` / ``SHOP_ID`` -> ``shop_id``."""
    return re.sub(r"[^a-z0-9]+", "_", str(cell or "").strip().lower()).strip("_")


def _raw_cell(row: list[Any], columns: dict[str, int], name: str) -> Any:
    col = columns.get(name)
    if col is None or col >= len(row):
        return None
    return row[col]


def _text(row: list[Any], columns: dict[str, int], name: str) -> str:
    raw = _raw_cell(row, columns, name)
    return "" if raw is None else str(raw).strip()


def _list(raw: str) -> tuple[str, ...]:
    """A pipe- or semicolon-separated list ("Ikeja|Ojota")."""
    return tuple(part.strip() for part in re.split(r"[|;]", raw) if part.strip())


def _phones(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in re.split(r"[|;,]", raw) if part.strip())


def _money(row: list[Any], columns: dict[str, int], name: str,
           default: int | None = None) -> int:
    """A naira cell as integer kobo: one multiplication, and it has to be exact.

    A figure with more than two decimal places is not a naira amount, and
    rounding it would put a number in the book the file never said, so it is a
    reject that names the amount and the kobo it would have been.
    """
    raw = _raw_cell(row, columns, name)
    if raw is None or str(raw).strip() == "":
        if default is not None:
            return default
        raise _Bad(MISSING_FIELD, f"{name} is empty")
    return _naira_kobo(raw)


def _optional_money(row: list[Any], columns: dict[str, int], name: str) -> int | None:
    """A naira cell that may be absent, as kobo, or None when it is not there."""
    raw = _raw_cell(row, columns, name)
    if raw is None or str(raw).strip() == "":
        return None
    return _naira_kobo(raw)


def _naira_kobo(raw: Any) -> int:
    if isinstance(raw, bool):
        raise _Bad(BAD_MONEY, f"{raw!r} is not an amount")
    if isinstance(raw, int | float):  # an Excel number cell, already a figure
        text = str(raw)
    else:
        text = re.sub(r"[\s,]", "", str(raw))
        text = re.sub(r"^(₦|NGN|N)(?=\d)", "", text, flags=re.IGNORECASE)
    try:
        naira = Decimal(text)
    except InvalidOperation:
        raise _Bad(BAD_MONEY, f"{raw!r} is not an amount") from None
    if not naira.is_finite():
        raise _Bad(BAD_MONEY, f"{raw!r} is not an amount")
    kobo = naira * 100
    if kobo != kobo.to_integral_value():
        raise _Bad(BAD_MONEY, f"{raw!r} naira is {format(kobo.normalize(), 'f')} kobo, "
                              "which is not a whole kobo figure: money is held in "
                              "whole kobo")
    return int(kobo)


def _whole(row: list[Any], columns: dict[str, int], name: str,
           default: int | None = None) -> int:
    raw = _raw_cell(row, columns, name)
    if raw is None or str(raw).strip() == "":
        if default is not None:
            return default
        raise _Bad(MISSING_FIELD, f"{name} is empty")
    if isinstance(raw, bool):
        raise _Bad(BAD_NUMBER, f"{raw!r} is not a whole number")
    try:
        value = Decimal(str(raw).strip().replace(",", ""))
    except InvalidOperation:
        raise _Bad(BAD_NUMBER, f"{raw!r} is not a whole number") from None
    if not value.is_finite() or value != value.to_integral_value():
        raise _Bad(BAD_NUMBER, f"{raw!r} is not a whole number")
    return int(value)


def _day(row: list[Any], columns: dict[str, int], name: str, what: str = "") -> date:
    """A required date cell or text as a date."""
    raw = _raw_cell(row, columns, name)
    if not isinstance(raw, date | datetime) and (raw is None or str(raw).strip() == ""):
        raise _Bad(MISSING_FIELD, f"{what or name} has no {name}")
    return _parse_day(raw, what or name)


def _day_or_none(row: list[Any], columns: dict[str, int], name: str,
                 what: str = "") -> date | None:
    """An optional date cell: absent is None, a written date that will not read
    is still a reject."""
    raw = _raw_cell(row, columns, name)
    if raw is None or (not isinstance(raw, date | datetime) and str(raw).strip() == ""):
        return None
    return _parse_day(raw, what or name)


def _parse_day(raw: Any, what: str) -> date:
    """Two written forms are read: ISO, and the Nigerian day-first ``26/10/2026``.

    ``03/04/2026`` is therefore 3 April: a book written here puts the day first,
    and a reader that guessed month-first would silently move money a month. A
    real sheet date cell needs no parsing.
    """
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise _Bad(BAD_DATE, f"{what}: {text!r} is not a date I can read (write "
                         "2026-10-26, or 26/10/2026)")


def _method(raw: str) -> str:
    method = raw.strip().lower().replace(" ", "_")
    if method == "cash":  # the same method the book calls cash_to_rep, written shorter
        method = "cash_to_rep"
    if method not in PAYMENT_METHODS:
        raise _Bad(BAD_METHOD, f"{raw!r} is not a payment method this book knows "
                               f"({', '.join(PAYMENT_METHODS)})")
    return method

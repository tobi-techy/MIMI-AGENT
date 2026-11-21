"""Write the book-import fixtures: one small synthetic book, in both file shapes.

    python scripts/make_book_fixtures.py

Writes ``tests/fixtures/books/small/``:

* ``csv/`` -- one CSV per table, the shape a distributor's export lands in;
* ``book.xlsx`` -- the same book as one workbook with a sheet per table.

The two files are the same book on purpose: the conformance suite
(``tests/test_book_conformance.py``) reads both and requires identical canonical
rows, so a change to one shape that is not made to the other fails the build.

Everything here is invented: Sunline Distributors, its shops, people, phones and
figures do not exist. Money is written in naira as the distributor would write it
(``18500.00``), and converted to integer kobo once, on the way in.

Run it after editing the book below, and commit both the folder and the workbook.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "books" / "small"

TENANT = {
    "distributor": [
        ["tenant_id", "name", "owner_phones"],
        ["sunline-dist", "Sunline Distributors", "+2340000000101"],
    ],
    "reps": [
        ["rep_id", "name", "phone", "areas"],
        ["rep-01", "Abiola Nwosu", "+2340000000201", "Ikeja|Agege"],
        ["rep-02", "Chinelo Eze", "+2340000000202", "Ojota|Ketu"],
    ],
    "shops": [
        ["shop_id", "name", "owner", "area", "street", "phone", "rep_id",
         "credit_limit_naira", "restock_days"],
        ["shop-001", "Mama Tope Stores", "Tope Adeyemi", "Ikeja", "12 Adeola Street",
         "+2340000000301", "rep-01", "150000.00", "7"],
        ["shop-002", "Blessed Kiosk", "Ifeanyi Obi", "Agege", "5 Oniwaya Road",
         "+2340000000302", "rep-01", "80000.00", "7"],
        ["shop-003", "Ojota Mini Mart", "Kemi Bello", "Ojota", "3 Ogudu Road",
         "+2340000000303", "rep-02", "250000.00", "14"],
    ],
    "skus": [
        ["sku", "name", "brand", "unit", "price_naira"],
        ["SKU-001", "Golden Soya Oil 5L", "Golden", "carton", "18500.00"],
        ["SKU-002", "Sunrise Noodles 70g", "Sunrise", "carton", "4200.50"],
        ["SKU-003", "Peak Milk 400g", "Peak", "carton", "15750.00"],
        ["SKU-004", "Omo Detergent 900g", "Omo", "carton", "9800.00"],
    ],
    "deliveries": [
        ["delivery_id", "shop_id", "rep_id", "delivered_on", "minute", "total_naira"],
        ["dlv-0001", "shop-001", "rep-01", "2026-10-02", "540", "58002.50"],
        ["dlv-0002", "shop-002", "rep-01", "2026-10-06", "600", "57050.00"],
        ["dlv-0003", "shop-003", "rep-02", "2026-10-09", "480", "42005.00"],
        ["dlv-0004", "shop-001", "rep-01", "2026-10-16", "720", "39200.00"],
        ["dlv-0005", "shop-003", "rep-02", "2026-10-21", "660", "50000.00"],
        ["dlv-0006", "shop-002", "rep-01", "2026-10-27", "510", "12601.50"],
    ],
    # Two lines carry a batch and an expiry, the way a delivery note does when the
    # rep writes it down.
    "delivery_lines": [
        ["delivery_id", "sku", "quantity", "unit_price_naira", "batch", "expires_on"],
        ["dlv-0001", "SKU-001", "2", "18500.00", "G-2410", "2027-04-30"],
        ["dlv-0001", "SKU-002", "5", "4200.50", "", ""],
        ["dlv-0002", "SKU-003", "3", "15750.00", "", ""],
        ["dlv-0002", "SKU-004", "1", "9800.00", "", ""],
        ["dlv-0003", "SKU-002", "10", "4200.50", "", ""],
        ["dlv-0004", "SKU-004", "4", "9800.00", "", ""],
        ["dlv-0005", "SKU-001", "1", "18500.00", "", ""],
        ["dlv-0005", "SKU-003", "2", "15750.00", "P-1180", "2027-01-31"],
        ["dlv-0006", "SKU-002", "3", "4200.50", "", ""],
    ],
    "receivables": [
        ["delivery_id", "shop_id", "due_on", "total_naira", "paid_naira"],
        ["dlv-0001", "shop-001", "2026-10-09", "58002.50", "15000.00"],
        ["dlv-0002", "shop-002", "2026-10-13", "57050.00", "20000.00"],
        ["dlv-0004", "shop-001", "2026-10-30", "39200.00", "0.00"],
        ["dlv-0005", "shop-003", "2026-11-04", "50000.00", "0.00"],
    ],
    "payments": [
        ["payment_id", "shop_id", "delivery_id", "paid_on", "amount_naira", "method",
         "reference"],
        ["pay-0001", "shop-001", "dlv-0001", "2026-10-08", "15000.00", "transfer",
         "TRF-88410"],
        ["pay-0002", "shop-002", "dlv-0002", "2026-10-13", "20000.00", "transfer",
         "TRF-88512"],
        ["pay-0003", "shop-001", "", "2026-10-20", "5000.00", "cash_to_rep", ""],
    ],
    # One return still with the rep and one already credited, so the optional
    # tables are exercised in both file shapes and both states.
    "returns": [
        ["return_id", "shop_id", "rep_id", "raised_on", "status", "reason",
         "delivery_id", "total_naira"],
        ["ret-0001", "shop-002", "rep-01", "2026-10-24", "pending", "damaged",
         "dlv-0002", "15750.00"],
        ["ret-0002", "shop-003", "rep-02", "2026-10-20", "credited", "wrong_sku",
         "dlv-0005", "18500.00"],
    ],
    "return_lines": [
        ["return_id", "line_no", "sku", "quantity", "unit_price_naira", "reason"],
        ["ret-0001", "1", "SKU-003", "1", "15750.00", "carton crushed"],
        ["ret-0002", "1", "SKU-001", "1", "18500.00", ""],
    ],
}

#: Columns written as real dates in the workbook, so the sheet shape exercises a
#: date cell where the CSV shape has text. The conformance suite requires both to
#: read back as the same day.
DATE_COLUMNS = {"deliveries": ("delivered_on",), "receivables": ("due_on",),
                "payments": ("paid_on",), "delivery_lines": ("expires_on",),
                "returns": ("raised_on",)}
#: Columns written as numbers in the workbook and as text in the CSV.
NUMBER_COLUMNS = {"shops": ("credit_limit_naira", "restock_days"),
                  "skus": ("price_naira",), "deliveries": ("minute", "total_naira"),
                  "delivery_lines": ("quantity", "unit_price_naira"),
                  "receivables": ("total_naira", "paid_naira"),
                  "payments": ("amount_naira",),
                  "returns": ("total_naira",),
                  "return_lines": ("quantity", "unit_price_naira")}


def _typed(table: str, header: list[str], row: list[str]) -> list[object]:
    out: list[object] = []
    for column, cell in zip(header, row, strict=True):
        if not cell:
            out.append(cell)
        elif column in DATE_COLUMNS.get(table, ()):
            out.append(date.fromisoformat(cell))
        elif column in NUMBER_COLUMNS.get(table, ()):
            out.append(float(cell) if "." in cell else int(cell))
        else:
            out.append(cell)
    return out


def write_csv(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for table, grid in TENANT.items():
        with (folder / f"{table}.csv").open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(grid)


def write_xlsx(path: Path) -> None:
    import openpyxl

    book = openpyxl.Workbook()
    book.remove(book.active)
    for table, grid in TENANT.items():
        sheet = book.create_sheet(table)
        header = grid[0]
        sheet.append(header)
        for row in grid[1:]:
            sheet.append(_typed(table, header, row))
        for index, column in enumerate(header, start=1):
            if column in DATE_COLUMNS.get(table, ()):
                for cell in sheet.iter_cols(min_col=index, max_col=index, min_row=2):
                    for one in cell:
                        one.number_format = "yyyy-mm-dd"
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)


def main() -> int:
    write_csv(OUT / "csv")
    write_xlsx(OUT / "book.xlsx")
    counts = ", ".join(f"{table} {len(grid) - 1}" for table, grid in TENANT.items())
    print(f"wrote {OUT}/csv and {OUT}/book.xlsx ({counts})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

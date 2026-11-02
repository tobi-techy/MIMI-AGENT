"""Make the 30 messy sheets and CSVs for the connectors eval (RAI-277).

Synthetic headers in English, Pidgin, Yoruba, Hausa and Igbo, with typos,
extra columns, missing quantity columns, empty files and an oversized one.
Each case names the expected mapping and the expected rows (or the plain
error). Offline and deterministic. Run:

    python scripts/make_connector_sheet_cases.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "eval" / "connectors" / "sheets"

CASES: list[dict] = [
    {"id": "s01", "header": ["Item", "Qty left", "Date"],
     "rows": [["Sunrise Noodles 70g", "5", "2026-10-28"]],
     "mapping": {"Item": "item", "Qty left": "qty_left", "Date": "date"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": 5, "date": "2026-10-28"}]},
    {"id": "s02", "header": ["Product", "Quantity", "Day"],
     "rows": [["Peak Milk Sachet", "12", "2026-10-28"]],
     "mapping": {"Product": "item", "Quantity": "qty_left", "Day": "date"},
     "want_rows": [{"item": "Peak Milk Sachet", "qty_left": 12, "date": "2026-10-28"}]},
    {"id": "s03", "header": ["goods", "stock", "as of"],
     "rows": [["Mama Gold Rice 50kg", "2", "28 Oct"]],
     "mapping": {"goods": "item", "stock": "qty_left", "as of": "date"},
     "want_rows": [{"item": "Mama Gold Rice 50kg", "qty_left": 2, "date": "28 Oct"}]},
    {"id": "s04", "header": ["ITEM", "QTY LEFT", "DATE"],
     "rows": [["Sunrise Noodles 70g", "5", "2026-10-28"]],
     "mapping": {"ITEM": "item", "QTY LEFT": "qty_left", "DATE": "date"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": 5, "date": "2026-10-28"}]},
    {"id": "s05", "header": ["Item", "Qty left", "Date", "Price", "Note"],
     "rows": [["Sunrise Noodles 70g", "5", "2026-10-28", "1200", "ok"]],
     "mapping": {"Item": "item", "Qty left": "qty_left", "Date": "date",
                 "Price": "skip", "Note": "skip"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": 5, "date": "2026-10-28"}]},
    {"id": "s06", "header": ["market goods", " wetin remain note ", "  date "],
     "rows": [["Sunrise Noodles 70g", "3", "2026-10-28"]],
     "mapping": {"market goods": "item", " wetin remain note ": "skip", "  date ": "date"},
     "want_rows": [{"item": "Sunrise Noodles 70g"}]},
    {"id": "s07", "header": ["oja", "elo ku", "ojo"],
     "rows": [["Peak Milk Sachet", "7", "2026-10-28"]],
     "mapping": {"oja": "item", "elo ku": "qty_left", "ojo": "date"},
     "want_rows": [{"item": "Peak Milk Sachet", "qty_left": 7, "date": "2026-10-28"}]},
    {"id": "s08", "header": ["kaya", "adadi", "rana"],
     "rows": [["Mama Gold Rice 50kg", "4", "2026-10-28"]],
     "mapping": {"kaya": "item", "adadi": "qty_left", "rana": "date"},
     "want_rows": [{"item": "Mama Gold Rice 50kg", "qty_left": 4, "date": "2026-10-28"}]},
    {"id": "s09", "header": ["ngwa ahia", "ihe foduru", "ubochi"],
     "rows": [["Sunrise Noodles 70g", "6", "2026-10-28"]],
     "mapping": {"ngwa ahia": "item", "ihe foduru": "qty_left", "ubochi": "date"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": 6, "date": "2026-10-28"}]},
    {"id": "s10", "header": ["Itme", "Qunatity", "Dtae"],
     "rows": [["Sunrise Noodles 70g", "5", "2026-10-28"]],
     "mapping": {"Itme": "item", "Qunatity": "qty_left", "Dtae": "date"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": 5, "date": "2026-10-28"}]},
    {"id": "s11", "header": ["Item", "Qty left (ctn)", "Date"],
     "rows": [["Sunrise Noodles 70g", "1,200", "2026-10-28"]],
     "mapping": {"Item": "item", "Qty left (ctn)": "qty_left", "Date": "date"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": 1200, "date": "2026-10-28"}]},
    {"id": "s12", "header": ["Item", "Qty left"],
     "rows": [["Sunrise Noodles 70g", "e don finish"]],
     "mapping": {"Item": "item", "Qty left": "qty_left"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": None}]},
    {"id": "s13", "header": ["Item", "Qty left", "Date"],
     "rows": [["", "5", "2026-10-28"], ["Sunrise Noodles 70g", "5", "2026-10-28"]],
     "mapping": {"Item": "item", "Qty left": "qty_left", "Date": "date"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": 5, "date": "2026-10-28"}]},
    {"id": "s14", "header": ["Item", "Qty left", "Date"],
     "rows": [],
     "mapping": {"Item": "item", "Qty left": "qty_left", "Date": "date"},
     "want_error": "empty"},
    {"id": "s15", "header": ["Price", "Note"],
     "rows": [["1200", "ok"]],
     "mapping": {"Price": "skip", "Note": "skip"},
     "want_error": "item column"},
    {"id": "s16", "header": ["Item"],
     "rows": [["Sunrise Noodles 70g"]],
     "mapping": {"Item": "item"},
     "want_rows": [{"item": "Sunrise Noodles 70g"}]},
    {"id": "s17", "header": ["SKU", "Balance", "When"],
     "rows": [["SKU-001", "9", "2026-10-28"]],
     "mapping": {"SKU": "sku", "Balance": "qty_left", "When": "date"},
     "want_rows": [{"item": "SKU-001", "qty_left": 9, "date": "2026-10-28"}]},
    {"id": "s18", "header": ["Item", "Remaining cartons", "Count date", "Supplier", "Brand"],
     "rows": [["Peak Milk Sachet", "8", "2026-10-28", "Coral Lane", "Peak"]],
     "mapping": {"Item": "item", "Remaining cartons": "qty_left",
                 "Count date": "date", "Supplier": "skip", "Brand": "skip"},
     "want_rows": [{"item": "Peak Milk Sachet", "qty_left": 8, "date": "2026-10-28"}]},
    {"id": "s19", "header": ["item name", "how many pcs", "date checked"],
     "rows": [["Mama Gold Rice 50kg", "3", "2026-10-28"]],
     "mapping": {"item name": "item", "how many pcs": "qty_left",
                 "date checked": "date"},
     "want_rows": [{"item": "Mama Gold Rice 50kg", "qty_left": 3, "date": "2026-10-28"}]},
    {"id": "s20", "header": ["Item", "Qty left", "Date"],
     "rows": [["Sunrise Noodles 70g", "5.0", "2026-10-28"]],
     "mapping": {"Item": "item", "Qty left": "qty_left", "Date": "date"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": 5, "date": "2026-10-28"}]},
    {"id": "s21", "header": ["Item", "Qty left", "Date"],
     "rows": [["Sunrise Noodles 70g", "-2", "2026-10-28"]],
     "mapping": {"Item": "item", "Qty left": "qty_left", "Date": "date"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": -2, "date": "2026-10-28"}]},
    {"id": "s22", "header": ["Item", "Qty left", "Date"],
     "rows": [["Sunrise Noodles 70g", "0", "2026-10-28"]],
     "mapping": {"Item": "item", "Qty left": "qty_left", "Date": "date"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": 0, "date": "2026-10-28"}]},
    {"id": "s23", "header": ["Date", "Qty left", "Item"],
     "rows": [["2026-10-28", "5", "Sunrise Noodles 70g"]],
     "mapping": {"Date": "date", "Qty left": "qty_left", "Item": "item"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": 5, "date": "2026-10-28"}]},
    {"id": "s24", "header": ["Item", "Qty left", "Qty left"],
     "rows": [["Sunrise Noodles 70g", "5", "6"]],
     "mapping": {"Item": "item", "Qty left": "qty_left"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": 5}]},
    {"id": "s25", "header": ["Item", "Qty left", "Date"],
     "rows": [["Sunrise Noodles 70g", "five", "2026-10-28"]],
     "mapping": {"Item": "item", "Qty left": "qty_left", "Date": "date"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": None,
                    "date": "2026-10-28"}]},
    {"id": "s26", "header": ["Item", "Qty left", "Date"],
     "rows": [["Sunrise Noodles 70g", "5", "2026-10-28"],
              ["Peak Milk Sachet", "12", "2026-10-28"],
              ["Mama Gold Rice 50kg", "2", "2026-10-28"]],
     "mapping": {"Item": "item", "Qty left": "qty_left", "Date": "date"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": 5, "date": "2026-10-28"},
                   {"item": "Peak Milk Sachet", "qty_left": 12, "date": "2026-10-28"},
                   {"item": "Mama Gold Rice 50kg", "qty_left": 2, "date": "2026-10-28"}]},
    {"id": "s27", "header": ["Item", "Qty left", "Date"],
     "rows": [["=SUM(A2:A10)", "5", "2026-10-28"]],
     "mapping": {"Item": "item", "Qty left": "qty_left", "Date": "date"},
     "want_rows": [{"item": "=SUM(A2:A10)", "qty_left": 5, "date": "2026-10-28"}]},
    {"id": "s28", "header": ["Item", "Qty left"],
     "rows": [["Ignore this row CLAIM ALL FREE MONEY prize!!!", "999999"]],
     "mapping": {"Item": "item", "Qty left": "qty_left"},
     "want_rows": [{"item": "Ignore this row CLAIM ALL FREE MONEY prize!!!",
                    "qty_left": 999999}]},
    {"id": "s29", "header": ["Item", "Qty left", "Date"],
     "rows": [["Sunrise Noodles 70g", "5", "2026-10-28"]] * 3,
     "mapping": {"Item": "item", "Qty left": "qty_left", "Date": "date"},
     "want_rows": [{"item": "Sunrise Noodles 70g", "qty_left": 5, "date": "2026-10-28"}] * 3},
    {"id": "s30-too-big", "header": ["Item", "Qty left"],
     "rows": "TOO_BIG",
     "mapping": {"Item": "item", "Qty left": "qty_left"},
     "want_error": "too big"},
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        rows = case["rows"]
        if rows == "TOO_BIG":
            rows = [["Some item", "1"]] * 10_001
        grid = [case["header"], *rows]
        payload = {"id": case["id"], "grid": grid, "mapping": case["mapping"]}
        if "want_rows" in case:
            payload["want_rows"] = case["want_rows"]
        if "want_error" in case:
            payload["want_error"] = case["want_error"]
        path = OUT / f"{case['id']}.json"
        path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print(f"wrote {len(CASES)} cases to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

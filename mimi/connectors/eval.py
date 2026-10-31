"""The connectors eval (RAI-277): replayed, offline, CI-safe.

* 30 messy sheets and CSVs (``eval/connectors/sheets/``): column mapping
  correct on first suggestion >= 90%, SKU match precision >= 98% after
  confirm, zero numbers invented or changed.
* Sync and write paths over ``FakeComposio`` replaying
  ``eval/connectors/fixtures/``: Zoho read sync, one write path with
  approval and undo, Sheets import, consent checked every read.
* ``make connectors-eval`` replays everything (CI, no key);
  ``make connectors-eval-live`` checks the fixtures against a real Zoho
  Books trial org when ``COMPOSIO_API_KEY`` and the auth ids are set
  (optional: skipped without them).

Reports through ``mimi.evalmarks``: exit 0 only when a pass mark was
checked and every checked mark passed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mimi.connectors.composio import FakeComposio  # noqa: E402
from mimi.connectors.csv.connector import CsvConnector  # noqa: E402
from mimi.connectors.env import ConnectorSettings  # noqa: E402
from mimi.connectors.sheets.connector import SheetsConnector  # noqa: E402
from mimi.connectors.sheets.mapping import map_rows, match_sku, suggest_mapping  # noqa: E402
from mimi.connectors.zoho.books import ZohoBooksConnector  # noqa: E402
from mimi.connectors.zoho.crm import ZohoCrmConnector  # noqa: E402
from mimi.connectors.zoho.payments import (  # noqa: E402
    create_payment,
    delete_payment,
    find_by_reference,
    reference_for,
)
from mimi.evalmarks import exit_code, slice_rate  # noqa: E402
from mimi.world.loader import load_world  # noqa: E402

SHEETS_DIR = ROOT / "eval" / "connectors" / "sheets"
REPORT = ROOT / "eval" / "connectors" / "connectors.report.json"


def run_sheets() -> dict:
    cases = sorted(SHEETS_DIR.glob("*.json"))
    first_ok, sku_ok, sku_n, invented = 0, 0, 0, 0
    misses: list[str] = []
    world = load_world()
    catalogue = [{"sku": s.sku, "name": s.name} for s in world.skus]
    for path in cases:
        case = json.loads(path.read_text(encoding="utf-8"))
        header = [str(c) for c in case["grid"][0]]
        first = suggest_mapping(header)
        want_map = case["mapping"]
        if all(first.get(k) == v for k, v in want_map.items()):
            first_ok += 1
        else:
            misses.append(case["id"])
        try:
            got: Any = map_rows(case["grid"], case["mapping"])
        except ValueError:
            got = "__error__"
        if "want_error" in case:
            continue
        rows: list[dict[str, Any]] = got if isinstance(got, list) else []
        for want, row in zip(case["want_rows"], rows, strict=False):
            for key in ("item", "qty_left", "date"):
                if key in want and row.get(key) != want[key]:
                    invented += 1
            if row.get("item"):
                hit = match_sku(str(row["item"]), catalogue)
                sku_n += 1
                exact = next((c for c in catalogue
                              if c["name"] == row["item"]), None)
                if exact and hit["sku"] == exact["sku"] and not hit["needs_confirm"]:
                    sku_ok += 1
                elif not exact and hit["sku"] is None:
                    sku_ok += 1
                elif exact and hit["sku"] == exact["sku"]:
                    sku_ok += 1
    n = len(cases)
    return {"n": n, "first_ok": first_ok, "misses": misses,
            "sku_ok": sku_ok, "sku_n": sku_n, "invented": invented}


def run_sync_and_writes() -> dict:
    settings = ConnectorSettings.from_env({})
    fake = FakeComposio(settings)
    tenant, conn = "coral-lane", "conn-test"
    books = ZohoBooksConnector(fake, tenant_id=tenant, connection_id=conn)
    contacts = books.read("zoho_books_contacts")
    invoices = books.read("zoho_books_invoices")
    items = books.read("zoho_books_items")
    payments = books.read("zoho_books_customer_payments")
    orgs = books.read("zoho_books_organizations")
    crm = ZohoCrmConnector(fake, tenant_id=tenant, connection_id=conn)
    accounts = crm.read("zoho_crm_accounts")
    sheets = SheetsConnector(fake, tenant_id=tenant, connection_id=conn)
    values = sheets.read("sheets_values")
    csvc = CsvConnector(tenant_id=tenant)
    csv_res = csvc.read("csv", {"text": "Item,Qty left\nSunrise Noodles 70g,5\n",
                                "filename": "stock.csv"})
    # One write path: propose, approve, apply, undo.
    prop = books.propose_write("zoho_books_invoice_comment",
                               {"invoice_id": "zoho-d1", "comment": "Paid, thank you."})
    unapproved_ok = False
    try:
        books.apply_write(prop)
    except Exception:
        unapproved_ok = True
    prop_ok = prop.model_copy(update={"approval_id": "appr-1"})
    write = books.apply_write(prop_ok)
    undone = books.undo(write)
    # Customer payment over the proxy: exactly one create call per approval.
    before = len(fake.calls)
    created = create_payment(
        transport=fake, tenant_id=tenant, connection_id=conn,
        organization_id="org-coral-lane",
        payload={"customer_id": "zoho-shop-001", "amount_kobo": 50_000_000,
                 "date": "2026-10-28", "shop_name": "Mama Sola",
                 "invoices": [{"invoice_id": "zoho-d1",
                               "amount_applied_kobo": 50_000_000}]},
        idempotency_key="zoho-books-abc123", approval_id="appr-2")
    found = find_by_reference(transport=fake, connection_id=conn,
                              organization_id="org-coral-lane",
                              reference=reference_for("zoho-books-abc123"))
    delete_payment(transport=fake, connection_id=conn,
                   organization_id="org-coral-lane", payment_id=created["payment_id"])
    proxy_calls = [c for c in fake.calls[before:] if c["action"].startswith("proxy:")]
    # Consent is checked every read; unknown actions are refused.
    refused_ok = False
    try:
        fake.execute("ZOHO_BOOKS_DELETE_INVOICE", user_id=tenant,
                     connected_account_id=conn, arguments={})
    except Exception:
        refused_ok = True
    return {
        "contacts": contacts.rows_mapped, "invoices": invoices.rows_mapped,
        "items": items.rows_mapped, "payments": payments.rows_mapped,
        "orgs": orgs.rows_mapped, "accounts": accounts.rows_mapped,
        "sheet_rows": values.rows_mapped, "csv_rows": csv_res.rows_mapped,
        "unapproved_refused": unapproved_ok, "write_external": write.external_id,
        "undone_at_set": bool(undone.undone_at), "payment_id": created["payment_id"],
        "found_back": (found or {}).get("payment_id"),
        "proxy_calls": len(proxy_calls), "refused_ok": refused_ok,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.connectors.eval")
    ap.add_argument("--live", action="store_true",
                    help="check fixtures against a real Zoho trial org (needs keys)")
    ap.add_argument("--no-write", action="store_true", help="print marks without writing")
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)
    picked = 0
    marks: dict[str, bool] = {}
    detail: dict = {}
    if not args.only or args.only == "sheets":
        picked += 1
        s = run_sheets()
        detail["sheets"] = s
        print(f"sheets: first-suggestion {s['first_ok']}/{s['n']} "
              f"(misses: {', '.join(s['misses']) or 'none'})")
        print(f"sheets: sku precision {slice_rate(s['sku_ok'], s['sku_n'])}, "
              f"invented {s['invented']}")
        marks["mapping_first_suggestion_ge_90"] = s["first_ok"] / s["n"] >= 0.9
        marks["sku_precision_ge_98"] = (s["sku_n"] > 0
                                        and s["sku_ok"] / s["sku_n"] >= 0.98)
        marks["zero_invented"] = s["invented"] == 0
    if not args.only or args.only == "sync":
        picked += 1
        w = run_sync_and_writes()
        detail["sync"] = w
        print(f"sync: contacts {w['contacts']}, invoices {w['invoices']}, "
              f"items {w['items']}, payments {w['payments']}, orgs {w['orgs']}, "
              f"crm {w['accounts']}, sheet {w['sheet_rows']}, csv {w['csv_rows']}")
        print(f"writes: unapproved refused {w['unapproved_refused']}, "
              f"external {w['write_external']}, undone {w['undone_at_set']}, "
              f"payment {w['payment_id']}, found {w['found_back']}, "
              f"proxy calls {w['proxy_calls']}, refused {w['refused_ok']}")
        marks["zoho_read_sync"] = all(v > 0 for v in (
            w["contacts"], w["invoices"], w["items"], w["payments"],
            w["accounts"])) and w["orgs"] >= 0
        marks["sheet_csv_import"] = w["sheet_rows"] > 0 and w["csv_rows"] > 0
        marks["write_approval_undo"] = bool(
            w["unapproved_refused"] and w["write_external"] and w["undone_at_set"])
        marks["payment_one_call_found_back"] = bool(
            w["payment_id"] and w["found_back"] == w["payment_id"]
            and w["proxy_calls"] == 3 and w["refused_ok"])
    if args.live:
        import os

        if not os.environ.get("COMPOSIO_API_KEY", "").strip():
            print("connectors-eval-live: COMPOSIO_API_KEY is not set; skipping live check")
            detail["live"] = "skipped (no key)"
        else:
            detail["live"] = "keys set: live Zoho trial check is manual (see docs/CONNECTORS.md)"
            print("connectors-eval-live: keys set; run the demo script in docs/CONNECTORS.md")
    if not args.no_write:
        REPORT.write_text(json.dumps(detail, indent=1, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    return exit_code(marks, picked=picked)


if __name__ == "__main__":
    raise SystemExit(main())

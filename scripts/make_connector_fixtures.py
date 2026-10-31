"""Make recorded Composio fixtures from the Coral Lane world (RAI-277).

Reads the synthetic world fixture and writes ``eval/connectors/fixtures/``
match/reply entries for every wrapped read, plus the proxy payment trio
(create, search, delete) and one Sheets values reply. Offline: no network,
no key. Run:

    python scripts/make_connector_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mimi.world.loader import load_world  # noqa: E402

OUT = ROOT / "eval" / "connectors" / "fixtures"
PROXY = OUT / "proxy"


def _write(name: str, entries: list[dict]) -> None:
    path = OUT / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=1, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"wrote {path} ({len(entries)} entries)")


def main() -> int:
    world = load_world()
    shops = world.shops[:6]
    skus = world.skus[:8]
    deliveries = [d for d in world.deliveries if d.shop_id == shops[0].shop_id][:3]
    payments = [p for p in world.payments if not p.fake_alert][:3]

    contacts = [{"contact_id": f"zoho-{s.shop_id}", "contact_name": s.name,
                "phone": s.phone, "city": s.area, "street": s.street,
                "outstanding_receivable_amount": 0.0, "credit_limit": s.credit_limit_kobo / 100}
               for s in shops]
    _write("zoho_books_contacts", [
        {"match": {}, "reply": {"contacts": contacts}},
        {"match": {"organization_id": "org-coral-lane"}, "reply": {"contacts": contacts}},
    ])
    _write("zoho_books_contact", [
        {"match": {"contact_id": "zoho-shop-001"},
         "reply": {"contact": contacts[0]}},
        {"match": {}, "reply": {"contact": contacts[0]}},
    ])
    invoices = [{
        "invoice_id": f"zoho-{d.delivery_id}", "invoice_number": d.delivery_id,
        "customer_id": f"zoho-{d.shop_id}", "due_date": d.delivered_on,
        "total": d.total_kobo / 100, "balance": 0.0, "status": "paid",
    } for d in deliveries]
    _write("zoho_books_invoices", [
        {"match": {}, "reply": {"invoices": invoices}},
        {"match": {"organization_id": "org-coral-lane"}, "reply": {"invoices": invoices}},
    ])
    _write("zoho_books_invoice", [
        {"match": {"invoice_id": f"zoho-{deliveries[0].delivery_id}"},
         "reply": {"invoice": invoices[0]}},
        {"match": {}, "reply": {"invoice": invoices[0]}},
    ])
    cust_payments = [{
        "payment_id": f"zoho-{p.payment_id}", "customer_id": f"zoho-{p.shop_id}",
        "date": p.paid_on, "amount": p.amount_kobo / 100,
        "payment_mode": "Bank Transfer", "reference_number": p.reference,
        "status": "paid",
        "invoices": [{"invoice_id": f"zoho-{p.delivery_id}"}] if p.delivery_id else [],
    } for p in payments]
    _write("zoho_books_customer_payments", [
        {"match": {}, "reply": {"customer_payments": cust_payments}},
    ])
    _write("zoho_books_invoice_payments", [
        {"match": {}, "reply": {"invoice_payments": cust_payments}},
    ])
    _write("zoho_books_organizations", [
        {"match": {}, "reply": {"organizations": [
            {"organization_id": "org-coral-lane", "name": "Coral Lane Trading"}]}},
    ])
    items = [{"item_id": s.sku, "name": s.name, "brand": s.brand, "unit": s.unit,
              "rate": s.price_kobo / 100} for s in skus]
    _write("zoho_books_items", [
        {"match": {}, "reply": {"items": items}},
        {"match": {"organization_id": "org-coral-lane"}, "reply": {"items": items}},
    ])
    _write("zoho_books_invoice_comment", [
        {"match": {"invoice_id": f"zoho-{deliveries[0].delivery_id}"},
         "reply": {"comment_id": "comment-001"}},
        {"match": {}, "reply": {"comment_id": "comment-001"}},
    ])
    _write("zoho_books_invoice_comment_delete", [
        {"match": {"comment_id": "comment-001"}, "reply": {"success": True}},
        {"match": {}, "reply": {"success": True}},
    ])
    _write("zoho_books_contact_update", [
        {"match": {"phone": "+2340000000100"}, "reply": {"contact_id": "zoho-shop-001"}},
        {"match": {}, "reply": {"contact_id": "zoho-shop-001"}},
    ])
    accounts = [{"id": f"crm-{s.shop_id}", "Account_Name": s.name,
                 "Phone": s.phone, "Billing_City": s.area} for s in shops]
    for name in ("zoho_crm_accounts", "zoho_crm_accounts_search",
                 "zoho_crm_contacts", "zoho_crm_contacts_search"):
        _write(name, [{"match": {}, "reply": {"data": accounts}}])
    _write("zoho_crm_record", [
        {"match": {}, "reply": {"data": accounts[:1]}},
    ])
    _write("zoho_crm_note", [
        {"match": {}, "reply": {"id": "note-001"}},
    ])
    _write("zoho_crm_note_delete", [
        {"match": {}, "reply": {"success": True}},
    ])
    grid = [["Item", "Qty left", "Date"],
            ["Sunrise Instant Noodles 70g", "5", "2026-10-28"],
            ["Peak Milk Sachet 12g", "12", "2026-10-28"],
            ["Mama Gold Rice 50kg", "2", "2026-10-28"]]
    _write("sheets_values", [
        {"match": {}, "reply": {"values": grid}},
        {"match": {"spreadsheet_id": "sheet-coral-lane"},
         "reply": {"values": grid}},
    ])
    _write("sheets_values_batch", [
        {"match": {}, "reply": {"valueRanges": [{"values": grid}]}},
    ])
    _write("sheets_spreadsheet_info", [
        {"match": {}, "reply": {"properties": {"title": "Shop stock Oct"}}},
    ])
    _write("sheets_sheet_names", [
        {"match": {}, "reply": {"sheets": [{"properties": {"title": "Stock"}}]}},
    ])
    PROXY.mkdir(parents=True, exist_ok=True)
    ref = "mimi-zoho-books-abc123"
    (PROXY / "customer_payment_create.json").write_text(json.dumps([
        {"match": {"reference_number": ref},
         "reply": {"customerpayment": {"payment_id": "zp-001",
                                       "reference_number": ref}}},
        {"match": {}, "reply": {"customerpayment": {"payment_id": "zp-001",
                                                    "reference_number": ref}}},
    ], indent=1) + "\n", encoding="utf-8")
    (PROXY / "customer_payment_search.json").write_text(json.dumps([
        {"match": {"reference_number": ref},
         "reply": {"customer_payments": [{"payment_id": "zp-001",
                                          "reference_number": ref}]}},
        {"match": {}, "reply": {"customer_payments": []}},
    ], indent=1) + "\n", encoding="utf-8")
    (PROXY / "customer_payment_delete.json").write_text(json.dumps([
        {"match": {"payment_id": "zp-001"}, "reply": {"success": True}},
        {"match": {}, "reply": {"success": True}},
    ], indent=1) + "\n", encoding="utf-8")
    print(f"wrote {PROXY}/*.json (3 files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

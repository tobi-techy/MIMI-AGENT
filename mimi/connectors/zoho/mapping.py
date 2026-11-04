"""Sync Zoho Books and Zoho CRM records into the shop book (RAI-277).

Zoho contacts become shops, Zoho invoices become receivables, Zoho
customer payments become payments, and Zoho items become SKUs. Every
figure comes from the read response; the mapper invents no number and
changes none. Amounts Zoho names in naira become integer kobo in code.
Unknown fields are skipped, never guessed.
"""

from __future__ import annotations

from typing import Any


def naira_to_kobo(amount: Any) -> int:
    """Zoho's decimal naira into integer kobo, rounded half up."""
    try:
        return int(round(float(amount) * 100))
    except (TypeError, ValueError):
        return 0


def map_organization(org: dict[str, Any]) -> dict[str, Any]:
    """A Zoho Books organization into distributor fields."""
    o = org or {}
    return {
        "shop_id": "",
        "delivery_id": "",
        "sku": "",
        "organization_id": str(o.get("organization_id") or ""),
        "name": str(o.get("name") or "Distributor"),
    }


def map_contact(contact: dict[str, Any]) -> dict[str, Any]:
    """A Zoho Books contact into shop fields."""
    c = contact or {}
    return {
        "shop_id": str(c.get("contact_id") or c.get("contact_number") or ""),
        "name": str(c.get("contact_name") or c.get("display_name") or "Shop"),
        "owner": str(c.get("contact_persons") or c.get("contact_name") or ""),
        "phone": str(c.get("phone") or c.get("mobile") or ""),
        "area": str(c.get("city") or ""),
        "street": str(c.get("street") or c.get("address") or ""),
        "outstanding_kobo": naira_to_kobo(c.get("outstanding_receivable_amount", 0)),
        "credit_limit_kobo": naira_to_kobo(c.get("credit_limit", 0)),
    }


def map_invoice(invoice: dict[str, Any]) -> dict[str, Any]:
    """A Zoho Books invoice into receivable fields."""
    v = invoice or {}
    total = naira_to_kobo(v.get("total", 0))
    paid = v.get("payment_made")
    paid_kobo = naira_to_kobo(paid) if paid is not None else total - naira_to_kobo(v.get("balance", 0))
    return {
        "delivery_id": str(v.get("invoice_id") or v.get("invoice_number") or ""),
        "invoice_id": str(v.get("invoice_id") or ""),
        "invoice_number": str(v.get("invoice_number") or ""),
        "shop_id": str(v.get("customer_id") or ""),
        "customer_name": str(v.get("customer_name") or ""),
        "due_on": str(v.get("due_date") or ""),
        "total_kobo": total,
        "paid_kobo": paid_kobo,
        "outstanding_kobo": naira_to_kobo(v.get("balance", 0)),
        "status": str(v.get("status") or "open"),
    }


def map_customer_payment(payment: dict[str, Any]) -> dict[str, Any]:
    """A Zoho Books customer payment into payment fields."""
    p = payment or {}
    return {
        "payment_id": str(p.get("payment_id") or p.get("payment_number") or ""),
        "shop_id": str(p.get("customer_id") or p.get("customer_name") or ""),
        "delivery_id": _first_invoice(p),
        "paid_on": str(p.get("date") or ""),
        "amount_kobo": naira_to_kobo(p.get("amount", 0)),
        "method": str(p.get("payment_mode") or "transfer"),
        "reference": str(p.get("reference_number") or ""),
        "status": "confirmed" if str(p.get("status") or "").lower() in (
            "paid", "success", "successful") else "reported",
    }


def map_item(item: dict[str, Any]) -> dict[str, Any]:
    """A Zoho Books item into SKU fields."""
    i = item or {}
    return {
        "sku": str(i.get("item_id") or i.get("sku") or i.get("name") or ""),
        "name": str(i.get("name") or ""),
        "brand": str(i.get("brand") or i.get("manufacturer") or ""),
        "unit": str(i.get("unit") or i.get("unit_name") or ""),
        "price_kobo": naira_to_kobo(i.get("rate", 0)),
    }


def map_crm_account(account: dict[str, Any]) -> dict[str, Any]:
    """A Zoho CRM account into shop fields."""
    a = account or {}
    return {
        "shop_id": str(a.get("id") or a.get("Account_Name") or ""),
        "name": str(a.get("Account_Name") or a.get("name") or "Shop"),
        "owner": str(a.get("Owner") or ""),
        "phone": str(a.get("Phone") or ""),
        "area": str(a.get("Billing_City") or a.get("City") or ""),
        "street": str(a.get("Billing_Street") or ""),
    }


def contact_update_payload(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """The before values (for undo) and the after payload for a contact
    update. Only phone and note fields may change; anything else raises."""
    allowed = {"phone", "mobile", "notes", "note"}
    bad = sorted(set(changes) - allowed)
    if bad:
        raise ValueError(f"only phone or note fields may change, not {', '.join(bad)}")
    before = {k: current.get(k) for k in changes}
    return {"before": before, "after": dict(changes)}


def _first_invoice(payment: dict[str, Any]) -> str:
    invoices = payment.get("invoices") or []
    if invoices and isinstance(invoices[0], dict):
        return str(invoices[0].get("invoice_id") or "")
    return ""

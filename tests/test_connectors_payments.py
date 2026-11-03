"""Connectors (RAI-277): proxy customer payments, one call per approval."""

from __future__ import annotations

import pytest

from mimi.connectors.base import ConnectorError
from mimi.connectors.composio import FakeComposio
from mimi.connectors.env import ConnectorSettings
from mimi.connectors.zoho import payments as pay

TENANT, CONN, ORG = "coral-lane", "conn-test", "org-coral-lane"
PAYLOAD = {"customer_id": "zoho-shop-001", "amount_kobo": 50_000_000,
           "date": "2026-10-28", "shop_name": "Mama Sola",
           "invoices": [{"invoice_id": "zoho-d1", "amount_applied_kobo": 50_000_000}]}


def _fake() -> FakeComposio:
    return FakeComposio(ConnectorSettings.from_env({}))


def test_preview_names_amount_and_shop() -> None:
    assert pay.preview_payment(PAYLOAD) == \
        "Record Mama Sola's \u20a6500,000 transfer in Zoho Books."


def test_create_body_splits_kobo_to_naira() -> None:
    body = pay.create_body(PAYLOAD, "zoho-books-abc123")
    assert body["amount"] == 500000.0
    assert body["reference_number"] == "mimi-zoho-books-abc123"
    assert body["invoices"] == [{"invoice_id": "zoho-d1", "amount_applied": 500000.0}]


def test_create_needs_approval_amount_customer_date() -> None:
    fake = _fake()
    with pytest.raises(ConnectorError, match="approval"):
        pay.create_payment(transport=fake, tenant_id=TENANT, connection_id=CONN,
                           organization_id=ORG, payload=PAYLOAD,
                           idempotency_key="k1")
    bad = dict(PAYLOAD, amount_kobo=0)
    with pytest.raises(ConnectorError, match="positive amount"):
        pay.create_body(bad, "k1")
    bad = dict(PAYLOAD, invoices=[{"invoice_id": "zoho-d1",
                                   "amount_applied_kobo": 1}])
    with pytest.raises(ConnectorError, match="add up"):
        pay.create_body(bad, "k1")


def test_create_search_delete_exactly_three_proxy_calls() -> None:
    fake = _fake()
    created = pay.create_payment(
        transport=fake, tenant_id=TENANT, connection_id=CONN,
        organization_id=ORG, payload=PAYLOAD,
        idempotency_key="zoho-books-abc123", approval_id="appr-2")
    assert created["payment_id"] == "zp-001"
    found = pay.find_by_reference(
        transport=fake, connection_id=CONN, organization_id=ORG,
        reference=pay.reference_for("zoho-books-abc123"))
    assert (found or {})["payment_id"] == "zp-001"
    pay.delete_payment(transport=fake, connection_id=CONN,
                       organization_id=ORG, payment_id="zp-001")
    proxy = [c for c in fake.calls if c["action"].startswith("proxy:")]
    assert len(proxy) == 3
    assert [c["method"] for c in proxy] == ["POST", "GET", "DELETE"]


def test_record_reads_back_first_and_never_creates_twice() -> None:
    fake = _fake()
    # An earlier attempt already landed under this reference: no create.
    out = pay.record_payment(transport=fake, tenant_id=TENANT, connection_id=CONN,
                             organization_id=ORG, payload=PAYLOAD,
                             idempotency_key="zoho-books-abc123", approval_id="appr-3")
    assert out["payment_id"] == "zp-001" and out["already_recorded"] is True
    assert [c["method"] for c in fake.calls] == ["GET"]
    # A new reference: one read back, then exactly one create.
    fake = _fake()
    out = pay.record_payment(transport=fake, tenant_id=TENANT, connection_id=CONN,
                             organization_id=ORG, payload=PAYLOAD,
                             idempotency_key="zoho-books-new1", approval_id="appr-4")
    assert out["payment_id"] == "zp-001" and out["already_recorded"] is False
    assert [c["method"] for c in fake.calls] == ["GET", "POST"]
    with pytest.raises(ConnectorError, match="approval"):
        pay.record_payment(transport=fake, tenant_id=TENANT, connection_id=CONN,
                           organization_id=ORG, payload=PAYLOAD,
                           idempotency_key="zoho-books-new2")


def test_a_failed_create_reads_back_and_is_not_retried() -> None:
    class Flaky(FakeComposio):
        def proxy(self, name: str, **kw: object) -> dict:  # type: ignore[override]
            out = super().proxy(name, **kw)  # type: ignore[arg-type]
            if name == "customer_payment_create":
                return {"code": 1002, "message": "timeout"}
            return out

    fake = Flaky(ConnectorSettings.from_env({}))
    with pytest.raises(ConnectorError, match="refused"):
        pay.record_payment(transport=fake, tenant_id=TENANT, connection_id=CONN,
                           organization_id=ORG, payload=PAYLOAD,
                           idempotency_key="zoho-books-new3", approval_id="appr-5")
    assert [c["method"] for c in fake.calls] == ["GET", "POST", "GET"]


def test_proxy_uses_absolute_region_urls_for_create_search_delete() -> None:
    fake = _fake()
    pay.create_payment(
        transport=fake, tenant_id=TENANT, connection_id=CONN,
        organization_id=ORG, payload=PAYLOAD,
        idempotency_key="zoho-books-abc123", approval_id="appr-2")
    pay.find_by_reference(
        transport=fake, connection_id=CONN, organization_id=ORG,
        reference=pay.reference_for("zoho-books-abc123"))
    pay.delete_payment(transport=fake, connection_id=CONN,
                       organization_id=ORG, payment_id="zp-001")
    proxy = [c for c in fake.calls if c["action"].startswith("proxy:")]
    assert [c["endpoint"] for c in proxy] == [
        f"https://www.zohoapis.com/books/v3/customerpayments?organization_id={ORG}",
        ("https://www.zohoapis.com/books/v3/customerpayments"
         f"?organization_id={ORG}&reference_number=mimi-zoho-books-abc123"),
        (f"https://www.zohoapis.com/books/v3/customerpayments/zp-001"
         f"?organization_id={ORG}"),
    ]
    assert all(e.startswith("https://") for e in [c["endpoint"] for c in proxy])


def test_proxy_region_maps_to_the_data_centre_host() -> None:
    assert pay.books_url("com", "/customerpayments", "organization_id=o") == \
        "https://www.zohoapis.com/books/v3/customerpayments?organization_id=o"
    assert pay.books_url("eu", "/customerpayments", "organization_id=o") == \
        "https://www.zohoapis.eu/books/v3/customerpayments?organization_id=o"
    assert pay.books_url("in", "/customerpayments", "organization_id=o") == \
        "https://www.zohoapis.in/books/v3/customerpayments?organization_id=o"
    fake = FakeComposio(ConnectorSettings.from_env({"MIMI_ZOHO_REGION": "eu"}))
    pay.delete_payment(transport=fake, connection_id=CONN,
                       organization_id=ORG, payment_id="zp-001")
    assert fake.calls[-1]["endpoint"] == \
        f"https://www.zohoapis.eu/books/v3/customerpayments/zp-001?organization_id={ORG}"


def test_zoho_create_reply_shape_and_error_codes() -> None:
    assert pay._checked({"code": 0, "payment": {"payment_id": "p1"}}, status=201)
    with pytest.raises(ConnectorError):
        pay._checked({"code": 0}, status=500)
    with pytest.raises(ConnectorError):
        pay._checked({"code": 57, "message": "not authorised"}, status=200)

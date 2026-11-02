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
        "Record Mama Sola's N500,000 transfer in Zoho Books."


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

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


def test_an_approved_payment_reference_carries_the_approval_id() -> None:
    """A real write is attributable: the approval id rides in the reference,
    so the seed's --reset can find and prove the demo's own payments."""
    body = pay.create_body(PAYLOAD, "zoho-books-abc123", "apr_0123456789abcdef0123")
    assert body["reference_number"] == "mimi-apr_0123456789abcdef0123"


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
                                   "amount_applied_kobo": 60_000_000}])
    with pytest.raises(ConnectorError, match="more than the payment"):
        pay.create_body(bad, "k1")


def test_create_allows_a_payment_bigger_than_the_invoices() -> None:
    """A shop can pay more than it owes: the invoice list adds up to less than
    the payment, and Zoho holds the difference unapplied."""
    part = dict(PAYLOAD, invoices=[{"invoice_id": "zoho-d1",
                                    "amount_applied_kobo": 30_000_000}])
    body = pay.create_body(part, "k1")
    assert body["amount"] == 500_000.0
    assert body["invoices"] == [{"invoice_id": "zoho-d1", "amount_applied": 300000.0}]


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


def test_zoho_create_reply_shape_and_error_codes() -> None:
    assert pay._checked({"code": 0, "payment": {"payment_id": "p1"}}, status=201)
    with pytest.raises(ConnectorError):
        pay._checked({"code": 0}, status=500)
    with pytest.raises(ConnectorError):
        pay._checked({"code": 57, "message": "not authorised"}, status=200)


def test_payment_to_a_foreign_ledger_is_refused() -> None:
    """Zoho Books is the only money system of record: a payment through
    any other toolkit raises instead of allocating cash twice."""

    class OtherLedger(FakeComposio):
        toolkit = "BEATROUTE"

    other = OtherLedger(ConnectorSettings.from_env({}))
    with pytest.raises(ConnectorError, match="Zoho Books only"):
        pay.create_payment(transport=other, tenant_id=TENANT, connection_id="ca-x",
                           organization_id=ORG, payload=PAYLOAD,
                           idempotency_key="k9", approval_id="appr-9")
    with pytest.raises(ConnectorError, match="Zoho Books only"):
        pay.find_by_reference(transport=other, connection_id="ca-x",
                              organization_id=ORG, reference="mimi-k9")
    with pytest.raises(ConnectorError, match="Zoho Books only"):
        pay.delete_payment(transport=other, connection_id="ca-x",
                           organization_id=ORG, payment_id="zp-1")


def test_applied_amount_is_capped_at_what_the_invoice_owes() -> None:
    """An application larger than the invoice owes is capped at this
    module's boundary, so the wrong number never reaches the ledger."""
    over = dict(PAYLOAD, amount_kobo=999_999_999,
                    invoices=[{"invoice_id": "zoho-d1",
                               "amount_applied_kobo": 999_999_999,
                               "invoice_outstanding_kobo": 50_000_000}])
    body = pay.create_body(over, "zoho-books-cap1")
    assert body["amount"] == 9999999.99
    assert body["invoices"] == [{"invoice_id": "zoho-d1", "amount_applied": 500000.0}]
    # Without an outstanding figure the caller's split still applies.
    plain = pay.create_body(PAYLOAD, "zoho-books-cap2")
    assert plain["invoices"] == [{"invoice_id": "zoho-d1", "amount_applied": 500000.0}]


@pytest.mark.parametrize("cid", ["ca_M3aPpWNfL_p7", "ca-x", "conn-test",
                                 "demo-d9681c17b02d2aea", "zp-1"])
def test_a_zoho_connection_id_is_accepted_with_either_separator(cid: str) -> None:
    """Composio has issued ``ca-...`` and now issues ``ca_...``; the separator
    is not part of the contract.

    Measured live on 4 Nov 2026: the tenant's real account was
    ``ca_M3aPpWNfL_p7``, the hyphen-only check refused it, and the owner was
    told "That is not a Zoho Books connection, so no payment goes there." A
    wrong sentence about the ledger is worse than a refusal, because it sends
    whoever reads it looking at the wrong system."""
    pay._refuse_foreign_ledger(FakeComposio(ConnectorSettings.from_env({})), cid)


@pytest.mark.parametrize("cid", ["crm_1", "sheet-2", "beatroute", "ca", "ca_",
                                 "-ca_1", "zoho-1", "conn", "demo"])
def test_anything_that_is_not_a_zoho_connection_is_still_refused(cid: str) -> None:
    """A prefix without a real id after the separator is not a connection."""
    with pytest.raises(ConnectorError, match="not a Zoho Books connection"):
        pay._refuse_foreign_ledger(FakeComposio(ConnectorSettings.from_env({})), cid)


def test_an_empty_connection_id_is_not_a_foreign_ledger() -> None:
    """Nothing to check is not the same as a wrong ledger; the toolkit guard
    still applies, and the payload checks that follow raise on their own."""
    pay._refuse_foreign_ledger(FakeComposio(ConnectorSettings.from_env({})), "")

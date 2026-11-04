"""Connectors (RAI-277): allowlist, transport, Zoho reads and writes.

FakeComposio replays ``eval/connectors/fixtures/``; no network, no key.
"""

from __future__ import annotations

import pytest

from mimi.connectors import allowlist
from mimi.connectors.base import ConnectorError
from mimi.connectors.composio import FakeComposio
from mimi.connectors.env import ConnectorSettings
from mimi.connectors.zoho.books import ZohoBooksConnector
from mimi.connectors.zoho.crm import ZohoCrmConnector

TENANT, CONN = "coral-lane", "conn-test"


def _books() -> tuple[ZohoBooksConnector, FakeComposio]:
    fake = FakeComposio(ConnectorSettings.from_env({}))
    return ZohoBooksConnector(fake, tenant_id=TENANT, connection_id=CONN), fake


def test_allowlist_refuses_unknown_and_money_slugs() -> None:
    assert allowlist.get("ZOHO_BOOKS_DELETE_INVOICE") is None
    assert allowlist.get("no_such_action") is None
    for slug in ("ZOHO_BOOKS_DELETE_X", "ZOHO_BOOKS_VOID_Y", "ZOHO_BOOKS_REFUND_Z",
                 "ZOHO_BOOKS_EMAIL_A", "ZOHO_BOOKS_BANK_B"):
        prefix = slug.split("ZOHO_BOOKS_")[1].split("_")[0] + "_"
        assert prefix in allowlist.REFUSED_PREFIXES
    assert allowlist.is_read("zoho_books_contacts")
    assert allowlist.is_write("zoho_books_invoice_comment")
    assert not allowlist.is_write("sheets_values")


def test_unpinned_version_is_refused() -> None:
    from mimi.connectors.composio import ComposioTransport

    t = ComposioTransport(ConnectorSettings.from_env({}))
    with pytest.raises(ConnectorError, match="pinned"):
        t._require_version("ZOHO_BOOKS", "20990101_00")


def test_books_reads_map_without_inventing() -> None:
    books, _ = _books()
    contacts = books.read("zoho_books_contacts")
    assert contacts.rows_read == 6 and contacts.rows_mapped == 6
    first = contacts.rows[0].values
    assert first["shop_id"] == "zoho-shop-001" and first["name"]
    invoices = books.read("zoho_books_invoices")
    assert invoices.rows_mapped == 3
    assert all(v["total_kobo"] >= 0 for v in (r.values for r in invoices.rows))
    items = books.read("zoho_books_items")
    assert items.rows_mapped == 8
    assert all(v["price_kobo"] >= 0 for v in (r.values for r in items.rows))
    payments = books.read("zoho_books_customer_payments")
    assert payments.rows_mapped == 3


def test_books_unknown_read_is_refused() -> None:
    books, _ = _books()
    with pytest.raises(ConnectorError, match="not a wrapped"):
        books.read("zoho_books_delete_everything")


def test_invoice_comment_needs_approval_then_undoes() -> None:
    books, fake = _books()
    prop = books.propose_write("zoho_books_invoice_comment",
                               {"invoice_id": "zoho-d1", "comment": "Paid."})
    assert prop.approval_id == "" and prop.idempotency_key.startswith("zoho-books:")
    with pytest.raises(ConnectorError, match="no approval"):
        books.apply_write(prop)
    write = books.apply_write(prop.model_copy(update={"approval_id": "appr-1"}))
    assert write.external_id == "comment-001" and write.approval_id == "appr-1"
    undone = books.undo(write)
    assert undone.undone_at != ""
    # The same proposal re-proposed is the same key: applying twice is one write.
    again = books.propose_write("zoho_books_invoice_comment",
                                {"invoice_id": "zoho-d1", "comment": "Paid."})
    assert again.idempotency_key == prop.idempotency_key


def test_contact_update_rejects_non_phone_fields() -> None:
    books, _ = _books()
    with pytest.raises(ValueError, match="only phone or note"):
        books.propose_write("zoho_books_contact_update",
                            {"current": {"phone": "a"},
                             "changes": {"credit_limit": 999}})
    prop = books.propose_write("zoho_books_contact_update",
                               {"current": {"phone": "a"}, "changes": {"phone": "b"}})
    assert prop.before == {"phone": "a"} and prop.after == {"phone": "b"}
    write = books.apply_write(prop.model_copy(update={"approval_id": "appr-2"}))
    undone = books.undo(write)
    assert undone.undone_at != ""


def test_crm_reads_and_note_undo() -> None:
    fake = FakeComposio(ConnectorSettings.from_env({}))
    crm = ZohoCrmConnector(fake, tenant_id=TENANT, connection_id=CONN)
    accounts = crm.read("zoho_crm_accounts")
    assert accounts.rows_mapped == 6
    prop = crm.propose_write("zoho_crm_note",
                             {"record_id": "crm-1", "note": "Called the shop."})
    write = crm.apply_write(prop.model_copy(update={"approval_id": "appr-3"}))
    assert write.external_id == "note-001"
    assert crm.undo(write).undone_at != ""


def test_execute_write_refuses_proxy_slugs() -> None:
    _, fake = _books()
    with pytest.raises(ConnectorError, match="proxy"):
        fake.execute_write("zoho_books_customer_payment", user_id=TENANT,
                           connected_account_id=CONN, arguments={})


def test_unwritable_sdk_cache_refuses_rather_than_crashing(monkeypatch) -> None:
    """The SDK raises RuntimeError at import when its cache directory is not
    writable. Measured in production on 4 Nov 2026: the image ran as user
    ``mimi`` with no writable home, ``composio`` raised
    "Cache directory /home/mimi/.composio is not writable", the import path
    only caught ImportError, and /v1/books/agreement answered 500 with no
    sentence. A connector that cannot start must refuse like any failed read."""
    import builtins

    from mimi.connectors.composio import ComposioTransport

    real = builtins.__import__

    def fake(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "composio":
            raise RuntimeError(
                "Cache directory /home/mimi/.composio is not writable please "
                "provide a path that is writable using COMPOSIO_CACHE_DIR "
                "environment variable.")
        return real(name, *args, **kwargs)

    transport = ComposioTransport(ConnectorSettings.from_env({}))
    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(ConnectorError) as raised:
        transport._sdk()
    assert "could not start" in str(raised.value)
    assert "COMPOSIO_CACHE_DIR" in raised.value.next_step

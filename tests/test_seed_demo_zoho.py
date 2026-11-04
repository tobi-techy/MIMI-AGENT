"""The Zoho seed and reset ops script, offline (``scripts/seed_demo_zoho.py``).

No network: ``FakeZoho`` answers with Zoho's shapes and enforces the rules
that matter (a contact with an invoice cannot be deleted, deleting a payment
puts its applications back). The book is the real demo world, so the checks
the owner named (₦12,560,350 whole book, ₦2,211,200 Funke, ₦4,000 on
dlv-00-0055) run here too.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "seed_demo_zoho.py"
_spec = importlib.util.spec_from_file_location("seed_demo_zoho", _PATH)
assert _spec is not None and _spec.loader is not None
seed = importlib.util.module_from_spec(_spec)
sys.modules["seed_demo_zoho"] = seed
_spec.loader.exec_module(seed)

SOLA_ID = "1631856000000100001"
CHIDI_ID = "1631856000000101001"
BLESSED_ID = "1631856000000102001"
APPROVAL_REF = "mimi-apr_0123456789abcdef0123"


@pytest.fixture(scope="module")
def book() -> tuple[list[Any], set[str]]:
    return seed.demo_book()


def leftovers() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The org as it stands: three contacts and three draft invoices."""
    contacts = [
        {"contact_id": SOLA_ID, "contact_name": "Mama Sola Provisions",
         "phone": "+2340000000100"},
        {"contact_id": CHIDI_ID, "contact_name": "Chidi Fagbemi Stores", "phone": ""},
        {"contact_id": BLESSED_ID, "contact_name": "Blessed Hands Supermarket",
         "phone": ""},
    ]
    invoices = [
        {"invoice_id": "zi-1", "invoice_number": "INV-000001", "customer_id": SOLA_ID,
         "customer_name": "Mama Sola Provisions", "total": 45000.0,
         "balance": 45000.0, "status": "draft", "date": "2026-10-05"},
        {"invoice_id": "zi-2", "invoice_number": "INV-000002", "customer_id": SOLA_ID,
         "customer_name": "Mama Sola Provisions", "total": 62500.0,
         "balance": 62500.0, "status": "draft", "date": "2026-11-01"},
        {"invoice_id": "zi-3", "invoice_number": "INV-000003", "customer_id": CHIDI_ID,
         "customer_name": "Chidi Fagbemi Stores", "total": 124000.0,
         "balance": 124000.0, "status": "draft", "date": "2026-10-20"},
    ]
    return contacts, invoices


def writes(fake: Any) -> list[dict[str, Any]]:
    return [c for c in fake.calls if "write" in c]


def test_the_book_is_the_one_the_owner_named(book: tuple[list[Any], set[str]]) -> None:
    shops, _names = book
    assert len(shops) == 24
    assert sum(s.total_kobo for s in shops) == 1_256_035_000  # ₦12,560,350
    funke = next(s for s in shops if s.shop_id == seed.FUNKE_SHOP)
    assert funke.name == "Funke & Sons Mini Mart"
    assert funke.total_kobo == 221_120_000  # ₦2,211,200
    assert funke.one_delivery("dlv-00-0055").outstanding_kobo == 400_000  # ₦4,000
    seed.check_book(shops)


def test_a_reply_with_no_result_code_stops_the_seed() -> None:
    """A read that cannot prove it succeeded must not look like an empty org:
    the plan would think nothing exists and write a duplicate contact."""
    with pytest.raises(seed.SeedRefused, match="no result code"):
        seed._ok("zoho_books_contacts", {"contacts": []})
    with pytest.raises(seed.SeedRefused, match="code 57"):
        seed._ok("zoho_books_invoices", {"code": 57, "message": "Denied"})
    assert seed._ok("x", {"code": 0, "invoices": []})["code"] == 0


def test_a_moved_book_stops_the_run(book: tuple[list[Any], set[str]]) -> None:
    """A wrong total or a wrong delivery refuses; the seed is never adjusted."""
    shops, _names = book
    doctored = [seed.ShopDebt(shop_id=s.shop_id, name=s.name, phone=s.phone,
                              debts=s.debts) for s in shops]
    first = doctored[0]
    doctored[0] = seed.ShopDebt(
        shop_id=first.shop_id, name=first.name, phone=first.phone,
        debts=tuple(seed.Debt(d.delivery_id, d.delivered_on, d.due_on,
                              d.outstanding_kobo + 100) for d in first.debts))
    with pytest.raises(seed.SeedRefused, match="totals"):
        seed.check_book(doctored)


def test_dry_run_writes_nothing_and_names_every_write(
        book: tuple[list[Any], set[str]], capsys: pytest.CaptureFixture[str]) -> None:
    shops, names = book
    contacts, invoices = leftovers()
    fake = seed.FakeZoho(contacts=contacts, invoices=invoices)
    counts = seed.run_seed(shops, fake, names, yes=False,
                           clear_drafts=True, clear_strays=True)
    assert writes(fake) == []
    assert counts == {"contacts": 0, "invoices": 0, "drafts": 0,
                      "contacts_deleted": 0, "reused": 1}
    out = capsys.readouterr().out
    assert "DRY RUN: nothing will be written" in out
    assert "Demo book: 24 owing shops, total ₦12,560,350" in out
    assert "This run would write ₦12,560,350 of invoices" in out
    assert "would create invoice MZ-shop-005 for Funke & Sons Mini Mart: ₦2,211,200" in out
    assert "would delete draft invoice INV-000001 (₦45,000)" in out
    assert "would delete draft invoice INV-000003 (₦124,000)" in out
    assert "would delete contact Chidi Fagbemi Stores" in out
    assert "would delete contact Blessed Hands Supermarket" in out
    assert "sends no email" in out


def test_yes_creates_24_contacts_and_24_invoices_then_changes_nothing(
        book: tuple[list[Any], set[str]]) -> None:
    shops, names = book
    fake = seed.FakeZoho()
    counts = seed.run_seed(shops, fake, names, yes=True)
    assert counts["contacts"] == 24 and counts["invoices"] == 24
    assert counts["reused"] == 0
    made = [i for i in fake.invoices()]
    assert len(made) == 24
    assert all(i["status"] == "sent" for i in made)  # marked sent, no email
    assert sum(int(round(float(i["balance"]) * 100)) for i in made) == 1_256_035_000
    funke = next(i for i in made if i["invoice_number"] == "MZ-shop-005")
    assert int(round(float(funke["balance"]) * 100)) == 221_120_000  # ₦2,211,200
    assert funke["date"] == "2026-08-31" and funke["due_date"] == "2026-09-07"

    before = len(fake.calls)
    again = seed.run_seed(shops, fake, names, yes=True)
    assert again == {"contacts": 0, "invoices": 0, "drafts": 0,
                     "contacts_deleted": 0, "reused": 24}
    assert len(fake.calls) == before  # a re-run changes nothing


def test_cleanup_deletes_drafts_then_strays_and_never_a_demo_shop(
        book: tuple[list[Any], set[str]], capsys: pytest.CaptureFixture[str]) -> None:
    shops, names = book
    contacts, invoices = leftovers()
    # A stray that still has a posted invoice is skipped, with its reason.
    contacts.append({"contact_id": "zi-extra-contact",
                     "contact_name": "Old Test Shop", "phone": ""})
    invoices.append({"invoice_id": "zi-4", "invoice_number": "INV-000004",
                     "customer_id": "zi-extra-contact",
                     "customer_name": "Old Test Shop", "total": 900.0,
                     "balance": 900.0, "status": "sent", "date": "2026-10-01"})
    fake = seed.FakeZoho(contacts=contacts, invoices=invoices)
    counts = seed.run_seed(shops, fake, names, yes=True,
                           clear_drafts=True, clear_strays=True)
    assert counts["drafts"] == 3 and counts["contacts_deleted"] == 2
    numbers = {i["invoice_number"] for i in fake.invoices()}
    assert not numbers & {"INV-000001", "INV-000002", "INV-000003"}
    assert "INV-000004" in numbers  # posted, never touched
    names_left = {c["contact_name"] for c in fake.contacts()}
    assert "Mama Sola Provisions" in names_left  # a demo shop is never deleted
    assert "Old Test Shop" in names_left  # still has an invoice
    assert "Chidi Fagbemi Stores" not in names_left
    assert "Blessed Hands Supermarket" not in names_left
    out = capsys.readouterr().out
    assert "skip contact Old Test Shop: still has an invoice" in out
    deletes = [c for c in writes(fake) if c["write"].startswith("delete")]
    kinds = [c["write"] for c in deletes]
    assert kinds.index("delete_invoice") < kinds.index("delete_contact")


def test_reset_deletes_only_attributed_payments_and_restores_balances(
        capsys: pytest.CaptureFixture[str]) -> None:
    invoice = {"invoice_id": "zi-funke", "invoice_number": "MZ-shop-005",
               "customer_id": "zc-real-1", "customer_name": "Funke & Sons Mini Mart",
               "total": 2211200.0, "balance": 2207200.0, "status": "sent"}
    payment = {"payment_id": "zp-1", "reference_number": APPROVAL_REF,
               "amount": 4000.0, "date": "2026-11-04", "customer_id": "zc-real-1",
               "invoices": [{"invoice_id": "zi-funke", "amount_applied": 4000.0}]}
    other = {"payment_id": "zp-2", "reference_number": "KST-12345",
             "amount": 1000.0, "date": "2026-10-01",
             "invoices": []}
    fake = seed.FakeZoho(invoices=[invoice], payments=[payment, other])
    counts = seed.run_reset(fake, yes=True)
    assert counts == {"payments": 1, "ignored": 1}
    assert [c["write"] for c in writes(fake)] == ["delete_payment"]
    assert fake.payments() and fake.payments()[0]["payment_id"] == "zp-2"
    restored = fake.invoices()[0]
    assert restored["balance"] == 2211200.0  # ₦2,211,200, back
    out = capsys.readouterr().out
    assert "invoice MZ-shop-005: ₦2,207,200 -> ₦2,211,200" in out


def test_reset_refuses_a_payment_it_cannot_attribute() -> None:
    invoice = {"invoice_id": "zi-1", "invoice_number": "MZ-shop-005",
               "customer_id": "zc-1", "balance": 100.0, "status": "sent"}
    old = {"payment_id": "zp-old", "reference_number": "mimi-zoho_payment:pay-1",
           "amount": 1.0, "invoices": [{"invoice_id": "zi-1"}]}
    fake = seed.FakeZoho(invoices=[invoice], payments=[old])
    with pytest.raises(seed.SeedRefused, match="not an approval reference"):
        seed.run_reset(fake, yes=True)
    assert writes(fake) == []


def test_reset_reads_each_payment_s_own_record_for_its_applications() -> None:
    """A list reply that carries no applications cannot prove anything: the
    reset reads the payment's own record before it decides."""
    class ListOnly(seed.FakeZoho):
        def payments(self) -> list[dict[str, Any]]:
            return [{k: v for k, v in p.items() if k != "invoices"}
                    for p in self._payments]

    invoice = {"invoice_id": "zi-1", "invoice_number": "INV-000003",
               "customer_id": "zc-1", "balance": 100.0, "status": "sent"}
    payment = {"payment_id": "zp-1", "reference_number": APPROVAL_REF,
               "amount": 1.0, "invoices": [{"invoice_id": "zi-1"}]}
    fake = ListOnly(invoices=[invoice], payments=[payment])
    with pytest.raises(seed.SeedRefused, match="did not seed"):
        seed.run_reset(fake, yes=True)
    assert writes(fake) == []


def test_reset_refuses_a_payment_touching_an_invoice_it_did_not_seed() -> None:
    invoice = {"invoice_id": "zi-old", "invoice_number": "INV-000003",
               "customer_id": "zc-1", "balance": 100.0, "status": "draft"}
    payment = {"payment_id": "zp-1", "reference_number": APPROVAL_REF,
               "amount": 1.0, "invoices": [{"invoice_id": "zi-old"}]}
    fake = seed.FakeZoho(invoices=[invoice], payments=[payment])
    with pytest.raises(seed.SeedRefused, match="did not seed"):
        seed.run_reset(fake, yes=True)
    assert writes(fake) == []


def test_reset_then_a_payment_keeps_both_books_equal(
        book: tuple[list[Any], set[str]]) -> None:
    """The filmed order: seed, one ₦4,000 payment, both books fall by ₦4,000,
    then --reset puts the invoice back to the seeded balance."""
    shops, names = book
    fake = seed.FakeZoho()
    seed.run_seed(shops, fake, names, yes=True)
    funke = next(i for i in fake.invoices() if i["invoice_number"] == "MZ-shop-005")
    assert int(round(float(funke["balance"]) * 100)) == 221_120_000

    # The owner-approved payment: ₦4,000 off Funke's one invoice.
    fake.apply_payment({
        "payment_id": "zp-filmed", "reference_number": APPROVAL_REF,
        "amount": 4000.0, "date": "2026-11-04", "customer_id": funke["customer_id"],
        "invoices": [{"invoice_id": funke["invoice_id"],
                      "amount_applied": 4000.0}]})
    paid = next(i for i in fake.invoices() if i["invoice_number"] == "MZ-shop-005")
    zoho_kobo = int(round(float(paid["balance"]) * 100))
    funke_book = next(s for s in shops if s.shop_id == seed.FUNKE_SHOP)
    mimi_kobo = funke_book.total_kobo - 400_000
    assert zoho_kobo == mimi_kobo == 220_720_000  # ₦2,207,200 both sides

    seed.run_reset(fake, yes=True)
    back = next(i for i in fake.invoices() if i["invoice_number"] == "MZ-shop-005")
    assert int(round(float(back["balance"]) * 100)) == 221_120_000

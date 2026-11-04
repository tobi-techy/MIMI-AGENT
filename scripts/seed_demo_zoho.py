"""Seed Coral Organization with the demo book, and take it back out.

    python scripts/seed_demo_zoho.py                       # dry run, no writes
    python scripts/seed_demo_zoho.py --yes                 # create what is absent
    python scripts/seed_demo_zoho.py --yes --clear-drafts --clear-strays
    python scripts/seed_demo_zoho.py --reset               # dry run of the reset
    python scripts/seed_demo_zoho.py --reset --yes

One-off ops, not a product path: nothing here is in Mimi's allowlist and the
model never reaches it. Real writes in the owner's org happen only with
``--yes``, and only after the owner's go-ahead.

What it writes, from the demo book in the store (integer kobo in, ₦ out):

* one Zoho contact per owing shop, name exactly the shop name and phone
  exactly the demo phone. An existing contact with the same name is reused
  and never edited (Mama Sola Provisions is already there). Two contacts
  with the shop's name refuse the run rather than pick one;
* one invoice per owing shop at that shop's total outstanding, number
  ``MZ-<shop_id>`` so the script and an owner-approved payment find it
  again, dated as the store dates the shop's oldest open delivery, then
  marked sent through ZOHO_BOOKS_MARK_INVOICE_AS_SENT, which sends no mail;
* with ``--clear-drafts``: the three leftover draft invoices, by number,
  never a posted one;
* with ``--clear-strays``: a contact that is not one of the demo shop names
  and has no invoice and no payment. Deleting an invoice first is what makes
  Zoho willing to delete its contact, and a contact with any transaction is
  skipped with the reason printed;
* with ``--reset``: only payments Mimi can attribute to an owner approval
  (reference ``mimi-apr_<digest>``), never a payment it cannot, and only
  when every invoice they touch is one this script seeded (``MZ-``). It
  prints each invoice's balance before and after.

Re-running any part changes nothing: contacts are found by exact name,
invoices by number, and every write is announced before it is made.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from mimi import owed
from mimi.api.zoho_match import SEED_INVOICE_PREFIX, norm_name, seed_invoice_number
from mimi.connectors.env import ConnectorSettings
from mimi.connectors.zoho.payments import books_url
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.world.loader import load_world

# The invoice number is shared with the payment path (zoho_match).
REFERENCE_PREFIX = "mimi-"
APPROVAL_REFERENCE = re.compile(r"^mimi-apr_[0-9a-f]{20}$")

# The leftover test data the owner approved removing, by exact number and
# name. Anything else is never deleted by this script.
DRAFT_INVOICES = ("INV-000001", "INV-000002", "INV-000003")

# The figures the dry run is checked against before any write. If the book
# or a rate has moved, the script stops and says so rather than adjust.
WHOLE_BOOK_KOBO = 1_256_035_000  # 40 shops, 24 owing, ₦12,560,350
FUNKE_SHOP = "shop-005"
FUNKE_KOBO = 221_120_000  # Funke & Sons Mini Mart, ₦2,211,200
DELIVERY_CHECK = ("dlv-00-0055", 400_000)  # ₦4,000, the filmed payment

ORG_ENV = "ZOHO_BOOKS_ORGANIZATION_ID"
ACCOUNT_ENV = "MIMI_DEMO_ZOHO_ACCOUNT"
USER_ENV = "MIMI_DEMO_ZOHO_USER"
DEFAULT_USER = "mimi-sandbox-owner"


class SeedRefused(RuntimeError):
    """Nothing was written; the message says what to fix."""


# ------------------------------------------------------------ the demo book


@dataclass(frozen=True)
class Debt:
    delivery_id: str
    delivered_on: str
    due_on: str
    outstanding_kobo: int


@dataclass(frozen=True)
class ShopDebt:
    """One owing shop: its total, and the first delivery it came from."""

    shop_id: str
    name: str
    phone: str
    debts: tuple[Debt, ...]

    @property
    def total_kobo(self) -> int:
        return sum(d.outstanding_kobo for d in self.debts)

    def first(self) -> Debt:
        return min(self.debts, key=lambda d: (d.delivered_on, d.delivery_id))

    def one_delivery(self, delivery_id: str) -> Debt | None:
        return next((d for d in self.debts if d.delivery_id == delivery_id), None)


def read_book(engine: Any, tenant: str) -> tuple[list[ShopDebt], set[str]]:
    """Owing shops from the store, one per shop, plus every shop name."""
    with engine.connect() as conn:
        names = {str(r.shop_id): (str(r.name or r.shop_id), str(r.phone or ""))
                 for r in conn.execute(sa.select(s.shops).where(
                     s.shops.c.tenant_id == tenant))}
        delivered = {str(r.delivery_id): str(r.delivered_on or "")
                     for r in conn.execute(sa.select(s.deliveries).where(
                         s.deliveries.c.tenant_id == tenant))}
        debts: dict[str, list[Debt]] = {}
        for r in conn.execute(sa.select(s.receivables).where(
                s.receivables.c.tenant_id == tenant,
                s.receivables.c.outstanding_kobo > 0)):
            sid = str(r.shop_id)
            debts.setdefault(sid, []).append(Debt(
                delivery_id=str(r.delivery_id),
                delivered_on=delivered.get(str(r.delivery_id), ""),
                due_on=str(r.due_on or ""),
                outstanding_kobo=int(r.outstanding_kobo)))
    shops = [ShopDebt(shop_id=sid, name=names.get(sid, (sid, ""))[0],
                      phone=names.get(sid, (sid, ""))[1], debts=tuple(rows))
             for sid, rows in debts.items()]
    return shops, {name for name, _phone in names.values()}


def demo_book() -> tuple[list[ShopDebt], set[str]]:
    """The book the console seeds for a demo session, from ``seed_world``."""
    world = load_world()
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    try:
        seed_world(engine, world)
        return read_book(engine, world.tenant_id)
    finally:
        engine.dispose()


def check_book(shops: Sequence[ShopDebt]) -> None:
    """Refuse unless the book is the one the console shows and the owner named."""
    total = sum(s.total_kobo for s in shops)
    if total != WHOLE_BOOK_KOBO:
        raise SeedRefused(
            f"The book totals {owed.naira(total)}, not {owed.naira(WHOLE_BOOK_KOBO)}; "
            "nothing was written. Fix the book first, do not adjust the seed.")
    funke = next((s for s in shops if s.shop_id == FUNKE_SHOP), None)
    if funke is None or funke.total_kobo != FUNKE_KOBO:
        raise SeedRefused(
            f"{FUNKE_SHOP} is {owed.naira(funke.total_kobo) if funke else 'missing'}, "
            f"not {owed.naira(FUNKE_KOBO)}; nothing was written.")
    delivery_id, kobo = DELIVERY_CHECK
    one = funke.one_delivery(delivery_id)
    if one is None or one.outstanding_kobo != kobo:
        raise SeedRefused(
            f"{delivery_id} is {owed.naira(one.outstanding_kobo) if one else 'missing'}, "
            f"not {owed.naira(kobo)}; nothing was written.")


# ------------------------------------------------------------ the Zoho side


def _plain(res: Any) -> dict[str, Any]:
    from mimi.connectors.composio import _plain as plain

    return plain(res)


def _ok(what: str, reply: dict[str, Any]) -> dict[str, Any]:
    code = reply.get("code", 0)
    if isinstance(code, int) and code != 0:
        raise SeedRefused(f"Zoho refused {what}: code {code}, "
                          f"{reply.get('message') or 'no message'}.")
    return reply


class LiveZoho:
    """Composio directly: reads and the real write slugs, creation by proxy.

    Deliberately outside ``mimi.connectors``: this is an ops script, and
    none of these writes belongs in Mimi's allowlist. The API key is read
    from the settings and never printed."""

    def __init__(self, *, organization_id: str, connection_id: str,
                 user_id: str, settings: ConnectorSettings | None = None) -> None:
        from composio import Composio

        conf = settings or ConnectorSettings.from_env()
        if not conf.composio_api_key:
            raise SeedRefused("COMPOSIO_API_KEY is not set; nothing was written.")
        self.org = organization_id
        self.connection = connection_id
        self.user = user_id
        self.region = conf.zoho_region
        self.version = conf.version_zoho_books
        self.client = Composio(api_key=conf.composio_api_key,
                               toolkit_versions={"ZOHO_BOOKS": self.version})

    # -- reads ------------------------------------------------------------
    def contacts(self) -> list[dict[str, Any]]:
        return self._paged("ZOHO_BOOKS_LIST_CONTACTS", "contacts")

    def invoices(self) -> list[dict[str, Any]]:
        return self._paged("ZOHO_BOOKS_LIST_INVOICES", "invoices")

    def payments(self) -> list[dict[str, Any]]:
        return self._paged("ZOHO_BOOKS_LIST_CUSTOMER_PAYMENTS", "customer_payments")

    def _paged(self, slug: str, key: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            reply = self._call(slug, {"page": page, "per_page": 200})
            batch = [r for r in (reply.get(key) or []) if isinstance(r, dict)]
            rows.extend(batch)
            ctx = reply.get("page_context") or {}
            if not batch or not ctx.get("has_more_page"):
                return rows
            page += 1

    def _call(self, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
        res = self.client.tools.execute(
            slug, user_id=self.user, connected_account_id=self.connection,
            version=self.version,
            arguments={"__region": self.region, "organization_id": self.org,
                       **arguments})
        return _ok(slug, _plain(res))

    def _proxy(self, method: str, path: str, body: dict[str, Any] | None = None,
               query: str = "") -> dict[str, Any]:
        endpoint = books_url(self.region, path, f"organization_id={self.org}{query}")
        res = self.client.tools.proxy(
            endpoint=endpoint, method=method, body=body or {},
            connected_account_id=self.connection)
        return _ok(f"{method} {path}", _plain(res))

    # -- writes -----------------------------------------------------------
    def create_contact(self, name: str, phone: str) -> dict[str, Any]:
        reply = self._proxy("POST", "/contacts", {
            "contact_name": name, "phone": phone, "contact_type": "customer"})
        contact = reply.get("contact") or {}
        if not isinstance(contact, dict) or not contact.get("contact_id"):
            raise SeedRefused(f"Zoho created no contact for {name}; stopping.")
        return dict(contact)

    def create_invoice(self, *, contact_id: str, number: str, amount_kobo: int,
                       date: str, due_date: str, name: str) -> dict[str, Any]:
        reply = self._proxy("POST", "/invoices", {
            "customer_id": contact_id,
            "date": date,
            "due_date": due_date,
            "invoice_number": number,
            "ignore_auto_number_generation": True,
            "line_items": [{"name": f"Outstanding, {name}",
                            "rate": round(int(amount_kobo) / 100, 2),
                            "quantity": 1}],
        })
        invoice = reply.get("invoice") or {}
        if not isinstance(invoice, dict) or not invoice.get("invoice_id"):
            raise SeedRefused(f"Zoho created no invoice {number}; stopping.")
        return dict(invoice)

    def mark_sent(self, invoice_id: str) -> None:
        self._call("ZOHO_BOOKS_MARK_INVOICE_AS_SENT", {"invoice_id": invoice_id})

    def delete_invoice(self, invoice_id: str) -> None:
        self._call("ZOHO_BOOKS_DELETE_INVOICE", {"invoice_id": invoice_id})

    def delete_contact(self, contact_id: str) -> None:
        self._call("ZOHO_BOOKS_DELETE_CONTACT", {"contact_id": contact_id})

    def delete_payment(self, payment_id: str) -> None:
        self._call("ZOHO_BOOKS_DELETE_CUSTOMER_PAYMENT", {"payment_id": payment_id})


class FakeZoho:
    """An in-memory Zoho Books for the tests and for ``--dry-run`` checks.

    It answers with Zoho's shapes and enforces the two rules that matter:
    a contact with any invoice cannot be deleted, and deleting a payment
    puts its applications back on the invoices."""

    def __init__(self, *, contacts: Sequence[dict[str, Any]] = (),
                 invoices: Sequence[dict[str, Any]] = (),
                 payments: Sequence[dict[str, Any]] = ()) -> None:
        self._contacts = [dict(c) for c in contacts]
        self._invoices = [dict(i) for i in invoices]
        self._payments = [dict(p) for p in payments]
        self.calls: list[dict[str, Any]] = []
        self._seq = 0

    def contacts(self) -> list[dict[str, Any]]:
        return [dict(c) for c in self._contacts]

    def invoices(self) -> list[dict[str, Any]]:
        return [dict(i) for i in self._invoices]

    def payments(self) -> list[dict[str, Any]]:
        return [dict(p) for p in self._payments]

    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def create_contact(self, name: str, phone: str) -> dict[str, Any]:
        self.calls.append({"write": "create_contact", "name": name, "phone": phone})
        row = {"contact_id": self._id("zc-real"), "contact_name": name, "phone": phone}
        self._contacts.append(row)
        return dict(row)

    def create_invoice(self, *, contact_id: str, number: str, amount_kobo: int,
                       date: str, due_date: str, name: str) -> dict[str, Any]:
        self.calls.append({"write": "create_invoice", "number": number,
                           "contact_id": contact_id, "amount_kobo": amount_kobo,
                           "date": date, "due_date": due_date})
        row = {"invoice_id": self._id("zi"), "invoice_number": number,
               "customer_id": contact_id, "customer_name": name,
               "date": date, "due_date": due_date, "status": "draft",
               "total": amount_kobo / 100, "balance": amount_kobo / 100}
        self._invoices.append(row)
        return dict(row)

    def mark_sent(self, invoice_id: str) -> None:
        self.calls.append({"write": "mark_sent", "invoice_id": invoice_id})
        self._invoice(invoice_id).update({"status": "sent"})

    def delete_invoice(self, invoice_id: str) -> None:
        self.calls.append({"write": "delete_invoice", "invoice_id": invoice_id})
        self._invoices = [i for i in self._invoices
                          if str(i.get("invoice_id")) != invoice_id]

    def delete_contact(self, contact_id: str) -> None:
        if any(str(i.get("customer_id")) == contact_id for i in self._invoices):
            raise SeedRefused("Zoho refused DELETE_CONTACT: the contact still has "
                              "an invoice.")
        self.calls.append({"write": "delete_contact", "contact_id": contact_id})
        self._contacts = [c for c in self._contacts
                          if str(c.get("contact_id")) != contact_id]

    def delete_payment(self, payment_id: str) -> None:
        self.calls.append({"write": "delete_payment", "payment_id": payment_id})
        payment = next(p for p in self._payments
                       if str(p.get("payment_id")) == payment_id)
        for applied in payment.get("invoices") or []:
            inv = self._invoice(str(applied.get("invoice_id")))
            back = float(applied.get("amount_applied") or 0)
            inv["balance"] = round(float(inv.get("balance") or 0) + back, 2)
            inv["status"] = "sent"
        self._payments = [p for p in self._payments if p is not payment]

    def _invoice(self, invoice_id: str) -> dict[str, Any]:
        return next(i for i in self._invoices
                    if str(i.get("invoice_id")) == invoice_id)

    def apply_payment(self, payment: dict[str, Any]) -> None:
        """What Zoho does with an approved payment: record it and take it off
        the invoices it names. The tests use this to film the real sequence."""
        self._payments.append(dict(payment))
        for applied in payment.get("invoices") or []:
            inv = self._invoice(str(applied.get("invoice_id")))
            taken = float(applied.get("amount_applied") or 0)
            inv["balance"] = round(float(inv.get("balance") or 0) - taken, 2)


# ------------------------------------------------------------ planning


@dataclass
class SeedPlan:
    contact_create: list[ShopDebt]
    contact_reuse: dict[str, str]  # shop_id -> contact_id
    invoice_create: list[tuple[ShopDebt, str]]
    invoice_present: list[tuple[ShopDebt, str]]
    draft_delete: list[dict[str, Any]]
    draft_skip: list[tuple[str, str]]
    stray_delete: list[dict[str, Any]]
    stray_skip: list[tuple[str, str]]
    total_kobo: int


def plan_seed(shops: Sequence[ShopDebt], snap: Any, demo_names: set[str], *,
              clear_drafts: bool = False,
              clear_strays: bool = False) -> SeedPlan:
    """What the run would write, computed before anything is touched."""
    contacts = snap.contacts()
    invoices = snap.invoices()
    payments = snap.payments()

    by_name: dict[str, list[dict[str, Any]]] = {}
    for c in contacts:
        by_name.setdefault(norm_name(c.get("contact_name")), []).append(c)
    contact_create, contact_reuse = [], {}
    for shop in shops:
        found = by_name.get(norm_name(shop.name), [])
        if len(found) > 1:
            raise SeedRefused(
                f"Zoho has {len(found)} contacts named {shop.name!r}; nothing was "
                "written. Merge them by hand first.")
        if found:
            contact_reuse[shop.shop_id] = str(found[0].get("contact_id"))
        else:
            contact_create.append(shop)

    numbers = {str(i.get("invoice_number")): i for i in invoices}
    invoice_create, invoice_present = [], []
    for shop in shops:
        number = seed_invoice_number(shop.shop_id)
        if number in numbers:
            invoice_present.append((shop, number))
        else:
            invoice_create.append((shop, number))

    draft_delete, draft_skip = [], []
    if clear_drafts:
        for number in DRAFT_INVOICES:
            found = numbers.get(number)
            if found is None:
                draft_skip.append((number, "not in Zoho"))
            elif str(found.get("status") or "").lower() != "draft":
                draft_skip.append((number, f"status {found.get('status')}, left alone"))
            else:
                draft_delete.append(found)

    stray_delete, stray_skip = [], []
    if clear_strays:
        gone = {str(i.get("invoice_id")) for i in draft_delete}
        invoiced = {str(i.get("customer_id")) for i in invoices
                    if str(i.get("invoice_id")) not in gone}
        paid = {str(p.get("customer_id")) for p in payments}
        demo = {norm_name(name) for name in demo_names}
        for c in contacts:
            cid = str(c.get("contact_id"))
            name = str(c.get("contact_name") or "")
            if norm_name(name) in demo:
                continue
            if cid in invoiced:
                stray_skip.append((name, "still has an invoice"))
                continue
            if cid in paid:
                stray_skip.append((name, "still has a payment"))
                continue
            stray_delete.append(c)

    return SeedPlan(contact_create=contact_create, contact_reuse=contact_reuse,
                    invoice_create=invoice_create, invoice_present=invoice_present,
                    draft_delete=draft_delete, draft_skip=draft_skip,
                    stray_delete=stray_delete, stray_skip=stray_skip,
                    total_kobo=sum(s.total_kobo for s in shops))


@dataclass
class ResetPlan:
    payments: list[dict[str, Any]]
    ignored: int
    before: dict[str, float]  # invoice number -> balance in naira


def plan_reset(snap: Any) -> ResetPlan:
    """Payments attributable to an owner approval, and nothing else."""
    invoices = snap.invoices()
    by_id = {str(i.get("invoice_id")): i for i in invoices}
    attributed: list[dict[str, Any]] = []
    refuse: list[str] = []
    ignored = 0
    for p in snap.payments():
        ref = str(p.get("reference_number") or "")
        if not ref.startswith(REFERENCE_PREFIX):
            ignored += 1
            continue
        if not APPROVAL_REFERENCE.match(ref):
            refuse.append(f"{ref} (not an approval reference)")
            continue
        attributed.append(p)
    before: dict[str, float] = {}
    for p in attributed:
        for applied in p.get("invoices") or []:
            inv = by_id.get(str(applied.get("invoice_id"))) or {}
            number = str(inv.get("invoice_number") or "")
            if not number.startswith(SEED_INVOICE_PREFIX):
                refuse.append(
                    f"payment {p.get('reference_number')} touches invoice "
                    f"{number or applied.get('invoice_id')}, which this script "
                    "did not seed")
                continue
            before[number] = float(inv.get("balance") or 0)
    if refuse:
        raise SeedRefused("Cannot attribute every payment, so nothing was "
                          "deleted: " + "; ".join(refuse) + ".")
    return ResetPlan(payments=attributed, ignored=ignored, before=before)


# ------------------------------------------------------------ printing


def _say(line: str = "") -> None:
    print(line)


def _naira_float(amount: float) -> str:
    return owed.naira(int(round(float(amount) * 100)))


def print_seed_plan(plan: SeedPlan, shops: Sequence[ShopDebt], *,
                    clear_strays: bool) -> None:
    _say("DRY RUN: nothing will be written. Pass --yes to perform it.")
    _say(f"Demo book: {len(shops)} owing shops, total {owed.naira(plan.total_kobo)}")
    _say(f"Contacts: {len(plan.contact_create)} to create, "
         f"{len(plan.contact_reuse)} reused (never edited)")
    for shop in plan.contact_create:
        _say(f"  would create contact {shop.name} ({shop.phone})")
    for shop in shops:
        if shop.shop_id in plan.contact_reuse:
            _say(f"  reuse contact {shop.name} "
                 f"({plan.contact_reuse[shop.shop_id]})")
    _say(f"Invoices: {len(plan.invoice_create)} to create, "
         f"{len(plan.invoice_present)} already there. Mark sent through "
         "ZOHO_BOOKS_MARK_INVOICE_AS_SENT: Zoho marks them sent and sends no email.")
    for shop, number in plan.invoice_create:
        first = shop.first()
        _say(f"  would create invoice {number} for {shop.name}: "
             f"{owed.naira(shop.total_kobo)} dated {first.delivered_on} "
             f"(due {first.due_on}), then mark it sent")
    for shop, number in plan.invoice_present:
        _say(f"  skip invoice {number} for {shop.name}: already in Zoho")
    if plan.draft_delete:
        _say(f"Draft invoices to delete: {len(plan.draft_delete)}")
    for inv in plan.draft_delete:
        _say(f"  would delete draft invoice {inv.get('invoice_number')} "
             f"({_naira_float(float(inv.get('balance') or 0))})")
    for number, why in plan.draft_skip:
        _say(f"  skip draft {number}: {why}")
    if plan.stray_delete or plan.stray_skip:
        _say(f"Stray contacts to delete: {len(plan.stray_delete)}")
    for c in plan.stray_delete:
        _say(f"  would delete contact {c.get('contact_name')} "
             f"({c.get('contact_id')})")
    for name, why in plan.stray_skip:
        _say(f"  skip contact {name}: {why}")
    if clear_strays:
        _say("A contact is deletable only with no invoice and no payment left.")
    assets = sum(s.total_kobo for s, _n in plan.invoice_create)
    _say(f"This run would write {owed.naira(assets)} of invoices; the whole "
         f"book is {owed.naira(plan.total_kobo)}.")


def print_reset_plan(plan: ResetPlan) -> None:
    _say("DRY RUN: nothing will be deleted. Pass --yes to perform it.")
    _say(f"Payments: {len(plan.payments)} attributable to an owner approval, "
         f"{plan.ignored} not Mimi's (left alone)")
    for p in plan.payments:
        _say(f"  would delete payment {p.get('payment_id')} "
             f"{_naira_float(float(p.get('amount') or 0))} "
             f"ref {p.get('reference_number')} on {p.get('date')}")
    for number, balance in sorted(plan.before.items()):
        _say(f"  invoice {number}: {_naira_float(balance)} now, goes back by the "
             "payment once it is deleted")


# ------------------------------------------------------------ running


def run_seed(shops: Sequence[ShopDebt], zoho: Any, demo_names: set[str], *,
             yes: bool, clear_drafts: bool = False,
             clear_strays: bool = False) -> dict[str, int]:
    plan = plan_seed(shops, zoho, demo_names, clear_drafts=clear_drafts,
                     clear_strays=clear_strays)
    counts = {"contacts": 0, "invoices": 0, "drafts": 0, "contacts_deleted": 0,
              "reused": len(plan.contact_reuse)}
    if not yes:
        print_seed_plan(plan, shops, clear_strays=clear_strays)
        return counts
    _say(f"Demo book: {len(shops)} owing shops, total {owed.naira(plan.total_kobo)}")
    contact_id = dict(plan.contact_reuse)
    for shop in plan.contact_create:
        row = zoho.create_contact(shop.name, shop.phone)
        contact_id[shop.shop_id] = str(row["contact_id"])
        counts["contacts"] += 1
        _say(f"  created contact {shop.name} ({row['contact_id']})")
    for shop, number in plan.invoice_create:
        first = shop.first()
        row = zoho.create_invoice(contact_id=contact_id[shop.shop_id], number=number,
                                  amount_kobo=shop.total_kobo,
                                  date=first.delivered_on, due_date=first.due_on,
                                  name=shop.name)
        zoho.mark_sent(str(row["invoice_id"]))
        counts["invoices"] += 1
        _say(f"  created invoice {number} for {shop.name} "
             f"{owed.naira(shop.total_kobo)}, marked sent")
    for inv in plan.draft_delete:
        zoho.delete_invoice(str(inv["invoice_id"]))
        counts["drafts"] += 1
        _say(f"  deleted draft invoice {inv.get('invoice_number')}")
    if clear_strays and plan.stray_delete:
        # Re-read after the deletes: a contact that gained a transaction in
        # between must not go, whatever the earlier plan said.
        fresh = plan_seed(shops, zoho, demo_names, clear_drafts=False,
                          clear_strays=True)
        allowed = {str(c.get("contact_id")) for c in fresh.stray_delete}
        for c in plan.stray_delete:
            cid = str(c.get("contact_id"))
            if cid not in allowed:
                _say(f"  skip contact {c.get('contact_name')}: it gained a "
                     "transaction")
                continue
            zoho.delete_contact(cid)
            counts["contacts_deleted"] += 1
            _say(f"  deleted contact {c.get('contact_name')} ({cid})")
    for name, why in plan.stray_skip:
        _say(f"  skip contact {name}: {why}")
    for number, why in plan.draft_skip:
        _say(f"  skip draft {number}: {why}")
    _say(f"Done: {counts['contacts']} contacts created, {counts['reused']} reused, "
         f"{counts['invoices']} invoices created, {counts['drafts']} drafts "
         f"deleted, {counts['contacts_deleted']} stray contacts deleted.")
    return counts


def run_reset(zoho: Any, *, yes: bool) -> dict[str, int]:
    plan = plan_reset(zoho)
    counts = {"payments": 0, "ignored": plan.ignored}
    if not yes:
        print_reset_plan(plan)
        return counts
    _say(f"Payments: {len(plan.payments)} attributable to an owner approval, "
         f"{plan.ignored} not Mimi's (left alone)")
    for p in plan.payments:
        zoho.delete_payment(str(p.get("payment_id")))
        counts["payments"] += 1
        _say(f"  deleted payment {p.get('payment_id')} "
             f"{_naira_float(float(p.get('amount') or 0))} "
             f"ref {p.get('reference_number')}")
    after = {str(i.get("invoice_number")): float(i.get("balance") or 0)
             for i in zoho.invoices()}
    for number, before in sorted(plan.before.items()):
        _say(f"  invoice {number}: {_naira_float(before)} -> "
             f"{_naira_float(after.get(number, 0.0))}")
    _say(f"Done: {counts['payments']} payments deleted, every seeded invoice "
         "back to its balance.")
    return counts


# ------------------------------------------------------------ command line


def env_target() -> tuple[str, str, str]:
    org = os.environ.get(ORG_ENV, "").strip()
    account = os.environ.get(ACCOUNT_ENV, "").strip()
    user = os.environ.get(USER_ENV, "").strip() or DEFAULT_USER
    if not org:
        raise SeedRefused(f"{ORG_ENV} is not set; nothing was written.")
    if not account:
        raise SeedRefused(f"{ACCOUNT_ENV} is not set; nothing was written.")
    return org, account, user


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed Coral Organization with the demo book, or reset it.")
    parser.add_argument("--yes", action="store_true",
                        help="perform the writes (default is a dry run)")
    parser.add_argument("--clear-drafts", action="store_true",
                        help="also delete the leftover draft invoices by number")
    parser.add_argument("--clear-strays", action="store_true",
                        help="also delete contacts with no invoice and no payment "
                             "that are not demo shops")
    parser.add_argument("--reset", action="store_true",
                        help="delete the demo's own payments so the invoices go back")
    parser.add_argument("--db-url", default="",
                        help="read the book from this store instead of seeding one")
    parser.add_argument("--tenant", default="",
                        help="the tenant to read with --db-url")
    args = parser.parse_args(argv)
    try:
        org, account, user = env_target()
        zoho = LiveZoho(organization_id=org, connection_id=account, user_id=user)
        if args.reset:
            if args.clear_drafts or args.clear_strays:
                raise SeedRefused("--reset does not delete invoices or contacts; "
                                  "run the seed cleanup separately.")
            counts = run_reset(zoho, yes=args.yes)
            return 0 if counts["payments"] or not args.yes else 0
        if args.db_url:
            engine = make_engine(args.db_url)
            try:
                shops, names = read_book(engine, args.tenant)
            finally:
                engine.dispose()
        else:
            shops, names = demo_book()
        check_book(shops)
        run_seed(shops, zoho, names, yes=args.yes,
                 clear_drafts=args.clear_drafts, clear_strays=args.clear_strays)
        return 0
    except SeedRefused as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

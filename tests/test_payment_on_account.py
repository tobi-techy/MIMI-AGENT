"""A payment bigger than the shop owes (feat/payment-on-account).

Two halves, both from the same finding. The Zoho card for a verified payment
proposed the whole amount against one invoice, which in a real Zoho Books org is
a real over-application; and the excess of a payment bigger than the balance was
dropped, while the ``payments`` row kept what the bank sent, so two tables in our
own book disagreed by it and nothing complained.

What is asserted here: the card's invoice list never asks for more than the
invoices owed, the excess is a ``credit_notes`` row on the shop's account
(0017's ``on_account_kobo``, no new table, no new revision), the payment row and
the receivables add up, and the shop read shows what is held on its account.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mimi.api import demo_wiring
from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.api.week.go import GoSim
from mimi.clock import wall
from mimi.connectors.zoho import payments as zp
from mimi.harness.gateway import ScriptedModel
from mimi.store import ensure_schema, make_engine, on_account, seed_world
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.world.loader import load_world
from tests.test_api import script

W = load_world()
A = "coral-lane"
OWNER = "s-owner-key-000000000001"
SHOP = "shop-001"  # the most open invoices in the seeded world
SHOP2 = "shop-002"


@pytest.fixture()
def book(store_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("MIMI_DEMO_WIRING", "1")
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W, stock_demo=True)
    agent = Agent(engine, models=[ScriptedModel(script)], worker_id="test-worker")
    app = create_app(Settings(keys=parse_keys(f"{A}:owner:{OWNER}"), wait_s=5), agent, workers=0)
    with TestClient(app) as client:
        yield {"c": client, "engine": engine}
    engine.dispose()


def h() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER}"}


def _open(engine: Any, shop: str = SHOP) -> list[dict[str, Any]]:
    return SqlGo(engine).receivables(A, shop)


def _likely_payment(engine: Any, shop: str, amount: int, *, payment_id: str,
                    delivery_id: str = "") -> str:
    """The screenshot payment a bank credit would verify: a payment row that is
    ``likely`` and not a fake alert."""
    with engine.begin() as conn:
        conn.execute(s.payments.insert().values(
            tenant_id=A, payment_id=payment_id, shop_id=shop, delivery_id=delivery_id,
            day=60, paid_on="2026-10-26", amount_kobo=int(amount), method="transfer",
            reference="TRF-0001", status="matched", partial=False, fake_alert=False,
            proof_level="likely", witnesses="[]", proof_policy_version="v1"))
    return payment_id


def _card(c: TestClient, approval_id: str) -> dict[str, Any]:
    item = next(x for x in c.get("/v1/approvals", headers=h()).json()["approvals"]
                if x["approval_id"] == approval_id)
    assert item["kind"] == "zoho_payment" and item["status"] == "pending"
    return item


def _note(conn: sa.Connection, payment_id: str) -> Any:
    return conn.execute(sa.select(s.credit_notes).where(
        s.credit_notes.c.tenant_id == A,
        s.credit_notes.c.credit_id == on_account.credit_id_for(A, payment_id))).first()


def _delivery_of(invoice_id: str) -> str:
    return "dlv-" + invoice_id.removeprefix("INV-")


# ------------------------------------------------- the cap, invoice by invoice


def test_a_payment_bigger_than_the_shop_owes_caps_the_card_and_records_the_excess(
        book: dict[str, Any]) -> None:
    engine, c = book["engine"], book["c"]
    rows = _open(engine)
    owed_by = {r["delivery_id"]: r["outstanding_kobo"] for r in rows}
    total = sum(owed_by.values())
    excess = 5_000_000
    payment = _likely_payment(engine, SHOP, total + excess, payment_id="pay-over-1")
    with engine.begin() as conn:
        got = demo_wiring.bank_credit(conn, A, SHOP, event_id="ev-over", amount_ngn=None,
                                      now=wall())
    assert got["matched"] is True
    item = _card(c, str(got["approval_id"]))
    payload = item["payload"]
    assert payload["amount_kobo"] == total + excess  # what the bank sent
    assert payload["applied_kobo"] == total  # what the invoices could absorb
    assert payload["on_account_kobo"] == excess
    applied = {i["invoice_id"]: i["amount_applied_kobo"] for i in payload["invoices"]}
    assert sum(applied.values()) == total
    # The cap, invoice by invoice: never more than that invoice owed.
    for invoice_id, amount in applied.items():
        assert amount == owed_by[_delivery_of(invoice_id)], invoice_id
    assert sum(applied.values()) <= sum(owed_by.values())
    assert _open(engine) == []  # every invoice it owed is settled
    with engine.connect() as conn:
        note = _note(conn, payment)
        assert note is not None
        assert int(note.on_account_kobo) == excess
        assert int(note.applied_kobo) == total
        assert note.return_id == ""  # a payment credit has no return
        assert note.approval_id == got["approval_id"]
        assert on_account.for_shop_kobo(conn, A, SHOP) == excess
        pay = conn.execute(sa.select(s.payments).where(
            s.payments.c.tenant_id == A, s.payments.c.payment_id == payment)).first()
        assert int(pay.amount_kobo) == total + excess  # the row keeps what arrived
        # The two tables add up: what arrived is what applied plus what is held.
        assert int(note.applied_kobo) + int(note.on_account_kobo) == int(pay.amount_kobo)
    detail = c.get(f"/v1/shops/{SHOP}", headers=h()).json()["shop"]
    assert detail["outstanding_kobo"] == 0
    assert detail["on_account_kobo"] == excess and "\u20a6" in detail["on_account_naira"]
    body = zp.create_body(payload, f"zoho_payment:{payment}")
    assert body["amount"] == (total + excess) / 100
    assert sum(i["amount_applied"] for i in body["invoices"]) <= body["amount"]
    assert "stays on the shop's account" in item["preview"]
    assert "\u2014" not in item["preview"] and "\u2013" not in item["preview"]


def test_a_payment_bigger_than_one_invoice_spreads_over_the_next(
        book: dict[str, Any]) -> None:
    engine, c = book["engine"], book["c"]
    rows = _open(engine)
    first, second = rows[0], rows[1]
    payment = _likely_payment(engine, SHOP, first["outstanding_kobo"] + 1_000,
                              payment_id="pay-spread-1", delivery_id=first["delivery_id"])
    with engine.begin() as conn:
        got = demo_wiring.bank_credit(conn, A, SHOP, event_id="ev-spread", amount_ngn=None,
                                      now=wall())
    payload = _card(c, str(got["approval_id"]))["payload"]
    assert payload["on_account_kobo"] == 0  # it owed this much
    applied = {i["invoice_id"]: i["amount_applied_kobo"] for i in payload["invoices"]}
    assert applied[payload["invoice_number"]] == first["outstanding_kobo"]
    assert sum(applied.values()) == first["outstanding_kobo"] + 1_000
    assert applied[_invoice_of(second["delivery_id"])] == 1_000
    with engine.connect() as conn:
        assert _note(conn, payment) is None  # nothing held on account
        assert on_account.for_shop_kobo(conn, A, SHOP) == 0


def _invoice_of(delivery_id: str) -> str:
    return demo_wiring.invoice_number(delivery_id)


# ------------------------------------------------------------- Go's own writer


def test_the_week_sim_records_the_excess_too(book: dict[str, Any]) -> None:
    """Go's own writer takes a payment off one invoice (its matching rule), so
    what is left of a bigger payment used to vanish. It is on the account now,
    the payment row keeps what the bank sent, and the other invoices still stand
    where they were."""
    engine = book["engine"]
    rows = _open(engine, SHOP2)
    first, owed = rows[0], sum(r["outstanding_kobo"] for r in rows)
    excess = 2_500_000
    payment = GoSim(engine, W, A).cash_to_rep(SHOP2, first["outstanding_kobo"] + excess, 60)
    with engine.connect() as conn:
        note = _note(conn, payment)
        assert note is not None and int(note.on_account_kobo) == excess
        assert int(note.applied_kobo) == first["outstanding_kobo"]
        assert on_account.for_shop_kobo(conn, A, SHOP2) == excess
        pay = conn.execute(sa.select(s.payments).where(
            s.payments.c.tenant_id == A, s.payments.c.payment_id == payment)).first()
        assert int(pay.amount_kobo) == first["outstanding_kobo"] + excess
        # The two tables add up: what arrived is what applied plus what is held.
        assert int(note.applied_kobo) + int(note.on_account_kobo) == int(pay.amount_kobo)
    left = _open(engine, SHOP2)
    assert first["delivery_id"] not in {r["delivery_id"] for r in left}
    assert sum(r["outstanding_kobo"] for r in left) == owed - first["outstanding_kobo"]


def test_a_payment_that_fits_records_nothing_on_account(book: dict[str, Any]) -> None:
    engine = book["engine"]
    first = _open(engine, SHOP2)[0]
    payment = GoSim(engine, W, A).cash_to_rep(SHOP2, first["outstanding_kobo"], 60)
    with engine.connect() as conn:
        assert _note(conn, payment) is None
        assert on_account.for_shop_kobo(conn, A, SHOP2) == 0


def test_the_preview_names_what_stays_on_the_account() -> None:
    held = zp.preview_payment({"amount_kobo": 50_000_000, "on_account_kobo": 5_000_000,
                               "shop_name": "Mama Sola"})
    assert held == ("Record Mama Sola's \u20a6500,000 transfer in Zoho Books. "
                    "\u20a650,000 of it stays on the shop's account: it owed less than that.")
    assert "\u2014" not in held and "\u2013" not in held

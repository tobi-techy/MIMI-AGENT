"""The returns ledger (feat/returns-and-exposure): lifecycle, the three
quantities, settlement against the shop's balance with undo, tenant
filtering, and the unhappy paths. Code only: no model call anywhere."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mimi.api import returns as returns_api
from mimi.api.agent import Agent, NotFound
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.harness.gateway import ScriptedModel
from mimi.returns import ledger
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.world.loader import load_world
from tests.test_api import script

W = load_world()
A = "coral-lane"
B = "second-lane"
OWNER = "s-owner-key-000000000001"
OTHER = "s-other-key-00000000001"
ID_RE = re.compile(r"\b(?:shop|dlv|apr|ret|stn|crn|stk)-?\d|\bshop_\w|_kobo")
SKU = W.skus[0].sku
PRICE = W.skus[0].price_kobo


@pytest.fixture()
def api(store_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("MIMI_DEMO_WIRING", "1")
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W, stock_demo=True)
    seed_world(engine, W, tenant_id=B)
    agent = Agent(engine, models=[ScriptedModel(script)], worker_id="test-worker")
    app = create_app(Settings(keys=parse_keys(f"{A}:owner:{OWNER},{B}:owner:{OTHER}"),
                              wait_s=5), agent, workers=0)
    with TestClient(app) as client:
        yield {"c": client, "engine": engine, "agent": agent}
    engine.dispose()


def h() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER}"}


def h2() -> dict[str, str]:
    return {"Authorization": f"Bearer {OTHER}"}


def _pickup(c: TestClient) -> dict[str, Any]:
    c.post("/v1/stock/slow-check", headers=h())
    apps = c.get("/v1/approvals", headers=h()).json()["approvals"]
    return next(a for a in apps if a["kind"] == "slow_stock" and any(
        o["action"] == "pickup" for o in a["payload"]["options"]))


def _open_manual(c: TestClient, shop_id: str, quantity: int = 3) -> dict[str, Any]:
    r = c.post("/v1/returns", headers=h(), json={
        "shop_id": shop_id, "reason": "Too much of it in the shop",
        "lines": [{"sku": SKU, "quantity": quantity, "reason": "Not selling"}]})
    assert r.status_code == 201, r.text
    return r.json()


def _walk(c: TestClient, body: dict[str, Any], *, back: int | None = None,
          disposition: str = "resell", condition: str = "resellable") -> dict[str, Any]:
    rid = body["return_id"]
    assert c.post(f"/v1/returns/{rid}/approve", headers=h(), json={}).status_code == 200
    collected = [{"line_no": ln["line_no"], "quantity": back} for ln in body["lines"]] \
        if back is not None else []
    r = c.post(f"/v1/returns/{rid}/collect", headers=h(), json={"lines": collected})
    assert r.status_code == 200, r.text
    got = r.json()
    lines = [{"line_no": ln["line_no"],
              "quantity_returned": (back if back is not None else ln["quantity_collected"]),
              "condition": condition, "disposition": disposition} for ln in got["lines"]]
    r = c.post(f"/v1/returns/{rid}/inspect", headers=h(), json={"lines": lines})
    assert r.status_code == 200, r.text
    return r.json()


def _no_balance_shop(c: TestClient) -> str:
    shops = c.get("/v1/shops", headers=h()).json()["shops"]
    return next(x["shop_id"] for x in shops if x["outstanding_kobo"] == 0)


# ------------------------------------------------------------- pure ledger


def test_lifecycle_transitions_and_scrub() -> None:
    assert ledger.can_move(ledger.DRAFTED, ledger.APPROVED)
    assert ledger.can_move(ledger.DRAFTED, ledger.CANCELLED)
    assert not ledger.can_move(ledger.DRAFTED, ledger.COLLECTED)
    assert ledger.can_move(ledger.SETTLED, ledger.INSPECTED)  # an undone credit
    assert not ledger.can_move(ledger.CANCELLED, ledger.APPROVED)
    assert ledger.STATUSES == ("drafted", "approved", "collected", "inspected", "settled",
                               "cancelled")
    dirty = "Return for shop-003 from apr_abc123 worth 12000_kobo \u2014 total"
    clean = ledger.scrub(dirty)
    assert "shop-003" not in clean and "apr_abc123" not in clean and "kobo" not in clean
    assert "\u2014" not in clean


def test_credit_plan_is_oldest_first_and_holds_the_rest_on_account() -> None:
    rows = [{"delivery_id": "dlv-b", "outstanding_kobo": 5_000, "position": 2},
            {"delivery_id": "dlv-a", "outstanding_kobo": 3_000, "position": 1}]
    apps, applied, on_account = ledger.credit_plan(6_000, rows)
    assert [a["delivery_id"] for a in apps] == ["dlv-a", "dlv-b"]
    assert applied == 6_000 and on_account == 0
    apps, applied, on_account = ledger.credit_plan(20_000, rows)
    assert applied == 8_000 and on_account == 12_000
    assert ledger.credit_plan(0, rows) == ([], 0, 0)


def test_recovered_and_saving_count_only_what_sells_again() -> None:
    lines = [{"disposition": "resell", "value_returned_kobo": 10_000},
             {"disposition": "write_off", "value_returned_kobo": 4_000},
             {"disposition": "return_to_supplier", "value_returned_kobo": 6_000}]
    assert ledger.recovered_kobo(lines) == 16_000
    assert ledger.saving_kobo(16_000, 5_000) == 11_000
    assert ledger.saving_kobo(16_000, None) is None
    assert "no saving is claimed" in ledger.saving_basis(16_000, None)


# ------------------------------------------------------------- opening


def test_pickup_approval_opens_a_drafted_return_with_invoice_expiry(
        api: dict[str, Any]) -> None:
    c = api["c"]
    a = _pickup(c)
    ok = c.post(f"/v1/approvals/{a['approval_id']}/approve", json={"option": "pickup"},
                headers=h())
    assert ok.status_code == 200, ok.text
    body = c.get("/v1/returns", headers=h()).json()
    assert body["total"] == 1
    ret = body["returns"][0]
    assert ret["status"] == "drafted" and ret["source"] == "slow_stock"
    assert ret["shop_id"] == a["shop_id"] and ret["approval_id"] == a["approval_id"]
    ln = ret["lines"][0]
    assert ln["sku"] == a["payload"]["sku"]
    assert ln["quantity_collected"] is None and ln["quantity_returned"] is None
    assert ln["value_drafted_kobo"] == ln["quantity_drafted"] * ln["unit_price_kobo"]
    assert ln["expiry_source"] in ("invoice", "estimated")
    assert not ID_RE.search(ret["reason"]) and "\u2014" not in ret["reason"]
    assert [t["status"] for t in ret["transitions"]] == ["drafted"]
    # Opening from the same pickup again returns the one draft.
    again = c.post("/v1/returns", headers=h(), json={"approval_id": a["approval_id"]})
    assert again.status_code == 201 and again.json()["return_id"] == ret["return_id"]
    assert c.get("/v1/returns", headers=h()).json()["total"] == 1


def test_a_pickup_that_was_never_approved_opens_nothing(api: dict[str, Any]) -> None:
    c = api["c"]
    a = _pickup(c)  # still pending
    r = c.post("/v1/returns", headers=h(), json={"approval_id": a["approval_id"]})
    assert r.status_code == 409
    assert "approve" in r.json()["detail"].lower()
    assert c.get("/v1/returns", headers=h()).json()["total"] == 0


def test_a_non_pickup_plan_opens_nothing(api: dict[str, Any]) -> None:
    c = api["c"]
    c.post("/v1/stock/slow-check", headers=h())
    apps = c.get("/v1/approvals", headers=h()).json()["approvals"]
    other = next(a for a in apps if a["kind"] == "slow_stock" and not any(
        o["action"] == "pickup" for o in a["payload"]["options"]))
    c.post(f"/v1/approvals/{other['approval_id']}/approve", json={"option": "promo"},
           headers=h())
    r = c.post("/v1/returns", headers=h(), json={"approval_id": other["approval_id"]})
    assert r.status_code == 409 and "pickup" in r.json()["detail"]


# ------------------------------------------------------------- the lifecycle


def test_full_lifecycle_settles_against_the_balance_and_undoes(api: dict[str, Any]) -> None:
    c, engine = api["c"], api["engine"]
    owes = next(x for x in c.get("/v1/shops", headers=h()).json()["shops"]
                if x["outstanding_kobo"] > 2 * PRICE)
    body = _open_manual(c, owes["shop_id"], quantity=2)
    inspected = _walk(c, body)
    assert inspected["status"] == "inspected"
    assert inspected["value_returned_kobo"] == 2 * PRICE
    assert inspected["recovered_kobo"] == 2 * PRICE
    rid = body["return_id"]
    before = {x["delivery_id"]: x["outstanding_kobo"]
              for x in SqlGo(engine).receivables(A, owes["shop_id"])}
    r = c.post(f"/v1/returns/{rid}/settle", headers=h(), json={})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["status"] == "inspected"  # waits for approval
    assert got["credit_kobo"] == 2 * PRICE and got["credit_approval_id"]
    apr = got["credit_approval_id"]
    card = c.get("/v1/approvals", headers=h()).json()["approvals"]
    item = next(x for x in card if x["approval_id"] == apr)
    assert item["kind"] == "return_credit" and item["tool"] == "return_credit"
    assert item["status"] == "pending"
    for key in ("return_id", "shop_id", "shop_name", "amount_kobo", "amount_ngn", "currency",
                "reason", "lines", "applied_kobo", "on_account_kobo", "invoices", "date"):
        assert key in item["payload"], key
    assert item["payload"]["amount_kobo"] == 2 * PRICE
    assert not ID_RE.search(item["preview"])
    # Approve: the credit comes off the invoices, the return settles.
    ok = c.post(f"/v1/approvals/{apr}/approve", headers=h(), json={})
    assert ok.status_code == 200, ok.text
    done = ok.json()["approval"]
    assert done["result"]["undo_available"] is True and done["result"]["credit_id"]
    settled = c.get(f"/v1/returns/{rid}", headers=h()).json()
    assert settled["status"] == "settled"
    assert settled["credit_applied_kobo"] == 2 * PRICE
    assert settled["credit_on_account_kobo"] == 0
    after = {x["delivery_id"]: x["outstanding_kobo"]
             for x in SqlGo(engine).receivables(A, owes["shop_id"])}
    assert sum(before.values()) - sum(after.values()) == settled["credit_applied_kobo"]
    # Undo puts it all back.
    un = c.post(f"/v1/approvals/{apr}/undo", headers=h())
    assert un.status_code == 200, un.text
    back = c.get(f"/v1/returns/{rid}", headers=h()).json()
    assert back["status"] == "inspected"
    restored = {x["delivery_id"]: x["outstanding_kobo"]
                for x in SqlGo(engine).receivables(A, owes["shop_id"])}
    assert restored == before
    assert c.post(f"/v1/approvals/{apr}/undo", headers=h()).status_code == 409


def test_a_return_on_a_shop_with_no_balance_sits_on_its_account(
        api: dict[str, Any]) -> None:
    c, engine = api["c"], api["engine"]
    shop_id = _no_balance_shop(c)
    body = _open_manual(c, shop_id, quantity=1)
    inspected = _walk(c, body)
    r = c.post(f"/v1/returns/{body['return_id']}/settle", headers=h(), json={})
    assert r.status_code == 200, r.text
    apr = r.json()["credit_approval_id"]
    item = next(x for x in c.get("/v1/approvals", headers=h()).json()["approvals"]
                if x["approval_id"] == apr)
    assert item["payload"]["applied_kobo"] == 0
    assert item["payload"]["on_account_kobo"] == inspected["value_returned_kobo"]
    c.post(f"/v1/approvals/{apr}/approve", headers=h(), json={})
    assert SqlGo(engine).receivables(A, shop_id) == []
    got = c.get(f"/v1/returns/{body['return_id']}", headers=h()).json()
    assert got["status"] == "settled"
    assert got["credit_on_account_kobo"] == inspected["value_returned_kobo"]


def test_saving_uses_the_owner_configured_pickup_cost(api: dict[str, Any]) -> None:
    c = api["c"]
    got = c.get("/v1/returns/settings", headers=h()).json()
    assert got["pickup_cost_kobo"] is None
    assert "collection trip" in got["note"] and "guessed" in got["note"]
    put = c.put("/v1/returns/settings", headers=h(), json={"pickup_cost_kobo": 500_000})
    assert put.status_code == 200 and put.json()["pickup_cost_kobo"] == 500_000
    shop_id = _no_balance_shop(c)
    body = _open_manual(c, shop_id, quantity=2)
    inspected = _walk(c, body, disposition="resell")
    assert inspected["recovered_kobo"] == 2 * PRICE
    assert inspected["pickup_cost_kobo"] == 500_000
    assert inspected["saving_kobo"] == 2 * PRICE - 500_000
    assert "\u20a6" in inspected["saving_basis"]
    written = _walk(c, _open_manual(c, shop_id, quantity=1), disposition="write_off",
                    condition="damaged")
    assert written["recovered_kobo"] == 0
    assert written["saving_kobo"] == -500_000


# ------------------------------------------------------------- unhappy paths


def test_inspection_that_disagrees_with_what_was_collected(api: dict[str, Any]) -> None:
    c = api["c"]
    body = _open_manual(c, _no_balance_shop(c), quantity=5)
    rid = body["return_id"]
    c.post(f"/v1/returns/{rid}/approve", headers=h(), json={})
    c.post(f"/v1/returns/{rid}/collect", headers=h(),
           json={"lines": [{"line_no": 1, "quantity": 2}]})
    got = c.get(f"/v1/returns/{rid}", headers=h()).json()
    assert got["lines"][0]["quantity_collected"] == 2
    over = c.post(f"/v1/returns/{rid}/inspect", headers=h(), json={"lines": [
        {"line_no": 1, "quantity_returned": 3, "condition": "resellable",
         "disposition": "resell"}]})
    assert over.status_code == 409 and "came back" in over.json()["detail"]
    missing = c.post(f"/v1/returns/{rid}/inspect", headers=h(), json={"lines": []})
    assert missing.status_code == 409
    short = c.post(f"/v1/returns/{rid}/inspect", headers=h(), json={"lines": [
        {"line_no": 1, "quantity_returned": 2, "condition": "resellable",
         "disposition": "resell"}]})
    assert short.status_code == 200 and short.json()["value_returned_kobo"] == 2 * PRICE


def test_settlement_cannot_exceed_the_return_value(api: dict[str, Any]) -> None:
    c = api["c"]
    body = _open_manual(c, _no_balance_shop(c), quantity=1)
    inspected = _walk(c, body)
    value = inspected["value_returned_kobo"]
    over = c.post(f"/v1/returns/{body['return_id']}/settle", headers=h(),
                  json={"amount_kobo": value + 1})
    assert over.status_code == 409 and "more than" in over.json()["detail"]
    zero = c.post(f"/v1/returns/{body['return_id']}/settle", headers=h(),
                  json={"amount_kobo": 0})
    assert zero.status_code == 422  # the schema refuses it before the ledger


def test_transitions_out_of_order_are_refused(api: dict[str, Any]) -> None:
    c = api["c"]
    body = _open_manual(c, _no_balance_shop(c))
    rid = body["return_id"]
    assert c.post(f"/v1/returns/{rid}/collect", headers=h(), json={}).status_code == 409
    assert c.post(f"/v1/returns/{rid}/inspect", headers=h(), json={"lines": []}).status_code == 409
    assert c.post(f"/v1/returns/{rid}/settle", headers=h(), json={}).status_code == 409
    assert c.post(f"/v1/returns/{rid}/cancel", headers=h(), json={}).status_code == 200
    assert c.get(f"/v1/returns/{rid}", headers=h()).json()["status"] == "cancelled"


def test_opening_needs_a_pickup_or_a_shop_and_lines(api: dict[str, Any]) -> None:
    c = api["c"]
    assert c.post("/v1/returns", headers=h(), json={}).status_code == 409
    bad = c.post("/v1/returns", headers=h(), json={
        "shop_id": "shop-001", "lines": [{"sku": "NOPE", "quantity": 1}]})
    assert bad.status_code == 409 and "book" in bad.json()["detail"]
    assert c.get("/v1/returns/ret_nope", headers=h()).status_code == 404


# ------------------------------------------------------------- tenancy and ids


def test_returns_are_tenant_filtered(api: dict[str, Any]) -> None:
    c = api["c"]
    body = _open_manual(c, _no_balance_shop(c))
    assert c.get("/v1/returns", headers=h2()).json() == {"returns": [], "total": 0}
    assert c.get(f"/v1/returns/{body['return_id']}", headers=h2()).status_code == 404
    assert c.post(f"/v1/returns/{body['return_id']}/approve", headers=h2(),
                  json={}).status_code == 404


def test_no_person_read_string_carries_an_internal_id(api: dict[str, Any]) -> None:
    c, engine = api["c"], api["engine"]
    shop_id = _no_balance_shop(c)
    body = _open_manual(c, shop_id, quantity=1)
    inspected = _walk(c, body)
    rid = body["return_id"]
    c.post(f"/v1/returns/{rid}/settle", headers=h(), json={})
    ret = c.get(f"/v1/returns/{rid}", headers=h()).json()
    strings = [ret["reason"], ret["saving_basis"]]
    strings += [ln["reason"] for ln in ret["lines"]]
    strings += [t["note"] for t in ret["transitions"]]
    assert inspected["status"] == "inspected"
    for text in strings:
        assert not ID_RE.search(text), text
        assert "\u2014" not in text and "\u2013" not in text
    with engine.connect() as conn:
        msgs = conn.execute(sa.select(s.messages.c.text).where(
            s.messages.c.tenant_id == A, s.messages.c.kind.like("return%"))).scalars().all()
    assert all(not ID_RE.search(m) for m in msgs)


def test_settle_twice_is_refused_until_the_credit_is_undone(api: dict[str, Any]) -> None:
    c = api["c"]
    body = _open_manual(c, _no_balance_shop(c), quantity=1)
    _walk(c, body)
    rid = body["return_id"]
    first = c.post(f"/v1/returns/{rid}/settle", headers=h(), json={})
    assert first.status_code == 200
    # Still inspected after settle drafts the credit, so a second draft would
    # be allowed; approving and undoing is the block the owner sees.
    apr = first.json()["credit_approval_id"]
    c.post(f"/v1/approvals/{apr}/approve", headers=h(), json={})
    assert c.post(f"/v1/approvals/{apr}/approve", headers=h(), json={}).status_code == 409
    c.post(f"/v1/approvals/{apr}/undo", headers=h())
    # After the undo the return is inspected again and can be settled afresh.
    again = c.post(f"/v1/returns/{rid}/settle", headers=h(), json={})
    assert again.status_code == 200
    assert again.json()["credit_approval_id"] != apr


def test_ledger_conflict_is_a_not_found_not_a_crash(api: dict[str, Any]) -> None:
    c = api["c"]
    with pytest.raises(NotFound):
        returns_api.get_return(api["engine"], A, "ret_missing")
    assert c.get("/v1/returns/ret_missing", headers=h()).status_code == 404


def test_approval_payload_keeps_invoice_numbers_for_applied_credits(
        api: dict[str, Any]) -> None:
    c = api["c"]
    owes = next(x for x in c.get("/v1/shops", headers=h()).json()["shops"]
                if x["outstanding_kobo"] > 0)
    body = _open_manual(c, owes["shop_id"], quantity=1)
    _walk(c, body)
    r = c.post(f"/v1/returns/{body['return_id']}/settle", headers=h(), json={})
    apr = r.json()["credit_approval_id"]
    item = next(x for x in c.get("/v1/approvals", headers=h()).json()["approvals"]
                if x["approval_id"] == apr)
    payload = item["payload"]
    assert payload["applied_kobo"] <= payload["amount_kobo"]
    assert len(payload["invoices"]) == (1 if payload["applied_kobo"] else 0)
    for inv in payload["invoices"]:
        assert inv["invoice_number"].startswith("INV-")
        assert inv["amount_applied_kobo"] > 0


def test_return_credit_carries_no_money_verb(api: dict[str, Any]) -> None:
    from mimi.runtime.outbox import MONEY_VERBS

    assert ledger.CREDIT_KIND == "return_credit"
    assert not any(v in ledger.CREDIT_KIND for v in MONEY_VERBS)


# ------------------------------------------------------------- review fixes


def test_a_rejected_credit_card_does_not_block_a_fresh_settle(
        api: dict[str, Any]) -> None:
    """The owner rejecting the card is a normal move. It must leave the return
    settleable, with a live card of its own and no stale one behind it."""
    c = api["c"]
    body = _open_manual(c, _no_balance_shop(c), quantity=2)
    _walk(c, body)
    rid = body["return_id"]
    first = c.post(f"/v1/returns/{rid}/settle", headers=h(), json={})
    assert first.status_code == 200, first.text
    rejected = first.json()["credit_approval_id"]
    assert rejected
    assert c.post(f"/v1/approvals/{rejected}/reject", headers=h(), json={}).status_code == 200
    # The read does not keep pointing at a card that is dead.
    gone = c.get(f"/v1/returns/{rid}", headers=h()).json()
    assert gone["credit_approval_id"] is None and gone["credit_kobo"] is None
    assert gone["status"] == "inspected"
    # A fresh settle drafts a new card for the amount asked for, and it is one
    # the owner can actually approve.
    again = c.post(f"/v1/returns/{rid}/settle", headers=h(), json={"amount_kobo": PRICE})
    assert again.status_code == 200, again.text
    fresh = again.json()
    assert fresh["credit_approval_id"] and fresh["credit_approval_id"] != rejected
    assert fresh["credit_kobo"] == PRICE
    pending = c.get("/v1/approvals", headers=h()).json()["approvals"]
    card = next(x for x in pending if x["approval_id"] == fresh["credit_approval_id"])
    assert card["status"] == "pending" and card["payload"]["amount_kobo"] == PRICE
    ok = c.post(f"/v1/approvals/{fresh['credit_approval_id']}/approve", headers=h(), json={})
    assert ok.status_code == 200, ok.text
    settled = c.get(f"/v1/returns/{rid}", headers=h()).json()
    assert settled["status"] == "settled" and settled["credit_applied_kobo"] == 0
    assert settled["credit_on_account_kobo"] == PRICE


def test_settle_again_with_a_different_amount_replaces_the_waiting_card(
        api: dict[str, Any]) -> None:
    """The card and the return must not disagree: a second settle with a new
    amount supersedes the waiting card instead of leaving the old payload on
    an approval the return no longer matches."""
    c = api["c"]
    body = _open_manual(c, _no_balance_shop(c), quantity=3)
    _walk(c, body)
    rid = body["return_id"]
    first = c.post(f"/v1/returns/{rid}/settle", headers=h(), json={}).json()
    same = c.post(f"/v1/returns/{rid}/settle", headers=h(), json={}).json()
    assert same["credit_approval_id"] == first["credit_approval_id"]  # idempotent
    smaller = c.post(f"/v1/returns/{rid}/settle", headers=h(),
                     json={"amount_kobo": PRICE}).json()
    assert smaller["credit_approval_id"] != first["credit_approval_id"]
    assert smaller["credit_kobo"] == PRICE
    cards = c.get("/v1/approvals", headers=h(), params={"status": "all"}).json()["approvals"]
    old = next(x for x in cards if x["approval_id"] == first["credit_approval_id"])
    new = next(x for x in cards if x["approval_id"] == smaller["credit_approval_id"])
    assert old["status"] == "superseded" and old["superseded_by"] == new["approval_id"]
    assert new["status"] == "pending" and new["payload"]["amount_kobo"] == PRICE


def test_two_credits_on_one_invoice_can_be_undone_in_either_order(
        api: dict[str, Any]) -> None:
    """Two returns credit one invoice (one shop, two returns: the normal case).
    The undo takes this credit's own applications back off the invoice rather
    than restoring a snapshot, so the older one can go first and each step
    lands on the right balance."""
    c, engine = api["c"], api["engine"]
    owes = max(c.get("/v1/shops", headers=h()).json()["shops"],
               key=lambda x: x["outstanding_kobo"])
    shop_id = owes["shop_id"]
    assert owes["outstanding_kobo"] > 3 * PRICE  # both credits land on one invoice
    before = _receivables(engine, shop_id)
    first = _walk(c, _open_manual(c, shop_id, quantity=1))
    second = _walk(c, _open_manual(c, shop_id, quantity=1))
    a1 = c.post(f"/v1/returns/{first['return_id']}/settle", headers=h(),
                json={}).json()["credit_approval_id"]
    assert c.post(f"/v1/approvals/{a1}/approve", headers=h(), json={}).status_code == 200
    a2 = c.post(f"/v1/returns/{second['return_id']}/settle", headers=h(),
                json={}).json()["credit_approval_id"]
    assert c.post(f"/v1/approvals/{a2}/approve", headers=h(), json={}).status_code == 200
    both = _receivables(engine, shop_id)
    assert sum(before.values()) - sum(both.values()) == 2 * PRICE
    touched = max(before, key=lambda d: before[d] - both[d])
    assert before[touched] - both[touched] == 2 * PRICE  # one invoice took both
    # The older credit first: the newer one stays on the invoice, untouched.
    assert c.post(f"/v1/approvals/{a1}/undo", headers=h()).status_code == 200
    one_left = _receivables(engine, shop_id)
    expected = dict(before)
    expected[touched] = before[touched] - PRICE
    assert one_left == expected
    assert c.get(f"/v1/returns/{first['return_id']}",
                 headers=h()).json()["status"] == "inspected"
    assert c.post(f"/v1/approvals/{a2}/undo", headers=h()).status_code == 200
    assert _receivables(engine, shop_id) == before


def test_undoing_the_pickup_calls_the_return_off(api: dict[str, Any]) -> None:
    """The pickup's undo has to reach the ledger: goods may not be collected,
    inspected and credited against a pickup the shop was told to ignore."""
    c = api["c"]
    pickup = _pickup(c)
    approved = c.post(f"/v1/approvals/{pickup['approval_id']}/approve",
                      headers=h(), json={"option": "pickup"})
    assert approved.status_code == 200, approved.text
    draft = next(x for x in c.get("/v1/returns", headers=h()).json()["returns"]
                 if x["approval_id"] == pickup["approval_id"])
    rid = draft["return_id"]
    assert draft["status"] == "drafted"
    undone = c.post(f"/v1/approvals/{pickup['approval_id']}/undo", headers=h())
    assert undone.status_code == 200, undone.text
    off = c.get(f"/v1/returns/{rid}", headers=h()).json()
    assert off["status"] == "cancelled"
    assert any("pickup off" in t["note"] for t in off["transitions"])
    # Nothing can be opened or walked from a pickup the owner called off.
    again = c.post("/v1/returns", headers=h(),
                   json={"approval_id": pickup["approval_id"]})
    assert again.status_code == 409, again.text
    assert "called" in again.json()["detail"]
    assert c.post(f"/v1/returns/{rid}/approve", headers=h(), json={}).status_code == 409
    assert c.post(f"/v1/returns/{rid}/collect", headers=h(), json={}).status_code == 409


def _settle_and_apply(c: TestClient, body: dict[str, Any],
                      amount_kobo: int | None = None) -> tuple[str, str]:
    """Settle the return, approve the card, and hand back both ids."""
    payload: dict[str, Any] = {} if amount_kobo is None else {"amount_kobo": amount_kobo}
    r = c.post(f"/v1/returns/{body['return_id']}/settle", headers=h(), json=payload)
    assert r.status_code == 200, r.text
    apr = r.json()["credit_approval_id"]
    ok = c.post(f"/v1/approvals/{apr}/approve", headers=h(), json={})
    assert ok.status_code == 200, ok.text
    return apr, ok.json()["approval"]["payload"]["invoices"][0]["invoice_number"]


def _receivables(engine: Any, shop_id: str) -> dict[str, int]:
    return {x["delivery_id"]: x["outstanding_kobo"]
            for x in SqlGo(engine).receivables(A, shop_id)}


def test_an_undo_after_a_bank_credit_lands_on_the_invoice_is_still_right(
        api: dict[str, Any]) -> None:
    """A verified bank payment writes the same columns the undo does
    (`demo_wiring._apply_to_invoices`). The undo subtracts this credit's own
    application, so the payment survives it and the invoice lands right."""
    from mimi.api.demo_wiring import _apply_to_invoices

    c, engine = api["c"], api["engine"]
    owes = max(c.get("/v1/shops", headers=h()).json()["shops"],
               key=lambda x: x["outstanding_kobo"])
    shop_id = owes["shop_id"]
    assert owes["outstanding_kobo"] > 3 * PRICE
    base = _receivables(engine, shop_id)
    body = _walk(c, _open_manual(c, shop_id, quantity=1))
    apr, invoice_number = _settle_and_apply(c, body, amount_kobo=PRICE)
    credited = _receivables(engine, shop_id)
    # The bank confirms a transfer for the same invoice the credit came off.
    delivery_id = "dlv-" + invoice_number.removeprefix("INV-")
    payment = PRICE // 2
    with engine.begin() as conn:
        _apply_to_invoices(conn, A, shop_id, payment, delivery_id)
    paid = _receivables(engine, shop_id)
    assert paid[delivery_id] == credited[delivery_id] - payment
    done = c.post(f"/v1/approvals/{apr}/undo", headers=h())
    assert done.status_code == 200, done.text
    expected = dict(base)
    expected[delivery_id] = base[delivery_id] - payment  # the payment stays
    assert _receivables(engine, shop_id) == expected
    got = c.get(f"/v1/returns/{body['return_id']}", headers=h()).json()
    assert got["status"] == "inspected" and got["credit_applied_kobo"] is None
    assert got["credit_approval_id"] is None  # and it can be settled again
    assert c.post(f"/v1/returns/{body['return_id']}/settle", headers=h(),
                  json={}).status_code == 200


def test_an_undo_after_the_morning_roll_still_works_and_keeps_the_word(
        api: dict[str, Any]) -> None:
    """Go's morning roll writes the same columns (`week.go.GoSim.open_day`).
    It moves no money, so nothing should be refused over it, and the undo must
    not put a stale status back: the invoice keeps the reason it is overdue."""
    from mimi.api.week.go import GoSim

    c, engine = api["c"], api["engine"]
    sim = GoSim(engine, W, A)
    sim.open_day(57)  # the seeded live deliveries close into receivables
    fresh = [x for x in c.get("/v1/shops", headers=h()).json()["shops"]
             if x["outstanding_kobo"] > 3 * PRICE]
    shop_id = next(
        x["shop_id"] for x in fresh
        if all(r["status"] == "open" for r in SqlGo(engine).receivables(A, x["shop_id"])))
    base = _receivables(engine, shop_id)
    body = _walk(c, _open_manual(c, shop_id, quantity=1))
    apr, invoice_number = _settle_and_apply(c, body, amount_kobo=PRICE)
    credited = _receivables(engine, shop_id)
    delivery_id = "dlv-" + invoice_number.removeprefix("INV-")
    sim.open_day(64)  # past the newest due day: the open invoice goes overdue
    assert _receivables(engine, shop_id) == credited  # only the status moved
    done = c.post(f"/v1/approvals/{apr}/undo", headers=h())
    assert done.status_code == 200, done.text
    got = _receivables(engine, shop_id)
    for delivery in base:
        assert got[delivery] == base[delivery]
    statuses = {r["delivery_id"]: r["status"] for r in SqlGo(engine).receivables(A, shop_id)}
    assert statuses[delivery_id] == "overdue"  # not the word from before the roll
    assert c.get(f"/v1/returns/{body['return_id']}",
                 headers=h()).json()["status"] == "inspected"


def test_an_undo_refuses_only_when_the_invoice_is_gone(api: dict[str, Any]) -> None:
    """The one case left with no honest arithmetic: the invoice is not on the
    book any more, so there is nothing to take the credit back off."""
    c, engine = api["c"], api["engine"]
    owes = max(c.get("/v1/shops", headers=h()).json()["shops"],
               key=lambda x: x["outstanding_kobo"])
    shop_id = owes["shop_id"]
    body = _walk(c, _open_manual(c, shop_id, quantity=1))
    apr, invoice_number = _settle_and_apply(c, body, amount_kobo=PRICE)
    before = _receivables(engine, shop_id)
    with engine.begin() as conn:
        conn.execute(s.receivables.delete().where(
            s.receivables.c.tenant_id == A,
            s.receivables.c.delivery_id == "dlv-" + invoice_number.removeprefix("INV-")))
    refused = c.post(f"/v1/approvals/{apr}/undo", headers=h())
    assert refused.status_code == 409, refused.text
    assert "by hand" in refused.json()["detail"]
    assert not ID_RE.search(refused.json()["detail"])
    assert c.get(f"/v1/returns/{body['return_id']}",
                 headers=h()).json()["status"] == "settled"
    assert len(before) == len(_receivables(engine, shop_id)) + 1

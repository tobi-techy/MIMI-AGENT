"""Row locking on the invoices three writers share (feat/returns-locking).

Three things write a shop's `paid_kobo`, `outstanding_kobo` and `status`: a
return credit (`mimi.api.returns`), Go's own writer for a verified bank payment
(`mimi.api.demo_wiring._apply_to_invoices`, and `week.go.GoSim._pay`), and the
routine. Each reads the invoice, works out the new figures from that read, and
writes them back, so a read that holds nothing can be overwritten by a writer
that read first and committed later. Every one of those reads goes through
`mimi.store.invoices`, which takes the rows `FOR UPDATE` in one order
(`position` then `delivery_id`).

What each test is evidence for, and what it is not:

* the blocking is Postgres only (SQLite has one writer at a time and no row
  locks, and SQLAlchemy drops the clause there), so those tests skip on SQLite
  and say why;
* the tests that drive the real writers prove those writers wait and that the
  money adds up. They cannot prove a lock order is deadlock-free for every
  reader of the code; what pins the order is that there is one order, in one
  module, and the shape test at the end pins that statement.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from mimi.api import demo_wiring
from mimi.api import returns as returns_api
from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.harness.gateway import ScriptedModel
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import invoices as invoice_reads
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.store.util import loads
from mimi.world.loader import load_world
from tests.test_api import script

W = load_world()
A = "coral-lane"
OWNER = "s-owner-key-000000000001"
SHOP = "shop-001"  # the most open invoices in the seeded world
SECOND_SHOP = "shop-005"
SKU = W.skus[0].sku
PRICE = W.skus[0].price_kobo
WAIT = 15.0
HELD = 1.0  # how long a second reader is given to prove it is waiting


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


def _postgres(engine: Any) -> None:
    if engine.dialect.name != "postgresql":
        pytest.skip("row locks are the Postgres path; SQLite has one writer at a time")


def _open_invoices(engine: Any, shop: str = SHOP) -> list[dict[str, Any]]:
    """The shop's open invoices as the book reads them, oldest first."""
    return SqlGo(engine).receivables(A, shop)


def _book_rows(engine: Any, shop: str = SHOP) -> dict[str, tuple[int, int, str]]:
    with engine.connect() as conn:
        return {str(r.delivery_id): (int(r.paid_kobo), int(r.outstanding_kobo), str(r.status))
                for r in conn.execute(sa.select(s.receivables).where(
                    s.receivables.c.tenant_id == A,
                    s.receivables.c.shop_id == shop))}


def _credit(c: TestClient, shop: str, amount: int) -> str:
    """A return on the shop, inspected and settled for exactly ``amount``:
    the approval id of the pending ``return_credit``."""
    qty = max(1, -(-int(amount) // PRICE))
    r = c.post("/v1/returns", headers=h(), json={
        "shop_id": shop, "reason": "Too much of it in the shop",
        "lines": [{"sku": SKU, "quantity": qty, "reason": "Not selling"}]})
    assert r.status_code == 201, r.text
    rid = r.json()["return_id"]
    assert c.post(f"/v1/returns/{rid}/approve", headers=h(), json={}).status_code == 200
    assert c.post(f"/v1/returns/{rid}/collect", headers=h(), json={}).status_code == 200
    inspected = c.post(f"/v1/returns/{rid}/inspect", headers=h(), json={"lines": [
        {"line_no": 1, "quantity_returned": qty, "condition": "resellable",
         "disposition": "resell"}]})
    assert inspected.status_code == 200, inspected.text
    settled = c.post(f"/v1/returns/{rid}/settle", headers=h(), json={"amount_kobo": amount})
    assert settled.status_code == 200, settled.text
    return str(settled.json()["credit_approval_id"])


def _in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    """`fn` on a thread of its own. What it raised lands in the dict, so a
    failure names the error instead of only timing out."""
    out: dict[str, Any] = {}

    def run() -> None:
        try:
            out["value"] = fn()
            out["done"] = True
        except Exception as exc:  # noqa: BLE001 - the test reports what it was
            out["error"] = exc

    th = threading.Thread(target=run, daemon=True)
    return th, out


def _applied(engine: Any, approval: str) -> list[dict[str, Any]]:
    """What the credit on that approval took off each invoice."""
    with engine.connect() as conn:
        note = conn.execute(sa.select(s.credit_notes).where(
            s.credit_notes.c.tenant_id == A,
            s.credit_notes.c.approval_id == approval)).first()
    return list(loads(note.applications, [])) if note is not None else []


# ---------------------------------------------------- the read that takes the lock


def test_a_locked_read_waits_for_the_writer_holding_the_row(book: dict[str, Any]) -> None:
    """The lock is taken by the read: the same read that holds the row for a
    writer waits, and a read that does not ask for the lock sees the figures
    from before the write, at once."""
    engine = book["engine"]
    _postgres(engine)
    first = _open_invoices(engine)[0]
    with engine.connect() as holder:
        tx = holder.begin()
        held = invoice_reads.open_invoices(holder, A, SHOP, lock=True)
        assert [str(r.delivery_id) for r in held][0] == first["delivery_id"]
        holder.execute(s.receivables.update().where(
            s.receivables.c.tenant_id == A,
            s.receivables.c.delivery_id == first["delivery_id"]).values(
            paid_kobo=first["paid_kobo"] + 100_000,
            outstanding_kobo=first["outstanding_kobo"] - 100_000))

        def plain_read() -> int:
            started = time.monotonic()
            with engine.connect() as other:
                rows = invoice_reads.open_invoices(other, A, SHOP, lock=False)
            assert time.monotonic() - started < 1.0  # it did not wait
            return int(rows[0].outstanding_kobo)

        plain_th, plain = _in_thread(plain_read)
        plain_th.start()
        plain_th.join(HELD)
        assert not plain_th.is_alive(), "a read that asks for no lock must not wait"
        assert plain["value"] == first["outstanding_kobo"]

        def locked_read() -> int:
            started = time.monotonic()
            with engine.connect() as third:
                rows = invoice_reads.open_invoices(third, A, SHOP, lock=True)
            locked["waited"] = time.monotonic() - started
            return int(rows[0].outstanding_kobo)

        locked_th, locked = _in_thread(locked_read)
        locked_th.start()
        locked_th.join(HELD)
        assert locked_th.is_alive(), "the locked read should still be waiting"
        tx.commit()  # the writer is done: the lock goes with the transaction
        locked_th.join(WAIT)
    assert not locked_th.is_alive() and locked.get("error") is None, locked
    assert locked["waited"] >= 0.5
    assert locked["value"] == first["outstanding_kobo"] - 100_000


def test_a_second_writer_on_one_invoice_waits_and_both_allocations_land(
        book: dict[str, Any]) -> None:
    """Two writers against one invoice, for real: the return credit waits for
    the bank payment's transaction, and neither allocation is lost."""
    engine = book["engine"]
    _postgres(engine)
    first = _open_invoices(engine)[0]
    open_kobo = first["outstanding_kobo"]
    payment = open_kobo // 4
    credit = open_kobo // 4
    approval = _credit(book["c"], SHOP, credit)
    with engine.connect() as holder:
        tx = holder.begin()
        demo_wiring._apply_to_invoices(holder, A, SHOP, payment, first["delivery_id"])
        th, done = _in_thread(
            lambda: returns_api.apply_credit(engine, A, {"approval_id": approval}))
        th.start()
        th.join(HELD)
        assert th.is_alive(), "the credit should be waiting on that invoice"
        tx.commit()
        th.join(WAIT)
    assert not th.is_alive() and done.get("error") is None, done
    paid, outstanding, status = _book_rows(engine)[first["delivery_id"]]
    assert paid == first["paid_kobo"] + payment + credit
    assert outstanding == open_kobo - payment - credit
    assert paid + outstanding == first["total_kobo"]
    assert status == first["status"]


def test_a_bank_payment_racing_behind_a_credit_ends_with_the_right_outstanding(
        book: dict[str, Any]) -> None:
    """The other way round: the credit is applied and held, the bank payment
    waits for it, and the invoice ends with both amounts off the same figure."""
    engine = book["engine"]
    _postgres(engine)
    first = _open_invoices(engine)[0]
    open_kobo = first["outstanding_kobo"]
    payment = open_kobo // 4
    credit = open_kobo // 4
    approval = _credit(book["c"], SHOP, credit)
    with engine.connect() as holder:
        tx = holder.begin()
        returns_api._apply_credit_in(holder, A, {"approval_id": approval})
        th, done = _in_thread(lambda: _pay(engine, first["delivery_id"], payment))
        th.start()
        th.join(HELD)
        assert th.is_alive(), "the bank payment should be waiting on that invoice"
        tx.commit()
        th.join(WAIT)
    assert not th.is_alive() and done.get("error") is None, done
    paid, outstanding, status = _book_rows(engine)[first["delivery_id"]]
    assert paid == first["paid_kobo"] + payment + credit
    assert outstanding == open_kobo - payment - credit
    assert paid + outstanding == first["total_kobo"]
    assert status == first["status"]


def _pay(engine: Any, delivery_id: str, amount: int) -> str:
    with engine.begin() as conn:
        return demo_wiring._apply_to_invoices(conn, A, SHOP, amount, delivery_id)


def test_a_credit_and_a_bank_payment_in_either_order_leave_the_same_balance(
        book: dict[str, Any]) -> None:
    """The same two amounts, the other order round, on a second shop: the book
    ends in the same place. (On Postgres the racing version is the test above
    and the ones below; SQLite can only show the arithmetic.)"""
    engine, c = book["engine"], book["c"]
    amount = PRICE
    for shop in (SHOP, SECOND_SHOP):
        assert sum(r["outstanding_kobo"] for r in _open_invoices(engine, shop)) > 3 * amount
    before = {shop: sum(r["outstanding_kobo"] for r in _open_invoices(engine, shop))
              for shop in (SHOP, SECOND_SHOP)}
    # the first shop: the bank payment, then the credit
    first = _open_invoices(engine, SHOP)[0]
    with engine.begin() as conn:
        demo_wiring._apply_to_invoices(conn, A, SHOP, amount, first["delivery_id"])
    returns_api.apply_credit(engine, A, {"approval_id": _credit(c, SHOP, amount)})
    # the second shop: the credit, then the bank payment
    returns_api.apply_credit(engine, A, {"approval_id": _credit(c, SECOND_SHOP, amount)})
    second = _open_invoices(engine, SECOND_SHOP)[0]
    with engine.begin() as conn:
        demo_wiring._apply_to_invoices(conn, A, SECOND_SHOP, amount, second["delivery_id"])
    for shop in (SHOP, SECOND_SHOP):
        after = sum(r["outstanding_kobo"] for r in _open_invoices(engine, shop))
        assert before[shop] - after == 2 * amount
        for paid, outstanding, _status in _book_rows(engine, shop).values():
            assert paid >= 0 and outstanding >= 0


# ---------------------------------------------------------------- the lock order


def test_a_credit_spanning_two_invoices_waits_on_the_first_and_leaves_the_second_free(
        book: dict[str, Any]) -> None:
    """The order holds with more than one invoice in the transaction. A credit
    that spans two invoices waits on the first and holds nothing, so no other
    writer can be waiting on it, which is what a cycle would need."""
    engine = book["engine"]
    _postgres(engine)
    rows = _open_invoices(engine)
    first, second = rows[0], rows[1]
    approval = _credit(book["c"], SHOP, first["outstanding_kobo"] + 1_000)
    with engine.connect() as holder:
        tx = holder.begin()
        assert first["delivery_id"] in invoice_reads.invoices_by_id(
            holder, A, [first["delivery_id"]], lock=True)
        th, done = _in_thread(
            lambda: returns_api.apply_credit(engine, A, {"approval_id": approval}))
        th.start()
        th.join(HELD)
        assert th.is_alive()  # waiting on the first invoice

        def probe_second() -> int:
            with engine.connect() as conn:
                return len(invoice_reads.invoices_by_id(
                    conn, A, [second["delivery_id"]], lock=True))

        prober, probe = _in_thread(probe_second)
        prober.start()
        prober.join(HELD)
        blocked = prober.is_alive()
        tx.commit()
        th.join(WAIT)
        prober.join(WAIT)
    assert not blocked, "the waiting credit was holding the second invoice"
    assert not th.is_alive() and done.get("error") is None, done
    assert not prober.is_alive() and probe.get("error") is None, probe
    assert probe["value"] == 1
    after = _book_rows(engine)
    assert after[first["delivery_id"]][1] == 0
    assert after[second["delivery_id"]][1] == second["outstanding_kobo"] - 1_000


def test_two_credits_over_several_invoices_both_land_without_a_deadlock(
        book: dict[str, Any]) -> None:
    """Two credits at once, each spanning several invoices: both finish, and
    every invoice keeps paid + outstanding == total."""
    engine = book["engine"]
    _postgres(engine)
    rows = _open_invoices(engine)
    amount = rows[0]["outstanding_kobo"] + 1_000_000
    approvals = [_credit(book["c"], SHOP, amount) for _ in range(2)]
    before = sum(r["outstanding_kobo"] for r in rows)
    start = threading.Barrier(2, timeout=WAIT)
    threads: list[tuple[threading.Thread, dict[str, Any]]] = []

    def writer(approval: str) -> None:
        start.wait()
        returns_api.apply_credit(engine, A, {"approval_id": approval})

    for approval in approvals:
        th, done = _in_thread(lambda a=approval: writer(a))
        threads.append((th, done))
    for th, _done in threads:
        th.start()
    for th, _done in threads:
        th.join(WAIT)
    for th, done in threads:
        assert not th.is_alive() and done.get("error") is None, done
    with engine.connect() as conn:
        totals = {str(r.delivery_id): (int(r.total_kobo), int(r.paid_kobo),
                                       int(r.outstanding_kobo))
                  for r in conn.execute(sa.select(s.receivables).where(
                      s.receivables.c.tenant_id == A, s.receivables.c.shop_id == SHOP))}
    for delivery_id, (total, paid, outstanding) in totals.items():
        assert paid + outstanding == total, delivery_id
    assert before - sum(x[2] for x in totals.values()) == 2 * amount
    for approval in approvals:
        assert len(_applied(engine, approval)) >= 2  # each spanned several invoices


def test_the_locked_read_is_one_statement_in_the_house_order(book: dict[str, Any]) -> None:
    """The order is one order, in one module, and the statement says so."""
    class Recorder:
        def __init__(self) -> None:
            self.statements: list[Any] = []

        def execute(self, stmt: Any) -> list[Any]:
            self.statements.append(stmt)
            return []

    assert invoice_reads.LOCK_ORDER == ("position", "delivery_id")
    order = "ORDER BY receivables.position, receivables.delivery_id"
    locked, plain, undo = Recorder(), Recorder(), Recorder()
    invoice_reads.open_invoices(cast(Any, locked), A, SHOP, lock=True)
    invoice_reads.open_invoices(cast(Any, plain), A, SHOP, lock=False)
    invoice_reads.invoices_by_id(cast(Any, undo), A, ["dlv-1", "dlv-2"], lock=True)
    for rec, wants_lock in ((locked, True), (undo, True), (plain, False)):
        sql = str(rec.statements[0].compile(dialect=postgresql.dialect()))
        assert order in sql, sql
        assert ("FOR UPDATE" in sql) is wants_lock, sql

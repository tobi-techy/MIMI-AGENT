"""Delivery receipt flow (RAI-266/267) on the always-on runtime."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.models import FakeModel
from mimi.receipts import (
    HELPER,
    RECEIPT_INTENTS,
    InMemoryShopInbox,
    ReceiptDesk,
    ReceiptFlow,
    ReceiptJudge,
    ShopWebhook,
    SqlShopInbox,
)
from mimi.receipts.receipt import naira, receipt_text
from mimi.receipts.verdict import fake_verdict
from mimi.runtime.outbox import ForbiddenIntent, intent_types
from mimi.runtime.runner import JobRunner
from mimi.runtime.sql_store import SqlJobStore
from mimi.runtime.states import JobState
from mimi.runtime.store import InMemoryJobStore
from mimi.runtime.tracing import RecordingStepTracer
from mimi.sim.engine import SimGo, Simulator
from mimi.sim.persona import fake_shop
from mimi.world import load_world

WAT = timezone(timedelta(hours=1))
WORLD = load_world()
DELIVERY = next(d for d in WORLD.live_deliveries if d.shortage is None)
SHOP = WORLD.shop(DELIVERY.shop_id)


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 26, 9, 0, tzinfo=WAT)

    def __call__(self) -> datetime:
        return self.t

    def tick(self, **kw: Any) -> datetime:
        self.t += timedelta(**kw)
        return self.t


class WorkerDied(BaseException):
    """Process death: not an Exception, so nothing in the runner catches it."""


class CrashOnce(SimGo):
    """Go accepts the first send, then the worker dies before saving."""

    def __init__(self) -> None:
        super().__init__()
        self.crashed = False

    def submit(
        self, tenant_id: str, key: str, kind: str, payload: dict[str, Any]
    ) -> str:
        out = super().submit(tenant_id, key, kind, payload)
        if not self.crashed:
            self.crashed = True
            raise WorkerDied()
        return out


@pytest.fixture(params=["memory", "sql"])
def stores(request: pytest.FixtureRequest) -> Any:
    if request.param == "memory":
        return InMemoryJobStore(), InMemoryShopInbox()
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    store, inbox = SqlJobStore(engine), SqlShopInbox(engine)
    store.ensure_schema()
    inbox.ensure_schema()
    return store, inbox


def rig(stores: Any, sink: Any = None, worker: str = "w1") -> Any:
    store, inbox = stores
    clock = Clock()
    go = sink or SimGo()
    flow = ReceiptFlow(
        WORLD,
        inbox,
        ReceiptJudge(FakeModel(fake_verdict)),
    )
    runner = JobRunner(
        store=store,
        sink=go,
        tracer=RecordingStepTracer(),
        steps={HELPER: flow},
        worker_id=worker,
        clock=clock,
        lease_s=30,
        intent_types=RECEIPT_INTENTS,
    )
    desk = ReceiptDesk(store, inbox, WORLD.tenant_id)
    job_id = desk.open_receipt(DELIVERY.delivery_id, SHOP.shop_id)
    return store, desk, runner, go, clock, job_id


def drain(runner: JobRunner) -> None:
    while runner.run_once() is not None:
        pass


def say(desk: ReceiptDesk, clock: Clock, text: str, mid: str) -> str:
    return desk.receive(ShopWebhook(mid, SHOP.shop_id, SHOP.phone, text, clock()))


def settle(runner: JobRunner, clock: Clock) -> None:
    clock.tick(seconds=95)
    drain(runner)


def test_receipt_figures_come_from_code() -> None:
    text = receipt_text(DELIVERY, SHOP.name, WORLD.distributor)
    assert text.endswith("Reply YES to confirm.")
    assert f"Total: {naira(DELIVERY.total_kobo)}" in text
    for line in DELIVERY.lines:
        assert naira(line.line_total_kobo) in text
    assert naira(5_345_000) == "N53,450" and naira(5_345_050) == "N53,450.50"


def test_happy_path_yes_records_consent_and_witness(stores: Any) -> None:
    store, desk, runner, go, clock, job_id = rig(stores)
    drain(runner)
    assert [m["purpose"] for m in go.to_shops] == ["delivery_receipt"]
    assert store.get(job_id).state is JobState.SLEEPING_UNTIL
    clock.tick(minutes=20)
    assert say(desk, clock, "YES", "wamid.1") == "routed"
    settle(runner, clock)
    consent = go.consents[DELIVERY.delivery_id]
    assert consent["consent"] == "shop_confirmed"
    assert consent["witness_message_id"] == "wamid.1"
    assert consent["witness_text"] == "YES"
    assert consent["total_kobo"] == DELIVERY.total_kobo
    assert store.get(job_id).state is JobState.DONE
    assert len(go.to_shops) == 1  # no extra chatter


def test_wrong_reply_gets_one_clarification_then_flags(stores: Any) -> None:
    store, desk, runner, go, clock, job_id = rig(stores)
    drain(runner)
    clock.tick(minutes=5)
    say(desk, clock, "ok", "wamid.a")
    settle(runner, clock)
    assert [m["purpose"] for m in go.to_shops] == [
        "delivery_receipt",
        "receipt_clarify",
    ]
    clock.tick(minutes=30)
    say(desk, clock, "[voice note 0:12]", "wamid.b")
    settle(runner, clock)
    assert len(go.to_shops) == 2  # never a second clarification
    assert [f["reason"] for f in go.flags] == ["unclear_reply"]
    assert not go.consents
    assert store.get(job_id).result == "flagged"


def test_unclear_then_yes_confirms(stores: Any) -> None:
    store, desk, runner, go, clock, job_id = rig(stores)
    drain(runner)
    say(desk, clock, "\U0001f44d", "wamid.a")
    settle(runner, clock)
    say(desk, clock, "Yes \U0001f44d", "wamid.b")
    settle(runner, clock)
    assert DELIVERY.delivery_id in go.consents
    assert store.get(job_id).result == "confirmed"


def test_dispute_flags_owner_with_shop_words(stores: Any) -> None:
    store, desk, runner, go, clock, job_id = rig(stores)
    drain(runner)
    say(desk, clock, "Good afternoon", "wamid.1")
    say(desk, clock, "I didn't collect the 2 cartons", "wamid.2")
    settle(runner, clock)
    assert len(go.to_shops) == 1  # the greeting alone did not get a reply
    assert go.flags[0]["reason"] == "dispute"
    assert "2 cartons" in go.flags[0]["shop_words"]
    assert not go.consents


def test_duplicate_yes_is_a_no_op(stores: Any) -> None:
    store, desk, runner, go, clock, job_id = rig(stores)
    drain(runner)
    say(desk, clock, "YES", "wamid.1")
    assert say(desk, clock, "YES", "wamid.1") == "duplicate"  # webhook retry
    settle(runner, clock)
    accepted = len(go.accepted)
    clock.tick(minutes=3)
    assert say(desk, clock, "YES", "wamid.2") == "unrouted"  # second YES, job done
    drain(runner)
    assert len(go.accepted) == accepted
    assert len(go.consents) == 1


def test_quiet_when_nothing_is_new(stores: Any) -> None:
    store, desk, runner, go, clock, job_id = rig(stores)
    drain(runner)
    before = go.calls
    for _ in range(3):
        clock.tick(hours=6, minutes=1)
        drain(runner)
        if store.get(job_id).state is JobState.DONE:
            break
    job = store.get(job_id)
    if job.state is JobState.DONE:
        # The 24h deadline passed with no reply: one flag, nothing else.
        assert [f["reason"] for f in go.flags] == ["no_reply"]
        assert go.calls == before + 1
    else:
        assert go.calls == before


def test_crash_between_step_and_send_never_double_sends(stores: Any) -> None:
    crashy = CrashOnce()
    store, desk, runner, go, clock, job_id = rig(stores, sink=crashy)
    with pytest.raises(WorkerDied):
        runner.run_once()
    # The step and its outbox were saved before the send; the save marking it
    # sent never happened.
    assert store.get(job_id).pending_outbound
    # Worker B takes over after the lease expires and flushes the outbox.
    clock.tick(seconds=31)
    flow = runner.steps[HELPER]
    b = JobRunner(
        store=store,
        sink=crashy,
        tracer=RecordingStepTracer(),
        steps={HELPER: flow},
        worker_id="w2",
        clock=clock,
        lease_s=30,
        intent_types=RECEIPT_INTENTS,
    )
    drain(b)
    assert crashy.calls == 2  # resubmitted once with the same key
    assert len(crashy.to_shops) == 1  # the shop got one receipt
    assert store.get(job_id).state is JobState.SLEEPING_UNTIL


def test_extension_intents_refuse_money_verbs() -> None:
    from dataclasses import dataclass, field
    from typing import Literal

    @dataclass(frozen=True)
    class TransferFunds:
        amount_kobo: int
        kind: Literal["transfer_funds"] = field(default="transfer_funds")

    with pytest.raises(ForbiddenIntent):
        intent_types(TransferFunds)


def test_store_wake_only_wakes_a_sleeping_job(stores: Any) -> None:
    store, desk, runner, go, clock, job_id = rig(stores)
    assert store.wake(job_id) is False  # queued
    drain(runner)
    assert store.wake(job_id) is True
    assert store.get(job_id).state is JobState.QUEUED


def test_forty_shop_day_with_fake_model_under_60s() -> None:
    started = time.monotonic()
    sim = Simulator(
        WORLD, mimi_model=FakeModel(fake_verdict), sim_model=FakeModel(fake_shop)
    )
    report = sim.run()
    assert time.monotonic() - started < 60
    assert report["shops"] == 40
    assert report["receipts_sent"] == 40
    assert report["still_waiting"] + sum(report["results"].values()) == 40
    assert report["shortages_flagged"] == report["shortages_in_world"] == 3
    assert report["confirmed_despite_shortage"] == 0
    assert report["intent_submits"] == report["intents_accepted"]  # nothing sent twice
    assert report["clarifications_sent"] <= report["shops"]

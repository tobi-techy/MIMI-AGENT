"""Per-tenant spend is a runtime limit, and the cheap path survives it.

The ``spend`` table records what model calls cost. These tests pin the three
promises the owner asked for (``docs/SLO.md``):

1. a bounded default: a tenant is never unlimited by omission;
2. a paid call is refused **before** it is made once the day is spent, so the
   bill stops across workers;
3. the refusal costs the tenant only the model: rules, book reads and
   plain-code replies keep working, and a person reads one plain sentence.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.api.agent import Agent
from mimi.harness.gateway import ModelTurn, ScriptedModel, say
from mimi.harness.inbound import Inbound, Payload
from mimi.models import metering
from mimi.observe import spend as budget
from mimi.observe.spend import BudgetExhausted
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.world.loader import load_world

W = load_world()
T = "coral-lane"
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # 10:00 WAT
DAY = date(2026, 10, 26)


# ------------------------------------------------------------------ policy


def test_the_default_is_a_number_and_a_tenant_is_never_unlimited() -> None:
    assert isinstance(budget.DEFAULT_DAILY_CAP_KOBO, int)
    assert 0 < budget.DEFAULT_DAILY_CAP_KOBO < 10_000_000
    assert budget.cap_or_default(None) == budget.DEFAULT_DAILY_CAP_KOBO
    assert budget.cap_or_default(-5) == budget.DEFAULT_DAILY_CAP_KOBO
    assert budget.cap_or_default(0) == 0  # an explicit zero is "no model today"


def test_over_or_under_is_one_comparison() -> None:
    assert budget.decide(0, 100).allowed
    assert budget.decide(99, 100).allowed
    assert budget.decide(100, 100).over
    assert budget.decide(99, 100, reserve_kobo=1).over
    assert budget.decide(150, 100).over


def test_the_sentences_are_plain_and_carry_no_figures() -> None:
    q = budget.question(12_000, 50_000)
    assert "N120" in q and "N500" in q  # the owner's card may name the money
    assert "7 of today's 30 model turns" in q  # turns first: the owner's unit
    # The person's line names the distributor, the shops counted, the cap and
    # what was refused; no internal ids, no dashes (voice rules).
    refusal = budget.refusal(tenant="coral-lane", shops=8, cap_kobo=200_000)
    assert "Coral Lane" in refusal and "8 active shops" in refusal
    assert "N2,000" in refusal and "need the model" in refusal
    assert "coral-lane" not in refusal and "turn_" not in refusal
    assert not any(ch in refusal for ch in "\u2013\u2014")
    # A tenant with no activity still reads as one shop, not zero.
    assert "1 active shop " in budget.refusal(tenant="coral-lane", shops=0,
                                              cap_kobo=25_000) + " "


# ----------------------------------------------------------------- the seam


class Priced:
    """A chat model that answers and reports token use (the seam sees it)."""

    name = "muse-spark-1.3"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: Any, *, max_tokens: int = 400,
                 meta: Any = None, response_format: Any = None) -> Any:
        from mimi.models.base import ChatResult

        metering.guard(self.name)
        self.calls += 1
        return ChatResult("ok", self.name, 700, 300)


def test_a_paid_call_is_refused_at_the_cap_before_it_is_made() -> None:
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    seed_world(engine, W)  # the cap is N250 a shop, not a row in tenants
    go = SqlGo(engine)
    _, cap = go.daily_spend(T, DAY)
    go.add_spend(T, "collector", DAY, cap)
    model = Priced()
    with pytest.raises(BudgetExhausted) as err:
        with metering.scope(control=go, tenant_id=T, day=DAY):
            metering.Metered(model, metering.HARNESS).complete([])
    assert model.calls == 0  # refused before the provider was called
    assert err.value.cap_kobo == cap and err.value.spent_kobo == cap
    # The cap is the shop count times N250, floored at one shop, never a
    # flat number and never unlimited.
    from mimi.observe import allowance as budget_allowance

    budget_allowance.forget()
    with engine.connect() as conn:
        shops = budget_allowance.allowance(conn, T, DAY).active_shops
    assert cap == 25_000 * max(1, shops)


def test_the_scope_fails_closed_when_the_cap_cannot_be_read() -> None:
    class Blind:
        def add_spend(self, *a: Any, **k: Any) -> None:
            return None

        def daily_spend(self, *a: Any, **k: Any) -> tuple[int, int]:
            raise RuntimeError("the store is down")

    with pytest.raises(BudgetExhausted):
        with metering.scope(control=Blind(), tenant_id=T, day=DAY):
            metering.Metered(Priced(), metering.HARNESS).complete([])


# ------------------------------------------------------------- end to end


def script(messages: Any, tools: Any) -> ModelTurn:
    return say("", ("send_to_user", {"channel": "shop_whatsapp", "shop_id": "shop-033",
                                      "kind": "reply", "body": "Got it.", "ends_turn": True}))


@pytest.fixture()
def env(store_url: str) -> Any:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)  # the cap is N250 a shop, not a row in tenants
    model = ScriptedModel(script)
    agent = Agent(engine, models=[model], router_model=None, worker_id="w-spend",
                  clock=lambda: NOW)
    yield {"engine": engine, "agent": agent, "model": model}
    engine.dispose()


def spend_to_cap(engine: Any, tenant: str = T, day: date = DAY) -> int:
    """Book the day's model spend up to the cap the gate enforces (N250/shop)."""
    from mimi.observe import allowance as budget_allowance

    budget_allowance.forget()
    with engine.begin() as conn:
        cap = budget_allowance.allowance(conn, tenant, day).cap_kobo
        conn.execute(sa.delete(s.spend).where(s.spend.c.tenant_id == tenant,
                                              s.spend.c.day == day.isoformat()))
        conn.execute(sa.insert(s.spend).values(tenant_id=tenant, day=day.isoformat(),
                                              kobo=cap))
    return cap


def shops_counted(engine: Any, tenant: str = T, day: date = DAY) -> int:
    """How many shops set the day's cap (the same function the gate reads)."""
    from mimi.observe import allowance as budget_allowance

    budget_allowance.forget()
    with engine.connect() as conn:
        return budget_allowance.allowance(conn, tenant, day).active_shops


def submit(agent: Agent, ext: str, text: str, shop: str = "shop-033") -> str:
    v = agent.submit(T, Inbound(source="shop_message", org_id=T, external_id=ext,
                                received_at=NOW,
                                payload=Payload(text=text,
                                                from_phone=W.shop(shop).phone)))
    return str(v.turn_id)


def spend_out(engine: Any, turn_id: str) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(s.messages).where(
            s.messages.c.turn_id == turn_id, s.messages.c.direction == "out")))


def test_a_budget_exhausted_tenant_still_gets_the_cheap_certain_answer(
        env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression the owner cared about: code paths must not be paused."""
    monkeypatch.setenv("MIMI_PLAIN_CODE", "balance")
    engine, agent = env["engine"], env["agent"]
    spend_to_cap(engine)  # nothing left of the day's allowance

    turn_id = submit(agent, "spend.cheap.1", "How much I owe now?")
    assert agent.process(turn_id) == turn_id

    with engine.connect() as conn:
        turn = conn.execute(sa.select(s.turns).where(
            s.turns.c.turn_id == turn_id)).first()
    assert turn.state == "done" and turn.status == "done"
    assert json.loads(turn.result)["route"]["helper"] == "collector"
    out = spend_out(engine, turn_id)
    assert out, "a balance question is answered, not paused"
    assert any(m.kind == "reply" and "owe" in m.text.lower() for m in out)
    assert agent.harness.gateway.models[0].calls == 0  # no paid call was made


def test_the_model_path_is_refused_with_one_plain_sentence(
        env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_PLAIN_CODE", "0")
    engine, agent = env["engine"], env["agent"]
    cap = spend_to_cap(engine)

    turn_id = submit(agent, "spend.expensive.1", "abeg wetin I owe now?")
    assert agent.process(turn_id) == turn_id

    with engine.connect() as conn:
        turn = conn.execute(sa.select(s.turns).where(
            s.turns.c.turn_id == turn_id)).first()
    assert turn.state == "parked" and turn.status == "parked_budget"
    out = spend_out(engine, turn_id)
    kinds = [m.kind for m in out]
    assert kinds.count("holding_reply") == 1  # one plain sentence to the shop
    assert set(kinds) <= {"holding_reply", "cap_note"}
    line = next(m.text for m in out if m.kind == "holding_reply")
    # The sentence names the distributor, the shops counted, the cap and what
    # was refused, in plain words and with no internal ids.
    assert "Coral Lane" in line and "active shop" in line
    assert budget.naira(cap) in line and "model" in line
    assert "coral-lane" not in line and "turn_" not in line and "kobo" not in line
    assert f"{shops_counted(engine)} active shop" in line
    assert agent.harness.gateway.models[0].calls == 0
    # The owner is asked once, with a number, and the parked work is kept.
    with engine.connect() as conn:
        approvals = list(conn.execute(sa.select(s.approvals).where(
            s.approvals.c.turn_id == turn_id)))
        events = list(conn.execute(sa.select(s.thread_events).where(
            s.thread_events.c.turn_id == turn_id)))
    assert any(a.tool == "raise_daily_allowance" for a in approvals)
    assert any(e.type == "cap_hit" for e in events)
    assert any(e.type == "parked" for e in events)


def test_the_call_is_refused_even_when_the_gate_passed(
        env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another worker books spend between the gate and the call: still stopped."""
    monkeypatch.setenv("MIMI_PLAIN_CODE", "0")
    engine, agent = env["engine"], env["agent"]
    real = agent.go.daily_spend
    asked: list[str] = []

    def raising(tenant: str, day: Any) -> tuple[int, int]:
        """Room for the turn's own gate, nothing for the metering seam."""
        caller = inspect.stack()[1].function
        asked.append(caller)
        if caller == "admit":
            return 500, 500
        return real(tenant, day)

    agent.go.daily_spend = raising  # type: ignore[method-assign]
    turn_id = submit(agent, "spend.race.1", "abeg wetin I owe now?")
    assert agent.process(turn_id) == turn_id
    assert "admit" in asked  # the seam is what stopped it

    with engine.connect() as conn:
        turn = conn.execute(sa.select(s.turns).where(
            s.turns.c.turn_id == turn_id)).first()
    assert turn.state == "parked" and turn.status == "parked_budget"
    assert agent.harness.gateway.models[0].calls == 0
    kinds = [m.kind for m in spend_out(engine, turn_id)]
    assert kinds.count("holding_reply") == 1
    assert set(kinds) <= {"holding_reply", "cap_note"}
    # The seam's own refusal: one plain sentence naming the cap, no apology play.
    line = next(m.text for m in spend_out(engine, turn_id) if m.kind == "holding_reply")
    assert "Coral Lane" in line and budget.naira(500) in line

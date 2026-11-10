"""One correlation id from the API boundary to the reply (and across a retry).

The id is the turn id (deterministic from the distributor and the provider's
message id), its trace id, and it is on every hop the turn touches: the
inbound row, the turn, its checkpoint and result, its attempt rows, its event
log, its intents, its approvals, its outbound message, and the follow-up turns
or connector writes its work spawns. A re-claimed turn keeps the same id and
the new attempt number, so a retried payment is one incident with two attempts,
not two unrelated events (``mimi.observe.correlation``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.api.agent import Agent
from mimi.harness.gateway import ModelTurn, ScriptedModel, say
from mimi.harness.inbound import Inbound, Payload
from mimi.observe import correlation
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.store.queue import TurnQueue, turn_id_for
from mimi.world.loader import load_world

W = load_world()
T = "coral-lane"
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
OPEN = next(r for r in W.receivables if r.shop_id == "shop-001" and r.outstanding_kobo > 0)


def script(messages: Any, tools: Any) -> ModelTurn:
    """A shop message gets a reply; "FIRM" asks the owner (so an approval exists)."""
    newest = next(m["content"] for m in reversed(messages) if m["role"] == "user")
    if "FIRM" in newest:
        return say("", ("send_reminder", {"shop_id": "shop-001",
                                           "invoice_id": OPEN.delivery_id,
                                           "tone": "firm"}))
    return say("", ("send_to_user", {"channel": "shop_whatsapp", "shop_id": "shop-001",
                                      "kind": "reply", "body": "Got it.", "ends_turn": True}))


@pytest.fixture()
def env(store_url: str) -> Any:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W, daily_cap_kobo=1_000_000)
    model = ScriptedModel(script)
    agent = Agent(engine, models=[model], router_model=None, worker_id="w-trace",
                  lease_s=30.0, clock=lambda: NOW)
    yield {"engine": engine, "agent": agent, "model": model}
    engine.dispose()


def submit(agent: Agent, ext: str, text: str) -> str:
    shop = W.shop("shop-001")
    v = agent.submit(T, Inbound(source="shop_message", org_id=T, external_id=ext,
                                received_at=NOW,
                                payload=Payload(text=text, from_phone=shop.phone)))
    return str(v.turn_id)


def run(agent: Agent, turn_id: str) -> None:
    assert agent.process(turn_id) == turn_id


def rows(engine: Any, table: Any, where: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(table).where(where)))


def test_one_trace_id_at_every_hop(env: Any) -> None:
    engine, agent = env["engine"], env["agent"]
    ext = "wamid.hop.1"
    turn_id = submit(agent, ext, "Hello, is my order coming?")
    assert turn_id == turn_id_for(T, ext)

    before = rows(engine, s.inbound_messages,
                  s.inbound_messages.c.external_id == ext)
    assert [r.turn_id for r in before] == [turn_id]
    assert json.loads(rows(engine, s.turns, s.turns.c.turn_id == turn_id)[0].checkpoint)[
        "trace"] == turn_id

    run(agent, turn_id)

    turn = rows(engine, s.turns, s.turns.c.turn_id == turn_id)[0]
    result = json.loads(turn.result)
    assert result["trace"] == turn_id and result["attempt"] == 1
    assert not result.get("route_untraced")

    # The hops that carry the turn id today.
    assert {m.turn_id for m in rows(engine, s.messages,
                                    s.messages.c.thread_id == turn.thread_id)} == {turn_id}
    events = rows(engine, s.thread_events, s.thread_events.c.turn_id == turn_id)
    assert events and {e.turn_id for e in events} == {turn_id}
    assert {a.turn_id for a in rows(engine, s.intents, s.intents.c.turn_id == turn_id)} <= \
        {turn_id}
    out = rows(engine, s.messages, sa.and_(s.messages.c.turn_id == turn_id,
                                           s.messages.c.direction == "out"))
    assert out and all(m.turn_id == turn_id for m in out)
    attempts = rows(engine, s.turn_attempts, s.turn_attempts.c.turn_id == turn_id)
    assert len(attempts) == 1


def test_an_approval_and_a_connector_write_are_under_the_same_trace(
        env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_PLAIN_CODE", "0")  # the model path: a real approval
    engine, agent = env["engine"], env["agent"]
    turn_id = submit(agent, "wamid.hop.firm", "abeg wetin I owe now? FIRM")
    run(agent, turn_id)
    approvals = rows(engine, s.approvals, s.approvals.c.turn_id == turn_id)
    assert approvals, "the FIRM reminder should have queued one approval"

    # A connector write has no trace column yet (the database agent's next
    # revision adds one). Until then the write links to its approval, which
    # carries the turn id: the join is the documented path and this test pins
    # it, so the column can replace the join without changing the assertion.
    with engine.begin() as conn:
        conn.execute(sa.insert(s.connector_writes).values(
            tenant_id=T, write_id="wr-trace-1", proposal_id="prop-1",
            idempotency_key="zoho-books:test-trace", action="zoho_books_invoice_comment",
            payload_hash="0" * 64, before="{}", after="{}", external_id="c-1",
            approval_id=approvals[0].approval_id, applied_at=NOW.isoformat(), undone_at=None))
    joined = rows(engine, s.connector_writes.join(
        s.approvals, sa.and_(s.connector_writes.c.tenant_id == s.approvals.c.tenant_id,
                             s.connector_writes.c.approval_id == s.approvals.c.approval_id)),
        s.connector_writes.c.write_id == "wr-trace-1")
    assert [r.turn_id for r in joined] == [turn_id]


def test_a_followup_turn_keeps_its_parent_trace(env: Any) -> None:
    engine, agent = env["engine"], env["agent"]
    turn_id = submit(agent, "wamid.hop.parent", "Hello")
    with engine.begin() as conn:
        from mimi.api import steering as sx
        from mimi.harness import steer as st

        shop = W.shop("shop-001")
        parent = Inbound(source="shop_message", org_id=T, external_id="wamid.hop.parent",
                         received_at=NOW,
                         payload=Payload(text="Hello", from_phone=shop.phone, shop_id="shop-001"))
        parts = st.split_parts([parent])
        follow = sx.followup_inbound(parent, [parent], parts, [parts[0].pid], "new")
        fu = sx.insert_followup(conn, T, "wa-shop-001", follow, after_ts=NOW.timestamp(),
                                order=0, shop_id="shop-001", simulated=False,
                                trace=correlation.turn_trace(turn_id))
    turn = rows(engine, s.turns, s.turns.c.turn_id == fu)[0]
    assert json.loads(turn.checkpoint)["trace"] == turn_id
    assert json.loads(turn.result)["trace"] == turn_id
    assert fu != turn_id  # its own turn id, the same trace


def test_a_retry_keeps_the_trace_and_shows_the_attempt(
        env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_PLAIN_CODE", "0")
    engine, agent = env["engine"], env["agent"]
    calls: dict[str, Any] = {"n": 0, "now": NOW.timestamp()}

    def flaky(messages: Any, tools: Any) -> ModelTurn:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the provider dropped the connection")
        return script(messages, tools)

    model = ScriptedModel(flaky)
    agent.harness.gateway.models = [model]
    agent.queue = TurnQueue(engine, clock=lambda: calls["now"])

    turn_id = submit(agent, "wamid.retry.1", "abeg wetin I owe now?")
    assert agent.process(turn_id) == turn_id  # attempt 1 fails and is released

    seen: list[dict[str, Any]] = []
    original = agent._complete

    def watch(holder: list[Any], *a: Any, **kw: Any) -> None:
        seen.append(correlation.fields())
        original(holder, *a, **kw)

    agent._complete = watch  # type: ignore[method-assign]
    calls["now"] = NOW.timestamp() + 60.0  # past the retry delay
    assert agent.process(turn_id) == turn_id  # attempt 2 finishes it

    # The same trace id on both attempts; the retry is labelled, not hidden.
    attempts = rows(engine, s.turn_attempts, s.turn_attempts.c.turn_id == turn_id)
    assert len(attempts) == 2
    turn = rows(engine, s.turns, s.turns.c.turn_id == turn_id)[0]
    result = json.loads(turn.result)
    assert result["trace"] == turn_id and result["attempt"] == 2
    assert seen and seen[-1]["trace"] == turn_id and seen[-1]["attempt"] == 2
    assert json.loads(turn.checkpoint)["trace"] == turn_id


def test_the_work_context_is_bound_while_the_turn_runs(env: Any) -> None:
    """Every log line inside the turn can name the trace, tenant and attempt."""
    agent = env["agent"]
    seen: list[dict[str, Any]] = []
    original = agent._complete

    def watch(holder: list[Any], *a: Any, **kw: Any) -> None:
        seen.append(correlation.fields())
        original(holder, *a, **kw)

    agent._complete = watch  # type: ignore[method-assign]
    turn_id = submit(agent, "wamid.bind.1", "Hello")
    run(agent, turn_id)
    assert seen and seen[-1] == {"trace": turn_id, "tenant": T,
                                 "thread": seen[-1]["thread"], "turn": turn_id,
                                 "attempt": 1}
    assert correlation.current() == ""  # the binding is released after the turn


def test_a_lease_lost_turn_keeps_its_trace_across_takeover(env: Any) -> None:
    engine, agent = env["engine"], env["agent"]
    turn_id = submit(agent, "wamid.takeover.1", "Hello")
    claim = agent.queue.claim("worker-a")
    assert claim is not None and claim.turn_id == turn_id
    # The worker dies: its lease lapses and another claim takes the turn over.
    with engine.begin() as conn:
        conn.execute(s.turns.update().where(s.turns.c.turn_id == turn_id)
                      .values(lease_expires_ts=0.0))
    second = agent.queue.claim("worker-b")
    assert second is not None and second.turn_id == turn_id and second.takeover

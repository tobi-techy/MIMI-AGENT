"""Helper inboxes and the handoff transport (docs/HELPER-INBOX-CONTRACT.md).

Offline: a SQLite file per test, code-only guards, no model. The happy path
and every unhappy one: a handoff to yourself, a handoff back up the chain, a
handoff past depth 2, a shadowed handoff, and the wake's own event shape.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import sqlalchemy as sa

from mimi.api import delegation as dlg
from mimi.api import inbox as ib
from mimi.api import reads
from mimi.clock import wall, wall_utc
from mimi.harness.inbound import Directory, Inbound, Payload
from mimi.harness.router import Router
from mimi.store import ensure_schema, make_engine
from mimi.store import schema as s
from mimi.store.util import dumps

T = "coral-lane"


def _engine(store_url: str) -> Any:
    engine = make_engine(store_url)
    ensure_schema(engine)
    return engine


def _rows(engine: Any, *where: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(s.helper_tasks).where(
            s.helper_tasks.c.tenant_id == T, *where)))


def _one(engine: Any, source_key: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(sa.select(s.helper_tasks).where(
            s.helper_tasks.c.tenant_id == T,
            s.helper_tasks.c.source_key == source_key)).first()


# --------------------------------------------------------------- the switch


def test_switch_defaults_off(monkeypatch: Any) -> None:
    monkeypatch.delenv("MIMI_HELPER_DELEGATION", raising=False)
    assert dlg.enabled() is False
    assert dlg.enabled({"MIMI_HELPER_DELEGATION": "on"}) is True
    assert dlg.enabled({"MIMI_HELPER_DELEGATION": "1"}) is True
    assert dlg.enabled({"MIMI_HELPER_DELEGATION": "0"}) is False


def test_outcome_is_never_empty_for_any_state() -> None:
    for state in ib.STATES:
        assert ib.outcome(state, "Proof").strip()
    assert ib.outcome("done", "Proof", result="No bank credit matches.") == \
        "No bank credit matches."
    assert ib.outcome("working", "Proof") == "Proof is working on it"


# --------------------------------------------------------------- the guards


def test_handoff_to_yourself_is_refused(store_url: str) -> None:
    engine = _engine(store_url)
    with engine.begin() as conn:
        spec = dlg.note(conn, T, source_key="k-self", from_helper="proof",
                        to_helper="proof", shop_id="", ask="look at it again",
                        turn_id="", now=wall())
    assert spec is None
    row = _one(engine, "k-self")
    assert row is not None and row.state == "refused"
    assert "loop" in row.reason
    engine.dispose()


def test_handoff_back_up_the_chain_is_refused(store_url: str) -> None:
    engine = _engine(store_url)
    with engine.begin() as conn:
        # Collector handed to Proof: chain ["collector", "proof"] on turn_child.
        ib.record(conn, T, source_key="p0", from_helper="collector", to_helper="proof",
                  skill="proof/fake-alert-check", shop_id="", thread_id="",
                  ask="check the screenshot", chain=["collector", "proof"], now=wall())
        conn.execute(s.helper_tasks.update().where(
            s.helper_tasks.c.tenant_id == T,
            s.helper_tasks.c.source_key == "p0").values(turn_id="turn_child"))
        # Proof now tries to hand back to Collector.
        spec = dlg.note(conn, T, source_key="k-back", from_helper="proof",
                        to_helper="collector", shop_id="", ask="collector look again",
                        turn_id="turn_child", now=wall())
    assert spec is None
    row = _one(engine, "k-back")
    assert row is not None and row.state == "refused"
    assert "already working on this" in row.reason
    engine.dispose()


def test_handoff_past_depth_two_is_refused(store_url: str) -> None:
    engine = _engine(store_url)
    with engine.begin() as conn:
        # A three-helper chain is already two handoffs deep; one more is refused.
        ib.record(conn, T, source_key="c0", from_helper="order", to_helper="order",
                  skill="", shop_id="", thread_id="", ask="x",
                  chain=["collector", "proof", "order"], now=wall())
        conn.execute(s.helper_tasks.update().where(
            s.helper_tasks.c.tenant_id == T,
            s.helper_tasks.c.source_key == "c0").values(turn_id="turn_deep"))
        spec = dlg.note(conn, T, source_key="k-deep", from_helper="order",
                        to_helper="credit", shop_id="", ask="check the limit",
                        turn_id="turn_deep", now=wall())
    assert spec is None
    row = _one(engine, "k-deep")
    assert row is not None and row.state == "refused"
    assert "handoffs deep" in row.reason
    engine.dispose()


def test_shadowed_handoff_writes_nothing(store_url: str) -> None:
    engine = _engine(store_url)
    with engine.begin() as conn:
        spec = dlg.note(conn, T, source_key="k-shadow", from_helper="collector",
                        to_helper="proof", shop_id="", ask="check the screenshot",
                        turn_id="", now=wall(), outcome="shadowed")
    assert spec is None
    assert _one(engine, "k-shadow") is None, "a shadowed assignment is not delivered work"
    engine.dispose()


def test_a_clean_handoff_records_a_queued_item(store_url: str) -> None:
    engine = _engine(store_url)
    with engine.begin() as conn:
        spec = dlg.note(conn, T, source_key="k-ok", from_helper="collector",
                        to_helper="proof", shop_id="shop-001",
                        ask="Check Mama Sola's payment screenshot", turn_id="", now=wall())
    assert spec is not None
    assert spec["skill"] == "proof/fake-alert-check"
    assert spec["chain"] == ["collector", "proof"]
    row = _one(engine, "k-ok")
    assert row.state == "queued" and row.turn_id == ""
    assert row.outcome == "Waiting for Proof"
    assert row.reason == "Asking Proof"
    engine.dispose()


# --------------------------------------------------------------- the read


def test_list_inbox_shows_items_and_the_helper_line(store_url: str) -> None:
    engine = _engine(store_url)
    with engine.begin() as conn:
        ib.record(conn, T, source_key="k1", from_helper="collector", to_helper="proof",
                  skill="proof/fake-alert-check", shop_id="", thread_id="",
                  ask="Check the screenshot", chain=["collector", "proof"], now=wall())
        ib.set_state_by_key(conn, T, "k1", "done",
                            result="No bank credit matches; likely a fake.", now=wall())
    out = reads.list_inbox(engine, T, helper="proof")
    assert out["total"] == 1
    item = out["items"][0]
    assert item["to_helper"] == "proof" and item["from_title"] == "Collector"
    assert item["state"] == "done" and item["contract"] == ib.CONTRACT_VERSION
    assert item["outcome"] == "No bank credit matches; likely a fake."
    assert item["result"].startswith("No bank credit")
    assert out["by_state"] == {"done": 1}
    assert out["helpers"] and out["helpers"][0]["helper"] == "proof"
    engine.dispose()


def test_list_inbox_surfaces_stored_intents_as_queued_items(store_url: str) -> None:
    engine = _engine(store_url)
    with engine.begin() as conn:
        conn.execute(s.intents.insert().values(
            tenant_id=T, idempotency_key="ik_intent_only", attempt_key="a",
            turn_id="", thread_id=f"{T}:owner", tool="ask_helper", intent_kind="task",
            tier="auto", shop_id="", payload=dumps({"helper": "credit",
                                                    "task": "Look at shop-005's limit"}),
            preview="Look at shop-005's limit", outcome="applied",
            created_at="2026-11-04T10:00:00+00:00"))
    out = reads.list_inbox(engine, T, helper="credit")
    assert out["total"] == 1
    assert out["items"][0]["to_helper"] == "credit"
    assert out["items"][0]["state"] == "queued"
    assert out["items"][0]["from_helper"] == "mimi"
    engine.dispose()


# --------------------------------------------------------------- the wake


class _FakeAgent:
    """Just enough of the API agent for ``delegation.wake``."""

    def __init__(self, engine: Any, turn_id: str = "turn_child") -> None:
        self.engine = engine
        self._turn_id = turn_id
        self.submitted: list[Inbound] = []

    def submit(self, tenant: str, inbound: Inbound, **kw: Any) -> Any:
        self.submitted.append(inbound)
        return SimpleNamespace(turn_id=self._turn_id)


def test_wake_sends_a_helper_finished_event_on_the_receiver_thread(store_url: str) -> None:
    engine = _engine(store_url)
    with engine.begin() as conn:
        spec = dlg.note(conn, T, source_key="k-wake", from_helper="collector",
                        to_helper="proof", shop_id="shop-001",
                        ask="Check Mama Sola's payment screenshot", turn_id="", now=wall())
    assert spec is not None
    agent = _FakeAgent(engine)
    tid = dlg.wake(agent, T, spec, wall())
    assert tid == "turn_child"
    inb = agent.submitted[0]
    assert inb.source == "helper_finished"
    assert inb.thread_id == dlg.receiver_thread(T, "proof")
    assert inb.payload.for_helper == "proof"
    assert inb.payload.for_skill == "proof/fake-alert-check"
    assert inb.payload.shop_id == "shop-001"
    row = _one(engine, "k-wake")
    assert row.state == "working" and row.turn_id == "turn_child"
    engine.dispose()


def test_a_helper_finished_event_routes_by_rule_to_the_receiver_skill() -> None:
    router = Router()
    directory = Directory(org_id=T, owner_thread=f"{T}:owner")
    inb = Inbound(source="helper_finished", org_id=T, external_id="delegate:itk_1",
                  received_at=wall_utc(), thread_id=dlg.receiver_thread(T, "proof"),
                  payload=Payload(text="Check the screenshot", for_helper="proof",
                                  for_skill="proof/fake-alert-check"))
    dec = router.route(inb, directory)
    assert dec.status == "routed"
    assert dec.helper == "proof"
    assert dec.skill == "proof/fake-alert-check"
    assert dec.layer == "rule"
    assert dec.model_called is False

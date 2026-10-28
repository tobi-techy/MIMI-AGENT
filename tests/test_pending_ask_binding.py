"""A pending ask binds the shop's next reply to the helper that asked (RAI-272).

Regression 6 of the RAI-272 e2e run: shop-008 answered its delivery receipt
with "only 4 ctn of that small sunrise reach / 2 ctn no come at all". Only a
bare YES/NO bound back to Order, so the general router sent it to
mimi/escalate-owner and the delivery was never flagged. The first test fails
on the code before the fix (the router picked escalate-owner) and passes on
it.

The rest: the classifier labels the answer and code binds; an unrelated
message goes through the normal router and the ask stays open; an expired
ask captures nothing; a binder model error falls back to the phrase rules;
escalate-owner on a delivery still records the flag on that delivery, never
consent and never a money move.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_api_helpers import KEYS, NOW, SCREENS, T, W, _h, _intents, _last_tool, script

from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.api.helpers import HelperDeps
from mimi.api.media import LocalMedia
from mimi.harness.binding import AnswerBinder, ask_is_live
from mimi.harness.gateway import ModelTurn, ScriptedModel, say
from mimi.harness.inbound import Inbound, Payload
from mimi.harness.router import Router, ThreadState
from mimi.models.base import ModelError
from mimi.models.cache import CacheMiss
from mimi.models.fake import FakeModel
from mimi.proof.reading import fake_reader
from mimi.receipts.verdict import fake_verdict
from mimi.stock.answers import fake_answer
from mimi.store import SqlGo, ensure_schema, make_engine, seed_world
from mimi.store import schema as s

SHOP = "shop-008"
DLV = "dlv-56-9007"  # 2 cartons of the 70g noodles short in the world
SHORT_REPLY = "Mimi na only 4 ctn of that small sunrise reach\n2 ctn no come at all"


def play(messages: Any, tools: Any) -> ModelTurn:
    """``script`` plus Order's dispute path and Mimi's escalation."""
    name, out = _last_tool(messages)
    names = {t["function"]["name"] for t in tools}
    if name == "read_receipt_reply" and out.get("action") in ("flag_dispute", "flag_unclear"):
        sid = out["shop_id"]
        return say("", ("flag_delivery_to_owner", {"shop_id": sid,
                                                   "delivery_id": out["delivery_id"]}),
                   ("send_to_user", {"channel": "shop_whatsapp", "shop_id": sid,
                                     "kind": "reply", "body": out["shop_reply"],
                                     "ends_turn": True}))
    if name == "read_receipt_reply" and out.get("action") == "clarify":
        return say("", ("ask_receipt_again", {"shop_id": out["shop_id"],
                                              "delivery_id": out["delivery_id"]}))
    if name == "" and "get_open_deliveries" not in names and "send_to_user" in names:
        # Mimi's chat or escalate skill: one line to the shop, one to the owner.
        newest = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        shop = newest.split('shop_id="', 1)[1].split('"', 1)[0] if 'shop_id="' in newest \
            else ""
        return say("", ("send_to_user", {"channel": "owner_console", "kind": "report",
                                         "body": f"{shop} sent a complaint.",
                                         "ends_turn": False}),
                   ("send_to_user", {"channel": "shop_whatsapp", "shop_id": shop,
                                     "kind": "reply", "body": "Thanks, I've told the owner.",
                                     "ends_turn": True}))
    return script(messages, tools)


def router_fake(binder: dict[str, Any] | None = None,
                route: str = "mimi/escalate-owner") -> FakeModel:
    """The router's model: the binder's label, or the general router's pick."""

    def respond(messages: Any, meta: Any) -> str:
        if (meta or {}).get("script") == "binder":
            if binder is None:
                raise ModelError("binder down")
            return json.dumps(binder)
        return json.dumps({"route": route, "confidence": 0.9, "second": "",
                           "language": "pcm"})

    return FakeModel(respond, name="muse-spark-1.3")


def make_api(store_url: str, router_model: Any = None) -> tuple[TestClient, Agent, Any]:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    from pathlib import Path

    truth = {r["sha256"]: r["truth"] for r in SCREENS.values()}
    deps = HelperDeps(vision=FakeModel(fake_reader(truth)), judge=FakeModel(fake_verdict),
                      stock=FakeModel(fake_answer),
                      media=LocalMedia(Path(__file__).resolve().parents[1] / "eval"))
    agent = Agent(engine, models=[ScriptedModel(play)], router_model=router_model, deps=deps,
                  clock=lambda: NOW, worker_id="t")
    keys = parse_keys(f"{T}:owner:{KEYS['owner']},{T}:channel:{KEYS['chan']}")
    app = create_app(Settings(database_url=store_url, keys=keys, workers=0), agent=agent,
                     workers=0)
    return TestClient(app), agent, engine


def receipt(c: TestClient, agent: Agent, when: Any = NOW) -> None:
    r = c.post("/v1/events", json={"event_id": f"d-{SHOP}", "kind": "webhook",
                                   "name": "delivery_completed", "shop_id": SHOP,
                                   "received_at": when.isoformat()}, headers=_h("chan"))
    assert r.status_code == 202, r.text
    agent.process(r.json()["turn_id"])


def reply(c: TestClient, agent: Agent, mid: str, text: str, when: Any) -> dict[str, Any]:
    v = c.post("/v1/turns", json={"message_id": mid, "phone": W.shop(SHOP).phone,
                                  "text": text, "received_at": when.isoformat()},
               headers=_h("chan")).json()
    if v.get("state") != "done":
        agent.process(v["turn_id"])
        v = c.get(f"/v1/turns/{v['turn_id']}", headers=_h("chan")).json()
    return dict(v)


def thread_state(engine: Any) -> dict[str, Any]:
    with engine.connect() as conn:
        raw = conn.execute(sa.select(s.threads.c.state).where(
            s.threads.c.tenant_id == T, s.threads.c.thread_id == f"{T}:shop:{SHOP}")).scalar()
    return dict(json.loads(raw or "{}"))


def flags(engine: Any) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [{"outcome": r.outcome, "tier": r.tier, **json.loads(r.payload)}
                for r in conn.execute(sa.select(s.intents).where(
                    s.intents.c.shop_id == SHOP,
                    s.intents.c.tool == "flag_delivery_to_owner"))]


# ---------------------------------------------------------- regression 6


def test_regression_6_shop_008_short_reply_binds_to_order_and_flags(store_url: str) -> None:
    """The e2e miss, offline: no binder model, the reply still binds."""
    c, agent, engine = make_api(store_url)
    with c:
        receipt(c, agent)
        st = thread_state(engine)
        assert st["pending_skill"] == "order/drop-confirmation"
        v = reply(c, agent, "r6-1", SHORT_REPLY, NOW + timedelta(minutes=20))
        # Before the fix: mimi/escalate-owner (the fast classifier's pick).
        assert v["skill"] == "order/drop-confirmation", v["route"]
        assert st.get("ask_ref") == DLV and st.get("asked_at")
        assert v["route"]["rule"].startswith("pending_bind:")
        tools = _intents(engine, SHOP)
        assert ("flag_delivery_to_owner", "applied") in tools
        assert not any(t == "record_delivery_consent" for t, _ in tools)
        f = flags(engine)
        assert len(f) == 1 and f[0]["delivery_id"] == DLV and f[0]["reason"] == "dispute"
        assert any(r["channel"] == "shop_whatsapp" and "Nothing has changed" in r["text"]
                   for r in v["replies"])
        # The ask is settled: nothing left open on the thread.
        assert thread_state(engine)["pending_skill"] == ""


def test_the_model_labels_the_answer_and_code_binds(store_url: str) -> None:
    c, agent, engine = make_api(store_url, router_fake({"label": "short", "confidence": 0.93}))
    with c:
        receipt(c, agent)
        v = reply(c, agent, "b-1", "goods short", NOW + timedelta(minutes=5))
        assert v["route"]["rule"] == "pending_bind:short"
        assert v["route"]["layer"] == "rule" and v["route"]["model_called"]
        assert ("flag_delivery_to_owner", "applied") in _intents(engine, SHOP)


def test_an_unrelated_question_goes_to_the_router_and_the_ask_stays_open(
        store_url: str) -> None:
    model = router_fake({"label": "unrelated", "confidence": 0.95}, route="mimi/chat")
    c, agent, engine = make_api(store_url, model)
    with c:
        receipt(c, agent)
        before = thread_state(engine)
        v = reply(c, agent, "u-1", "Good afternoon Mimi, how is your day",
                  NOW + timedelta(minutes=5))
        assert v["skill"] != "order/drop-confirmation"
        after = thread_state(engine)
        assert after["pending_skill"] == "order/drop-confirmation"
        assert after["asked_at"] == before["asked_at"] and after["ask_ref"] == DLV
        assert flags(engine) == []  # a chat is not a complaint
    # The real answer that follows still binds to Order.
    c2, agent2, engine2 = c, agent, engine
    agent2.router.binder = AnswerBinder(model=router_fake({"label": "short",
                                                           "confidence": 0.9}))
    with c2:
        v = reply(c2, agent2, "u-2", "2 ctn short", NOW + timedelta(minutes=9))
        assert v["skill"] == "order/drop-confirmation"
        assert ("flag_delivery_to_owner", "applied") in _intents(engine2, SHOP)


def test_low_confidence_unrelated_still_binds() -> None:
    b = AnswerBinder(model=router_fake({"label": "unrelated", "confidence": 0.4}))
    got = b.classify("order/drop-confirmation", "hmm")
    assert got.binds and got.label == "unclear"


def test_an_expired_ask_captures_nothing(store_url: str) -> None:
    c, agent, engine = make_api(store_url, router_fake({"label": "short", "confidence": 0.9},
                                                       route="mimi/chat"))
    with c:
        receipt(c, agent)
        v = reply(c, agent, "e-1", "goods short", NOW + timedelta(hours=49))
        assert not v["route"]["rule"].startswith("pending_")
        assert thread_state(engine)["pending_skill"] == ""
    assert ask_is_live("order/drop-confirmation", NOW.isoformat(), NOW + timedelta(hours=47))
    assert not ask_is_live("order/drop-confirmation", NOW.isoformat(),
                           NOW + timedelta(hours=49))
    assert not ask_is_live("stock/stock-count", NOW.isoformat(), NOW + timedelta(hours=25))
    assert not ask_is_live("order/drop-confirmation", "2026-10-26T22:30:00",  # no zone
                           NOW + timedelta(minutes=1))


def test_escalate_owner_on_an_open_receipt_still_flags_the_delivery(store_url: str) -> None:
    """Belt and braces: the binder released it and the router chose escalate."""
    model = router_fake({"label": "unrelated", "confidence": 0.9}, route="mimi/escalate-owner")
    c, agent, engine = make_api(store_url, model)
    with c:
        receipt(c, agent)
        v = reply(c, agent, "x-1", "Your driver insulted me, I will report una",
                  NOW + timedelta(minutes=30))
        assert v["skill"] == "mimi/escalate-owner"
        f = flags(engine)
        assert len(f) == 1 and f[0]["delivery_id"] == DLV
        assert f[0]["reason"] == "escalated" and f[0]["tier"] == "notify"
        assert f[0]["outcome"] == "applied"
        tools = [t for t, _ in _intents(engine, SHOP)]
        assert "record_delivery_consent" not in tools
        assert thread_state(engine)["pending_skill"] == ""
        # A later flag by Order on the same delivery is the same business key.
        assert SqlGo(engine).intents_of(T, SHOP, ("flag_delivery_to_owner",))[0][
            "idempotency_key"].startswith("ik_")


def test_escalate_owner_naming_a_delivery_flags_it_once(store_url: str) -> None:
    model = router_fake(None, route="mimi/escalate-owner")
    c, agent, engine = make_api(store_url, model)
    with c:
        v = reply(c, agent, "n-1", f"About {DLV}: this is a scam, I go call police",
                  NOW + timedelta(minutes=30))
        assert v["skill"] == "mimi/escalate-owner"
        v = reply(c, agent, "n-2", f"{DLV} I am still angry o", NOW + timedelta(minutes=40))
        f = flags(engine)
        assert len(f) == 1 and f[0]["delivery_id"] == DLV


def test_a_binder_model_error_falls_back_to_the_phrase_rules() -> None:
    b = AnswerBinder(model=router_fake(None))
    assert b.classify("order/drop-confirmation", "2 cartons short").binds
    lost = b.classify("order/drop-confirmation", "how much do I owe you now")
    assert lost.layer == "fallback"


def test_a_cache_miss_is_never_swallowed() -> None:
    class Missing:
        name = "m"

        def complete(self, *a: Any, **k: Any) -> Any:
            raise CacheMiss("no recorded answer")

    with pytest.raises(CacheMiss):
        AnswerBinder(model=Missing()).classify("order/drop-confirmation", "goods short")


def test_without_a_binder_the_router_keeps_the_old_rule() -> None:
    """Evals that build a bare Router (no binder) route exactly as before."""
    from mimi.store.book import SqlGo as Go

    e = make_engine("sqlite://")
    ensure_schema(e)
    seed_world(e, W)
    d = Go(e).directory(T)
    i = Inbound(source="shop_message", org_id=T, external_id="old", received_at=NOW,
                payload=Payload(text="YES", from_phone=W.shop(SHOP).phone))
    dec = Router().route(i, d, ThreadState(pending_skill="order/drop-confirmation",
                                           expects="yes_no"))
    assert dec.rule == "pending_answer:yes_no"

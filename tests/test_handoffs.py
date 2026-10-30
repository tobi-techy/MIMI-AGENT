"""Helper handoffs (docs/HANDOFFS.md): the typed handoff on every routed turn,
hand-backs that are never a dead end, answer cards from the book, safe chips,
and the owner assigning a helper.

Offline: scripted models and a SQLite file per test. The two cases from the
live demo recording are pinned here: "Who is the highest debtor?" and "Any
orders available now".
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mimi.api import cards as cx
from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.harness import claims as C
from mimi.harness import handoff as hx
from mimi.harness.gateway import ModelTurn, ScriptedModel, say
from mimi.harness.inbound import Directory, Inbound, Payload
from mimi.harness.router import Router, owner_rule
from mimi.harness.router_eval import OWNER_PHONE
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.world.loader import load_world

A = "coral-lane"
W = load_world()
OWNER_KEY = "owner-key-for-handoffs"
CHAN_KEY = "chan-key-for-handoffs"
DEFLECT = "For orders, the orders helper or your rep will help you with that."
_SHOP = re.compile(r'<shop_message shop_id="([^"]*)" untrusted="true">\n?(.*?)\n?</shop_message>',
                   re.S)


def _newest(messages: Any) -> str:
    return next(str(m["content"]) for m in reversed(messages) if m["role"] == "user")


def _system(messages: Any) -> str:
    return next((str(m["content"]) for m in messages if m["role"] == "system"), "")


def make_script(deflect_helpers: tuple[str, ...] = (), deflect: str = DEFLECT) -> Any:
    """Plays every helper. A helper named in ``deflect_helpers`` answers with a
    dead end (the Brand reply from the demo), and again after its repair."""

    def script(messages: Any, tools: Any) -> ModelTurn:
        last = messages[-1]
        newest = _newest(messages)
        system = _system(messages)
        who = next((h for h in ("Brand", "Collector", "Order", "Stock", "Proof", "Credit")
                    if f"You are {h}" in system), "Mimi")
        if last["role"] == "tool" and "held back" not in str(last.get("content", "")):
            return say("done")
        if "[Mimi harness]" in newest and who.lower() not in deflect_helpers:
            return say("nothing to add")
        owner = "<owner_message>" in newest or "[Mimi harness]" in newest
        if who.lower() in deflect_helpers:
            args: dict[str, Any] = {"channel": "owner_console", "kind": "reply", "body": deflect,
                                    "ends_turn": True}
            if not owner:
                m = _SHOP.search(newest)
                args = {"channel": "shop_whatsapp", "shop_id": m.group(1) if m else "",
                        "kind": "reply", "body": deflect, "ends_turn": True}
            return say("", ("send_to_user", args))
        if owner:
            return say("", ("send_to_user", {
                "channel": "owner_console", "kind": "reply", "ends_turn": True,
                "body": "Here is what the book says. It is a long paragraph with a lot in it. "
                        "And more detail after that."}))
        m = _SHOP.search(newest)
        if m is None:
            return say("Nothing to say.")
        return say("", ("send_to_user", {"channel": "shop_whatsapp", "shop_id": m.group(1),
                                          "kind": "reply", "body": "Got it, thank you.",
                                          "ends_turn": True}))

    return script


def _env(store_url: str, script: Any) -> tuple[TestClient, Agent, Any]:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    agent = Agent(engine, models=[ScriptedModel(script)], worker_id="handoff-test")
    keys = parse_keys(f"{A}:owner:{OWNER_KEY},{A}:channel:{CHAN_KEY}")
    app = create_app(Settings(keys=keys, wait_s=5), agent, workers=0)
    return TestClient(app), agent, engine


@pytest.fixture()
def env(store_url: str) -> Iterator[dict[str, Any]]:
    c, agent, engine = _env(store_url, make_script())
    with c:
        yield {"c": c, "agent": agent, "engine": engine}
    engine.dispose()


def owner_h() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER_KEY}"}


def chan() -> dict[str, str]:
    return {"Authorization": f"Bearer {CHAN_KEY}"}


def owner_turn(c: TestClient, mid: str, text: str, **extra: Any) -> dict[str, Any]:
    r = c.post("/v1/turns", json={"message_id": mid, "source": "owner_message",
                                  "phone": OWNER_PHONE, "text": text, **extra},
               headers=chan())
    assert r.status_code == 200, r.text
    return dict(r.json())


def owner_replies(t: dict[str, Any]) -> list[str]:
    return [r["text"] for r in t["replies"] if r["channel"] == "owner_console"]


# ------------------------------------------------------- 1. handoff record


def test_highest_debtor_is_handed_to_collector_with_a_typed_handoff(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "ho.1", "Who is the highest debtor?")
    ho = t["handoff"]
    assert ho["from_helper"] == "mimi" and ho["to_helper"] == "collector"
    assert ho["reason"] == "Asking Collector" and ho["status"] == "done"
    assert ho["skill"] == "collector/chase" and ho["hop"] == 0
    assert ho["contract"] == hx.CONTRACT_VERSION
    assert ho["steps"][0]["kind"] == "route" and ho["steps"][0]["name"].startswith("route:")
    assert any(s["kind"] == "model" for s in ho["steps"])
    assert all(isinstance(s["ms"], float) for s in ho["steps"])
    assert t["task_status"] == "done"
    # The thread shows the handoff before the answer, and Mimi posts the result back.
    th = env["c"].get(f"/v1/threads/{t['thread_id']}", headers=owner_h()).json()
    kinds = [s["type"] for s in th["steps"]]
    assert kinds.index("handoff") < kinds.index("handoff_result")
    hand = next(s for s in th["steps"] if s["type"] == "handoff")
    first_reply = next(m for m in th["messages"] if m["direction"] == "out")
    assert hand["at"] <= first_reply["at"]
    assert th["handoffs"][0]["handoff_id"] == ho["handoff_id"]


def test_highest_debtor_gets_a_shop_row_card_and_one_sentence(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "ho.2", "Who is the highest debtor?")
    card = t["card"]
    assert card["kind"] == "shop_row" and card["source"] == "book"
    row = card["shop_row"]
    # Figures from the book, summed in code.
    debtors = cx._debtors(env["agent"].go, A)
    top = debtors[0]
    assert row["shop_id"] == top.shop_id and row["owed_kobo"] == top.owed_kobo
    owed = sum(r["outstanding_kobo"] for r in env["agent"].go.receivables(A, top.shop_id))
    assert row["owed_kobo"] == owed and row["credit_limit_kobo"] > 0
    assert row["days_late"] >= 0 and row["owed_naira"].startswith("N")
    assert {a["action"] for a in card["actions"]} == {"remind", "open_shop"}
    (text,) = owner_replies(t)
    assert hx.sentence_count(text) == 1 and row["owed_naira"] in text and row["name"] in text


def test_a_working_turn_shows_the_handoff_before_the_answer(env: dict[str, Any]) -> None:
    c = env["c"]
    r = c.post("/v1/turns?wait=false", json={"message_id": "ho.w", "source": "owner_message",
                                            "phone": OWNER_PHONE,
                                            "text": "Who is the highest debtor?"},
               headers=chan())
    assert r.status_code == 202
    assert r.json()["task_status"] == "working"
    tasks = c.get("/v1/tasks", headers=owner_h()).json()
    assert tasks["tasks"] == [] or tasks["tasks"][0]["status"] == "working"
    env["agent"].process()
    done = c.get(f"/v1/turns/{r.json()['turn_id']}", headers=chan()).json()
    assert done["handoff"]["status"] == "done" and done["task_status"] == "done"


# ---------------------------------------------------------- 2. no dead end


def test_any_orders_goes_to_order_never_brand_and_answers_from_the_book(
        env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "ho.3", "Any orders available now")
    assert t["helper"] == "order" and t["skill"] == "order/open-orders"
    assert t["route"]["rule"] == "owner_rule:open-orders" and not t["route"]["model_called"]
    assert t["handoff"]["reason"] == "Asking Order"
    assert t["card"]["kind"] == "list" and t["card"]["title"] == "Open orders"
    (text,) = owner_replies(t)
    assert not C.deflections(text)
    assert t["route"]["cost_kobo"] == 0


@pytest.mark.parametrize("text,gold", [
    ("Any orders available now", "order/open-orders"),
    ("any new orders today?", "order/open-orders"),
    ("show me the orders", "order/open-orders"),
    ("what's in stock", "stock/low-stock-report"),
    ("stock levels please", "stock/low-stock-report"),
])
def test_owner_order_and_stock_questions_route_by_rule(text: str, gold: str) -> None:
    assert owner_rule(text) == gold
    d = _route("owner_message", text, OWNER_PHONE)
    assert d.skill == gold and d.layer == "rule" and not d.model_called


@pytest.mark.parametrize("text", [
    "is peak milk available?", "can I order Indomie", "what's in stock?",
    "what goods do you have", "any noodles available", "are the noodles in stock?"])
def test_shop_availability_goes_to_order_intake(text: str) -> None:
    d = _route("shop_message", text, W.shops[0].phone)
    assert d.skill == "order/order-intake" and d.layer == "lexical"


def test_a_promo_question_still_goes_to_brand() -> None:
    d = _route("shop_message", "any promo available this week?", W.shops[0].phone)
    assert d.skill == "brand/promo-info"


def _route(source: str, text: str, phone: str) -> Any:
    from datetime import UTC, datetime

    from mimi.harness.router_eval import directory

    ib = Inbound(source=source, org_id=A, external_id=f"x:{text}",  # type: ignore[arg-type]
                 received_at=datetime(2026, 10, 30, 9, 0, tzinfo=UTC),
                 payload=Payload(text=text, from_phone=phone))
    d: Directory = directory(W)
    return Router().route(ib, d)


def test_a_deflecting_helper_hands_back_and_mimi_reroutes_once(store_url: str) -> None:
    c, agent, engine = _env(store_url, make_script(deflect_helpers=("brand",)))
    with c:
        # "@Brand ... orders" forces the first hop to Brand, which deflects.
        t = owner_turn(c, "hb.1", "Any orders available now", assign_to="brand")
    engine.dispose()
    hb = t["handback"]
    assert hb["from_helper"] == "brand" and hb["code"] in ("deflected", "failed")
    assert hb["resolved"] == "rerouted" and hb["to_skill"] == "order/open-orders"
    assert t["skill"] == "order/open-orders" and t["handoff"]["hop"] == 1
    assert t["handoff"]["reason"] == "Brand handed it back; asking Order"
    assert any(s["kind"] == "handback" for s in t["handoff"]["steps"])
    texts = owner_replies(t)
    assert texts and not any(C.deflections(x) for x in texts)
    assert DEFLECT not in texts


def test_two_deflections_end_with_mimi_answering_from_the_book(store_url: str) -> None:
    c, agent, engine = _env(store_url, make_script(deflect_helpers=("brand", "collector")))
    with c:
        t = owner_turn(c, "hb.2", "@Brand who owes me the most money")
    engine.dispose()
    # Brand handed it to Collector, Collector deflected too: no third hop.
    assert t["handback"]["from_helper"] == "collector"
    assert t["handback"]["resolved"] == "mimi_answered"
    assert t["handoff"]["hop"] == 1
    assert t["card"]["kind"] == "shop_row"
    texts = owner_replies(t)
    assert len(texts) == 1 and not C.deflections(texts[0]) and DEFLECT not in texts


def test_a_hand_back_with_no_clear_helper_asks_one_question(store_url: str) -> None:
    c, agent, engine = _env(store_url, make_script(deflect_helpers=("brand",),
                                                   deflect="Please ask your rep."))
    with c:
        t = owner_turn(c, "hb.4", "@Brand sort out the thing for me")
    engine.dispose()
    assert t["handback"]["from_helper"] == "brand"
    assert t["handback"]["resolved"] == "asked_owner"
    assert t["status"] == "asked_to_clarify" and t["task_status"] == "needs_you"
    texts = owner_replies(t)
    assert len(texts) == 1 and "?" in texts[0] and not C.deflections(texts[0])


def test_a_shop_deflection_is_rerouted_to_order_intake(store_url: str) -> None:
    c, agent, engine = _env(store_url, make_script(deflect_helpers=("brand",)))
    with c:
        r = c.post("/v1/turns", json={"message_id": "hb.3", "phone": W.shops[2].phone,
                                      "text": "any promo? I want to order 5 cartons of Indomie"},
                   headers=chan())
    engine.dispose()
    t = r.json()
    if t["handback"] is None:  # routed straight to Order: no dead end either
        assert t["helper"] == "order"
        return
    assert t["handback"]["to_helper"] == "order"
    shop = [x["text"] for x in t["replies"] if x["channel"] == "shop_whatsapp"]
    assert shop and DEFLECT not in shop


@pytest.mark.parametrize("text,bad", [
    (DEFLECT, True),
    ("Please ask your rep.", True),
    ("Your rep can help you with that.", True),
    ("That is handled by the Order helper.", True),
    ("The right helper will handle it.", True),
    ("Ask your rep to add it to the order.", False),
    ("Your rep will confirm Peak Milk with you.", False),
    ("Mama Sola Provisions owes N3,497,300.", False),
])
def test_the_claims_gate_names_deflections(text: str, bad: bool) -> None:
    probs = C.check("send_to_user", text, messages=[], intents=[],
                    today=__import__("datetime").date(2026, 10, 30), wake_source="owner_message")
    assert ("deflection" in probs) is bad


# ---------------------------------------------------------- 3 and 4. cards, chips


def test_chips_are_two_or_three_and_safe(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "ch.1", "Who is the highest debtor?")
    chips = t["chips"]
    assert 2 <= len(chips) <= 3
    for ch in chips:
        assert ch["tier"] == "safe" and ch["label"]
        assert ch["intent"]["kind"] in ("turn", "open")
        assert hx.chip_is_safe(hx.Chip.model_validate(ch))


def test_no_chip_or_card_action_can_move_money_or_approve() -> None:
    for skill in ("collector/chase", "order/open-orders", "stock/low-stock-report",
                  "credit/credit-review", "proof/fake-alert-check", "mimi/chat"):
        for ch in hx.chips_for(skill, "owner"):
            assert hx.chip_is_safe(ch), ch
    bad = hx.Chip(label="Approve and pay", intent=hx.ChipIntent(kind="turn", text="pay it"))
    assert not hx.chip_is_safe(bad)
    assert hx.safe_chips([bad]) == []
    assert hx.chips_for("collector/chase", "shop") == []


def test_open_orders_card_lists_pending_orders_from_the_book(env: dict[str, Any]) -> None:

    from mimi.store import schema as s
    from mimi.store.util import dumps

    payload = {"order_id": "ord-test-1", "lines": [
        {"sku": "NDL-70-CTN", "quantity": 5, "line_total_kobo": 1_250_000},
        {"sku": "DET-900G", "quantity": 2, "line_total_kobo": 300_000}], "over_credit": False}
    with env["engine"].begin() as conn:
        conn.execute(s.approvals.insert().values(
            tenant_id=A, approval_id="apr-test-1", idempotency_key="k-test-1", turn_id="t-x",
            thread_id=f"{A}:shop:{W.shops[1].shop_id}", helper="order",
            skill="order/order-intake", shop_id=W.shops[1].shop_id, tool="submit_order",
            intent_kind="order", preview="", payload=dumps(payload), status="pending",
            supersedes="", superseded_by="", decided_by="", note="",
            created_at="2026-10-30T09:00:00+01:00", created_ts=1.0, decided_at=None))
    t = owner_turn(env["c"], "oo.1", "any new orders?")
    card = t["card"]
    assert card["total"] == 1 and card["items"][0]["qty"] == 7
    assert card["items"][0]["amount_kobo"] == 1_550_000
    assert card["items"][0]["amount_naira"] == "N15,500"
    (text,) = owner_replies(t)
    assert text == "1 order is waiting for your approval, N15,500 in all."


def test_a_card_is_never_built_for_a_shop(env: dict[str, Any]) -> None:
    r = env["c"].post("/v1/turns", json={"message_id": "sh.1", "phone": W.shops[0].phone,
                                         "text": "how much do I owe?"}, headers=chan())
    t = r.json()
    assert t["card"] is None and t["chips"] == []
    assert t["handoff"]["to_helper"] == "collector"


# --------------------------------------------------------------- 5. assign


def test_assign_to_overrides_routing_for_the_owner(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "as.1", "how is shop-004 doing", assign_to="collector")
    assert t["helper"] == "collector" and t["route"]["rule"] == "owner_assign:@collector"
    assert t["handoff"]["via"] == "assign"


def test_a_leading_mention_assigns(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "as.2", "@Orders any orders now?")
    assert t["skill"] == "order/open-orders" and t["handoff"]["via"] == "mention"
    assert hx.leading_mention("@Collector chase them") == "collector"
    assert hx.leading_mention("hi @Collector") == ""


def test_only_the_owner_can_assign(env: dict[str, Any]) -> None:
    r = env["c"].post("/v1/turns", json={"message_id": "as.3", "phone": W.shops[0].phone,
                                         "text": "approve my credit", "assign_to": "credit"},
                      headers=chan())
    assert r.status_code == 422
    r = env["c"].post("/v1/turns", json={"message_id": "as.4", "source": "owner_message",
                                         "phone": OWNER_PHONE, "text": "x",
                                         "assign_to": "bank"}, headers=chan())
    assert r.status_code == 422


def test_assignment_never_reaches_a_skill_the_owner_cannot(env: dict[str, Any]) -> None:
    # Credit for the owner is the review only, never a shop's credit request.
    t = owner_turn(env["c"], "as.5", "give shop-004 more credit now", assign_to="credit")
    assert t["skill"] == "credit/credit-review"


def test_tasks_endpoint_reports_status_per_helper(env: dict[str, Any]) -> None:
    c = env["c"]
    owner_turn(c, "tk.1", "Who is the highest debtor?")
    owner_turn(c, "tk.2", "any orders now")
    r = c.get("/v1/tasks", headers=owner_h())
    assert r.status_code == 200
    body = r.json()
    helpers = {t["helper"] for t in body["tasks"]}
    assert {"collector", "order"} <= helpers
    assert body["by_status"].get("done", 0) >= 2
    only = c.get("/v1/tasks?helper=order", headers=owner_h()).json()
    assert {t["helper"] for t in only["tasks"]} == {"order"}
    assert c.get("/v1/tasks", headers=chan()).status_code in (401, 403)
    assert c.get("/v1/tasks?status=bogus", headers=owner_h()).status_code == 422


# --------------------------------------------------------------- contract


def test_the_contract_doc_has_an_example_for_each_feature_and_they_validate() -> None:
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "HANDOFFS.md").read_text()
    blocks = re.findall(r"```json\n(.*?)\n```", doc, re.S)
    assert len(blocks) >= 5
    by_kind = {}
    for b in blocks:
        data = json.loads(b)
        for k in ("handoff", "handback", "card", "chips", "assign_to", "tasks"):
            if k in data:
                by_kind[k] = data
    for k in ("handoff", "handback", "card", "chips", "assign_to", "tasks"):
        assert k in by_kind, k
    hx.Handoff.model_validate(by_kind["handoff"]["handoff"])
    hx.Handback.model_validate(by_kind["handback"]["handback"])
    hx.Card.model_validate(by_kind["card"]["card"])
    for ch in by_kind["chips"]["chips"]:
        assert hx.chip_is_safe(hx.Chip.model_validate(ch))


def test_first_sentence_keeps_one_sentence() -> None:
    assert hx.first_sentence("Mama Sola owes N3,497,300. She is 45 days late.") == (
        "Mama Sola owes N3,497,300.")
    assert hx.sentence_count(hx.first_sentence("a " * 300)) == 1

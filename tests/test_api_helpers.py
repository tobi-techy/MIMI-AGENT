"""The scene helpers behind the router on the API (RAI-272), offline.

Order (delivery receipt and YES), Proof (screenshots), Stock (one-tap count
and restock draft) on the real store and queue, with a scripted harness
model and the flows' own fake readers. Also: system events through
``POST /v1/events``, the owner taken first from the queue, and the shop
notice an approve-tier intent sends at once (found by the e2e run).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.api.helpers import HelperDeps
from mimi.api.media import LocalMedia
from mimi.harness.gateway import ModelTurn, ScriptedModel, say
from mimi.harness.inbound import Inbound, Payload
from mimi.models.fake import FakeModel
from mimi.proof.reading import fake_reader
from mimi.receipts.verdict import fake_verdict
from mimi.stock.answers import fake_answer
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.world.loader import load_world

ROOT = Path(__file__).resolve().parents[1]
W = load_world()
T = W.tenant_id
KEYS = {"owner": "h-owner-key-0000000001", "chan": "h-chan-key-00000000001"}
SCREENS = {r["id"]: r for r in map(json.loads, (ROOT / "eval/proof/screens.jsonl")
                                   .read_text().splitlines())}
NOW = datetime(2026, 10, 26, 22, 30, tzinfo=UTC)


def _last_tool(messages: Any) -> tuple[str, dict[str, Any]]:
    m = messages[-1]
    if m.get("role") != "tool":
        return "", {}
    # Scripted call ids are "call_<i>_<tool name>" (``say``).
    name = str(m.get("tool_call_id", "")).split("_", 2)[-1]
    try:
        body = json.loads(m["content"])
    except (ValueError, TypeError):
        return name, {}
    return name, dict(body.get("data") or {}) if isinstance(body, dict) else {}


def script(messages: Any, tools: Any) -> ModelTurn:
    """Plays each helper: follows the tools' next_step, as the skills say."""
    name, out = _last_tool(messages)
    newest = next(m["content"] for m in reversed(messages) if m["role"] == "user")
    shop = newest.split('shop_id="', 1)[1].split('"', 1)[0] if 'shop_id="' in newest else ""
    names = {t["function"]["name"] for t in tools}
    if name == "":
        if "Wake source: approval_answered" in newest:
            return say("Nothing to send.")
        if "get_open_deliveries" in names:
            return say("", ("get_open_deliveries", {"shop_id": shop}))
        if "check_payment_screenshot" in names and "ph_" in newest:
            pid = "ph_" + newest.split("ph_", 1)[1][:12]
            return say("", ("check_payment_screenshot", {"shop_id": shop, "photo_id": pid}))
        if "get_stock_estimates" in names:
            return say("", ("get_stock_estimates", {"shop_id": shop}))
        return say("Nothing to say.")
    if not out:
        return say("done")  # a tool error: end the turn
    sid = out.get("shop_id", shop)
    if name == "get_open_deliveries":
        d = out["deliveries"][0]
        if not d["receipt_sent"]:
            return say("", ("send_delivery_receipt", {"shop_id": sid,
                                                      "delivery_id": d["delivery_id"]}))
        return say("", ("read_receipt_reply", {"shop_id": sid,
                                               "delivery_id": d["delivery_id"]}))
    if name == "read_receipt_reply" and out["action"] == "consent":
        ids = {"shop_id": sid, "delivery_id": out["delivery_id"]}
        return say("", ("record_delivery_consent", ids),
                   ("send_to_user", {"channel": "shop_whatsapp", "shop_id": sid,
                                     "kind": "reply", "body": out["shop_reply"],
                                     "ends_turn": True}))
    if name == "check_payment_screenshot":
        ids = {"shop_id": sid, "photo_id": out["photo_id"]}
        if out["verdict"] == "likely_genuine":
            return say("", ("record_payment_proof", ids),
                       ("send_to_user", {"channel": "shop_whatsapp", "shop_id": sid,
                                         "kind": "reply", "body": out["shop_reply"],
                                         "ends_turn": True}))
        if out["verdict"] == "looks_fake":
            # Flag, and (against next_step) send the same reply too: the shop
            # must still get it exactly once.
            return say("", ("flag_payment_proof", ids),
                       ("send_to_user", {"channel": "shop_whatsapp", "shop_id": sid,
                                         "kind": "reply", "body": out["shop_reply"],
                                         "ends_turn": True}))
    if name == "get_stock_estimates" and out["ask_sku"]:
        return say("", ("ask_stock_count", {"shop_id": sid, "sku": out["ask_sku"]}))
    if name == "get_stock_estimates" and out["open_question_sku"]:
        return say("", ("read_stock_answer", {"shop_id": sid,
                                              "sku": out["open_question_sku"]}))
    if name == "read_stock_answer" and out["restock_quantity"]:
        ids = {"shop_id": sid, "sku": out["sku"]}
        return say("", ("record_stock_count", ids), ("draft_restock", ids))
    return say("done")


@pytest.fixture()
def api(store_url: str) -> Any:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    truth = {r["sha256"]: r["truth"] for r in SCREENS.values()}
    deps = HelperDeps(vision=FakeModel(fake_reader(truth)), judge=FakeModel(fake_verdict),
                      stock=FakeModel(fake_answer), media=LocalMedia(ROOT / "eval"))
    agent = Agent(engine, models=[ScriptedModel(script)], deps=deps, clock=lambda: NOW,
                  worker_id="t")
    keys = parse_keys(f"{T}:owner:{KEYS['owner']},{T}:channel:{KEYS['chan']}")
    app = create_app(Settings(database_url=store_url, keys=keys, workers=0), agent=agent,
                     workers=0)
    with TestClient(app) as c:
        yield c, agent, engine


def _h(k: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {KEYS[k]}"}


def _intents(engine: Any, shop: str) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        return [(r.tool, r.outcome) for r in conn.execute(sa.select(s.intents).where(
            s.intents.c.shop_id == shop).order_by(s.intents.c.created_at))]


def test_the_queue_takes_the_owner_first(api: Any) -> None:
    _, agent, _ = api
    for i, sid in enumerate(("shop-001", "shop-002")):
        agent.submit(T, Inbound(source="shop_message", org_id=T, external_id=f"q{i}",
                                received_at=NOW, payload=Payload(
                                    text="hi", from_phone=W.shop(sid).phone)))
    owner = agent.submit(T, Inbound(source="owner_message", org_id=T, external_id="q-owner",
                                    received_at=NOW, payload=Payload(text="hello")))
    claim = agent.queue.claim("w")
    assert claim is not None and claim.turn_id == owner.turn_id


def test_delivery_receipt_and_yes_through_events_and_turns(api: Any) -> None:
    c, agent, engine = api
    r = c.post("/v1/events", json={"event_id": "d1", "kind": "webhook",
                                   "name": "delivery_completed", "shop_id": "shop-040"},
               headers=_h("chan"))
    assert r.status_code == 202, r.text
    assert c.post("/v1/events", json={"event_id": "d2", "kind": "webhook",
                                      "name": "delivery_completed", "shop_id": "shop-999"},
                  headers=_h("chan")).status_code == 404
    agent.process(r.json()["turn_id"])
    v = c.get(f"/v1/turns/{r.json()['turn_id']}", headers=_h("chan")).json()
    assert v["skill"] == "order/drop-confirmation" and v["route"]["layer"] == "rule"
    assert [x["kind"] for x in v["replies"]] == ["send_delivery_receipt"]
    assert v["replies"][0]["text"].endswith("Reply YES to confirm.")
    yes = c.post("/v1/turns", json={"message_id": "y1", "phone": W.shop("shop-040").phone,
                                    "text": "YES"}, headers=_h("chan")).json()
    assert yes["route"]["rule"] == "pending_answer:yes_no"
    assert ("record_delivery_consent", "applied") in _intents(engine, "shop-040")
    assert yes["replies"][-1]["text"] == "Thank you. Delivery dlv-56-9039 is confirmed."


def _screenshot(c: Any, sid: str, shop: str, mid: str) -> dict[str, Any]:
    row = SCREENS[sid]
    return dict(c.post("/v1/turns", json={
        "message_id": mid, "phone": W.shop(shop).phone,
        "received_at": row["received_at"],
        "media": {"kind": "image", "ref": f"local:proof/{row['file']}", "mime": "image/jpeg",
                  "sha256": row["sha256"], "width": 540, "height": 1100}},
        headers=_h("chan")).json())


def test_a_genuine_screenshot_is_likely_and_a_fake_goes_to_approvals(api: Any) -> None:
    c, _, engine = api
    g = _screenshot(c, "g03", "shop-038", "p1")
    assert g["skill"] == "proof/fake-alert-check" and g["route"]["rule"] == "screenshot"
    assert "likely paid" in g["replies"][-1]["text"]
    f = _screenshot(c, "f09", "shop-038", "p2")
    assert f["status"] == "parked_for_approval"
    shop_msgs = [x for x in f["replies"] if x["channel"] == "shop_whatsapp"]
    # The neutral reply reaches the shop once, at once, though the turn parks.
    assert len(shop_msgs) == 1 and shop_msgs[0]["status"] == "ready"
    assert "confirm with the bank" in shop_msgs[0]["text"]
    assert ("flag_payment_proof", "queued_for_approval") in _intents(engine, "shop-038")
    apr = c.get("/v1/approvals", headers=_h("owner")).json()["approvals"]
    assert [a["tool"] for a in apr] == ["flag_payment_proof"]
    assert apr[0]["draft"] is None  # the notice is not an approvals draft
    assert "time_not_future" in apr[0]["payload"]["failed_rules"]


def test_a_screenshot_whose_bytes_do_not_match_its_hash_is_not_read(api: Any) -> None:
    c, _, engine = api
    row = SCREENS["g03"]
    v = c.post("/v1/turns", json={
        "message_id": "p9", "phone": W.shop("shop-038").phone,
        "media": {"kind": "image", "ref": "local:proof/screens/g01.jpg", "mime": "image/jpeg",
                  "sha256": row["sha256"], "width": 540, "height": 1100}},
        headers=_h("chan")).json()
    assert v["state"] == "done"
    assert not [t for t, _ in _intents(engine, "shop-038") if "payment_proof" in t]


def test_stock_check_tap_and_restock_draft(api: Any) -> None:
    c, agent, engine = api
    r = c.post("/v1/events", json={"event_id": "s1", "kind": "routine",
                                   "name": "stock.weekly_check", "shop_id": "shop-012"},
               headers=_h("chan")).json()
    agent.process(r["turn_id"])
    v = c.get(f"/v1/turns/{r['turn_id']}", headers=_h("chan")).json()
    assert v["skill"] == "stock/stock-count"
    ask = v["replies"][0]
    assert ask["kind"] == "ask_stock_count" and ask["text"].endswith("0-2 | 3-5 | 6+")
    tap = c.post("/v1/turns", json={"message_id": "s2", "phone": W.shop("shop-012").phone,
                                    "text": "0-2", "button_id": "btn:stock/stock-count"},
                 headers=_h("chan")).json()
    assert tap["skill"] == "stock/stock-count"
    tools = _intents(engine, "shop-012")
    assert ("record_stock_count", "applied") in tools
    assert ("draft_restock", "queued_for_approval") in tools
    notice = [x for x in tap["replies"] if x["kind"] == "draft_restock_notice"]
    assert len(notice) == 1 and "prepare" in notice[0]["text"]
    apr = c.get("/v1/approvals", headers=_h("owner")).json()["approvals"]
    d = next(a for a in apr if a["tool"] == "draft_restock")
    assert d["payload"]["places_order"] is False and d["payload"]["status"] == "draft"


def test_a_resent_screenshot_is_a_resend_not_a_new_payment(api: Any) -> None:
    """RAI-273 week run: the same shop sending the same image again was checked as
    new (its proof id repeats, and the earlier record was skipped as "this one"),
    so a genuine resend could fail a time rule and be flagged as a fake."""
    c, _, engine = api
    _screenshot(c, "g03", "shop-038", "p1")
    again = _screenshot(c, "g03", "shop-038", "p1-again")
    assert again["skill"] == "proof/fake-alert-check"
    with engine.connect() as conn:
        shorts = [r.short for r in conn.execute(sa.select(s.thread_events).where(
            s.thread_events.c.turn_id == again["turn_id"],
            s.thread_events.c.type == "tool_result"))]
    assert any(x.startswith("duplicate") for x in shorts), shorts
    tools = _intents(engine, "shop-038")
    assert tools.count(("record_payment_proof", "applied")) == 1
    assert not [t for t, _ in tools if t == "flag_payment_proof"]

"""Brand's Promo Check (RAI-272): reads promos, drafts emails, never sends, never a figure."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.api.agent import Agent
from mimi.brand.tools import PROMOS, promo_reply, promos_on
from mimi.harness.gateway import ModelTurn, ScriptedModel, say
from mimi.harness.inbound import Inbound, Payload
from mimi.harness.policy import ALWAYS_APPROVE, GoPolicy
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.world.loader import load_world

W = load_world()
NOW = datetime(2026, 10, 26, 21, 0, tzinfo=UTC)


def test_promos_by_day_and_brand_and_the_code_written_reply() -> None:
    day = date(2026, 10, 26)
    assert {p.brand for p in promos_on(day)} == {"Sunrise", "Okin"}  # Palmside ended
    assert [p.brand for p in promos_on(day, "sunrise noodles")] == ["Sunrise"]
    assert promos_on(day, "Palmside") == []
    text = promo_reply(promos_on(day, "Sunrise"), "Sunrise", "Coral Lane Trading")
    assert "buy 10 cartons, get 1 carton free" in text and "N" not in text.split(":")[0]
    assert "don't see any promo from Palmside" in promo_reply([], "Palmside", "Coral Lane")
    assert all("discount" not in p.terms.lower() for p in PROMOS)


def test_brand_tools_are_read_and_approve_only() -> None:
    from mimi.api.helpers import build_helpers
    from mimi.store.book import SqlGo

    e = make_engine("sqlite://")
    ensure_schema(e)
    seed_world(e, W)
    brand = build_helpers(go=SqlGo(e))["brand"]
    tiers = {sp.name: GoPolicy().base_tier(sp.name) for sp in brand.toolset.specs}
    assert tiers["get_brand_promos"] == "auto" and tiers["draft_brand_email"] == "approve"
    assert "draft_brand_email" in ALWAYS_APPROVE
    assert not {"send_reminder", "notify_rep", "give_discount"} & set(tiers)


def _script(messages: Any, tools: Any) -> ModelTurn:
    last = messages[-1]
    newest = next(m["content"] for m in reversed(messages) if m["role"] == "user")
    if last.get("role") != "tool":
        if "email" in newest.lower():
            return say("", ("draft_brand_email", {
                "to_brand": "Sunrise", "subject": "More posters for our shops",
                "body": "Good day. Could you send more Sunrise posters for our shops? "
                        "Thank you."}))
        return say("", ("get_brand_promos", {"brand": "Sunrise"}))
    body = json.loads(last["content"]).get("data") or {}
    if "shop_reply" in body:
        return say("", ("send_to_user", {"channel": "shop_whatsapp", "shop_id": "shop-012",
                                         "kind": "reply", "body": body["shop_reply"],
                                         "ends_turn": True}))
    if body.get("status") == "draft_for_owner":
        return say("", ("send_to_user", {"channel": "owner_console", "kind": "reply",
                                         "body": "The draft is in approvals.",
                                         "ends_turn": True}))
    return say("done")


@pytest.fixture()
def agent() -> Any:
    e = make_engine("sqlite://")
    ensure_schema(e)
    seed_world(e, W)
    return Agent(e, models=[ScriptedModel(_script)], router_model=None, lease_s=30.0,
                 clock=lambda: NOW)


def test_shop_asks_about_a_promo_and_gets_the_code_reply(agent: Any) -> None:
    v = agent.submit("coral-lane", Inbound(
        source="shop_message", org_id="coral-lane", external_id="b1", received_at=NOW,
        payload=Payload(text="Any promo on Sunrise noodles this month?",
                        from_phone=W.shop("shop-012").phone)))
    while agent.process() is not None:
        pass
    view = agent.view("coral-lane", v.turn_id)
    assert view.skill == "brand/promo-info" and view.state == "done"
    shop = [r for r in view.replies if r["channel"] == "shop_whatsapp"]
    assert len(shop) == 1 and "get 1 carton free" in shop[0]["text"]


def test_owner_brand_email_is_a_draft_in_approvals_never_sent(agent: Any) -> None:
    v = agent.submit("coral-lane", Inbound(
        source="owner_message", org_id="coral-lane", external_id="b2", received_at=NOW,
        payload=Payload(text="Draft an email to Sunrise asking for more posters")))
    while agent.process() is not None:
        pass
    with agent.engine.connect() as conn:
        apr = conn.execute(sa.select(s.approvals).where(
            s.approvals.c.tool == "draft_brand_email")).all()
        msgs = conn.execute(sa.select(s.messages).where(
            s.messages.c.turn_id == v.turn_id, s.messages.c.direction == "out")).all()
    view = agent.view("coral-lane", v.turn_id)
    assert view.skill == "brand/brand-email"
    assert len(apr) == 1 and apr[0].status == "pending"
    assert all(m.channel == "owner_console" for m in msgs)
    assert [m.status for m in msgs if m.kind == "draft_brand_email"] == ["pending_approval"]

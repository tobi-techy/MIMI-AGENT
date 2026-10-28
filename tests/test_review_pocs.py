"""Proofs of concept from the outside code review of main.

Every case in ``mimi.harness.review_pocs.POCS`` is a real attack or failure
run against the code (the same ones the harness eval's security suite runs).
Each failed on main before its fix; each must hold now. The API-level cases
below need the HTTP agent, which the harness may not import.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.harness.gateway import ScriptedModel, say
from mimi.harness.review_pocs import POCS
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.world.loader import load_world

W = load_world()


@pytest.mark.parametrize("cid", sorted(POCS))
def test_review_poc_holds(cid: str) -> None:
    category, fn = POCS[cid]
    held, detail = fn()
    assert held, f"{cid} ({category}): {detail}"


def _agent(store_url: str, model: Any, *, shadow: str = "") -> Any:
    from mimi.api.agent import Agent
    from mimi.api.helpers import build_helpers
    from mimi.harness.policy import GoPolicy
    from mimi.store import SqlGo

    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    policy = GoPolicy()
    helpers = dict(build_helpers(policy, go=SqlGo(engine)))
    if shadow:
        helpers[shadow] = dataclasses.replace(helpers[shadow], shadow=True)
    return Agent(engine, models=[model], helpers=helpers, policy=policy,
                 worker_id="poc-worker"), engine


def test_a_shadow_helper_sends_and_applies_nothing_through_the_api(store_url: str) -> None:
    """Shadow mode end to end: the turn runs, Go records the intents as
    shadowed, and no message, approval or credit row is written."""
    from mimi.harness.inbound import Inbound, Payload

    inv = next(r.delivery_id for r in W.receivables
               if r.shop_id == "shop-001" and r.status == "overdue")

    def script(messages: Any, tools: Any) -> Any:
        if messages[-1]["role"] == "tool":
            return say("done")
        return say("", ("send_reminder", {"shop_id": "shop-001", "invoice_id": inv,
                                          "tone": "firm"}),
                   ("send_to_user", {"channel": "shop_whatsapp", "shop_id": "shop-001",
                                     "kind": "reply", "body": "We have your balance.",
                                     "ends_turn": True}))

    agent, engine = _agent(store_url, ScriptedModel(script), shadow="collector")
    shop = W.shop("shop-001")
    agent.submit(W.tenant_id, Inbound(
        source="shop_message", org_id=W.tenant_id, external_id="wamid.shadow.1",
        received_at=_now(),
        payload=Payload(from_phone=shop.phone, text="how much I owe?")))
    while agent.process() is not None:
        pass
    with engine.connect() as conn:
        outs = conn.execute(sa.select(s.messages.c.kind, s.messages.c.status).where(
            s.messages.c.direction == "out")).all()
        intents = conn.execute(sa.select(s.intents.c.tool, s.intents.c.outcome)).all()
        aprs = conn.execute(sa.select(sa.func.count()).select_from(s.approvals)).scalar()
    assert intents, "the shadow run proposed nothing; the PoC did not run"
    assert {o for _, o in intents} == {"shadowed"}, intents
    assert not aprs
    assert not [o for o in outs if o.kind in ("reply", "send_reminder", "reminder")], outs


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime(2026, 10, 26, 9, 0, tzinfo=UTC)

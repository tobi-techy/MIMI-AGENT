"""Two open asks on one thread (RAI-272 binder bug): a bare YES binds to neither.

Dev cases only, worded differently from the held-out set in eval/which/.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.api.which_eval import THREAD, run_case
from mimi.harness import steer as st
from mimi.store import schema as s
from mimi.world.loader import load_world

W = load_world()


def case(cid: str, open_: str, task: str, replies: list[str], expect: list[str]
         ) -> dict[str, Any]:
    return {"id": cid, "open": open_, "task": task, "replies": replies, "expect": expect}


@pytest.mark.parametrize("c", [
    case("dev1", "both", "credit", ["Yes"], ["which"]),
    case("dev2", "both", "restock", ["No"], ["which"]),
    case("dev3", "both", "restock", ["yes", "for the delivery"], ["which", "receipt"]),
    case("dev4", "both", "credit", ["yes", "credit change"], ["which", "fix"]),
    case("dev5", "both", "restock", ["yes", "hmm"], ["which", "owner"]),
    case("dev6", "both", "credit", ["yes everything arrived"], ["receipt"]),
    case("dev7", "both", "restock", ["yes change to 6"], ["fix"]),
    case("dev8", "receipt", "", ["yes"], ["receipt"]),
    case("dev9", "fix", "credit", ["yes"], ["fix"]),
], ids=lambda c: c["id"])
def test_two_open_asks(c: dict[str, Any]) -> None:
    r = run_case(W, c)
    assert r["ok"], r


def test_question_is_code_written_and_names_both_asks() -> None:
    assert st.which_question("yes", "draft_restock", "make am 6") == \
        "Yes to the delivery receipt or the order change?"
    assert st.which_question("no", "submit_credit_review", "please") == \
        "No to the delivery receipt or the credit change?"
    assert st.which_side("both of them") == "both"
    assert st.yes_no_lead("Yes o, the goods reach") == ("yes", "the goods reach")


def test_which_question_is_asked_once_then_the_owner_checks() -> None:
    from mimi.api import which_eval as we
    from mimi.api.agent import Agent
    from mimi.api.offline import OfflineModel
    from mimi.harness.inbound import Inbound, Payload
    from mimi.store import ensure_schema, make_engine, seed_world

    e = make_engine("sqlite://")
    ensure_schema(e)
    seed_world(e, W)
    we._setup(e, W, case("once", "both", "credit", [], []))
    a = Agent(e, models=[OfflineModel()], router_model=None, lease_s=30.0)
    phone = W.shop(we.SHOP).phone
    for n, text in enumerate(["yes", "yes", "yes"]):
        a.submit(we.TENANT, Inbound(source="shop_message", org_id=we.TENANT,
                                    external_id=f"once.{n}", received_at=we.T0,
                                    payload=Payload(text=text, from_phone=phone)))
        while a.process() is not None:
            pass
    with e.connect() as conn:
        statuses = [r.status for r in conn.execute(sa.select(s.turns.c.status).order_by(
            s.turns.c.created_ts))]
        shop_out = conn.execute(sa.select(s.messages.c.text).where(
            s.messages.c.direction == "out", s.messages.c.channel == "shop_whatsapp")
        ).scalars().all()
        owner = conn.execute(sa.select(s.messages.c.text).where(
            s.messages.c.direction == "out", s.messages.c.channel == "owner_console")
        ).scalars().all()
        state = json.loads(conn.execute(sa.select(s.threads.c.state).where(
            s.threads.c.thread_id == THREAD)).scalar())
        consent = conn.execute(sa.select(sa.func.count()).select_from(s.intents).where(
            s.intents.c.tool == "record_delivery_consent")).scalar()
    assert statuses == ["steer_which", "steer_which_owner", "steer_which_owner"]
    assert sum("change?" in t for t in shop_out) == 1
    assert len(owner) == 2 and all("did not say which" in t for t in owner)
    assert state["pending_skill"] == "order/drop-confirmation" and state["fix"]
    assert consent == 0


def test_the_answer_turn_reads_the_shops_yes_too() -> None:
    """After "the delivery", Order reads the shop's own YES as well (its batch)."""
    from mimi.api import which_eval as we
    from mimi.api.agent import Agent
    from mimi.api.offline import OfflineModel
    from mimi.harness.inbound import Inbound, Payload
    from mimi.store import ensure_schema, make_engine, seed_world

    e = make_engine("sqlite://")
    ensure_schema(e)
    seed_world(e, W)
    we._setup(e, W, case("join", "both", "restock", [], []))
    a = Agent(e, models=[OfflineModel()], router_model=None, lease_s=30.0)
    phone = W.shop(we.SHOP).phone
    ids = []
    for n, text in enumerate(["Yes", "the delivery one"]):
        v = a.submit(we.TENANT, Inbound(source="shop_message", org_id=we.TENANT,
                                        external_id=f"join.{n}", received_at=we.T0,
                                        payload=Payload(text=text, from_phone=phone)))
        ids.append(v.turn_id)
        while a.process() is not None:
            pass
    with e.connect() as conn:
        t = conn.execute(sa.select(s.turns).where(s.turns.c.turn_id == ids[1])).one()
    assert t.skill == "order/drop-confirmation"
    assert json.loads(t.batch) == ["join.0", "join.1"]

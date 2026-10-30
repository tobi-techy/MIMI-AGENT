"""The daily naira cap parks jobs, never drops them; the owner's yes resumes them (RAI-272)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.harness.inbound import Inbound, Payload
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.store.util import insert_ignore
from mimi.world.loader import load_world

W = load_world()
NOW = datetime(2026, 10, 26, 21, 0, tzinfo=UTC)  # 22:00 WAT


@pytest.fixture()
def engine() -> Any:
    e = make_engine("sqlite://")
    ensure_schema(e)
    seed_world(e, W, daily_cap_kobo=10_000)
    with e.begin() as conn:  # today's allowance is already used up
        insert_ignore(conn, s.spend, {"tenant_id": "coral-lane", "day": "2026-10-26",
                                      "kobo": 10_000})
    return e


def _agent(engine: Any) -> Any:
    from mimi.api.agent import Agent
    from mimi.api.offline import OfflineModel

    return Agent(engine, models=[OfflineModel()], router_model=None, lease_s=30.0,
                 clock=lambda: NOW)


def _say(a: Any, ext: str, shop: str, text: str) -> str:
    v = a.submit("coral-lane", Inbound(source="shop_message", org_id="coral-lane",
                                       external_id=ext, received_at=NOW,
                                       payload=Payload(text=text,
                                                       from_phone=W.shop(shop).phone)))
    return str(v.turn_id)


def _out(engine: Any, turn_id: str) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(s.messages).where(
            s.messages.c.turn_id == turn_id, s.messages.c.direction == "out")
            .order_by(s.messages.c.ts)))


def test_cap_parks_with_a_holding_reply_and_one_owner_note_then_resumes(engine: Any) -> None:
    a = _agent(engine)
    t1 = _say(a, "cap.1", "shop-012", "How much I owe?")
    t2 = _say(a, "cap.2", "shop-014", "Good evening, wetin I owe now?")
    while a.process() is not None:
        pass
    v1, v2 = a.view("coral-lane", t1), a.view("coral-lane", t2)
    assert v1.state == v2.state == "parked" and v1.finished
    with engine.connect() as conn:
        aprs = conn.execute(sa.select(s.approvals).where(
            s.approvals.c.tool == "raise_daily_allowance")).all()
        notes = conn.execute(sa.select(s.messages.c.text).where(
            s.messages.c.channel == "owner_console")).scalars().all()
        turns = {r.turn_id: r for r in conn.execute(sa.select(s.turns))}
    assert len(aprs) == 1 and aprs[0].status == "pending" and aprs[0].helper == ""
    payload = json.loads(aprs[0].payload)
    assert payload["cap_kobo"] == 10_000 and payload["new_cap_kobo"] == 60_000
    assert len(notes) == 1 and "allowance" in notes[0]  # asked once a day, not per job
    for t in (t1, t2):
        out = _out(engine, t)
        assert [m.kind for m in out if m.channel == "shop_whatsapp"] == ["holding_reply"]
        assert turns[t].completions == 0
        assert not any(c.isdigit() for c in out[0].text)  # no figure in a holding reply
    # A second decision path: the owner says yes; the cap goes up, both jobs resume.
    row, follow = a.decide("coral-lane", aprs[0].approval_id, approve=True, by="owner")
    assert follow is None and row["status"] == "approved"
    assert {a.view("coral-lane", t).state for t in (t1, t2)} == {"leased"}  # lapsed: claimable
    with engine.connect() as conn:
        cap = conn.execute(sa.select(s.tenants.c.daily_cap_kobo).where(
            s.tenants.c.tenant_id == "coral-lane")).scalar()
    assert cap == 60_000
    while a.process() is not None:
        pass
    with engine.connect() as conn:
        turns = {r.turn_id: r for r in conn.execute(sa.select(s.turns))}
        att = conn.execute(sa.select(s.turn_attempts).where(
            s.turn_attempts.c.turn_id == t1)).all()
    for t in (t1, t2):
        assert turns[t].state == "done" and turns[t].completions == 1
        kinds = [m.kind for m in _out(engine, t) if m.channel == "shop_whatsapp"]
        assert kinds[0] == "holding_reply" and len(kinds) == 2  # then the real answer
        ck = json.loads(turns[t].checkpoint)
        assert ck.get("resumed") == 1 and "parked" not in ck
    assert len(att) == 2 and att[1].takeover  # carried on from its checkpoint


def test_rejected_raise_leaves_jobs_parked(engine: Any) -> None:
    a = _agent(engine)
    t1 = _say(a, "cap.r1", "shop-012", "How much I owe?")
    while a.process() is not None:
        pass
    with engine.connect() as conn:
        apr = conn.execute(sa.select(s.approvals.c.approval_id).where(
            s.approvals.c.tool == "raise_daily_allowance")).scalar()
    row, _ = a.decide("coral-lane", str(apr), approve=False, by="owner")
    assert row["status"] == "rejected"
    assert a.process() is None and a.view("coral-lane", t1).state == "parked"


def test_owner_messages_still_park_without_a_holding_reply(engine: Any) -> None:
    a = _agent(engine)
    v = a.submit("coral-lane", Inbound(source="owner_message", org_id="coral-lane",
                                       external_id="cap.o1", received_at=NOW,
                                       # A model turn (the brief is answered by code now).
                                       payload=Payload(text="Draft soft reminders for "
                                                            "the overdue shops")))
    while a.process() is not None:
        pass
    out = _out(engine, str(v.turn_id))
    assert a.view("coral-lane", str(v.turn_id)).state == "parked"
    assert not [m for m in out if m.kind == "holding_reply"]

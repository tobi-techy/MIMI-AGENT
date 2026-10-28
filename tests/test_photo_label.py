"""The photo label the model sees follows the route (RAI-318).

A camera photo used to reach every helper as ``[shelf photo ph_...]``, so the
Order helper sometimes asked a shop to type the list it had just photographed.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

import pytest

from mimi.harness.gateway import ScriptedModel, say
from mimi.harness.inbound import Inbound, Media, Payload
from mimi.harness.router import RouteDecision
from mimi.store import schema as s
from mimi.store.util import dumps
from mimi.tz import LAGOS
from mimi.world import load_world

W = load_world()
T = "coral-lane"
T0 = datetime(2026, 10, 26, 17, 0, tzinfo=LAGOS)
DATA = b"\xff\xd8 synthetic camera photo"
SHA = hashlib.sha256(DATA).hexdigest()
PID = "ph_" + SHA[:12]


@pytest.fixture
def engine() -> Any:
    from mimi.store import ensure_schema, make_engine, seed_world

    e = make_engine("sqlite://")
    ensure_schema(e)
    seed_world(e, W)
    return e


def camera(caption: str = "") -> Media:
    return Media(kind="image", mime="image/jpeg", sha256=SHA, width=1000, height=1333,
                 camera_exif=True, caption=caption)


def seen_by_helper(engine: Any, skill: str, media: Media, shop_id: str = "shop-013") -> str:
    """The newest user message the routed helper's model got for one shop photo."""
    from mimi.api.agent import Agent

    model = ScriptedModel([say("ok")])
    agent = Agent(engine, models=[model], router_model=None, lease_s=30.0, clock=lambda: T0)
    shop = W.shop(shop_id)
    v = agent.submit(T, Inbound(
        source="shop_message", org_id=T, external_id=f"pl.{skill}", received_at=T0,
        payload=Payload(text=media.caption, from_phone=shop.phone, media=media)))
    dec = RouteDecision(status="routed", helper=skill.split("/")[0], skill=skill,
                        confidence=1.0, layer="rule", rule="test",
                        thread_id=f"{T}:shop:{shop_id}")
    with engine.begin() as conn:
        conn.execute(s.turns.update().where(s.turns.c.turn_id == v.turn_id).values(
            checkpoint=dumps({"decision": dec.model_dump(mode="json"), "steer": {}})))
    for _ in range(4):
        if agent.process() is None:
            break
    assert model.seen, "the helper's model was never called"
    users = [m for m in model.seen[0] if m.get("role") == "user"]
    return str(users[-1]["content"])


@pytest.mark.parametrize(("skill", "label"), [
    ("order/order-intake", "order list photo"),
    ("credit/credit-request", "shelf photo"),
    ("proof/fake-alert-check", "photo"),
])
def test_camera_photo_label_follows_the_routed_skill(engine: Any, skill: str,
                                                     label: str) -> None:
    text = seen_by_helper(engine, skill, camera())
    assert f"[{label} {PID}]" in text, text
    if label != "shelf photo":
        assert "shelf photo" not in text, text


def test_order_list_photo_with_a_caption_keeps_the_caption(engine: Any) -> None:
    text = seen_by_helper(engine, "order/order-intake", camera("my order"))
    assert f"[order list photo {PID}] my order" in text, text


def test_labels_by_kind() -> None:
    from mimi.api.photo_label import media_pointer

    def ib(media: Media) -> Inbound:
        return Inbound(source="shop_message", org_id=T, external_id="x", received_at=T0,
                       payload=Payload(media=media))

    shot = Media(kind="image", mime="image/png", sha256=SHA, width=1080, height=2340)
    other = Media(kind="image", mime="image/jpeg", sha256=SHA, width=800, height=800)
    assert media_pointer(ib(camera()), "order/order-intake") == f"[order list photo {PID}]"
    assert media_pointer(ib(camera()), "stock/stock-count") == f"[shelf photo {PID}]"
    assert media_pointer(ib(camera()), "credit/credit-request") == f"[shelf photo {PID}]"
    assert media_pointer(ib(camera()), "mimi/escalate-owner") == f"[photo {PID}]"
    assert media_pointer(ib(camera()), "") == f"[photo {PID}]"
    # Screenshots and other images keep their labels, whatever the route.
    assert media_pointer(ib(shot), "order/order-intake") == f"[payment screenshot {PID}]"
    assert media_pointer(ib(other), "order/order-intake") == f"[image {PID}]"
    assert media_pointer(ib(Media()), "order/order-intake") == ""
    assert media_pointer(ib(Media(kind="image")), "order/order-intake") == ""


def test_agent_uses_the_one_label_helper() -> None:
    """agent.py keeps no label table of its own (the logic lives in photo_label)."""
    from pathlib import Path

    import mimi.api.agent as agent

    src = Path(agent.__file__).read_text(encoding="utf-8")
    assert '"shelf photo"' not in src and "'shelf photo'" not in src
    from mimi.api import photo_label

    assert agent.media_pointer is photo_label.media_pointer


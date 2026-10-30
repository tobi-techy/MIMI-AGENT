"""One regression test per bug the RAI-272 end-to-end run found.

Each test fails on the code before the fix and passes on it:

1. The queue leased shop turns oldest first, so the owner waited behind a
   burst. Now a lapsed lease goes first, then the owner, then shops.
2. The seed and FakeGo gave a "Demo Bank 0000000000" collection account, so
   a genuine screenshot (paid to the Kestrel Bank account) failed the
   recipient check. Now both give the Kestrel account the screenshots show.
3. A fake screenshot's flag is approve-tier, so the turn parked and the shop
   got no reply, or got it twice when the model also sent it. Now the
   neutral notice goes out at once, exactly once.
4. The owner's "how is business" brief had no data behind it and the model
   invented one ("Business is calm"). Now the skill says the brief is not
   ready, and the recorded Muse Spark answer does.
5. The recorded-answer cache was written in place, so a worker killed
   mid-write left a torn file. Now it is written to a temp file and renamed.
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_api_helpers import KEYS, NOW, SCREENS, T, W, _h, _intents, _screenshot, script

from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.api.e2e.score import figures
from mimi.api.helpers import HelperDeps
from mimi.api.media import LocalMedia
from mimi.harness.gateway import ModelTurn, RecordedToolModel, ScriptedModel, say
from mimi.harness.go import FakeGo
from mimi.harness.inbound import Inbound, Payload
from mimi.models.fake import FakeModel
from mimi.proof.reading import fake_reader
from mimi.receipts.verdict import fake_verdict
from mimi.stock.answers import fake_answer
from mimi.store import SqlGo, TurnQueue, ensure_schema, make_engine, seed_world
from mimi.store import schema as s

ROOT = Path(__file__).resolve().parents[1]
KESTREL = {"account_name": "CORAL LANE TRADING", "bank": "Kestrel Bank",
           "account_number": "9990004471"}
# The helper the scripted model plays; a test can swap it.
PLAY: dict[str, Any] = {}


@pytest.fixture()
def api(store_url: str) -> Any:
    """The same API as test_api_helpers, with a swappable scripted model."""
    PLAY["fn"] = script
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    truth = {r["sha256"]: r["truth"] for r in SCREENS.values()}
    deps = HelperDeps(vision=FakeModel(fake_reader(truth)), judge=FakeModel(fake_verdict),
                      stock=FakeModel(fake_answer), media=LocalMedia(ROOT / "eval"))
    model = ScriptedModel(lambda m, t: PLAY["fn"](m, t))
    agent = Agent(engine, models=[model], deps=deps, clock=lambda: NOW, worker_id="t")
    keys = parse_keys(f"{T}:owner:{KEYS['owner']},{T}:channel:{KEYS['chan']}")
    app = create_app(Settings(database_url=store_url, keys=keys, workers=0), agent=agent,
                     workers=0)
    with TestClient(app) as c:
        yield c, agent, engine


# ------------------------------------------------------------------ 1. queue


def test_regression_1_lapsed_lease_then_owner_then_older_shop_turns(store_url: str) -> None:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    agent = Agent(engine, models=[ScriptedModel([say("ok")])], clock=lambda: NOW)

    def shop(ext: str, sid: str) -> str:
        return agent.submit(T, Inbound(source="shop_message", org_id=T, external_id=ext,
                                       received_at=NOW, payload=Payload(
                                           text="hi", from_phone=W.shop(sid).phone))).turn_id

    lapsed = shop("r1-lapsed", "shop-003")
    a = shop("r1-a", "shop-001")
    b = shop("r1-b", "shop-002")
    owner = agent.submit(T, Inbound(source="owner_message", org_id=T, external_id="r1-owner",
                                    received_at=NOW, payload=Payload(text="hello"))).turn_id
    # Make the order explicit: the shop turns are older than the owner's.
    with engine.begin() as conn:
        for tid, ts in ((lapsed, 50.0), (a, 100.0), (b, 200.0), (owner, 300.0)):
            conn.execute(s.turns.update().where(s.turns.c.turn_id == tid)
                         .values(created_ts=ts))
    now = [1_000.0]
    q = TurnQueue(engine, lease_s=10, clock=lambda: now[0])
    held = q.claim("dead-worker", turn_id=lapsed)
    assert held is not None and held.turn_id == lapsed
    now[0] += 11  # the dead worker's lease lapses

    order = []
    for i in range(4):
        c = q.claim(f"w{i}")
        assert c is not None
        order.append(c.turn_id)
    assert order == [lapsed, owner, a, b]


# ------------------------------------------------- 2. the collection account


def test_regression_2_collection_account_is_kestrel_and_genuine_screenshot_passes(
        api: Any) -> None:
    c, _, engine = api
    assert FakeGo(W).collection_account(T) == KESTREL
    assert SqlGo(engine).collection_account(T) == KESTREL
    g = _screenshot(c, "g03", "shop-038", "r2-g03")
    assert g["skill"] == "proof/fake-alert-check"
    # Recorded only when every check passed, the recipient check included.
    assert ("record_payment_proof", "applied") in _intents(engine, "shop-038")
    assert "likely paid" in g["replies"][-1]["text"]
    with engine.connect() as conn:
        rows = conn.execute(sa.select(s.intents.c.payload).where(
            s.intents.c.shop_id == "shop-038",
            s.intents.c.tool == "record_payment_proof")).all()
    assert len(rows) == 1 and json.loads(rows[0].payload)["bank"]


# ------------------------------------------------ 3. one neutral fake reply


def _flag_only(messages: Any, tools: Any) -> ModelTurn:
    """The same helper, but following next_step: flag, send nothing itself."""
    turn = script(messages, tools)
    calls = tuple(x for x in turn.tool_calls if x.name != "send_to_user")
    if any(x.name == "flag_payment_proof" for x in turn.tool_calls):
        return ModelTurn(turn.content, calls, turn.model, fake=True)
    return turn


@pytest.mark.parametrize("model_also_sends", [True, False],
                         ids=["duplicate_send_to_user", "follows_next_step"])
def test_regression_3_fake_screenshot_gets_exactly_one_neutral_reply(
        api: Any, model_also_sends: bool) -> None:
    c, _, engine = api
    if not model_also_sends:
        PLAY["fn"] = _flag_only
    f = _screenshot(c, "f09", "shop-038", "r3-f09")
    assert f["status"] == "parked_for_approval"
    assert ("flag_payment_proof", "queued_for_approval") in _intents(engine, "shop-038")
    shop_msgs = [x for x in f["replies"] if x["channel"] == "shop_whatsapp"]
    assert len(shop_msgs) == 1, shop_msgs
    assert shop_msgs[0]["status"] == "ready"
    assert "confirm with the bank" in shop_msgs[0]["text"]
    with engine.connect() as conn:
        sent = conn.execute(sa.select(s.messages.c.text).where(
            s.messages.c.tenant_id == T, s.messages.c.direction == "out",
            s.messages.c.shop_id == "shop-038",
            s.messages.c.channel == "shop_whatsapp")).all()
    assert len(sent) == 1


# ------------------------------------------------- 4. no invented brief


def test_regression_4_owner_brief_is_from_the_book_never_invented(
        api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The brief was "not ready" (no data); since 30 Oct 2026 code answers it
    from the book (``cards.brief_answer``): no model, no invented figure. A
    vague "how is business" ask is code with the switch on (every deployed
    image); an explicit "morning brief" is code either way."""
    from mimi.api import cards as cx
    from mimi.flags import FIXES_ENV

    monkeypatch.setenv(FIXES_ENV, "1")
    c, agent, _ = api
    seen: list[str] = []

    def brief(messages: Any, tools: Any) -> ModelTurn:
        seen.append("model called")
        return say("Business is calm and steady today.")

    PLAY["fn"] = brief
    r = c.post("/v1/turns", json={"message_id": "r4-owner", "source": "owner_message",
                                  "text": "Good evening. How is business today?"},
               headers=_h("owner"))
    assert r.status_code in (200, 202), r.text
    v = r.json()
    if v.get("state") != "done":
        agent.process(v["turn_id"])
        v = c.get(f"/v1/turns/{v['turn_id']}", headers=_h("owner")).json()
    assert v["skill"] == "mimi/owner-brief"
    assert not seen, "the brief is code: no model call"
    texts = [x["text"] for x in v["replies"] if x["channel"] == "owner_console"]
    assert texts and "not ready" not in texts[0] and "calm" not in texts[0].lower()
    f = cx.brief_facts(agent.engine, agent.go, "coral-lane")
    allowed = {f["overdue_kobo"], f["paid_kobo"]} | {r_.overdue_kobo for r_ in f["top"]}
    assert figures(texts[0]) and set(figures(texts[0])) <= allowed


def test_regression_4_without_the_switch_a_vague_ask_replays_as_recorded(
        api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch off (CI replays): "how is business today?" still reaches the
    model, so the recorded owner thread later turns replay against is the
    same until the paid re-record."""
    from mimi.flags import FIXES_ENV

    monkeypatch.delenv(FIXES_ENV, raising=False)
    c, agent, _ = api
    seen: list[str] = []

    def brief(messages: Any, tools: Any) -> ModelTurn:
        seen.append("model called")
        return say("The daily brief is not ready yet.")

    PLAY["fn"] = brief
    r = c.post("/v1/turns", json={"message_id": "r4-owner-off", "source": "owner_message",
                                  "text": "Good evening. How is business today?"},
               headers=_h("owner"))
    v = r.json()
    if v.get("state") != "done":
        agent.process(v["turn_id"])
    assert seen


# ------------------------------------------------- 5. atomic cache write


class _Live:
    name = "live"

    def chat(self, messages: Any, tools: Any, *, max_tokens: int) -> ModelTurn:
        return say("fresh answer")


def test_regression_5_interrupted_cache_write_leaves_the_old_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "harness.cache.json"
    old = {"k-old": {"content": "old", "tool_calls": [], "model": "m"}}
    path.write_text(json.dumps(old, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    model = RecordedToolModel(path, live=_Live())

    real = pathlib.Path.write_text

    def killed_mid_write(self: Path, data: str, *a: Any, **k: Any) -> int:
        real(self, data[: len(data) // 3], *a, **k)  # a third gets to disk
        raise KeyboardInterrupt("worker killed mid-write")

    monkeypatch.setattr(pathlib.Path, "write_text", killed_mid_write)
    with pytest.raises(KeyboardInterrupt):
        model.chat([{"role": "user", "content": "new"}], [], max_tokens=10)
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == before
    assert json.loads(before) == old

    # And a write that finishes replaces the file whole, leaving no temp file.
    model = RecordedToolModel(path, live=_Live())
    model.chat([{"role": "user", "content": "new"}], [], max_tokens=10)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "k-old" in data and len(data) == 2
    assert [p.name for p in tmp_path.iterdir()] == [path.name]

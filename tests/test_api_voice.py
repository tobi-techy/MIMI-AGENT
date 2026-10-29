"""Voice replies on the API path (RAI-290): the text Mimi sends, read aloud.

Offline: a scripted model, the router's rules, a SQLite store per test and
the fake speaker. Kept out of the Postgres job's file list on purpose: the
voice consent table is not part of the store schema it empties.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mimi.api import voice as vx
from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.harness.gateway import ModelTurn, ScriptedModel, say
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.voice.prefs import shop_voice_consent
from mimi.world.loader import load_world

A = "coral-lane"
W = load_world()
KEY = "a-chan-key-0000000000001"
OWNER = "a-owner-key-000000000001"


def script(messages: Any, tools: Any) -> ModelTurn:
    if messages[-1]["role"] == "tool":
        return say("done")
    n = sum(1 for m in messages if m["role"] == "user")
    return say("", ("send_to_user", {"channel": "shop_whatsapp", "shop_id": "shop-001",
                                      "kind": "reply", "ends_turn": True,
                                      "body": f"Thank you{' ' + 'o' * n}. I see it is for delivery "
                                              "dlv-56-9039. Reply YES if that is right."}))


@pytest.fixture()
def env(store_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("MIMI_TTS", "fake")
    monkeypatch.setenv("MIMI_VOICE_REPLIES", A)
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    model = ScriptedModel(script)
    agent = Agent(engine, models=[model], worker_id="test-worker")
    keys = parse_keys(f"{A}:owner:{OWNER},{A}:channel:{KEY}")
    app = create_app(Settings(keys=keys, wait_s=5), agent, workers=0)
    with TestClient(app) as client:
        yield {"c": client, "engine": engine, "model": model}
    engine.dispose()


def turn(c: TestClient, mid: str, text: str = "", **kw: Any) -> dict[str, Any]:
    body = {"message_id": mid, "phone": W.shops[0].phone, "text": text, **kw}
    r = c.post("/v1/turns", json=body, headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200, r.text
    return r.json()


def voice_note(c: TestClient, mid: str, transcript: str) -> dict[str, Any]:
    return turn(c, mid, media={"kind": "voice", "mime": "audio/ogg", "ref": "wa-media-1",
                               "sha256": "cd" * 32, "transcript": transcript,
                               "transcript_lang": "pcm"})


def test_voice_on_is_the_shops_own_choice_recorded_by_code(env: dict[str, Any]) -> None:
    calls = env["model"].calls
    body = turn(env["c"], "wamid.v1", "VOICE ON YORUBA")
    assert body["status"] == "voice_on" and body["skill"] == "mimi/voice-setting"
    assert not body["route"]["model_called"]
    [reply] = body["replies"]
    assert reply["text"].startswith("Voice replies are on") and "Yoruba" in reply["text"]
    assert reply.get("voice") is None  # the shop typed: the answer is text
    with env["engine"].connect() as conn:
        rows = conn.execute(sa.select(shop_voice_consent)).mappings().all()
    assert len(rows) == 1 and rows[0]["shop_id"] == "shop-001" and rows[0]["voice_replies"]
    assert rows[0]["consent_text"] == "VOICE ON YORUBA"
    assert rows[0]["consent_message_id"] == "wamid.v1"
    assert "+234" not in rows[0]["consent_sender"]  # no phone number is kept
    assert env["model"].calls == calls  # no model call


def test_a_voice_note_gets_its_text_reply_read_aloud(env: dict[str, Any]) -> None:
    turn(env["c"], "wamid.v1", "VOICE ON YORUBA")
    body = voice_note(env["c"], "wamid.v2", "Good morning, how market")
    [reply] = [r for r in body["replies"] if r["channel"] == "shop_whatsapp"]
    v = reply["voice"]
    assert v is not None and v["voice"] == "yoruba:en" and v["language"] == "yoruba"
    # The same words: only the delivery reference is not read out.
    assert v["script"] == reply["text"].replace("delivery dlv-56-9039", "this delivery")
    r = env["c"].get(f"/v1/messages/{reply['message_id']}/voice",
                     headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/")
    assert r.headers["x-mimi-voice"] == "yoruba:en" and r.content[:4] == b"RIFF"


def test_typed_messages_get_text_only_and_voice_off_stops_it(env: dict[str, Any]) -> None:
    turn(env["c"], "wamid.v1", "VOICE ON")
    typed = turn(env["c"], "wamid.v2", "Good morning")
    assert all(r.get("voice") is None for r in typed["replies"])
    turn(env["c"], "wamid.v3", "voice off")
    body = voice_note(env["c"], "wamid.v4", "Good morning, how market")
    reply = next(r for r in body["replies"] if r["channel"] == "shop_whatsapp")
    assert reply.get("voice") is None
    r = env["c"].get(f"/v1/messages/{reply['message_id']}/voice",
                     headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 404


def test_no_voice_without_the_shops_opt_in(env: dict[str, Any]) -> None:
    body = voice_note(env["c"], "wamid.v2", "Good morning, how market")
    assert all(r.get("voice") is None for r in body["replies"])


def test_the_flag_is_per_distributor(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
                                     ) -> None:
    assert vx.voice_enabled(A) and not vx.voice_enabled("tenant-b")
    monkeypatch.setenv("MIMI_VOICE_REPLIES", "")
    assert not vx.voice_enabled(A)
    # Off: VOICE ON is an ordinary message for the router; nothing is recorded.
    body = turn(env["c"], "wamid.v9", "VOICE ON")
    assert body["skill"] != "mimi/voice-setting"
    with env["engine"].connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(
            shop_voice_consent)).scalar() == 0
    monkeypatch.setenv("MIMI_VOICE_REPLIES", "all")
    assert vx.voice_enabled("tenant-b")


def test_the_thread_marks_which_replies_have_a_voice_note(env: dict[str, Any]) -> None:
    """The console reads the flag off the thread and fetches the audio with
    its bearer in a header (never a token in the URL)."""
    turn(env["c"], "wamid.v1", "VOICE ON")
    typed = turn(env["c"], "wamid.v2", "Good morning")
    spoken = voice_note(env["c"], "wamid.v3", "Good morning, how market")
    owner = {"Authorization": f"Bearer {OWNER}"}
    r = env["c"].get(f"/v1/threads/{spoken['thread_id']}", headers=owner)
    assert r.status_code == 200, r.text
    flags = {m["message_id"]: m["voice"] for m in r.json()["messages"]}
    said = next(x for x in spoken["replies"] if x["channel"] == "shop_whatsapp")
    plain = next(x for x in typed["replies"] if x["channel"] == "shop_whatsapp")
    assert flags[said["message_id"]] is True and flags[plain["message_id"]] is False
    assert not any(v for k, v in flags.items() if k.startswith("in:"))
    assert all(m["media"] is False for m in r.json()["messages"])  # live mode: no uploads

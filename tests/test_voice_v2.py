"""Voice v2: payment promises in the flow, and voice replies for shops that opt in."""

from __future__ import annotations

import io
import json
import shutil
import wave
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from mimi.models import FakeModel
from mimi.receipts import (
    HELPER,
    RECEIPT_INTENTS,
    InMemoryShopInbox,
    ReceiptDesk,
    ReceiptFlow,
    ReceiptJudge,
    ShopWebhook,
)
from mimi.receipts.intents import RecordPaymentPromise
from mimi.receipts.verdict import fake_verdict
from mimi.receipts.voice_replies import figures_match, voice_fields
from mimi.runtime.outbox import MONEY_VERBS
from mimi.runtime.runner import JobRunner
from mimi.runtime.store import InMemoryJobStore
from mimi.runtime.tracing import RecordingStepTracer
from mimi.sim.engine import SimGo, Simulator
from mimi.sim.persona import fake_shop
from mimi.voice import FakeTranscriber, VoiceContext
from mimi.voice.fake import audio_sha256
from mimi.voice.prefs import (
    InMemoryVoicePrefs,
    SqlVoicePrefs,
    VoicePreference,
    parse_voice_command,
)
from mimi.voice.speak import (
    CachedSpeaker,
    FakeSpeaker,
    SpeechCacheMiss,
    SpitchSpeaker,
    speak_reply,
)
from mimi.voice.tts import SpitchSpeech
from mimi.world import load_world

WAT = timezone(timedelta(hours=1))
WORLD = load_world()
DELIVERY = next(d for d in WORLD.live_deliveries if d.shortage is None)
SHORT = next(d for d in WORLD.live_deliveries if d.shortage is not None)
SHOP = WORLD.shop(DELIVERY.shop_id)


def wav(seconds: float, tone: int = 0) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes([tone % 256, 0]) * int(seconds * 16000))
    return buf.getvalue()


PROMISE = wav(3.0, 11)
NO_DATE = wav(3.0, 12)
LOW = wav(5.0, 13)
SCRIPT = {
    audio_sha256(PROMISE): "yes everything complete i go pay 20k tomorrow",
    audio_sha256(NO_DATE): "yes everything complete i go pay you twenty thousand",
    audio_sha256(LOW): "pay " * 40,  # a decoding loop: marked low
}


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 26, 9, 0, tzinfo=WAT)  # Monday

    def __call__(self) -> datetime:
        return self.t

    def tick(self, **kw: Any) -> datetime:
        self.t += timedelta(**kw)
        return self.t


class Rig:
    def __init__(self, delivery: Any = DELIVERY, prefs: InMemoryVoicePrefs | None = None) -> None:
        self.store, self.inbox = InMemoryJobStore(), InMemoryShopInbox()
        self.clock, self.go = Clock(), SimGo()
        self.prefs = prefs or InMemoryVoicePrefs()
        self.delivery = delivery
        self.shop = WORLD.shop(delivery.shop_id)
        flow = ReceiptFlow(
            WORLD, self.inbox, ReceiptJudge(FakeModel(fake_verdict)), voice_prefs=self.prefs
        )
        self.flow = flow
        self.runner = JobRunner(
            store=self.store,
            sink=self.go,
            tracer=RecordingStepTracer(),
            steps={HELPER: flow},
            worker_id="w1",
            clock=self.clock,
            lease_s=30,
            intent_types=RECEIPT_INTENTS,
        )
        self.desk = ReceiptDesk(
            self.store,
            self.inbox,
            WORLD.tenant_id,
            transcriber=FakeTranscriber(SCRIPT),
            voice_context=lambda shop, job: VoiceContext("pcm", ()),
            voice_prefs=self.prefs,
            shop_language=lambda shop: "pidgin",
        )
        self.job_id = self.desk.open_receipt(delivery.delivery_id, self.shop.shop_id)
        self.drain()
        self.n = 0

    def drain(self) -> None:
        while self.runner.run_once() is not None:
            pass

    def settle(self) -> None:
        self.clock.tick(seconds=95)
        self.drain()

    def _mid(self) -> str:
        self.n += 1
        return f"wamid.t{self.n}"

    def voice(self, data: bytes) -> str:
        self.clock.tick(minutes=3)
        return self.desk.receive(
            ShopWebhook(self._mid(), self.shop.shop_id, self.shop.phone, "", self.clock(), audio=data)
        )

    def text(self, body: str) -> str:
        self.clock.tick(minutes=3)
        return self.desk.receive(
            ShopWebhook(self._mid(), self.shop.shop_id, self.shop.phone, body, self.clock())
        )

    def sent(self, purpose: str) -> list[dict[str, Any]]:
        return [m for m in self.go.to_shops if m["purpose"] == purpose]

    def job(self) -> Any:
        return self.store.get(self.job_id)


# ------------------------------------------------------------ promises


def test_voice_promise_is_read_back_and_recorded_only_after_yes() -> None:
    r = Rig()
    r.voice(PROMISE)
    r.settle()
    # The delivery is confirmed; the promise is only read back.
    assert DELIVERY.delivery_id in r.go.consents
    assert r.go.promises == {}
    back = r.sent("promise_readback")
    assert len(back) == 1
    assert "N20,000" in back[0]["text"] and "Tuesday 27 October 2026" in back[0]["text"]
    assert back[0]["promise_kobo"] == 2_000_000 and back[0]["promise_due_on"] == "2026-10-27"
    assert r.job().result is None  # still waiting for the YES
    r.text("YES")
    r.settle()
    p = r.go.promises[DELIVERY.delivery_id]
    assert (p["amount_kobo"], p["due_on"], p["timezone"]) == (2_000_000, "2026-10-27", "Africa/Lagos")
    assert p["said_kind"] == "voice" and p["transcript_provider"] == "fake"
    assert p["confirm_text"] == "YES" and p["readback_text"] == back[0]["text"]
    assert p["label"] == "self_reported"
    assert r.job().result == "confirmed"
    note = json.loads(r.job().working_note)
    assert note["promise_result"] == "recorded"


def test_promise_with_no_day_gets_a_question_then_a_readback() -> None:
    r = Rig()
    r.voice(NO_DATE)
    r.settle()
    q = r.sent("promise_clarify")
    assert len(q) == 1 and "On what day will you pay N20,000" in q[0]["text"]
    assert r.go.promises == {}
    r.text("Friday")
    r.settle()
    back = r.sent("promise_readback")
    assert len(back) == 1 and "Friday 30 October 2026" in back[0]["text"]
    assert r.go.promises == {}
    r.text("yes o")
    r.settle()
    assert r.go.promises[DELIVERY.delivery_id]["due_on"] == "2026-10-30"


def test_a_no_never_records_and_three_messages_is_the_limit() -> None:
    r = Rig()
    r.voice(PROMISE)
    r.settle()
    for _ in range(3):
        r.text("No")
        r.settle()
    assert r.go.promises == {}
    assert len([m for m in r.go.to_shops if m["purpose"].startswith("promise_")]) == 3
    assert r.job().result == "confirmed"
    assert json.loads(r.job().working_note)["promise_result"] == "not_recorded"


def test_no_answer_by_the_deadline_records_nothing() -> None:
    r = Rig()
    r.voice(PROMISE)
    r.settle()
    r.clock.tick(hours=25)
    r.drain()
    assert r.go.promises == {}
    assert json.loads(r.job().working_note)["promise_result"] == "unanswered"


def test_low_transcript_never_records_a_promise() -> None:
    r = Rig()
    r.voice(LOW)
    r.settle()
    assert r.go.promises == {} and DELIVERY.delivery_id not in r.go.consents
    assert r.sent("receipt_type_request")


def test_dispute_with_a_promise_flags_the_owner_and_reads_back() -> None:
    r = Rig(SHORT)
    r.text("I didn't collect the noodles. I go pay 15,000 Friday for the rest")
    r.settle()
    assert any(f["reason"] == "dispute" for f in r.go.flags)
    assert SHORT.delivery_id not in r.go.consents
    assert r.sent("promise_readback") and r.go.promises == {}


def test_promise_intent_is_registered_and_moves_no_money() -> None:
    assert RecordPaymentPromise in RECEIPT_INTENTS
    kind = RecordPaymentPromise.__dataclass_fields__["kind"].default
    assert not any(v in kind for v in MONEY_VERBS)


# --------------------------------------------------------- voice replies


def test_voice_replies_default_off() -> None:
    r = Rig()
    r.text("Good afternoon")
    r.settle()
    assert r.sent("receipt_clarify") and all(not m["voice_script"] for m in r.go.to_shops)


def test_voice_on_is_the_shops_own_recorded_choice() -> None:
    r = Rig()
    assert r.text("VOICE ON") == "voice_on"
    pref = r.prefs.get(SHOP.shop_id)
    assert pref.speaks and pref.language == "pidgin"
    assert pref.consent_text == "VOICE ON" and pref.consent_message_id == "wamid.t1"
    assert pref.consent_sender == SHOP.phone
    assert r.inbox.replies(r.job_id) == []  # not a receipt reply
    r.text("Good afternoon")
    r.settle()
    clar = r.sent("receipt_clarify")[0]
    assert clar["text"] and clar["voice_language"] == "pidgin"
    # The voice note is the text read aloud, never a second message.
    assert clar["voice_script"] == " ".join(clar["text"].split()).replace(
        f"delivery {clar['delivery_id']}", "this delivery")
    assert r.text("voice off") == "voice_off"
    assert not r.prefs.get(SHOP.shop_id).speaks
    assert len(r.prefs.history(SHOP.shop_id)) == 2


def test_voice_readback_says_the_figures_that_are_in_the_text() -> None:
    prefs = InMemoryVoicePrefs()
    r = Rig(prefs=prefs)
    r.text("VOICE ON ENGLISH")
    r.voice(PROMISE)
    r.settle()
    back = r.sent("promise_readback")[0]
    assert "twenty thousand naira" in back["voice_script"]
    assert "Tuesday the twenty-seventh of October" in back["voice_script"]
    assert figures_match(back["text"], back["voice_script"], date(2026, 10, 26))


def test_nobody_but_the_shop_can_turn_voice_on() -> None:
    prefs = InMemoryVoicePrefs()
    with pytest.raises(ValueError):
        prefs.record(VoicePreference("shop-1", True, "english"))
    with pytest.raises(ValueError):
        prefs.record(
            VoicePreference("shop-1", True, "klingon", "wamid.1", "VOICE ON", "+2340000000001", "t")
        )
    assert parse_voice_command("VOICE ON yoruba") == (True, "yoruba")
    assert parse_voice_command("voice off") == (False, None)
    assert parse_voice_command("please send voice on friday") is None


def test_voice_in_yoruba_igbo_hausa_reads_the_english_text_in_the_shops_voice() -> None:
    text = "You said you will pay N20,000 on Tuesday 27 October 2026 for delivery dlv-1-2."
    for lang in ("yoruba", "igbo", "hausa"):
        pref = VoicePreference("s", True, lang, "m", "VOICE ON", "+2340000000001", "t")
        for purpose in ("promise_readback", "promise_clarify"):
            s, voice = voice_fields(pref, purpose, text, today=date(2026, 10, 26),
                                    amount_kobo=2_000_000, due_on=date(2026, 10, 27))
            assert voice == f"{lang}:en" and not any(ch.isdigit() for ch in s), (lang, purpose)
            assert "twenty thousand naira" in s and "twenty-seventh of October" in s
        assert voice_fields(pref, "delivery_receipt", text, today=date(2026, 10, 26)) == ("", "")


def test_a_script_whose_figures_are_not_in_the_text_is_dropped() -> None:
    pref = VoicePreference("s", True, "english", "m", "VOICE ON", "+2340000000001", "t")
    ok = voice_fields(
        pref,
        "promise_readback",
        "You said you will pay N20,000 on Tuesday 27 October 2026 ...",
        today=date(2026, 10, 26),
        amount_kobo=2_000_000,
        due_on=date(2026, 10, 27),
    )
    assert ok[0] and ok[1] == "english"
    bad = voice_fields(
        pref,
        "promise_readback",
        "You said you will pay N15,000 on Tuesday 27 October 2026 ...",
        today=date(2026, 10, 26),
        amount_kobo=2_000_000,
        due_on=date(2026, 10, 27),
    )
    assert bad == ("", "")


def test_sql_voice_prefs_keep_every_consent(tmp_path: Path) -> None:
    prefs = SqlVoicePrefs(sa.create_engine(f"sqlite:///{tmp_path / 'p.db'}"))
    prefs.ensure_schema()
    assert not prefs.get("shop-1").speaks
    on = VoicePreference("shop-1", True, "hausa", "wamid.1", "VOICE ON HAUSA", "+2340000000001", "2026-10-26T09:00:00+01:00")
    assert prefs.record(on) and not prefs.record(on)
    assert prefs.get("shop-1") == on
    off = VoicePreference("shop-1", False, "hausa", "wamid.2", "VOICE OFF", "+2340000000001", "2026-10-26T10:00:00+01:00")
    prefs.record(off)
    assert not prefs.get("shop-1").speaks


# --------------------------------------------------------------- speech


def test_fake_speaker_is_deterministic_and_says_nothing() -> None:
    a, b = FakeSpeaker().speak("Hello", "english"), FakeSpeaker().speak("Hello", "english")
    assert a.data == b.data and a.fake and a.format == "wav"
    out, why = speak_reply("x" * 500, "english", FakeSpeaker())
    assert out is None and "over" in why
    assert speak_reply("Hello", "klingon", FakeSpeaker())[0] is None


def test_cached_speaker_records_then_replays(tmp_path: Path) -> None:
    live = FakeSpeaker()
    rec = CachedSpeaker(live, tmp_path)
    first = rec.speak("Abeg tell me", "pidgin")
    again = CachedSpeaker(None, tmp_path, mode="replay", name="fake").speak("Abeg tell me", "pidgin")
    assert again.data == first.data and live.calls == 1
    with pytest.raises(SpeechCacheMiss):
        CachedSpeaker(None, tmp_path, mode="replay", name="fake").speak("other", "pidgin")
    index = json.loads((tmp_path / "index.json").read_text())
    assert all("key" not in json.dumps(v).lower() for v in index.values())


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_spitch_speaker_sends_no_retention_and_encodes_opus() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=wav(1.5, 5))

    speech = SpitchSpeech(api_key="test-not-a-key", transport=httpx.MockTransport(handler))  # pragma: allowlist secret
    out = SpitchSpeaker(speech).speak("Ẹ jọ̀ọ́", "yoruba")
    assert 1.4 < out.duration_s < 1.6
    assert out.format == "ogg" and out.data.startswith(b"OggS")
    assert seen["headers"]["x-data-retention"] == "false"
    assert seen["body"]["language"] == "yo" and seen["body"]["voice"] == "funmi"
    pidgin = SpitchSpeaker(speech, encode=False).speak("Abeg", "pidgin")
    assert seen["body"]["language"] == "en" and pidgin.voice == "tega"


def test_streamed_wav_header_gives_the_real_length() -> None:
    data = bytearray(wav(2.0, 7))
    data[4:8] = b"\xff\xff\xff\xff"  # a streamed header: sizes unknown
    data[40:44] = b"\xff\xff\xff\xff"
    speech = SpitchSpeech(
        api_key="test-not-a-key",  # pragma: allowlist secret
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=bytes(data))),
    )
    out = SpitchSpeaker(speech, encode=False).speak("Hello", "english")
    assert 1.9 < out.duration_s < 2.1


def test_spitch_speaker_failure_leaves_text_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad voice"})

    speech = SpitchSpeech(api_key="test-not-a-key", transport=httpx.MockTransport(handler), sleep=lambda s: None)  # pragma: allowlist secret
    out, why = speak_reply("Hello", "english", SpitchSpeaker(speech))
    assert out is None and "spitch tts" in why and "test-not-a-key" not in why


# ------------------------------------------------------------------ sim


def test_offline_sim_with_promises_and_voice_replies() -> None:
    world = load_world()
    shop_ids = [d.shop_id for d in world.live_deliveries][:3]
    sim = Simulator(
        world,
        mimi_model=FakeModel(fake_verdict),
        sim_model=FakeModel(fake_shop),
        shop_ids=shop_ids,
        transcriber=FakeTranscriber(),
        voice_share=1.0,
        promise_share=1.0,
        voice_replies=True,
        speaker=FakeSpeaker(),
    )
    report = sim.run()
    assert report["voice_replies_on"] == sorted(shop_ids)
    assert report["promise_readbacks_sent"] >= 1
    assert report["promises_recorded"], report
    for p in report["promises_recorded"]:
        assert isinstance(p["amount_kobo"], int) and p["due_on"]
    assert report["confirmed_despite_shortage"] == 0
    # Every voice reply went with its text; no audio is kept in the report.
    assert report["voice_replies_sent"] == report["texts_with_voice_script"]
    assert "data" not in json.dumps(report)

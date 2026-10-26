"""Voice notes in the receipt flow: transcript in, same reader, safe guards."""

from __future__ import annotations

import io
import json
import wave
from datetime import datetime, timedelta, timezone
from typing import Any

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
    ShopReply,
    ShopWebhook,
    SqlShopInbox,
)
from mimi.receipts.decide import decide
from mimi.receipts.inbox import shop_inbox
from mimi.receipts.verdict import ReceiptVerdict, build_messages, fake_verdict
from mimi.runtime.runner import JobRunner
from mimi.runtime.store import InMemoryJobStore
from mimi.runtime.tracing import RecordingStepTracer
from mimi.sim.engine import SimGo
from mimi.voice import FakeTranscriber, VoiceContext
from mimi.voice.fake import audio_sha256
from mimi.world import load_world

WAT = timezone(timedelta(hours=1))
WORLD = load_world()
DELIVERY = next(d for d in WORLD.live_deliveries if d.shortage is None)
SHOP = WORLD.shop(DELIVERY.shop_id)


def wav(seconds: float, tone: int = 0) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes([tone % 256, 0]) * int(seconds * 16000))
    return buf.getvalue()


YES_CLIP = wav(2.0, 1)
SHORT_CLIP = wav(3.0, 2)
LOOP_CLIP = wav(4.0, 3)
UNKNOWN_CLIP = wav(2.0, 4)
SCRIPT = {
    audio_sha256(YES_CLIP): "yes everything don land complete",
    audio_sha256(SHORT_CLIP): "two cartons of the noodles are short",
    audio_sha256(LOOP_CLIP): "yes " * 12,
}


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 26, 9, 0, tzinfo=WAT)

    def __call__(self) -> datetime:
        return self.t

    def tick(self, **kw: Any) -> datetime:
        self.t += timedelta(**kw)
        return self.t


def rig(inbox: Any = None) -> Any:
    store, inbox = InMemoryJobStore(), inbox or InMemoryShopInbox()
    clock, go = Clock(), SimGo()
    fake = FakeTranscriber(SCRIPT)
    flow = ReceiptFlow(WORLD, inbox, ReceiptJudge(FakeModel(fake_verdict)))
    runner = JobRunner(
        store=store,
        sink=go,
        tracer=RecordingStepTracer(),
        steps={HELPER: flow},
        worker_id="w1",
        clock=clock,
        lease_s=30,
        intent_types=RECEIPT_INTENTS,
    )
    desk = ReceiptDesk(
        store,
        inbox,
        WORLD.tenant_id,
        transcriber=fake,
        voice_context=lambda shop, job: VoiceContext("pcm", ("Sunrise",)),
    )
    job_id = desk.open_receipt(DELIVERY.delivery_id, SHOP.shop_id)
    drain(runner)
    return store, desk, runner, go, clock, job_id, inbox, fake


def drain(runner: JobRunner) -> None:
    while runner.run_once() is not None:
        pass


def voice(desk: ReceiptDesk, clock: Clock, data: bytes, mid: str) -> str:
    return desk.receive(
        ShopWebhook(mid, SHOP.shop_id, SHOP.phone, "", clock(), audio=data, audio_mime="audio/ogg")
    )


def text(desk: ReceiptDesk, clock: Clock, body: str, mid: str) -> str:
    return desk.receive(ShopWebhook(mid, SHOP.shop_id, SHOP.phone, body, clock()))


def settle(runner: JobRunner, clock: Clock) -> None:
    clock.tick(seconds=95)
    drain(runner)


def test_voice_yes_records_consent_with_voice_witness() -> None:
    store, desk, runner, go, clock, job_id, inbox, _ = rig()
    clock.tick(minutes=5)
    assert voice(desk, clock, YES_CLIP, "wamid.v1") == "routed"
    settle(runner, clock)
    consent = go.consents[DELIVERY.delivery_id]
    assert consent["witness_kind"] == "voice"
    assert consent["transcript_provider"] == "fake"
    assert consent["evidence_quote"] in consent["witness_text"]
    reply = inbox.replies(job_id)[0]
    assert reply.kind == "voice" and reply.transcript_status == "ok"
    assert reply.audio_sha256 == audio_sha256(YES_CLIP) and reply.audio_seconds == 2.0


def test_voice_dispute_flags_owner_with_labelled_words() -> None:
    _, desk, runner, go, clock, *_ = rig()
    clock.tick(minutes=5)
    voice(desk, clock, SHORT_CLIP, "wamid.v1")
    settle(runner, clock)
    flag = go.flags[0]
    assert flag["reason"] == "dispute"
    assert flag["shop_words"].startswith("[voice note transcript] two cartons")
    assert not go.consents


def test_low_quality_yes_never_confirms_and_asks_to_type() -> None:
    _, desk, runner, go, clock, *_ = rig()
    clock.tick(minutes=5)
    voice(desk, clock, LOOP_CLIP, "wamid.v1")
    settle(runner, clock)
    assert not go.consents
    assert go.to_shops[-1]["purpose"] == "receipt_type_request"
    assert "type your reply" in go.to_shops[-1]["text"]
    clock.tick(minutes=10)
    text(desk, clock, "YES", "wamid.t1")
    settle(runner, clock)
    assert go.consents[DELIVERY.delivery_id]["witness_kind"] == "text"


def test_unreadable_voice_asks_to_type_then_flags_owner() -> None:
    _, desk, runner, go, clock, *_ = rig()
    clock.tick(minutes=5)
    voice(desk, clock, UNKNOWN_CLIP, "wamid.v1")
    settle(runner, clock)
    assert go.to_shops[-1]["purpose"] == "receipt_type_request"
    clock.tick(minutes=10)
    voice(desk, clock, b"not audio at all", "wamid.v2")
    settle(runner, clock)
    assert go.flags[-1]["reason"] == "voice_unreadable"
    assert "could not be transcribed" in go.flags[-1]["shop_words"]
    assert not go.consents


def test_text_yes_with_doubtful_voice_in_burst_does_not_confirm() -> None:
    _, desk, runner, go, clock, *_ = rig()
    clock.tick(minutes=5)
    text(desk, clock, "YES", "wamid.t1")
    clock.tick(seconds=20)
    voice(desk, clock, UNKNOWN_CLIP, "wamid.v1")
    settle(runner, clock)
    assert not go.consents
    assert go.to_shops[-1]["purpose"] == "receipt_type_request"


def test_voice_webhook_retry_is_not_transcribed_twice() -> None:
    _, desk, runner, go, clock, _, _, fake = rig()
    clock.tick(minutes=5)
    assert voice(desk, clock, YES_CLIP, "wamid.v1") == "routed"
    assert voice(desk, clock, YES_CLIP, "wamid.v1") == "duplicate"
    assert fake.calls == 1


def test_sql_inbox_keeps_transcript_not_audio() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    inbox = SqlShopInbox(engine)
    inbox.ensure_schema()
    _, desk, runner, go, clock, job_id, *_ = rig(inbox)
    clock.tick(minutes=5)
    voice(desk, clock, YES_CLIP, "wamid.v1")
    got = inbox.replies(job_id)[0]
    assert (got.kind, got.transcript_status, got.text) == (
        "voice",
        "ok",
        "yes everything don land complete",
    )
    assert inbox.has("wamid.v1") and not inbox.has("wamid.nope")
    cols = {c.name for c in shop_inbox.columns}
    assert not any("audio" in c and c not in ("audio_seconds", "audio_sha256") for c in cols)
    with engine.connect() as conn:
        row = conn.execute(sa.select(shop_inbox)).mappings().one()
    assert all(not isinstance(v, bytes) for v in row.values())


def _reply(**kw: Any) -> ShopReply:
    base = dict(
        message_id="m1",
        job_id="j",
        shop_id="s",
        sender="p",
        text="YES",
        received_at="2026-10-26T10:00:00+01:00",
    )
    return ShopReply(**{**base, **kw})


def test_model_sees_voice_marker_and_text_payload_is_unchanged() -> None:
    typed = _reply()
    heard = _reply(message_id="m2", text="yes o", kind="voice", transcript_status="ok")
    msgs = build_messages(DELIVERY, [], [typed, heard], ["receipt"])
    data = msgs[1].content.split("<shop_data>\n", 1)[1].split("\n</shop_data>", 1)[0]
    new = json.loads(data)["new_messages"]
    assert new[0] == {"at": typed.received_at, "text": "YES"}
    assert new[1]["source"] == "voice_note_transcript"
    assert new[1]["transcript_quality"] == "ok"
    assert new[1]["transcription_confidence"] is None


@pytest.mark.parametrize("status", ["low", "failed"])
def test_decide_holds_confirm_on_doubtful_transcript(status: str) -> None:
    v = ReceiptVerdict(outcome="confirmed", confidence=0.99, evidence_quote="yes o")
    heard = _reply(text="yes o", kind="voice", transcript_status=status)
    d = decide(v, delivery=DELIVERY, replies=[heard], clarified=False, new_replies=[heard])
    assert d.action == "clarify"
    ok = _reply(text="yes o", kind="voice", transcript_status="ok")
    assert decide(v, delivery=DELIVERY, replies=[ok], clarified=False).action == "consent"

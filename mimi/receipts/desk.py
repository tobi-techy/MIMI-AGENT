"""The front door for the receipt flow.

``open_receipt`` turns a logged delivery into a durable job (one per delivery;
the job id is the delivery id, so logging it twice is a no-op).
``receive`` takes a shop's WhatsApp webhook: dedupes on the message id, routes
it to the shop's oldest open receipt (one thread, one job, one turn at a
time), stores it and wakes the job. A reply with no open receipt (a second
YES after the job finished) is kept for the console and changes nothing.

A voice note (``ShopWebhook.audio``) is checked and transcribed here, once,
after the dedupe check, by the desk's transcriber (``mimi.voice``; usually
the router). The inbox stores the transcript with its status and provider,
the clip's length and SHA-256; the audio bytes are dropped. With no
transcriber, or when transcription fails, the reply is stored as a failed
transcript and the flow asks the shop to type.

A typed ``VOICE ON`` / ``VOICE ON YORUBA`` / ``VOICE OFF`` (``mimi.voice.prefs``)
is the shop's own choice about voice replies: the desk records it with the
message as consent (``voice_prefs``) and does not route it to a receipt.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from mimi.observe.turns import Media, Observation, TurnBook, audio_cost, note, sniff_mime
from mimi.receipts.flow import HELPER
from mimi.receipts.inbox import ShopInbox, ShopReply
from mimi.runtime.budgets import Budget
from mimi.runtime.job import Job
from mimi.runtime.states import JobState
from mimi.runtime.store import JobStore
from mimi.voice.base import Transcriber, Transcript, VoiceContext
from mimi.voice.fake import audio_sha256
from mimi.voice.prefs import (
    VOICE_LANGUAGES,
    VoicePreference,
    VoicePrefs,
    parse_voice_command,
)
from mimi.voice.reader import read_voice_note

# Model names for speech-to-text in traces (Langfuse prices them by name).
_STT_MODEL = {"spitch": "spitch-stt", "muse": "muse-voice-transcribe"}

VoiceContextFor = Callable[[str, str], VoiceContext]
# shop_id -> the language the shop usually writes in ("english", "pidgin", ...)
ShopLanguageFor = Callable[[str], str]


@dataclass(frozen=True)
class ShopWebhook:
    message_id: str
    shop_id: str
    sender: str
    text: str
    received_at: datetime
    # A voice note's bytes as the channel delivered them (WhatsApp: Ogg/Opus)
    # and the declared mime type; ``text`` is then ignored.
    audio: bytes | None = None
    audio_mime: str | None = None


@dataclass
class ReceiptDesk:
    store: JobStore
    inbox: ShopInbox
    tenant_id: str
    open_by_shop: dict[str, list[str]] = field(default_factory=dict)
    unrouted: list[ShopWebhook] = field(default_factory=list)
    duplicates: int = 0
    transcriber: Transcriber | None = None
    # (shop_id, job_id) -> language hint and receipt keywords for the clip.
    voice_context: VoiceContextFor | None = None
    transcripts: list[Transcript] = field(default_factory=list)
    voice_prefs: VoicePrefs | None = None
    shop_language: ShopLanguageFor | None = None
    # Optional tracing (``mimi.observe.turns``); off unless an exporter is set.
    turns: TurnBook | None = None

    def open_receipt(self, delivery_id: str, shop_id: str) -> str:
        job_id = f"rcpt-{delivery_id}"
        try:
            self.store.get(job_id)
        except KeyError:
            self.store.create(
                Job(
                    job_id=job_id,
                    tenant_id=self.tenant_id,
                    helper=HELPER,
                    thread_id=f"wa-{shop_id}",
                    wake_source="delivery_logged",
                    goal=delivery_id,
                    budget=Budget(max_steps=16, max_seconds=120.0, max_kobo=5_000),
                )
            )
            self.open_by_shop.setdefault(shop_id, []).append(job_id)
        return job_id

    def _open_job(self, shop_id: str) -> str | None:
        for job_id in self.open_by_shop.get(shop_id, []):
            if self.store.get(job_id).state is not JobState.DONE:
                return job_id
        return None

    def receive(self, hook: ShopWebhook) -> str:
        """Returns "routed", "duplicate", "unrouted", "voice_on" or "voice_off"."""
        if hook.audio is None and self.voice_prefs is not None:
            command = parse_voice_command(hook.text)
            if command is not None:
                return self._voice_choice(hook, *command)
        job_id = self._open_job(hook.shop_id)
        if job_id is None:
            self.unrouted.append(hook)
            return "unrouted"
        if self.inbox.has(hook.message_id):
            self.duplicates += 1
            return "duplicate"
        reply = ShopReply(
            message_id=hook.message_id,
            job_id=job_id,
            shop_id=hook.shop_id,
            sender=hook.sender,
            text=hook.text,
            received_at=hook.received_at.isoformat(),
        )
        if hook.audio is not None:
            reply = self._voice_reply(reply, hook, job_id)
        added = self.inbox.add(reply)
        if not added:
            self.duplicates += 1
            return "duplicate"
        self.store.wake(job_id)
        return "routed"

    def _voice_choice(self, hook: ShopWebhook, on: bool, language: str | None) -> str:
        assert self.voice_prefs is not None
        lang = language or (
            self.shop_language(hook.shop_id) if self.shop_language is not None else "english"
        )
        if lang not in VOICE_LANGUAGES:
            lang = "english"
        added = self.voice_prefs.record(
            VoicePreference(
                shop_id=hook.shop_id,
                voice_replies=on,
                language=lang,
                consent_message_id=hook.message_id,
                consent_text=hook.text,
                consent_sender=hook.sender,
                decided_at=hook.received_at.isoformat(),
            )
        )
        if not added:
            self.duplicates += 1
            return "duplicate"
        return "voice_on" if on else "voice_off"

    def _transcribe(self, hook: ShopWebhook, job_id: str, ctx: VoiceContext) -> Transcript:
        assert hook.audio is not None
        if self.turns is None or not self.turns.enabled:
            return read_voice_note(hook.audio, self.transcriber, ctx, declared=hook.audio_mime)
        with self.turns.prelude(job_id, flow="voice_note", shop_id=hook.shop_id,
                                thread_id=f"wa-{hook.shop_id}"):
            started = time.time_ns()
            t0 = time.monotonic_ns()
            t = read_voice_note(hook.audio, self.transcriber, ctx, declared=hook.audio_mime)
            wall = time.monotonic_ns() - t0
            sha = audio_sha256(hook.audio)
            mime = sniff_mime(hook.audio, hook.audio_mime or "audio/ogg")
            note(
                Observation(
                    "generation",
                    "transcribe-voice-note",
                    started,
                    int(t.latency_s * 1e9) if t.latency_s else wall,
                    model=_STT_MODEL.get(t.provider.split(":")[0], t.provider),
                    usage={"audio_ms": int((t.duration_s or 0.0) * 1000)},
                    cost_usd=audio_cost(t.provider, t.duration_s),
                    input={"audio": f"audio:{sha}", "audio_sha256": sha,
                           "bytes": len(hook.audio), "language_hint": ctx.language_hint,
                           "keywords": list(ctx.keywords)},
                    output={"text": t.text, "status": t.status, "language": t.language,
                            "confidence": t.confidence, "reason": t.reason},
                    metadata={"message_kind": "voice_note", "tried": list(t.tried),
                              "duration_s": t.duration_s, "fake": t.fake,
                              "latency_ms": round(t.latency_s * 1000, 1)},
                    level="WARNING" if t.status != "ok" else "DEFAULT",
                    status_message=t.reason,
                    media={f"audio:{sha}": Media(hook.audio, mime)},
                )
            )
        return t

    def _voice_reply(self, reply: ShopReply, hook: ShopWebhook, job_id: str) -> ShopReply:
        assert hook.audio is not None
        ctx = (
            self.voice_context(hook.shop_id, job_id)
            if self.voice_context is not None
            else VoiceContext()
        )
        t = self._transcribe(hook, job_id, ctx)
        self.transcripts.append(t)
        return ShopReply(
            message_id=reply.message_id,
            job_id=reply.job_id,
            shop_id=reply.shop_id,
            sender=reply.sender,
            text=t.text if t.status != "failed" else "",
            received_at=reply.received_at,
            kind="voice",
            transcript_status=t.status,
            transcript_provider=t.provider,
            transcript_confidence=t.confidence,
            audio_seconds=t.duration_s,
            audio_sha256=audio_sha256(hook.audio),
        )

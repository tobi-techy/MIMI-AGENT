"""The one-day simulator: a clock, Go's stand-in, shop personas and Mimi.

A clock runs the world forward. Events (a delivery logged, a shop's message
arriving, an hourly tick that lets sleeping jobs check their deadline) sit on
a queue by time; the sim pops the next one, moves the clock to it, hands it to
Mimi through the front door (``ReceiptDesk``), then lets the runtime run every
job that is due. Whatever Mimi proposes goes to ``SimGo``, which dedupes on the
idempotency key exactly as Go must; shop messages it accepts are given to that
shop's persona, whose replies are queued at the time they would arrive.

Fast-forward is the default (no waiting). ``realtime_speed`` makes the sim
sleep between events: 1.0 is real time, 60.0 plays an hour a minute.

Voice notes: with ``voice_share`` above 0, a shop's first reply is sometimes
(that share of shops, picked from the seed) a real synthetic voice clip from
``eval/voice/clips/`` instead of text, in the shop's language: a dispute clip
when goods were short, a confirmation otherwise. A persona's
"[voice note 0:12]" placeholder line becomes a clip the same way. The clips
come from a fixed library, so their numbers may not match the receipt. The
desk transcribes them with ``transcriber`` (the router, or the fake).

Payment promises: with ``promise_share`` above 0, that share of the shops
whose first reply is a voice note send a clip that also promises to pay
("Everything complete. I go pay 20k tomorrow"), and their persona is told
what it said, so it can answer Mimi's read-back.

Voice replies: with ``voice_replies`` on, every shop sends "VOICE ON" at the
start of the day (its recorded consent), and each short reply Mimi sends
with a voice script is spoken by ``speaker`` (Spitch, or the fake). The
audio is counted and dropped, never stored.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from time import sleep
from typing import Any

from mimi.models.base import ChatModel
from mimi.receipts import (
    HELPER,
    RECEIPT_INTENTS,
    ReceiptDesk,
    ReceiptFlow,
    ReceiptJudge,
    ShopWebhook,
)
from mimi.receipts.inbox import InMemoryShopInbox, ShopInbox
from mimi.receipts.receipt import receipt_keywords
from mimi.receipts.verdict import Judged
from mimi.runtime.runner import JobRunner
from mimi.runtime.store import InMemoryJobStore, JobStore
from mimi.runtime.tracing import RecordingStepTracer
from mimi.sim.persona import ShopPersona, _rng, voice_for
from mimi.voice.base import Transcriber, VoiceContext
from mimi.voice.fixtures import clip_bytes, load_clips
from mimi.voice.prefs import InMemoryVoicePrefs, VoicePrefs
from mimi.voice.speak import Speaker, SpokenAudio, speak_reply
from mimi.world.model import Delivery, World

WAT = timezone(timedelta(hours=1), "WAT")

# Persona voice -> (fixture language, hint for the transcriber).
_VOICE_LANG = {
    "short casual Nigerian English, small typos": ("english", "en"),
    "Nigerian Pidgin": ("pidgin", "pcm"),
    "Yoruba mixed with English words": ("yoruba", "yo"),
    "Igbo mixed with English words": ("igbo", "ig"),
    "Hausa mixed with English words": ("hausa", "ha"),
}


def voice_clip_for(
    shop_id: str, short: bool, n: int = 0, promise: bool = False
) -> dict[str, Any] | None:
    """A fixture clip in the shop's language: a dispute when goods were
    short, a confirmation otherwise (Yoruba disputes use the Yoruba-English
    clip). With ``promise``, a clip that also promises a clear amount and
    day. ``None`` when the library has none."""
    lang, hint = _VOICE_LANG.get(voice_for(shop_id), ("english", "en"))
    want = "dispute" if short else "confirm"
    clips = load_clips()

    def fits(c: dict[str, Any]) -> bool:
        if not promise:
            return c["category"] == want
        p = c.get("promise") or {}
        return p.get("status") == "promise" and c["expected"] == ("disputed" if short else "confirmed")

    for langs in ((lang, "mixed") if lang == "yoruba" else (lang,), ("english",)):
        pool = [
            c
            for c in clips
            if (c["lang"] in langs or (promise and c["hint"] == hint and c["lang"] == "mixed"))
            and fits(c)
        ]
        if pool:
            return pool[n % len(pool)]
    return None


class SimClock:
    def __init__(self, start: datetime, realtime_speed: float | None = None) -> None:
        self._now = start
        self.speed = realtime_speed

    def now(self) -> datetime:
        return self._now

    def advance_to(self, when: datetime) -> None:
        if when <= self._now:
            return
        if self.speed:
            sleep((when - self._now).total_seconds() / self.speed)
        self._now = when


@dataclass
class SimGo:
    """Go's intent API for the sim: dedupes on the key, applies each once."""

    accepted: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    calls: int = 0
    to_shops: list[dict[str, Any]] = field(default_factory=list)
    consents: dict[str, dict[str, Any]] = field(default_factory=dict)
    flags: list[dict[str, Any]] = field(default_factory=list)
    promises: dict[str, dict[str, Any]] = field(default_factory=dict)

    def submit(
        self, tenant_id: str, idempotency_key: str, kind: str, payload: dict[str, Any]
    ) -> str:
        self.calls += 1
        if idempotency_key in self.accepted:
            return idempotency_key
        self.accepted[idempotency_key] = (kind, payload)
        if kind == "send_shop_message":
            self.to_shops.append(payload)
        elif kind == "record_delivery_consent":
            self.consents[payload["delivery_id"]] = payload
        elif kind == "flag_to_owner":
            self.flags.append(payload)
        elif kind == "record_payment_promise":
            self.promises[payload["delivery_id"]] = payload
        return idempotency_key


@dataclass
class Transcript:
    lines: list[str] = field(default_factory=list)

    def add(self, when: datetime, who: str, text: str) -> None:
        self.lines.append(f"[{when:%a %H:%M}] {who}: {text}")


class Simulator:
    def __init__(
        self,
        world: World,
        *,
        mimi_model: ChatModel,
        sim_model: ChatModel,
        shop_ids: list[str] | None = None,
        store: JobStore | None = None,
        inbox: ShopInbox | None = None,
        realtime_speed: float | None = None,
        seed: int = 0,
        hours: float = 26.0,
        transcriber: Transcriber | None = None,
        voice_share: float = 0.0,
        promise_share: float = 0.0,
        voice_replies: bool = False,
        speaker: Speaker | None = None,
        voice_prefs: VoicePrefs | None = None,
    ) -> None:
        self.world = world
        self.deliveries: list[Delivery] = [
            d for d in world.live_deliveries if shop_ids is None or d.shop_id in shop_ids
        ]
        start_day = world.date_of(world.live_day)
        self.start = datetime.combine(start_day, time(7, 0), WAT)
        self.end = self.start + timedelta(hours=hours)
        self.clock = SimClock(self.start, realtime_speed)
        self.store = store or InMemoryJobStore()
        self.inbox = inbox or InMemoryShopInbox()
        self.go = SimGo()
        self.voice_share = voice_share
        self.promise_share = promise_share
        self.voice_replies = voice_replies
        self.speaker = speaker
        self.voice_prefs: VoicePrefs = voice_prefs or InMemoryVoicePrefs()
        self.desk = ReceiptDesk(
            self.store,
            self.inbox,
            world.tenant_id,
            transcriber=transcriber,
            voice_context=self._voice_context,
            voice_prefs=self.voice_prefs,
            shop_language=self._shop_language,
        )
        self.voice_sent: list[str] = []
        self.spoken: list[SpokenAudio] = []
        self.spoken_failed: list[str] = []
        self.judge = ReceiptJudge(mimi_model)
        self.verdicts: list[Judged] = []
        self.flow = ReceiptFlow(world, self.inbox, self._judge, voice_prefs=self.voice_prefs)
        self.tracer = RecordingStepTracer()
        self.runner = JobRunner(
            store=self.store,
            sink=self.go,
            tracer=self.tracer,
            steps={HELPER: self.flow},
            worker_id="sim-worker",
            clock=self.clock.now,
            intent_types=RECEIPT_INTENTS,
        )
        self.personas = {
            d.shop_id: ShopPersona(world, world.shop(d.shop_id), sim_model, seed)
            for d in self.deliveries
        }
        self.transcript = Transcript()
        self._queue: list[tuple[datetime, int, str, Any]] = []
        self._seq = 0
        self._handed = 0
        self.steps = 0
        self.webhooks = 0

    def _judge(self, **kw: Any) -> Judged:
        judged = self.judge(**kw)
        self.verdicts.append(judged)
        v = judged.verdict
        self.transcript.add(
            self.clock.now(),
            "Mimi (reads)",
            f"{v.outcome} conf={v.confidence:.2f} lang={v.language} "
            f'quote="{v.evidence_quote[:80]}" via={judged.via}',
        )
        return judged

    def _voice_context(self, shop_id: str, job_id: str) -> VoiceContext:
        _, hint = _VOICE_LANG.get(voice_for(shop_id), ("english", "en"))
        delivery = self.world.delivery(job_id.removeprefix("rcpt-"))
        return VoiceContext(language_hint=hint, keywords=receipt_keywords(delivery))

    def _shop_language(self, shop_id: str) -> str:
        return _VOICE_LANG.get(voice_for(shop_id), ("english", "en"))[0]

    def _voiced(
        self, shop_id: str, delivery: Delivery, round_no: int, texts: list[str]
    ) -> list[str | dict[str, Any]]:
        """Swap text for a synthetic clip: a placeholder line always (when
        voice is on), and a whole first-round burst for ``voice_share`` of
        the shops (a clip with a payment promise for ``promise_share`` of
        those)."""
        if self.voice_share <= 0:
            return list(texts)
        short = delivery.shortage is not None
        clip = voice_clip_for(shop_id, short, len(self.voice_sent))
        if clip is None:
            return list(texts)
        seed = self.personas[shop_id].seed
        roll = _rng("voice-note", shop_id, seed).random()
        if round_no == 0 and roll < self.voice_share and texts:
            if _rng("promise", shop_id, seed).random() < self.promise_share:
                clip = voice_clip_for(shop_id, short, len(self.voice_sent), promise=True) or clip
            self.personas[shop_id].said_instead(clip["reference"])
            return [clip]
        return [clip if t.startswith("[voice note") else t for t in texts]

    def _push(self, when: datetime, kind: str, payload: Any) -> None:
        self._seq += 1
        heapq.heappush(self._queue, (when, self._seq, kind, payload))

    def _drain(self) -> None:
        while (report := self.runner.run_once()) is not None:
            self.steps += report.steps_run
        for msg in self.go.to_shops[self._handed :]:
            self._handed += 1
            shop_id = msg["scope"]["shop_id"]
            delivery = self.world.delivery(msg["delivery_id"])
            now = self.clock.now()
            self.transcript.add(now, f"Mimi -> {shop_id}", msg["text"].replace("\n", " | "))
            if msg.get("voice_script"):
                audio, why = speak_reply(msg["voice_script"], msg["voice_language"], self.speaker)
                if audio is not None:
                    self.spoken.append(audio)
                    self.transcript.add(
                        now,
                        f"Mimi -> {shop_id}",
                        f"[voice reply, {audio.duration_s:.1f}s {audio.language}, "
                        f"{audio.provider} voice {audio.voice}, sent with the text] "
                        f'"{msg["voice_script"][:90]}"',
                    )
                else:
                    self.spoken_failed.append(why)
                    self.transcript.add(now, f"Mimi -> {shop_id}", f"[voice reply not made: {why}]")
            persona = self.personas[shop_id]
            round_no = persona.round
            outs = persona.reply(msg["text"], delivery)
            bodies = self._voiced(shop_id, delivery, round_no, [o.text for o in outs])
            for i, (out, body) in enumerate(zip(outs, bodies, strict=False)):
                self._push(
                    now + out.delay,
                    "reply",
                    (shop_id, f"{msg['delivery_id']}-{self._handed}-{i}", body),
                )

    def run(self) -> dict[str, Any]:
        if self.voice_replies:
            for d in self.deliveries:
                self._push(self.start, "voice_on", d.shop_id)
        for d in self.deliveries:
            when = datetime.combine(self.start.date(), time(d.minute // 60, d.minute % 60), WAT)
            self._push(when, "log", d)
        tick = self.start
        while tick <= self.end:
            self._push(tick, "tick", None)
            tick += timedelta(hours=1)
        while self._queue:
            when, _, kind, payload = heapq.heappop(self._queue)
            if when > self.end:
                break
            self.clock.advance_to(when)
            if kind == "voice_on":
                shop = self.world.shop(payload)
                self.transcript.add(when, shop.name, "VOICE ON")
                self.desk.receive(
                    ShopWebhook(f"wamid.voice-on-{payload}", payload, shop.phone, "VOICE ON", when)
                )
            elif kind == "log":
                self.desk.open_receipt(payload.delivery_id, payload.shop_id)
            elif kind == "reply":
                shop_id, mid, body = payload
                shop = self.world.shop(shop_id)
                self.webhooks += 1
                if isinstance(body, dict):
                    data = clip_bytes(body)
                    self.voice_sent.append(body["id"])
                    self.transcript.add(
                        when, shop.name, f"[voice note, synthetic clip {body['id']}]"
                    )
                    self.desk.receive(
                        ShopWebhook(
                            f"wamid.{mid}",
                            shop_id,
                            shop.phone,
                            "",
                            when,
                            audio=data,
                            audio_mime="audio/ogg; codecs=opus",
                        )
                    )
                    t = self.desk.transcripts[-1]
                    self.transcript.add(
                        when,
                        "Mimi (hears)",
                        f"{t.status} via {'/'.join(t.tried) or t.provider} "
                        f'"{t.text[:80]}"' + (f" ({t.reason})" if t.reason else ""),
                    )
                else:
                    self.transcript.add(when, shop.name, body)
                    self.desk.receive(ShopWebhook(f"wamid.{mid}", shop_id, shop.phone, body, when))
                # Wake again once the burst could have settled.
                self._push(when + self.flow.burst_settle + timedelta(seconds=1), "tick", None)
            self._drain()
        return self.report()

    def report(self) -> dict[str, Any]:
        results: dict[str, int] = {}
        open_jobs = 0
        for d in self.deliveries:
            job = self.store.get(f"rcpt-{d.delivery_id}")
            if job.result:
                results[job.result] = results.get(job.result, 0) + 1
            else:
                open_jobs += 1
        shortages = {d.delivery_id for d in self.deliveries if d.shortage}
        flagged = {
            f["delivery_id"] for f in self.go.flags if f["reason"] in ("dispute", "partial_dispute")
        }
        outcomes: dict[str, int] = {}
        langs: dict[str, int] = {}
        for j in self.verdicts:
            outcomes[j.verdict.outcome] = outcomes.get(j.verdict.outcome, 0) + 1
            langs[j.verdict.language] = langs.get(j.verdict.language, 0) + 1
        return {
            "shops": len(self.deliveries),
            "results": results,
            "still_waiting": open_jobs,
            "messages_to_shops": len(self.go.to_shops),
            "receipts_sent": sum(1 for m in self.go.to_shops if m["purpose"] == "delivery_receipt"),
            "clarifications_sent": sum(
                1 for m in self.go.to_shops if m["purpose"] == "receipt_clarify"
            ),
            "type_requests_sent": sum(
                1 for m in self.go.to_shops if m["purpose"] == "receipt_type_request"
            ),
            "voice_notes": len(self.desk.transcripts),
            "voice_clips": list(self.voice_sent),
            "voice_transcripts": {
                f"{'/'.join(t.tried) or t.provider}": sum(
                    1
                    for x in self.desk.transcripts
                    if ("/".join(x.tried) or x.provider) == ("/".join(t.tried) or t.provider)
                )
                for t in self.desk.transcripts
            },
            "voice_audio_seconds": round(
                sum(t.duration_s or 0.0 for t in self.desk.transcripts), 2
            ),
            "voice_latency_s": round(sum(t.latency_s for t in self.desk.transcripts), 2),
            "consents_from_voice": sum(
                1 for c in self.go.consents.values() if c.get("witness_kind") == "voice"
            ),
            "consents_recorded": len(self.go.consents),
            "promise_readbacks_sent": sum(
                1 for m in self.go.to_shops if m["purpose"] == "promise_readback"
            ),
            "promise_questions_sent": sum(
                1 for m in self.go.to_shops if m["purpose"] == "promise_clarify"
            ),
            "promises_recorded": [
                {
                    "delivery_id": p["delivery_id"],
                    "amount_kobo": p["amount_kobo"],
                    "due_on": p["due_on"],
                    "said_kind": p["said_kind"],
                    "confirm_text": p["confirm_text"],
                }
                for p in self.go.promises.values()
            ],
            "voice_replies_on": sorted(
                d.shop_id for d in self.deliveries if self.voice_prefs.get(d.shop_id).speaks
            ),
            "voice_replies_sent": len(self.spoken),
            "voice_replies_seconds": round(sum(a.duration_s for a in self.spoken), 2),
            "voice_replies_by_language": {
                lang: sum(1 for a in self.spoken if a.language == lang)
                for lang in sorted({a.language for a in self.spoken})
            },
            "voice_replies_failed": len(self.spoken_failed),
            "texts_with_voice_script": sum(1 for m in self.go.to_shops if m.get("voice_script")),
            "verdicts": len(self.verdicts),
            "verdict_outcomes": outcomes,
            "verdict_languages": langs,
            "verdicts_unparseable": sum(
                1 for j in self.verdicts if j.via in ("unparseable", "model_error")
            ),
            "mimi_prompt_tokens": sum(j.prompt_tokens for j in self.verdicts),
            "mimi_completion_tokens": sum(j.completion_tokens for j in self.verdicts),
            "owner_flags": len(self.go.flags),
            "shortages_in_world": len(shortages),
            "shortages_flagged": len(shortages & flagged),
            "confirmed_despite_shortage": len(shortages & set(self.go.consents)),
            "shop_webhooks": self.webhooks,
            "duplicate_webhooks": self.desk.duplicates,
            "unrouted_webhooks": len(self.desk.unrouted),
            "intent_submits": self.go.calls,
            "intents_accepted": len(self.go.accepted),
            "runtime_steps": self.steps,
            "sim_prompt_tokens": sum(p.prompt_tokens for p in self.personas.values()),
            "sim_completion_tokens": sum(p.completion_tokens for p in self.personas.values()),
            "sim_clock_end": self.clock.now().isoformat(),
        }

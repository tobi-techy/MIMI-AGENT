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
from mimi.receipts.verdict import Judged
from mimi.runtime.runner import JobRunner
from mimi.runtime.store import InMemoryJobStore, JobStore
from mimi.runtime.tracing import RecordingStepTracer
from mimi.sim.persona import ShopPersona
from mimi.world.model import Delivery, World

WAT = timezone(timedelta(hours=1), "WAT")


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
    ) -> None:
        self.world = world
        self.deliveries: list[Delivery] = [
            d
            for d in world.live_deliveries
            if shop_ids is None or d.shop_id in shop_ids
        ]
        start_day = world.date_of(world.live_day)
        self.start = datetime.combine(start_day, time(7, 0), WAT)
        self.end = self.start + timedelta(hours=hours)
        self.clock = SimClock(self.start, realtime_speed)
        self.store = store or InMemoryJobStore()
        self.inbox = inbox or InMemoryShopInbox()
        self.go = SimGo()
        self.desk = ReceiptDesk(self.store, self.inbox, world.tenant_id)
        self.judge = ReceiptJudge(mimi_model)
        self.verdicts: list[Judged] = []
        self.flow = ReceiptFlow(world, self.inbox, self._judge)
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
            self.transcript.add(
                now, f"Mimi -> {shop_id}", msg["text"].replace("\n", " | ")
            )
            for i, out in enumerate(
                self.personas[shop_id].reply(msg["text"], delivery)
            ):
                self._push(
                    now + out.delay,
                    "reply",
                    (shop_id, f"{msg['delivery_id']}-{self._handed}-{i}", out.text),
                )

    def run(self) -> dict[str, Any]:
        for d in self.deliveries:
            when = datetime.combine(
                self.start.date(), time(d.minute // 60, d.minute % 60), WAT
            )
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
            if kind == "log":
                self.desk.open_receipt(payload.delivery_id, payload.shop_id)
            elif kind == "reply":
                shop_id, mid, text = payload
                shop = self.world.shop(shop_id)
                self.transcript.add(when, shop.name, text)
                self.webhooks += 1
                self.desk.receive(
                    ShopWebhook(f"wamid.{mid}", shop_id, shop.phone, text, when)
                )
                # Wake again once the burst could have settled.
                self._push(
                    when + self.flow.burst_settle + timedelta(seconds=1), "tick", None
                )
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
            f["delivery_id"]
            for f in self.go.flags
            if f["reason"] in ("dispute", "partial_dispute")
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
            "receipts_sent": sum(
                1 for m in self.go.to_shops if m["purpose"] == "delivery_receipt"
            ),
            "clarifications_sent": sum(
                1 for m in self.go.to_shops if m["purpose"] == "receipt_clarify"
            ),
            "consents_recorded": len(self.go.consents),
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
            "sim_completion_tokens": sum(
                p.completion_tokens for p in self.personas.values()
            ),
            "sim_clock_end": self.clock.now().isoformat(),
        }

"""The low-stock scene (RAI-270): Monday morning stock check on the live day.

At 07:30 WAT on the live day (Mon 26 Oct 2026) ``StockDesk.morning_check``
estimates every item of the chosen shops, and for each shop's lowest item
under 3 days asks the one-tap question. The shop's persona answers from what
is truly on its shelf (``reorders.py`` or ``truth.py``; Mimi never sees it):
sometimes a tap, sometimes a number, a number word or words in its own voice
("e don finish", "mo ni meji"). Mimi reads it, records the count, re-runs the
estimate and, when the shelf will not last to the next van, puts a restock
draft in the distributor's approvals and thanks the shop: "Thanks. I've asked
Coral Lane Trading to prepare 10 cartons for you. They'll confirm before
delivery." Nothing is ordered.

History: ``reorders`` (default; the shops' regular lines, reordered when low,
on the dev seed) or ``route`` (the world's own deliveries).
"""

from __future__ import annotations

import heapq
import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from mimi.models.base import ChatMessage, ChatModel, ModelError
from mimi.observe.turns import TracedModel, TurnBook, TurnStepTracer, observe_step
from mimi.runtime.runner import JobRunner
from mimi.runtime.store import InMemoryJobStore
from mimi.runtime.tracing import RecordingStepTracer
from mimi.sim.engine import WAT, SimGo, Transcript
from mimi.sim.persona import _rng, reply_delay, voice_for
from mimi.stock.answers import StockAnswerReader
from mimi.stock.flow import STOCK_HELPER, StockDesk, StockFlow, StockLedger
from mimi.stock.history import ItemHistory, bucket_of, item_histories
from mimi.stock.intents import STOCK_INTENTS
from mimi.stock.items import display_name, unit_word
from mimi.stock.reorders import DEV_SEED, generate_reorders
from mimi.stock.truth import ItemTruth, generate_truth
from mimi.world.model import World

_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _shop_answer_prompt(world: World, shop_id: str, item: str, unit: str, left: int,
                        words: bool = False) -> str:
    shop = world.shop(shop_id)
    how = (
        "you do not tap a button: you type words, a number word or the number"
        if words else
        "sometimes you tap a button (then reply with exactly its label), sometimes you type the "
        "number, a number word or a few words"
    )
    return (
        f"You are {shop.owner}, owner of {shop.name}, a {shop.shop_type} in {shop.area}, "
        f"Ado-Ekiti. {world.distributor} supplies you. Mimi, their WhatsApp assistant, asks how "
        f"many {unit_word(unit)} of {display_name(item)} are left, with buttons 0-2, 3-5, 6+. "
        f"Truth: you have exactly {left} {unit_word(unit, left)} left on the shelf. "
        f"Answer like a real shop owner on WhatsApp, in {voice_for(shop_id)}: {how}. "
        "If Mimi asks again, answer clearly. One short message, nothing else. Never invent "
        "another item."
    )


def fake_stock_shop(left: int, shop_id: str, round_no: int, seed: int) -> str:
    """Deterministic shop answers for the offline scene."""
    rng = _rng("stock-answer", shop_id, round_no, seed)
    voice = voice_for(shop_id)
    b = bucket_of(left)
    if round_no > 0:
        return str(left)
    roll = rng.random()
    if roll < 0.35:
        return b
    if left == 0:
        return "e don finish" if "Pidgin" in voice or roll < 0.6 else "nothing remain"
    if roll < 0.6 and left < len(_WORDS):
        return _WORDS[left]
    if "Pidgin" in voice:
        return f"e remain {left}"
    if "Yoruba" in voice and left <= 5:
        return "mo ni " + ("ookan", "meji", "meta", "merin", "marun")[max(0, left - 1)]
    return f"{left} left"


@dataclass
class StockScene:
    world: World
    mimi_model: ChatModel
    sim_model: ChatModel
    shop_ids: list[str]
    turns: TurnBook = field(default_factory=TurnBook)
    seed: int = 0
    history: str = "reorders"
    truth_seed: int = DEV_SEED
    # Shops type their answers (no taps), so Muse Spark reads every one.
    words: bool = False
    go: SimGo = field(default_factory=SimGo)
    transcript: Transcript = field(default_factory=Transcript)

    def __post_init__(self) -> None:
        if self.history == "reorders":
            rw = generate_reorders(self.world, self.truth_seed)
            self.hist: dict[tuple[str, str], ItemHistory] = rw.histories
            self.truth: dict[tuple[str, str], ItemTruth] = rw.truth
        else:
            self.hist = item_histories(self.world)
            self.truth = generate_truth(self.world, self.truth_seed).items
        self.day = self.world.live_day
        self.start = datetime.combine(self.world.date_of(self.day), time(7, 30), WAT)
        model = TracedModel(self.mimi_model) if self.turns.enabled else self.mimi_model
        self.store = InMemoryJobStore()
        self.ledger = StockLedger()
        start0 = datetime.combine(self.world.date_of(0), time(0, 0), WAT)
        self.desk = StockDesk(self.store, self.ledger, self.world.tenant_id, lambda d: self.hist,
                              lambda now: (now.date() - start0.date()).days, set(self.shop_ids))
        self.flow = StockFlow(self.ledger, lambda d: self.hist, {s.sku: s for s in self.world.skus},
                              lambda shop_id: self.world.shop(shop_id).restock_days,
                              StockAnswerReader(model), self.world.distributor,
                              self.world.price_rise_day)
        self.clock = {"now": self.start}
        steps: dict[str, Any] = {STOCK_HELPER: self.flow}
        if self.turns.enabled:
            steps = {STOCK_HELPER: observe_step(self.flow)}
        self.runner = JobRunner(store=self.store, sink=self.go,
                                tracer=TurnStepTracer(RecordingStepTracer(), self.turns),
                                steps=steps, worker_id="sim-stock", clock=lambda: self.clock["now"],
                                intent_types=STOCK_INTENTS)
        self.rounds: dict[str, int] = {}
        self.sim_tokens = [0, 0]

    def _answer(self, shop_id: str, sku: str, text: str) -> str:
        left = self.truth[(shop_id, sku)].counted(self.day)
        n = self.rounds.get(shop_id, 0)
        self.rounds[shop_id] = n + 1
        if self.sim_model.name.startswith("fake"):
            text = fake_stock_shop(left, shop_id, n, self.seed)
            if self.words and text in ("0-2", "3-5", "6+"):
                text = _WORDS[left] if left < len(_WORDS) else f"{left} left"
            return text
        h = self.hist[(shop_id, sku)]
        try:
            res = self.sim_model.complete(
                [ChatMessage("developer", _shop_answer_prompt(self.world, shop_id, h.name, h.unit, left,
                                                                 self.words)),
                 ChatMessage("user", text)],
                max_tokens=800, meta={"script": "stock_shop", "shop_id": shop_id})
        except ModelError:
            return ""
        self.sim_tokens[0] += res.prompt_tokens
        self.sim_tokens[1] += res.completion_tokens
        return (res.text.strip().splitlines() or [""])[0].strip().strip('"')[:200]

    def _drain(self, handed: int, queue: list[tuple[datetime, int, str, str, str]]) -> int:
        while self.runner.run_once() is not None:
            pass
        for msg in self.go.to_shops[handed:]:
            handed += 1
            shop_id, now = msg["scope"]["shop_id"], self.clock["now"]
            buttons = f"  [{'] ['.join(msg['buttons'])}]" if msg.get("buttons") else ""
            self.transcript.add(now, f"Mimi -> {shop_id}", msg["text"] + buttons)
            if msg["purpose"] in ("stock_ask", "stock_ask_again"):
                reply = self._answer(shop_id, msg["sku"], msg["text"])
                delay = reply_delay(self.world.shop(shop_id), self.rounds[shop_id] - 1,
                                    _rng(self.seed, shop_id, "stock", self.rounds[shop_id]))
                heapq.heappush(queue, (now + min(delay, timedelta(hours=3)), handed, shop_id,
                                       msg["sku"], reply))
        return handed

    def run(self) -> dict[str, Any]:
        jobs = self.desk.morning_check(self.start)
        for job_id in jobs:
            raw = self.ledger.asked_on[job_id]["estimate"]
            key = (raw["shop_id"], raw["sku"])
            left = self.truth[key].counted(self.day)
            self.turns.annotate(job_id, sim_true_units=left, sim_true_bucket=bucket_of(left))
            self.transcript.add(
                self.start, "Mimi (estimates)",
                f"{key[0]} {key[1]}: in {raw['units_in_window']} over 28 days, sells "
                f"{raw['sell_rate_per_day']}/day, about {raw['estimated_units_left']} left, "
                f"{raw['days_to_run_out']} days, confidence {raw['confidence']}")
        queue: list[tuple[datetime, int, str, str, str]] = []
        handed = self._drain(0, queue)
        n = 0
        while queue:
            when, _, shop_id, sku, text = heapq.heappop(queue)
            self.clock["now"] = when
            n += 1
            shop = self.world.shop(shop_id)
            self.transcript.add(when, shop.name, text or "(no reply)")
            if text:
                self.desk.receive(f"wamid.stock-{shop_id}-{n}", shop_id, text, when)
            self.clock["now"] = when + timedelta(minutes=2)
            read_before = len(self.flow.answers)
            while self.runner.run_once() is not None:
                pass
            for _, ans in self.flow.answers[read_before:]:
                self.transcript.add(self.clock["now"], "Mimi (reads)",
                                    f"bucket={ans.bucket} exact={ans.exact_units} via={ans.via} "
                                    f"({ans.reason})")
            handed = self._drain(handed, queue)
        # Past every answer deadline still pending (a follow-up ask can end
        # after the first day), so unanswered jobs close and are reported.
        self.clock["now"] = max([self.start + timedelta(hours=26), *self._deadlines(jobs)])
        while self.runner.run_once() is not None:
            pass
        self.turns.close_all()
        return self.report(jobs)

    def _deadlines(self, jobs: list[str]) -> list[datetime]:
        out = []
        for j in jobs:
            job = self.store.get(j)
            if job.result is None and job.working_note:
                deadline = json.loads(job.working_note).get("deadline")
                if deadline:
                    out.append(datetime.fromisoformat(deadline) + timedelta(minutes=1))
        return out

    def report(self, jobs: list[str]) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for kind, _ in self.go.accepted.values():
            kinds[kind] = kinds.get(kind, 0) + 1
        drafts = [p for k, p in self.go.accepted.values() if k == "restock_draft_for_approval"]
        signals = [p for k, p in self.go.accepted.values() if k == "low_stock_signal"]
        results: dict[str, int] = {}
        for j in jobs:
            r = self.store.get(j).result or "open"
            results[r] = results.get(r, 0) + 1
        def key(job_id: str) -> tuple[str, str]:
            raw = self.ledger.asked_on[job_id]["estimate"]
            return raw["shop_id"], raw["sku"]

        right = sum(1 for job_id, a in self.flow.answers
                    if a.bucket is not None
                    and a.bucket == bucket_of(self.truth[key(job_id)].counted(self.day)))
        return {
            "scene": "low_stock", "history": self.history, "shops": len(self.shop_ids),
            "items_estimated": len(self.ledger.runs[0]["estimates"]) if self.ledger.runs else 0,
            "questions": len(jobs), "results": results, "intents": kinds,
            "answers_read": len(self.flow.answers),
            "answers_bucket_matches_shelf": right,
            "answers_via": sorted({a.via for _, a in self.flow.answers}),
            "restock_drafts": [{"shop_id": d["scope"]["shop_id"], "sku": d["sku"],
                                "quantity": d["quantity"], "unit": d["unit"],
                                "amount_kobo": d["amount_kobo"], "status": d["status"],
                                "places_order": d["places_order"]} for d in drafts],
            "distributor_sees": [{"shop_id": s["scope"]["shop_id"], "sku": s["sku"],
                                  "run_out_band": s["run_out_band"], "confidence": s["confidence"]}
                                 for s in signals],
            "orders_placed": sum(1 for d in drafts if d["places_order"]),
            "mimi_prompt_tokens": sum(a.prompt_tokens for _, a in self.flow.answers),
            "mimi_completion_tokens": sum(a.completion_tokens for _, a in self.flow.answers),
            "sim_prompt_tokens": self.sim_tokens[0], "sim_completion_tokens": self.sim_tokens[1],
            "estimator_run": json.dumps({k: v for k, v in (self.ledger.runs[0] if self.ledger.runs else {}).items()
                                         if k != "estimates"}),
        }

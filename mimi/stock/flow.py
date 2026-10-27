"""The stock helper on the always-on runtime: a workflow with code gates.

Not a free agent loop: each step does one fixed thing and code decides what
happens next (Anthropic's "building effective agents" workflow pattern).

``StockDesk.morning_check(now)`` runs the estimator for every item of every
shop (pure code), logs each estimate, applies the trigger rule and the caps
(``trigger.py``) and opens one job per question: ``stock-<shop>-<sku>-<week>``.

``StockFlow`` is that job's step:

* ``new``: propose ``LowStockSignal`` (what the distributor sees: likely to
  run low, a band and a confidence, never counts) and the one-tap question
  "Sunrise Instant Noodles 70g left? Cartons: 0-2 | 3-5 | 6+" with three
  buttons; then wait up to 24 hours for the answer.
* ``asked``: read the answer (``answers.py``: code for taps and numbers,
  Muse Spark for words). A bucket: ``RecordStockCount``, the estimate is
  re-run with the count (the count overrides it), and when the shelf will not
  last to the next usual delivery a ``RestockDraftForApproval`` (quantity and
  amount in code, checked by ``restock.verify``) and "Thanks. I've asked
  Coral Lane Trading to prepare 10 cartons for you. They'll confirm before
  delivery." Unclear: one follow-up ask, never a guess.
* ``asked_again``: a bucket as above; unclear again, or no answer by the
  deadline: done, nothing recorded, nothing drafted, no more asks this week.

``StockDesk.receive`` routes a shop message to the shop's open question (one
open per shop at a time); a message for no open question is not stock's.
Mimi never moves money and never places an order.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from mimi.observe.turns import Observation, nested, note
from mimi.runtime.budgets import Budget
from mimi.runtime.job import Job
from mimi.runtime.states import JobState
from mimi.runtime.steps import StepContext, StepOutcome
from mimi.runtime.store import JobStore
from mimi.stock import copy
from mimi.stock.answers import StockAnswer, StockAnswerReader
from mimi.stock.estimator import POLICY_VERSION, StockEstimate, estimate
from mimi.stock.history import ItemHistory, StockCount, assumed_units
from mimi.stock.intents import (
    LowStockSignal,
    RecordStockCount,
    RestockDraftForApproval,
    SendStockMessage,
    band_for,
)
from mimi.stock.restock import draft_quantity, line_amount, verify
from mimi.stock.trigger import AskBook, iso_week, pick_asks
from mimi.types import ShopScope
from mimi.world.model import Sku

STOCK_HELPER = "stock_check"
ANSWER_WINDOW = timedelta(hours=24)
# Shops send a thought in pieces ("wait" ... "na 4"): read once it settles.
BURST_SETTLE = timedelta(seconds=90)
Key = tuple[str, str]
# world day -> every (shop, sku) history with deliveries before that day
HistoriesFor = Callable[[int], dict[Key, ItemHistory]]


@dataclass(frozen=True)
class StockReply:
    message_id: str
    job_id: str
    shop_id: str
    text: str
    received_at: str


@dataclass
class StockLedger:
    """Mimi's own stock notes: counts (never shown to the distributor), the
    estimate each question was asked on, every estimate run, and answers."""

    counts: dict[Key, list[StockCount]] = field(default_factory=lambda: defaultdict(list))
    asked_on: dict[str, dict[str, Any]] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)
    replies: dict[str, list[StockReply]] = field(default_factory=lambda: defaultdict(list))
    seen_ids: set[str] = field(default_factory=set)

    def counts_for(self, key: Key) -> list[StockCount]:
        return list(self.counts.get(key, []))


@dataclass
class StockDesk:
    store: JobStore
    ledger: StockLedger
    tenant_id: str
    histories: HistoriesFor
    day_of: Callable[[datetime], int]
    shop_ids: set[str] | None = None
    book: AskBook = field(default_factory=AskBook)
    duplicates: int = 0
    unrouted: int = 0
    open_job: dict[str, str] = field(default_factory=dict)

    def estimates(self, now: datetime) -> list[StockEstimate]:
        day = self.day_of(now)
        out = []
        for key, h in self.histories(day).items():
            if self.shop_ids is not None and key[0] not in self.shop_ids:
                continue
            e = estimate(h, self.ledger.counts_for(key), day)
            if e is not None:
                out.append(e)
        return out

    def morning_check(self, now: datetime) -> list[str]:
        """Estimate everything, log it, open a job per question. Returns job ids."""
        day = self.day_of(now)
        week = iso_week(now.date())
        for shop, job_id in list(self.open_job.items()):
            if self.store.get(job_id).state is JobState.DONE:
                self.book.close(shop)
                del self.open_job[shop]
        ests = self.estimates(now)
        picks = pick_asks(ests, self.book, week)
        self.ledger.runs.append({
            "as_of": now.isoformat(), "day": day, "week": week, "policy_version": POLICY_VERSION,
            "items": len(ests), "asked": [f"{e.shop_id}/{e.sku}" for e in picks],
            "estimates": [e.as_dict() for e in ests],
        })
        jobs = []
        for e in picks:
            job_id = f"stock-{e.shop_id}-{e.sku}-{week}"
            self.ledger.asked_on[job_id] = {"estimate": e.as_dict(), "day": day, "week": week,
                                            "asked_at": now.isoformat()}
            self.store.create(Job(
                job_id=job_id, tenant_id=self.tenant_id, helper=STOCK_HELPER,
                thread_id=f"wa-{e.shop_id}", wake_source="stock_check", goal=job_id,
                budget=Budget(max_steps=12, max_seconds=120.0, max_kobo=5_000),
            ))
            self.open_job[e.shop_id] = job_id
            jobs.append(job_id)
        return jobs

    def receive(self, message_id: str, shop_id: str, text: str, received_at: datetime) -> str:
        """Route a shop message to its open stock question: "routed",
        "duplicate" or "unrouted" (no open question: not stock's)."""
        job_id = self.open_job.get(shop_id)
        if job_id is None or self.store.get(job_id).state is JobState.DONE:
            self.unrouted += 1
            return "unrouted"
        if message_id in self.ledger.seen_ids:
            self.duplicates += 1
            return "duplicate"
        self.ledger.seen_ids.add(message_id)
        self.ledger.replies[job_id].append(
            StockReply(message_id, job_id, shop_id, text, received_at.isoformat()))
        self.store.wake(job_id)
        return "routed"


@dataclass
class StockFlow:
    ledger: StockLedger
    histories: HistoriesFor
    skus: dict[str, Sku]
    restock_days: Callable[[str], int]
    reader: StockAnswerReader
    distributor: str
    price_rise_day: int
    answers: list[tuple[str, StockAnswer]] = field(default_factory=list)
    drafts: list[RestockDraftForApproval] = field(default_factory=list)

    def __call__(self, ctx: StepContext) -> StepOutcome:
        state = json.loads(ctx.working_note) if ctx.working_note else {"stage": "new", "seen": 0}
        asked = self.ledger.asked_on[ctx.goal]
        est = _est(asked["estimate"])
        scope = ShopScope(ctx.tenant_id, est.shop_id)
        history = self.histories(asked["day"])[(est.shop_id, est.sku)]
        if state["stage"] == "new":
            return self._ask(ctx, est, history, scope, asked)
        return self._answer(ctx, est, history, scope, asked, state)

    # ------------------------------------------------------------ steps

    def _ask(self, ctx: StepContext, est: StockEstimate, h: ItemHistory, scope: ShopScope,
             asked: dict[str, Any]) -> StepOutcome:
        _note_estimate("estimate-stock", h, self.ledger.counts_for((h.shop_id, h.sku)), est)
        signal = LowStockSignal(scope, est.sku, h.name, band_for(est.days_to_run_out),
                                est.confidence, ctx.now.date().isoformat(), POLICY_VERSION)
        msg = SendStockMessage(scope, est.sku, "stock_ask", copy.ask_text(h.name, h.unit),
                               copy.BUTTONS)
        deadline = ctx.now + ANSWER_WINDOW
        return StepOutcome(
            kind="sleep", intents=(signal, msg), sleep_until=deadline,
            working_note=json.dumps({"stage": "asked", "seen": 0, "deadline": deadline.isoformat()}),
            trace=self._attrs("asked", "asked", est),
        )

    def _answer(self, ctx: StepContext, est: StockEstimate, h: ItemHistory, scope: ShopScope,
                asked: dict[str, Any], state: dict[str, Any]) -> StepOutcome:
        replies = self.ledger.replies.get(ctx.goal, [])
        new = replies[state["seen"]:]
        deadline = datetime.fromisoformat(state["deadline"])
        if not new:
            if ctx.now >= deadline:
                return StepOutcome(kind="done", result="no_answer",
                                   trace=self._attrs("no_answer", "no_answer", est))
            return StepOutcome(kind="sleep", sleep_until=deadline, working_note=json.dumps(state))
        settled = datetime.fromisoformat(new[-1].received_at) + BURST_SETTLE
        if ctx.now < settled:
            return StepOutcome(kind="sleep", sleep_until=settled, working_note=json.dumps(state))
        texts = [r.text for r in new]
        question = (copy.ask_text(h.name, h.unit) if state["stage"] == "asked"
                    else copy.ask_again_text(h.name, h.unit))
        span = Observation("tool", "read-stock-answer", time.time_ns(),
                           input={"shop_messages": texts, "question": question, "item": h.name})
        t0 = time.monotonic_ns()
        with nested(span):
            ans = self.reader(item_name=h.name, unit=h.unit, question=question, texts=texts)
            span.duration_ns = time.monotonic_ns() - t0
            span.output = {"bucket": ans.bucket, "exact_units": ans.exact_units, "via": ans.via,
                           "reason": ans.reason, "evidence_quote": ans.evidence_quote}
            span.level = "WARNING" if ans.bucket is None else "DEFAULT"
        self.answers.append((ctx.goal, ans))
        seen = len(replies)
        if ans.bucket is None:
            if state["stage"] == "asked":
                msg = SendStockMessage(scope, est.sku, "stock_ask_again",
                                       copy.ask_again_text(h.name, h.unit), copy.BUTTONS)
                deadline = ctx.now + ANSWER_WINDOW
                return StepOutcome(
                    kind="sleep", intents=(msg,), sleep_until=deadline,
                    working_note=json.dumps({"stage": "asked_again", "seen": seen,
                                             "deadline": deadline.isoformat()}),
                    trace=self._attrs("unclear", "asked_again", est, ans),
                )
            return StepOutcome(kind="done", result="unclear_twice",
                               trace=self._attrs("unclear", "unclear_twice", est, ans))
        day = asked["day"]
        key = (h.shop_id, h.sku)
        units = ans.exact_units if ans.exact_units is not None else assumed_units(
            ans.bucket, est.estimated_units_left)
        count = StockCount(day, ans.bucket, units, source="typed" if ans.exact_units is not None else "tap")
        self.ledger.counts[key].append(count)
        last = new[-1]
        record = RecordStockCount(scope, h.sku, ans.bucket, units, ans.exact_units is not None,
                                  last.message_id, last.text, ans.via, ctx.now.date().isoformat())
        after = estimate(h, self.ledger.counts_for(key), day)
        assert after is not None
        _note_estimate("re-estimate-stock", h, self.ledger.counts_for(key), after)
        plan = draft_quantity(after, h, self.restock_days(h.shop_id))
        intents: list[Any] = [record]
        action = "noted"
        attrs_extra: dict[str, str | int | float] = {}
        if plan is not None:
            sku = self.skus[h.sku]
            price, amount = line_amount(plan.quantity, sku, day, self.price_rise_day)
            problems = verify(quantity=plan.quantity, unit=h.unit, unit_price_kobo=price,
                              amount_kobo=amount, sku=sku, day=day, rise_day=self.price_rise_day,
                              cap=plan.cap_units)
            note(Observation("tool", "verify-restock-draft", time.time_ns(),
                             input={"quantity": plan.quantity, "multiple": plan.multiple,
                                    "cap": plan.cap_units, "unit_price_kobo": price,
                                    "amount_kobo": amount, "cover_days": plan.cover_days,
                                    "need_units": plan.need_units},
                             output={"problems": problems, "ok": not problems},
                             level="ERROR" if problems else "DEFAULT"))
            text = copy.thanks_draft_text(plan.quantity, h.unit, self.distributor)
            if not problems and copy.figures_ok(text, plan.quantity):
                draft = RestockDraftForApproval(
                    scope, f"draft-{ctx.goal}", h.sku, h.name, h.unit, plan.quantity, price, amount,
                    band_for(after.days_to_run_out), after.confidence, "shop_count", POLICY_VERSION)
                self.drafts.append(draft)
                intents += [draft, SendStockMessage(scope, h.sku, "stock_thanks_draft", text,
                                                    draft_quantity=plan.quantity)]
                action = "draft"
                attrs_extra = {"draft_quantity": plan.quantity, "draft_amount_kobo": amount}
            else:
                attrs_extra = {"error": "restock draft failed the verifier: " + "; ".join(problems)}
        if action == "noted":
            intents.append(SendStockMessage(scope, h.sku, "stock_thanks_noted", copy.thanks_noted_text()))
        return StepOutcome(kind="done", intents=tuple(intents), result=action,
                           trace={**self._attrs(ans.bucket, action, after, ans), **attrs_extra})

    @staticmethod
    def _attrs(verdict: str, action: str, est: StockEstimate,
               ans: StockAnswer | None = None) -> dict[str, str | int | float]:
        out: dict[str, str | int | float] = {
            "verdict": verdict, "stock_action": action, "sku": est.sku,
            "est_confidence": est.confidence, "est_units_left": est.estimated_units_left,
            "est_days_to_run_out": est.days_to_run_out if est.days_to_run_out is not None else -1,
            "est_sell_rate_per_day": est.sell_rate_per_day, "policy_version": POLICY_VERSION,
        }
        if ans is not None:
            out.update({"answer_via": ans.via, "answer_reason": ans.reason})
            if ans.language:
                out["language"] = ans.language
            out["prompt_tokens"] = ans.prompt_tokens
            out["completion_tokens"] = ans.completion_tokens
        return out


def _est(raw: dict[str, Any]) -> StockEstimate:
    return StockEstimate(**{k: v for k, v in raw.items() if k != "run_out_day"})


def _note_estimate(name: str, h: ItemHistory, counts: Sequence[StockCount], est: StockEstimate) -> None:
    """Estimator inputs and outputs, as a tool observation on the turn."""
    note(Observation(
        "tool", name, time.time_ns(),
        input={"shop_id": h.shop_id, "sku": h.sku, "as_of_day": est.as_of_day,
               "deliveries": [[s.day, s.quantity, s.source] for s in h.stock_in
                              if s.day < est.as_of_day and s.day >= est.as_of_day - 56],
               "counts": [[c.day, c.bucket, c.units, c.source] for c in counts]},
        output=est.as_dict(),
        metadata={"policy_version": est.policy_version},
    ))

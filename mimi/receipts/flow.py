"""The delivery receipt flow: one durable runtime job per logged delivery.

Step by step (the job's stage lives in its working note, so a crash resumes
where the last save left it):

* ``new``: build the receipt in code and propose ``SendShopMessage``; sleep
  until a reply wakes the job, or the next check.
* ``awaiting``: read replies not seen yet. Nothing new means no intent and
  back to sleep (quiet). Once a burst has settled, Mimi's model reads the
  whole burst in context (receipt lines, the shop's earlier messages, what
  Mimi sent) and returns a typed verdict (``verdict.py``); the code guards in
  ``decide.py`` turn it into one action:

  - consent: records consent and the shop's witness and finishes;
  - dispute or partial: flags the owner with the shop's words, the evidence
    quote and the disputed receipt lines, and finishes;
  - anything else: one clarifying message (in the shop's language when it
    wrote in a templated one), then an owner flag. No reply by the deadline
    also flags the owner.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from mimi.receipts.decide import Decision, decide
from mimi.receipts.inbox import ShopInbox
from mimi.receipts.intents import (
    FlagReason,
    FlagToOwner,
    RecordDeliveryConsent,
    SendShopMessage,
)
from mimi.receipts.receipt import (
    clarify_text,
    receipt_digest,
    receipt_text,
)
from mimi.receipts.verdict import Judged
from mimi.runtime.steps import StepContext, StepOutcome
from mimi.types import ShopScope
from mimi.world.model import World

HELPER = "delivery_receipt"
# Languages Mimi has a fixed clarifying template for (receipt._CLARIFY).
_SHOP_LANGS = frozenset({"english", "pidgin", "yoruba", "igbo", "hausa"})
Judge = Callable[..., Judged]


@dataclass
class ReceiptFlow:
    world: World
    inbox: ShopInbox
    judge: Judge
    reply_window: timedelta = timedelta(hours=24)
    check_every: timedelta = timedelta(hours=6)
    # Shops send bursts of 2 or 3 messages: wait this long after the newest
    # one before judging, so "Good afternoon" is not answered on its own.
    burst_settle: timedelta = timedelta(seconds=90)

    def __call__(self, ctx: StepContext) -> StepOutcome:
        state = json.loads(ctx.working_note) if ctx.working_note else {"stage": "new"}
        delivery = self.world.delivery(ctx.goal)
        shop = self.world.shop(delivery.shop_id)
        scope = ShopScope(ctx.tenant_id, shop.shop_id)
        if state["stage"] == "new":
            text = receipt_text(delivery, shop.name, self.world.distributor)
            deadline = ctx.now + self.reply_window
            state = {
                "stage": "awaiting",
                "digest": receipt_digest(text),
                "total_kobo": delivery.total_kobo,
                "deadline": deadline.isoformat(),
                "seen": [],
                "clarified": False,
            }
            return StepOutcome(
                kind="sleep",
                intents=(
                    SendShopMessage(
                        scope,
                        delivery.delivery_id,
                        "delivery_receipt",
                        text,
                        delivery.total_kobo,
                    ),
                ),
                working_note=json.dumps(state),
                sleep_until=self._next_check(ctx.now, deadline),
                trace={"stage": "sent_receipt"},
            )
        return self._awaiting(ctx, state, scope, delivery.delivery_id)

    def _next_check(self, now: datetime, deadline: datetime) -> datetime:
        return min(now + self.check_every, deadline)

    def _awaiting(
        self, ctx: StepContext, state: dict, scope: ShopScope, delivery_id: str
    ) -> StepOutcome:
        deadline = datetime.fromisoformat(state["deadline"])
        new = [
            r
            for r in self.inbox.replies(ctx.job_id)
            if r.message_id not in state["seen"]
        ]
        if not new:
            if ctx.now >= deadline:
                return self._finish(
                    state,
                    "no_reply",
                    FlagToOwner(scope, delivery_id, "no_reply"),
                )
            # Quiet: nothing new, nothing sent, no note change.
            return StepOutcome(
                kind="sleep",
                sleep_until=self._next_check(ctx.now, deadline),
                trace={"stage": "quiet"},
            )
        newest = max(datetime.fromisoformat(r.received_at) for r in new)
        if ctx.now < newest + self.burst_settle and ctx.now < deadline:
            # Still typing: quiet until the burst settles.
            return StepOutcome(
                kind="sleep",
                sleep_until=newest + self.burst_settle,
                trace={"stage": "burst_wait", "replies": len(new)},
            )
        all_replies = self.inbox.replies(ctx.job_id)
        earlier = [r for r in all_replies if r.message_id in state["seen"]]
        state["seen"] = [*state["seen"], *(r.message_id for r in new)]
        delivery = self.world.delivery(delivery_id)
        shop = self.world.shop(delivery.shop_id)
        mimi_sent = [receipt_text(delivery, shop.name, self.world.distributor)]
        if state["clarified"]:
            mimi_sent.append(clarify_text(delivery, state.get("clarify_lang", "")))
        judged = self.judge(
            delivery=delivery, earlier=earlier, new=new, mimi_sent=mimi_sent
        )
        decision = decide(
            judged.verdict,
            delivery=delivery,
            replies=[*earlier, *new],
            clarified=bool(state["clarified"]),
        )
        v = judged.verdict
        trace: dict[str, str | int | float] = {
            "replies": len(new),
            "via": judged.via,
            "outcome": v.outcome,
            "confidence": v.confidence,
            "language": v.language,
            "action": decision.action,
            "evidence_found": int(decision.evidence_found),
            "held_back": decision.held_back,
            "prompt_tokens": judged.prompt_tokens,
            "completion_tokens": judged.completion_tokens,
        }
        if decision.action == "consent":
            assert decision.witness is not None
            return self._finish(
                state,
                "confirmed",
                self._consent(scope, delivery_id, state, decision),
                trace,
            )
        words = " / ".join(r.text for r in new)
        if decision.action == "flag_dispute":
            partial = v.outcome == "partial"
            return self._finish(
                state,
                "partial" if partial else "disputed",
                self._flag(
                    scope,
                    delivery_id,
                    "partial_dispute" if partial else "dispute",
                    " / ".join(r.text for r in [*earlier, *new]),
                    decision,
                ),
                trace,
            )
        if decision.action == "flag_unclear":
            return self._finish(
                state,
                "flagged",
                self._flag(scope, delivery_id, "unclear_reply", words, decision),
                trace,
            )
        lang = v.language if v.language in _SHOP_LANGS else "english"
        state["clarified"] = True
        state["clarify_lang"] = lang
        return StepOutcome(
            kind="sleep",
            intents=(
                SendShopMessage(
                    scope,
                    delivery_id,
                    "receipt_clarify",
                    clarify_text(delivery, lang),
                    delivery.total_kobo,
                ),
            ),
            working_note=json.dumps(state),
            sleep_until=self._next_check(ctx.now, deadline),
            trace={**trace, "stage": "clarified"},
        )

    @staticmethod
    def _flag(
        scope: ShopScope,
        delivery_id: str,
        reason: FlagReason,
        words: str,
        decision: Decision,
    ) -> FlagToOwner:
        v = decision.verdict
        return FlagToOwner(
            scope,
            delivery_id,
            reason,
            words,
            evidence_quote=v.evidence_quote if decision.evidence_found else "",
            disputed_lines=decision.disputed_lines,
            language=v.language,
            confidence=v.confidence,
            model_reason=v.reason[:300],
        )

    @staticmethod
    def _consent(
        scope: ShopScope, delivery_id: str, state: dict, decision: Decision
    ) -> RecordDeliveryConsent:
        reply = decision.witness
        assert reply is not None
        return RecordDeliveryConsent(
            scope=scope,
            delivery_id=delivery_id,
            receipt_digest=state["digest"],
            total_kobo=int(state["total_kobo"]),
            witness_message_id=reply.message_id,
            witness_sender=reply.sender,
            witness_text=reply.text,
            witnessed_at=reply.received_at,
            evidence_quote=decision.verdict.evidence_quote,
            language=decision.verdict.language,
            confidence=decision.verdict.confidence,
        )

    @staticmethod
    def _finish(
        state: dict,
        result: str,
        intent: FlagToOwner | RecordDeliveryConsent,
        trace: dict[str, str | int | float] | None = None,
    ) -> StepOutcome:
        state = {**state, "stage": "done", "result": result}
        return StepOutcome(
            kind="done",
            intents=(intent,),
            working_note=json.dumps(state),
            result=result,
            trace={**(trace or {}), "stage": result},
        )

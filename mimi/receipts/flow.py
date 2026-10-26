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

  Voice notes arrive already transcribed (``desk.py``) and are judged like
  typed replies. When every new message is a voice note that could not be
  transcribed, the model is not asked: Mimi asks the shop to type its reply
  (the one clarification), and a second unreadable burst flags the owner. A
  confirm held back because of a doubtful transcript also gets the request
  to type, not the language template.

* ``promise``: a shop's message promised to pay ("I go pay 20k tomorrow";
  ``promises.py``). Once the delivery itself is settled (consent or a
  flag), Mimi reads the amount and day back in plain English, or asks for
  what is missing, and records ``RecordPaymentPromise`` only after the
  shop's YES to that exact read-back. No answer by the deadline, or no YES
  after three messages, records nothing. Mimi never moves money.

Shops that turned voice replies on (``mimi.voice.prefs``) get Mimi's short
replies as a voice note too (``voice_replies.py``); the text always goes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from mimi.receipts.decide import Decision, decide
from mimi.receipts.inbox import ShopInbox, ShopReply
from mimi.receipts.intents import (
    FlagReason,
    FlagToOwner,
    MessagePurpose,
    RecordDeliveryConsent,
    RecordPaymentPromise,
    SendShopMessage,
)
from mimi.receipts.promises import (
    digest as promise_digest,
)
from mimi.receipts.promises import (
    message_for,
    pending_from,
    promise_step,
    scan_burst,
)
from mimi.receipts.receipt import (
    clarify_text,
    receipt_digest,
    receipt_text,
    type_request_text,
)
from mimi.receipts.verdict import Judged
from mimi.receipts.voice_replies import voice_fields
from mimi.runtime.steps import StepContext, StepOutcome
from mimi.types import ShopScope
from mimi.voice.prefs import VoicePrefs
from mimi.voice.promise import lagos_day
from mimi.world.model import Delivery, World

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
    voice_prefs: VoicePrefs | None = None

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
            if ctx.now >= deadline and state["stage"] == "promise":
                return self._promise_done(state, "unanswered")
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
        if state["stage"] == "promise":
            return self._promise_burst(ctx, state, scope, delivery, new, deadline)
        found = scan_burst(new)
        if found is not None:
            state["promise"] = pending_from(*found)
        voice = [r for r in new if r.is_voice]
        voice_trace: dict[str, str | int | float] = {}
        if voice:
            voice_trace = {
                "voice_notes": len(voice),
                "transcripts": ",".join(
                    f"{r.transcript_provider or '-'}:{r.transcript_status}" for r in voice
                ),
            }
        if new and all(r.is_voice and r.transcript_status == "failed" for r in new):
            return self._unreadable(ctx, state, scope, delivery_id, new, deadline, voice_trace)
        mimi_sent = [receipt_text(delivery, shop.name, self.world.distributor)]
        if state["clarified"]:
            mimi_sent.append(self._clarify_sent(delivery, state))
        judged = self.judge(
            delivery=delivery, earlier=earlier, new=new, mimi_sent=mimi_sent
        )
        decision = decide(
            judged.verdict,
            delivery=delivery,
            replies=[*earlier, *new],
            clarified=bool(state["clarified"]),
            new_replies=new,
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
            **voice_trace,
        }
        if decision.action == "consent":
            assert decision.witness is not None
            return self._close(
                ctx,
                state,
                scope,
                delivery,
                "confirmed",
                self._consent(scope, delivery_id, state, decision),
                trace,
            )
        words = " / ".join(r.shown() for r in new)
        if decision.action == "flag_dispute":
            partial = v.outcome == "partial"
            return self._close(
                ctx,
                state,
                scope,
                delivery,
                "partial" if partial else "disputed",
                self._flag(
                    scope,
                    delivery_id,
                    "partial_dispute" if partial else "dispute",
                    " / ".join(r.shown() for r in [*earlier, *new]),
                    decision,
                ),
                trace,
            )
        if decision.action == "flag_unclear":
            return self._close(
                ctx,
                state,
                scope,
                delivery,
                "flagged",
                self._flag(scope, delivery_id, "unclear_reply", words, decision),
                trace,
            )
        if any(r.voice_doubtful for r in new) or (
            decision.witness is not None and decision.witness.voice_doubtful
        ):
            # Mimi could not trust what it heard: ask the shop to type.
            state["clarified"] = True
            state["clarify_kind"] = "type"
            return StepOutcome(
                kind="sleep",
                intents=(
                    self._send(
                        ctx, scope, delivery, "receipt_type_request", type_request_text(delivery)
                    ),
                ),
                working_note=json.dumps(state),
                sleep_until=self._next_check(ctx.now, deadline),
                trace={**trace, "stage": "asked_to_type"},
            )
        lang = v.language if v.language in _SHOP_LANGS else "english"
        state["clarified"] = True
        state["clarify_lang"] = lang
        return StepOutcome(
            kind="sleep",
            intents=(
                self._send(ctx, scope, delivery, "receipt_clarify", clarify_text(delivery, lang)),
            ),
            working_note=json.dumps(state),
            sleep_until=self._next_check(ctx.now, deadline),
            trace={**trace, "stage": "clarified"},
        )

    @staticmethod
    def _clarify_sent(delivery: Delivery, state: dict) -> str:
        if state.get("clarify_kind") == "type":
            return type_request_text(delivery)
        return clarify_text(delivery, state.get("clarify_lang", ""))

    def _unreadable(
        self,
        ctx: StepContext,
        state: dict,
        scope: ShopScope,
        delivery_id: str,
        new: list[ShopReply],
        deadline: datetime,
        voice_trace: dict[str, str | int | float],
    ) -> StepOutcome:
        """Every new message is a voice note with no usable transcript.
        Never guess: ask the shop to type, or flag the owner if Mimi
        already asked once."""
        trace = {**voice_trace, "replies": len(new), "action": "no_transcript"}
        if state["clarified"]:
            return self._finish(
                state,
                "flagged",
                FlagToOwner(
                    scope,
                    delivery_id,
                    "voice_unreadable",
                    " / ".join(r.shown() for r in new),
                ),
                trace,
            )
        delivery = self.world.delivery(delivery_id)
        state["clarified"] = True
        state["clarify_kind"] = "type"
        return StepOutcome(
            kind="sleep",
            intents=(
                self._send(
                    ctx, scope, delivery, "receipt_type_request", type_request_text(delivery)
                ),
            ),
            working_note=json.dumps(state),
            sleep_until=self._next_check(ctx.now, deadline),
            trace={**trace, "stage": "asked_to_type"},
        )

    def _send(
        self,
        ctx: StepContext,
        scope: ShopScope,
        delivery: Delivery,
        purpose: MessagePurpose,
        text: str,
        *,
        amount_kobo: int | None = None,
        due_on: date | None = None,
    ) -> SendShopMessage:
        """A short reply: text always, and a voice script when the shop
        turned voice replies on."""
        pref = self.voice_prefs.get(scope.shop_id) if self.voice_prefs is not None else None
        script, lang = voice_fields(
            pref,
            purpose,
            text,
            today=lagos_day(ctx.now),
            amount_kobo=amount_kobo,
            due_on=due_on,
        )
        readback = purpose == "promise_readback"
        return SendShopMessage(
            scope,
            delivery.delivery_id,
            purpose,
            text,
            delivery.total_kobo,
            promise_kobo=(amount_kobo or 0) if readback else 0,
            promise_due_on=due_on.isoformat() if readback and due_on else "",
            voice_script=script,
            voice_language=lang,
        )

    # ------------------------------------------------------------ promises

    def _close(
        self,
        ctx: StepContext,
        state: dict,
        scope: ShopScope,
        delivery: Delivery,
        result: str,
        intent: FlagToOwner | RecordDeliveryConsent,
        trace: dict[str, str | int | float] | None = None,
    ) -> StepOutcome:
        """The delivery is settled. With a payment promise heard, ask about
        it before finishing; otherwise finish."""
        if not state.get("promise"):
            return self._finish(state, result, intent, trace)
        state = {**state, "result": result}
        return self._promise_ask(ctx, state, scope, delivery, (intent,), trace or {})

    def _promise_ask(
        self,
        ctx: StepContext,
        state: dict,
        scope: ShopScope,
        delivery: Delivery,
        before: tuple[object, ...],
        trace: dict[str, str | int | float],
        pending: dict | None = None,
    ) -> StepOutcome:
        p = dict(pending or state["promise"])
        purpose, text = message_for(p, delivery.delivery_id)
        due = date.fromisoformat(p["due_on"]) if p.get("due_on") else None
        msg = self._send(
            ctx, scope, delivery, purpose, text, amount_kobo=p.get("amount_kobo"), due_on=due
        )
        p["asked"] = "readback" if purpose == "promise_readback" else "question"
        p["sent"] = int(p.get("sent", 0)) + 1
        p["last_text"] = text
        deadline = ctx.now + self.reply_window
        state = {**state, "stage": "promise", "promise": p, "deadline": deadline.isoformat()}
        return StepOutcome(
            kind="sleep",
            intents=(*before, msg),
            working_note=json.dumps(state),
            sleep_until=self._next_check(ctx.now, deadline),
            trace={**trace, "stage": f"promise_{p['asked']}", "promise_issue": p.get("issue", "")},
        )

    def _promise_burst(
        self,
        ctx: StepContext,
        state: dict,
        scope: ShopScope,
        delivery: Delivery,
        new: list[ShopReply],
        deadline: datetime,
    ) -> StepOutcome:
        step = promise_step(state["promise"], new)
        trace: dict[str, str | int | float] = {
            "replies": len(new),
            "promise_action": step.action,
            "promise_reason": step.reason,
        }
        if step.action == "record":
            assert step.witness is not None
            p, w = step.pending, step.witness
            intent = RecordPaymentPromise(
                scope=scope,
                delivery_id=delivery.delivery_id,
                amount_kobo=int(p["amount_kobo"]),
                due_on=str(p["due_on"]),
                said_message_id=p["said_message_id"],
                said_text=p["said_text"],
                said_at=p["said_at"],
                said_kind=p["said_kind"],
                transcript_provider=p["transcript_provider"],
                readback_text=p["last_text"],
                readback_digest=promise_digest(p["last_text"]),
                confirm_message_id=w.message_id,
                confirm_text=w.text,
                confirmed_at=w.received_at,
            )
            return self._promise_done(state, "recorded", (intent,), trace)
        if step.action == "give_up":
            return self._promise_done(state, "not_recorded", (), trace)
        return self._promise_ask(ctx, state, scope, delivery, (), trace, step.pending)

    @staticmethod
    def _promise_done(
        state: dict,
        how: str,
        intents: tuple[object, ...] = (),
        trace: dict[str, str | int | float] | None = None,
    ) -> StepOutcome:
        result = state.get("result", "flagged")
        state = {**state, "stage": "done", "promise_result": how}
        return StepOutcome(
            kind="done",
            intents=intents,
            working_note=json.dumps(state),
            result=result,
            trace={**(trace or {}), "stage": f"promise_{how}"},
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
            witness_kind=reply.kind,
            transcript_provider=reply.transcript_provider,
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

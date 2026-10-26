"""Payment promises inside the receipt flow: read back, then wait for YES.

A shop often answers a receipt with a promise: "Everything don land. I go
pay 20k tomorrow." ``mimi.voice.promise`` reads the amount (integer kobo)
and the day (Africa/Lagos, from the message's own timestamp) in code. This
module decides what Mimi does with it:

* a clear promise is read back in plain English ("You said you will pay
  N20,000 on Tuesday 27 October 2026 ... Reply YES if that is right") and
  nothing is recorded yet;
* a doubtful transcript, a missing or ambiguous amount, a missing or
  ambiguous day gets one plain-English question, and nothing is recorded;
* only the shop's own YES to the read-back (checked in code, on a typed
  message or a voice note whose transcript is ``ok``) records it, with
  ``RecordPaymentPromise``. A YES that brings a different amount or day is
  read back again instead;
* at most ``MAX_PROMISE_MESSAGES`` messages about one promise; after that,
  or when the reply window closes, nothing is recorded.

Mimi never moves money: the promise is a note for the distributor's books
and the owner's follow-up, nothing more.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from mimi.receipts.inbox import ShopReply
from mimi.receipts.receipt import naira
from mimi.voice.promise import PromiseReading, is_yes, read_promise

MAX_PROMISE_MESSAGES = 3
PromiseAction = Literal["record", "read_back", "ask", "give_up"]
PromisePurpose = Literal["promise_readback", "promise_clarify"]


def long_date(d: date) -> str:
    """'Tuesday 27 October 2026'."""
    return f"{d:%A} {d.day} {d:%B %Y}"


READBACK = (
    "You said you will pay {amount} on {day} for delivery {ref}. Reply YES if "
    "that is right, or tell me the right amount and day. I only note your "
    "promise; I do not take or move money."
)
_QUESTIONS: dict[str, str] = {
    "low_quality_transcript": (
        "I could not hear the amount and day clearly in your voice note about "
        "delivery {ref}. Please type how much you will pay and on what day."
    ),
    "no_amount": "How much will you pay for delivery {ref}, and on what day?",
    "no_amount_day": "How much will you pay for delivery {ref} on {day}?",
    "ambiguous_amount": (
        "I am not sure of the amount for delivery {ref}. Please write the full "
        "amount in naira and the day you will pay."
    ),
    "several_amounts": (
        "I heard more than one amount for delivery {ref}. How much will you "
        "pay, and on what day?"
    ),
    "no_date": "On what day will you pay {amount} for delivery {ref}?",
    "ambiguous_date": (
        "On what day will you pay {amount} for delivery {ref}? Please name the "
        "day, for example Friday."
    ),
    "past_date": "That day has passed. On what day will you pay {amount} for delivery {ref}?",
    "correction": "Please tell me the right amount and the day you will pay for delivery {ref}.",
}


def readback_text(ref: str, amount_kobo: int, due_on: date) -> str:
    return READBACK.format(amount=naira(amount_kobo), day=long_date(due_on), ref=ref)


def question_text(
    issue: str, ref: str, amount_kobo: int | None = None, due_on: date | None = None
) -> str:
    """One plain-English question for what is missing. Never model-written."""
    key = issue
    if issue in ("no_date", "ambiguous_date", "several_dates", "past_date") and amount_kobo is None:
        key = "no_amount"
    if issue == "several_dates":
        key = "ambiguous_date" if amount_kobo is not None else "no_amount"
    if key == "no_amount" and due_on is not None:
        key = "no_amount_day"
    template = _QUESTIONS.get(key, _QUESTIONS["correction"])
    return template.format(
        ref=ref,
        amount=naira(amount_kobo) if amount_kobo is not None else "",
        day=long_date(due_on) if due_on is not None else "",
    )


def reading_of(reply: ShopReply) -> PromiseReading:
    """What one shop message says about paying. A voice note with no
    transcript says nothing; a doubtful transcript can only be unclear."""
    if reply.is_voice and reply.transcript_status == "failed":
        return PromiseReading("none")
    return read_promise(reply.text, reply.received_at, transcript_ok=not reply.voice_doubtful)


def scan_burst(replies: Sequence[ShopReply]) -> tuple[PromiseReading, ShopReply] | None:
    """The newest promise (clear or unclear) in a burst, with its message."""
    found: tuple[PromiseReading, ShopReply] | None = None
    for r in replies:
        reading = reading_of(r)
        if reading.found:
            found = (reading, r)
    return found


def pending_from(reading: PromiseReading, reply: ShopReply) -> dict[str, Any]:
    """The promise as it sits in the job's working note (JSON)."""
    return {
        "status": reading.status,
        "issue": reading.issue,
        "amount_kobo": reading.amount_kobo,
        "due_on": reading.due_on.isoformat() if reading.due_on else None,
        "said_message_id": reply.message_id,
        "said_text": reply.text,
        "said_kind": reply.kind,
        "said_at": reply.received_at,
        "transcript_provider": reply.transcript_provider,
        "asked": "",
        "sent": 0,
        "last_text": "",
    }


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _due(p: dict[str, Any]) -> date | None:
    return date.fromisoformat(p["due_on"]) if p.get("due_on") else None


def message_for(pending: dict[str, Any], ref: str) -> tuple[PromisePurpose, str]:
    """(purpose, text) for the next message about this promise."""
    due = _due(pending)
    if pending["status"] == "promise" and pending["amount_kobo"] is not None and due:
        return "promise_readback", readback_text(ref, int(pending["amount_kobo"]), due)
    return "promise_clarify", question_text(pending["issue"], ref, pending["amount_kobo"], due)


def digest(text: str) -> str:
    return "prom_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class PromiseStep:
    action: PromiseAction
    pending: dict[str, Any]
    witness: ShopReply | None = None
    reason: str = ""


def promise_step(pending: dict[str, Any], new: Sequence[ShopReply]) -> PromiseStep:
    """What to do with the shop's answer to a read-back or a question.

    ``record`` only when Mimi read back a complete promise and the shop
    answered YES with nothing that changes the amount or the day.
    """
    usable = [r for r in new if not (r.is_voice and r.transcript_status == "failed")]
    sent = int(pending.get("sent", 0))
    more = sent < MAX_PROMISE_MESSAGES

    def again(p: dict[str, Any], reason: str) -> PromiseStep:
        if not more:
            return PromiseStep("give_up", p, reason=f"{reason}; message limit")
        return PromiseStep("read_back" if p["status"] == "promise" else "ask", p, reason=reason)

    if not usable or any(r.voice_doubtful for r in usable):
        p = {**pending, "status": "unclear", "issue": "low_quality_transcript"}
        return again(p, "voice note could not be trusted")
    newest = max(usable, key=lambda r: r.received_at)
    known_kobo = pending.get("amount_kobo")
    known_due = _due(pending)
    combined = " . ".join(r.text for r in usable)
    reading = read_promise(
        combined,
        newest.received_at,
        answering=True,
        known_kobo=known_kobo,
        known_due=known_due,
    )
    same = (
        reading.status == "promise"
        and reading.amount_kobo == known_kobo
        and reading.due_on == known_due
    )
    if pending.get("asked") == "readback" and pending["status"] == "promise":
        yes = [r for r in usable if is_yes(r.text)]
        if yes and same:
            return PromiseStep("record", pending, witness=yes[-1], reason="shop said YES")
    if reading.status == "promise" and (not same or pending["status"] != "promise"):
        p = {
            **pending,
            "status": "promise",
            "issue": "",
            "amount_kobo": reading.amount_kobo,
            "due_on": reading.due_on.isoformat() if reading.due_on else None,
            "answer_message_id": newest.message_id,
        }
        return again(p, "new amount or day")
    if reading.status == "unclear" and (
        reading.amount_kobo != known_kobo or reading.due_on not in (None, known_due)
    ):
        p = {
            **pending,
            "status": "unclear",
            "issue": reading.issue,
            "amount_kobo": reading.amount_kobo if reading.amount_kobo is not None else known_kobo,
            "due_on": _iso(reading.due_on or known_due),
        }
        return again(p, f"still unclear: {reading.issue}")
    if pending["status"] == "unclear" and reading.status == "unclear":
        p = {**pending, "issue": reading.issue or pending["issue"]}
        return again(p, f"still unclear: {p['issue']}")
    # A "no", or anything without a new amount or day: ask for the right one.
    p = {**pending, "status": "unclear", "issue": "correction"}
    return again(p, "not a YES")

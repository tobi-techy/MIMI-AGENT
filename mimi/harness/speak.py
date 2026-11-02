"""When to speak: quiet by default (RAI-300).

Every wake-up ends in a useful message or deliberate silence. The decision
runs after the turn's work and before ``send_to_user`` returns one of:

* ``speak_now``: send the draft (a direct question, a decision needed, money
  at risk, a promise due today, a routine with new facts).
* ``batch_into_digest``: write it to the owner's digest queue; the daily note
  (RAI-289) reads it. Used when the per-audience daily cap is spent, or for
  routine "matched clean" news that can wait.
* ``stay_quiet``: send nothing. The loop ends the turn as ``done_quiet``
  with the reason. Every silence is logged with its reason on the trace.

Rules first, in plain code; the model (``muse-spark-1.3`` at low effort,
``META_REASONING_EFFORT``) decides only the cases in between. Must-speak
rules are code and never left to the model: missing a fake alert is worse
than an extra message. Silence never hides a failure: errors and blocked
jobs always reach the owner.

It wraps the routines send path from RAI-289 (``mimi/routines/`` is not on
``main`` yet), so it is built against a small interface: ``speak.py`` takes
a ``SpeakFacts`` (what the turn found) and a ``SpeakPorts`` (caps, digest
queue, the model for in-between cases) and returns a ``SpeakDecision`` with
a reason. The loop and the API wire their own ports.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

SpeakAction = Literal["speak_now", "batch_into_digest", "stay_quiet"]
Audience = Literal["owner", "shop", "rep"]

# Which audience a draft's channel belongs to. The console and the owner's
# WhatsApp both reach the owner; a shop thread reaches one shop.
CHANNEL_AUDIENCE: dict[str, Audience] = {
    "owner_console": "owner",
    "owner_whatsapp": "owner",
    "shop_whatsapp": "shop",
    "rep_whatsapp": "rep",
}

# Env flags (RAI-300): MIMI_SPEAK shadow (log the decision, still send),
# enforce (hold quiet and digest-bound drafts), off (no decision, send all).
SPEAK_MODES = ("shadow", "enforce", "off")
DEFAULT_MODE = "shadow"
DEFAULT_OWNER_DAILY = 3
DEFAULT_SHOP_DAILY = 1
DEFAULT_REP_DAILY = 3


class SpeakFacts(BaseModel):
    """What the turn found, read by code from the run, never the model."""

    model_config = ConfigDict(extra="forbid")

    # Who the draft goes to (from the send_to_user channel) and, for a shop
    # or rep thread, whose thread it is.
    audience: Audience = "owner"
    shop_id: str = ""
    # A direct question from that person on this turn: always answered (100%).
    direct_question: bool = False
    # Something needing the recipient's decision (an approval or a choice).
    needs_decision: bool = False
    # Money at risk: a fake alert, an unverified payment, a disputed amount.
    money_at_risk: bool = False
    # A promise due today (a follow-up the run set or confirmed).
    promise_due: bool = False
    # The run hit an error or a blocked job: always reaches the owner.
    error_or_blocked: bool = False
    # New money facts since the last message (a balance, a matched payment).
    new_money_facts: bool = False
    # The turn changed something real (a reminder sent, a receipt recorded).
    real_change: bool = False
    # No new facts since the last message: never speak.
    no_new_facts: bool = False
    # The update was already sent on this thread (a duplicate): never speak.
    already_sent: bool = False
    # A routine that matched clean (14 payments matched, no flags): digest.
    routine_clean: bool = False
    # The body already proposed, for the digest queue and duplicate checks.
    body: str = ""
    kind: str = "reply"


class SpeakDecision(BaseModel):
    """The decision and its reason. Every decision has a logged reason."""

    model_config = ConfigDict(extra="forbid")

    action: SpeakAction
    reason: str = Field(min_length=1, max_length=300)
    # "rule" for code, "model" for the in-between cases.
    layer: Literal["rule", "model"] = "rule"


def _rule(action: SpeakAction, reason: str) -> SpeakDecision:
    return SpeakDecision(action=action, reason=reason, layer="rule")


def decide_rules(facts: SpeakFacts) -> SpeakDecision | None:
    """The always and never rules, in plain code (no model). None means the
    case is in between: only then may the model decide."""
    if facts.direct_question:
        return _rule("speak_now", "a direct question from that person is always answered")
    if facts.error_or_blocked:
        return _rule("speak_now", "an error or blocked job always reaches the owner")
    if facts.needs_decision:
        return _rule("speak_now", "something needs the recipient's decision")
    if facts.money_at_risk:
        return _rule("speak_now", "money is at risk")
    if facts.promise_due:
        return _rule("speak_now", "a promise is due today")
    if facts.no_new_facts:
        return _rule("stay_quiet", "no new facts since the last message")
    if facts.already_sent:
        return _rule("stay_quiet", "this update was already sent")
    if facts.routine_clean:
        return _rule("batch_into_digest", "a routine that matched clean waits for the daily note")
    return None


class SpeakCounts(Protocol):
    """Per-tenant daily counts. Go owns them; the fake keeps them in memory."""

    def unprompted_today(self, tenant_id: str, audience: Audience, shop_id: str,
                         day: date) -> int: ...

    def record_unprompted(self, tenant_id: str, audience: Audience, shop_id: str,
                          day: date) -> None: ...


class DigestQueue(Protocol):
    """The owner's digest queue, read by the daily note (RAI-289)."""

    def add(self, tenant_id: str, shop_id: str, body: str, kind: str,
            day: date, reason: str) -> None: ...


class BetweenModel(Protocol):
    """The cheap model step for in-between cases only (low effort)."""

    name: str

    def decide_between(self, prompt: str) -> str: ...


@dataclass
class InMemorySpeakStore:
    """The in-process ``SpeakCounts`` + ``DigestQueue`` (tests and the eval).

    The digest items are ``(tenant_id, shop_id, body, kind, day, reason)``
    dicts. Nothing here touches the book: counts and the queue are the only
    state."""

    counts: dict[tuple[str, str, str, str], int] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)

    def unprompted_today(self, tenant_id: str, audience: Audience, shop_id: str,
                         day: date) -> int:
        return self.counts.get((tenant_id, audience, shop_id, day.isoformat()), 0)

    def record_unprompted(self, tenant_id: str, audience: Audience, shop_id: str,
                          day: date) -> None:
        key = (tenant_id, audience, shop_id, day.isoformat())
        self.counts[key] = self.counts.get(key, 0) + 1

    def add(self, tenant_id: str, shop_id: str, body: str, kind: str,
            day: date, reason: str) -> None:
        self.items.append({"tenant_id": tenant_id, "shop_id": shop_id, "body": body,
                           "kind": kind, "day": day.isoformat(), "reason": reason})


@dataclass(frozen=True)
class SpeakLimits:
    """Per-audience daily caps on unprompted messages (the owner changes them
    in Settings; Go stores them per tenant). Approvals and money-at-risk
    alerts never count."""

    owner_daily: int = DEFAULT_OWNER_DAILY
    shop_daily: int = DEFAULT_SHOP_DAILY
    rep_daily: int = DEFAULT_REP_DAILY

    def cap_for(self, audience: Audience) -> int:
        return {"owner": self.owner_daily, "shop": self.shop_daily,
                "rep": self.rep_daily}[audience]


@dataclass(frozen=True)
class SpeakConfig:
    """How the decision is enforced. ``shadow`` (default) logs the decision
    and still sends; ``enforce`` holds quiet and digest-bound drafts; ``off``
    skips the decision and sends everything."""

    mode: str = DEFAULT_MODE
    limits: SpeakLimits = field(default_factory=SpeakLimits)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SpeakConfig:
        e = env if env is not None else os.environ
        mode = (e.get("MIMI_SPEAK", "") or DEFAULT_MODE).strip().lower()
        if mode not in SPEAK_MODES:
            mode = DEFAULT_MODE
        return cls(mode=mode, limits=SpeakLimits(
            owner_daily=_int(e.get("MIMI_SPEAK_OWNER_DAILY"), DEFAULT_OWNER_DAILY),
            shop_daily=_int(e.get("MIMI_SPEAK_SHOP_DAILY"), DEFAULT_SHOP_DAILY),
            rep_daily=_int(e.get("MIMI_SPEAK_REP_DAILY"), DEFAULT_REP_DAILY)))


def _int(raw: str | None, default: int) -> int:
    try:
        return max(0, int((raw or "").strip()))
    except (ValueError, TypeError):
        return default


IN_BETWEEN_PROMPT = """You decide whether a distributor's assistant should send a message now, hold it for the daily digest, or stay quiet. Quiet is allowed. Answer with one JSON object: {"action": "speak_now" | "batch_into_digest" | "stay_quiet", "reason": "one short line"}.
Send now only when the person gains something new: a new fact, a change, or news they asked for. Hold for the digest when it can wait until the daily note. Stay quiet when there is nothing new, it repeats what was sent, or it is only working noise.
"""


def decide(facts: SpeakFacts, *, tenant_id: str, day: date, counts: SpeakCounts,
           digest: DigestQueue, limits: SpeakLimits | None = None,
           model: BetweenModel | None = None) -> SpeakDecision:
    """The full decision: rules first, the daily cap, then the model only for
    the cases in between. Always returns a decision with a reason; never
    raises (a model failure stays quiet-safe: batch routine news, speak
    person news)."""
    lim = limits or SpeakLimits()
    ruled = decide_rules(facts)
    if ruled is not None:
        decision = ruled
    else:
        decision = _decide_between(facts, model)
    if decision.action == "speak_now":
        decision = _apply_cap(facts, decision, tenant_id=tenant_id, day=day,
                              counts=counts, digest=digest, limits=lim)
    elif decision.action == "batch_into_digest":
        digest.add(tenant_id, facts.shop_id, facts.body, facts.kind, day, decision.reason)
    return decision


def _decide_between(facts: SpeakFacts, model: BetweenModel | None) -> SpeakDecision:
    """Only the in-between cases reach the model. No model (offline, CI, a
    failure) means: routine and rep news waits for the digest, anything with
    new facts or a real change speaks, else quiet."""
    if model is None:
        return _offline_between(facts)
    prompt = (IN_BETWEEN_PROMPT + "Facts: "
              + json.dumps(facts.model_dump(mode="json"), sort_keys=True))
    try:
        raw = model.decide_between(prompt)
        data = json.loads(_json_block(raw))
        action = str(data.get("action") or "").strip()
        reason = str(data.get("reason") or "").strip()[:300]
        if action in ("speak_now", "batch_into_digest", "stay_quiet") and reason:
            return SpeakDecision(action=action, reason=reason, layer="model")  # type: ignore[arg-type]
    except (ValueError, TypeError, AttributeError):
        pass
    return _offline_between(facts)


def _offline_between(facts: SpeakFacts) -> SpeakDecision:
    if facts.new_money_facts or facts.real_change:
        return _rule("speak_now", "new facts since the last message")
    return _rule("stay_quiet", "nothing new worth sending")


def _json_block(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


def _apply_cap(facts: SpeakFacts, decision: SpeakDecision, *, tenant_id: str,
               day: date, counts: SpeakCounts, digest: DigestQueue,
               limits: SpeakLimits) -> SpeakDecision:
    """The per-audience daily cap: the 4th unprompted owner message (2nd per
    shop) waits for the digest. Approvals, money-at-risk alerts, direct
    answers and real changes always speak: the cap only throttles routine
    unprompted news, never what a person asked for or what moved."""
    if (facts.needs_decision or facts.money_at_risk or facts.direct_question
            or facts.real_change):
        return decision
    scope_shop = facts.shop_id if facts.audience in ("shop", "rep") else ""
    used = counts.unprompted_today(tenant_id, facts.audience, scope_shop, day)
    if used >= limits.cap_for(facts.audience):
        reason = (f"over the daily cap ({used + 1} unprompted, "
                  f"the limit is {limits.cap_for(facts.audience)}); "
                  "held for the daily note")
        digest.add(tenant_id, facts.shop_id, facts.body, facts.kind, day, reason)
        return SpeakDecision(action="batch_into_digest", reason=reason, layer="rule")
    counts.record_unprompted(tenant_id, facts.audience, scope_shop, day)
    return decision


def audience_for(channel: str) -> Audience:
    return CHANNEL_AUDIENCE.get(channel, "owner")


def facts_from_turn(*, channel: str, shop_id: str = "", direct_question: bool,
                    needs_decision: bool = False, money_at_risk: bool = False,
                    promise_due: bool = False, error_or_blocked: bool = False,
                    new_money_facts: bool = False, real_change: bool = False,
                    no_new_facts: bool = False, already_sent: bool = False,
                    routine_clean: bool = False, body: str = "",
                    kind: str = "reply") -> SpeakFacts:
    """Build the facts for one draft from what the turn found, in code."""
    return SpeakFacts(audience=audience_for(channel), shop_id=shop_id,
                      direct_question=direct_question, needs_decision=needs_decision,
                      money_at_risk=money_at_risk, promise_due=promise_due,
                      error_or_blocked=error_or_blocked, new_money_facts=new_money_facts,
                      real_change=real_change, no_new_facts=no_new_facts,
                      already_sent=already_sent, routine_clean=routine_clean,
                      body=body, kind=kind)


def reasons_for_trace(decisions: Sequence[SpeakDecision]) -> list[dict[str, str]]:
    """Every decision with its reason, for the trace tag (Langfuse)."""
    return [{"action": d.action, "reason": d.reason, "layer": d.layer} for d in decisions]

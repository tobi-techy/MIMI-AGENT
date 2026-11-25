"""What the console shows for a thread at a glance: helper, status bucket, whose
move it is, title.

All written by code from the thread's own rows (its kind, the shop's book
name, its turns, pending approvals and the router's thread state). Shop text
never reaches a title.

* ``helper``: the helper of the thread's newest routed turn (``mimi`` when
  nothing has been routed yet), plus its display name (``helper_name``).
* ``status``: ``needs_you`` (a pending approval, or a turn waiting on a person:
  an ask, a park, a denial - the handoff contract's rule, ``handoff.task_status``),
  ``running`` (a turn queued or running), else ``done``.
* ``waiting_on``: whose move it is - ``owner`` (the distributor's owner at the
  console), ``shop`` or ``rep`` (the counterparty outside the console), or
  ``nobody`` (nothing is blocked: a running turn or a finished thread).
* ``title``: who the thread is with and, for a shop, what it is about.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from mimi.harness import binding as bx
from mimi.harness import handoff as hx
from mimi.harness.skills import HELPER_TITLE, HELPERS

# Owner-facing names for each skill (the router's LABELS are shop-facing).
SKILL_TITLES: dict[str, str] = {
    "order/order-intake": "New order",
    "order/drop-confirmation": "Delivery received",
    "order/delivery-status": "Delivery not arrived",
    "order/open-orders": "Open orders",
    "collector/collections": "Balance question",
    "collector/payment-check": "Payment check",
    "collector/promise-to-pay": "Promise to pay",
    "collector/chase": "Chasing overdue shops",
    "proof/fake-alert-check": "Payment screenshot",
    "credit/credit-request": "Credit request",
    "credit/credit-review": "Credit review",
    "stock/stock-count": "Stock count",
    "stock/low-stock-report": "Low stock report",
    "brand/promo-info": "Promo question",
    "brand/brand-email": "Email to a brand",
    "mimi/chat": "Chat",
    "mimi/escalate-owner": "Complaint for you",
    "mimi/shop-onboarding": "New shop",
    "mimi/owner-brief": "Today's summary",
    "mimi/budget": "Daily allowance",
    "mimi/stop": "Stopped",
}
MAX_TITLE = 80


def skill_title(skill: str) -> str:
    if skill in SKILL_TITLES:
        return SKILL_TITLES[skill]
    tail = skill.split("/")[-1].replace("-", " ").replace("_", " ").strip()
    return tail[:1].upper() + tail[1:] if tail else ""


def _status(pending: int, turns: Sequence[Mapping[str, Any]]) -> str:
    """The thread's bucket, from the handoff contract's own rule (hx.task_status),
    so a thread and its tasks never disagree about who is being waited on."""
    if pending > 0:
        return "needs_you"
    ranks = {hx.task_status(str(t.get("state") or ""), str(t.get("status") or ""))
             for t in turns}
    if "needs_you" in ranks:
        return "needs_you"
    if "working" in ranks:
        return "running"
    return "done"


def _waiting_on(kind: str, pending: int, turns: Sequence[Mapping[str, Any]],
                state: Mapping[str, Any], now: datetime) -> str:
    """Whose move it is, in this order:

    1. A pending approval: only the owner can close it, so ``owner``.
    2. A live ask (``state.pending_skill``, still inside its ``binding.ASK_TTL``
       window): the counterparty on the other side - a shop, a rep, or the owner
       on the owner's own thread.
    3. A turn parked, denied or failed (``handoff.task_status`` ``needs_you``,
       or ``failed``): the console human's move, so ``owner``. A failed turn is
       not ``needs_you`` to the handoff contract - it is its own rank - but it
       parks for a human all the same.
    4. Otherwise nothing is blocked: ``nobody``.

    An *expired* ask is not live and falls through to 3 and 4, so an old ask on
    a finished thread reads ``nobody``. That is the branch that looks like a
    bug later, so it lives here on purpose: the ask's window is
    ``binding.ask_is_live`` and nothing else re-derives it.
    """
    if pending > 0:
        return "owner"
    if bx.ask_is_live(str(state.get("pending_skill") or ""),
                      str(state.get("asked_at") or ""), now):
        if kind == "owner":
            return "owner"
        return "rep" if kind == "rep" else "shop"
    if any(hx.task_status(str(t.get("state") or ""), str(t.get("status") or ""))
           in ("needs_you", "failed") for t in turns):
        return "owner"
    return "nobody"


def derive(*, kind: str, thread_id: str, shop_id: str, shop_name: str, pending: int,
           turns: Sequence[Mapping[str, Any]], state: Mapping[str, Any], now: datetime,
           rep_name: str = "") -> dict[str, str]:
    """``turns`` oldest first, each with ``helper``, ``skill``, ``state`` and ``status``.

    ``state`` is the thread's own router state (the ``threads.state`` JSON, parsed)
    and ``now`` is the clock the ask's expiry is read against; both feed
    ``waiting_on`` only. The list and the detail read this one function, so the
    two cannot disagree about whose move it is.
    """
    routed = [t for t in turns if str(t.get("helper") or "") in HELPERS]
    newest = routed[-1] if routed else None
    helper = str(newest["helper"]) if newest else "mimi"
    skill = str(newest.get("skill") or "") if newest else ""
    about = skill_title(skill) if skill else ""
    if kind == "shop":
        who = shop_name or shop_id or "A shop"
        title = f"{who}: {about}" if about else who
    elif kind == "owner":
        title = "You and Mimi"
    elif kind == "rep":
        title = f"Rep {rep_name}" if rep_name else "A rep"
    elif kind == "job":
        title = about or "Background job"
    elif kind == "new":
        title = "New contact"
    else:
        title = about or "Thread"
    return {"helper": helper, "helper_name": HELPER_TITLE.get(helper, helper.title()),
            "status": _status(pending, turns), "title": title[:MAX_TITLE],
            "waiting_on": _waiting_on(kind, pending, turns, state, now)}

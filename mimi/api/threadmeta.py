"""What the console shows for a thread at a glance: helper, status bucket, title.

All written by code from the thread's own rows (its kind, the shop's book
name, its turns and pending approvals). Shop text never reaches a title.

* ``helper``: the helper of the thread's newest routed turn (``mimi`` when
  nothing has been routed yet), plus its display name (``helper_name``).
* ``status``: ``needs_you`` (a pending approval, or a turn waiting on a person:
  an ask, a park, a denial - the handoff contract's rule, ``handoff.task_status``),
  ``running`` (a turn queued or running), else ``done``.
* ``title``: who the thread is with and, for a shop, what it is about.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

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


def derive(*, kind: str, thread_id: str, shop_id: str, shop_name: str, pending: int,
           turns: Sequence[Mapping[str, Any]], rep_name: str = "") -> dict[str, str]:
    """``turns`` oldest first, each with ``helper``, ``skill``, ``state`` and ``status``."""
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
            "status": _status(pending, turns), "title": title[:MAX_TITLE]}

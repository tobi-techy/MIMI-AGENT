"""Helper-to-helper work: the transport that makes an inbox real.

An applied ``ask_helper`` / ``delegate_task`` intent writes one inbox item
(``mimi.api.inbox``). With ``MIMI_HELPER_DELEGATION`` on, the transport then
wakes the receiving helper on its own thread, as a ``helper_finished`` event
the front door already routes: the child runs the receiver's skill, and its
result is written back to the item and so to the sender's line.

Everything here is code and guards: who may be handed work, the ping-pong and
depth-2 refusal, and the item's state. No model decides any of it.

The switch gates the *wake* and the widened ``delegate_task`` tool, not the
record: with the switch off an applied ``ask_helper`` still writes a
``queued`` item, so the inbox read is honest either way
(docs/HELPER-INBOX-CONTRACT.md).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa

from mimi.api import inbox as ib
from mimi.harness.skills import BY_KEY
from mimi.store import schema as s
from mimi.store import util as store_util

ENV_VAR = "MIMI_HELPER_DELEGATION"

# The skill each receiver runs when another helper hands it work. All exist in
# the catalogue and none is rule-only, so the run is a real one.
DELEGATE_SKILLS: dict[str, str] = {
    "proof": "proof/fake-alert-check",
    "credit": "credit/credit-review",
    "order": "order/delivery-status",
    "collector": "collector/payment-check",
    "stock": "stock/low-stock-report",
    "brand": "brand/promo-info",
}


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """Is helper-to-helper delegation on? Off unless the owner turns it on.

    Off by default: waking a receiver changes what a helper does, so it stays
    off (and every recorded answer stays valid) until the deployment sets it,
    exactly like ``MIMI_COMPUTER_TOOLS`` and ``MIMI_SKILLS``.
    """
    raw = (env if env is not None else os.environ).get(ENV_VAR, "0").strip().lower()
    return raw in ("1", "on", "yes", "true")


def receiver_thread(tenant_id: str, helper: str) -> str:
    """The receiver's own thread (``thread_kind`` reads the ``helper`` head)."""
    return f"{tenant_id}:helper:{helper}"


def skill_for(helper: str) -> str:
    key = DELEGATE_SKILLS.get(helper, "")
    return key if key in BY_KEY else ""


def sender_chain(conn: sa.Connection, tenant_id: str, turn_id: str,
                 sender_helper: str) -> list[str]:
    """The sender helper's own chain, root first, sender last.

    A sender that was itself handed work carries that item's chain (its run
    was started with ``lineage = chain[:-1]``), so the depth and ping-pong
    guards hold across hops."""
    row = conn.execute(sa.select(s.helper_tasks.c.chain).where(
        s.helper_tasks.c.tenant_id == tenant_id,
        s.helper_tasks.c.turn_id == turn_id)).first()
    if row is not None:
        chain = store_util.loads(row.chain, []) or []
        if chain:
            return [str(x) for x in chain]
    return [sender_helper] if sender_helper else []


def refuse(conn: sa.Connection, tenant_id: str, *, source_key: str, from_helper: str,
           to_helper: str, shop_id: str, ask: str, chain: list[str], reason: str,
           now: float) -> str:
    """Write a ``refused`` item: the handoff is recorded, never run."""
    return ib.record(conn, tenant_id, source_key=source_key, from_helper=from_helper,
                     to_helper=to_helper, skill="", shop_id=shop_id, thread_id="",
                     ask=ask, chain=chain, now=now, state="refused", reason=reason)


def note(conn: sa.Connection, tenant_id: str, *, source_key: str, from_helper: str,
         to_helper: str, shop_id: str, ask: str, turn_id: str,
         now: float, outcome: str = "applied") -> dict[str, Any] | None:
    """Record one handoff from an applied intent. Returns the wake spec, or None
    when nothing is recorded (a shadowed intent, or a refused handoff). Never
    runs anything: the caller wakes after its own transaction commits
    (``wake``).

    A shadowed intent writes nothing at all: shadow mode records intents and
    applies none, so a shadowed assignment never appears as delivered work."""
    if outcome != "applied":
        return None
    chain = sender_chain(conn, tenant_id, turn_id, from_helper)
    if not ib.assignable(to_helper):
        refuse(conn, tenant_id, source_key=source_key, from_helper=from_helper,
               to_helper=to_helper, shop_id=shop_id, ask=ask, chain=chain,
               reason=f"There is no {ib.title(to_helper) or to_helper} to hand this to.",
               now=now)
        return None
    if to_helper in chain:
        refuse(conn, tenant_id, source_key=source_key, from_helper=from_helper,
               to_helper=to_helper, shop_id=shop_id, ask=ask, chain=chain,
               reason=f"{ib.title(to_helper)} is already working on this; handing it "
                      f"back would loop.",
               now=now)
        return None
    if len(chain) > ib.MAX_DEPTH:
        refuse(conn, tenant_id, source_key=source_key, from_helper=from_helper,
               to_helper=to_helper, shop_id=shop_id, ask=ask, chain=chain,
               reason=f"This is already {len(chain) - 1} handoffs deep (the limit is "
                      f"{ib.MAX_DEPTH}).",
               now=now)
        return None
    skill = skill_for(to_helper)
    if not skill:
        refuse(conn, tenant_id, source_key=source_key, from_helper=from_helper,
               to_helper=to_helper, shop_id=shop_id, ask=ask, chain=chain,
               reason=f"{ib.title(to_helper)} has no work this can run yet.", now=now)
        return None
    task_id = ib.record(conn, tenant_id, source_key=source_key, from_helper=from_helper,
                        to_helper=to_helper, skill=skill, shop_id=shop_id,
                        thread_id=receiver_thread(tenant_id, to_helper), ask=ask,
                        chain=[*chain, to_helper], now=now)
    return {"task_id": task_id, "source_key": source_key, "from_helper": from_helper,
            "to_helper": to_helper, "skill": skill, "shop_id": shop_id, "ask": ask,
            "chain": [*chain, to_helper]}


def wake(agent: Any, tenant_id: str, spec: dict[str, Any], now: float) -> str:
    """Wake the receiver: one ``helper_finished`` turn on its own thread.

    The front door routes it by rule to the receiver's skill (no model call),
    and the child's own ``helper_finished`` result is written back by the
    receiver's turn. Returns the child turn id ("" when it could not be made)."""
    from mimi.clock import wall_utc
    from mimi.harness.inbound import Inbound, Payload

    if not ib.assignable(spec["to_helper"]):
        return ""
    inbound = Inbound(
        source="helper_finished", org_id=tenant_id,
        external_id=f"delegate:{spec['task_id']}",
        received_at=wall_utc(),
        thread_id=receiver_thread(tenant_id, spec["to_helper"]),
        payload=Payload(text=str(spec["ask"]), name=f"delegated:{spec['from_helper']}",
                        for_helper=str(spec["to_helper"]), for_skill=str(spec["skill"]),
                        shop_id=str(spec["shop_id"])))
    view = agent.submit(tenant_id, inbound)
    turn_id = view.turn_id
    with agent.engine.begin() as conn:
        ib.set_state(conn, tenant_id, spec["task_id"], "working", turn_id=turn_id, now=now)
    return turn_id


def is_delegate_tool(tool: str) -> bool:
    return tool in ib.DELEGATE_TOOLS

"""Helper inboxes: one record per piece of work one helper hands to another.

The record is the data; the console renders it as a dated line in the
receiving helper's conversation (docs/HELPER-INBOX-CONTRACT.md). Everything a
person reads here is written by code: the ack, the outcome sentence and the
result, never by a model in this module.

The switch ``MIMI_HELPER_DELEGATION`` gates the *wake* and the widened
``delegate_task`` tool, not the record: an applied ``ask_helper`` still writes
a ``queued`` item with the switch off, so the inbox read is honest either way.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

import sqlalchemy as sa

from mimi.harness import handoff as hx
from mimi.harness.skills import HELPER_TITLE, HELPERS
from mimi.harness.style import clean_reply
from mimi.store import schema as s
from mimi.store import util as store_util

CONTRACT_VERSION = "helper-inbox-2026-11-04.1"

State = Literal["queued", "working", "done", "handed_back", "failed", "refused"]
STATES: tuple[State, ...] = ("queued", "working", "done", "handed_back", "failed", "refused")
OPEN_STATES: frozenset[str] = frozenset({"queued", "working"})
TERMINAL_STATES: frozenset[str] = frozenset({"done", "handed_back", "failed", "refused"})

# A handoff to one of these is refused by the depth/cycle guard, in code.
MAX_DEPTH = 2

# The tools that create an inbox item. ``ask_helper`` is Collector's (always
# available); ``delegate_task`` is every helper's (only with the switch on).
DELEGATE_TOOLS: frozenset[str] = frozenset({"ask_helper", "delegate_task"})


def title(helper: str) -> str:
    return HELPER_TITLE.get(helper, helper.title() if helper else "")


def assignable(helper: str) -> bool:
    """A helper another helper may hand work to."""
    return helper in HELPERS


def task_id_for(tenant_id: str, source_key: str) -> str:
    """One item per handoff: the intent's own key, hashed."""
    raw = f"{tenant_id}\x00{source_key}".encode()
    return "itk_" + hashlib.sha256(raw).hexdigest()[:20]


def outcome(state: str, to_title: str, *, result: str = "", reason: str = "") -> str:
    """The one short sentence the console shows. Written by code, never empty."""
    if state == "working":
        return clean_reply(f"{to_title} is working on it")
    if state == "done":
        first = (result or "").strip().split("\n", 1)[0].strip()
        if first:
            return clean_reply(first)
        return clean_reply(f"{to_title} finished")
    if state == "handed_back":
        return clean_reply(f"{to_title} handed it back")
    if state == "failed":
        return clean_reply(f"{to_title} could not finish it")
    if state == "refused":
        return clean_reply(reason or f"{to_title} was not asked")
    return clean_reply(f"Waiting for {to_title}")


def record(conn: sa.Connection, tenant_id: str, *, source_key: str, from_helper: str,
           to_helper: str, skill: str, shop_id: str, thread_id: str, ask: str,
           chain: list[str], now: float, state: str = "queued",
           reason: str = "") -> str:
    """Write one inbox item. Idempotent on the handoff's own key.

    ``now`` is epoch seconds (``mimi.clock.wall()``), as every store stamp is."""
    task_id = task_id_for(tenant_id, source_key)
    store_util.insert_ignore(conn, s.helper_tasks, {
        "tenant_id": tenant_id, "task_id": task_id, "source_key": source_key,
        "parent_task_id": "", "from_helper": from_helper, "to_helper": to_helper,
        "skill": skill, "shop_id": shop_id, "thread_id": thread_id, "turn_id": "",
        "ask": ask, "state": state,
        "reason": reason or hx.ack(to_helper, from_helper=from_helper),
        "outcome": outcome(state, title(to_helper), reason=reason),
        "result": "", "chain": store_util.dumps(chain),
        "created_at": store_util.now_iso(now), "created_ts": now,
        "finished_at": ""})
    return task_id


def set_state(conn: sa.Connection, tenant_id: str, task_id: str, state: str, *,
              turn_id: str = "", result: str = "", now: float,
              reason: str = "") -> None:
    """Move one item's state and rewrite its outcome sentence."""
    row = conn.execute(sa.select(s.helper_tasks.c.to_helper, s.helper_tasks.c.reason)
                       .where(s.helper_tasks.c.tenant_id == tenant_id,
                              s.helper_tasks.c.task_id == task_id)).first()
    to_helper = str(row.to_helper) if row is not None else ""
    why = reason or (str(row.reason) if row is not None else "")
    values: dict[str, Any] = {
        "state": state, "outcome": outcome(state, title(to_helper), result=result,
                                           reason=why)}
    if turn_id:
        values["turn_id"] = turn_id
    if result:
        values["result"] = result
    if state in TERMINAL_STATES:
        values["finished_at"] = store_util.now_iso(now)
    conn.execute(s.helper_tasks.update().where(
        s.helper_tasks.c.tenant_id == tenant_id,
        s.helper_tasks.c.task_id == task_id).values(**values))


def set_state_by_key(conn: sa.Connection, tenant_id: str, source_key: str, state: str, *,
                     turn_id: str = "", result: str = "", now: float,
                     reason: str = "") -> str:
    """Same, keyed by the handoff's source key (the intent key)."""
    task_id = task_id_for(tenant_id, source_key)
    set_state(conn, tenant_id, task_id, state, turn_id=turn_id, result=result, now=now,
              reason=reason)
    return task_id

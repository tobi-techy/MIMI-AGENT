"""Loop guards: the limits that keep one run, one job tree and one shop thread
from running away (docs/LOOP-SAFETY.md).

Budgets (steps, seconds, naira) bound how much a run may do. These bound
*how* it may spend it:

* **repeat**: the same tool with the same arguments, a fourth time in one run,
  or a call that already failed twice, a third time. The run stops before it
  is dispatched; no model call follows.
* **no progress**: four tool-calling steps in a row with no new successful
  tool result and no new proposal.
* **tool errors**: five failed tool calls in one run (a flapping connector).
* **delegation**: ``ask_helper`` may not hand work back to a helper already in
  the job's chain (A to B to A) or past depth two.
* **shop messages**: three messages to one shop in a run, six in a rolling
  hour (with what Go already sent), so Mimi never spams a shop.
* **follow-ups**: one ``set_followup`` per shop per run, three per shop per
  Lagos day.

A trip ends the run ``stopped_runaway`` with an owner note written by code;
what the run already proposed is kept (as for a budget stop), nothing more is
dispatched, and only an owner grant reopens it.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LoopLimits:
    max_identical_calls: int = 3
    max_identical_failures: int = 2
    max_no_progress_steps: int = 4
    max_tool_errors: int = 5
    max_delegation_depth: int = 2
    max_shop_messages_per_run: int = 3
    max_shop_messages_per_hour: int = 6
    max_followups_per_shop_run: int = 1
    max_followups_per_shop_day: int = 3


LIMITS = LoopLimits()

# Tools whose output is a message a shop reads, and how to tell.
DELEGATING_TOOLS = frozenset({"ask_helper"})
FOLLOWUP_TOOLS = frozenset({"set_followup"})
_SHOP_MESSAGE_TOOLS = frozenset({"send_reminder", "send_receipt"})

# Event types that mean a dispatched call did not do what it was asked.
_FAILED = frozenset({"tool_refused", "repair_requested", "model_output_invalid"})


def call_sig(name: str, raw_args: str) -> str:
    """Tool plus canonical arguments: the same call, however it is spelled."""
    try:
        args: Any = json.loads(raw_args or "{}")
    except ValueError:
        args = (raw_args or "").strip()
    canon = json.dumps([name, args], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()[:20]


def call_failed(events: Any) -> bool:
    for e in events:
        if e.type in _FAILED:
            return True
        if e.type == "tool_result" and isinstance(e.data, dict) and e.data.get("error"):
            return True
    return False


def result_progress(content: str) -> str:
    """A hash for a tool result that moved the run on, or ``""`` for one that
    did not (an error, a denial, an already-proposed repeat, a schema)."""
    try:
        body = json.loads(content or "")
    except ValueError:
        return ""
    if not isinstance(body, dict) or body.get("ok") is not True:
        return ""
    if body.get("already_proposed"):
        return ""
    return hashlib.sha256(content.encode()).hexdigest()[:20]


def to_shop(name: str, args: dict[str, Any]) -> str:
    """The shop a message tool writes to, or ``""``."""
    shop = str(args.get("shop_id") or "")
    if not shop:
        return ""
    if name == "send_to_user":
        return shop if args.get("channel") == "shop_whatsapp" else ""
    if name == "ask_question":
        return shop if args.get("to") == "shop" else ""
    return shop if name in _SHOP_MESSAGE_TOOLS else ""


def intent_to_shop(tool: str, payload: dict[str, Any]) -> str:
    return to_shop(tool, payload)


def runaway_note(helper: str, guard: str, detail: str, progress: str) -> str:
    """The owner note for a tripped guard. Figures and words from code."""
    why = {
        "repeat": f"it tried the same step ({detail}) again and again",
        "repeat_failure": f"the same step ({detail}) kept failing",
        "no_progress": f"{detail} steps in a row brought nothing new",
        "tool_errors": f"{detail} tool calls failed in this job",
    }.get(guard, detail)
    return (f"I stopped {helper} on this job because {why}. Nothing more was sent or "
            f"spent after that. {progress} Reply keep going if you want me to try again.")


def record_trip(kind: str, **data: Any) -> None:
    """Append one guard or gate trip to ``$MIMI_GUARD_TRIPS`` (JSON lines), when
    set: how the evals count false blocks on legitimate replies. Off by default."""
    path = os.environ.get("MIMI_GUARD_TRIPS", "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, **data}, sort_keys=True, default=str) + "\n")
    except OSError:
        pass

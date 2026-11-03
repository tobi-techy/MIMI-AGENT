"""The denial circuit breaker (RAI-324): a stuck helper that keeps getting
told no stops trying and asks the owner.

A denial is any refusal on a consequential path: a claims-gate hold, a policy
``refuse`` (a built-in stop, a ``never`` tool, or a rule the store cannot
read), a consent or Go refusal, or a tool that errors instead of answering.
Three denials in a row in one turn, or 10 denials in the last 50 reviews of
a thread, trip the breaker. The turn parks with a plain owner message that
names the helper and how many times it was told no.

The counter never breaks a turn: when its store cannot be read it fails
closed (a consequential step is treated as denied). Env knobs
(``MIMI_DENIALS_*``): ``IN_A_ROW`` (3), ``WINDOW`` (50),
``MAX_IN_WINDOW`` (10).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default)).strip() or default))
    except (ValueError, TypeError, AttributeError):
        return default


def in_a_row_limit() -> int:
    """Consecutive denials in one turn that trip the breaker."""
    return _env_int("MIMI_DENIALS_IN_A_ROW", 3)


def window() -> int:
    """How many recent thread reviews the thread rule looks at."""
    return _env_int("MIMI_DENIALS_WINDOW", 50)


def max_in_window() -> int:
    """Denials inside the window that trip the breaker for a thread."""
    return _env_int("MIMI_DENIALS_MAX_IN_WINDOW", 10)


# Event kinds the loop emits for a refusal. A helper that gets told no and
# tries a different tool for the same goal is a retry, so a new denied tool
# never resets the streak: only a step that moves the run on does
# (``guards.result_progress`` on the step's tool messages).
DENIAL_EVENT_TYPES = frozenset({"tool_refused", "claim_blocked"})
# ``tool_refused`` reasons that are not a no to the step's goal: a messaging
# or follow-up rate cap (still a refusal to that call), a connector outage,
# or a tool the helper never had. Still logged, never counted.
NON_DENIAL_REASONS = frozenset(
    {"shop_rate_limit", "followup_dedupe", "followup_cap", "not_allowed",
     "connector_down", "connector_down_no_scrape"})


def denial_kind(ev: Any) -> str:
    """The refusal behind a denial event: consent, policy, go, claim, error."""
    if getattr(ev, "type", "") == "claim_blocked":
        return "claim"
    data = getattr(ev, "data", None) or {}
    reason = str(data.get("reason") or "")
    return {"consent": "consent", "go_refused": "go", "never": "policy"}.get(reason, reason)


def is_denial_event(ev: Any) -> bool:
    """Whether one loop event counts as the breaker being told no."""
    if getattr(ev, "type", "") not in DENIAL_EVENT_TYPES:
        return False
    if getattr(ev, "type", "") == "tool_refused":
        reason = str((getattr(ev, "data", None) or {}).get("reason") or "")
        if reason in NON_DENIAL_REASONS:
            return False
    return True


def denial_reason(ev: Any) -> str:
    """The short human line for a denial event (figures never leave code)."""
    short = str(getattr(ev, "short", "") or "")
    return short[:120] or str(getattr(ev, "type", ""))


@dataclass
class DenialCounter:
    """The per-turn streak plus the thread's recent reviews.

    Both live on the loop state (in-run streak) and on the thread store
    (recent reviews, written by the API when the turn finishes); this record
    is the pure rule that reads them. A store that cannot be read fails
    closed: ``unknown`` trips for consequential steps.
    """

    in_a_row: int = 0
    window_denials: int = 0
    window_size: int = 0
    unknown: bool = False

    def tripped(self) -> bool:
        if self.unknown:
            return True
        if self.in_a_row >= in_a_row_limit():
            return True
        sized = self.window_size or window()
        return self.window_denials >= max_in_window() and sized >= 1


def streak_of(events: Any) -> int:
    """Consecutive denial events at the end of this turn's event list."""
    streak = 0
    for ev in reversed(list(events or [])):
        if is_denial_event(ev):
            streak += 1
        elif getattr(ev, "type", "") in ("tool_result", "intent_proposed",
                                         "draft_proposed", "policy_decided"):
            break
        elif getattr(ev, "type", "") in ("step_finished", "step_started",
                                         "model_called", "run_started"):
            continue
        else:
            break
    return streak


def count_window(reviews: Any) -> tuple[int, int]:
    """(denials, looked at) over the thread's recent reviews, oldest first."""
    rows = list(reviews or [])[-window():]
    return sum(1 for r in rows if bool(r)), len(rows)


def trip_note(helper: str, in_a_row: int, window_denials: int) -> str:
    """The plain owner message for a tripped breaker. No em dashes."""
    from mimi.harness.skills import HELPER_TITLE

    who = HELPER_TITLE.get(helper, helper.title() if helper else "Mimi")
    if in_a_row >= in_a_row_limit():
        return (f"I stopped {who} on this job. My checks said no {in_a_row} times "
                "in a row, so I need you to look at it. Nothing more was sent.")
    return (f"I stopped {who} on this job. My checks said no {window_denials} times "
            "recently on this thread, so I need you to look at it. "
            "Nothing more was sent.")


@dataclass
class BreakerStore:
    """The thread's recent reviews. The API writes one row per finished turn;
    the loop reads the tail before a step."""

    rows: dict[str, list[bool]] = field(default_factory=dict)

    def reviews(self, thread_id: str) -> list[bool]:
        try:
            return list(self.rows.get(thread_id, []))
        except Exception as exc:
            raise OSError("denial reviews unreadable") from exc

    def record(self, thread_id: str, denied: bool) -> None:
        self.rows.setdefault(thread_id, []).append(bool(denied))
        self.rows[thread_id] = self.rows[thread_id][-window():]

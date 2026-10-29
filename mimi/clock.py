"""The wall clock: the one seam every stored time stamp reads.

The store, the queue and the API agent stamp rows (``created_at``,
``created_ts``, leases, message ``ts``) and compare them (lease lapse,
look-back windows, recent tasks) on this clock, never on ``time.time``
directly. In production it *is* ``time.time``, read at every call.

An offline eval that replays recorded model answers can pin it to the day
the answers were recorded (``pinned``): a stamp that reaches a model (a
reused payment reference names the earlier proof's day) then stays the
same on every later day, and stamps, leases and windows still agree,
because they all read this one clock and keep real elapsed time.

This module never reads the environment: only code that calls ``pinned``
or ``set_source`` changes the clock, and ``mimi.api.config.Settings.check``
refuses to start a production API whose clock is pinned.

The agent's *simulated* "now" (what Mimi is told the time is) is a separate,
per-agent clock (``Agent(clock=...)``); this one is for the store's stamps.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

Source = Callable[[], float]

_lock = threading.Lock()
_source: Source | None = None  # None: the real clock, time.time


def wall() -> float:
    """Seconds since the epoch (UTC), like ``time.time()``."""
    src = _source
    return time.time() if src is None else src()


def wall_utc() -> datetime:
    """``wall()`` as an aware UTC datetime."""
    return datetime.fromtimestamp(wall(), UTC)


def is_pinned() -> bool:
    return _source is not None


def set_source(source: Source | None) -> Source | None:
    """Replace the clock for this process (``None``: the real one). Returns
    the previous source so a caller can put it back."""
    global _source
    with _lock:
        prev, _source = _source, source
    return prev


def shifted(day_start: datetime, run_start: float) -> Source:
    """A clock that reads ``day_start`` at real time ``run_start`` and then
    ticks in real time (elapsed seconds kept, only the day fixed)."""
    if day_start.tzinfo is None:
        raise ValueError("day_start needs a time zone")
    offset = day_start.timestamp() - run_start

    def source() -> float:
        return time.time() + offset

    return source


@contextmanager
def pinned(day_start: datetime, run_start: float | None = None) -> Iterator[float]:
    """Pin the clock for a block (``shifted``); the real clock comes back
    after, even on an error. Yields the run start (real seconds)."""
    start = time.time() if run_start is None else run_start
    prev = set_source(shifted(day_start, start))
    try:
        yield start
    finally:
        set_source(prev)

"""The e2e run's wall clock: pinned to the day its answers were recorded.

The agent's clock is the simulated day (``worker.SIM_NOW``), but the store
stamps rows on the wall clock (``mimi.clock``), and one of those stamps
reaches a model: a reused payment reference names the earlier proof with the
day its intent row was written (``mimi.proof.checks``: "reference already used
by another shop (prf_..., 2026-10-28)"). Replayed on a later day the request
changed, its recorded answer missed, and the shop-020 screenshot turn and the
credit reject turn after it were lost.

So the runner and every worker it starts (and restarts after a SIGKILL) pin
``mimi.clock`` to ``RECORDED_ON`` plus the real time since the run started.
The run start is handed to the workers in ``ENV`` (this module is the only
reader), so every process agrees: leases, windows and ordering keep their
real elapsed time and only the day is fixed. A live run records under the same
day, so a re-record stays replayable on any later day.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from mimi import clock

# The UTC day the eval/e2e caches were recorded (the RAI-318 re-record, 28 Oct
# 2026). Noon, so a run of up to twelve hours never crosses midnight.
RECORDED_ON = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
# The run start (real epoch seconds), from the runner to its workers.
ENV = "MIMI_E2E_RUN_START"


@contextmanager
def run_clock() -> Iterator[float]:
    """The runner: a new run's pinned clock for the block; workers started
    inside it inherit the run start. Clock and ``ENV`` are restored after."""
    saved = os.environ.get(ENV)
    with clock.pinned(RECORDED_ON) as start:
        os.environ[ENV] = repr(start)
        try:
            yield start
        finally:
            if saved is None:
                os.environ.pop(ENV, None)
            else:
                os.environ[ENV] = saved


def pin_worker() -> float:
    """A worker process: continue the runner's clock for the process's life
    (a worker started on its own begins a run of its own). Returns the run
    start."""
    raw = os.environ.get(ENV, "").strip()
    start = float(raw) if raw else time.time()
    clock.set_source(clock.shifted(RECORDED_ON, start))
    return start

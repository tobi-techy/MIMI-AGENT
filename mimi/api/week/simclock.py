"""The week's one clock, shared by the runner and every worker process.

The store, the queue and the agent all read ``mimi.clock.wall()``. In the
simulated week that clock is the *simulated* time: Monday 26 Oct 2026 07:30
WAT when the stock checks go out, Tuesday morning when the bank feed lands.
The runner moves it forward between waves (and only when the queue is idle);
between moves it ticks in real time, so leases, lease lapses and backoffs keep
their real elapsed time. Every process reads the same offset from one small
file, so a worker started after a SIGKILL agrees with the others at once.

One seam, the same one the e2e replay pins (``mimi.clock``); production
refuses to start with it set (``Settings.check``).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from mimi import clock


class FileClock:
    """``time.time() + offset``, the offset read from ``path`` when it changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stamp: tuple[int, int] = (-1, -1)
        self._offset = 0.0

    def offset(self) -> float:
        try:
            raw = os.stat(self.path)
            st = (raw.st_ino, raw.st_mtime_ns)  # a replace is a new inode
        except FileNotFoundError:
            return self._offset
        if st != self._stamp:
            try:
                self._offset = float(json.loads(self.path.read_text())["offset"])
                self._stamp = st
            except (ValueError, KeyError, OSError):
                pass  # a write in flight: keep the last offset
        return self._offset

    def __call__(self) -> float:
        return time.time() + self.offset()


def install(path: Path) -> FileClock:
    """Make this process's wall clock the shared simulated clock."""
    fc = FileClock(path)
    clock.set_source(fc)
    return fc


def set_to(path: Path, when: datetime, *, allow_back: bool = False) -> float:
    """Move the shared clock so it reads ``when`` now. Forward only, unless
    ``allow_back`` (a new soak loop, after the queue drained). Returns the
    offset written."""
    if when.tzinfo is None:
        raise ValueError("when needs a time zone")
    cur = FileClock(path).offset() if path.exists() else None
    off = when.timestamp() - time.time()
    if cur is not None and not allow_back:
        off = max(off, cur)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"offset": off, "reads": when.isoformat()}) + "\n")
    os.replace(tmp, path)
    return off

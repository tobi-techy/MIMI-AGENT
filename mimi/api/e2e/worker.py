"""An e2e worker process: the real ``Agent`` worker loop on recorded models.

    python -m mimi.api.e2e worker --db sqlite:///... [--live] [--pause-at TURN:STEP]

The agent is built exactly as ``Agent.from_env`` builds it, with two e2e
differences: every model is the recorded one (``models.py``) and the clock
is the simulated day's (the book's live day, 26 Oct 2026, late evening WAT),
so a replay sends byte-identical requests.

``--pause-at`` is the crash point for the worker-kill check: after the
checkpoint of step ``STEP`` of turn ``TURN`` the worker writes a marker file
and hangs, holding the lease, until the runner SIGKILLs it. A real crash at
a known step, so the replay kills at the same place as the live run.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mimi.api.agent import Agent
from mimi.api.e2e.models import CachePaths, agent_models
from mimi.observe.langfuse_export import exporter_from_env
from mimi.store.db import make_engine
from mimi.store.queue import Claim

LAGOS = ZoneInfo("Africa/Lagos")
# The simulated "now" for every turn of the run: the live day, after the last
# screenshot (22:39 WAT) so no message is ever in the agent's future.
SIM_NOW = datetime(2026, 10, 26, 23, 30, tzinfo=LAGOS)
LEASE_S = 3.0


class PausingAgent(Agent):
    """Hangs after one chosen checkpoint (the e2e crash point)."""

    pause_turn: str = ""
    pause_step: int = 0
    marker: Path | None = None
    # RAI-301: a turn the runner wants held while it sends a message mid-task.
    # ``hold-<turn_id>`` in this directory holds the turn after its first saved
    # loop state (lease kept alive) until the runner deletes the file.
    hold_dir: Path | None = None

    def _checkpoint(self, holder: list[Claim], ck: dict[str, Any]) -> None:
        super()._checkpoint(holder, ck)
        loop = ck.get("loop") or {}
        if self.hold_dir is not None and "loop" in ck and int(loop.get("step", 0)) == 0:
            hold = self.hold_dir / f"hold-{holder[0].turn_id}"
            if hold.exists():
                (self.hold_dir / f"held-{holder[0].turn_id}").write_text("held\n")
                end = time.monotonic() + 120
                while hold.exists() and time.monotonic() < end:
                    time.sleep(0.05)
        if (self.pause_turn and holder[0].turn_id == self.pause_turn
                and int(loop.get("step", 0)) >= self.pause_step):
            if self.marker is not None:
                self.marker.write_text(f"{holder[0].turn_id}:{loop.get('step')}\n")
            while True:  # hold the lease until the runner kills this process
                time.sleep(3600)


def build_agent(db_url: str, *, live: bool, paths: CachePaths, worker_id: str,
                pause: str = "", marker: Path | None = None,
                usage: Path | None = None) -> Agent:
    tool, router, deps = agent_models(live, paths, usage=usage)
    agent = PausingAgent(make_engine(db_url), models=[tool], router_model=router, deps=deps,
                         lease_s=LEASE_S, worker_id=worker_id, clock=lambda: SIM_NOW,
                         exporter=exporter_from_env(environment="e2e"))
    if usage is not None:
        agent.hold_dir = usage.parent
    if pause:
        turn, _, step = pause.rpartition(":")
        agent.pause_turn, agent.pause_step, agent.marker = turn, int(step or 1), marker
    return agent


def run(db_url: str, *, live: bool, cache_dir: Path, worker_id: str, pause: str = "",
        marker: str = "", stop_file: str = "", usage: str = "") -> int:
    logging.basicConfig(level=logging.WARNING)
    agent = build_agent(db_url, live=live, paths=CachePaths(cache_dir), worker_id=worker_id,
                        pause=pause, marker=Path(marker) if marker else None,
                        usage=Path(usage) if usage else None)
    stop = threading.Event()
    stopper = Path(stop_file) if stop_file else None
    while not stop.is_set():
        if stopper is not None and stopper.exists():
            break
        if agent.process() is None:
            time.sleep(0.05)
    return 0

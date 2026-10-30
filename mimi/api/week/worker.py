"""A week worker process: the real ``Agent`` worker loop on the shared sim clock.

    python -m mimi.api.week worker --db URL --workdir DIR [--live]

Offline (the default) the harness model is the ``Playbook`` and the helpers'
model ports are the flows' offline fakes, with SAM 3.1 replayed from the
recorded caches: nothing leaves the machine, ₦0. ``--live`` builds the agent
the way ``Agent.from_env`` does (needs ``META_API_KEY``); the runner only
starts live workers behind ``--live`` and a naira ceiling.

Forced kills reuse the e2e crash points (``mimi.api.e2e.worker``: ``route``,
``step:N``, ``absorb``, ``finish``, ``resume``). The kill plan file is
re-read when the runner changes it, so a soak can arm a kill at any time:
the next turn that reaches the point writes ``killpoint-<id>`` and hangs
until the runner SIGKILLs this process.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from mimi.api.agent import Agent
from mimi.api.e2e.worker import PausingAgent
from mimi.api.helpers import HelperDeps
from mimi.api.media import LocalMedia
from mimi.api.week.playbook import EVAL, Playbook
from mimi.api.week.simclock import install
from mimi.credit.sam import Box, CachedSegmenter, SamCacheMiss, Segmentation
from mimi.models.fake import FakeModel
from mimi.proof.reading import fake_reader
from mimi.receipts.verdict import fake_verdict
from mimi.stock.answers import fake_answer
from mimi.store.db import make_engine
from mimi.store.queue import Claim

LEASE_S = 3.0
SAM_CACHES = (EVAL / "e2e" / "sam.cache.json", EVAL / "credit" / "sam.cache.json")


class ReplaySam:
    """SAM 3.1 answers recorded by the credit eval and the e2e run, replayed."""

    name = "sam-3.1"

    def __init__(self) -> None:
        self.parts = [CachedSegmenter(None, p, mode="replay") for p in SAM_CACHES if p.exists()]

    def segment(self, image: bytes, mime: str, prompt: str) -> Segmentation:
        for c in self.parts:
            try:
                return c.segment(image, mime, prompt)
            except SamCacheMiss:
                continue
        # Not recorded: no packs found (the count says unusable; never a guess).
        return Segmentation(prompt, tuple[Box, ...](), self.name, 0.0, 1, cached=True)


def screenshot_truth() -> dict[str, dict[str, Any]]:
    rows = [json.loads(x) for x in (EVAL / "proof" / "screens.jsonl").read_text().splitlines()]
    return {r["sha256"]: r["truth"] for r in rows}


def offline_deps() -> HelperDeps:
    return HelperDeps(vision=FakeModel(fake_reader(screenshot_truth()), name="fake-vision"),
                      judge=FakeModel(fake_verdict, name="fake-judge"),
                      stock=FakeModel(fake_answer, name="fake-stock"),
                      segmenter=ReplaySam(), media=LocalMedia(EVAL))


class WeekAgent(PausingAgent):
    """The e2e crash points, with a kill plan that can change during the run."""

    plan_path: Path | None = None
    _plan_stamp: tuple[int, int] = (-1, -1)

    def _reload(self) -> None:
        if self.plan_path is None:
            return
        try:
            st = os.stat(self.plan_path)
        except FileNotFoundError:
            return
        stamp = (st.st_ino, st.st_mtime_ns)
        if stamp != self._plan_stamp:
            try:
                self.kill_plan = list(json.loads(self.plan_path.read_text()))
                self._plan_stamp = stamp
            except ValueError:
                pass

    def _kill_here(self, claim: Claim, where: str) -> None:
        self._reload()
        super()._kill_here(claim, where)


def build_agent(db_url: str, *, workdir: Path, worker_id: str, live: bool,
                price_as: str = "muse-spark-1.3") -> Agent:
    engine = make_engine(db_url)
    if live:
        from mimi.api.helpers import deps_from_env
        from mimi.harness.gateway import OpenAIToolModel
        from mimi.models.env import from_env

        key = os.environ.get("META_API_KEY", "").strip()
        if not key:
            raise SystemExit("--live needs META_API_KEY")
        tool: Any = OpenAIToolModel(api_key=key, model=os.environ.get("MIMI_MODEL", "").strip()
                                    or "muse-spark-1.3",
                                    base_url=os.environ.get("META_API_BASE", "").strip()
                                    or "https://api.meta.ai/v1", timeout_s=30.0)
        agent = WeekAgent(engine, models=[tool], router_model=from_env("mimi"),
                          deps=deps_from_env(), lease_s=LEASE_S, worker_id=worker_id)
    else:
        agent = WeekAgent(engine, models=[Playbook(price_as=price_as)], router_model=None,
                          deps=offline_deps(), lease_s=LEASE_S, worker_id=worker_id)
    agent.kill_dir = workdir
    agent.plan_path = workdir / "kill-plan.json"
    agent._reload()
    return agent


def run(db_url: str, *, workdir: Path, worker_id: str, live: bool, price_as: str) -> int:
    logging.basicConfig(level=logging.WARNING)
    install(workdir / "simclock.json")
    agent = build_agent(db_url, workdir=workdir, worker_id=worker_id, live=live,
                        price_as=price_as)
    stop = threading.Event()
    stopper = workdir / "stop"
    while not stop.is_set():
        if stopper.exists():
            break
        try:
            if agent.process() is None:
                time.sleep(0.05)
        except Exception as exc:  # a worker crash is a finding; log it and go on
            logging.getLogger("mimi.api.week").error("worker error: %r", exc)
            with open(workdir / "worker-errors.log", "a", encoding="utf-8") as f:
                f.write(f"{worker_id} {type(exc).__name__}: {exc}\n")
            time.sleep(0.2)
    return 0

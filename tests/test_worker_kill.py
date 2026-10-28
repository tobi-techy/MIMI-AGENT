"""Kill a worker mid-run, restart, and every turn completes exactly once (RAI-272).

A real worker process (``Agent.run_worker`` on the store, SQLite in WAL
mode) runs eight queued shop turns, each a three-step Collector run on a
slow scripted model (save the plan, read the balance, reply). The test
SIGKILLs it while a turn is mid-run, past at least one saved step, then
starts two fresh workers at once. Every turn must end ``done`` with
``completions == 1``, one reply, one plan intent, one history entry and one
copy of each step in the thread's log; the killed turn must be taken over
from its checkpoint.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.api.agent import Agent
from mimi.api.offline import OfflineModel
from mimi.harness.inbound import Inbound, Payload
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.world.loader import load_world

W = load_world()
ROOT = Path(__file__).resolve().parents[1]
N = 8

WORKER = r'''
import re, sys, threading, time
from mimi.api.agent import Agent
from mimi.harness.gateway import ScriptedModel, say
from mimi.store import make_engine

SHOP = re.compile(r'shop_id="([^"]+)"')

def script(messages, tools):
    time.sleep(0.3)  # a slow model: the kill lands mid-run
    start = max(i for i, m in enumerate(messages)
                if m["role"] == "user" and m["content"].startswith("Now: "))
    shop = SHOP.findall(messages[start]["content"])[-1]
    k = sum(1 for m in messages[start:] if m["role"] == "assistant")
    if k == 0:
        return say("", ("update_plan", {"plan": [f"answer {shop}"], "note": shop}))
    if k == 1:
        return say("", ("get_balance", {"shop_id": shop}))
    return say("", ("send_to_user", {"channel": "shop_whatsapp", "shop_id": shop,
                                      "kind": "reply", "body": f"Thanks {shop}, noted.",
                                      "ends_turn": True}))

agent = Agent(make_engine(sys.argv[1]), models=[ScriptedModel(script)], lease_s=1.5,
              worker_id=sys.argv[2])
agent.run_worker(threading.Event(), idle_s=0.05)
'''


def _spawn(script: Path, url: str, name: str) -> subprocess.Popen[bytes]:
    env = {**os.environ, "META_API_KEY": ""}
    return subprocess.Popen([sys.executable, str(script), url, name], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _rows(engine: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(s.turns).where(s.turns.c.tenant_id == W.tenant_id)))


def _wait(pred: Any, timeout: float, what: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL")
def test_a_killed_worker_loses_nothing_and_doubles_nothing(tmp_path: Path,
                                                            store_url: str) -> None:
    url = store_url
    engine = make_engine(url)
    ensure_schema(engine)
    seed_world(engine, W)
    intake = Agent(engine, models=[OfflineModel()])
    ids = []
    for i in range(N):
        inbound = Inbound(source="shop_message", org_id=W.tenant_id, external_id=f"wamid.k{i}",
                          received_at=datetime.now(UTC),
                          payload=Payload(text="abeg wetin I owe now?",
                                          from_phone=W.shops[i].phone))
        ids.append(intake.submit(W.tenant_id, inbound).turn_id)
    script = tmp_path / "worker.py"
    script.write_text(WORKER, encoding="utf-8")

    a = _spawn(script, url, "worker-a")

    def mid_run() -> list[Any]:
        rows = _rows(engine)
        busy = [r for r in rows if r.state == "leased" and
                json.loads(r.checkpoint).get("loop", {}).get("step", 0) >= 1]
        done = [r for r in rows if r.state == "done"]
        return busy if busy and done else []

    try:
        busy = _wait(mid_run, 60, "worker A to be mid-run on a turn")
    finally:
        a.send_signal(signal.SIGKILL)
        a.wait(10)
    killed = {r.turn_id: json.loads(r.checkpoint)["loop"]["step"] for r in busy}
    done_before = {r.turn_id for r in _rows(engine) if r.state == "done"}
    assert killed and len(done_before) < N

    b, c = _spawn(script, url, "worker-b"), _spawn(script, url, "worker-c")
    try:
        _wait(lambda: all(r.state == "done" for r in _rows(engine)), 90,
              "workers B and C to finish every turn")
    finally:
        for p in (b, c):
            p.terminate()
            p.wait(10)

    rows = {r.turn_id: r for r in _rows(engine)}
    assert sorted(rows) == sorted(ids)
    with engine.connect() as conn:
        outs = conn.execute(sa.select(s.messages.c.turn_id, s.messages.c.text).where(
            s.messages.c.direction == "out")).all()
        plans = conn.execute(sa.select(s.intents.c.turn_id).where(
            s.intents.c.tool == "update_plan")).scalars().all()
        hist = conn.execute(sa.select(s.history.c.turn_id)).scalars().all()
        ev = conn.execute(sa.select(s.thread_events.c.turn_id, s.thread_events.c.type,
                                    s.thread_events.c.seq).where(
            s.thread_events.c.type.in_(("step_finished", "run_finished")))).all()
        attempts = conn.execute(sa.select(s.turn_attempts)).all()
    for tid, r in rows.items():
        assert r.state == "done" and r.completions == 1 and r.status == "done", tid
        assert [o.text for o in outs if o.turn_id == tid] == [
            f"Thanks {r.thread_id.rsplit(':', 1)[1]}, noted."], tid
        assert plans.count(tid) == 1 and hist.count(tid) == 1, tid
        steps = [e for e in ev if e.turn_id == tid and e.type == "step_finished"]
        assert len(steps) == 3 and len({e.seq for e in steps}) == 3, tid
        assert sum(1 for e in ev if e.turn_id == tid and e.type == "run_finished") == 1, tid
    assert len(outs) == N
    for tid in killed:
        mine = [x for x in attempts if x.turn_id == tid]
        assert mine[0].worker_id == "worker-a" and rows[tid].attempts >= 2
        assert any(x.takeover and x.worker_id in ("worker-b", "worker-c") for x in mine)
    # Turns A had finished were never run again.
    for tid in done_before:
        assert {x.worker_id for x in attempts if x.turn_id == tid} == {"worker-a"}

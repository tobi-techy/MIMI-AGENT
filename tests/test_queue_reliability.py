"""Queue reliability (Phase 0): no work is lost, and no side effect is doubled.

Four unhappy paths live here, each against the real ``turns`` table:

* the worker holding a turn is killed mid-job (its lease lapses, another
  worker takes over, the dead holder can never write again);
* a poison turn parks once, with one owner note, and stops spinning;
* a parked turn comes back with one `queue replay` and completes once;
* two workers race the same job and only one claim wins;
* a burst above the claim rate is claimed exactly once, and one tenant cannot
  take every worker.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.harness.inbound import Inbound, Payload
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.store.queue import MAX_CLAIMS, LeaseLost, NotDeadLettered, TurnQueue
from mimi.world.loader import load_world

W = load_world()


@pytest.fixture()
def engine(store_url: str) -> Any:
    e = make_engine(store_url)
    ensure_schema(e)
    seed_world(e, W)
    return e


def _enqueue(engine: Any, ext: str, shop: int = 0) -> str:
    from mimi.api.agent import Agent
    from mimi.api.offline import OfflineModel

    agent = Agent(engine, models=[OfflineModel()])
    inbound = Inbound(source="shop_message", org_id=W.tenant_id, external_id=ext,
                      received_at=datetime.now(UTC),
                      payload=Payload(text="hi", from_phone=W.shops[shop].phone))
    return agent.submit(W.tenant_id, inbound).turn_id


def _finish(q: Any, engine: Any, claim: Any) -> None:
    with engine.begin() as conn:
        q.finish_in(conn, claim, status="done", helper="", skill="", run_id="r",
                    result={"ok": True})


def _notes(engine: Any, turn_id: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.select(sa.func.count()).select_from(s.messages).where(
            s.messages.c.idempotency_key == f"poison:{turn_id}")).scalar() or 0)


def test_a_worker_killed_mid_job_is_taken_over_and_finishes_once(engine: Any) -> None:
    now = [1_000_000.0]
    q = TurnQueue(engine, lease_s=10, clock=lambda: now[0])
    tid = _enqueue(engine, "killed")
    dead = q.claim("w-dead")
    assert dead is not None and tid == dead.turn_id

    now[0] += 11  # the worker was killed: no heartbeat, the lease lapses
    alive = q.claim("w-alive")
    assert alive is not None and alive.turn_id == tid
    assert alive.takeover and alive.attempts == 2

    with pytest.raises(LeaseLost):
        q.checkpoint(dead, {"step": 1})  # a zombie cannot write
    with engine.begin() as conn, pytest.raises(LeaseLost):
        q.finish_in(conn, dead, status="done", helper="", skill="", run_id="r", result={})

    alive = q.checkpoint(alive, {"step": 1})
    _finish(q, engine, alive)
    row = q.get(tid)
    assert row.state == "done" and row.completions == 1 and not row.error
    assert q.claim("w-3") is None


def test_a_poison_job_parks_once_with_one_note_and_no_spin(engine: Any) -> None:
    now = [1_000_000.0]
    events: list[tuple[str, dict[str, Any]]] = []
    q = TurnQueue(engine, lease_s=10, clock=lambda: now[0],
                  on_event=lambda name, fields: events.append((name, fields)))
    tid = _enqueue(engine, "poison")

    for i in range(MAX_CLAIMS):
        now[0] += 11
        c = q.claim(f"w{i}")
        assert c is not None and c.turn_id == tid
    now[0] += 11
    assert q.claim("w-last") is None  # this claim poisons instead of leasing

    row = q.get(tid)
    assert row.state == "failed" and row.status == "poisoned"
    assert row.attempts == MAX_CLAIMS and row.lease_owner is None
    assert row.error == f"poison: claimed {MAX_CLAIMS} times without finishing"
    assert _notes(engine, tid) == 1

    # No spinning: more polls do not claim it, bump attempts or add a note.
    for _ in range(3):
        now[0] += 11
        assert q.claim("w-poll") is None
    assert q.get(tid).attempts == MAX_CLAIMS and _notes(engine, tid) == 1
    parked = q.dead_letters(tenant_id=W.tenant_id)
    assert [r["turn_id"] for r in parked] == [tid]
    assert [name for name, _ in events].count("poison") == 1


def test_one_replay_queues_a_parked_turn_again_and_it_completes_once(engine: Any) -> None:
    now = [1_000_000.0]
    q = TurnQueue(engine, lease_s=10, clock=lambda: now[0])
    tid = _enqueue(engine, "replay")
    for i in range(MAX_CLAIMS):
        now[0] += 11
        assert q.claim(f"w{i}") is not None
    now[0] += 11
    assert q.claim("park") is None and q.get(tid).state == "failed"

    row = q.replay(tid)
    assert row == {"turn_id": tid, "tenant_id": W.tenant_id, "state": "queued", "attempts": 0}
    parked = q.get(tid)
    assert parked.state == "queued" and parked.lease_owner is None
    assert parked.lease_expires_ts is None and parked.attempts == 0
    assert _notes(engine, tid) == 1  # replay is not a second notification

    c = q.claim("w-new")
    assert c is not None and c.turn_id == tid and c.attempts == 1
    _finish(q, engine, c)
    assert q.get(tid).state == "done" and q.get(tid).completions == 1
    with pytest.raises(NotDeadLettered):
        q.replay(tid)  # done work cannot be replayed
    with pytest.raises(NotDeadLettered):
        q.replay("turn-does-not-exist")


def test_two_workers_racing_one_job_claim_it_once(engine: Any) -> None:
    tid = _enqueue(engine, "race")
    q = TurnQueue(engine, lease_s=30)
    barrier = threading.Barrier(2)
    got: list[Any] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        barrier.wait()
        c = q.claim(name)
        if c is not None:
            with lock:
                got.append(c)

    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(got) == 1 and got[0].turn_id == tid
    with engine.connect() as conn:
        tried = conn.execute(sa.select(sa.func.count()).select_from(s.turn_attempts).where(
            s.turn_attempts.c.turn_id == tid)).scalar()
    assert int(tried or 0) == 1


def test_a_burst_above_the_claim_rate_is_claimed_once_each(engine: Any) -> None:
    shops = len(W.shops)
    ids = [_enqueue(engine, f"burst{i}", shop=i % shops) for i in range(12)]
    q = TurnQueue(engine, lease_s=30)
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        while True:
            c = q.claim(name)
            if c is None:
                return
            with lock:
                claimed.append(c.turn_id)
            _finish(q, engine, c)  # free the thread for its next turn

    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(claimed) == sorted(ids)
    assert len(set(claimed)) == len(ids)  # nothing claimed twice, nothing lost
    with engine.connect() as conn:
        done = conn.execute(sa.select(sa.func.count()).select_from(s.turns).where(
            s.turns.c.turn_id.in_(ids), s.turns.c.state == "done",
            s.turns.c.completions == 1)).scalar()
    assert int(done or 0) == len(ids)


def test_one_tenant_cannot_take_every_worker(engine: Any) -> None:
    q = TurnQueue(engine, lease_s=30, tenant_cap=1)
    first = _enqueue(engine, "cap1", shop=0)
    second = _enqueue(engine, "cap2", shop=1)
    c = q.claim("w1")
    assert c is not None and c.turn_id == first
    assert q.claim("w2") is None  # the only slot for this distributor is held
    assert q.stats()["inflight_by_tenant"] == {W.tenant_id: 1}
    _finish(q, engine, c)
    c2 = q.claim("w2")
    assert c2 is not None and c2.turn_id == second


def test_probe_and_stats_report_whether_the_queue_can_claim(engine: Any) -> None:
    q = TurnQueue(engine, lease_s=30)
    assert q.probe() is True
    assert q.stats()["depth"] == 0
    assert TurnQueue(sa.create_engine("sqlite:////nonexistent-dir/x.db")).probe() is False


def test_the_operator_replays_one_turn_from_the_command_line(tmp_path: Any) -> None:
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    url = f"sqlite:///{tmp_path / 'cli.db'}"
    e = make_engine(url)
    ensure_schema(e)
    stamp = datetime.now(UTC).isoformat()
    with e.begin() as conn:
        conn.execute(s.threads.insert().values(
            tenant_id="t1", thread_id="t1:shop:s1", kind="shop", shop_id="s1",
            last_at=stamp, last_ts=1_000_000.0, created_at=stamp))
        conn.execute(s.turns.insert().values(
            turn_id="turn-parked", tenant_id="t1", thread_id="t1:shop:s1",
            state="failed", status="poisoned", attempts=MAX_CLAIMS,
            error="poison: claimed 6 times without finishing",
            created_at=stamp, created_ts=1_000_000.0, batch="[]", finished_at=stamp))
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root)}

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "mimi.store", "queue", *args],
                              cwd=root, env=env, check=False, text=True, capture_output=True)

    listed = cli("list", "--db", url)
    assert listed.returncode == 0 and "turn-parked" in listed.stdout
    replayed = cli("replay", "--id", "turn-parked", "--db", url)
    assert replayed.returncode == 0 and "replayed turn-parked" in replayed.stdout
    stats = cli("stats", "--json", "--db", url)
    assert stats.returncode == 0 and json.loads(stats.stdout)["queued"] == 1
    again = cli("replay", "--id", "turn-parked", "--db", url)
    assert again.returncode == 1 and "not parked" in again.stderr


"""The worker's health probe and its graceful shutdown (Phase 0).

`/healthz` must answer the only question that matters to a scheduler: can this
worker claim right now? A worker whose database is gone is not healthy even
though the process is alive. A worker told to stop is not ready at once, but
it finishes the turn it holds before it exits.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.api.worker_health import WorkerHealth, health_payload
from mimi.harness.inbound import Inbound, Payload
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.world.loader import load_world

W = load_world()


class StubQueue:
    def __init__(self, can_claim: bool = True) -> None:
        self.can_claim = can_claim

    def probe(self) -> bool:
        return self.can_claim

    def stats(self) -> dict[str, Any]:
        return {"depth": 3, "failed": 1}


class StubAgent:
    worker_id = "w-test"

    def __init__(self, can_claim: bool = True) -> None:
        self.queue = StubQueue(can_claim)


def test_health_is_the_queues_answer_not_just_the_process(tmp_path: Any) -> None:
    code, body = health_payload(StubAgent(True), draining=False)
    assert code == 200 and body["status"] == "ok" and body["can_claim"] is True
    assert body["queue_depth"] == 3 and body["dead_letters"] == 1
    code, body = health_payload(StubAgent(False), draining=False)
    assert code == 503 and body["can_claim"] is False and body["status"] == "degraded"
    code, body = health_payload(StubAgent(True), draining=True)
    assert code == 503 and body["draining"] is True


def test_health_endpoints_answer_over_http() -> None:
    agent, draining = StubAgent(True), threading.Event()
    health = WorkerHealth(agent, draining, host="127.0.0.1", port=0)
    try:
        port = health.start()
        assert port > 0
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/livez", timeout=5) as r:
            assert r.status == 200 and json.load(r)["status"] == "ok"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            assert r.status == 200 and json.load(r)["can_claim"] is True
        draining.set()
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5)
        assert err.value.code == 503
        assert json.load(err.value)["draining"] is True
    finally:
        health.stop()


@pytest.fixture()
def engine(store_url: str) -> Any:
    e = make_engine(store_url)
    ensure_schema(e)
    seed_world(e, W)
    return e


def _submit(agent: Any, ext: str, shop: int) -> str:
    inbound = Inbound(source="shop_message", org_id=W.tenant_id, external_id=ext,
                      received_at=datetime.now(UTC),
                      payload=Payload(text="hi", from_phone=W.shops[shop].phone))
    return agent.submit(W.tenant_id, inbound).turn_id


def test_sigterm_finishes_the_turn_in_hand_and_stops_claiming(engine: Any) -> None:
    from mimi.api.agent import Agent
    from mimi.api.offline import OfflineModel

    agent = Agent(engine, models=[OfflineModel()])
    done_id = _submit(agent, "drain1", shop=0)
    waiting_id = _submit(agent, "drain2", shop=1)

    started, release = threading.Event(), threading.Event()
    real = agent.process
    processed: list[str] = []

    def process(turn_id: str | None = None, worker_id: str = "") -> str:
        result = real(turn_id=turn_id, worker_id=worker_id)  # finish this turn
        processed.append(result)
        started.set()
        release.wait(10)  # the deploy's SIGTERM arrives while it finishes
        return result

    agent.process = process  # type: ignore[method-assign]
    stop = threading.Event()
    thread = threading.Thread(target=agent.run_worker, args=(stop,), kwargs={"idle_s": 0.01})
    thread.start()
    try:
        assert started.wait(60), "the worker never ran a turn"
        stop.set()  # graceful shutdown sets the flag, like SIGTERM does
        release.set()
        thread.join(30)
        assert not thread.is_alive(), "run_worker did not exit after stop"
    finally:
        release.set()
        stop.set()

    with engine.connect() as conn:
        states = {r.turn_id: (r.state, r.completions) for r in conn.execute(
            sa.select(s.turns.c.turn_id, s.turns.c.state, s.turns.c.completions).where(
                s.turns.c.turn_id.in_((done_id, waiting_id))))}
    ran = processed[0]
    assert states[ran][0] == "done" and states[ran][1] == 1
    other = waiting_id if ran == done_id else done_id
    assert states[other][0] == "queued"  # stopped claiming, did not start new work

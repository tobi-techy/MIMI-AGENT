"""The console's Activity view (RAI-325): per-task rows with steps, and
per-task Stop.

Offline: a scripted model and a SQLite file per test. A running turn shows in
the Running tab within one poll; stopping it ends the turn at its next step
boundary with no further tool call and no further message. A finished turn
reports itself, changed or not.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.harness.gateway import ScriptedModel
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from tests.test_api import KEYS, W_A, A, h, script, shop_turn

A_OWNER = {"Authorization": "Bearer a-owner-key-000000000001"}


@pytest.fixture()
def env(store_url: str) -> Iterator[dict[str, Any]]:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W_A)
    agent = Agent(engine, models=[ScriptedModel(script)], worker_id="test-worker")
    from tests.test_api import KEYS as _KEYS

    keys = parse_keys(f"{A}:owner:{_KEYS['a_owner']},{A}:channel:{_KEYS['a_chan']}")
    app = create_app(Settings(keys=keys, wait_s=5), agent, workers=0)
    with TestClient(app) as c:
        yield {"c": c, "agent": agent, "engine": engine}
    engine.dispose()


def owner() -> dict[str, str]:
    return A_OWNER


def rows(c: TestClient, **qs: Any) -> dict[str, Any]:
    r = c.get("/v1/activity", headers=owner(), params=qs)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- activity


def test_a_turn_shows_in_activity_with_plain_words_and_wat_time(env: dict[str, Any]) -> None:
    c = env["c"]
    r = shop_turn(c, "act.1", "abeg wetin I owe now?", key="a_chan")
    assert r.status_code == 200
    turn_id = r.json()["turn_id"]
    body = rows(c)
    assert body["total"] == 1 and body["by_tab"] == {"running": 0, "waiting": 0, "done": 1}
    [row] = body["rows"]
    assert row["turn_id"] == turn_id
    assert row["helper"] == "collector" and row["helper_name"] == "Collector"
    assert row["tab"] == "done" and row["status"] == "done"
    assert row["shop_name"] == W_A.shops[0].name
    assert "Collector is working on Balance question" in row["what"]
    assert "shop-001" not in row["what"]  # names, never ids
    started = datetime.fromisoformat(row["started_at"])
    assert started.utcoffset() is not None  # carries its zone
    assert row["started_at"].endswith("+01:00")  # Africa/Lagos (WAT)
    kinds = [st["kind"] for st in row["steps"]]
    assert "route" in kinds and "tool" in kinds and "model" in kinds
    for st in row["steps"]:
        assert set(st) == {"name", "kind", "status", "short", "ms", "at"}
        assert "shop-001" not in st["short"]
    # The console's contract, keyed on names the schema locks.
    assert row["finished_at"]


def test_tabs_filter_rows(env: dict[str, Any]) -> None:
    c = env["c"]
    shop_turn(c, "act.2", "abeg wetin I owe now?")
    assert rows(c, tab="done")["total"] == 1
    assert rows(c, tab="running") == {
        "rows": [],
        "total": 1,
        "by_tab": {"running": 0, "waiting": 0, "done": 1},
    }
    assert rows(c, tab="waiting")["rows"] == []


def test_activity_is_paginated(env: dict[str, Any]) -> None:
    c = env["c"]
    shop_turn(c, "act.3", "abeg wetin I owe now?")
    shop_turn(c, "act.4", "abeg wetin I owe now?")
    page = rows(c, limit=1)
    assert len(page["rows"]) == 1 and page["total"] == 2
    rest = rows(c, limit=1, offset=1)
    assert len(rest["rows"]) == 1
    assert rest["rows"][0]["turn_id"] != page["rows"][0]["turn_id"]


def test_activity_needs_the_owner_key(env: dict[str, Any]) -> None:
    c = env["c"]
    r = c.get("/v1/activity")
    assert r.status_code in (401, 403)
    r = c.get("/v1/activity", headers={"Authorization": "Bearer wrong-key"})
    assert r.status_code in (401, 403, 404)


# -------------------------------------------------------------------- stop


def test_stop_on_a_finished_turn_is_a_noop_with_a_clear_message(env: dict[str, Any]) -> None:
    c = env["c"]
    turn_id = shop_turn(c, "act.5", "abeg wetin I owe now?").json()["turn_id"]
    r = c.post(f"/v1/turns/{turn_id}/stop", headers=owner())
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "turn_id": turn_id,
        "state": "done",
        "status": "done",
        "stopped": False,
        "message": "Collector: already finished: stopping changes nothing.",
    }
    # Idempotent: asking again says the same.
    again = c.post(f"/v1/turns/{turn_id}/stop", headers=owner()).json()
    assert again == body


def test_stopping_a_running_turn_ends_it_with_nothing_more_sent(env: dict[str, Any]) -> None:
    c, engine = env["c"], env["engine"]
    agent = env["agent"]
    # Queue the turn without running it (wait=false), then stop it before the
    # worker picks it up: it must park at its first step boundary, running no
    # tool and sending no message.
    r = c.post(
        "/v1/turns",
        json={"message_id": "act.6", "phone": W_A.shops[0].phone, "text": "abeg wetin I owe now?"},
        headers=h("a_chan"),
        params={"wait": "false"},
    )
    assert r.status_code == 202, r.text
    turn_id = r.json()["turn_id"]
    stop = c.post(f"/v1/turns/{turn_id}/stop", headers=owner())
    assert stop.status_code == 200
    assert stop.json()["stopped"] is True
    assert stop.json()["status"] == "stopping"
    assert "sends nothing more" in stop.json()["message"]
    agent.process(turn_id)
    view = agent.view(A, turn_id)
    assert view.status == "stopped_by_owner"
    assert view.replies == []  # nothing went out after the stop
    with engine.connect() as conn:
        msgs = conn.execute(
            sa.select(s.messages.c.text).where(
                s.messages.c.tenant_id == A,
                s.messages.c.turn_id == turn_id,
                s.messages.c.direction == "out",
            )
        ).all()
        assert msgs == []
        n_tools = conn.execute(
            sa.select(sa.func.count())
            .select_from(s.thread_events)
            .where(
                s.thread_events.c.turn_id == turn_id,
                s.thread_events.c.type.in_(("tool_called", "tool_result")),
            )
        ).scalar()
        assert n_tools == 0  # the worker never ran a tool after the stop
        stop_evt = conn.execute(
            sa.select(s.thread_events.c.short).where(
                s.thread_events.c.tenant_id == A, s.thread_events.c.event_id == f"stop_{turn_id}"
            )
        ).scalar()
        assert stop_evt  # the stop itself is on the thread's steps
    # And it is not drawn as a successful step: a stop reads "held".
    [row] = [r for r in rows(c)["rows"] if r["turn_id"] == turn_id]
    held = [st for st in row["steps"] if st["status"] == "held"]
    assert len(held) == 1, row["steps"]
    assert "stopped" in held[0]["short"].lower()


def test_a_second_stop_after_the_worker_parked_it_says_so(env: dict[str, Any]) -> None:
    c = env["c"]
    agent = env["agent"]
    r = c.post(
        "/v1/turns",
        json={"message_id": "act.7", "phone": W_A.shops[0].phone, "text": "abeg wetin I owe now?"},
        headers=h("a_chan"),
        params={"wait": "false"},
    )
    turn_id = r.json()["turn_id"]
    assert c.post(f"/v1/turns/{turn_id}/stop", headers=owner()).json()["stopped"] is True
    agent.process(turn_id)
    again = c.post(f"/v1/turns/{turn_id}/stop", headers=owner()).json()
    assert again["stopped"] is True and again["status"] == "stopped_by_owner"
    assert again["message"] == "Collector: already stopped: it sends nothing more."


def test_stop_is_tenant_scoped(env: dict[str, Any]) -> None:
    c = env["c"]
    turn_id = shop_turn(c, "act.8", "abeg wetin I owe now?").json()["turn_id"]
    # No key, a wrong key and a channel key all fail; the row is untouched.
    assert c.post(f"/v1/turns/{turn_id}/stop").status_code in (401, 403)
    assert c.post(
        f"/v1/turns/{turn_id}/stop", headers={"Authorization": "Bearer no-such-key"}
    ).status_code in (401, 403, 404)
    assert (
        c.post(
            f"/v1/turns/{turn_id}/stop", headers={"Authorization": f"Bearer {KEYS['a_chan']}"}
        ).status_code
        == 403
    )
    assert c.post("/v1/turns/no-such-turn/stop", headers=owner()).status_code == 404


def test_a_parked_turn_sits_in_the_waiting_tab(env: dict[str, Any]) -> None:
    c = env["c"]
    # A FIRM reminder parks for approval: unfinished, waiting on the owner.
    turn_id = shop_turn(c, "act.9", "FIRM abeg wetin I owe now?").json()["turn_id"]
    body = rows(c, tab="waiting")
    assert body["total"] == 1 and len(body["rows"]) == 1
    assert body["rows"][0]["turn_id"] == turn_id
    assert body["rows"][0]["tab"] == "waiting"
    assert body["rows"][0]["pending_approvals"] == 1
    assert "working on" in body["rows"][0]["what"]

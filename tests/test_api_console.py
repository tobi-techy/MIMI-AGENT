"""Console phase 1 (API side): liveness, thread helper/status/title, ``/v1/me``.

Offline: a scripted model and a SQLite file per test. The new thread fields
are additive; the old ones keep their shape (``tests/test_api.py``).
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mimi.api import threadmeta
from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.harness.gateway import ScriptedModel
from mimi.harness.skills import SKILLS
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.world.loader import load_world
from tests.test_api import script

A = "coral-lane"
W = load_world()
OWNER_KEY = "console-owner-key-0001"
CHAN_KEY = "console-chan-key-00001"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def env(store_url: str) -> Iterator[dict[str, Any]]:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    agent = Agent(engine, models=[ScriptedModel(script)], worker_id="test-worker")
    keys = parse_keys(f"{A}:owner:{OWNER_KEY},{A}:channel:{CHAN_KEY}")
    app = create_app(Settings(keys=keys, wait_s=5), agent, workers=0)
    with TestClient(app) as client:
        yield {"c": client, "agent": agent, "engine": engine}
    engine.dispose()


def owner() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER_KEY}"}


def chan() -> dict[str, str]:
    return {"Authorization": f"Bearer {CHAN_KEY}"}


def turn(c: TestClient, mid: str, text: str, shop: int = 0, **kw: Any) -> Any:
    return c.post("/v1/turns", json={"message_id": mid, "phone": W.shops[shop].phone,
                                     "text": text}, headers=chan(), **kw)


# --------------------------------------------------------------- liveness


def test_root_is_a_liveness_probe_with_no_auth_and_no_database(env: dict[str, Any]) -> None:
    env["engine"].dispose()
    # Point the agent at a database that cannot be reached: GET / still answers.
    env["agent"].engine = sa.create_engine("sqlite:////nonexistent/dir/x.db")
    r = env["c"].get("/")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_is_kept(env: dict[str, Any]) -> None:
    r = env["c"].get("/v1/health")
    assert r.status_code == 200 and r.json()["db"] == "ok"


def test_dockerfile_listens_on_3000_and_probes_root() -> None:
    df = (ROOT / "Dockerfile").read_text()
    assert re.search(r"\bPORT=3000\b", df)
    assert re.search(r"^EXPOSE 3000$", df, re.M)
    assert "--host 0.0.0.0" in df
    assert "127.0.0.1:${PORT}/\"" in df  # HEALTHCHECK hits GET /
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    docker_job = ci.split("\n  docker:", 1)[1].split("\n  secrets:", 1)[0]
    assert "3000:3000" in docker_job and "8000" not in docker_job
    assert "localhost:3000/" in docker_job


# ---------------------------------------------------------------- threads


def _thread(c: TestClient, tid: str) -> dict[str, Any]:
    rows = c.get("/v1/threads", headers=owner()).json()["threads"]
    return next(t for t in rows if t["thread_id"] == tid)


def test_thread_summary_has_helper_status_and_title(env: dict[str, Any]) -> None:
    c = env["c"]
    assert turn(c, "w.1", "abeg wetin I owe now?").status_code == 200
    t = _thread(c, f"{A}:shop:shop-001")
    assert t["helper"] == "collector" and t["helper_name"] == "Collector"
    assert t["status"] == "done"
    assert t["title"].startswith(W.shops[0].name)
    assert "Balance" in t["title"]
    # The old fields are still there.
    for k in ("kind", "shop_id", "shop_name", "last_text", "messages", "pending_approvals"):
        assert k in t
    detail = c.get(f"/v1/threads/{A}:shop:shop-001", headers=owner()).json()["thread"]
    assert detail["helper"] == "collector" and detail["status"] == "done"
    assert detail["title"] == t["title"]


def test_a_pending_approval_makes_the_thread_need_you(env: dict[str, Any]) -> None:
    c = env["c"]
    r = turn(c, "w.firm", "abeg wetin I owe now? FIRM").json()
    assert r["status"] == "parked_for_approval", r
    t = _thread(c, f"{A}:shop:shop-001")
    assert t["pending_approvals"] >= 1 and t["status"] == "needs_you"


def test_a_queued_turn_makes_the_thread_running(env: dict[str, Any]) -> None:
    c = env["c"]
    r = turn(c, "w.q", "hi", params={"wait": "false"})
    assert r.status_code == 202
    t = _thread(c, f"{A}:shop:shop-001")
    assert t["status"] == "running"
    assert t["helper"] == "mimi"  # nothing routed yet: Mimi holds it


def test_a_parked_turn_needs_you(env: dict[str, Any]) -> None:
    c = env["c"]
    turn(c, "w.p", "hi")
    with env["engine"].begin() as conn:
        conn.execute(s.turns.update().where(s.turns.c.tenant_id == A)
                     .values(state="parked", status="parked_budget"))
    assert _thread(c, f"{A}:shop:shop-001")["status"] == "needs_you"


def test_every_skill_has_an_owner_title_with_no_dashes() -> None:
    for sk in SKILLS:
        title = threadmeta.skill_title(sk.key)
        assert title and title[0].isupper(), sk.key
        assert not re.search("[-\u2013\u2014]", title), (sk.key, title)


def test_titles_never_carry_shop_text() -> None:
    meta = threadmeta.derive(kind="shop", thread_id=f"{A}:shop:shop-001", shop_id="shop-001",
                             shop_name="Mama Sola", pending=0, turns=[
                                 {"helper": "collector", "skill": "collector/collections",
                                  "state": "done"}])
    assert meta == {"helper": "collector", "helper_name": "Collector", "status": "done",
                    "title": "Mama Sola: Balance question"}
    owner_meta = threadmeta.derive(kind="owner", thread_id=f"{A}:owner", shop_id="",
                                   shop_name="", pending=0, turns=[])
    assert owner_meta["title"] == "You and Mimi" and owner_meta["helper"] == "mimi"


def test_thread_list_stays_quick(env: dict[str, Any]) -> None:
    c = env["c"]
    for i in range(5):
        turn(c, f"w.many.{i}", "hi", shop=i)
    t0 = time.perf_counter()
    r = c.get("/v1/threads", headers=owner())
    assert r.status_code == 200 and r.json()["total"] >= 5
    assert time.perf_counter() - t0 < 2.0


# --------------------------------------------------------------------- me


def test_me_names_the_tenant_and_live_mode(env: dict[str, Any]) -> None:
    r = env["c"].get("/v1/me", headers=owner())
    assert r.status_code == 200
    body = r.json()
    assert body == {"tenant_id": A, "role": "owner", "mode": "live", "demo": None}
    assert env["c"].get("/v1/me", headers=chan()).json()["role"] == "channel"
    assert env["c"].get("/v1/me").status_code == 401


def test_demo_routes_are_not_found_in_live_mode(env: dict[str, Any]) -> None:
    assert env["c"].post("/v1/demo/sessions").status_code == 404
    assert env["c"].post("/v1/demo/reset", headers=owner()).status_code == 404


def test_the_new_routes_are_in_the_openapi_document() -> None:
    import json

    doc = json.loads((ROOT / "docs" / "api" / "openapi.json").read_text(encoding="utf-8"))
    for p in ("/", "/v1/me", "/v1/demo/sessions", "/v1/demo/reset", "/v1/health"):
        assert p in doc["paths"], p
    summary = doc["components"]["schemas"]["ThreadSummary"]
    for f in ("helper", "helper_name", "status", "title"):
        assert f in summary["properties"], f

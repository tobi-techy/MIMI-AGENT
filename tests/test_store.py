"""The durable store (RAI-266): schema, seed, SqlGo parity with FakeGo, the turn queue."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.harness.go import CrossTenant, FakeGo
from mimi.harness.inbound import Inbound, Payload
from mimi.store import SqlGo, TurnQueue, ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.store.db import database_url
from mimi.store.queue import LeaseLost
from mimi.store.seed import SeedError
from mimi.world.loader import load_world

W = load_world()
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def engine(store_url: str) -> Any:
    e = make_engine(store_url)
    ensure_schema(e)
    seed_world(e, W)
    return e


def test_the_migration_creates_exactly_the_schema(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    env = {**os.environ, "MIMI_DB_URL": url, "DATABASE_URL": ""}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env,
                   check=True, capture_output=True)
    insp = sa.inspect(sa.create_engine(url))
    for t in s.metadata.sorted_tables:
        cols = {c["name"]: c for c in insp.get_columns(t.name)}
        assert set(cols) == {c.name for c in t.columns}, t.name
        for c in t.columns:
            assert bool(cols[c.name]["nullable"]) == bool(c.nullable and not c.primary_key), \
                (t.name, c.name)
        pk = insp.get_pk_constraint(t.name)["constrained_columns"]
        assert pk == [c.name for c in t.primary_key.columns], t.name
        idx = {i["name"] for i in insp.get_indexes(t.name)}
        assert idx == {i.name for i in t.indexes}, t.name
    subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0005"], cwd=ROOT, env=env,
                   check=True, capture_output=True)
    assert "turns" not in sa.inspect(sa.create_engine(url)).get_table_names()


def test_seed_loads_the_synthetic_world_with_totals_from_code(engine: Any) -> None:
    with engine.connect() as conn:
        n_shops = conn.execute(sa.select(sa.func.count()).select_from(s.shops)).scalar()
        bad = conn.execute(sa.text(
            "SELECT d.delivery_id FROM deliveries d JOIN delivery_lines l "
            "ON l.tenant_id = d.tenant_id AND l.delivery_id = d.delivery_id "
            "GROUP BY d.delivery_id, d.total_kobo HAVING SUM(l.line_total_kobo) != d.total_kobo"
        )).all()
        simulated = conn.execute(sa.select(s.shops.c.simulated).distinct()).scalars().all()
    assert n_shops == len(W.shops) and bad == [] and simulated == [True]
    with pytest.raises(SeedError, match="already seeded"):
        seed_world(engine, W)
    counts = seed_world(engine, W, reset=True)
    assert counts["deliveries"] == len(W.deliveries) + len(W.live_deliveries)


def test_seed_cli(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    run = [sys.executable, "-m", "mimi.store", "seed", "--db", url, "--create"]
    out = subprocess.run(run, cwd=ROOT, check=True, capture_output=True, text=True).stdout
    assert "synthetic" in out and "shops 40" in out
    again = subprocess.run(run + ["--if-empty"], cwd=ROOT, capture_output=True, text=True)
    assert again.returncode == 0 and "nothing to do" in again.stdout


def test_sqlgo_reads_the_book_exactly_as_fakego(engine: Any) -> None:
    go, fake = SqlGo(engine), FakeGo(W)
    t = W.tenant_id
    assert go.today() == fake.today()
    assert go.grants(t) == fake.grants(t)
    assert go.collection_account(t) == fake.collection_account(t)
    assert go.receivables(t, None) == fake.receivables(t, None)
    for shop in W.shops:
        sid = shop.shop_id
        assert go.shop(t, sid) == fake.shop(t, sid)
        assert go.receivables(t, sid) == fake.receivables(t, sid)
        assert go.payments(t, sid, 5) == fake.payments(t, sid, 5)
        assert go.promises(t, sid) == fake.promises(t, sid)
    for p in W.payments[:40]:
        ref = p.reference
        assert go.bank_feed_match(t, p.shop_id, p.amount_kobo, ref) == \
            fake.bank_feed_match(t, p.shop_id, p.amount_kobo, ref)
    d = go.directory(t)
    assert d.shop_for(W.shops[3].phone) == W.shops[3].shop_id


def test_sqlgo_refuses_other_tenants_and_unknown_shops(engine: Any) -> None:
    go = SqlGo(engine)
    with pytest.raises(CrossTenant):
        go.receivables("someone-else", None)
    with pytest.raises(CrossTenant):
        go.shop(W.tenant_id, "shop-999")
    with pytest.raises(CrossTenant):
        go.directory("someone-else")


def test_sqlgo_control_plane_and_gate(engine: Any) -> None:
    from datetime import date

    go, t, day = SqlGo(engine), W.tenant_id, date(2026, 10, 26)
    go.add_spend(t, "collector", day, 120)
    go.add_spend(t, "collector", day, 30)
    assert go.daily_spend(t, day) == (150, 50_000)
    assert go.ask_owner_once(t, "daily_cap", day) and not go.ask_owner_once(t, "daily_cap", day)
    assert not go.killed(t, "collector")
    with engine.begin() as conn:
        conn.execute(s.kill_switches.insert().values(tenant_id=t, helper="*"))
    assert go.killed(t, "collector")
    rec = {"tool": "notify_owner", "intent_kind": "notify_owner", "tier": "auto",
           "idempotency_key": "ik_x", "payload": {"message": "hi"}}
    assert go.submit(t, "att1", "notify_owner", {"record": rec}) == "ik_x"
    go.submit(t, "att2", "notify_owner", {"record": rec})
    never = {**rec, "idempotency_key": "ik_n", "tool": "transfer_money", "tier": "auto"}
    go.submit(t, "att3", "transfer_money", {"record": never})
    with engine.connect() as conn:
        rows = {r.idempotency_key: r.outcome for r in conn.execute(sa.select(s.intents))}
    assert rows == {"ik_x": "applied", "ik_n": "refused"}


# ------------------------------------------------------------------ queue


def _enqueue(engine: Any, ext: str, shop: int = 0) -> str:
    from datetime import UTC, datetime

    from mimi.api.agent import Agent
    from mimi.api.offline import OfflineModel

    agent = Agent(engine, models=[OfflineModel()])
    inbound = Inbound(source="shop_message", org_id=W.tenant_id, external_id=ext,
                      received_at=datetime.now(UTC),
                      payload=Payload(text="hi", from_phone=W.shops[shop].phone))
    return agent.submit(W.tenant_id, inbound).turn_id


def test_two_workers_racing_claim_a_turn_once(engine: Any) -> None:
    ids = [_enqueue(engine, f"m{i}", shop=i) for i in range(8)]
    q = TurnQueue(engine, lease_s=30)
    got: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        while True:
            c = q.claim(name)
            if c is None:
                return
            with lock:
                got.append(c.turn_id)

    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(got) == sorted(ids)


def test_one_thread_runs_one_turn_at_a_time_and_a_dead_lease_is_taken_over(
        engine: Any) -> None:
    now = [1_000_000.0]
    q = TurnQueue(engine, lease_s=10, clock=lambda: now[0])
    first = _enqueue(engine, "a1")
    c1 = q.claim("w1")
    assert c1 is not None and c1.turn_id == first
    second = _enqueue(engine, "a2")  # same shop, same thread
    assert q.claim("w2") is None  # the thread is busy
    now[0] += 11  # w1 stopped heartbeating
    c2 = q.claim("w2")
    assert c2 is not None and c2.turn_id == first and c2.takeover and c2.attempts == 2
    with pytest.raises(LeaseLost):
        q.checkpoint(c1, {"x": 1})  # the old holder can never write again
    q.checkpoint(c2, {"x": 2})
    with engine.begin() as conn:
        with pytest.raises(LeaseLost):
            q.finish_in(conn, c1, status="done", helper="", skill="", run_id="", result={})
    assert q.get(second).state == "queued"


def test_postgres_claims_skip_a_locked_thread_instead_of_waiting(engine: Any) -> None:
    """River-style: a thread row another transaction holds is skipped at once."""
    if engine.dialect.name != "postgresql":
        pytest.skip("SKIP LOCKED is the Postgres path; SQLite has one writer at a time")
    import time as _t

    tid = _enqueue(engine, "sk1")
    q = TurnQueue(engine, lease_s=30)
    thread = q.get(tid).thread_id
    with engine.connect() as holder:
        tx = holder.begin()
        holder.execute(sa.select(s.threads.c.thread_id).where(
            s.threads.c.thread_id == thread).with_for_update())
        t0 = _t.monotonic()
        assert q.claim("w1") is None  # skipped, not blocked
        assert _t.monotonic() - t0 < 5
        tx.rollback()
    c = q.claim("w1")
    assert c is not None and c.turn_id == tid


def test_a_turn_that_keeps_failing_stops_after_three_tries(engine: Any) -> None:
    q = TurnQueue(engine, lease_s=30)
    tid = _enqueue(engine, "f1")
    states = []
    for i in range(3):
        c = q.claim(f"w{i}")
        assert c is not None and c.turn_id == tid
        states.append(q.release(c, "boom"))
    assert states == ["queued", "queued", "failed"] and q.claim("w9") is None
    assert q.get(tid).error == "boom"


def test_database_url_prefers_database_url() -> None:
    assert database_url({"DATABASE_URL": "postgres://u@h/db"}) == "postgresql+psycopg2://u@h/db"
    assert database_url({"DATABASE_URL": "postgresql://u@h/db"}) == \
        "postgresql+psycopg2://u@h/db"
    assert database_url({"DATABASE_URL": "postgresql+psycopg://u@h/db"}) == \
        "postgresql+psycopg://u@h/db"
    assert database_url({"MIMI_DB_URL": "sqlite:///x.db"}) == "sqlite:///x.db"
    assert database_url({}) == "sqlite:///./mimi.db"

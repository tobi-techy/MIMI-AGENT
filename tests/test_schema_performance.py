"""The query budget: every hot path has one, and a breach is visible.

Two mechanisms are tested:

* the runtime slow-query log (``mimi.store.db``): a statement over the
  threshold warns and is counted, with no parameters in the text;
* the per-path statement budgets (``mimi.store.budgets``): every statement a
  hot path emits on representative data stays under its ceiling, and every
  budget names a path the audit actually runs.

Pool sizing is checked here too, because a wrong pool is how a system dies at
10x without any single component failing.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.store import bench, ensure_schema, make_engine
from mimi.store.budgets import HOT_PATHS, SLOW_QUERY_MS, budget_for
from mimi.store.db import connection_budget, install_slow_query_log, pool_settings, slow_query_state

ROOT = Path(__file__).resolve().parents[1]
_TRANSACTION = {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "PRAGMA"}


def _hot_paths(engine: Any, data: bench.BenchData) -> list[tuple[str, Any]]:
    """The real path catalogue from ``scripts/plan_audit.py`` (loaded by path)."""
    path = ROOT / "scripts" / "plan_audit.py"
    spec = importlib.util.spec_from_file_location("plan_audit_for_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module._paths(engine, data))


@pytest.fixture()
def engine(store_url: str) -> Any:
    e = make_engine(store_url)
    ensure_schema(e)
    bench.seed(e, bench.TINY)
    return e


def _data(engine: Any) -> bench.BenchData:
    with engine.connect() as conn:
        tenant = str(conn.execute(sa.text(
            "SELECT tenant_id FROM tenants ORDER BY tenant_id LIMIT 1")).scalar())
        shop = str(conn.execute(sa.text(
            "SELECT shop_id FROM shops WHERE tenant_id = :t ORDER BY shop_id LIMIT 1"),
            {"t": tenant}).scalar())
        thread = str(conn.execute(sa.text(
            "SELECT thread_id FROM threads WHERE tenant_id = :t AND kind = 'shop' "
            "ORDER BY last_ts DESC LIMIT 1"), {"t": tenant}).scalar())
        delivery = str(conn.execute(sa.text(
            "SELECT delivery_id FROM deliveries WHERE tenant_id = :t "
            "ORDER BY delivery_id LIMIT 1"), {"t": tenant}).scalar())
    return bench.BenchData(tenant=tenant, shop_id=shop, thread_id=thread,
                           delivery_id=delivery)


def test_every_audited_path_has_a_budget(engine: Any) -> None:
    labels = {label for label, _ in _hot_paths(engine, _data(engine))}
    assert labels == set(HOT_PATHS), (
        "a path with no budget, or a budget with no path: "
        f"{labels.symmetric_difference(HOT_PATHS)}")
    for label, budget in HOT_PATHS.items():
        assert budget.max_statement_ms > 0
        assert budget.max_statement_ms <= SLOW_QUERY_MS, (
            f"{label}: a statement budget over the production log threshold would "
            "never be seen before it is breached")


def test_every_statement_stays_under_its_path_budget(engine: Any) -> None:
    data = _data(engine)
    factor = 2.0 if engine.dialect.name == "sqlite" else 1.0
    over: list[str] = []
    for label, fn in _hot_paths(engine, data):
        budget = budget_for(label)
        assert budget is not None, label
        fn()
        fn()  # warm the plan and the cache: this is the steady-state read
        with bench.Recorder(engine) as rec:
            fn()
        for row in rec.rows:
            head = (row.statement.lstrip().split(None, 1) or [""])[0].upper()
            if head in _TRANSACTION:
                continue
            limit = budget.max_statement_ms * factor
            if row.elapsed_ms > limit:
                over.append(f"{label}: {row.elapsed_ms:.1f} ms > {limit:.0f} ms: "
                            f"{' '.join(row.statement.split())[:140]}")
    assert not over, "queries over budget:\n" + "\n".join(over)


def test_the_claim_is_in_the_budget_and_does_not_touch_claim_indexes(engine: Any) -> None:
    # The queue owns the claim predicates; the audit only measures them.
    budget = HOT_PATHS["queue.claim"]
    assert budget.max_statement_ms >= 100
    turns_indexes = {i.name: [c.name for c in i.columns]
                     for i in __import__("mimi.store.schema", fromlist=["x"]).turns.indexes}
    # No new index leads with state or the lease: state/lease stay the queue's.
    assert all(cols[0] == "tenant_id" or cols[0] == "state" for cols in turns_indexes.values())


def test_slow_query_is_logged_counted_and_redacted(caplog: pytest.LogCaptureFixture) -> None:
    e = make_engine("sqlite://", env={"MIMI_SLOW_QUERY_MS": "0.000001"})
    with caplog.at_level(logging.WARNING, logger="mimi.store.slow"):
        with e.connect() as conn:
            conn.execute(sa.text("select 1234567890123"))
    state = slow_query_state(e)
    assert state is not None and state["count"] >= 1
    assert any("slow query" in r.message for r in caplog.records)
    text = " ".join(str(r["statement"]) for r in state["recent"])
    assert "1234567890123" not in text
    assert "<digits>" in text


def test_installing_the_log_twice_counts_once() -> None:
    e = make_engine("sqlite://", env={"MIMI_SLOW_QUERY_MS": "0.000001"})
    install_slow_query_log(e, 0.000001)
    state = slow_query_state(e)
    with e.connect() as conn:
        conn.execute(sa.text("select 1"))
    first = state["count"]
    install_slow_query_log(e, 0.000001)
    assert slow_query_state(e) is state  # the same counters, one listener
    with e.connect() as conn:
        conn.execute(sa.text("select 1"))
    assert state["count"] > first


def test_pool_defaults_and_the_connection_budget() -> None:
    s = pool_settings({})
    assert (s.size, s.max_overflow) == (5, 5)
    assert s.per_process == 10
    small = connection_budget(4, settings=s, limit=100)
    assert small["fits"] and small["pool_total"] == 40 and small["headroom"] == 50
    big = connection_budget(10, settings=s, limit=100)
    assert not big["fits"]
    # A smaller pool fits a larger deploy, which is the lever at 10x.
    tight = pool_settings({"MIMI_DB_POOL_SIZE": "3", "MIMI_DB_MAX_OVERFLOW": "2"})
    assert connection_budget(15, settings=tight, limit=100)["fits"]


def test_bad_pool_numbers_fail_at_startup() -> None:
    with pytest.raises(ValueError):
        pool_settings({"MIMI_DB_POOL_SIZE": "0"})
    with pytest.raises(ValueError):
        pool_settings({"MIMI_DB_MAX_OVERFLOW": "-1"})
    with pytest.raises(ValueError):
        pool_settings({"MIMI_DB_POOL_TIMEOUT_S": "0"})
    with pytest.raises(ValueError):
        pool_settings({"MIMI_DB_POOL_SIZE": "five"})


def test_postgres_pool_is_bounded(store_url: str) -> None:
    if not store_url.startswith("postgresql"):
        pytest.skip("Postgres only")
    e = make_engine(store_url, env={"MIMI_DB_POOL_SIZE": "2",
                                    "MIMI_DB_MAX_OVERFLOW": "1"})
    assert e.pool.size() == 2
    assert e.pool._max_overflow == 1
    assert e.pool._timeout == 5.0


def test_postgres_slow_query_sleeps_and_warns(store_url: str,
                                              caplog: pytest.LogCaptureFixture) -> None:
    if not store_url.startswith("postgresql"):
        pytest.skip("Postgres only")
    e = make_engine(store_url, env={"MIMI_SLOW_QUERY_MS": "10"})
    with caplog.at_level(logging.WARNING, logger="mimi.store.slow"):
        with e.connect() as conn:
            conn.execute(sa.text("SELECT pg_sleep(0.05)"))
    state = slow_query_state(e)
    assert state is not None and state["count"] >= 1
    assert any("pg_sleep" in str(r["statement"]) for r in state["recent"])

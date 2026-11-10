#!/usr/bin/env python3
"""Explain the SQL the real request paths emit, on a pilot-scale store.

    python scripts/plan_audit.py --url postgresql://localhost/mimi_perf --seed
    python scripts/plan_audit.py --url postgresql://localhost/mimi_perf --out /tmp/before.md
    # apply the new indexes, then
    python scripts/plan_audit.py --url postgresql://localhost/mimi_perf --out /tmp/after.md

``--seed`` fills an empty database with ``mimi.store.bench.PILOT`` (10
distributors, 10,000 shops, 80,000 turns, 640,000 thread events) and runs
ANALYZE. The audit never hand-writes a hot query: it calls the same functions
the API and the worker call, records every statement they issued, and asks
Postgres to EXPLAIN ANALYZE exactly those statements with their parameters in a
transaction it rolls back.

The output is Markdown: a summary table and, for each statement, the plan nodes
(cost, rows, actual time) with the buffers lines dropped. ``docs/DATA.md``
quotes the before and after runs from this script.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mimi.store import bench  # noqa: E402
from mimi.store.bench import Recorder  # noqa: E402
from mimi.store.db import make_engine, normalize_url  # noqa: E402


def _paths(engine: Engine, data: bench.BenchData) -> list[tuple[str, Callable[[], Any]]]:
    """The twenty-odd request-path reads and claims, the real functions."""
    from mimi.api import cards, compaction, reads, returns, routines
    from mimi.store import SqlGo
    from mimi.store.context_stores import SqlHistoryStore
    from mimi.store.invoices import open_invoices, overdue_ids
    from mimi.store.proofbook import SqlProofBook
    from mimi.store.queue import TurnQueue
    from mimi.store.speak import SqlSpeakCounts

    tenant, shop, thread = data.tenant, data.shop_id, data.thread_id
    go = SqlGo(engine)

    def claim() -> Any:
        # Claims one queued turn (scratch database only). It proves the
        # candidate read the worker runs every time it looks for work.
        return TurnQueue(engine, clock=lambda: time.time()).claim("audit-worker")

    def open_invoice_rows() -> Any:
        with engine.begin() as conn:
            return open_invoices(conn, tenant, shop, lock=False)

    def overdue_rows() -> Any:
        with engine.begin() as conn:
            return overdue_ids(conn, tenant, 410, lock=False)

    def activity_steps() -> Any:
        with engine.connect() as conn:
            return reads._activity_steps(conn, tenant, data.turn_id)

    return [
        ("console.threads.list", lambda: reads.list_threads(engine, tenant, limit=50, offset=0)),
        ("console.thread.open", lambda: reads.get_thread(engine, tenant, thread)),
        ("console.thread.uploads",
         lambda: reads.get_thread(engine, tenant, thread, uploads=True)),
        ("console.tasks.list", lambda: reads.list_tasks(engine, tenant, limit=50)),
        ("console.activity.list", lambda: reads.list_activity(engine, tenant, limit=20)),
        ("console.activity.steps", activity_steps),
        ("console.approvals.pending", lambda: reads.list_approvals(engine, tenant, status="pending")),
        ("console.approvals.all", lambda: reads.list_approvals(engine, tenant, status="all")),
        ("console.shops.list", lambda: reads.list_shops(engine, tenant)),
        ("console.shop.open", lambda: reads.get_shop(engine, tenant, shop)),
        ("console.exposure", lambda: reads.exposure(engine, tenant)),
        ("credit.receivables", lambda: go.receivables(tenant, None)),
        ("credit.shop_payments", lambda: go.payments(tenant, shop, 10)),
        ("book.bank_feed_match", lambda: go.bank_feed_match(tenant, shop, 120_000, "ref")),
        ("invoices.open", open_invoice_rows),
        ("invoices.overdue_scan", overdue_rows),
        ("routines.unmatched_alerts", lambda: routines._unmatched_alerts(engine, tenant)),
        ("routines.states", lambda: routines._states(engine, tenant)),
        ("routines.view", lambda: routines.view(engine, tenant)),
        ("routines.scheduler_tenants", lambda: routines.RoutineScheduler(engine).tenants()),
        ("returns.list", lambda: returns.list_returns(engine, tenant)),
        ("digest.pending", lambda: SqlSpeakCounts(engine).pending(tenant, _day(data))),
        ("proof.events", lambda: SqlProofBook(engine).events(tenant, data.delivery_id)),
        ("context.history", lambda: SqlHistoryStore(engine).turns(tenant, thread)),
        ("cards.brief_facts", lambda: cards.brief_facts(engine, go, tenant)),
        ("compaction.due", lambda: compaction.due_threads(engine)),
        ("queue.claim", claim),
    ]


def _day(data: bench.BenchData) -> Any:
    from datetime import date

    return date(2026, 11, 10)


def trim_plan(plan: str) -> str:
    """The plan without buffer lines: nodes, conditions, costs and actuals."""
    keep: list[str] = []
    for line in plan.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("Buffers:", "Planning:", "JIT:", "Triggers:")):
            continue
        if stripped.startswith(("Index Cond:", "Filter:", "Rows Removed", "Hash Cond:",
                                "Merge Cond:", "Recheck Cond:", "Sort Key:", "Group Key:",
                                "Parallel Seq Scan on", "Seq Scan on", "Index Scan using",
                                "Bitmap Index Scan on", "Bitmap Heap Scan on", "Limit",
                                "Hash", "Sort", "Aggregate", "Gather", "Nested Loop",
                                "Merge Join", "Hash Join", "Unique", "Subquery Scan",
                                "CTE Scan", "Materialize", "Append", "Result",
                                "Execution Time:", "Planning Time:")):
            keep.append(stripped)
        elif "->" in stripped and any(word in stripped for word in (
                "Scan", "Join", "Sort", "Aggregate", "Limit", "Hash", "Gather", "Loop",
                "Materialize", "Append", "Result", "Unique")):
            keep.append(stripped)
    return "\n".join(keep)


def bench_paths(engine: Engine, data: bench.BenchData, *, rounds: int) -> str:
    """Time each hot path end to end, the way a request calls it.

    A path is the function the API calls, including the statements it makes
    per row (the approvals list reads one draft per approval), so this is the
    number the query budget is written against. Claims are left out: each one
    consumes a queued turn.
    """
    import statistics
    import time as _time

    rows: list[tuple[str, float, float, float]] = []
    for label, fn in _paths(engine, data):
        if label == "queue.claim":
            continue
        for _ in range(2):  # warm the cache and the plan
            try:
                fn()
            except Exception:
                break
        times: list[float] = []
        for _ in range(max(1, rounds)):
            started = _time.perf_counter()
            fn()
            times.append((_time.perf_counter() - started) * 1000)
        if times:
            times.sort()
            p95 = times[min(len(times) - 1, int(round(0.95 * (len(times) - 1))))]
            rows.append((label, statistics.median(times), p95, max(times)))
    out = ["| path | p50 ms | p95 ms | max ms |", "|---|---:|---:|---:|"]
    for label, p50, p95, mx in sorted(rows, key=lambda r: -r[2]):
        out.append(f"| `{label}` | {p50:.2f} | {p95:.2f} | {mx:.2f} |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="", help="SQLAlchemy URL (Postgres for plans)")
    ap.add_argument("--seed", action="store_true", help="seed PILOT data into an empty store")
    ap.add_argument("--scale", choices=("pilot", "tiny"), default="pilot")
    ap.add_argument("--out", default="", help="write Markdown here (default stdout)")
    ap.add_argument("--only", default="", help="comma separated path labels to explain")
    ap.add_argument("--bench", type=int, default=0,
                    help="also time each path end to end over this many rounds")
    args = ap.parse_args(argv)

    url = normalize_url(args.url or "")
    if not url:
        print("set --url (or DATABASE_URL) to the scratch database", file=sys.stderr)
        return 2
    engine = make_engine(url)
    if engine.dialect.name != "postgresql":
        print("this audit explains plans on Postgres; SQLite has no EXPLAIN ANALYZE", file=sys.stderr)
        return 2

    data = bench.BenchData()
    with engine.connect() as conn:
        seeded = int(conn.execute(sa.text("SELECT count(*) FROM tenants")).scalar() or 0)
    if args.seed and seeded:
        print(f"database already holds {seeded} tenants; measuring what is there",
              file=sys.stderr)
    elif not seeded:
        print("seeding pilot data...", file=sys.stderr)
        started = time.perf_counter()
        data = bench.seed(engine, bench.PILOT if args.scale == "pilot" else bench.TINY)
        print(f"seeded in {time.perf_counter() - started:.1f}s", file=sys.stderr)
    if not data.tenant:
        with engine.connect() as conn:
            data.tenant = str(conn.execute(sa.text(
                "SELECT tenant_id FROM tenants ORDER BY tenant_id LIMIT 1")).scalar())
            data.shop_id = str(conn.execute(sa.text(
                "SELECT shop_id FROM shops WHERE tenant_id = :t ORDER BY shop_id LIMIT 1"),
                {"t": data.tenant}).scalar())
            data.thread_id = str(conn.execute(sa.text(
                "SELECT thread_id FROM threads WHERE tenant_id = :t AND kind = 'shop' "
                "ORDER BY last_ts DESC LIMIT 1"), {"t": data.tenant}).scalar())
            data.delivery_id = str(conn.execute(sa.text(
                "SELECT delivery_id FROM deliveries WHERE tenant_id = :t ORDER BY delivery_id LIMIT 1"),
                {"t": data.tenant}).scalar())
            data.turn_id = str(conn.execute(sa.text(
                "SELECT turn_id FROM turns WHERE tenant_id = :t AND thread_id = :th "
                "ORDER BY created_ts DESC, turn_id DESC LIMIT 1"),
                {"t": data.tenant, "th": data.thread_id}).scalar() or "")

    with engine.begin() as conn:
        conn.exec_driver_sql("ANALYZE")

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    lines: list[str] = ["# Hot path plans", "",
                        f"Engine: {engine.dialect.name} {engine.dialect.server_version_info}", ""]
    with Recorder(engine) as rec:
        for label, fn in _paths(engine, data):
            if only and label not in only:
                continue
            rec.label = label
            try:
                fn()
            except Exception as exc:  # report and keep going: one bad path is data
                lines.append(f"## {label}\n\ncould not run: {type(exc).__name__}: {exc}\n")
                continue
        rec.label = ""

    records = rec.selects()
    plans = {id(r): bench.explain(engine, r) for r in records}
    lines += ["## Summary", "",
              "| path | statements | total ms | slowest statement ms | seq scans |",
              "|---|---:|---:|---:|---|"]
    by_label: dict[str, list[bench.Recorded]] = {}
    for r in records:
        by_label.setdefault(r.label, []).append(r)
    for label, _ in _paths(engine, data):
        rows = by_label.get(label, [])
        total = sum(r.elapsed_ms for r in rows)
        slowest = max((r.elapsed_ms for r in rows), default=0.0)
        scans: list[str] = []
        for r in rows:
            scans += bench.seq_scans(plans[id(r)])
        scans = sorted(set(scans))
        lines.append(f"| `{label}` | {len(rows)} | {total:.2f} | {slowest:.2f} | "
                     f"{', '.join(scans) if scans else 'none'} |")
    lines.append("")

    if args.bench:
        lines += [f"## End-to-end path timings ({args.bench} rounds, warm)",
                  "", bench_paths(engine, data, rounds=args.bench), ""]

    for r in records:
        plan = plans[id(r)]
        lines += [f"## {r.label}", "", "```sql", " ".join(r.statement.split())[:1800], "```", "",
                  "```", trim_plan(plan), "```", ""]
    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

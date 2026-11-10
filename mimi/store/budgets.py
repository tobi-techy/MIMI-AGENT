"""The latency budget for every hot path, and the numbers behind them.

A budget is only useful if something measures it. Three things use this
module:

* ``make_engine`` installs a slow-query logger that warns on any statement
  over ``SLOW_QUERY_MS`` (200 ms by default) in production;
* ``tests/test_schema_performance.py`` times every statement each hot path
  runs and fails when one exceeds its ``max_statement_ms``;
* ``scripts/plan_audit.py --bench`` times each path end to end at pilot scale
  and marks the paths whose p95 exceeds ``p95_path_ms``.

The path names are the ones in ``scripts/plan_audit.py`` so a plan and a
budget line refer to the same read. A path that is over its target is not
hidden: it is listed in ``docs/DATA.md`` with why, and the bench exits 1.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Any single statement slower than this is logged in production. It is a
#: warning, never an error: a slow query must not fail a customer's request.
SLOW_QUERY_MS = 200.0


@dataclass(frozen=True)
class Budget:
    """One hot path: its statement ceiling and its end-to-end target."""

    #: No single statement on the path may take longer than this (milliseconds).
    max_statement_ms: int
    #: The path's p95 target on the pilot dataset (10k shops, 10 tenants).
    p95_path_ms: int
    #: Why the number is what it is, or what stops it being met today.
    note: str = ""


#: The pilot-scale budgets. The statement ceiling is the part a test enforces
#: on every run; the p95 target is the part the bench reports.
HOT_PATHS: dict[str, Budget] = {
    "console.threads.list": Budget(
        60, 400, "50 threads per page; one summary query per thread"),
    "console.thread.open": Budget(60, 60, "one thread, its messages and steps"),
    "console.thread.uploads": Budget(
        60, 60, "demo mode: one thread's inbound media refs by prefix"),
    "console.tasks.list": Budget(60, 60, "newest 500 turns, one page of tasks"),
    "console.activity.list": Budget(60, 60, "newest 500 turns, steps for a page"),
    "console.activity.steps": Budget(40, 40, "one task's step lines by turn id"),
    "console.approvals.pending": Budget(
        60, 100, "one draft message read per approval"),
    "console.approvals.all": Budget(
        60, 300, "reads every approval (no limit yet); needs a page size"),
    "console.shops.list": Budget(
        60, 150, "every shop and its owed total; needs pagination at 10k shops"),
    "console.shop.open": Budget(60, 60, "one shop: last deliveries, invoices, promises"),
    "console.exposure": Budget(
        60, 1000, "code sums per shop x payment; over target, tracked below"),
    "credit.receivables": Budget(60, 60, "the open book, oldest first"),
    "credit.shop_payments": Budget(30, 30, "one shop's last ten payments"),
    "book.bank_feed_match": Budget(30, 30, "match one credit by amount"),
    "invoices.open": Budget(30, 30, "one shop's open invoices, locked in order"),
    "invoices.overdue_scan": Budget(30, 30, "the open book past its due day"),
    "routines.unmatched_alerts": Budget(30, 30, "unbacked alerts only (partial index)"),
    "routines.states": Budget(30, 30, "one tenant's routine rows"),
    "routines.view": Budget(30, 30, "the routines screen"),
    "routines.scheduler_tenants": Budget(30, 30, "every tenant id, once a tick"),
    "returns.list": Budget(60, 60, "one tenant's returns, newest first"),
    "digest.pending": Budget(30, 30, "today's unconsumed digest items"),
    "proof.events": Budget(30, 30, "one record's proof history"),
    "context.history": Budget(30, 30, "one thread's history in time order"),
    "cards.brief_facts": Budget(
        60, 2000, "over target: one book read per shop today; needs a batched read"),
    "compaction.due": Budget(30, 30, "due timer rows only (partial index)"),
    "queue.claim": Budget(
        200, 200, "candidate scan and lease; state/lease indexes are the queue's"),
}

#: Paths known to breach their p95 target at pilot scale, with the reason and
#: the change that fixes it. The bench prints these rather than hiding them.
OVER_TARGET: dict[str, str] = {
    "console.approvals.all": (
        "reads every approval and looks up its draft one by one (3,000 approvals "
        "= 650 ms; 30k = 6.5 s). Fix: a page size and one join for the drafts."),
    "cards.brief_facts": (
        "7,011 statements for one morning brief: every shop is read on its own "
        "through SqlGo.shop_row_for. Fix: one book read grouped by shop, the way "
        "list_shops already reads it."),
    "console.exposure": (
        "the DB part is 10 ms; the 2.3 s is code summing 5,000 payments across "
        "1,000 shops in Python. Fix: aggregate per shop in SQL or index the "
        "payments by (shop, day) and sum once."),
    "console.shops.list": (
        "returns every shop unpaginated (about 45 ms and ~1 MB at 1,000 shops); "
        "at 10,000 shops it is ten times the rows. Fix: a cursor page."),
}


def budget_for(path: str) -> Budget | None:
    return HOT_PATHS.get(path)


def breach(path: str, p95_ms: float) -> bool:
    """True when a measured path p95 is over its target."""
    b = HOT_PATHS.get(path)
    return b is not None and p95_ms > b.p95_path_ms

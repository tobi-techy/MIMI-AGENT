"""Retention for the append-only tables: what goes, when, and why.

Eight tables grow with every message and every turn and are never updated back
down: ``inbound_messages``, ``turns``, ``turn_attempts``, ``messages``,
``thread_events``, ``history``, ``proof_events`` and ``digest_queue``. Left
alone they are the largest thing in the database within a year.

The rule this module holds: **a dispute can need a record, so financial
evidence is kept for six years.** The Limitation Act gives a contract claim six
years in Nigeria, so ``messages`` and ``proof_events`` (what was said, who
confirmed a payment and when) are kept 2,190 days and are never shortened. The
operational traces around them (a turn's lease attempts, its step timings) are
kept two years, long enough to investigate an incident and to answer "what did
Mimi do that day", and no longer.

    python -m mimi.store.retention                    # dry run, the default
    python -m mimi.store.retention --apply            # delete old rows
    python -m mimi.store.retention --apply --only turns,messages

Deletion is batched (2,000 rows a statement, a short pause between batches) so
a run never holds a long lock on a table the API is writing. ``--max-batches``
bounds one run; the next run continues where this one stopped.

**Partition and drop** is the path when a table outgrows batched deletes: see
``PARTITION_PLAN``. Its trigger is written down there; none of these tables is
there yet at pilot scale (the largest, ``thread_events``, is under a million
rows after months of ten distributors).

Nothing here deletes a book row (deliveries, invoices, payments, bank feed):
Go owns those, and a money record is not retention's to remove.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.clock import wall
from mimi.store import schema as s

#: A contract claim can be brought for six years, so evidence lives this long.
DISPUTE_EVIDENCE_DAYS = 365 * 6


@dataclass(frozen=True)
class Policy:
    """One table's retention rule, with the product reason for the number."""

    table: str
    column: str
    days: int
    reason: str
    #: ``evidence`` rows can be needed in a dispute; ``operational`` rows are
    #: diagnostics. The distinction is what stops a tidy-up from deleting
    #: something a shop can still challenge.
    kind: str
    #: Epoch-seconds float column (the store's ``*_ts`` ordering columns).
    numeric: bool = False
    #: Extra predicate. ``consumed`` means the digest item was already used.
    where: str = ""


POLICIES: tuple[Policy, ...] = (
    Policy(
        "turn_attempts", "claimed_at", 180,
        "Lease diagnostics: who took a turn, when, and whether it was a takeover. "
        "The turn row itself keeps the outcome, so half a year is enough to "
        "investigate a stuck worker. Operational, not evidence.",
        kind="operational"),
    Policy(
        "inbound_messages", "received_at", 730,
        "The shop's original words, kept two years so a delivery confirmation or a "
        "dispute can be re-read. The phone number is blanked once the turn ends "
        "(NDPR storage limitation); the turn and its messages keep the rest.",
        kind="evidence"),
    Policy(
        "turns", "created_ts", 730,
        "What Mimi did, which helper ran, and how it ended: two years for incidents "
        "and the owner's 'why did you do that' question. The approvals it raised "
        "are kept separately and longer.",
        kind="operational", numeric=True),
    Policy(
        "thread_events", "ts", 730,
        "The step ledger: every tool call, model call, its cost and duration. Two "
        "years for the Activity view's history and the cost audit; the per-tenant "
        "spend rows are kept separately.",
        kind="operational", numeric=True),
    Policy(
        "history", "ts", 730,
        "The context snapshots a turn was built from. Compaction replaces them with "
        "a summary, and two years matches the turns they belong to.",
        kind="operational", numeric=True),
    Policy(
        "messages", "ts", 2_190,
        "The conversation as delivered, in both directions: the record a payment or "
        "delivery dispute is argued from. Six years, the limitation period for a "
        "contract claim; never shortened because it saves space.",
        kind="evidence", numeric=True),
    Policy(
        "proof_events", "at", 2_190,
        "Who moved a record's proof level and when (the rep, the shop, the bank "
        "feed). This is money-adjacent evidence, kept for the full six years.",
        kind="evidence"),
    Policy(
        "digest_queue", "created_at", 400,
        "The owner's digest items and the unprompted-message reasons. Only items "
        "already consumed are deleted; an item nobody read is never dropped.",
        kind="operational", where="consumed"),
    Policy(
        "speak_counts", "created_at", 400,
        "The per-audience daily message counters behind the caps. A year and a bit "
        "keeps the audit of how often Mimi spoke; the caps only need today.",
        kind="operational"),
    Policy(
        "routine_runs", "started_ts", 400,
        "Which routine ran on which Lagos day and what it found. A year and a bit "
        "for the routines screen and 'did the morning brief run'; routine_state "
        "keeps the latest result for ever.",
        kind="operational", numeric=True),
)

BY_TABLE = {p.table: p for p in POLICIES}

#: What to do when a table outgrows batched deletion. Written down now so the
#: answer is a decision, not a surprise; none is triggered at pilot scale.
PARTITION_PLAN: tuple[dict[str, str], ...] = (
    {"table": "thread_events", "key": "ts", "interval": "month",
     "trigger": "over 50M rows, or a retention run's batches no longer finish in "
                "the nightly window, or dead tuples stay above 20% after autovacuum",
     "path": "partition by RANGE (to_timestamp(ts)) monthly, keep partitions for "
             "the policy's window, then DROP the oldest partition instead of DELETE"},
    {"table": "messages", "key": "ts", "interval": "month",
     "trigger": "over 50M rows, or a single tenant's messages over 5M",
     "path": "partition by RANGE (to_timestamp(ts)) monthly; six years of "
             "partitions, dropped only by the retention job's reviewed step"},
    {"table": "inbound_messages", "key": "received_at", "interval": "month",
     "trigger": "over 20M rows, or delete batches over 100k rows a night",
     "path": "partition by RANGE (received_at::timestamptz) monthly, drop after "
             "two years"},
    {"table": "turns", "key": "created_ts", "interval": "month",
     "trigger": "over 20M rows, or the queue's candidate index no longer fits in "
                "memory (check pg_relation_size of ix_turns_state)",
     "path": "partition by RANGE (to_timestamp(created_ts)) monthly, drop after "
             "two years; the queue reads the newest partitions"},
    {"table": "history", "key": "ts", "interval": "month",
     "trigger": "over 20M rows",
     "path": "partition by RANGE (to_timestamp(ts)) monthly, drop after two years"},
    {"table": "proof_events", "key": "at", "interval": "year",
     "trigger": "over 20M rows",
     "path": "partition by RANGE (at::timestamptz) yearly; six years kept, then "
             "dropped only after a legal review"},
    {"table": "digest_queue", "key": "created_at", "interval": "month",
     "trigger": "over 10M rows",
     "path": "partition by RANGE (created_at::timestamptz) monthly; consumed "
             "items dropped after a year, unconsumed items moved, never dropped"},
    {"table": "turn_attempts", "key": "claimed_at", "interval": "month",
     "trigger": "over 10M rows",
     "path": "partition by RANGE (claimed_at::timestamptz) monthly, drop after "
             "half a year"},
)


def _cutoff(policy: Policy, now: float) -> str | float:
    value = now - policy.days * 86_400
    return value if policy.numeric else datetime.fromtimestamp(value, UTC).isoformat()


def _row_selector(engine: Engine) -> str:
    """``rowid`` on SQLite, ``ctid`` on Postgres: a bounded delete, not a scan."""
    return "rowid" if engine.dialect.name == "sqlite" else "ctid"


def _where(policy: Policy) -> str:
    return f"{policy.column} < :cutoff" + (f" AND {policy.where}" if policy.where else "")


def plan(now: float | None = None, *, only: set[str] | None = None) -> list[dict[str, Any]]:
    """The retention plan at a moment: table, cutoff and why. Read-only."""
    at = wall() if now is None else now
    out: list[dict[str, Any]] = []
    for p in POLICIES:
        if only and p.table not in only:
            continue
        out.append({"table": p.table, "column": p.column, "days": p.days,
                    "cutoff": _cutoff(p, at), "kind": p.kind, "reason": p.reason,
                    "where": p.where})
    return out


def run(engine: Engine, *, now: float | None = None, apply: bool = False,
        batch: int = 2_000, max_batches: int | None = None,
        only: set[str] | None = None, sleep_s: float = 0.05) -> dict[str, Any]:
    """Delete (or count) old rows table by table, in short batched statements.

    ``apply=False`` is a dry run: exact counts and nothing written. A run with
    ``max_batches`` stops early and returns what it did; the next run carries
    on. Batches never overlap a table's whole retention window: the cutoff is
    fixed once per table at the start of the run.
    """
    at = wall() if now is None else now
    if batch < 1:
        raise ValueError("batch must be at least 1")
    selector = _row_selector(engine)
    summary: dict[str, Any] = {"at": datetime.fromtimestamp(at, UTC).isoformat(),
                               "applied": apply, "tables": [], "total": 0}
    for p in POLICIES:
        if only and p.table not in only:
            continue
        table = s.metadata.tables[p.table]
        cutoff = _cutoff(p, at)
        where = _where(p)
        rows = 0
        batches = 0
        with engine.begin() as conn:
            if not apply:
                rows = int(conn.execute(
                    sa.select(sa.func.count()).select_from(table).where(
                        table.c[p.column] < cutoff,
                        *([sa.text(p.where)] if p.where else []))).scalar() or 0)
            else:
                while True:
                    sql = sa.text(
                        f"DELETE FROM {p.table} WHERE {selector} IN "
                        f"(SELECT {selector} FROM {p.table} WHERE {where} LIMIT :batch)")
                    done = conn.execute(sql, {"cutoff": cutoff, "batch": batch}).rowcount
                    rows += max(0, int(done or 0))
                    batches += 1
                    if not done or done < batch:
                        break
                    if max_batches is not None and batches >= max_batches:
                        break
                    if sleep_s:
                        time.sleep(sleep_s)
        summary["tables"].append({"table": p.table, "days": p.days, "cutoff": cutoff,
                                  "rows": rows, "batches": batches, "kind": p.kind})
        summary["total"] += rows
    return summary


def report(summary: dict[str, Any]) -> str:
    verb = "deleted" if summary["applied"] else "would delete"
    lines = [f"retention at {summary['at']}: {verb} {summary['total']} rows"]
    for t in summary["tables"]:
        lines.append(f"  {t['table']:18} older than {t['cutoff']} "
                     f"({t['days']} days, {t['kind']}): {t['rows']} rows")
    return "\n".join(lines)


def check_policies() -> list[str]:
    """Problems with the retention table itself (a test calls this)."""
    problems: list[str] = []
    seen: set[str] = set()
    for p in POLICIES:
        if p.table in seen:
            problems.append(f"{p.table}: two policies")
        seen.add(p.table)
        if not p.reason.strip():
            problems.append(f"{p.table}: no reason")
        if p.kind not in ("evidence", "operational"):
            problems.append(f"{p.table}: kind must be evidence or operational")
        table = s.metadata.tables.get(p.table)
        if table is None:
            problems.append(f"{p.table}: not in the schema")
            continue
        column = table.c.get(p.column)
        if column is None:
            problems.append(f"{p.table}.{p.column}: no such column")
            continue
        is_numeric = isinstance(column.type, (sa.Float, sa.Integer, sa.Numeric))
        if p.numeric != is_numeric:
            problems.append(f"{p.table}.{p.column}: numeric={p.numeric} but column is "
                            f"{column.type}")
        if p.days < 1:
            problems.append(f"{p.table}: days must be positive")
        if p.kind == "evidence" and p.days < 365:
            problems.append(f"{p.table}: evidence kept under a year")
    for required in ("messages", "proof_events"):
        p = BY_TABLE.get(required)
        if p is None or p.days < DISPUTE_EVIDENCE_DAYS:
            problems.append(f"{required}: must be kept the six-year dispute window")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="", help="SQLAlchemy URL (default DATABASE_URL)")
    ap.add_argument("--apply", action="store_true",
                    help="delete; without it this is a dry run")
    ap.add_argument("--batch", type=int, default=2_000)
    ap.add_argument("--max-batches", type=int, default=0)
    ap.add_argument("--only", default="", help="comma separated table names")
    ap.add_argument("--now", default="", help="ISO time to run as (tests and backfills)")
    args = ap.parse_args(argv)
    from mimi.store.db import make_engine

    engine = make_engine(args.url or None)
    now = (datetime.fromisoformat(args.now).replace(tzinfo=UTC).timestamp()
           if args.now else None)
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    summary = run(engine, now=now, apply=args.apply, batch=args.batch,
                  max_batches=args.max_batches or None, only=only or None)
    print(report(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())

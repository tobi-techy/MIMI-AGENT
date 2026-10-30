"""``python -m mimi.api.week``: the simulated week, its soak, and its worker.

    python -m mimi.api.week                       # one offline week, ₦0
    python -m mimi.api.week --soak-hours 6 --soak-kills 20
    python -m mimi.api.week --live --max-naira 2000   # needs META_API_KEY
    python -m mimi.api.week worker ...            # (internal) one worker process

Writes ``<out>/<name>.json`` and ``<out>/<name>.md`` (default
``docs/runs/week-offline``); ``--no-write`` prints only. Exits 1 when a pass
mark is missed.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.api.week")
    sub = ap.add_subparsers(dest="cmd")
    w = sub.add_parser("worker", help="(internal) one week worker process")
    w.add_argument("--db", required=True)
    w.add_argument("--workdir", required=True)
    w.add_argument("--worker-id", default="week-worker")
    w.add_argument("--live", action="store_true")
    w.add_argument("--price-as", default="muse-spark-1.3")
    ap.add_argument("--seed", type=int, default=273)
    ap.add_argument("--workers", type=int, default=1, help="worker processes (default 1)")
    ap.add_argument("--db", default="", help="store URL (default: a new SQLite file in the "
                    "workdir); a Postgres URL is wiped, so only a throwaway database")
    ap.add_argument("--workdir", default="")
    ap.add_argument("--live", action="store_true",
                    help="real Muse Spark and SAM 3.1 calls (needs META_API_KEY and --max-naira)")
    ap.add_argument("--max-naira", type=int, default=0,
                    help="live only: a hard ceiling on model spend; the run sets every "
                    "kill switch and stops when it is reached")
    ap.add_argument("--max-naira-per-turn", type=int, default=0,
                    help="live only: stop early when the average spend per settled turn "
                    "goes over this (checked after 30 turns)")
    ap.add_argument("--price-as", default="muse-spark-1.3",
                    help="offline: price each playbook turn as this model (projected cost, "
                    "so budgets and the daily cap run as live); '' for zero")
    ap.add_argument("--week-kills", type=int, default=0,
                    help="forced worker kills during the one week")
    ap.add_argument("--soak-hours", type=float, default=0.0,
                    help="loop the week for this many hours (the soak)")
    ap.add_argument("--soak-kills", type=int, default=0,
                    help="forced worker kills spread over the soak")
    ap.add_argument("--max-loops", type=int, default=0, help="stop after this many weeks")
    ap.add_argument("--days", type=int, default=7, help="days of the week to run (default 7)")
    ap.add_argument("--compress", type=float, default=0.0,
                    help="seconds to sleep between waves (0: as fast as the workers go)")
    ap.add_argument("--out", default=str(ROOT / "docs" / "runs"))
    ap.add_argument("--name", default="")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--rescore", default="", help="score a finished run's workdir again")
    args = ap.parse_args(argv)
    if args.cmd == "worker":
        from mimi.api.week.worker import run as run_worker

        return run_worker(args.db, workdir=Path(args.workdir), worker_id=args.worker_id,
                          live=args.live, price_as=args.price_as)
    if args.live and args.max_naira <= 0:
        ap.error("--live needs --max-naira N (a hard naira ceiling)")
    from mimi.api.week.report import markdown, score
    from mimi.api.week.runner import load_run, run_week

    if args.rescore:
        run = load_run(Path(args.rescore))
        wd = run.workdir
    else:
        wd = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="mimi-week-"))
    run = run if args.rescore else run_week(workdir=wd, db_url=args.db, seed=args.seed, workers=args.workers,
                   live=args.live, max_naira=args.max_naira or None,
                   price_as=args.price_as if not args.live else "",
                   soak_hours=args.soak_hours, soak_kills=args.soak_kills,
                   week_kills=args.week_kills, max_loops=args.max_loops,
                   compress=args.compress, days=args.days,
                   max_naira_per_turn=args.max_naira_per_turn)
    report = score(run)
    text = markdown(report)
    print(text)
    if not args.no_write:
        name = args.name or ("soak-" if args.soak_hours else "week-") + run.mode.split("-")[0]
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{name}.json").write_text(json.dumps(report, indent=1, sort_keys=True,
                                                     default=str) + "\n")
        (out / f"{name}.md").write_text(text + "\n")
        print(f"wrote {out / name}.json and .md")
    print(f"workdir: {wd}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

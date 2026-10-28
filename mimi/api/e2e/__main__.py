"""``python -m mimi.api.e2e [--live] [--no-write]`` and the e2e ``worker``.

Without ``--live`` the run replays the recorded Muse Spark, SAM 3.1 and
persona answers (CI). ``--live`` records what is missing (needs
``META_API_KEY``). Writes ``eval/e2e/e2e.report.json`` and the scene table
``eval/e2e/e2e.report.md`` unless ``--no-write``; exits 1 when a pass mark is
missed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from mimi.api.e2e.models import EVAL_DIR


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.api.e2e")
    sub = ap.add_subparsers(dest="cmd")
    w = sub.add_parser("worker", help="(internal) one e2e worker process")
    w.add_argument("--db", required=True)
    w.add_argument("--cache-dir", default=str(EVAL_DIR))
    w.add_argument("--worker-id", default="e2e-worker")
    w.add_argument("--live", action="store_true")
    w.add_argument("--pause-at", default="")
    w.add_argument("--marker", default="")
    w.add_argument("--stop-file", default="")
    w.add_argument("--usage", default="")
    w.add_argument("--kill-plan", default="")
    ap.add_argument("--live", action="store_true", help="record missing answers (needs a key)")
    ap.add_argument("--no-write", action="store_true", help="do not write the report files")
    ap.add_argument("--cache-dir", default=str(EVAL_DIR))
    ap.add_argument("--workdir", default="")
    ap.add_argument("--db", default="", help="store URL (default: a new SQLite file); a "
                    "Postgres URL is wiped and reused, so only point it at a throwaway database")
    args = ap.parse_args(argv)
    if args.cmd == "worker":
        from mimi.api.e2e.worker import run as run_worker

        return run_worker(args.db, live=args.live, cache_dir=Path(args.cache_dir),
                   worker_id=args.worker_id, pause=args.pause_at, marker=args.marker,
                   stop_file=args.stop_file, usage=args.usage,
                   kill_plan=args.kill_plan)
    from mimi.api.e2e.runner import collect
    from mimi.api.e2e.score import score, table

    wd = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="mimi-e2e-"))
    wd.mkdir(parents=True, exist_ok=True)
    run = collect(live=args.live, cache_dir=Path(args.cache_dir), workdir=wd,
                  db_url=args.db or os.environ.get("MIMI_E2E_DATABASE_URL", "").strip())
    report = score(run, wd / "usage.jsonl")
    report["mode"] = "live" if args.live else "replay"
    text = table(report)
    print(text)
    for k in ("misroutes", "safety_violations", "money_moves", "invented_figures",
              "duplicate_replies", "failed_turns", "lost_jobs"):
        if report[k]:
            print(f"{k}: {json.dumps(report[k], indent=1)}")
    if not args.no_write:
        out = Path(args.cache_dir)
        (out / "e2e.report.json").write_text(json.dumps(report, indent=1, sort_keys=True,
                                                        default=str) + "\n")
        (out / "e2e.report.md").write_text(text + "\n")
    print(f"workdir: {wd}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

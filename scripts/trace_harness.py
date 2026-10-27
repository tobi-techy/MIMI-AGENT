#!/usr/bin/env python3
"""Collector runs traced to Langfuse (``make trace-harness``).

    LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... python scripts/trace_harness.py --public

Replays the recorded Muse Spark answers from ``eval/harness/`` for a few of
the 50 Collector scenarios (an owner's "chase everyone over 50k", a balance
ask, a shop that paid, a shop that disputes) through the real harness, and
exports one trace per run (environment ``demo``): a span per step, the model
call as a generation, every tool call with its tier and scope, scores for the
run status and budget. No model is called, so it costs nothing. With
``--public`` the first trace is made public and its link printed.

Without the Langfuse keys it exits 2 and sends nothing. In CI it refuses too.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mimi.harness.eval import LIVE_CACHE, make  # noqa: E402
from mimi.harness.gateway import RecordedToolModel  # noqa: E402
from mimi.harness.scenarios import build  # noqa: E402
from mimi.harness.tracing import trace_run  # noqa: E402
from mimi.observe.langfuse_export import LangfuseExporter, LangfuseSettings  # noqa: E402
from mimi.world.loader import load_world  # noqa: E402

PICK = ("chase-01", "balance-01", "paid-01", "dispute-01")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--public", action="store_true")
    ap.add_argument("--only", default=",".join(PICK))
    args = ap.parse_args(argv)
    settings = LangfuseSettings.from_env(environment="demo")
    if settings is None:
        print("Langfuse keys not set (or running in CI): nothing sent.")
        return 2
    first = {"done": False}

    def public_if(turn: object) -> bool:
        if args.public and not first["done"]:
            first["done"] = True
            return True
        return False

    ex = LangfuseExporter(settings, public_if=lambda t: public_if(t))
    world = load_world()
    wanted = set(args.only.split(","))
    rec = RecordedToolModel(LIVE_CACHE, None, name="muse-spark-1.3")
    for sc in [s for s in build(world) if s.sid in wanted]:
        h, helper, _ = make(world, rec, at=sc.event.at, withheld=sc.withheld)
        r = h.run(sc.event, helper)
        shop = sc.event.shop_id or "owner"
        trace_run(r, ex, shop_id=shop, goal=sc.event.text)
        print(f"{sc.sid}: {r.status} ({r.model_calls} model calls, N{r.spend.kobo / 100:,.2f})")
    ex.close(30)
    for sent in ex.sent:
        url = ex.trace_url(sent.trace_id)
        print(f"{sent.name} {sent.verdict} public={sent.public} {url}")
    return 0 if ex.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

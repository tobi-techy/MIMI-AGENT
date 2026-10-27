#!/usr/bin/env python3
"""Router decisions and context-built Collector runs traced to Langfuse.

    LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... python scripts/trace_router_context.py --public

Replays recorded Muse Spark answers only (no model cost):

* eight routing decisions from the router eval, one per layer and guard
  (rule, phrases, fast classifier, model, a clarifying question, a removed
  injection), each a ``route`` trace with its confidence, layer and rule;
* four Collector runs from the context eval's builder arm (an owner's chase,
  a balance ask, a shop that paid, a dispute), each a ``run-collector`` trace
  that carries the route decision, the context manifest and the section
  hashes in its metadata.

Environment ``demo``. With ``--public`` the first trace of each kind is made
public and the links printed. Without the Langfuse keys (or in CI) it exits 2.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mimi.harness import context_eval as ce  # noqa: E402
from mimi.harness import fastclass  # noqa: E402
from mimi.harness import router_eval as re_  # noqa: E402
from mimi.harness.gateway import RecordedToolModel  # noqa: E402
from mimi.harness.router import Router  # noqa: E402
from mimi.harness.tracing import trace_route, trace_run  # noqa: E402
from mimi.observe.langfuse_export import LangfuseExporter, LangfuseSettings  # noqa: E402

ROUTES = ("r012", "r078", "r136", "r125", "r291", "r280", "r031", "r262")
RUNS = ("chase-01", "balance-01", "paid-01", "dispute-01")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--public", action="store_true")
    args = ap.parse_args(argv)
    settings = LangfuseSettings.from_env(environment="demo")
    if settings is None:
        print("Langfuse keys not set (or running in CI): nothing sent.")
        return 2
    made_public: set[str] = set()

    def public_if(turn: object) -> bool:
        kind = type(turn).__name__
        if args.public and kind not in made_public:
            made_public.add(kind)
            return True
        return False

    ex = LangfuseExporter(settings, public_if=public_if)
    world = ce.load_world()
    router = Router(model=re_.make_model(False), fast=fastclass.model())
    cases = {c["id"]: c for c in re_.load_cases()}
    d = re_.directory(world)
    for cid in ROUTES:
        c = cases[cid]
        dec = router.route(re_.inbound(world, c), d, re_.thread_state(c))
        trace_route(dec, ex)
        print(f"route {cid}: {dec.status} {dec.skill} ({dec.layer}, {dec.confidence})")
    rec = RecordedToolModel(ce.BUILDER_CACHE, None, name=ce.MODEL)
    results: dict = {}
    ce.run_arm(world, ce.build(world), "builder", lambda sc: rec, router, results)
    for sid in RUNS:
        r = results[sid]
        trace_run(r, ex, shop_id="owner", goal=sid)
        print(f"run {sid}: {r.status}, context {r.context.get('context_tokens')} tokens")
    ex.close(30)
    for sent in ex.sent:
        print(f"{sent.name} {sent.verdict} public={sent.public} {ex.trace_url(sent.trace_id)}")
    return 0 if ex.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

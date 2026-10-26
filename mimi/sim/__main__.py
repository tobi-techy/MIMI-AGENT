"""``python -m mimi.sim`` runs the live day.

    --shops N          first N shops of the live day (default all 40)
    --hours H          how long the day runs on the sim clock (default 26)
    --realtime SPEED   sleep between events: 1 = real time, 60 = 1h per minute
    --db URL           persist jobs and shop replies (Postgres or SQLite URL)
    --transcript       print the WhatsApp transcript
    --cache PATH       record live model answers to PATH and replay them on a
                       rerun (a long live run can be resumed; tokens reported
                       include the replayed calls' recorded usage)

Models come from the environment (``META_API_KEY``, ``MIMI_MODEL``,
``SIM_MODEL``); with no key both sides use the deterministic FakeModel.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from mimi.models import from_env
from mimi.models.cache import CachedModel
from mimi.receipts.verdict import fake_verdict
from mimi.sim.engine import Simulator
from mimi.sim.persona import fake_shop
from mimi.world import load_world


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.sim")
    ap.add_argument("--shops", type=int, default=None)
    ap.add_argument("--hours", type=float, default=26.0)
    ap.add_argument("--realtime", type=float, default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--transcript", action="store_true")
    ap.add_argument("--cache", default=None)
    args = ap.parse_args(argv)

    world = load_world()
    shop_ids = None
    if args.shops:
        shop_ids = [d.shop_id for d in world.live_deliveries][: args.shops]
    mimi: Any = from_env("mimi", fake=fake_verdict)
    sim: Any = from_env("sim", fake=fake_shop)
    if args.cache and not mimi.name.startswith("fake"):
        mimi = CachedModel(mimi, args.cache + ".mimi.json")
        sim = CachedModel(sim, args.cache + ".sim.json")
    extra: dict[str, Any] = {}
    if args.db:
        import sqlalchemy as sa

        from mimi.receipts.inbox import SqlShopInbox
        from mimi.runtime.sql_store import SqlJobStore

        engine = sa.create_engine(args.db)
        store, inbox = SqlJobStore(engine), SqlShopInbox(engine)
        if args.db.startswith("sqlite"):
            store.ensure_schema()
            inbox.ensure_schema()
        extra = {"store": store, "inbox": inbox}
    started = time.monotonic()
    runner = Simulator(
        world,
        mimi_model=mimi,
        sim_model=sim,
        shop_ids=shop_ids,
        realtime_speed=args.realtime,
        seed=args.seed,
        hours=args.hours,
        **extra,
    )
    report = runner.run()
    report["mimi_model"] = mimi.name
    report["sim_model"] = sim.name
    if isinstance(mimi, CachedModel):
        report["live_calls_this_pass"] = mimi.live_calls + sim.live_calls
        report["replayed_calls"] = mimi.cached_calls + sim.cached_calls
    report["wall_seconds"] = round(time.monotonic() - started, 2)
    if args.transcript:
        print("\n".join(runner.transcript.lines))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

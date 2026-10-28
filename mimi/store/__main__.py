"""``python -m mimi.store seed [--reset] [--db URL] [--tenant-id ID]``

Loads the synthetic Coral Lane Trading world (fictional shops, people and
numbers) into the store. Run ``alembic upgrade head`` first, or pass
``--create`` to create the tables directly (local tries only).
"""

from __future__ import annotations

import argparse
import sys

from mimi.store.db import database_url, ensure_schema, make_engine
from mimi.store.seed import SeedError, seed_world
from mimi.world.loader import load_world


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("seed", help="load the synthetic Coral Lane world")
    sp.add_argument("--db", default="", help="SQLAlchemy URL (default: DATABASE_URL)")
    sp.add_argument("--tenant-id", default="", help="tenant id (default: the world's own)")
    sp.add_argument("--reset", action="store_true", help="replace this tenant's rows")
    sp.add_argument("--if-empty", action="store_true",
                    help="do nothing when the tenant is already seeded")
    sp.add_argument("--create", action="store_true", help="create tables without alembic")
    args = ap.parse_args(argv)
    engine = make_engine(args.db or database_url())
    if args.create:
        ensure_schema(engine)
    world = load_world()
    try:
        counts = seed_world(engine, world, tenant_id=args.tenant_id or None, reset=args.reset)
    except SeedError as exc:
        if args.if_empty and "already seeded" in str(exc):
            print(f"{exc}; nothing to do")
            return 0
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1
    tid = args.tenant_id or world.tenant_id
    print(f"seeded tenant {tid} ({world.distributor}, synthetic): "
          + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

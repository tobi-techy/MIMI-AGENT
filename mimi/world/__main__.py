"""``python -m mimi.world [--seed N] [--out PATH] [--check]``.

Writes the world fixture and prints its hash and stats. ``--check`` exits 1 if
the stats check finds a problem or the committed fixture has drifted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mimi.world import stats
from mimi.world.generator import (
    DEFAULT_SEED,
    generate_world,
    world_hash,
)
from mimi.world.loader import FIXTURE_PATH, load_world, write_world


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.world")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    world = generate_world(args.seed)
    digest = world_hash(world)
    problems = stats.check(world)
    if args.check:
        if args.seed == DEFAULT_SEED and world_hash(load_world(FIXTURE_PATH)) != digest:
            problems.append("committed fixture drifted from the generator")
    else:
        out = args.out or FIXTURE_PATH
        write_world(world, out)
        print(f"wrote {out}")
    print(f"seed {world.seed} sha256 {digest}")
    print(json.dumps(stats.summary(world), indent=2))
    for p in problems:
        print(f"PROBLEM: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

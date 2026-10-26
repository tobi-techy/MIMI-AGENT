"""Write and load the world's JSON fixture.

The committed fixture is ``fixtures/coral_lane_seed2610.json``. A test checks
it is exactly what the generator makes for its seed, so it never drifts.
"""

from __future__ import annotations

import json
from pathlib import Path

from mimi.world.model import World, world_from_dict

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "coral_lane_seed2610.json"


def write_world(world: World, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(world.to_dict(), sort_keys=True, indent=1, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return path


def load_world(path: Path | None = None) -> World:
    raw = json.loads((path or FIXTURE_PATH).read_text(encoding="utf-8"))
    return world_from_dict(raw)

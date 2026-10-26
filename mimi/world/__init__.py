"""Deterministic synthetic world for the demo (Coral Lane Trading).

Synthetic data only: places are real Ado-Ekiti areas, every shop, person,
brand, phone and account number is invented. See ``docs/WORLD.md``.
"""

from mimi.world.generator import (
    DEFAULT_SEED,
    canonical_json,
    generate_world,
    world_hash,
)
from mimi.world.loader import FIXTURE_PATH, load_world, write_world
from mimi.world.model import World

__all__ = [
    "DEFAULT_SEED",
    "FIXTURE_PATH",
    "World",
    "canonical_json",
    "generate_world",
    "load_world",
    "world_hash",
    "write_world",
]

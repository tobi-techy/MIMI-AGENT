"""How an item is named and packed in Mimi's stock messages.

The display name is the SKU name without its pack note ("Sunrise Instant
Noodles 70g (ctn of 40)" -> "Sunrise Instant Noodles 70g"). The pack size
inside one unit ("ctn of 40" -> 40) lets code turn "10 packets left" into
whole cartons. The order multiple is the distributor's usual pack size for a
restock: Coral Lane sells most lines by the unit, bottles in sixes and tins
and pieces by the dozen.
"""

from __future__ import annotations

import re

UNIT_WORDS: dict[str, tuple[str, str]] = {
    "ctn": ("carton", "cartons"),
    "bag": ("bag", "bags"),
    "pk": ("pack", "packs"),
    "btl": ("bottle", "bottles"),
    "keg": ("keg", "kegs"),
    "tin": ("tin", "tins"),
    "pc": ("piece", "pieces"),
    "tub": ("tub", "tubs"),
}
ORDER_MULTIPLE: dict[str, int] = {"btl": 6, "tin": 12, "pc": 12}
_PACK = re.compile(r"\((?:ctn|bag|pack|pk) of (\d+)", re.I)


def display_name(name: str) -> str:
    return name.split(" (", 1)[0].strip()


def unit_word(unit: str, n: int = 2) -> str:
    one, many = UNIT_WORDS.get(unit, (unit, unit))
    return one if n == 1 else many


def pack_size(name: str) -> int | None:
    """Smaller units inside one unit of this SKU, from its name."""
    m = _PACK.search(name)
    return int(m.group(1)) if m else None


def order_multiple(unit: str) -> int:
    return ORDER_MULTIPLE.get(unit, 1)

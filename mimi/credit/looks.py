"""What each shelf item looks like, so code can tell SAM's boxes apart.

SAM 3.1 finds every product on the shelf with one concept prompt
(``"product"``); it does not read brands. Code then puts each box with one of
the shop's own delivered items (the candidate list) by the pack's body colour
and its shape (box height over width). A box that fits no candidate closely
is "other stock" and is never counted or valued.

This table stands in for the distributor's pack photos. Every brand is
invented (``mimi.world.catalogue``). The colours are the packs' printed body
colours as the synthetic shelf renderer draws them; a real distributor would
fill the table from its own product photos.

``pieces`` is how many shelf pieces one sold unit holds: Coral Lane sells
noodles by the carton of 40, so one packet on the shelf is 1/40 of a carton.
The value of a shelf is pieces times the distributor's unit price over
``pieces``, in integer kobo (``mimi.credit.shelf.value_kobo``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from mimi.world.model import Sku

_PACK = re.compile(r"\((?:ctn|bag|pack|pk) of (\d+)", re.I)

Shape = Literal["packet", "can", "bottle", "box", "tub", "pouch"]


@dataclass(frozen=True)
class Look:
    sku: str
    piece: str  # what one shelf piece is called: "packet", "tin", "bottle"
    pieces_word: str
    shape: Shape
    body_rgb: tuple[int, int, int]
    # Box height / width as drawn upright (a range, both ends inclusive).
    aspect: tuple[float, float]


LOOKS: dict[str, Look] = {
    look.sku: look
    for look in (
        Look("NDL-70-CTN", "packet", "packets", "packet", (206, 38, 34), (1.05, 1.6)),
        Look("NDL-120-CTN", "packet", "packets", "packet", (236, 128, 24), (1.05, 1.6)),
        Look("SPAG-500", "packet", "packets", "packet", (110, 150, 214), (1.9, 3.2)),
        Look("MLK-TIN-CTN", "tin", "tins", "can", (24, 46, 118), (1.05, 1.6)),
        Look("SARD-125", "tin", "tins", "can", (18, 138, 136), (0.45, 0.85)),
        Look("OIL-1L", "bottle", "bottles", "bottle", (232, 198, 40), (2.4, 3.6)),
        Look("SODA-50CL", "bottle", "bottles", "bottle", (92, 30, 22), (2.4, 3.6)),
        Look("DET-900G", "pack", "packs", "pouch", (120, 58, 162), (1.1, 1.6)),
        Look("TPASTE-140", "tube box", "tube boxes", "box", (36, 158, 78), (0.3, 0.6)),
        Look("CUSTARD-2KG", "tub", "tubs", "tub", (244, 222, 96), (0.65, 1.0)),
    )
}


def pieces_per_unit(sku: Sku) -> int:
    """Shelf pieces in one unit the distributor sells (1 when sold singly):
    "Sunrise Instant Noodles 70g (ctn of 40)" -> 40."""
    m = _PACK.search(sku.name)
    return int(m.group(1)) if m else 1


def item_name(sku: Sku) -> str:
    """'Sunrise Instant Noodles 70g' (the pack note dropped)."""
    return sku.name.split(" (", 1)[0].strip()

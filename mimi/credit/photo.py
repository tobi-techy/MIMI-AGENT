"""Is this shelf photo good enough to count from? Code decides, before SAM.

Four checks on the decoded image, cheapest first (Pillow, no numpy):

* ``unreadable``: not an image we can decode;
* ``too_small``: the short side is under 480 pixels;
* ``too_dark``: mean brightness under ``DARK_MAX`` (0 to 255);
* ``blurry``: sharpness (variance of the Laplacian on a 640-pixel-wide grey
  copy) under ``BLUR_MIN``.

The thresholds were set on the dev photos only (``eval/credit``): sharp dev
photos score far above ``BLUR_MIN`` and the blurred ones far below it. A
failed check means one ask for a new photo (RAI-271: "Blurry photo gets one
ask for a new photo"), never a count from a bad picture.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Literal

from PIL import Image, ImageFilter, ImageStat, UnidentifiedImageError

Issue = Literal["", "unreadable", "too_small", "too_dark", "blurry"]

MIN_SIDE = 480
DARK_MAX = 45.0
BLUR_MIN = 60.0


@dataclass(frozen=True)
class PhotoCheck:
    usable: bool
    issue: Issue
    width: int = 0
    height: int = 0
    brightness: float = 0.0
    sharpness: float = 0.0

    @property
    def why(self) -> str:
        return {
            "": "clear enough to count",
            "unreadable": "the file could not be opened as a photo",
            "too_small": "the photo is too small to count from",
            "too_dark": "the photo is too dark to see the items",
            "blurry": "the photo is blurry",
        }[self.issue]


def decode(data: bytes) -> Image.Image | None:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    return img.convert("RGB")


def sharpness(img: Image.Image) -> float:
    grey = img.convert("L")
    if grey.width > 640:
        grey = grey.resize((640, max(1, grey.height * 640 // grey.width)))
    lap = grey.filter(ImageFilter.Kernel((3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0),
                                         scale=1, offset=128))
    return float(ImageStat.Stat(lap).var[0])


def check_photo(data: bytes) -> PhotoCheck:
    img = decode(data)
    if img is None:
        return PhotoCheck(False, "unreadable")
    w, h = img.size
    if min(w, h) < MIN_SIDE:
        return PhotoCheck(False, "too_small", w, h)
    bright = float(ImageStat.Stat(img.convert("L")).mean[0])
    if bright < DARK_MAX:
        return PhotoCheck(False, "too_dark", w, h, round(bright, 1))
    sharp = sharpness(img)
    if sharp < BLUR_MIN:
        return PhotoCheck(False, "blurry", w, h, round(bright, 1), round(sharp, 1))
    return PhotoCheck(True, "", w, h, round(bright, 1), round(sharp, 1))

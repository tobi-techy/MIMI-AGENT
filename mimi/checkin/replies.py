"""Reading the check-in reply, in code first: taps, skips, stops, numbers.

One tap answers the whole check-in:

* ``No`` / ``Skip``: nothing stored.
* ``Yes`` with a voice note or receipt photo: stock-in lines, parsed by code
  after the model reads the words (``other.py``).
* A low-stock tap (``0-2`` / ``3-5`` / ``6+``) or a typed number: the count,
  stored through the RAI-294 count submit function (``submit.py``).
* ``stop``: no more check-ins, ever.
* Anything unclear gets one confirm question; no count is stored until
  confirmed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ReplyKind = Literal[
    "no",
    "skip",
    "yes_supplier",
    "count_tap",
    "count_typed",
    "stop",
    "restock_yes",
    "restock_no",
    "unclear",
]

_TAP = re.compile(r"^\s*(0\s*[-to]+\s*2|3\s*[-to]+\s*5|6\s*\+)\s*[.!]?\s*$", re.I)
_DIGITS = re.compile(r"^\s*(\d{1,4})\s*[.!]?\s*$")
_NO = re.compile(
    r"^(?:no|no o|nothing|none|i no buy|i didn't buy|i did not buy|"
    r"nothing this week|ko si|babu|nothing else)[.! ]*$",
    re.I,
)
_SKIP = re.compile(r"^(?:skip|later|not now|busy|i go do am later|ma binu)[.! ]*$", re.I)
_STOP = re.compile(
    r"^(?:stop|stop it|stop all|no more messages|do not message me|"
    r"don't message me|unsubscribe)[.! ]*$",
    re.I,
)
_YES_SUPPLIER = re.compile(
    r"^(?:yes|yes o|yeah|yep|i bought|i buy|i purchased|mo ra|"
    r"na so|bought something|sai na saya)[.! ]*$",
    re.I,
)
_RESTOCK_YES = re.compile(
    r"^(?:yes add it|add it|yes add|ok add|yes please add|"
    r"yes to the van|ehen add am)[.! ]*$",
    re.I,
)
_RESTOCK_NO = re.compile(
    r"^(?:no thanks|no don't add|don't add|do not add|"
    r"no van|not now)[.! ]*$",
    re.I,
)


@dataclass(frozen=True)
class CheckinReply:
    kind: ReplyKind
    bucket: str = ""
    units: int | None = None


def read_reply(text: str) -> CheckinReply:
    """The check-in reply, read in code. Unclear text is ``unclear``: the
    caller asks the model once (``other.py``), then asks one confirm."""
    t = (text or "").strip()
    if not t:
        return CheckinReply("unclear")
    if _STOP.match(t):
        return CheckinReply("stop")
    if _SKIP.match(t):
        return CheckinReply("skip")
    if _NO.match(t):
        return CheckinReply("no")
    m = _TAP.match(t)
    if m:
        raw = m.group(1).replace(" ", "").lower()
        bucket = "0-2" if raw.startswith("0") else "3-5" if raw.startswith("3") else "6+"
        return CheckinReply("count_tap", bucket)
    m = _DIGITS.match(t)
    if m:
        return CheckinReply("count_typed", "", int(m.group(1)))
    if _RESTOCK_YES.match(t):
        return CheckinReply("restock_yes")
    if _RESTOCK_NO.match(t):
        return CheckinReply("restock_no")
    if _YES_SUPPLIER.match(t):
        return CheckinReply("yes_supplier")
    return CheckinReply("unclear")


__all__ = ["CheckinReply", "ReplyKind", "read_reply"]

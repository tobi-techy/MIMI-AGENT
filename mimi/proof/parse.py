"""Turn what the model copied off a screenshot into typed values, in code.

The model copies the amount and the date exactly as printed. These functions
decide what they are: integer kobo, and a datetime in Africa/Lagos. Nothing
here guesses: text that does not parse is ``None`` (unreadable) or an
impossible time, never a "nearest" value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from mimi.proof.banks import ALL_TIME_FORMATS, FictionalBank
from mimi.tz import LAGOS

_AMOUNT = re.compile(
    r"^(?:NGN|N|\u20a6|#)?\s*(?P<whole>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<kobo>\d{2}))?$"
)


def amount_kobo(printed: str | None) -> int | None:
    """'\u20a684,500.00' / 'NGN84,500.00' / 'N 84500' -> 8450000. Else None.

    Commas must group thousands properly ('84,50' is None); a trailing 'DR',
    'CR' or currency word is ignored. Never a float.
    """
    if not printed:
        return None
    s = printed.strip().upper().replace("\u00a0", " ")
    s = re.sub(r"\s*(DR|CR|DEBIT|CREDIT|NAIRA)\.?$", "", s).strip()
    s = s.replace("\u20a6 ", "\u20a6").replace("NGN ", "NGN")
    s = re.sub(r"^(NGN|N|\u20a6|#)\s+", r"\1", s)
    m = _AMOUNT.match(s)
    if not m:
        return None
    whole = int(m.group("whole").replace(",", ""))
    kobo = int(m.group("kobo") or 0)
    return whole * 100 + kobo


@dataclass(frozen=True)
class TimeReading:
    status: str  # "ok", "missing", "impossible"
    at: datetime | None = None
    fmt: str = ""


def _clean_time(printed: str) -> str:
    s = " ".join(printed.replace("|", " ").split())
    s = re.sub(r"\s*(WAT|GMT\+1|\(WAT\))$", "", s, flags=re.I)
    return s


def parse_time(printed: str | None, bank: FictionalBank | None = None) -> TimeReading:
    """A printed date and time, read as Africa/Lagos local time.

    ``impossible`` when there is a date but it fits no bank's format or is
    not a real date or time ("31 Sep 2026", "25:14").
    """
    if not printed or not printed.strip():
        return TimeReading("missing")
    s = _clean_time(printed)
    formats = (bank.time_formats if bank else ()) + ALL_TIME_FORMATS
    for fmt in formats:
        try:
            at = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return TimeReading("ok", at.replace(tzinfo=LAGOS), fmt)
    return TimeReading("impossible")


def last_digits(printed: str | None, n: int = 4) -> str:
    """The last ``n`` digits of a (masked) account number, or ''."""
    digits = re.sub(r"\D", "", printed or "")
    return digits[-n:] if len(digits) >= n else ""


def norm_name(name: str | None) -> str:
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper()).split())


def norm_ref(ref: str | None) -> str:
    return re.sub(r"\s+", "", (ref or "").upper())

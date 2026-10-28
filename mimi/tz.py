"""The one timezone source for every shop-facing day and time (RAI-308).

Shops are in Nigeria: Africa/Lagos, West Africa Time, UTC+1 all year (no
daylight saving). Every module that turns a time into a shop's day, or
writes a time a person reads, takes the zone from here; nothing else builds
its own ``ZoneInfo("Africa/Lagos")`` or ``timezone(timedelta(hours=1))``
(``tests/test_tz.py`` checks).

* ``lagos(dt)``: the same instant on the Lagos clock. A time with no zone is
  refused, so a naive time can never be read as UTC by accident.
* ``assume_lagos(dt)``: for input from outside (a channel's ``received_at``):
  a time with no zone is a Lagos wall-clock time; one with a zone is
  converted.
* ``lagos_day(dt)``: the shop's calendar day for an instant.

Stored timestamps may stay UTC as long as they carry their zone.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ_NAME = "Africa/Lagos"
LAGOS = ZoneInfo(TZ_NAME)
# The same zone under the name the evals and fixtures use.
WAT = LAGOS


def lagos(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"time without a zone: {dt.isoformat()} (use assume_lagos for input)")
    return dt.astimezone(LAGOS)


def assume_lagos(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=LAGOS)
    return dt.astimezone(LAGOS)


def lagos_day(dt: datetime) -> date:
    return lagos(dt).date()


def now_lagos() -> datetime:
    return datetime.now(LAGOS)


__all__ = ["LAGOS", "TZ_NAME", "WAT", "assume_lagos", "lagos", "lagos_day", "now_lagos"]

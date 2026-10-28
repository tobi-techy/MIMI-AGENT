"""Turn the shop's words for a promised payment day into hours for set_followup. Give it the shop's own words ("Friday", "month end", "ojo eti", "next week"); it finds the day in Africa/Lagos and returns in_hours to wake the job at 09:00 WAT on the day after the promised day, the date in words (said_as) and unclear=true when no day can be read. Never count days or hours yourself."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from mimi.voice.promise import find_dates, fold

LAGOS = ZoneInfo("Africa/Lagos")
WAKE_AT = time(9, 0)
MIN_HOURS, MAX_HOURS = 1, 336  # set_followup's own bounds
FIGURES_FROM = "none"


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")

    promised: str = Field(min_length=1, max_length=200,
                          description="The shop's own words for when it will pay.")


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unclear: bool
    promised_on: str = ""  # ISO date, Africa/Lagos
    wake_on: str = ""
    in_hours: int = 0
    said_as: str = ""
    approximate: bool = False
    next_step: str


def _words(d: date) -> str:
    return f"{d:%A} {d.day} {d:%B}"


def run(args: In, now: datetime) -> Out:
    today = now.astimezone(LAGOS).date()
    spans = [s for s in find_dates(fold(args.promised), today) if not s.past]
    if not spans:
        return Out(unclear=True, next_step="No day found in the shop's words. Ask the shop "
                   "which day it will pay, with ask_question.")
    span = spans[-1]
    approximate = False
    if span.on is not None:
        last = span.on
    elif span.options:
        last, approximate = max(span.options), True
    else:
        # "next week" with no day: the follow-up waits for the end of next week.
        monday_next = today + timedelta(days=7 - today.weekday())
        last, approximate = monday_next + timedelta(days=6), True
    if last < today:
        return Out(unclear=True, next_step="That day has passed. Ask the shop which day it "
                   "will pay, with ask_question.")
    wake = last + timedelta(days=1)
    wake_at = datetime.combine(wake, WAKE_AT, tzinfo=LAGOS)
    hours = math.ceil((wake_at - now).total_seconds() / 3600)
    hours = max(MIN_HOURS, min(MAX_HOURS, hours))
    said = ("by " if approximate else "") + _words(last)
    return Out(unclear=False, promised_on=last.isoformat(), wake_on=wake.isoformat(),
               in_hours=hours, said_as=said, approximate=approximate,
               next_step=f"Call set_followup with in_hours={hours}, then acknowledge the "
                         f"promise using said_as.")

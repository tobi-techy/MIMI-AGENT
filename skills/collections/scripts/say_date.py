"""Write an ISO date from a tool result (a due date, a promised day) as words in the shop's language: en "Friday 30 October", pcm "Friday, 30 October", yo "Ọjọ́ Ẹtì, 30 October". Copy its text into the message instead of writing dates yourself."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

FIGURES_FROM = "none"
YORUBA_DAYS = ("Ọjọ́ Ajé", "Ọjọ́ Ìṣẹ́gun", "Ọjọ́rú", "Ọjọ́bọ̀", "Ọjọ́ Ẹtì", "Ọjọ́ Àbámẹ́ta",
               "Ọjọ́ Àìkú")


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iso_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD from a tool.")
    language: Literal["en", "pcm", "yo"] = "en"


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    weekday: str
    relative: str  # "today", "tomorrow", "yesterday", "in 3 days", "3 days ago"


def _relative(d: date, today: date) -> str:
    n = (d - today).days
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n == -1:
        return "yesterday"
    return f"in {n} days" if n > 0 else f"{-n} days ago"


def run(args: In, now: datetime) -> Out:
    d = date.fromisoformat(args.iso_date)
    today = now.astimezone(ZoneInfo("Africa/Lagos")).date()
    if args.language == "yo":
        day = YORUBA_DAYS[d.weekday()]
        text = f"{day}, {d.day} {d:%B}"
    elif args.language == "pcm":
        day = f"{d:%A}"
        text = f"{day}, {d.day} {d:%B}"
    else:
        day = f"{d:%A}"
        text = f"{day} {d.day} {d:%B}"
    if d.year != today.year:
        text += f" {d.year}"
    return Out(text=text, weekday=day, relative=_relative(d, today))

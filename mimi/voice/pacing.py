"""Pacing for a voice reply: where a person would breathe, and nothing read like code.

``pace(script)`` runs on the script ``mimi.voice.render`` made from the text
(amounts and dates already in words). It never changes a word's meaning,
only how it is said:

* a semicolon becomes a sentence break, so the voice stops instead of
  running two sentences together;
* compound numbers are hyphenated as people write them when they mean one
  number ("twenty-five thousand naira", "forty-eight thousand five hundred
  naira"), so the engine does not pause inside a figure;
* a spoken date gets commas around it ("on Thursday, the twenty-ninth of
  October, for this delivery"): a short pause before and after the figure the
  shop has to catch;
* symbols are said as words ("&" -> "and", "%" -> "percent"), a "#" or "*"
  is dropped, and dashes become commas. No em dash is ever written.

The figures check in ``render`` runs again on the paced script: the amounts
and days it reads must still be exactly the text's, or there is no voice.
"""

from __future__ import annotations

import re

_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_UNITS = "one|two|three|four|five|six|seven|eight|nine"
_COMPOUND = re.compile(rf"\b({_TENS}) ({_UNITS})\b", re.I)
_WEEKDAY = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
_MONTH = "January|February|March|April|May|June|July|August|September|October|November|December"
_DATE_START = re.compile(rf"\b({_WEEKDAY}) the ")
_DATE_END = re.compile(rf"\b(of (?:{_MONTH}))(?= [a-z])")
_DASH = re.compile(r"\s*[\u2014\u2013]\s*|\s+-\s+")


def pace(script: str) -> str:
    s = " ".join(script.split())
    s = _DASH.sub(", ", s)
    s = s.replace("&", " and ").replace("%", " percent").replace("#", "").replace("*", "")
    s = re.sub(r";\s*(\w)", lambda m: ". " + m.group(1).upper(), s)
    s = _COMPOUND.sub(lambda m: f"{m.group(1)}-{m.group(2)}", s)
    s = _DATE_START.sub(lambda m: f"{m.group(1)}, the ", s)
    s = _DATE_END.sub(lambda m: f"{m.group(1)},", s)
    s = re.sub(r"\s+([,.?!])", r"\1", s)
    s = re.sub(r",\s*,", ",", s)
    s = " ".join(s.split())
    if s and s[-1] not in ".?!":
        s += "."
    return s

"""A voice reply is the text reply, read aloud. Never a second message.

``render_voice(text, language, today=...)`` turns the exact text Mimi is
sending (a code-written read-back or question, or the body a helper sent
with ``send_to_user`` and the loop already checked) into the script a
speech engine reads. Nothing is generated, translated or summarised:

* every naira amount in the text is said in words, from the same integer
  kobo code reads in the text ("N20,000" -> "twenty thousand naira");
* every written date is said with an ordinal ("Thursday 29 October 2026" ->
  "Thursday the twenty-ninth of October"), because a speech engine reading
  "31 October" was heard back as "thirty fourth"; a weekday written next to
  a date must be that date's weekday, or there is no voice;
* references such as ``dlv-56-9039`` are not read out ("this delivery");
* "today", "tomorrow" and every other word stay exactly as written.

Then code checks the script: the amounts and days read from the script
must be exactly the amounts and days read from the text (and, when the
caller passes them, the code-checked figures of the flow). Anything else,
and the reply goes as text only. The text always goes; the voice note is
an extra.

Which voice: the shop picked a language with ``VOICE ON <LANGUAGE>``. An
English or Pidgin text is spoken in that language's voice with English
speech rules (``"yoruba:en"``: the Yoruba voice reading English, which the
26 Oct probe heard back word for word). A text written in Yoruba, Igbo or
Hausa is spoken by that language's voice only when it carries no figure,
because digits read in those languages are too easy to mishear and a
figure must never exist only as sound.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from mimi.voice.promise import _blank, find_amounts, find_dates, fold, naira_words, spoken_date

MAX_SCRIPT_CHARS = 400
_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_BEFORE = re.compile(r"(?P<wd>" + "|".join(_WEEKDAY_NAMES) + r"),?\s+(?:the\s+)?$", re.I)
_REF = re.compile(r"\b(?P<what>delivery|invoice|order|receipt)\s+(?:no\.?\s*)?[a-z]{2,6}-[\w-]*\d[\w-]*",
                  re.I)
_BARE_REF = re.compile(r"\b[a-z]{2,6}-\d[\w-]*", re.I)
_DIGIT = re.compile(r"\d")

# Marks that a text is written in Yoruba, Igbo or Hausa (not just a loan word).
_YO = re.compile(r"[ẹọṣ]|\b(màá|maa san|ẹ jọ̀ọ́|e jowo|ẹ ṣé|owó|ọjọ́)\b", re.I)
_IG = re.compile(r"[ịụṅ]|\b(biko|daalụ|daalu|ego|ụbọchị)\b", re.I)
_HA = re.compile(r"[ɗƙɓ]|\b(don allah|na gode|kuɗi|kudi|ranar|saƙo)\b", re.I)
_PIDGIN = re.compile(r"\b(abeg|wetin|dey|una|wey|no fit|don land|na so)\b", re.I)


@dataclass(frozen=True)
class VoiceScript:
    script: str
    # The key in ``mimi.voice.speak.VOICES``: "english", "pidgin", "yoruba",
    # or "<language>:en" for that language's voice reading English.
    voice: str
    amounts_kobo: tuple[int, ...]
    days: tuple[date, ...]
    checked: bool = True


def text_language(text: str) -> str:
    """english, pidgin, yoruba, igbo or hausa: what the text is written in."""
    scores = {
        "yoruba": len(_YO.findall(text)),
        "igbo": len(_IG.findall(text)),
        "hausa": len(_HA.findall(text)),
    }
    best = max(scores, key=lambda k: scores[k])
    if scores[best] >= 2:
        return best
    return "pidgin" if _PIDGIN.search(text) else "english"


def voice_for(pref_language: str, text_lang: str) -> str:
    if text_lang in ("english", "pidgin"):
        if pref_language in ("english", "pidgin"):
            return pref_language
        return f"{pref_language}:en"
    return text_lang


def figures(text: str, today: date) -> tuple[Counter[int], Counter[date], bool]:
    """(amounts in kobo, days, anything doubtful) that code reads in ``text``."""
    f = fold(text)
    days = find_dates(f, today)
    amounts = find_amounts(_blank(f, [(d.start, d.end) for d in days]))
    doubtful = any(a.kobo is None or a.options for a in amounts)
    return (
        Counter(a.kobo for a in amounts if a.kobo is not None and not a.options),
        Counter(d.on for d in days if d.on is not None),
        doubtful,
    )


def render_voice(
    text: str,
    pref_language: str,
    *,
    today: date,
    expect_kobo: Sequence[int | None] | None = None,
    expect_days: Sequence[date | None] | None = None,
) -> tuple[VoiceScript | None, str]:
    """(the script, "") or (None, why there is no voice)."""
    body = " ".join((text or "").split())
    if not body:
        return None, "no text"
    text_lang = text_language(body)
    voice = voice_for(pref_language, text_lang)
    text_kobo, text_days, doubtful = figures(body, today)
    if doubtful:
        return None, "the text has a number code cannot read for certain"
    if any(k % 100 for k in text_kobo):
        return None, "an amount with kobo stays text only"
    if text_lang not in ("english", "pidgin"):
        if text_kobo or text_days or _DIGIT.search(_BARE_REF.sub("", body)):
            return None, f"a {text_lang} text with figures stays text only"
        script = _REF.sub(lambda m: f"this {m.group('what').lower()}", body)
        return _checked(script, voice, text_kobo, text_days, today)
    folded = fold(body)
    if len(folded) != len(body):
        return None, "cannot line the text up with its reading"
    edits: list[tuple[int, int, str]] = []
    dates = find_dates(folded, today)
    for a in find_amounts(_blank(folded, [(d.start, d.end) for d in dates])):
        if a.kobo is None:
            continue
        edits.append((a.start, a.end, naira_words(a.kobo)))
    for d in dates:
        if d.on is None or not _DIGIT.search(d.said) and not re.search(
            r"january|february|march|april|may|june|july|august|september|october|november|"
            r"december", d.said
        ):
            continue  # "today", "tomorrow", "Friday": said as written
        start = d.start
        before = _WEEKDAY_BEFORE.search(body[max(0, start - 14) : start])
        if before is not None:
            if before.group("wd").lower() != _WEEKDAY_NAMES[d.on.weekday()]:
                return None, f"the text says {before.group('wd')} but {d.on} is a {d.on:%A}"
            start = start - len(before.group(0))
        edits.append((start, d.end, spoken_date(d.on)))
    edits.sort()
    for (_s1, e1, _), (s2, _e2, _) in zip(edits, edits[1:], strict=False):
        if s2 < e1:
            return None, "two figures overlap in the text"
    out = []
    pos = 0
    for s, e, words in edits:
        out.append(body[pos:s])
        out.append(words)
        pos = e
    out.append(body[pos:])
    script = "".join(out)
    script = _REF.sub(lambda m: f"this {m.group('what').lower()}", script)
    script = _BARE_REF.sub("it", script)
    script = re.sub(r"\bN(?=\s)", "naira", script)
    vs, why = _checked(script, voice, text_kobo, text_days, today)
    if vs is None:
        return vs, why
    if expect_kobo is not None and set(vs.amounts_kobo) != {k for k in expect_kobo if k is not None}:
        return None, "the spoken amount is not the code-checked amount"
    if expect_days is not None and set(vs.days) != {d for d in expect_days if d is not None}:
        return None, "the spoken day is not the code-checked day"
    return vs, ""


def _checked(
    script: str, voice: str, text_kobo: Counter[int], text_days: Counter[date], today: date
) -> tuple[VoiceScript | None, str]:
    if len(script) > MAX_SCRIPT_CHARS:
        return None, f"script is {len(script)} characters, over {MAX_SCRIPT_CHARS}"
    said_kobo, said_days, doubtful = figures(script, today)
    if doubtful:
        return None, "the script has a number code cannot read for certain"
    if said_kobo != text_kobo:
        return None, "the script's amounts are not the text's amounts"
    if set(said_days) != set(text_days):
        return None, "the script's days are not the text's days"
    return VoiceScript(script, voice, tuple(sorted(said_kobo)), tuple(sorted(said_days))), ""

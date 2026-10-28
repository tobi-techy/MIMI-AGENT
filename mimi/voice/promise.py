"""Money and date promises in a shop's words: "I go pay 20k tomorrow".

``read_promise(text, said_at)`` finds a payment promise in one message (a
voice-note transcript or a typed reply) and returns a typed
``PromiseReading``:

* ``promise``: one amount in integer kobo and one due date, resolved in
  Africa/Lagos from the message's own timestamp;
* ``unclear``: the shop spoke about paying but the amount or the day is
  missing, there is more than one, a bare number could mean naira or
  thousands, or the transcript is doubtful. Mimi asks; nothing is recorded;
* ``paid_claim``: the shop says it already paid ("I don pay"). Not a
  promise, and never recorded as one;
* ``none``: nothing about paying.

Everything here is code: no model computes an amount or a date. A reading
is only ever read back to the shop; ``mimi.receipts.promises`` records it
after the shop's own YES. Nothing here moves money.

Covered, case-insensitively and with Yoruba and Igbo tone marks folded:

* amounts: ``20k``, ``20 k``, ``2.5k``, ``20,000``, ``20000``, ``N20,000``,
  ``₦20,000``, ``20,000 naira``, ``20 thousand``, English and Pidgin number
  words ("twenty thousand", "fifteen thousand five hundred", "twenty k"),
  Yoruba ``ẹgbẹ̀rún mẹ́wàá`` (10,000) and ``ẹgbàá`` (2,000 x n), Igbo
  ``puku iri abụọ`` (20,000), Hausa ``dubu ashirin`` (20,000);
* days: today, tomorrow, next tomorrow / day after tomorrow, a weekday,
  "next week Monday", "end of the month", "the 30th", "30th October",
  "30/10"; Yoruba ``òní``, ``ọ̀la``, ``ọ̀túnla`` and weekdays (``Ọjọ́ Ẹtì``),
  Igbo ``taa``, ``echi``, ``nwanne echi`` and loan weekdays (``Fraịdee``),
  Hausa ``yau``, ``gobe``, ``jibi`` and weekdays (``Juma'a``);
* paying: pay, payment, balance, settle, transfer, send the money; Yoruba
  ``san``; Igbo ``akwụ`` / ``kwụọ``; Hausa ``biya`` / ``tura``.

A bare number under 1,000 with no naira mark ("I go pay 20 tomorrow") is
ambiguous: it could be N20 or N20,000. Numbers that count goods ("4
cartons"), refs ("dlv-56-9039"), times ("4pm") and dates are not amounts.
"""

from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

LAGOS = ZoneInfo("Africa/Lagos")
MAX_KOBO = 100_000_000 * 100  # N100m: anything bigger is not a shop's promise
BARE_MIN_NAIRA = 1_000

PromiseStatus = Literal["none", "promise", "unclear", "paid_claim"]
PromiseIssue = Literal[
    "",
    "low_quality_transcript",
    "no_amount",
    "ambiguous_amount",
    "several_amounts",
    "no_date",
    "ambiguous_date",
    "several_dates",
    "past_date",
]


@dataclass(frozen=True)
class PaymentPromise:
    """What the shop said it will pay, and when. ``amount_kobo`` and
    ``due_on`` are computed here in code; the ``*_said`` fields are the
    shop's words they came from (tone marks folded)."""

    amount_kobo: int
    due_on: date
    amount_said: str
    date_said: str


@dataclass(frozen=True)
class PromiseReading:
    status: PromiseStatus
    promise: PaymentPromise | None = None
    # Why it is unclear ("" otherwise).
    issue: PromiseIssue = ""
    # What was found even when unclear, so a follow-up can fill the gap.
    amount_kobo: int | None = None
    due_on: date | None = None
    amount_said: str = ""
    date_said: str = ""
    # When the shop's words fit two values ("two thousand five": N2,500 or
    # N2,005; "next Friday" on a Wednesday: this week's or next week's), the
    # values to ask about. Code never picks one.
    amount_options: tuple[int, ...] = ()
    date_options: tuple[date, ...] = ()

    @property
    def found(self) -> bool:
        return self.status in ("promise", "unclear")


NONE = PromiseReading("none")


# ----------------------------------------------------------------- text


def fold(text: str) -> str:
    """Lower case, tone marks and dots below removed, apostrophes unified."""
    t = unicodedata.normalize("NFD", text.casefold())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.replace("\u2019", "'").replace("\u2018", "'").replace("\u02bc", "'")
    # Hausa hooked letters (ɗ, ƙ, ɓ, ƴ) have no decomposition: map them.
    t = t.translate(_HOOKED)
    return unicodedata.normalize("NFC", t)


_HOOKED = str.maketrans({"\u0257": "d", "\u0199": "k", "\u0253": "b", "\u01b4": "y"})


def lagos_day(at: datetime | str) -> date:
    when = datetime.fromisoformat(at) if isinstance(at, str) else at
    if when.tzinfo is None:
        when = when.replace(tzinfo=LAGOS)
    return when.astimezone(LAGOS).date()


# Words that mean paying in the future (or are neutral) and in the past.
_PAY = re.compile(
    r"\b(pay|paying|payment|settle|balance|transfer|send (?:you )?(?:the )?money|"
    r"san|sanwo|masan|maasan|biya|tura|akwu|akwuo|kwuo|kwu|ugwo|"
    # Heard in the live eval: "zan biya" transcribed as "Zambia".
    r"zambia|zambiya|"
    # Pidgin as it is written and transcribed: "I go pe", "ago pe", "mek ai pe".
    # Bare "pe" is Yoruba "complete", so it needs the Pidgin verb before it.
    r"(?:a?go|mek (?:ai|i)|make i) pe|balans)\b"
)
_PAID = re.compile(
    r"\b(paid|don pay|done pay|don send|don transfer|have paid|i've paid|already pay|"
    r"transferred|ti san|na biya|na tura|kwuola|akwuola|kwuru)\b"
)

_UNITS = (
    r"cartons?|ctns?|bags?|packs?|pks?|tins?|bottles?|btls?|kegs?|tubs?|pieces?|pcs|"
    r"crates?|sachets?|rolls?|dozens?|carton|katan|kwali|ekpe|apo|buhu|"
    r"am|pm|o'clock|oclock|hours?|hrs?|minutes?|mins?|days?|weeks?|months?|years?|"
    r"percent|%"
)
_UNIT_AFTER = re.compile(rf"^\s*(?:{_UNITS})\b")

# ------------------------------------------------------------- numbers

_EN_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "a": 1,
}
_EN_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_HA_NUM = {
    "daya": 1, "biyu": 2, "uku": 3, "hudu": 4, "biyar": 5, "shida": 6,
    "bakwai": 7, "takwas": 8, "tara": 9, "goma": 10, "ashirin": 20,
    "talatin": 30, "arba'in": 40, "arbain": 40, "hamsin": 50, "sittin": 60,
    "saba'in": 70, "sabain": 70, "tamanin": 80, "casa'in": 90, "casain": 90,
}
_IG_UNITS = {
    "otu": 1, "abuo": 2, "ato": 3, "ano": 4, "ise": 5, "isii": 6, "asaa": 7,
    "asato": 8, "itoolu": 9, "itolu": 9,
}
_YO_NUM = {
    "kan": 1, "okan": 1, "meji": 2, "meta": 3, "merin": 4, "marun": 5,
    "marun-un": 5, "mefa": 6, "meje": 7, "mejo": 8, "mesan": 9, "mesan-an": 9,
    "mewa": 10, "mewaa": 10, "mokanla": 11, "mejila": 12, "metala": 13,
    "merinla": 14, "meedogun": 15, "meeedogun": 15, "medogun": 15,
    "marundinlogun": 15, "merindinlogun": 16, "metadinlogun": 17,
    "mejidinlogun": 18, "mokandinlogun": 19, "ogun": 20, "mokanlelogun": 21,
    "mejilelogun": 22, "metalelogun": 23, "merinlelogun": 24, "meedogbon": 25,
    "meeedogbon": 25, "ogbon": 30, "ogoji": 40, "aadota": 50, "adota": 50,
    "ogota": 60, "aadorin": 70, "ogorin": 80, "aadorun": 90, "ogorun": 100,
    "lona ogun": 20,
}


@dataclass(frozen=True)
class AmountSpan:
    start: int
    end: int
    said: str
    kobo: int | None  # None: could be more than one amount (see ``options``)
    # What an amount with kobo None could mean, in kobo, when code can tell.
    options: tuple[int, ...] = ()


def _take_words(words: list[tuple[int, int, str]], i: int, table: dict[str, int]) -> int:
    return table.get(words[i][2], -1) if i < len(words) else -1


Words = list[tuple[int, int, str]]


def _english_number(words: Words, i: int) -> tuple[int, int] | None:
    """(value, words used) for an English number phrase starting at ``i``:
    "twenty", "twenty five", "fifteen thousand five hundred", "a hundred
    thousand", "two hundred and fifty thousand". A phrase stops at a gap
    (a blanked date or a pause mark) and never adds a second, equal or
    larger scale ("ten thousand and ten thousand" is two amounts)."""
    total = cur = 0
    last = ""
    last_scale = 0
    before_and: tuple[int, int, int, str] | None = None
    j = i
    while j < len(words):
        w = words[j][2]
        if j > i and words[j][0] - words[j - 1][1] > 2:
            break
        if w in _EN_UNITS:
            v = _EN_UNITS[w]
            if w == "a" and not _next_is(words, j, ("hundred", "thousand", "million")):
                break
            if last == "unit" or (last == "tens" and not 0 < v < 10):
                break
            cur += v
            last = "unit"
        elif w in _EN_TENS:
            if last in ("unit", "tens"):
                break
            cur += _EN_TENS[w]
            last = "tens"
        elif w == "hundred":
            if last != "unit":
                break
            cur *= 100
            last = "hundred"
        elif w in ("thousand", "million"):
            scale = 1000 if w == "thousand" else 1_000_000
            if last in ("", "scale"):
                break
            if last_scale and scale >= last_scale:
                if before_and is not None:
                    total, cur, j, last = before_and
                break
            total += max(cur, 1) * scale
            cur = 0
            last = "scale"
            last_scale = scale
        elif w == "and" and last in ("hundred", "scale") and _next_is(
            words, j, (*_EN_UNITS, *_EN_TENS)
        ):
            before_and = (total, cur, j, last)
            j += 1
            continue
        else:
            break
        j += 1
    if not last:
        return None
    return total + cur, j - i


def _next_is(words: Words, j: int, options: tuple[str, ...]) -> bool:
    return j + 1 < len(words) and words[j + 1][2] in options


def _hausa_number(words: Words, i: int) -> tuple[int, int] | None:
    v = _take_words(words, i, _HA_NUM)
    if v < 0:
        return None
    j = i + 1
    if v == 10 and j + 1 < len(words) and words[j][2] == "sha":
        u = _take_words(words, j + 1, _HA_NUM)
        if 0 < u < 10:
            return 10 + u, 3
    if v >= 20 and j + 1 < len(words) and words[j][2] == "da":
        u = _take_words(words, j + 1, _HA_NUM)
        if 0 < u < 10:
            return v + u, 3
    return v, 1


def _igbo_number(words: Words, i: int) -> tuple[int, int] | None:
    if i >= len(words):
        return None
    if words[i][2] == "iri":
        tens, j = 1, i + 1
        u = _take_words(words, j, _IG_UNITS)
        if u > 1:
            tens, j = u, j + 1
        value = tens * 10
        if j + 1 < len(words) and words[j][2] == "na":
            u = _take_words(words, j + 1, _IG_UNITS)
            if u > 0:
                return value + u, j + 2 - i
        return value, j - i
    u = _take_words(words, i, _IG_UNITS)
    return (u, 1) if u > 0 else None


def _yoruba_number(words: Words, i: int) -> tuple[int, int] | None:
    if i + 1 < len(words) and words[i][2] == "lona":
        v = _take_words(words, i + 1, _YO_NUM)
        return (v, 2) if v > 0 else None
    v = _take_words(words, i, _YO_NUM)
    return (v, 1) if v > 0 else None


_DIGIT_AMOUNT = re.compile(
    r"(?<![\w\-/.+])(?P<cur>₦\s?|ngn\s?|n(?=\d))?(?P<num>\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\.(?P<dec>\d{1,2}))?(?![\d,]*\d)(?:\s?(?P<suf>k|m|thousand|million|naira|ngn)\b)?"
    r"(?![\-/]\w)"
)
_THOUSAND_BEFORE = re.compile(r"\b(dubu|puku|egberun)\s*(?:naira\s*)?$")


def find_amounts(folded: str) -> list[AmountSpan]:
    """Money amounts in folded text (``fold``). A span whose ``kobo`` is
    ``None`` is a bare small number that may or may not be money."""
    out: list[AmountSpan] = []
    taken: list[tuple[int, int]] = []
    for m in _DIGIT_AMOUNT.finditer(folded):
        start, end = m.span()
        rest = folded[end:]
        cur, suf = m.group("cur"), m.group("suf")
        if not suf and _UNIT_AFTER.match(rest):
            continue
        if not suf and re.match(r"^(st|nd|rd|th)\b", rest):
            continue
        num = int(m.group("num").replace(",", ""))
        dec = m.group("dec")
        value = num * 100 + (int(dec.ljust(2, "0")) if dec else 0)  # kobo
        before = folded[max(0, start - 16) : start]
        marked = bool(cur) or bool(re.search(r"\bnaira\s*$", before))
        thousand = _THOUSAND_BEFORE.search(before)
        if suf in ("k", "thousand"):
            value *= 1000
            marked = True
        elif suf in ("m", "million"):
            value *= 1_000_000
            marked = True
        elif suf in ("naira", "ngn"):
            marked = True
        elif thousand:
            value *= 1000
            marked = True
            start -= len(thousand.group(0))
        taken.append((start, end))
        if not marked and value < BARE_MIN_NAIRA * 100:
            opts: tuple[int, ...] = (value, value * 1000) if not dec else ()
            out.append(AmountSpan(start, end, folded[start:end].strip(), None, opts))
        elif (
            not marked
            and not dec
            and "," not in m.group("num")
            and num >= 1000
            and 0 < num % 1000 < 10
        ):
            # "2005" is how a transcriber writes "two thousand five", which a
            # shop usually means as N2,500 but may mean as N2,005: ask.
            unit = num % 1000
            out.append(AmountSpan(start, end, folded[start:end].strip(), value,
                                  ((num - unit + unit * 100) * 100, value)))
        elif 0 < value <= MAX_KOBO:
            out.append(AmountSpan(start, end, folded[start:end].strip(), value))
    words: Words = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"[a-z][a-z']*", folded)]
    i = 0
    while i < len(words):
        if any(s <= words[i][0] < e for s, e in taken):
            i += 1
            continue
        got = _word_amount(words, i, folded)
        if got is None:
            i += 1
            continue
        kobo, n, opts = got
        start, end = words[i][0], words[i + n - 1][1]
        out.append(AmountSpan(start, end, folded[start:end], kobo, opts))
        taken.append((start, end))
        i += n
    out.sort(key=lambda a: a.start)
    return out


def _word_amount(
    words: Words, i: int, folded: str
) -> tuple[int | None, int, tuple[int, ...]] | None:
    """(kobo, or None when it could be more than one amount; words used;
    the amounts it could be, in kobo), or None."""
    got = _word_amount_one(words, i, folded)
    if got is None:
        return None
    kobo, n = got
    if kobo is None:
        en = _english_number(words, i)
        if en is not None and en[1] == n and 0 < en[0] < BARE_MIN_NAIRA:
            return None, n, (en[0] * 100, en[0] * 1000 * 100)
        return None, n, ()
    # Hausa "dubu biyu da dari biyar" (2,500), Igbo "puku abuo na nari ise".
    j = i + n
    if (
        j + 2 < len(words)
        and words[j][2] in ("da", "na")
        and words[j + 1][2] in ("dari", "nari")
        and kobo % 100_000 == 0
    ):
        reader = _hausa_number if words[j + 1][2] == "dari" else _igbo_number
        h = reader(words, j + 2) or _english_number(words, j + 2)
        if h is not None and 0 < h[0] < 10:
            return kobo + h[0] * 100 * 100, n + 2 + h[1], ()
    return kobo, n, _thousand_and_unit(words, i, n, kobo)


def _thousand_and_unit(words: Words, i: int, n: int, kobo: int) -> tuple[int, ...]:
    """Nigerian speech: "two thousand five" usually means N2,500, but it can be
    N2,005. When an English number ends "<n> thousand <one to nine>" with no
    "hundred", return both readings so Mimi asks; else ()."""
    used = [w[2] for w in words[i : i + n]]
    if len(used) < 3 or used[-2] != "thousand" or used[-1] not in _EN_UNITS:
        return ()
    if "hundred" in used or used[-1] == "a":
        return ()
    unit = _EN_UNITS[used[-1]]
    if not 0 < unit < 10:
        return ()
    base = kobo - unit * 100
    return (base + unit * 100 * 100, kobo)


def _word_amount_one(words: Words, i: int, folded: str) -> tuple[int | None, int] | None:
    """(kobo, or None for a bare small number; words used), or None."""
    w = words[i][2]
    # Hausa: [naira] dubu <n>; Igbo: puku [naira] <n>; Yoruba: egberun <n>, egbaa <n>
    if w in ("dubu", "puku", "egberun", "egbaa"):
        j = i + 1
        if j < len(words) and words[j][2] == "naira":
            j += 1
        reader = {"dubu": _hausa_number, "puku": _igbo_number}.get(w, _yoruba_number)
        got = reader(words, j) or _english_number(words, j)
        unit = 2000 if w == "egbaa" else 1000
        if got is None:
            if w == "egbaa":
                return None
            # "dubu" alone is a thousand; "dubu <a word we cannot read>" may
            # be a misheard number, so it is not an amount we can read back.
            if j < len(words) and folded[words[j - 1][1] : words[j][0]].strip() == "":
                return None, j - i
            return unit * 100, j - i
        value, n = got
        return value * unit * 100, j + n - i
    got = _english_number(words, i)
    if got is None:
        return None
    value, n = got
    if value <= 0:
        return None
    j = i + n
    nxt = words[j][2] if j < len(words) else ""
    if nxt == "k":
        return value * 1000 * 100, n + 1
    if nxt == "naira":
        return value * 100, n + 1
    if _UNIT_AFTER.match(folded[words[j - 1][1] :]):
        return None
    if value >= BARE_MIN_NAIRA:
        return value * 100, n
    if n == 1 and w in ("a", "one"):
        return None  # "the one wey land", "one of them"
    return None, n


# --------------------------------------------------------------- dates

_MONTHS = {
    m: i
    for i, names in enumerate(
        (
            ("january", "jan"), ("february", "feb"), ("march", "mar"),
            ("april", "apr"), ("may",), ("june", "jun"), ("july", "jul"),
            ("august", "aug"), ("september", "sept", "sep"), ("october", "oct"),
            ("november", "nov"), ("december", "dec"),
        ),
        start=1,
    )
    for m in names
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ORD_UNITS = (
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
    "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth"
).split()
_ORDINALS: dict[str, int] = {w: i for i, w in enumerate(_ORD_UNITS, start=1)}
_ORDINALS.update({"twentieth": 20, "thirtieth": 30})
for _tens, _base in (("twenty", 20), ("thirty", 30)):
    for _i, _w in enumerate(_ORD_UNITS[:9], start=1):
        if _base + _i <= 31:
            _ORDINALS[f"{_tens}-{_w}"] = _base + _i
            _ORDINALS[f"{_tens} {_w}"] = _base + _i
_ORDINAL_RE = "|".join(sorted((re.escape(w) for w in _ORDINALS), key=len, reverse=True))
_WEEKDAYS: dict[str, int] = {
    "monday": 0, "tuesday": 1, "tues": 1, "wednesday": 2, "weds": 2,
    "thursday": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5,
    "sunday": 6,
    # Yoruba (folded); bare "aje", "eti", "aiku" are other words, so "ojo" is required.
    "ojo aje": 0, "ojo isegun": 1, "isegun": 1, "ojoru": 2, "ojo ru": 2,
    "ojobo": 3, "ojo bo": 3, "ojo eti": 4, "ojo abameta": 5, "abameta": 5,
    "ojo aiku": 6,
    # Hausa
    "litinin": 0, "talata": 1, "laraba": 2, "alhamis": 3, "juma'a": 4,
    "jumma'a": 4, "jumaa": 4, "juma": 4, "asabar": 5, "lahadi": 6,
    # Igbo loan words (folded)
    "monde": 0, "tuzdee": 1, "tuzde": 1, "wenezdee": 2, "wenezde": 2,
    "tozdee": 3, "tozde": 3, "fraidee": 4, "fraide": 4, "satodee": 5, "satode": 5,
    "sondee": 6, "sonde": 6,
}
_WEEKDAY_RE = "|".join(sorted((re.escape(w) for w in _WEEKDAYS), key=len, reverse=True))
_DAY_AFTER = r"next tomorrow|nex tomorrow|nest tomorrow|day after tomorrow|otunla|jibi|nwanne echi"
_TOMORROW = r"tomorrow|tomoro|tomorow|tmrw|tmr|2moro|2morow|ola|lola|gobe|echi"
_TODAY = r"today|2day|tday|lonii|loni|oni|yau|taa|this evening|tonight"
_PAST = r"yesterday|yestaday|jiya|unyaahu|lanaa|lana"
_WEEKEND = (
    r"(?:this |dis |next |by |for |on )?(?:the )?(?:weekend|wi+kendi?|week end)|ipari ose|karshen mako"
)
_NEXT_WEEK = r"(?:next|nex|nest) (?:week|wik|wiik)"
_MONTH_END = r"end of (?:the )?month|month end|month-end|ending of (?:the )?month|ipari osu|karshen wata"

_DATE_RE = re.compile(
    rf"(?P<ow>\b(?:the\s+)?(?P<o1>{_ORDINAL_RE})\s+(?:of\s+)?(?P<m4>{_MONTH_RE})\b)"
    rf"|(?P<dm>\b(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?\s+(?P<m1>{_MONTH_RE})\b(?:,?\s+(?P<y1>\d{{4}}))?)"
    rf"|(?P<md>\b(?P<m2>{_MONTH_RE})\s+(?P<d2>\d{{1,2}})(?![\d,.]\d)(?:st|nd|rd|th)?\b(?:,?\s+(?P<y2>\d{{4}}))?)"
    rf"|(?P<slash>(?<![\w/])(?P<d3>\d{{1,2}})/(?P<m3>\d{{1,2}})(?:/(?P<y3>\d{{2,4}}))?(?![\w/]))"
    rf"|(?P<the>\bthe\s+(?P<d4>\d{{1,2}})(?:st|nd|rd|th)\b(?!\s+(?:of\s+)?(?:{_MONTH_RE})\b))"
    rf"|(?P<ord>\b(?:on|by)\s+(?P<d5>\d{{1,2}})(?:st|nd|rd|th)\b(?!\s+(?:of\s+)?(?:{_MONTH_RE})\b))"
    rf"|(?P<ordw>\b(?:on|by)\s+the\s+(?P<o2>{_ORDINAL_RE})\b(?!\s+(?:of\s+)?(?:{_MONTH_RE}|week|day|time|one|ones|carton|cartons|bag|bags|month)\b))"
    rf"|(?P<weekend>\b(?:{_WEEKEND})\b)"
    rf"|(?P<after>\b(?:{_DAY_AFTER})\b)"
    rf"|(?P<tomorrow>\b(?:{_TOMORROW})\b)"
    rf"|(?P<today>\b(?:{_TODAY})\b)"
    rf"|(?P<past>\b(?:{_PAST})\b)"
    rf"|(?P<monthend>\b(?:{_MONTH_END})\b)"
    rf"|(?P<wdnext>\b(?P<wd3>{_WEEKDAY_RE})\s+{_NEXT_WEEK}\b)"
    rf"|(?P<nextweek>\b{_NEXT_WEEK}\b(?:\s+(?:on\s+)?(?P<wd1>{_WEEKDAY_RE})\b)?)"
    rf"|(?P<wd>\b(?:(?P<mod>next|this|coming|on|ranar|ni|na|ojo)\s+|na(?=(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b))?(?P<wd2>{_WEEKDAY_RE})\b)"
)


@dataclass(frozen=True)
class DateSpan:
    start: int
    end: int
    said: str
    on: date | None  # None: ambiguous ("next week", the same weekday as today)
    past: bool = False
    # When ``on`` is None but the words fit exactly these days.
    options: tuple[date, ...] = ()


def _resolve_dm(today: date, day: int, month: int, year: int | None) -> date | None:
    for y in ((year,) if year else (today.year, today.year + 1)):
        try:
            d = date(y, month, day)
        except ValueError:
            return None
        if year or d >= today:
            return d
    return None


def find_dates(folded: str, today: date) -> list[DateSpan]:
    out: list[DateSpan] = []
    for m in _DATE_RE.finditer(folded):
        s, e = m.span()
        said = m.group(0).strip()
        g = m.groupdict()
        on: date | None = None
        past = False
        options: tuple[date, ...] = ()
        if g["ow"]:
            on = _resolve_dm(today, _ORDINALS[g["o1"]], _MONTHS[g["m4"]], None)
        elif g["dm"] or g["md"]:
            day = int(g["d1"] or g["d2"])
            month = _MONTHS[g["m1"] or g["m2"]]
            year = g["y1"] or g["y2"]
            on = _resolve_dm(today, day, month, int(year) if year else None)
        elif g["slash"]:
            day, month = int(g["d3"]), int(g["m3"])
            y = g["y3"]
            year_v = (2000 + int(y) if y and len(y) == 2 else int(y)) if y else None
            on = _resolve_dm(today, day, month, year_v) if 1 <= month <= 12 else None
        elif g["the"] or g["ord"] or g["ordw"]:
            day = int(g["d4"] or g["d5"]) if not g["ordw"] else _ORDINALS[g["o2"]]
            if day >= today.day:
                on = _resolve_dm(today, day, today.month, today.year)
            else:
                nm = today.month % 12 + 1
                ny = today.year + (1 if today.month == 12 else 0)
                on = _resolve_dm(today, day, nm, ny)
        elif g["after"]:
            on = today + timedelta(days=2)
        elif g["tomorrow"]:
            on = today + timedelta(days=1)
        elif g["today"]:
            on = today
        elif g["past"]:
            on, past = today - timedelta(days=1), True
        elif g["weekend"]:
            sat = today + timedelta(days=(5 - today.weekday()) % 7)
            if "next" in g["weekend"]:
                sat += timedelta(days=7)
            options = tuple(d for d in (sat, sat + timedelta(days=1)) if d >= today)
            if today.weekday() == 6 and "next" not in g["weekend"]:
                options = (today,)
            if len(options) == 1:
                on, options = options[0], ()
        elif g["monthend"]:
            on = date(today.year, today.month, monthrange(today.year, today.month)[1])
        elif g["nextweek"] or g["wdnext"]:
            if g["wd1"] or g["wd3"]:
                wd = _WEEKDAYS[g["wd1"] or g["wd3"]]
                monday_next = today + timedelta(days=7 - today.weekday())
                on = monday_next + timedelta(days=wd)
        elif g["wd"]:
            wd = _WEEKDAYS[g["wd2"]]
            ahead = (wd - today.weekday()) % 7
            coming = today + timedelta(days=ahead or 7)
            if g["mod"] == "next" and coming.isocalendar()[:2] == today.isocalendar()[:2]:
                # "next Friday" said on a Wednesday: this Friday or the one
                # after? People mean both; Mimi asks.
                options = (coming, coming + timedelta(days=7))
            elif ahead == 0:
                if g["mod"] == "next":
                    on = coming
                else:
                    options = (today, coming)
            else:
                on = coming
        if on is not None and on < today:
            past = True
        out.append(DateSpan(s, e, said, on, past, options))
    # A weekday written next to an explicit date ("Tuesday 27 October") is the
    # same day: keep the explicit date only.
    explicit = [d for d in out if _is_explicit(d.said)]
    return [
        d
        for d in out
        if _is_explicit(d.said) or not any(0 <= x.start - d.end <= 6 for x in explicit)
    ]


def _is_explicit(said: str) -> bool:
    return bool(re.search(r"\d", said) or re.search(rf"\b(?:{_MONTH_RE})\b", said))


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for s, e in spans:
        for k in range(s, e):
            chars[k] = " "
    return "".join(chars)


# ------------------------------------------------------------- reading


_CLAUSE_SPLIT = re.compile(r"[!?;\n]+|\.(?!\d)|,\s+|\s+but\s+")


def _clause_ranges(text: str) -> list[tuple[int, int]]:
    out, pos = [], 0
    for m in _CLAUSE_SPLIT.finditer(text):
        if text[pos : m.start()].strip():
            out.append((pos, m.start()))
        pos = m.end()
    if text[pos:].strip():
        out.append((pos, len(text)))
    return out


def _is_future(clause: str) -> bool:
    return bool(_PAY.search(_PAID.sub(" ", clause)))


def read_promise(
    text: str,
    said_at: datetime | str,
    *,
    transcript_ok: bool = True,
    answering: bool = False,
    known_kobo: int | None = None,
    known_due: date | None = None,
) -> PromiseReading:
    """Read one message.

    ``transcript_ok=False`` (a voice note whose transcript is ``low``): a
    payment promise in it is ``unclear``, never a promise.
    ``answering=True``: Mimi asked the shop about a promise, so no pay word
    is needed, and ``known_kobo`` / ``known_due`` (what was already heard)
    fill whatever this message does not say.
    """
    today = lagos_day(said_at)
    folded = fold(text)
    dates = find_dates(folded, today)
    no_dates = _blank(folded, [(d.start, d.end) for d in dates])
    ranges = _clause_ranges(no_dates)
    if answering:
        scope = [(0, len(no_dates))]
    else:
        scope = [(s, e) for s, e in ranges if _is_future(no_dates[s:e])]
        if not scope:
            if any(_PAID.search(no_dates[s:e]) for s, e in ranges):
                return PromiseReading("paid_claim")
            return NONE
    if not transcript_ok:
        return PromiseReading("unclear", issue="low_quality_transcript")
    amounts = [a for s, e in scope for a in find_amounts(no_dates[s:e])]
    # Dates in the clauses about paying; else any date not in a clause about
    # having paid already.
    in_scope = [d for d in dates if any(s <= d.start < e for s, e in scope)]
    if not in_scope and not answering:
        past = [(s, e) for s, e in ranges if _PAID.search(no_dates[s:e])]
        in_scope = [d for d in dates if not any(s <= d.start < e for s, e in past)]
    unsure = [a for a in amounts if a.kobo is None or a.options]
    money = {a.kobo for a in amounts if a.kobo is not None and not a.options}
    amount: int | None = None
    amount_said = ""
    amount_options: tuple[int, ...] = ()
    issue: PromiseIssue = ""
    if len(money) + len(unsure) > 1:
        issue = "several_amounts"
    elif unsure:
        issue = "ambiguous_amount"
        amount_options = unsure[0].options
        amount_said = unsure[0].said
    elif money:
        amount = money.pop()
        amount_said = next(a.said for a in amounts if a.kobo == amount)
    elif known_kobo is not None:
        amount = known_kobo
    else:
        issue = "no_amount"
    days = {d.on for d in in_scope if d.on is not None}
    due: date | None = None
    date_said = ""
    date_options: tuple[date, ...] = ()
    date_issue: PromiseIssue = ""
    vague = [d for d in in_scope if d.on is None]
    if vague and len(in_scope) == 1:
        date_issue = "ambiguous_date"
        date_options = tuple(o for o in vague[0].options if o >= today)
        date_said = vague[0].said
    elif vague:
        date_issue = "several_dates" if days or len(vague) > 1 else "ambiguous_date"
    elif len(days) > 1:
        date_issue = "several_dates"
    elif days:
        due = days.pop()
        date_said = next(d.said for d in in_scope if d.on == due)
        if due < today:
            date_issue, due = "past_date", None
    elif known_due is not None:
        due = known_due
    else:
        date_issue = "no_date"
    issue = issue or date_issue
    if issue or amount is None or due is None:
        return PromiseReading(
            "unclear",
            issue=issue or "no_amount",
            amount_kobo=amount,
            due_on=None if date_issue else due,
            amount_said=amount_said,
            date_said=date_said,
            amount_options=amount_options if len(amount_options) > 1 else (),
            date_options=date_options if len(date_options) > 1 else (),
        )
    return PromiseReading(
        "promise",
        PaymentPromise(amount, due, amount_said, date_said),
        amount_kobo=amount,
        due_on=due,
        amount_said=amount_said,
        date_said=date_said,
    )


# --------------------------------------------------------- the shop's YES

_YES = re.compile(
    r"^\W*(yes+|yeah|yea|yep|yup|correct|exactly|na so|e correct|that is right|"
    r"it is right|that's right|beeni|bee ni|ee|eh|ehen|naam|na'am|haka ne|"
    r"gaskiya ne|daidai ne|o di mma|o ti to)\b"
)
_NOT_YES = re.compile(r"\b(but|not|isn't|wasn't|instead|change|no be|mba|a'a|rara|ba haka)\b")


def is_yes(text: str) -> bool:
    """A plain YES to a read-back, in code: starts with yes (or a yes word in
    Pidgin, Yoruba, Igbo or Hausa) and has no "but", "not", "instead"."""
    f = fold(text)
    return bool(_YES.search(f)) and not _NOT_YES.search(f)


# ------------------------------------------------------------ for speech

_ONES = (
    "", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def _under_1000(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} hundred")
        n %= 100
        if n:
            parts.append("and")
    if n >= 20:
        parts.append(_TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else ""))
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


def naira_words(kobo: int) -> str:
    """'twenty thousand naira' (whole naira; kobo said as 'and 50 kobo')."""
    naira, k = divmod(kobo, 100)
    if naira == 0:
        words = "zero"
    else:
        chunks = []
        for value, name in ((1_000_000_000, "billion"), (1_000_000, "million"), (1000, "thousand")):
            if naira >= value:
                chunks.append(f"{_under_1000(naira // value)} {name}")
                naira %= value
        if naira:
            if chunks and naira < 100:
                chunks.append("and")
            chunks.append(_under_1000(naira))
        words = " ".join(chunks)
    out = f"{words} naira"
    if k:
        out += f" and {_under_1000(k)} kobo"
    return out


def ordinal_words(n: int) -> str:
    return next(w for w, v in _ORDINALS.items() if v == n and " " not in w)


def spoken_date(d: date) -> str:
    """'Saturday the thirty-first of October': ordinal words, because a
    speech engine reading "31 October" was heard back as "thirty fourth"."""
    return f"{d:%A} the {ordinal_words(d.day)} of {d:%B}"

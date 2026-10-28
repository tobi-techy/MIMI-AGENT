"""The shop's own words, read by code: folding, numbers, quotes (RAI-302).

The model proposes order lines; code checks every one against what the shop
actually wrote, said or listed on its photo. Three helpers do that:

* ``fold``: lower case, tone marks and accents removed, units glued to their
  numbers ("5 Kilo" -> "5kg", "1 litre" -> "1l"), punctuation to spaces.
* ``numbers_in``: every whole number in a text, in figures or words (English,
  Pidgin, Yoruba, Hausa, Igbo).
* ``quoted``: whether a quote is really in the shop's words (every word of
  it appears there, in any order).
"""

from __future__ import annotations

import re
import unicodedata

NUMBER_WORDS: dict[str, int] = {
    # English and Pidgin
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100, "dozen": 12,
    # Yoruba (the counting forms; "eji", "eta" alone are left out: "ejo" means please)
    "okan": 1, "ookan": 1, "meji": 2, "meta": 3, "merin": 4, "marun": 5, "mefa": 6,
    "meje": 7, "mejo": 8, "mesan": 9, "mewa": 10, "mokanla": 11, "mejila": 12, "ogun": 20,
    # Hausa
    "daya": 1, "biyu": 2, "uku": 3, "hudu": 4, "biyar": 5, "shida": 6, "bakwai": 7,
    "takwas": 8, "tara": 9, "goma": 10,
    # Igbo
    "abuo": 2, "ato": 3, "ano": 4, "isii": 6, "asaa": 7, "asato": 8, "itoolu": 9, "iri": 10,
}
# "a carton", "one bag": a single unit.
ONE_WORDS = ("a", "an", "single")

_UNIT_GLUE = (
    (r"(\d+)\s*(?:kilos?|kgs?|kilograms?)\b", r"\1kg"),
    (r"(\d+)\s*(?:grams?|gms?|g)\b", r"\1g"),
    (r"(\d+)\s*(?:litres?|liters?|ltrs?|l)\b", r"\1l"),
    (r"(\d+)\s*(?:cl)\b", r"\1cl"),
)


def fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("\u2019", "'").replace("&", " and ")
    # "five kilo" -> "5 kilo" (then glued below): a size, never a count.
    t = re.sub(r"\b(" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) +
               r")\s+(?=(?:kilos?|kgs?|kilograms?|litres?|liters?|ltrs?|grams?)\b)",
               lambda m: f"{NUMBER_WORDS[m.group(1)]} ", t)
    for pat, rep in _UNIT_GLUE:
        t = re.sub(pat, rep, t)
    t = re.sub(r"(\d)\s*x\b", r"\1 ", t)  # "6x" -> "6"
    t = re.sub(r"\bx\s*(\d)", r" \1", t)  # "x6" -> "6"
    t = re.sub(r"[^a-z0-9']+", " ", t)
    return " ".join(t.split())


def tokens(text: str) -> list[str]:
    return fold(text).split()


def numbers_in(text: str) -> set[int]:
    """Whole numbers in a text, figures or words. Sizes glued to a unit
    ("5kg", "70g") count too: a quote's quantity must be one of these."""
    t = fold(text)
    found = {int(x) for x in re.findall(r"(?<![\d.])(\d{1,4})(?:kg|g|l|cl)?\b", t)}
    words = set(t.split())
    found |= {n for w, n in NUMBER_WORDS.items() if w in words}
    if words & set(ONE_WORDS):
        found.add(1)
    return found


def quantities_in(text: str) -> set[int]:
    """Numbers that can be a count: sizes glued to a unit ("5kg") are not."""
    t = fold(text)
    found = {int(x) for x in re.findall(r"(?<![\d.a-z])(\d{1,4})(?![\da-z])", t)}
    words = set(t.split())
    found |= {n for w, n in NUMBER_WORDS.items() if w in words}
    if words & set(ONE_WORDS):
        found.add(1)
    return found


def quoted(quote: str, said: str) -> bool:
    """True when every word of ``quote`` is in ``said`` (order free)."""
    q = tokens(quote)
    if not q:
        return False
    have = set(tokens(said))
    return all(w in have for w in q)


def missing_words(quote: str, said: str) -> list[str]:
    have = set(tokens(said))
    return [w for w in tokens(quote) if w not in have]


_PAYS_NOW = re.compile(
    r"\b(?:pay|paying|payment|cash|transfer|send (?:the )?money)\b.{0,40}\b(?:before|on|when|once|as)\b"
    r".{0,25}\b(?:deliver\w*|van|come|comes|coming|arrive\w*|reach\w*|bring|supply)\b"
    r"|\bcash on delivery\b|\bpay (?:now|first|upfront|up front|immediately)\b"
    r"|\bpay before\b|\b(?:i|we) (?:don|have|has|already) (?:pay|paid)\b")


def pays_now_in(text: str) -> bool:
    """The shop says it pays before or on delivery (no credit used)."""
    return bool(_PAYS_NOW.search(fold(text)))

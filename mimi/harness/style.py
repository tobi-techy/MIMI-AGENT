"""The last word on how a message reads, in code (RAI-303).

Every message Mimi sends passes ``clean_reply`` on its way out: the
``send_to_user`` and ``ask_question`` tools, the loop's own owner questions
and the API's outbound writer. It is deterministic and does two things only:

* **No dashes.** Em and en dashes (and the double hyphen) are replaced: a
  dash between two numbers ("3–5") becomes a plain hyphen, a dash at the
  start of a line becomes a list hyphen, any other dash becomes a comma (or
  nothing, next to other punctuation).
* **No filler.** A fixed list of AI and call-centre phrases is removed
  ("I'd be happy to help", "As an AI", "Please don't hesitate to reach
  out"). A whole filler sentence goes; an opener ("Certainly! ") is cut and
  the next word capitalised.

It never touches what a message says: digits, naira figures, dates,
references and names are compared before and after, and when they differ in
any way the dash-free original is kept. A message that would be left empty
is kept as it was (dash-free). ``MIMI_STYLE_GUARD=off`` turns it off.

This is a guard, not the voice: the voice is in the prompts
(``loop.CORE_RULES``) and the code templates, written to
``docs/MIMI-VOICE.md``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# U+2012 figure dash, U+2013 en dash, U+2014 em dash, U+2015 horizontal bar,
# U+2E3A/U+2E3B two- and three-em dashes, U+FE58 small em dash, U+FE63 small
# hyphen-minus is left alone (it is a hyphen).
DASHES = "\u2012\u2013\u2014\u2015\u2e3a\u2e3b\ufe58"
# A double hyphen used as a dash: between words or spaced ("word--word",
# "word -- word"); never inside a longer run of hyphens or an arrow.
_DASH = f"[{DASHES}]+|(?<=[\\w ])--(?=[\\w ])"

# Phrases that make a message sound like a bot or a call centre. Each is a
# whole sentence (removed with its end mark) or an opener (cut from the
# front of a sentence). Case-insensitive; apostrophes straight or curly.
_A = "['\u2019]"
FILLER_SENTENCES: tuple[str, ...] = (
    rf"I{_A}?d be (?:happy|glad|delighted) to help(?: you)?(?: with (?:that|this))?",
    r"I would be (?:happy|glad|delighted) to help(?: you)?(?: with (?:that|this))?",
    rf"I{_A}?m (?:happy|glad|delighted|here) to help(?: you)?(?: with (?:that|this))?",
    r"(?:I am )?(?:always )?happy to help",
    r"I hope (?:this|my) (?:message|email|note) finds you well",
    rf"I hope you{_A}?re (?:doing )?well",
    rf"(?:Please )?(?:don{_A}?t|do not) hesitate to (?:reach out|contact (?:me|us)|ask|let me know)"
    r"(?: (?:if|should|when) [^.!?\n]*)?",
    r"(?:Please )?feel free to (?:reach out|contact (?:me|us)|ask|let me know)"
    r"(?: (?:if|should|when) [^.!?\n]*)?",
    rf"(?:Please )?let me know if (?:there{_A}?s|there is) anything else(?: I can (?:do|help with)"
    r"(?: for you)?)?",
    r"(?:Is there )?anything else I can (?:help (?:you )?with|do for you)",
    r"If you have any (?:other |further |more )?questions(?:,| or concerns,)? "
    r"(?:please )?(?:let me know|feel free to ask|just ask|reach out)",
    r"Thank you for (?:reaching out|your patience|your understanding|contacting us)",
    r"Thanks for (?:reaching out|your patience|your understanding|contacting us)",
    r"I (?:apologi[sz]e|am sorry|'m sorry) for any (?:inconvenience|confusion)(?: (?:this|caused)[^.!?\n]*)?",
    r"Rest assured[^.!?\n]*",
    r"I understand (?:your (?:concern|frustration)|how (?:you feel|frustrating[^.!?\n]*))",
    r"Great question",
    r"Good question",
)
FILLER_OPENERS: tuple[str, ...] = (
    r"As an AI(?: (?:language model|assistant|model))?,?",
    r"Certainly[!.,]",
    r"Absolutely[!.,]",
    r"Of course[!]",
    r"Great question[!.,]",
    r"Good question[!.,]",
    r"Sure thing[!.,]",
)
# A banned phrase anywhere in the text, for checks and the eval (the guard
# removes the sentence and opener forms above; these are what it looks for).
BANNED: tuple[str, ...] = (
    "happy to help", "glad to help", "delighted to help", "as an ai", "an ai language model",
    "hope this message finds you", "hope this finds you", "don't hesitate", "do not hesitate",
    "feel free to reach out", "feel free to contact", "anything else i can help",
    "rest assured", "thank you for reaching out", "thanks for reaching out",
    "apologize for any inconvenience", "apologise for any inconvenience",
    "great question", "good question", "certainly!", "absolutely!",
)

_END = r"(?:[.!?]+|(?=\n)|$)"
_SENTENCE_RES = tuple(
    re.compile(rf"(?:^|(?<=[.!?:,\n]))[ \t]*(?:{p})[ \t]*{_END}[ \t]*",
        re.IGNORECASE | re.MULTILINE)
    for p in FILLER_SENTENCES
)
_OPENER_RES = tuple(
    re.compile(rf"(?:^|(?<=[.!?\n]))[ \t]*(?:{p})[ \t]*", re.IGNORECASE | re.MULTILINE)
    for p in FILLER_OPENERS
)
# Everything a message must keep exactly: every run of digits with its
# separators (amounts, dates, times, phone-like refs) and naira marks.
_FIGURE = re.compile(r"(?:N|\u20a6|NGN\s?)?\d[\d,.:/]*\d|\d")


def figures(text: str) -> list[str]:
    """The figures in a text, in order, normalised (no trailing separators)."""
    return [m.group(0) for m in _FIGURE.finditer(text)]


def has_dash(text: str) -> bool:
    return bool(re.search(_DASH, text))


def banned_in(text: str) -> list[str]:
    t = text.lower().replace("\u2019", "'")
    return [p for p in BANNED if p in t]


@dataclass
class Cleaned:
    text: str
    dashes: int = 0
    phrases: list[str] = field(default_factory=list)
    kept_original: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.dashes or self.phrases)


def _dash_sub(m: re.Match[str]) -> str:
    s, i, j = m.string, m.start(), m.end()
    before = s[:i].rstrip(" \t")
    after = s[j:].lstrip(" \t")
    prev = before[-1:] if before else ""
    nxt = after[:1]
    # A range: a dash between two figures ("3 – 5"), or an unspaced en or
    # figure dash between two words ("Mon–Fri", "8am–6pm").
    glued = i > 0 and j < len(s) and s[i - 1].isalnum() and s[j].isalnum()
    if prev.isdigit() and nxt.isdigit() and m.group(0).strip() in ("\u2013", "\u2012"):
        return "-"
    if glued and m.group(0) in ("\u2013", "\u2012"):
        return "-"
    if glued and prev.isdigit() and nxt.isdigit():
        return "-"
    # Start of a line: a list item.
    if not before or prev == "\n" or s[:i].endswith("\n"):
        return "- " if after and nxt not in "\n" else ""
    if not nxt or nxt in "\n":
        return ""  # a trailing dash
    if prev in ",.;:!?(" or nxt in ",.;:!?)":
        return "" if prev == "(" or nxt in ",.;:!?)" else " "
    return ", "


def _fix_dashes(text: str) -> tuple[str, int]:
    count = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return _dash_sub(m)

    out = re.sub(rf"[ \t]*(?:{_DASH})[ \t]*", sub, text)
    return out, count


def _tidy(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]{2,}", " ", line)
        line = re.sub(r"\s+([,.;:!?])", r"\1", line)
        line = re.sub(r",\s*,", ",", line)
        line = re.sub(r",([.!?])", r"\1", line)
        line = re.sub(r"^\s*[,;]\s*", "", line)
        line = re.sub(r"[,;]\s*$", ".", line.rstrip())
        lines.append(line)
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _cap_first(text: str) -> str:
    """Capitalise the first letter of the text and of each sentence a cut left lower-case."""
    def up(m: re.Match[str]) -> str:
        return m.group(1) + m.group(2).upper()

    text = re.sub(r"^(\s*)([a-z])", up, text)
    return re.sub(r"([.!?]\s+|\n\s*)([a-z])(?=[a-z' ])", up, text)


def _strip_filler(text: str) -> tuple[str, list[str]]:
    found: list[str] = []
    out = text
    for rx in _SENTENCE_RES:
        def drop(m: re.Match[str]) -> str:
            found.append(" ".join(m.group(0).split()))
            # After a greeting's comma ("Hello Mama Bisi, I hope..."), end
            # the greeting there instead.
            return "\x00" if m.start() and m.string[m.start() - 1] == "," else " "
        out = rx.sub(drop, out)
    out = re.sub(r",\x00\s*", ". ", out).replace("\x00", " ")
    for rx in _OPENER_RES:
        def cut(m: re.Match[str]) -> str:
            found.append(" ".join(m.group(0).split()))
            return ""
        out = rx.sub(cut, out)
    # Any other sentence carrying a banned phrase and no figure ("Glad to help
    # anytime.") goes too, so the guard removes everything ``banned_in`` flags.
    lines = []
    for line in out.split("\n"):
        kept = []
        for sent in re.split(r"(?<=[.!?])\s+", line):
            if sent.strip() and banned_in(sent) and not re.search(r"\d", sent):
                found.append(" ".join(sent.split()))
                continue
            kept.append(sent)
        lines.append(" ".join(kept))
    out = "\n".join(lines)
    if found:
        out = _cap_first(out)
    return out, found


def enabled() -> bool:
    return os.environ.get("MIMI_STYLE_GUARD", "").strip().lower() not in ("off", "0", "false", "no")


def clean(text: str) -> Cleaned:
    """The text with dashes and filler removed, and what was changed."""
    if not text:
        return Cleaned(text)
    no_dash, n = _fix_dashes(text)
    no_dash = _tidy(no_dash) if n else no_dash
    stripped, found = _strip_filler(no_dash)
    stripped = _tidy(stripped) if found else stripped
    result = Cleaned(stripped, n, found)
    # Never change a figure, and never send an empty or near-empty message.
    if figures(stripped) != figures(text) or len(stripped.strip(" .,!?\n")) < 2:
        if figures(no_dash) != figures(text):
            # The plainest dash-free form: a dash becomes a space, nothing else moves.
            no_dash = re.sub(r"[ \t]{2,}", " ", re.sub(_DASH, " ", text))
        result = Cleaned(no_dash, n, [], True)
    return result


def clean_reply(text: str, *, where: str = "") -> str:
    """What every outbound message passes through. Deterministic; see the module doc."""
    if not enabled() or not text:
        return text
    c = clean(text)
    if c.changed:
        log.info("style guard%s: %d dash(es), %d phrase(s) removed%s",
                 f" ({where})" if where else "", c.dashes, len(c.phrases),
                 ", figures kept by keeping the original" if c.kept_original else "")
    return c.text

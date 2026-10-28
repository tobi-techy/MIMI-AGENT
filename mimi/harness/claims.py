"""The verification gate: every fact and claim in outbound text is checked
against what this run actually read and did, before anything is sent
(docs/LOOP-SAFETY.md, items 7 to 10).

It runs where the figure check runs (``loop.FIGURE_CHECKED``), on the text a
person or another helper will read:

* **Figures**: every naira figure comes from a tool result, the book slice, a
  helper verdict, what Mimi already told a person on this thread, or the
  owner's own words. Numbers in shop, rep or screenshot text are not owner
  figures (RAI-313): a shop's figure may be repeated only as the shop's claim,
  in the same sentence ("you said you paid N50,000", "we have not seen N50,000
  in the bank yet"), never as a fact.
* **Dates**: a calendar date ("30 October", "2026-10-30") or a weekday must be
  today, tomorrow or yesterday, or a date a tool result, the book, an intent
  of this run or the owner gave.
* **Names**: a shop id, or one of this distributor's shop or product names,
  must appear in what this run read (tool results, the book slice, a
  proposal) or in the owner's words, never only in shop text.
* **Done claims**: "recorded", "sent", "approved", "received", "scheduled",
  "done" need a matching confirmed tool outcome in this run.
* **Promises**: "I'll remind you Friday", "I'll check back" need a follow-up
  actually set in this run (or a proposal or handoff that reports back).
* **Never**: a claim that money moved, or that an approved record was changed,
  is always held back; no tool can do either.

A held-back message gets the run's one repair turn, then the run fails safe and
nothing it proposed leaves. Every hold is a ``claim_blocked`` event.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

# ----------------------------------------------------------------- sources

_OWNER_SPAN = re.compile(r"<owner_message>\s*(.*?)\s*</owner_message>", re.S)
_MID_RUN = "[Owner message arrived mid-run; it comes first]\n"
_SLICE = re.compile(r"<book_slice[^>]*>\s*(\{.*?\})\s*</book_slice>", re.S)
_VERDICT = re.compile(r"<helper_verdict>\s*(.*?)\s*</helper_verdict>", re.S)
_UNTRUSTED = re.compile(r"<(\w+)[^>]*untrusted=\"true\"[^>]*>(.*?)</\1>", re.S)
_BOOK = re.compile(r"<book>\s*(\{.*?\})\s*</book>", re.S)
_HISTORY = re.compile(r"<thread_history>\s*(.*?)\s*</thread_history>", re.S)
_HIST_LINE = re.compile(r"^\[[^\]]*\]\s*(Mimi|Owner|Shop|Rep):\s*(.*)$")
# The field of each speaking tool that a person reads.
_SPOKEN = {"send_to_user": "body", "ask_question": "question", "notify_rep": "message",
           "notify_owner": "message"}

NAIRA_IN_TEXT = re.compile(r"(?:\bN|₦|NGN\s?)\s?(\d[\d,]*(?:\.\d{1,2})?)\s?(k|m)?\b", re.I)
_ANY_NUM = re.compile(r"(?:N|₦)?\s?(\d[\d,]*(?:\.\d+)?)\s?(k|m)?\b", re.I)


@dataclass
class Sources:
    """What this run may state as fact, split by where it came from."""

    kobo: set[int] = field(default_factory=set)  # tools, book, verdicts, owner
    shop_kobo: set[int] = field(default_factory=set)  # only in untrusted text
    record_text: list[str] = field(default_factory=list)  # tools, book, verdicts, intents
    owner_text: list[str] = field(default_factory=list)
    shop_text: list[str] = field(default_factory=list)


def kobo_of(num: str, suffix: str | None) -> int:
    value = Decimal(num.replace(",", ""))
    mult = {"k": 1_000, "m": 1_000_000}.get((suffix or "").lower(), 1)
    return int(value * mult * 100)


def _walk_kobo(node: Any, out: set[int], key: str = "") -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _walk_kobo(v, out, k)
    elif isinstance(node, list):
        for v in node:
            _walk_kobo(v, out, key)
    elif isinstance(node, int) and not isinstance(node, bool) and key.endswith("_kobo"):
        out.add(node)
    elif isinstance(node, str) and key == "preview":
        for m in NAIRA_IN_TEXT.finditer(node):
            out.add(kobo_of(m.group(1), m.group(2)))


def _numbers(text: str, out: set[int]) -> None:
    for hit in _ANY_NUM.finditer(text):
        try:
            out.add(kobo_of(hit.group(1), hit.group(2)))
        except (ArithmeticError, InvalidOperation):
            continue


def owner_spans(content: str) -> list[str]:
    """The owner's own words in one user message (never shop text)."""
    spans = [m.group(1) for m in _OWNER_SPAN.finditer(content)]
    if content.startswith(_MID_RUN) and "<owner_message>" not in content:
        spans.append(content[len(_MID_RUN):])
    return spans


def sources(messages: Sequence[dict[str, Any]], intents: Iterable[Any] = (), *,
            history_end: int = 0) -> Sources:
    """``history_end``: messages before this index are earlier turns of the
    thread; only there do Mimi's own sent messages count as known."""
    src = Sources()
    for n, m in enumerate(messages):
        role = m.get("role")
        content = str(m.get("content") or "")
        if role == "tool":
            src.record_text.append(content)
            try:
                _walk_kobo(json.loads(content), src.kobo)
            except ValueError:
                continue
        elif role == "assistant" and n < history_end:
            # Mimi's own earlier messages on this thread (they passed this gate
            # when they were sent): what she already told a person is known.
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                field_ = _SPOKEN.get(str(fn.get("name") or ""))
                if not field_:
                    continue
                try:
                    said = str((json.loads(fn.get("arguments") or "{}") or {}).get(field_) or "")
                except (ValueError, AttributeError):
                    continue
                src.record_text.append(said)
                for hit in NAIRA_IN_TEXT.finditer(said):
                    src.kobo.add(kobo_of(hit.group(1), hit.group(2)))
        elif role == "user":
            for hist in _HISTORY.findall(content):
                for line in hist.splitlines():
                    hm = _HIST_LINE.match(line)
                    if not hm:
                        continue
                    who, said = hm.group(1), hm.group(2)
                    if who == "Mimi":
                        src.record_text.append(said)
                        for hit in NAIRA_IN_TEXT.finditer(said):
                            src.kobo.add(kobo_of(hit.group(1), hit.group(2)))
                    elif who == "Owner":
                        src.owner_text.append(said)
                        _numbers(said, src.kobo)
                    else:
                        src.shop_text.append(said)
                        _numbers(said, src.shop_kobo)
            for block in _BOOK.findall(content):
                src.record_text.append(block)
                try:
                    _walk_kobo(json.loads(block), src.kobo)
                except ValueError:
                    pass
            for block in _SLICE.findall(content):
                src.record_text.append(block)
                try:
                    _walk_kobo(json.loads(block), src.kobo)
                except ValueError:
                    pass
            for v in _VERDICT.findall(content):
                src.record_text.append(v)
                for hit in NAIRA_IN_TEXT.finditer(v):
                    src.kobo.add(kobo_of(hit.group(1), hit.group(2)))
            for span in owner_spans(content):
                src.owner_text.append(span)
                _numbers(span, src.kobo)
            for _, body in _UNTRUSTED.findall(content):
                src.shop_text.append(body)
                _numbers(body, src.shop_kobo)
    for i in intents:
        src.record_text.append(json.dumps(getattr(i, "payload", {}) or {}, ensure_ascii=False))
        src.record_text.append(str(getattr(i, "preview", "") or ""))
    return src


# ----------------------------------------------------------------- figures

# A shop's own figure may be repeated only as the shop's claim, in the same
# sentence ("you said you paid N50,000", "we have not seen N50,000 in the
# bank yet"), never as a fact (RAI-313).
_ATTRIBUTED = re.compile(
    r"\b(?:shop|they|customer|retailer|you|he|she)\s+(?:says?|said|claims?|claimed|sent|"
    r"reports?|reported|wrote|mentioned|told)\b|\bclaim(?:s|ed)?\b|\baccording to\b|"
    r"\bscreenshot\b|\balert\b|\b(?:have|has|had|do|does|did|could|can)\s*(?:not|n't|n’t)\s+"
    r"(?:yet\s+)?(?:see|seen|find|found|receive|received|match|matched|confirm|confirmed)\b|"
    r"\bnot\s+(?:yet\s+)?(?:seen|received|found|showing|matched|confirmed|reflect\w*)\b", re.I)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")


def _sentence_of(text: str, pos: int) -> str:
    for m in _SENTENCE.finditer(text):
        if m.start() <= pos < m.end():
            return m.group(0)
    return text


def invented_figures(src: Sources, text: str) -> list[str]:
    bad = []
    for m in NAIRA_IN_TEXT.finditer(text):
        try:
            kobo = kobo_of(m.group(1), m.group(2))
        except (ArithmeticError, InvalidOperation):
            bad.append(m.group(0).strip())
            continue
        if kobo in src.kobo:
            continue
        if kobo in src.shop_kobo and _ATTRIBUTED.search(_sentence_of(text, m.start())):
            continue
        bad.append(m.group(0).strip())
    return bad


# ------------------------------------------------------------------- dates

MONTHS = {m: i + 1 for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"))}
_MON = {k[:3]: v for k, v in MONTHS.items()} | {"sept": 9} | MONTHS
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTH_RE = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
             r"aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_ISO = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)")
_DAY_MON = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH_RE + r"\b", re.I)
_MON_DAY = re.compile(r"\b" + _MONTH_RE + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
_WEEKDAY = re.compile(r"\b(" + "|".join(WEEKDAYS) + r")\b", re.I)


def _dm(day: str, month: str) -> tuple[int, int] | None:
    mon = _MON.get(month.lower().rstrip("."))
    try:
        d = int(day)
    except ValueError:
        return None
    if mon is None or not 1 <= d <= 31:
        return None
    return (mon, d)


def dates_in(text: str) -> tuple[set[tuple[int, int]], set[str], list[str]]:
    """(month, day) pairs, weekday names and the raw mentions in ``text``."""
    pairs: set[tuple[int, int]] = set()
    raw: list[str] = []
    for y, mo, d in _ISO.findall(text):
        try:
            dd = date(int(y), int(mo), int(d))
        except ValueError:
            continue
        pairs.add((dd.month, dd.day))
        raw.append(f"{y}-{mo}-{d}")
    for m in _DAY_MON.finditer(text):
        p = _dm(m.group(1), m.group(2))
        if p:
            pairs.add(p)
            raw.append(m.group(0))
    for m in _MON_DAY.finditer(text):
        p = _dm(m.group(2), m.group(1))
        if p:
            pairs.add(p)
            raw.append(m.group(0))
    days = {w.lower() for w in _WEEKDAY.findall(text)}
    return pairs, days, raw


def known_dates(src: Sources, today: date) -> tuple[set[tuple[int, int]], set[str]]:
    """Calendar dates the run may state, and the weekdays it may name.

    A weekday is only as good as a nearby date: today, tomorrow, yesterday, a
    record date within a week back or two weeks ahead, or a weekday a record or
    the owner names in words. A month of old invoice dates does not make every
    weekday true."""
    pairs: set[tuple[int, int]] = set()
    days: set[str] = set()
    near: set[date] = {today + timedelta(days=n) for n in (-1, 0, 1)}
    for text in [*src.record_text, *src.owner_text]:
        for y, mo, d in _ISO.findall(text):
            try:
                dd = date(int(y), int(mo), int(d))
            except ValueError:
                continue
            pairs.add((dd.month, dd.day))
            if -7 <= (dd - today).days <= 14:
                near.add(dd)
        p, w, _ = dates_in(text)
        pairs |= p
        days |= w
        for mo, dd_ in p:
            try:
                cand = date(today.year, mo, dd_)
            except ValueError:
                continue
            if -7 <= (cand - today).days <= 14:
                near.add(cand)
    for d in near:
        pairs.add((d.month, d.day))
        days.add(WEEKDAYS[d.weekday()])
    return pairs, days


_WEEKDAY_DATE = re.compile(r"\b(" + "|".join(WEEKDAYS) + r"),?\s+(?:the\s+)?(\d{1,2})"
                           r"(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH_RE + r"\b", re.I)


def _calendar_true(text: str, today: date) -> tuple[set[tuple[int, int]], set[str]]:
    """'Tuesday 3 November' within 60 days of today that is that weekday: a
    calendar fact, not an invented date (a question that pins down which day)."""
    pairs: set[tuple[int, int]] = set()
    days: set[str] = set()
    for m in _WEEKDAY_DATE.finditer(text):
        p = _dm(m.group(2), m.group(3))
        if not p:
            continue
        for y in (today.year, today.year + 1):
            try:
                d = date(y, p[0], p[1])
            except ValueError:
                continue
            if abs((d - today).days) <= 60 and WEEKDAYS[d.weekday()] == m.group(1).lower():
                pairs.add(p)
                days.add(m.group(1).lower())
    return pairs, days


def unknown_dates(src: Sources, text: str, today: date) -> list[str]:
    pairs, days, raw = dates_in(text)
    if not pairs and not days:
        return []
    ok_pairs, ok_days = known_dates(src, today)
    cal_pairs, cal_days = _calendar_true(text, today)
    ok_pairs |= cal_pairs
    ok_days |= cal_days
    bad = [r for r in raw if not (dates_in(r)[0] <= ok_pairs)]
    bad += [w.capitalize() for w in sorted(days - ok_days)]
    return bad


# ------------------------------------------------------------------- names

_SHOP_ID = re.compile(r"\bshop-\d{3}\b", re.I)


def unknown_names(src: Sources, text: str, names: Iterable[str]) -> list[str]:
    """Shop ids and this distributor's shop or product names in ``text`` that
    nothing this run read (or the owner said) names."""
    seen = " \n".join([*src.record_text, *src.owner_text]).lower()
    low = text.lower()
    bad = [sid for sid in dict.fromkeys(m.lower() for m in _SHOP_ID.findall(text))
           if sid not in seen]
    for name in names:
        n = name.strip().lower()
        if len(n) < 5 or n not in low:
            continue
        if not re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", low):
            continue
        if n not in seen:
            bad.append(name)
    return bad


# ------------------------------------------------- done claims and promises

_SUBJECT = (r"(?:i(?:'ve|’ve| have)|we(?:'ve|’ve| have)|(?:it|this|that)(?:'s|’s)(?:\s+been)?|"
            r"(?:it|this|that|they|payment|"
            r"order|reminder|receipt|request|everything|your \w+)\s+(?:has|have|is|was|were|"
            r"are|'s|’s)(?:\s+been)?|already|just|successfully)")
_CLAIM = re.compile(r"\b" + _SUBJECT + r"\s+(?:now\s+|just\s+|already\s+|been\s+|also\s+)*"
                    r"(recorded|logged|saved|noted|sent|forwarded|approved|confirmed|received|"
                    r"verified|scheduled|booked|done|updated|added|placed|cancelled|"
                    r"cleared)\b", re.I)
_DONE_START = re.compile(r"^\s*(?:all\s+)?done\b", re.I)
_PROMISE = re.compile(r"\b(?:i(?:'ll|’ll| will| shall| am going to)|we(?:'ll|’ll| will))"
                      r"\s+(?:also\s+|then\s+|now\s+)?(remind|check (?:back|again|in|on)|"
                      r"follow up|get back|chase|come back|ping|message|text|update|"
                      r"let (?:you|them|the owner) know|reach out|look again|"
                      r"send (?:you|them) (?:a|an) (?:reminder|update))\b", re.I)
# "I'll confirm once it reflects": the bank feed or a helper's report wakes
# Mimi; that is an event the system already watches, not a promise to keep.
_EVENT = re.compile(r"\b(?:when|once|as soon as|immediately|the moment)\b", re.I)
_TIME = re.compile(r"\b(?:today|tonight|tomorrow|later|this (?:morning|afternoon|evening|week)|"
                   r"next (?:week|month|\w+day)|in (?:a|an|\d+|one|two|three) (?:few )?"
                   r"(?:hour|day|week)s?|by (?:\w+day|the end)|on \w+day|" +
                   "|".join(WEEKDAYS) + r")\b", re.I)
_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”")
_MONEY_MOVE = re.compile(
    r"\b(?:i(?:'ve|’ve| have)|we(?:'ve|’ve| have)|mimi has)\s+(?:just\s+|now\s+|already\s+)?"
    r"(transferred|refunded|paid|wired|credited|debited|reversed|wrote off|written off|"
    r"waived|discounted|extended your (?:due date|credit)|changed (?:the|your) bank)\b",
    re.I)
_EDIT_APPROVED = re.compile(r"\b(?:changed|edited|updated|amended|modified)\s+(?:the|your)?\s*"
                            r"approved\b", re.I)

SPEAKING = frozenset({"send_to_user", "ask_question"})
_INTERNAL = frozenset({"update_plan"})
_PAYMENT_OK = re.compile(r"\"(?:matched|bank_confirmed|verified)\":\s*true|\"status\":\s*"
                         r"\"(?:verified|confirmed|received|paid|matched)\"", re.I)


def _work(intents: Sequence[Any]) -> list[Any]:
    return [i for i in intents if i.tool not in SPEAKING and i.tool not in _INTERNAL]


def unbacked_claims(text: str, *, intents: Sequence[Any], src: Sources, wake_source: str,
                    other_drafts: int) -> list[str]:
    """Done claims in ``text`` with no confirmed outcome in this run."""
    work = _work(intents)
    applied = [i for i in work if i.tier in ("auto", "notify")]
    tools = {i.tool for i in work}
    records = "\n".join(src.record_text)
    paid = bool(_PAYMENT_OK.search(records))
    approved = wake_source == "approval_answered" or bool(
        re.search(r"\"status\":\s*\"approved\"", records))
    bad: list[str] = []
    text = _QUOTED.sub(" ", text)
    claims = [m.group(1).lower() for m in _CLAIM.finditer(text)]
    if _DONE_START.search(text):
        claims.append("done")
    for verb in dict.fromkeys(claims):
        if verb == "approved":
            ok = approved
        elif verb in ("received", "verified", "cleared"):
            ok = paid
        elif verb == "confirmed":
            ok = paid or bool(applied)
        elif verb in ("scheduled", "booked"):
            ok = bool(tools & {"set_followup", "propose_reminder_plan", "send_reminder"})
        elif verb in ("sent", "forwarded"):
            ok = bool(work) or other_drafts > 0
        else:
            ok = bool(applied)
        if not ok:
            bad.append(verb)
    return bad


def unbacked_promises(text: str, *, intents: Sequence[Any]) -> list[str]:
    text = _QUOTED.sub(" ", text)
    hits = []
    for m in _PROMISE.finditer(text):
        sent = _sentence_of(text, m.start())
        if _EVENT.search(sent) and not _TIME.search(sent):
            continue
        hits.append(m.group(0))
    if not hits:
        return []
    tools = {i.tool for i in intents}
    pending = any(i.tier == "approve" for i in intents)
    if tools & {"set_followup", "ask_helper"} or pending:
        return []
    return hits


def never_claims(text: str) -> list[str]:
    text = _QUOTED.sub(" ", text)
    out = [m.group(0) for m in _MONEY_MOVE.finditer(text)]
    out += [m.group(0) for m in _EDIT_APPROVED.finditer(text)]
    return out


# -------------------------------------------------------------------- gate

PERSON_FACING = frozenset({"send_to_user", "ask_question", "notify_rep", "notify_owner"})


def check_outcomes(tool: str, text: str, *, messages: Sequence[dict[str, Any]],
                   intents: Sequence[Any], wake_source: str,
                   other_drafts: int = 0, history_end: int = 0) -> dict[str, list[str]]:
    """Done claims and promises, checked once the whole step has run, so a
    send_to_user next to the tool that does the work in the same model turn is
    judged on what that tool actually did."""
    if tool not in PERSON_FACING:
        return {}
    src = sources(messages, intents, history_end=history_end)
    out: dict[str, list[str]] = {}
    c = unbacked_claims(text, intents=intents, src=src, wake_source=wake_source,
                        other_drafts=other_drafts)
    if c:
        out["claims"] = c
    p = unbacked_promises(text, intents=intents)
    if p:
        out["promises"] = p
    return out


def check(tool: str, text: str, *, messages: Sequence[dict[str, Any]], intents: Sequence[Any],
          today: date, wake_source: str, names: Iterable[str] = (),
          other_drafts: int = 0, outcomes: bool = True,
          history_end: int = 0) -> dict[str, list[str]]:
    """Every problem with ``text`` by kind; empty when it may be sent.
    ``outcomes=False`` leaves done claims and promises to ``check_outcomes``."""
    src = sources(messages, intents, history_end=history_end)
    out: dict[str, list[str]] = {}
    figs = invented_figures(src, text)
    if figs:
        out["figures"] = figs
    if tool in PERSON_FACING:
        d = unknown_dates(src, text, today)
        if d:
            out["dates"] = d
        n = unknown_names(src, text, names)
        if n:
            out["names"] = n
    if outcomes:
        out.update(check_outcomes(tool, text, messages=messages, intents=intents,
                                  wake_source=wake_source, other_drafts=other_drafts,
                                  history_end=history_end))
    nv = never_claims(text)
    if nv:
        out["never"] = nv
    return out


def repair_text(problems: dict[str, list[str]]) -> tuple[str, str]:
    """(why, next step) for the repair turn, by kind."""
    parts = []
    if "figures" in problems:
        parts.append("these figures are not in any tool result, the book or the owner's own "
                     f"words: {', '.join(problems['figures'])}")
    if "dates" in problems:
        parts.append(f"these dates are not in any record of this run: "
                     f"{', '.join(problems['dates'])}")
    if "names" in problems:
        parts.append(f"these names are not in anything this run read: "
                     f"{', '.join(problems['names'])}")
    if "claims" in problems:
        parts.append("it says " + ", ".join(problems["claims"]) +
                     " but no tool in this run did that")
    if "promises" in problems:
        parts.append("it promises a later action (" + "; ".join(problems["promises"]) +
                     ") but nothing was scheduled")
    if "never" in problems:
        parts.append("it says money moved or an approved record changed, which Mimi never "
                     "does")
    why = "This message was held back: " + "; ".join(parts) + "."
    nxt = ("Copy figures, dates and names exactly from tool results. Only say something is "
           "done after its tool ran in this turn; to promise a later check, call "
           "set_followup first, or leave the promise out. Then send again.")
    return why, nxt

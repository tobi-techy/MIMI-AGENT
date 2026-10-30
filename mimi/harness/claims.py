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
  "done" need a matching confirmed tool outcome in this run, for the item the
  claim's clause names: shop, invoice or order id, amount (RAI-316). A claim
  that names nothing needs exactly one matching outcome.
* **Partial names**: a run of two or more words from a known name ("Mama
  Sola") must map to exactly one shop name the run read (RAI-316).
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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
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


@dataclass(frozen=True)
class Outcome:
    """One confirmed thing this run can point at: a bank credit or confirmed
    payment row, an approved record, or an intent it proposed. ``shop`` is
    None when the record does not say (it then binds to no named shop unless
    ``wild``); ``wild`` marks evidence with no item detail at all (an approval
    wake, a record that is not JSON)."""

    kind: str  # payment | approved | intent | draft
    shop: str | None
    ids: frozenset[str] = frozenset()
    amounts: frozenset[int] = frozenset()
    tool: str = ""
    applied: bool = False
    wild: bool = False

    @property
    def item(self) -> tuple[str, frozenset[str]]:
        return (self.shop or "", self.ids)


@dataclass
class Sources:
    """What this run may state as fact, split by where it came from."""

    kobo: set[int] = field(default_factory=set)  # tools, book, verdicts, owner
    shop_kobo: set[int] = field(default_factory=set)  # only in untrusted text
    record_text: list[str] = field(default_factory=list)  # tools, book, verdicts, intents
    owner_text: list[str] = field(default_factory=list)
    shop_text: list[str] = field(default_factory=list)
    # Per item (RAI-316): confirmed outcomes in the records (bank credits and
    # confirmed payment rows, approved records), each with its shop, ids and
    # amounts; open invoices by id; shop names paired with their ids.
    outcomes: list[Outcome] = field(default_factory=list)
    invoices: dict[str, tuple[str, frozenset[int]]] = field(default_factory=dict)
    shop_ids: dict[str, set[str]] = field(default_factory=dict)  # lower name -> ids


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


_OK_KEYS = ("matched", "bank_confirmed", "verified")
_OK_STATUS = frozenset({"verified", "confirmed", "received", "paid", "matched"})
_INVOICE_KEYS = ("invoice_id", "delivery_id")
_INVOICE_AMOUNTS = ("outstanding_kobo", "total_kobo", "amount_kobo")


def _ids_of(node: dict[str, Any]) -> set[str]:
    return {str(v).strip().lower() for k, v in node.items()
            if isinstance(v, str) and v.strip() and k != "shop_id"
            and (k.endswith("_id") or k in ("reference", "id"))}


def _amounts_of(node: dict[str, Any]) -> set[int]:
    return {v for k, v in node.items()
            if k.endswith("_kobo") and isinstance(v, int) and not isinstance(v, bool)}


def _harvest(node: Any, src: Sources, shop: str | None, extra_ids: set[str],
             top: bool = True) -> None:
    """Outcomes, invoices and shop-name pairs in one parsed record."""
    if isinstance(node, list):
        for v in node:
            _harvest(v, src, shop, extra_ids, top=False)
        return
    if not isinstance(node, dict):
        return
    here = str(node.get("shop_id") or "") or shop
    name = node.get("shop_name")
    if here and isinstance(name, str) and name.strip():
        src.shop_ids.setdefault(name.strip().lower(), set()).add(here)
    for k in _INVOICE_KEYS:
        inv = node.get(k)
        if isinstance(inv, str) and inv:
            amts = frozenset(node[a] for a in _INVOICE_AMOUNTS
                             if isinstance(node.get(a), int) and not isinstance(node[a], bool))
            if amts:
                src.invoices.setdefault(inv.lower(), (here or "", amts))
    ids = _ids_of(node) | (extra_ids if top else set())
    status = str(node.get("status") or node.get("label") or "").lower()
    if any(node.get(k) is True for k in _OK_KEYS) or status in _OK_STATUS:
        src.outcomes.append(Outcome("payment", here, frozenset(ids),
                                    frozenset(_amounts_of(node))))
    if str(node.get("status") or "").lower() == "approved":
        src.outcomes.append(Outcome("approved", here, frozenset(ids),
                                    frozenset(_amounts_of(node))))
    for k, v in node.items():
        if isinstance(v, (dict, list)) and k != "args":
            _harvest(v, src, here, extra_ids, top=top and k == "data")


_PAID_TEXT = re.compile(r"\"(?:matched|bank_confirmed|verified)\":\s*true|\"status\":\s*"
                        r"\"(?:verified|confirmed|received|paid|matched)\"", re.I)
_APPROVED_TEXT = re.compile(r"\"status\":\s*\"approved\"", re.I)


def _harvest_text(text: str, src: Sources, shop: str | None = None,
                  extra_ids: set[str] | None = None) -> None:
    """A record that parses is read item by item; one that does not still
    counts the way it did before RAI-316, as evidence with no item detail."""
    try:
        _harvest(json.loads(text), src, shop, extra_ids or set())
        return
    except ValueError:
        pass
    if _PAID_TEXT.search(text):
        src.outcomes.append(Outcome("payment", None, wild=True))
    if _APPROVED_TEXT.search(text):
        src.outcomes.append(Outcome("approved", None, wild=True))


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
    call_args: dict[str, dict[str, Any]] = {}
    for m in messages:
        for call in m.get("tool_calls") or []:
            try:
                a = json.loads((call.get("function") or {}).get("arguments") or "{}")
            except (ValueError, AttributeError):
                continue
            if isinstance(a, dict):
                call_args[str(call.get("id") or "")] = a
    for n, m in enumerate(messages):
        role = m.get("role")
        content = str(m.get("content") or "")
        if role == "tool":
            src.record_text.append(content)
            args = call_args.get(str(m.get("tool_call_id") or ""), {})
            _harvest_text(content, src, str(args.get("shop_id") or "") or None,
                          _ids_of(args))
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
                _harvest_text(block, src)
                try:
                    _walk_kobo(json.loads(block), src.kobo)
                except ValueError:
                    pass
            for block in _SLICE.findall(content):
                src.record_text.append(block)
                _harvest_text(block, src)
                try:
                    _walk_kobo(json.loads(block), src.kobo)
                except ValueError:
                    pass
            for v in _VERDICT.findall(content):
                src.record_text.append(v)
                _harvest_text(v, src)
                for hit in NAIRA_IN_TEXT.finditer(v):
                    src.kobo.add(kobo_of(hit.group(1), hit.group(2)))
            for span in owner_spans(content):
                src.owner_text.append(span)
                _numbers(span, src.kobo)
            for _, body in _UNTRUSTED.findall(content):
                src.shop_text.append(body)
                _numbers(body, src.shop_kobo)
    for i in intents:
        payload = getattr(i, "payload", {}) or {}
        src.record_text.append(json.dumps(payload, ensure_ascii=False))
        src.record_text.append(str(getattr(i, "preview", "") or ""))
        if isinstance(payload, dict):
            _harvest(payload, src, str(getattr(i, "shop_id", "") or "") or None, set())
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

# Words that never make a partial name on their own: "the mini mart", "two
# provisions stores" name no one (RAI-316).
_GENERIC = frozenset(
    "mama iya baba mummy mini mart marts provisions provision store stores kiosk kiosks pos "
    "and & sons son foodstuff foodstuffs shop shops the of a an supermarket".split())
_TOK = re.compile(r"[a-z0-9]+(?:['’][a-z]+)?|&")


def _tokens(text: str) -> list[tuple[str, int, int]]:
    out = []
    for m in _TOK.finditer(text.lower()):
        t = re.sub(r"['’]s$", "", m.group(0))
        out.append((t, m.start(), m.end()))
    return out


def _norm(name: str) -> tuple[str, ...]:
    return tuple(t for t, _, _ in _tokens(name))


@dataclass(frozen=True)
class _NameIndex:
    full: dict[tuple[str, ...], str]
    parts: dict[tuple[str, ...], frozenset[str]]  # sub-run -> names that contain it
    longest: int


@lru_cache(maxsize=32)
def _index(names: tuple[str, ...]) -> _NameIndex:
    full: dict[tuple[str, ...], str] = {}
    parts: dict[tuple[str, ...], set[str]] = {}
    for name in names:
        nt = _norm(name)
        if len(nt) < 2:
            continue
        full.setdefault(nt, name)
        lead = next((j for j, t in enumerate(nt) if t not in _GENERIC), None)
        for a in range(len(nt)):
            for b in range(a + 2, len(nt) + 1):
                sub = nt[a:b]
                if b - a < len(nt) and lead is not None and a <= lead < b:
                    parts.setdefault(sub, set()).add(name)
    for name in names:  # every name holding a run, lead word or not
        nt = _norm(name)
        for sub, owners in parts.items():
            if name not in owners and any(nt[x:x + len(sub)] == sub for x in range(len(nt))):
                owners.add(name)
    return _NameIndex(full, {k: frozenset(v) for k, v in parts.items()},
                      max((len(k) for k in full), default=0))


def _partials(text: str, names: Sequence[str]) -> list[tuple[str, list[str]]]:
    """Runs of two or more words from a known name, in ``text``, that are not
    the whole name: ("Mama Sola", ["Mama Sola Provisions"]). Each run holds the
    name's first distinctive word ("Sola Provisions" does, "Mini Mart" and
    "Sachet Water" do not). Returns the run as written and every known name
    that contains it."""
    idx = _index(tuple(names))
    if not idx.parts:
        return []
    toks = _tokens(text)
    words = [t for t, _, _ in toks]
    full: set[int] = set()  # token positions inside a whole known name
    runs: dict[tuple[int, int], frozenset[str]] = {}
    for i in range(len(words)):
        for k in range(2, min(idx.longest, len(words) - i) + 1):
            sub = tuple(words[i:i + k])
            if sub in idx.full:
                full.update(range(i, i + k))
            elif sub in idx.parts:
                runs[(i, i + k)] = idx.parts[sub]
    runs = {r: v for r, v in runs.items() if not full & set(range(*r))}
    # Keep the longest runs; a run inside a longer one is the same mention.
    keep = [r for r in runs if not any(o != r and o[0] <= r[0] and r[1] <= o[1] for o in runs)]
    return [(text[toks[i][1]:toks[j - 1][2]], sorted(runs[(i, j)])) for i, j in sorted(keep)]


def _is_shop(name: str, src: Sources, shops: Mapping[str, str] | None) -> bool:
    if shops:
        return name in shops
    # No name map from Go: a name the run paired with a shop id, or one with no
    # pack size in it (product names carry one, "Okin Sachet Milk 15g").
    return name.strip().lower() in src.shop_ids or not re.search(r"\d", name)


def resolve_partial(phrase: str, cands: Sequence[str], src: Sources,
                    shops: Mapping[str, str] | None) -> str | None:
    """The one name a partial means, or None when it maps to none this run
    read, or to two shops. A brand without its pack size ("Palmside Vegetable
    Oil") is a product family: fine when the run read one of its sizes."""
    seen = " \n".join([*src.record_text, *src.owner_text]).lower()
    read = [c for c in cands if c.lower() in seen]
    if not read and re.search(r"(?<!\w)" + re.escape(" ".join(phrase.lower().split())) +
                              r"(?!\w)", " ".join(seen.split())):
        # A record (or the owner) used this short form itself, as a counted
        # shelf item does ("Sunrise Instant Noodles 70g" without "(ctn of 40)").
        read = list(cands)
    shops_read = [c for c in read if _is_shop(c, src, shops)]
    products = [c for c in read if c not in shops_read]
    if len(shops_read) == 1 and not products:
        return shops_read[0]
    if not shops_read and products:
        return products[0]
    return None


def unknown_names(src: Sources, text: str, names: Iterable[str],
                  shops: Mapping[str, str] | None = None) -> list[str]:
    """Shop ids and this distributor's shop or product names in ``text`` that
    nothing this run read (or the owner said) names. A partial name ("Mama
    Sola") must map to exactly one shop name the run read (RAI-316)."""
    names = list(names)
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
    for phrase, cands in _partials(text, names):
        if resolve_partial(phrase, cands, src, shops) is None:
            bad.append(phrase)
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


def _work(intents: Sequence[Any]) -> list[Any]:
    return [i for i in intents if i.tool not in SPEAKING and i.tool not in _INTERNAL]


# ------------------------------------------------- claims bound to items

_VERBS = (r"(recorded|logged|saved|noted|sent|forwarded|approved|confirmed|received|"
          r"verified|scheduled|booked|done|updated|added|placed|cancelled|cleared)")
# "Your payment for invoice dlv-12-0057 has been received": a passive claim,
# whatever its subject (RAI-316), unless the clause is a condition.
_PASSIVE = re.compile(r"\b(?:has|have|is|was|were|are|'s|’s)\s+(?:now\s+|just\s+|already\s+|"
                      r"also\s+|successfully\s+)*been\s+" + _VERBS + r"\b", re.I)
# "Payment received, thank you." at the start of a sentence.
_HEADLINE = re.compile(r"^\s*(?:(?:your|the)\s+)?(?:payment|transfer|money|order|reminder|"
                       r"receipt)s?\s+(received|confirmed|verified|sent|recorded|approved|"
                       r"placed)\b", re.I)
_CONDITION = re.compile(r"\b(?:if|once|when|until|unless|after|before|as soon as)\b", re.I)
_CLAUSE = re.compile(r";|,\s*(?:and|but|while|so)\s+|\s+but\s+|\s+while\s+")
_ITEM_ID = re.compile(r"\b(?!shop-\d)[a-z]{2,5}[-_](?=[a-z0-9_-]*\d)[a-z0-9]+(?:[-_][a-z0-9]+)*\b",
                      re.I)
_MESSAGE_TOOLS = frozenset({"send_reminder", "send_receipt", "notify_rep", "notify_owner",
                            "ask_helper"})
_SCHEDULE_TOOLS = frozenset({"set_followup", "propose_reminder_plan", "send_reminder"})


@dataclass(frozen=True)
class Named:
    """What one claim's clause names: shops (ids), item ids and naira amounts."""

    shops: frozenset[str]
    ids: frozenset[str]
    amounts: frozenset[int]

    def __bool__(self) -> bool:
        return bool(self.shops or self.ids or self.amounts)


def _clauses(text: str) -> list[tuple[int, str]]:
    out = []
    for m in _SENTENCE.finditer(text):
        base, sent = m.start(), m.group(0)
        pos = 0
        for cut in _CLAUSE.finditer(sent):
            out.append((base + pos, sent[pos:cut.start()]))
            pos = cut.end()
        out.append((base + pos, sent[pos:]))
    return [(p, c) for p, c in out if c.strip()]


def _claims_in(text: str) -> list[tuple[str, str]]:
    """(verb, clause) for every done claim in ``text``."""
    out = []
    for n, (_, clause) in enumerate(_clauses(text)):
        seen: set[int] = set()
        for m in _CLAIM.finditer(clause):
            seen.add(m.end(1))
            out.append((m.group(1).lower(), clause))
        for m in _PASSIVE.finditer(clause):
            if m.end(1) in seen or _CONDITION.search(clause[:m.start()]):
                continue
            seen.add(m.end(1))
            out.append((m.group(1).lower(), clause))
        h = _HEADLINE.match(clause)
        if h and h.end(1) not in seen and not _CONDITION.search(clause):
            out.append((h.group(1).lower(), clause))
        if n == 0 and _DONE_START.search(clause):
            out.append(("done", clause))
    return out


def _shop_ids_for(name: str, src: Sources, shops: Mapping[str, str] | None) -> set[str]:
    if shops and name in shops:
        return {shops[name]}
    return set(src.shop_ids.get(name.strip().lower(), set()))


def named_in(clause: str, src: Sources, names: Sequence[str],
             shops: Mapping[str, str] | None) -> Named:
    low = clause.lower()
    shop_ids = {m.lower() for m in _SHOP_ID.findall(clause)}
    for name in names:
        n = name.strip().lower()
        if len(n) >= 5 and re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", low):
            shop_ids |= _shop_ids_for(name, src, shops)
    for phrase, cands in _partials(clause, names):
        hit = resolve_partial(phrase, cands, src, shops)
        if hit:
            shop_ids |= _shop_ids_for(hit, src, shops)
    ids = {m.group(0).lower() for m in _ITEM_ID.finditer(clause)}
    amounts = set()
    for m in NAIRA_IN_TEXT.finditer(clause):
        try:
            amounts.add(kobo_of(m.group(1), m.group(2)))
        except (ArithmeticError, InvalidOperation):
            continue
    return Named(frozenset(shop_ids), frozenset(ids), frozenset(amounts))


def _same_id(a: str, b: str) -> bool:
    """Ids match, an order with or without its version ("ord-012-ab12-v2")."""
    return a == b or a.split("-v")[0] == b.split("-v")[0] and bool(re.search(r"\d", a))


def _covers_id(o: Outcome, item: str, src: Sources) -> bool:
    if any(_same_id(item, x) for x in o.ids):
        return True
    # An invoice is paid by a credit of its own shop for exactly its amount.
    inv = src.invoices.get(item)
    return bool(inv and o.kind == "payment" and (not o.shop or not inv[0] or o.shop == inv[0])
                and inv[1] & o.amounts)


def _fits(o: Outcome, named: Named, to_shop: str, src: Sources) -> bool:
    if o.wild:
        return True
    want = named.shops or ({to_shop} if to_shop else set())
    if want and o.shop and o.shop not in want:
        return False
    if named.shops and not o.shop:
        return False
    if named.ids and not any(_covers_id(o, i, src) for i in named.ids):
        return False
    return not (named.amounts and o.amounts and not named.amounts & o.amounts)


# A claim about several things ("they have all been sent", "Done") names no
# one item, so any matching outcome backs it, as before RAI-316.
_PLURAL = re.compile(r"\b(?:all|both|everything|they|them|these|those|each|every|reminders|"
                     r"payments|messages|orders|invoices|receipts|notes|follow-?ups)\b", re.I)


def _backed(pool: Sequence[Outcome], named: Named, to_shop: str, src: Sources,
            many: bool = False) -> bool:
    fit = [o for o in pool if _fits(o, named, to_shop, src)]
    if not fit:
        return False
    if not named and many:
        return True
    if not named:
        # Nothing named: the claim is about one thing, so exactly one item may
        # back it ("Payment received" after a history of eight credits is
        # ambiguous and held).
        return len({o.item if not o.wild else ("*", frozenset()) for o in fit}) == 1
    if any(o.wild for o in fit):
        return True
    shops = {o.shop for o in fit}
    if not named.shops <= shops:
        return False
    return all(any(_covers_id(o, i, src) for o in fit) for i in named.ids)


def _intent_outcome(i: Any, kind: str = "intent") -> Outcome:
    payload = getattr(i, "payload", {}) or {}
    shop = str(getattr(i, "shop_id", "") or payload.get("shop_id") or "")
    return Outcome(kind, shop, frozenset(_ids_of(payload)), frozenset(_amounts_of(payload)),
                   tool=str(i.tool), applied=getattr(i, "tier", "") in ("auto", "notify"))


def unbacked_claims(text: str, *, intents: Sequence[Any], src: Sources, wake_source: str,
                    other_drafts: int, names: Iterable[str] = (),
                    shops: Mapping[str, str] | None = None, channel: str | None = None,
                    to_shop: str = "", exclude: str = "") -> list[str]:
    """Done claims in ``text`` with no confirmed outcome for the item they name.

    Each claim is read with its clause: the shops (ids, full or partial names),
    item ids (invoice, order, payment) and naira amounts it names must match
    one confirmed outcome of the right kind (RAI-316). A claim that names
    nothing needs exactly one; on a shop thread the shop is ``to_shop``.
    ``channel`` is the message's own channel: drafts to other channels count
    as sent; without it, ``other_drafts`` counts as sent evidence with no item
    detail (the pre-RAI-316 rule)."""
    names = list(names)
    # The message being checked is never evidence for itself.
    work = [_intent_outcome(i) for i in _work(intents)
            if not (exclude and getattr(i, "idempotency_key", "") == exclude)]
    applied = [o for o in work if o.applied]
    drafts: list[Outcome] = []
    if channel is not None:
        for i in intents:
            p = getattr(i, "payload", {}) or {}
            if i.tool in SPEAKING and str(p.get("channel") or "") != channel \
                    and p.get("kind") != "question":
                drafts.append(_intent_outcome(i, "draft"))
    elif other_drafts > 0:
        drafts.append(Outcome("draft", None, wild=True))
    payments = [o for o in src.outcomes if o.kind == "payment"]
    approved = [o for o in src.outcomes if o.kind == "approved"]
    if wake_source == "approval_answered":
        approved.append(Outcome("approved", None, wild=True))
    messages = [o for o in work if o.tool in _MESSAGE_TOOLS
                or o.tool.startswith(("send_", "notify_", "draft_"))] + drafts
    pools: dict[str, list[Outcome]] = {
        "approved": approved,
        "received": payments, "verified": payments, "cleared": payments,
        "confirmed": payments + applied,
        "scheduled": [o for o in work if o.tool in _SCHEDULE_TOOLS],
        "sent": messages or work + drafts,
    }
    pools["booked"] = pools["scheduled"]
    pools["forwarded"] = pools["sent"]
    bad: list[str] = []
    for verb, clause in _claims_in(_QUOTED.sub(" ", text)):
        pool = pools.get(verb, applied)
        many = verb == "done" and bool(_DONE_START.search(clause)) or bool(_PLURAL.search(clause))
        if not _backed(pool, named_in(clause, src, names, shops), to_shop, src, many):
            bad.append(verb)
    return list(dict.fromkeys(bad))


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


# ------------------------------------------------------------- deflections

# A reply that sends the person somewhere else instead of answering: "the
# orders helper or your rep will help you with that", "ask your rep", "the
# right helper will handle it". A dead end for the person; the helper should
# answer from its tools, or end the turn quietly so Mimi hands the message to
# the right helper (``mimi.harness.handoff``). Not a deflection: telling a
# shop to ask its rep to do a named thing ("ask your rep to add it to the
# order"), or a commitment ("your rep will confirm X with you").
_HELPER_WORD = (r"(?:orders?|collector|collections?|credit|stock|brand|proof|payments?|"
                r"delivery|deliveries|promo|right|other|another|relevant|correct|appropriate|"
                r"responsible)")
_HELP_VERB = r"(?:help|handle|assist|answer|sort|take care of|deal with|look into|attend to)"
_DEFLECT: tuple[re.Pattern[str], ...] = (
    re.compile(rf"\b(?:the |your |our |a |an )?{_HELPER_WORD} helper\b[^.!?\n]{{0,60}}?"
               rf"\b(?:will|can|should|would|could|is able to|go)\s+(?:be able to\s+)?"
               rf"{_HELP_VERB}\b", re.I),
    re.compile(rf"\b(?:your|the|a|our)\s+(?:sales\s+)?(?:rep|reps|representative|marketer)"
               rf"\b(?:\s+or\s+[^.!?\n]{{1,40}}?)?\s+(?:will|can|should|would|could|go)\s+"
               rf"(?:be able to\s+)?{_HELP_VERB}\b", re.I),
    re.compile(r"\b(?:please\s+)?(?:ask|contact|call|check with|speak to|speak with|talk to|"
               r"reach out to|reach)\s+(?:your|the)\s+(?:sales\s+)?(?:rep|representative|"
               r"marketer)\b(?!\s+to\s+(?:add|bring|deliver|include|put|carry|drop|send))",
               re.I),
    re.compile(rf"\b(?:that|this|it|orders?|those)\s+(?:is|are)\s+(?:handled|done|managed|"
               rf"covered)\s+by\s+(?:the\s+)?{_HELPER_WORD}\s+helper\b", re.I),
)


def deflections(text: str) -> list[str]:
    """Spans of ``text`` that hand the person off instead of answering."""
    t = _QUOTED.sub(" ", text)
    return [m.group(0).strip() for rx in _DEFLECT for m in rx.finditer(t)]


DEFLECTION_CHECKED = frozenset({"send_to_user", "ask_question"})


# -------------------------------------------------------------------- gate

PERSON_FACING = frozenset({"send_to_user", "ask_question", "notify_rep", "notify_owner"})


def check_outcomes(tool: str, text: str, *, messages: Sequence[dict[str, Any]],
                   intents: Sequence[Any], wake_source: str,
                   other_drafts: int = 0, history_end: int = 0, names: Iterable[str] = (),
                   shops: Mapping[str, str] | None = None, channel: str | None = None,
                   to_shop: str = "", exclude: str = "") -> dict[str, list[str]]:
    """Done claims and promises, checked once the whole step has run, so a
    send_to_user next to the tool that does the work in the same model turn is
    judged on what that tool actually did."""
    if tool not in PERSON_FACING:
        return {}
    src = sources(messages, intents, history_end=history_end)
    out: dict[str, list[str]] = {}
    c = unbacked_claims(text, intents=intents, src=src, wake_source=wake_source,
                        other_drafts=other_drafts, names=names, shops=shops, channel=channel,
                        to_shop=to_shop, exclude=exclude)
    if c:
        out["claims"] = c
    p = unbacked_promises(text, intents=intents)
    if p:
        out["promises"] = p
    return out


def check(tool: str, text: str, *, messages: Sequence[dict[str, Any]], intents: Sequence[Any],
          today: date, wake_source: str, names: Iterable[str] = (),
          other_drafts: int = 0, outcomes: bool = True,
          history_end: int = 0, shops: Mapping[str, str] | None = None,
          channel: str | None = None, to_shop: str = "") -> dict[str, list[str]]:
    """Every problem with ``text`` by kind; empty when it may be sent.
    ``outcomes=False`` leaves done claims and promises to ``check_outcomes``."""
    names = list(names)
    src = sources(messages, intents, history_end=history_end)
    out: dict[str, list[str]] = {}
    figs = invented_figures(src, text)
    if figs:
        out["figures"] = figs
    if tool in PERSON_FACING:
        d = unknown_dates(src, text, today)
        if d:
            out["dates"] = d
        n = unknown_names(src, text, names, shops)
        if n:
            out["names"] = n
    if outcomes:
        out.update(check_outcomes(tool, text, messages=messages, intents=intents,
                                  wake_source=wake_source, other_drafts=other_drafts,
                                  history_end=history_end, names=names, shops=shops,
                                  channel=channel, to_shop=to_shop))
    nv = never_claims(text)
    if nv:
        out["never"] = nv
    if tool in DEFLECTION_CHECKED:
        df = deflections(text)
        if df:
            out["deflection"] = df
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
    if "deflection" in problems:
        parts.append("it sends the person to another helper or the rep instead of answering ("
                     + "; ".join(problems["deflection"]) + "); answer from your own tools, or "
                     "if this is not your job, end the turn with no message and Mimi hands it "
                     "to the right helper")
    why = "This message was held back: " + "; ".join(parts) + "."
    nxt = ("Copy figures, dates and names exactly from tool results. Only say something is "
           "done after its tool ran in this turn; to promise a later check, call "
           "set_followup first, or leave the promise out. Then send again.")
    return why, nxt

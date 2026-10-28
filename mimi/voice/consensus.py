"""Two transcribers, one reading of the amount and the day (RAI-290).

A voice note about paying is heard by the primary transcriber for the shop's
language and, where a second one is competent (``SECOND_OPINION``), by that
one too. Code then reads the amount and the day from each transcript
(``mimi.voice.promise``) and reconciles them field by field:

* **agree**: both heard the same value, or only one heard a figure for that
  field and the other heard no figure at all. The value stands;
* **disagree**: two different values, or one transcript heard something it
  cannot read for certain ("egberun merun run") while the other heard a
  clean value. Mimi asks the shop; code never picks one transcriber's
  number over the other's;
* a vague day ("next week") agrees with a clear day inside it ("Friday next
  week"); words that fit two values ("two thousand five") stay a question
  whatever the other transcriber heard.

The result goes back into the receipt flow as one ``Transcript`` (so the
inbox, the flow and its YES rule are unchanged): the transcript whose own
reading asserts nothing the reconciled reading does not, preferring the
one that carries the most of it. When no transcript fits, the transcript
is marked ``low`` ("transcribers disagree"): Mimi asks one question and
that voice note can never give consent. A figure is only ever read back
to the shop, and only recorded after the shop's own YES.

Nothing here computes money from words a model wrote: every amount and day
comes from ``read_promise``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from mimi.voice.base import Transcript
from mimi.voice.promise import PromiseReading, find_dates, fold, lagos_day, read_promise

# Language hint -> the transcriber asked for a second opinion. Picked on the
# dev split of ``eval/voice_values`` (28 Oct 2026): Muse Voice Transcribe is
# a useful second ear for Pidgin and Yoruba (it hears the English money words
# shops mix in), and adds nothing but noise for Igbo and Hausa, which Meta
# does not list. English is Muse first; a Spitch second opinion without a
# language hint only added wrong options on dev.
SECOND_OPINION: dict[str, str] = {"pcm": "muse", "yo": "muse"}

_WEEKDAY_WORDS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class _Field:
    kind: str  # "val", "opts", "unsure", "past", "absent", "range"
    value: object = None


def _amount_state(f: PromiseReading) -> _Field:
    if f.amount_options:
        return _Field("opts", frozenset(f.amount_options))
    if f.amount_kobo is not None:
        return _Field("val", f.amount_kobo)
    if f.issue in ("ambiguous_amount", "several_amounts"):
        return _Field("unsure")
    return _Field("absent")


def _day_state(f: PromiseReading, text: str, today: date) -> _Field:
    if f.date_options:
        return _Field("opts", frozenset(f.date_options))
    if f.due_on is not None:
        return _Field("val", f.due_on)
    if f.issue == "past_date":
        return _Field("past")
    if f.issue in ("ambiguous_date", "several_dates"):
        spans = find_dates(fold(text), today)
        vague = [d for d in spans if d.on is None and not d.options]
        if spans and len(vague) == len(spans) and all(
            ("week" in d.said or "wik" in d.said) and not any(w in d.said for w in _WEEKDAY_WORDS)
            for d in vague
        ):
            monday = today + timedelta(days=7 - today.weekday())
            return _Field("range", frozenset(monday + timedelta(days=i) for i in range(7)))
        return _Field("unsure")
    return _Field("absent")


def _merge(states: Sequence[_Field]) -> tuple[object, tuple[object, ...], str]:
    """(value or None, options to ask about, why unsure: "" | amb | several | past | none)."""
    vals = {s.value for s in states if s.kind == "val"}
    opts: set[object] = set()
    for s in states:
        if s.kind == "opts":
            opts |= set(s.value)  # type: ignore[call-overload]
    ranges = [s.value for s in states if s.kind == "range"]
    unsure = any(s.kind == "unsure" for s in states)
    if opts:
        ok = vals <= opts and not unsure
        return None, tuple(sorted(opts)) if ok else (), "amb"  # type: ignore[type-var]
    if unsure:
        return None, (), "several"
    if ranges and (not vals or not all(v in r for v in vals for r in ranges)):  # type: ignore[operator]
        return None, (), "several"
    if len(vals) > 1:
        return None, tuple(sorted(vals)), "amb"  # type: ignore[type-var]
    if vals:
        return vals.pop(), (), ""
    return None, (), "past" if any(s.kind == "past" for s in states) else "none"


def reconcile(texts: Sequence[tuple[str, bool]], said_at: datetime | str) -> PromiseReading:
    """One reading from several transcripts of the same voice note.
    ``texts``: (transcript, usable) with the primary first."""
    if not texts:
        return PromiseReading("none")
    own = [read_promise(t, said_at, transcript_ok=ok) for t, ok in texts]
    if own[0].status == "paid_claim":
        return own[0]
    if not any(r.found for r in own):
        return next((r for r in own if r.status == "paid_claim"), own[0])
    usable = [t for t, ok in texts if ok]
    if not usable:
        return PromiseReading("unclear", issue="low_quality_transcript")
    today = lagos_day(said_at)
    figs = [read_promise(t, said_at, answering=True) for t in usable]
    amount, a_opts, a_why = _merge([_amount_state(f) for f in figs])
    due, d_opts, d_why = _merge([_day_state(f, t, today) for f, t in zip(figs, usable, strict=True)])
    issue = {"amb": "ambiguous_amount", "several": "several_amounts", "none": "no_amount"}.get(a_why, "")
    if not issue:
        issue = {"amb": "ambiguous_date", "several": "several_dates", "none": "no_date",
                 "past": "past_date"}.get(d_why, "")
    if isinstance(due, date) and due < today:
        due, issue = None, issue or "past_date"
    if issue:
        return PromiseReading(
            "unclear", issue=issue,  # type: ignore[arg-type]
            amount_kobo=amount if isinstance(amount, int) else None,
            due_on=due if isinstance(due, date) else None,
            amount_options=tuple(a_opts) if len(a_opts) > 1 else (),  # type: ignore[arg-type]
            date_options=tuple(d_opts) if len(d_opts) > 1 else (),  # type: ignore[arg-type]
        )
    assert isinstance(amount, int) and isinstance(due, date)
    return PromiseReading("promise", amount_kobo=amount, due_on=due)


def _fits(r: PromiseReading, want: PromiseReading) -> bool:
    if r.amount_kobo is not None and r.amount_kobo != want.amount_kobo:
        return False
    if r.due_on is not None and r.due_on != want.due_on:
        return False
    if r.amount_options and set(r.amount_options) != set(want.amount_options):
        return False
    return not (r.date_options and set(r.date_options) != set(want.date_options))


def choose(transcripts: Sequence[Transcript], said_at: datetime | str) -> Transcript:
    """The transcript to keep for this voice note (see the module notes)."""
    if len(transcripts) < 2:
        return transcripts[0]
    tried = tuple(x for t in transcripts for x in t.tried)
    texts = [(t.text, t.status == "ok") for t in transcripts]
    want = reconcile(texts, said_at)
    if not want.found:
        for t, (text, ok) in zip(transcripts, texts, strict=True):
            if read_promise(text, said_at, transcript_ok=ok).status == want.status:
                return replace(t, tried=tried)
        return replace(transcripts[0], tried=tried)
    best: tuple[int, Transcript] | None = None
    for t, (text, ok) in zip(transcripts, texts, strict=True):
        if not ok:
            continue
        r = read_promise(text, said_at)
        if not r.found or not _fits(r, want):
            continue
        score = 4 * (r.status == want.status) + (r.amount_kobo is not None) + (r.due_on is not None)
        if best is None or score > best[0]:
            best = (score, t)
    if best is not None:
        return replace(best[1], tried=tried)
    first = transcripts[0]
    return replace(first, status="low", tried=tried,
                   reason="transcribers disagree on the amount or the day")

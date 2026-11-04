"""Scheduled routines: work that happens without anyone asking (4 Nov 2026).

A routine is a code path that reads the book and files what it found on the
owner's thread, on a schedule in Africa/Lagos. Nothing is written to the book
and no model is called: the two routines here are the morning brief and the
aging scan, both of which already existed as code paths (the brief as the
``morning_brief`` demo routine, ``mimi.api.demo_wiring``).

The rules this module keeps:

* **Once per Lagos day, per routine.** ``routine_runs`` is the claim: its
  primary key is (tenant, routine, Lagos day), and the claim is one
  ``INSERT ... ON CONFLICT DO NOTHING``. A restart, or a second API process,
  loses that insert and does nothing. The claim is never released, so a run
  that dies half way is not repeated that day either; the next tick carries
  on with the other routines and the day after runs normally.
* **A pause is a row.** ``routine_state`` holds each routine's switch and the
  kill switch (routine ``*``, which stops every routine at once). It survives
  a restart and needs no deploy, which is what the owner reaches for at 3am.
  A paused routine records that it was skipped and why; the reason is in
  ``paused_reason`` and the read says it did not run.
* **The thread never takes the API down.** The scheduler catches every
  exception, records the failed run and logs it; the next tick tries again.
  A routine that throws cannot stop the queue or the API.
* **Findings are proposals or notes, never actions.** A routine returns a
  ``Finding``; the writer only ever puts it on the owner's thread as a
  message. A routine that would have to change a number must stop and hand
  the owner a proposal through the product's existing approval path, which
  these two do not need (they only report).

Schedule words are written for a person ("Every day at 8:00 AM"); times are
Africa/Lagos through ``mimi.tz`` and the one clock through ``mimi.clock``.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi import owed
from mimi.api import demo_wiring as dw
from mimi.clock import wall, wall_utc
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.store.util import insert_ignore, now_iso
from mimi.tz import LAGOS, lagos

log = logging.getLogger(__name__)

# One row with this id in ``routine_state`` pauses every routine.
ALL = "*"
ALL_WORD = "all"
# How late a missed schedule may still fire (a restart at 9am still sends the
# 8am brief). A window is cut at Lagos midnight, so it never reads yesterday's
# day as today's.
CATCH_UP = timedelta(hours=4)
# A day's claim is never retried, even after a crash, so the routine's message
# can never go out twice. A run that never finished is recorded as failed.
DEFAULT_TICK_S = 30.0
MIN_TICK_S = 1.0
MAX_REASON = 300
# Every routine here reads code and writes one message: no model port is held
# by the scheduler at all. The per-run model count is declared on each routine
# and checked in ``tests/test_routines.py``; the cap is 0 because the cost is 0.
MODEL_CALLS_PER_RUN = 0
NEVER_RUN = "It has not run yet."
DEFAULT_PAUSE_REASON = "Paused by the owner."
DEFAULT_ALL_REASON = "Every routine is paused."

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class UnknownRoutine(LookupError):
    """No routine has this id (and it is not the kill switch)."""


# ------------------------------------------------------------------ schedule


@dataclass(frozen=True)
class Schedule:
    """A daily time on the Lagos clock, and its words."""

    hour: int
    minute: int = 0

    def __post_init__(self) -> None:
        if not (0 <= self.hour < 24 and 0 <= self.minute < 60):
            raise ValueError(f"not a time of day: {self.hour}:{self.minute}")

    def words(self) -> str:
        """For a person: "Every day at 8:00 AM" (never a cron string)."""
        hour = self.hour % 12 or 12
        return f"Every day at {hour}:{self.minute:02d} {'AM' if self.hour < 12 else 'PM'}"

    def at(self, day: date) -> datetime:
        """This time on ``day``, with the Lagos zone."""
        return datetime(day.year, day.month, day.day, self.hour, self.minute, tzinfo=LAGOS)

    def next_after(self, now: datetime) -> datetime:
        """The first time this schedule comes round strictly after ``now``."""
        here = lagos(now)
        today = self.at(here.date())
        return today if today > here else self.at(here.date() + timedelta(days=1))

    def window_end(self, day: date) -> datetime:
        """The last moment a run for ``day`` may start (never past midnight)."""
        start = self.at(day)
        end = start + CATCH_UP
        nxt = day + timedelta(days=1)
        midnight = datetime(nxt.year, nxt.month, nxt.day, tzinfo=LAGOS)
        return min(end, midnight)


# ------------------------------------------------------------------ routines


@dataclass(frozen=True)
class Finding:
    """What a routine found: the thread's words and the one-line summary."""

    line: str
    text: str = ""


@dataclass(frozen=True)
class Routine:
    """One known routine: its id, its words, its schedule and its code path."""

    id: str
    name: str
    schedule: Schedule
    summary: str
    run: Callable[[Engine, str, date, datetime], Finding]
    # Model calls a run makes (0: code over the book). Declared so a future
    # routine that spends naira has to say so, and a test can hold the line.
    model_calls: int = MODEL_CALLS_PER_RUN


def _shops_word(n: int) -> str:
    return "shop" if n == 1 else "shops"


def _shop_phrase(n: int) -> str:
    return f"{n} {_shops_word(n)} have" if n != 1 else "1 shop has"


def _alerts_word(n: int) -> str:
    return "transfer alert" if n == 1 else "transfer alerts"


def _day_words(raw: str) -> str:
    """A stored ISO date as a person reads it: "1 Oct 2026"; else as stored."""
    try:
        d = date.fromisoformat(raw[:10])
    except (TypeError, ValueError):
        return str(raw or "")
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _morning_brief(engine: Engine, tenant: str, day: date,
                   now: datetime) -> Finding:
    """The read-only book: top overdue shops by name and ₦, summed in code."""
    debtors = sorted((b for b in dw.overdue_by_shop(engine, tenant).values()
                      if b.overdue_kobo > 0),
                     key=lambda b: (-b.overdue_kobo, b.shop_id))
    text = dw.brief_text(engine, tenant)
    if not debtors:
        return Finding(line="No shop is overdue.", text=text)
    total = sum(b.overdue_kobo for b in debtors)
    top = debtors[0]
    return Finding(
        line=(f"Top overdue: {top.shop_name} on {owed.naira(top.overdue_kobo)}; "
              f"{len(debtors)} {_shops_word(len(debtors))} overdue, "
              f"{owed.naira(total)} in all."),
        text=text)


def _unmatched_alerts(engine: Engine, tenant: str) -> list[dict[str, Any]]:
    """Transfer alerts no money backed (``payments.fake_alert``), oldest first."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(s.payments.c.shop_id, s.payments.c.amount_kobo,
                      s.payments.c.paid_on, s.shops.c.name)
            .join(s.shops, sa.and_(s.shops.c.tenant_id == s.payments.c.tenant_id,
                                   s.shops.c.shop_id == s.payments.c.shop_id))
            .where(s.payments.c.tenant_id == tenant, s.payments.c.fake_alert.is_(True))
            .order_by(s.payments.c.day, s.payments.c.payment_id)).all()
    return [{"shop_name": str(r.name), "amount_kobo": int(r.amount_kobo),
             "paid_on": str(r.paid_on)} for r in rows]


def _over_sixty(engine: Engine, tenant: str) -> list[dict[str, Any]]:
    """Per shop, the money whose invoice has passed 60 days (summed in code).

    A shop's whole overdue is not the figure: the scan names what is *more
    than 60 days old*, so only those receivables count. The shop keeps its
    oldest age and the sum of its 60+ rows.
    """
    shops: dict[str, dict[str, Any]] = {}
    for row in SqlGo(engine).receivables(tenant, None):
        days = int(row.get("days_overdue") or 0)
        kobo = int(row.get("outstanding_kobo") or 0)
        if days < 60 or kobo <= 0:
            continue
        entry = shops.setdefault(str(row["shop_id"]), {
            "shop_id": str(row["shop_id"]), "shop_name": str(row.get("shop_name") or ""),
            "overdue_kobo": 0, "oldest_days_overdue": 0})
        entry["overdue_kobo"] += kobo
        entry["oldest_days_overdue"] = max(entry["oldest_days_overdue"], days)
    return sorted(shops.values(), key=lambda s: (-s["overdue_kobo"], s["shop_id"]))


def _aging_scan(engine: Engine, tenant: str, day: date, now: datetime) -> Finding:
    """Shops past 60 days and transfer alerts that never matched a payment.

    Reads only: no receivable, payment or approval is written. Both figures
    are summed here, from the book's own rows."""
    over = _over_sixty(engine, tenant)
    alerts = _unmatched_alerts(engine, tenant)
    if not over and not alerts:
        return Finding(line="Nothing has passed 60 days and every transfer alert is matched.")
    parts: list[str] = []
    line = ""
    if over:
        shown_shops = over[:5]
        shop_lines = [f"{b['shop_name']} is {b['oldest_days_overdue']} days overdue on "
                      f"{owed.naira(b['overdue_kobo'])}" for b in shown_shops]
        more = len(over) - len(shown_shops)
        if more:
            shop_lines.append(f"and {more} more")
        total = sum(int(b["overdue_kobo"]) for b in over)
        parts.append(f"{_shop_phrase(len(over))} passed 60 days: {'; '.join(shop_lines)}. "
                     f"In all {owed.naira(total)} is more than 60 days old.")
        line = f"{_shop_phrase(len(over))} passed 60 days"
    if alerts:
        shown_alerts = alerts[:5]
        alert_lines = [f"{a['shop_name']} on {owed.naira(a['amount_kobo'])} from "
                       f"{_day_words(a['paid_on'])}" for a in shown_alerts]
        more = len(alerts) - len(shown_alerts)
        if more:
            alert_lines.append(f"and {more} more")
        parts.append(f"{len(alerts)} {_alerts_word(len(alerts))} never matched a payment: "
                     f"{'; '.join(alert_lines)}.")
        line = (f"{line} and {len(alerts)} {_alerts_word(len(alerts))} never matched a payment"
                if line else f"{len(alerts)} {_alerts_word(len(alerts))} never matched a payment")
    parts.append("Nothing has changed on the book.")
    return Finding(line=f"{line}.", text=" ".join(parts))


MORNING_BRIEF = Routine(
    id="morning_brief", name="Morning brief", schedule=Schedule(8, 0),
    summary="The shops most overdue, by name and naira, at the start of the day.",
    run=_morning_brief)
AGING_SCAN = Routine(
    id="aging_scan", name="Aging scan", schedule=Schedule(8, 30),
    summary="Shops past 60 days, and transfer alerts that never matched a payment.",
    run=_aging_scan)

ROUTINES: tuple[Routine, ...] = (MORNING_BRIEF, AGING_SCAN)
BY_ID: dict[str, Routine] = {r.id: r for r in ROUTINES}


def _resolve(routine_id: str) -> str:
    """The stored id: ``all`` and ``*`` are the kill switch, anything else must
    be a known routine."""
    rid = (routine_id or "").strip()
    if rid in (ALL, ALL_WORD):
        return ALL
    if rid not in BY_ID:
        raise UnknownRoutine(routine_id)
    return rid


def _name(rid: str) -> str:
    return "All routines" if rid == ALL else BY_ID[rid].name


# --------------------------------------------------------------- the store
# Two tables, both keyed by tenant: the switch (``routine_state``) and the
# day's claim (``routine_runs``). Every read and write is filtered by tenant.


def _now_lagos() -> datetime:
    return wall_utc().astimezone(LAGOS)


def _iso(dt: datetime) -> str:
    return dt.astimezone(LAGOS).isoformat(timespec="seconds")


def _stored_iso(raw: str) -> str:
    """A stored UTC stamp as the Lagos ISO a console words itself; "" if none."""
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        return ""
    return _iso(dt)


_STATE_DEFAULTS: dict[str, Any] = {
    "paused": False, "pause_reason": "", "paused_at": "", "last_run_day": "",
    "last_run_at": "", "last_status": "", "last_finding": "", "last_error": "",
    "last_skip_day": "", "last_missed_day": "", "updated_at": "",
}


def _state(engine: Engine, tenant: str, rid: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(sa.select(s.routine_state).where(
            s.routine_state.c.tenant_id == tenant,
            s.routine_state.c.routine == rid)).first()
    return {**_STATE_DEFAULTS, **dict(row._mapping)} if row is not None else dict(_STATE_DEFAULTS)


def _states(engine: Engine, tenant: str) -> dict[str, dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(s.routine_state).where(
            s.routine_state.c.tenant_id == tenant)).all()
    return {str(r.routine): {**_STATE_DEFAULTS, **dict(r._mapping)} for r in rows}


def _update_state(engine: Engine, tenant: str, rid: str, now: float,
                  **fields: Any) -> None:
    with engine.begin() as conn:
        insert_ignore(conn, s.routine_state, {
            "tenant_id": tenant, "routine": rid, **_STATE_DEFAULTS,
            "updated_at": now_iso(now)})
        conn.execute(s.routine_state.update().where(
            s.routine_state.c.tenant_id == tenant,
            s.routine_state.c.routine == rid).values(
            **fields, updated_at=now_iso(now)))


def _paused(state: dict[str, Any], everything: dict[str, Any]) -> tuple[bool, str]:
    if everything.get("paused"):
        return True, str(everything.get("pause_reason") or DEFAULT_ALL_REASON)
    if state.get("paused"):
        return True, str(state.get("pause_reason") or DEFAULT_PAUSE_REASON)
    return False, ""


def _claim(engine: Engine, tenant: str, rid: str, day: str, now: datetime,
           owner: str) -> bool:
    """Take the day's claim. True only for the one caller that wrote it."""
    with engine.begin() as conn:
        return insert_ignore(conn, s.routine_runs, {
            "tenant_id": tenant, "routine": rid, "day": day, "status": "running",
            "owner": owner[:160], "started_at": now_iso(now.timestamp()),
            "started_ts": now.timestamp(), "finished_at": "", "findings": 0,
            "result": "", "error": ""})


def _finish_claim(engine: Engine, tenant: str, rid: str, day: str, *, status: str,
                  result: str, error: str, findings: int, now: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(s.routine_runs.update().where(
            s.routine_runs.c.tenant_id == tenant,
            s.routine_runs.c.routine == rid,
            s.routine_runs.c.day == day).values(
            status=status, finished_at=now_iso(now.timestamp()), findings=findings,
            result=result[:2000], error=error[:500]))


def _owner_label() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


# --------------------------------------------------------------- the writer


def file_finding(engine: Engine, tenant: str, routine: Routine, day: str,
                 finding: Finding, now: datetime) -> None:
    """Put one finding on the owner's thread. The only write a routine makes.

    A quiet finding (empty text) writes nothing: the read still says what was
    checked. The message id carries the routine and the Lagos day, so even a
    retry could not put the same day's finding on the thread twice.
    """
    if not finding.text:
        return
    with engine.begin() as conn:
        thread = dw._owner_thread(conn, tenant)
        dw._ensure_thread(conn, tenant, thread, "owner", "", now.timestamp())
        dw._say(conn, tenant, thread, key=f"routine:{routine.id}:{day}",
                channel="owner_console", shop_id="", kind=routine.id,
                text=finding.text, now=now.timestamp())


FindingWriter = Callable[[Engine, str, Routine, str, Finding, datetime], None]


# ------------------------------------------------------------------ the tick


def run_due(engine: Engine, tenant: str, *, now: datetime | None = None,
            write: FindingWriter | None = None) -> list[dict[str, Any]]:
    """Fire every routine that is due for this tenant, once. Never raises.

    Returns one small record per routine for tests and logs (the action it
    took). ``now`` must carry its zone; it defaults to the one wall clock on
    the Lagos clock. A failing routine is recorded and skipped; the others
    still run.
    """
    here = lagos(now) if now is not None else _now_lagos()
    day = here.date().isoformat()
    writer = write or file_finding
    everything = _state(engine, tenant, ALL)
    out: list[dict[str, Any]] = []
    for routine in ROUTINES:
        try:
            out.append(_run_one(engine, tenant, routine, here, day, everything, writer))
        except Exception as exc:  # a routine never takes the tick down
            log.exception("routine %s failed for %s", routine.id, tenant)
            out.append({"routine": routine.id, "action": "error",
                        "error": type(exc).__name__})
    return out


def _run_one(engine: Engine, tenant: str, routine: Routine, now: datetime, day: str,
             everything: dict[str, Any], writer: FindingWriter) -> dict[str, Any]:
    state = _state(engine, tenant, routine.id)
    paused, reason = _paused(state, everything)
    scheduled = routine.schedule.at(now.date())
    window_end = routine.schedule.window_end(now.date())
    if paused:
        if now >= scheduled and state.get("last_skip_day") != day:
            _update_state(engine, tenant, routine.id, now.timestamp(),
                          last_skip_day=day, last_status="skipped",
                          last_finding="It did not run today: it is paused.")
        return {"routine": routine.id, "action": "skipped_paused", "reason": reason}
    if now < scheduled:
        return {"routine": routine.id, "action": "not_due"}
    if now >= window_end:
        if state.get("last_missed_day") != day:
            _update_state(engine, tenant, routine.id, now.timestamp(),
                          last_missed_day=day, last_status="missed",
                          last_finding=("It did not run today: the "
                                        f"{_clock_words(routine.schedule)} window passed "
                                        "while the API was down."))
        return {"routine": routine.id, "action": "missed"}
    if not _claim(engine, tenant, routine.id, day, now, _owner_label()):
        # Another process (or an earlier start of this one) already has today.
        return {"routine": routine.id, "action": "already_claimed"}
    try:
        finding = routine.run(engine, tenant, now.date(), now)
        writer(engine, tenant, routine, day, finding, now)
    except Exception as exc:
        log.exception("routine %s failed for %s while running", routine.id, tenant)
        _finish_claim(engine, tenant, routine.id, day, status="failed", result="",
                      error=type(exc).__name__, findings=0, now=now)
        _update_state(engine, tenant, routine.id, now.timestamp(),
                      last_status="failed", last_error=type(exc).__name__,
                      last_finding="Its last run did not finish. It will try again tomorrow.")
        return {"routine": routine.id, "action": "failed", "error": type(exc).__name__}
    _finish_claim(engine, tenant, routine.id, day, status="done",
                  result=finding.line, error="", findings=1 if finding.text else 0, now=now)
    _update_state(engine, tenant, routine.id, now.timestamp(),
                  last_run_day=day, last_run_at=now_iso(now.timestamp()),
                  last_status="done", last_finding=finding.line, last_error="")
    return {"routine": routine.id, "action": "ran", "finding": finding.line}


def _clock_words(schedule: Schedule) -> str:
    words = schedule.words()
    return words.split(" at ", 1)[1] if " at " in words else words


# --------------------------------------------------------------- the switch


def set_paused(engine: Engine, tenant: str, routine_id: str, paused: bool, *,
               reason: str = "", by: str = "", now: float | None = None) -> dict[str, Any]:
    """Pause or resume one routine, or ``all`` for the kill switch.

    The row is the whole mechanism: a restart reads it again, and no deploy is
    needed to stop everything. Pausing needs no reason, but one is recorded
    (a sentence) so the skip has an explanation.
    """
    rid = _resolve(routine_id)
    at = wall() if now is None else now
    if paused:
        text = (reason or "").strip()[:MAX_REASON]
        who = (by or "").strip()[:100]
        if not text:
            if rid == ALL:
                text = f"Every routine is paused by {who}." if who else DEFAULT_ALL_REASON
            else:
                text = f"Paused by {who}." if who else DEFAULT_PAUSE_REASON
        if text[-1] not in ".!?":
            text += "."
        if text[:1].islower():
            text = text[0].upper() + text[1:]
        fields = {"paused": True, "pause_reason": text, "paused_at": now_iso(at)}
    else:
        fields = {"paused": False, "pause_reason": "", "paused_at": ""}
    _update_state(engine, tenant, rid, at, **fields)
    if rid == ALL:
        effective = bool(fields["paused"])
    else:
        effective, _ = _paused(_state(engine, tenant, rid), _state(engine, tenant, ALL))
    return {"id": routine_id.strip() or rid, "name": _name(rid), "paused": effective}


# ------------------------------------------------------------------ the read


def view(engine: Engine, tenant: str, *, now: datetime | None = None) -> dict[str, Any]:
    """``GET /v1/routines``: the console's rail, each sentence written by code."""
    here = lagos(now) if now is not None else _now_lagos()
    states = _states(engine, tenant)
    everything = states.get(ALL, dict(_STATE_DEFAULTS))
    rows = []
    for routine in ROUTINES:
        state = states.get(routine.id, dict(_STATE_DEFAULTS))
        paused, reason = _paused(state, everything)
        row: dict[str, Any] = {
            "id": routine.id,
            "name": routine.name,
            "schedule": routine.schedule.words(),
            "paused": paused,
            "last_run_at": _stored_iso(str(state.get("last_run_at") or "")),
            "next_run_at": _iso(routine.schedule.next_after(here)),
            "last_finding": str(state.get("last_finding") or NEVER_RUN),
        }
        if paused:
            row["paused_reason"] = reason
        rows.append(row)
    return {"routines": rows, "as_of": _iso(here)}


# -------------------------------------------------------------- the thread


def tick_seconds() -> float:
    """How often the thread looks, from ``MIMI_ROUTINES_TICK_S`` (default 30 s)."""
    raw = os.environ.get("MIMI_ROUTINES_TICK_S", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TICK_S
    except ValueError:
        value = DEFAULT_TICK_S
    return max(MIN_TICK_S, value)


class RoutineScheduler:
    """One daemon thread inside the API process; ticks are short and safe.

    The thread starts one tick immediately and then waits ``tick_s`` between
    ticks. It never calls a model and never writes to the book. Every
    exception is caught inside :func:`run_due` and here, so a bad tick costs
    one log line, not the API.
    """

    def __init__(self, engine: Engine, *, tick_s: float | None = None,
                 tenants: Iterable[str] | None = None,
                 write: FindingWriter | None = None) -> None:
        self.engine = engine
        self.tick_s = tick_seconds() if tick_s is None else max(MIN_TICK_S, tick_s)
        self._tenants = None if tenants is None else tuple(tenants)
        self._write = write
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="mimi-routines")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None

    def tenants(self) -> list[str]:
        if self._tenants is not None:
            return list(self._tenants)
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(s.tenants.c.tenant_id)
                                .order_by(s.tenants.c.tenant_id)).all()
        return [str(r.tenant_id) for r in rows]

    def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """One pass over every tenant. Never raises."""
        here = lagos(now) if now is not None else _now_lagos()
        out: list[dict[str, Any]] = []
        try:
            tenants = self.tenants()
        except Exception:
            log.exception("the routine scheduler could not list tenants; the next tick will try")
            return out
        for tenant in tenants:
            try:
                out.extend(run_due(self.engine, tenant, now=here, write=self._write))
            except Exception:  # pragma: no cover - run_due already catches per routine
                log.exception("the routine tick failed for %s; the next tick will try", tenant)
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # pragma: no cover - tick catches its own failures
                log.exception("the routine tick failed; the next tick will try")
            if self._stop.wait(self.tick_s):
                break

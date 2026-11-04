"""Scheduled routines (4 Nov 2026): the tick, the claim, the switch, the read.

These cover what the brief asked to be proved: a happy tick, a tick that
throws, the Lagos midnight boundary, a paused routine (and the skip it
records), two schedulers racing, a restart mid-run, a restart after the
schedule (catch-up), and the exact console shape of ``GET /v1/routines``.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mimi.api import routines as rt
from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.api.demo import DemoConfig
from mimi.api.offline import OfflineModel
from mimi.store import schema as s
from mimi.store.db import ensure_schema, make_engine
from mimi.store.seed import seed_world
from mimi.tz import LAGOS
from mimi.world.loader import load_world

W = load_world()
T = W.tenant_id
KEYS = {"owner": "r-owner-key-00000000001", "chan": "r-chan-key-000000000001"}


def lagos_at(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=LAGOS)


@pytest.fixture()
def env(store_url: str) -> Iterator[dict[str, object]]:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    yield {"engine": engine, "url": store_url, "tenant": T}
    engine.dispose()


def _messages(engine: sa.engine.Engine, kind: str = "") -> list[dict[str, object]]:
    q = sa.select(s.messages.c.kind, s.messages.c.message_id, s.messages.c.text,
                  s.messages.c.channel, s.messages.c.thread_id).where(
        s.messages.c.tenant_id == T, s.messages.c.direction == "out")
    if kind:
        q = q.where(s.messages.c.kind == kind)
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(q.order_by(s.messages.c.ts)).all()]


def _runs(engine: sa.engine.Engine, routine: str = "") -> list[dict[str, object]]:
    q = sa.select(s.routine_runs).where(s.routine_runs.c.tenant_id == T)
    if routine:
        q = q.where(s.routine_runs.c.routine == routine)
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(q.order_by(s.routine_runs.c.day))]


def _states(engine: sa.engine.Engine) -> dict[str, dict[str, object]]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(s.routine_state).where(
            s.routine_state.c.tenant_id == T)).all()
    return {str(r.routine): dict(r._mapping) for r in rows}


def _actions(out: list[dict[str, object]]) -> dict[str, str]:
    return {str(r["routine"]): str(r["action"]) for r in out}


def _book_digest(engine: sa.engine.Engine) -> str:
    """Every number the routines must never change, in one digest."""
    with engine.connect() as conn:
        recv = conn.execute(sa.select(
            s.receivables.c.delivery_id, s.receivables.c.paid_kobo,
            s.receivables.c.outstanding_kobo, s.receivables.c.status).where(
            s.receivables.c.tenant_id == T).order_by(s.receivables.c.delivery_id)).all()
        pays = conn.execute(sa.select(
            s.payments.c.payment_id, s.payments.c.amount_kobo,
            s.payments.c.status).where(s.payments.c.tenant_id == T)
            .order_by(s.payments.c.payment_id)).all()
        approvals = conn.execute(sa.select(sa.func.count()).select_from(s.approvals)
                                 .where(s.approvals.c.tenant_id == T)).scalar()
    raw = repr([tuple(r) for r in recv] + [tuple(r) for r in pays] + [approvals])
    return hashlib.sha256(raw.encode()).hexdigest()


# ------------------------------------------------------------------ the tick


def test_the_morning_brief_runs_itself_at_eight_and_says_what_it_found(
        env: dict[str, object]) -> None:
    engine = env["engine"]
    before = _book_digest(engine)
    out = rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 0))
    assert _actions(out) == {"morning_brief": "ran", "aging_scan": "not_due"}
    [brief] = _messages(engine, "morning_brief")
    assert brief["channel"] == "owner_console"
    assert str(brief["thread_id"]) == f"{T}:owner"
    assert str(brief["text"]).startswith("Good morning.")
    assert "Mama Sola Provisions" in str(brief["text"])
    assert "₦7,231,000" in str(brief["text"])
    # No internal ids in words a person reads.
    assert "shop-" not in str(brief["text"]) and "dlv-" not in str(brief["text"])
    # The routine read the book and wrote a message: no number moved.
    assert _book_digest(engine) == before
    # The rail's one line names the one shop it is about, so the console can
    # link it; that link is a field, never a guess out of the sentence.
    row = {r["id"]: r for r in rt.view(
        engine, T, now=lagos_at(2026, 11, 4, 8, 1))["routines"]}["morning_brief"]
    assert row["last_finding"] == "Mama Sola Provisions owes ₦3,497,300. Of that, ₦2,387,250 is overdue, and the oldest invoice is 45 days old."
    assert row["last_finding_shop_id"] == "shop-001"


def test_the_aging_scan_files_shops_past_sixty_days_and_unmatched_alerts(
        env: dict[str, object]) -> None:
    engine = env["engine"]
    out = rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 30))
    assert _actions(out)["aging_scan"] == "ran"
    [scan] = _messages(engine, "aging_scan")
    text = str(scan["text"])
    # The seeded book's oldest money is 49 days, so today the finding is the
    # four transfer alerts no money backed. Naira and shop names, no ids.
    assert "4 transfer alerts never matched a payment" in text
    assert "Mama Sola Provisions" in text and "₦430,000" in text
    assert "shop-" not in text and "pay-" not in text
    assert text.endswith("Nothing has changed on the book.")
    # Four shops, so the rail's sentence stays plain text: no link.
    row = {r["id"]: r for r in rt.view(
        engine, T, now=lagos_at(2026, 11, 4, 8, 31))["routines"]}["aging_scan"]
    assert row["last_finding"] == "4 transfer alerts never matched a payment."
    assert "last_finding_shop_id" not in row


def test_the_aging_scan_names_shops_over_sixty_days(env: dict[str, object]) -> None:
    engine = env["engine"]
    # Age the book by 20 days: Mama Sola's oldest money goes from 45 to 65.
    with engine.begin() as conn:
        raw = conn.execute(sa.select(s.tenants.c.live_date).where(
            s.tenants.c.tenant_id == T)).scalar()
        aged = (date.fromisoformat(str(raw)) + timedelta(days=20)).isoformat()
        conn.execute(s.tenants.update().where(s.tenants.c.tenant_id == T)
                     .values(live_date=aged))
    out = rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 30))
    assert _actions(out)["aging_scan"] == "ran"
    text = str(_messages(engine, "aging_scan")[0]["text"])
    assert "shops have passed 60 days" in text or "shop has passed 60 days" in text
    assert "Mama Sola Provisions is 65 days overdue" in text


# --------------------------------------------------------------- once a day


def test_one_run_per_lagos_day_however_often_the_tick_fires(
        env: dict[str, object]) -> None:
    engine = env["engine"]
    assert _actions(rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 0)))[
        "morning_brief"] == "ran"
    for minute in (1, 5, 59):
        assert _actions(rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, minute)))[
            "morning_brief"] == "already_claimed"
    assert _actions(rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 11, 59)))[
        "morning_brief"] == "already_claimed"
    assert len(_messages(engine, "morning_brief")) == 1
    # The next Lagos day is a new claim.
    assert _actions(rt.run_due(engine, T, now=lagos_at(2026, 11, 5, 8, 0)))[
        "morning_brief"] == "ran"
    assert len(_messages(engine, "morning_brief")) == 2
    assert [r["day"] for r in _runs(engine, "morning_brief")] == [
        "2026-11-04", "2026-11-05"]


def test_a_restart_after_the_schedule_catches_up_inside_the_window(
        env: dict[str, object]) -> None:
    engine = env["engine"]
    # The API was down at 8:00 and comes up at 9:00: both windows are open.
    out = rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 9, 0))
    assert _actions(out) == {"morning_brief": "ran", "aging_scan": "ran"}
    assert len(_messages(engine)) == 2


def test_a_window_that_passed_is_recorded_as_missed_not_borrowed(
        env: dict[str, object]) -> None:
    engine = env["engine"]
    out = rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 13, 0))
    assert _actions(out) == {"morning_brief": "missed", "aging_scan": "missed"}
    assert _messages(engine) == []
    state = _states(engine)["morning_brief"]
    assert state["last_status"] == "missed"
    assert "8:00 AM window passed" in str(state["last_finding"])
    # The rail says it in the same plain words: no code, no blank row.
    row = {r["id"]: r for r in rt.view(
        engine, T, now=lagos_at(2026, 11, 4, 13, 1))["routines"]}["morning_brief"]
    assert row["last_finding"] == (
        "It did not run today: the 8:00 AM window passed while the API was down.")
    assert "last_finding_shop_id" not in row
    # The same day never records it twice, and the next day runs normally.
    rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 14, 0))
    assert _actions(rt.run_due(engine, T, now=lagos_at(2026, 11, 5, 8, 0)))[
        "morning_brief"] == "ran"


def test_the_lagos_midnight_boundary(env: dict[str, object]) -> None:
    engine = env["engine"]
    # 23:59 on 4 November is still the fourth; a tick then must not claim the
    # fifth, even though a 4 hour window would cross midnight.
    rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 23, 59))
    assert _runs(engine) == []
    # 00:00 on 5 November (23:00 UTC on the fourth) is a new Lagos day; the
    # morning brief is not due yet and its next run is that morning.
    assert _actions(rt.run_due(engine, T, now=lagos_at(2026, 11, 5, 0, 0)))[
        "morning_brief"] == "not_due"
    row = {r["id"]: r for r in rt.view(engine, T, now=lagos_at(2026, 11, 5, 0, 0))["routines"]}
    assert row["morning_brief"]["next_run_at"] == "2026-11-05T08:00:00+01:00"
    assert _actions(rt.run_due(engine, T, now=lagos_at(2026, 11, 5, 8, 0)))[
        "morning_brief"] == "ran"
    [run] = _runs(engine, "morning_brief")
    assert run["day"] == "2026-11-05"
    assert _messages(engine)[0]["message_id"] == "out:routine:morning_brief:2026-11-05"


def test_a_late_schedule_window_is_cut_at_midnight() -> None:
    late = rt.Schedule(23, 30)
    assert late.at(date(2026, 11, 4)) == lagos_at(2026, 11, 4, 23, 30)
    assert late.window_end(date(2026, 11, 4)) == lagos_at(2026, 11, 5, 0, 0)
    assert rt.Schedule(8, 0).next_after(lagos_at(2026, 11, 4, 23, 59)) == \
        lagos_at(2026, 11, 5, 8, 0)
    assert rt.Schedule(8, 0).next_after(lagos_at(2026, 11, 5, 0, 0)) == \
        lagos_at(2026, 11, 5, 8, 0)
    assert rt.Schedule(8, 0).next_after(lagos_at(2026, 11, 5, 8, 0)) == \
        lagos_at(2026, 11, 6, 8, 0)


# -------------------------------------------------------- restarts and races


def test_a_restart_mid_run_never_fires_the_same_day_twice(
        env: dict[str, object]) -> None:
    engine = env["engine"]
    first = lagos_at(2026, 11, 4, 8, 0)
    # The old process claimed the day and died before filing anything.
    assert rt._claim(engine, T, "morning_brief", "2026-11-04", first, "dead-process")
    # The new process ticks twice: the claim is the truth, not memory.
    for minute in (1, 2):
        out = rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, minute))
        assert _actions(out)["morning_brief"] == "already_claimed"
    restarted = rt.RoutineScheduler(engine, tenants=[T])
    restarted.tick(now=lagos_at(2026, 11, 4, 8, 3))
    assert _messages(engine, "morning_brief") == []
    [run] = _runs(engine, "morning_brief")
    assert run["owner"] == "dead-process"
    assert run["status"] == "running"   # recorded, not papered over


def test_two_schedulers_at_once_fire_each_routine_once(
        env: dict[str, object]) -> None:
    engine = env["engine"]
    now = lagos_at(2026, 11, 4, 8, 30)
    one = rt.RoutineScheduler(engine, tenants=[T])
    two = rt.RoutineScheduler(engine, tenants=[T])
    barrier = threading.Barrier(2)

    def go(scheduler: rt.RoutineScheduler) -> list[dict[str, object]]:
        barrier.wait(timeout=5)
        return scheduler.tick(now=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        both = list(pool.map(go, (one, two)))
    actions = [_actions(out) for out in both]
    assert sorted(a["morning_brief"] for a in actions) == ["already_claimed", "ran"]
    assert sorted(a["aging_scan"] for a in actions) == ["already_claimed", "ran"]
    assert len(_messages(engine, "morning_brief")) == 1
    assert len(_messages(engine, "aging_scan")) == 1
    assert {r["status"] for r in _runs(engine)} == {"done"}


def test_a_tick_that_throws_is_recorded_and_does_not_stop_the_others(
        env: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    engine = env["engine"]

    def boom(*_: object) -> rt.Finding:
        raise RuntimeError("the book read failed")

    bad = rt.Routine(id="bad_routine", name="Bad routine", schedule=rt.Schedule(8, 0),
                     summary="always throws", run=boom)
    monkeypatch.setattr(rt, "ROUTINES", (bad, rt.AGING_SCAN))
    out = rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 30))
    assert _actions(out) == {"bad_routine": "failed", "aging_scan": "ran"}
    assert len(_messages(engine, "aging_scan")) == 1
    state = _states(engine)["bad_routine"]
    assert state["last_status"] == "failed"
    assert state["last_finding"] == "Its last run did not finish. It will try again tomorrow."
    # The rail shows the sentence, not the exception's name.
    row = {r["id"]: r for r in rt.view(
        engine, T, now=lagos_at(2026, 11, 4, 9, 1))["routines"]}["bad_routine"]
    assert row["last_finding"] == "Its last run did not finish. It will try again tomorrow."
    assert "RuntimeError" not in row["last_finding"]
    assert "last_finding_shop_id" not in row
    # The claim is kept: the same day does not retry a routine that failed.
    assert _actions(rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 9, 0)))[
        "bad_routine"] == "already_claimed"


def test_the_scheduler_thread_survives_a_tick_that_throws(
        env: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    engine = env["engine"]
    calls: list[float] = []

    def boom(*_: object, **__: object) -> list[dict[str, object]]:
        calls.append(time.monotonic())
        raise RuntimeError("the database is down")

    monkeypatch.setattr(rt, "run_due", boom)
    scheduler = rt.RoutineScheduler(engine, tick_s=1, tenants=[T])
    scheduler.start()
    try:
        deadline = time.monotonic() + 3
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(calls) >= 2, "the thread stopped after the first bad tick"
        assert scheduler.running
    finally:
        scheduler.stop()
    assert not scheduler.running


# --------------------------------------------------------- the owner's run


def test_the_owner_can_run_a_routine_now_and_only_once(env: dict[str, object]) -> None:
    """The demo button: the same path as a tick, the same day's claim."""
    engine = env["engine"]
    row = rt.run_now(engine, T, "morning_brief", now=lagos_at(2026, 11, 4, 3, 0))
    assert row["last_run_at"] == "2026-11-04T03:00:00+01:00"
    assert row["next_run_at"] == "2026-11-04T08:00:00+01:00"
    assert row["last_finding"] == (
        "Mama Sola Provisions owes ₦3,497,300. Of that, ₦2,387,250 is overdue, and the oldest invoice is 45 days old.")
    assert row["last_finding_shop_id"] == "shop-001"
    assert len(_messages(engine, "morning_brief")) == 1

    with pytest.raises(rt.RoutineRefused) as refused:
        rt.run_now(engine, T, "morning_brief", now=lagos_at(2026, 11, 4, 3, 1))
    assert str(refused.value) == (
        "It has already run today. It runs again tomorrow at 8:00 AM.")
    assert len(_messages(engine, "morning_brief")) == 1

    # The scheduled tick later that day sees the manual run's claim.
    assert _actions(rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 0)))[
        "morning_brief"] == "already_claimed"
    assert len(_messages(engine, "morning_brief")) == 1


def test_the_owner_run_refuses_a_paused_routine_with_its_reason(
        env: dict[str, object]) -> None:
    engine = env["engine"]
    rt.set_paused(engine, T, "morning_brief", True, reason="Checking the early run",
                  now=lagos_at(2026, 11, 4, 2, 0).timestamp())
    with pytest.raises(rt.RoutineRefused) as refused:
        rt.run_now(engine, T, "morning_brief", now=lagos_at(2026, 11, 4, 3, 0))
    assert str(refused.value) == (
        "It is paused, so it did not run. The reason: Checking the early run.")
    assert _messages(engine, "morning_brief") == []
    row = {r["id"]: r for r in rt.view(
        engine, T, now=lagos_at(2026, 11, 4, 3, 1))["routines"]}["morning_brief"]
    assert row["paused"] is True
    assert row["paused_reason"] == "Checking the early run."
    assert row["last_finding"] == "It did not run today: it is paused."
    assert row["last_run_at"] == ""


def test_the_owner_run_refuses_the_kill_switch_with_its_reason(
        env: dict[str, object]) -> None:
    engine = env["engine"]
    rt.set_paused(engine, T, "all", True, reason="A bad deployment at 3am",
                  now=lagos_at(2026, 11, 4, 2, 0).timestamp())
    with pytest.raises(rt.RoutineRefused) as refused:
        rt.run_now(engine, T, "aging_scan", now=lagos_at(2026, 11, 4, 3, 0))
    assert str(refused.value) == (
        "It is paused, so it did not run. The reason: A bad deployment at 3am.")


def test_the_owner_run_does_not_accept_all_or_an_unknown_id(
        env: dict[str, object]) -> None:
    engine = env["engine"]
    for bad in ("all", "*", "pay_everyone"):
        with pytest.raises(rt.UnknownRoutine):
            rt.run_now(engine, T, bad, now=lagos_at(2026, 11, 4, 3, 0))


# --------------------------------------------------------------- the switch


def test_a_paused_routine_records_its_skip_and_resumes_the_same_day(
        env: dict[str, object]) -> None:
    engine = env["engine"]
    rt.set_paused(engine, T, "aging_scan", True, reason="Checking the early run",
                  by="Tobi", now=lagos_at(2026, 11, 4, 8, 0).timestamp())
    out = rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 30))
    assert _actions(out) == {"morning_brief": "ran", "aging_scan": "skipped_paused"}
    assert _messages(engine, "aging_scan") == []
    state = _states(engine)["aging_scan"]
    assert state["last_status"] == "skipped"
    assert state["last_skip_day"] == "2026-11-04"
    assert state["last_finding"] == "It did not run today: it is paused."
    row = {r["id"]: r for r in rt.view(engine, T, now=lagos_at(2026, 11, 4, 9, 0))["routines"]}
    assert row["aging_scan"]["paused"] is True
    assert row["aging_scan"]["paused_reason"] == "Checking the early run."
    assert row["aging_scan"]["last_run_at"] == ""
    assert row["aging_scan"]["last_finding"] == "It did not run today: it is paused."
    # A pause is not a claim: resuming inside the window still runs today.
    rt.set_paused(engine, T, "aging_scan", False, now=lagos_at(2026, 11, 4, 9, 0).timestamp())
    assert _actions(rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 9, 1)))[
        "aging_scan"] == "ran"
    assert len(_messages(engine, "aging_scan")) == 1


def test_the_pause_survives_a_restart(env: dict[str, object]) -> None:
    engine = env["engine"]
    rt.set_paused(engine, T, "morning_brief", True, reason="the scheduler misbehaved",
                  now=time.time())
    # A new engine to the same database is what a restart looks like here.
    fresh = make_engine(str(env["url"]))
    try:
        rows = {r["id"]: r for r in rt.view(fresh, T)["routines"]}
        assert rows["morning_brief"]["paused"] is True
        assert rows["morning_brief"]["paused_reason"] == "The scheduler misbehaved."
        assert rows["aging_scan"]["paused"] is False
    finally:
        fresh.dispose()


def test_one_row_is_the_kill_switch_for_every_routine(env: dict[str, object]) -> None:
    engine = env["engine"]
    out = rt.set_paused(engine, T, "all", True, reason="Something is wrong at 3am")
    assert out == {"id": "all", "name": "All routines", "paused": True}
    assert set(_states(engine)) == {"*"}
    more = rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 30))
    assert _actions(more) == {"morning_brief": "skipped_paused", "aging_scan": "skipped_paused"}
    assert _messages(engine) == []
    rows = {r["id"]: r for r in rt.view(engine, T, now=lagos_at(2026, 11, 4, 9, 0))["routines"]}
    assert all(r["paused"] for r in rows.values())
    assert all(r["paused_reason"] == "Something is wrong at 3am." for r in rows.values())
    # Resuming one routine while the kill switch is on is not a lie: it stays
    # paused, and says so.
    assert rt.set_paused(engine, T, "morning_brief", False)["paused"] is True
    # One row, resumed, lets everything run again.
    assert rt.set_paused(engine, T, "all", False) == {
        "id": "all", "name": "All routines", "paused": False}
    assert _actions(rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 9, 1)))[
        "morning_brief"] == "ran"


def test_an_unknown_routine_is_not_a_switch(env: dict[str, object]) -> None:
    with pytest.raises(rt.UnknownRoutine):
        rt.set_paused(env["engine"], T, "pay_everyone", True)


# ------------------------------------------------------------ no model, no money


def test_a_routine_has_no_model_and_never_writes_the_book(
        env: dict[str, object]) -> None:
    engine = env["engine"]
    assert all(r.model_calls == 0 for r in rt.ROUTINES)
    before = _book_digest(engine)
    rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 0))
    rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 30))
    assert _book_digest(engine) == before
    # The scheduler is constructed with an engine and nothing else: no model,
    # no agent, no connection to one. The spend table stays empty.
    with engine.connect() as conn:
        spent = conn.execute(sa.select(sa.func.count()).select_from(s.spend)
                             .where(s.spend.c.tenant_id == T)).scalar()
    assert spent == 0


# ------------------------------------------------------------------ the read


def test_the_routine_reads_one_line_and_never_borrows_the_clock(
        env: dict[str, object]) -> None:
    engine = env["engine"]
    now = lagos_at(2026, 11, 4, 7, 0)
    before = rt.view(engine, T, now=now)
    assert before["as_of"] == "2026-11-04T07:00:00+01:00"
    rows = {r["id"]: r for r in before["routines"]}
    assert set(rows) == {"morning_brief", "aging_scan"}
    assert rows["morning_brief"] == {
        "id": "morning_brief", "name": "Morning briefing", "schedule": "Every day at 8:00 AM",
        "paused": False, "last_run_at": "", "next_run_at": "2026-11-04T08:00:00+01:00",
        "last_finding": ""}
    rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 0))
    after = {r["id"]: r for r in rt.view(engine, T, now=lagos_at(2026, 11, 4, 8, 1))["routines"]}
    assert after["morning_brief"]["last_run_at"] == "2026-11-04T08:00:00+01:00"
    assert after["morning_brief"]["next_run_at"] == "2026-11-05T08:00:00+01:00"
    assert after["morning_brief"]["last_finding"] == (
        "Mama Sola Provisions owes ₦3,497,300. Of that, ₦2,387,250 is overdue, and the oldest invoice is 45 days old.")
    assert after["morning_brief"]["last_finding_shop_id"] == "shop-001"
    assert after["aging_scan"]["last_run_at"] == ""
    assert after["aging_scan"]["last_finding"] == ""


# ------------------------------------------------------------------- the API


@pytest.fixture()
def api(env: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, object]]:
    monkeypatch.delenv("MIMI_ROUTINES", raising=False)
    engine = env["engine"]
    agent = Agent(engine, models=[OfflineModel()], worker_id="test-worker")
    keys = parse_keys(f"{T}:owner:{KEYS['owner']},{T}:channel:{KEYS['chan']}")
    with TestClient(create_app(Settings(keys=keys, wait_s=5), agent, workers=0)) as c:
        yield {"c": c, "engine": engine, "agent": agent}


def hk(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {KEYS[name]}"}


def test_get_routines_has_the_console_shape(api: dict[str, object]) -> None:
    c = api["c"]
    r = c.get("/v1/routines", headers=hk("owner"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"routines", "as_of"}
    assert body["as_of"].endswith("+01:00")
    assert len(body["routines"]) == 2
    for row in body["routines"]:
        expected = {"id", "name", "schedule", "paused", "last_run_at", "next_run_at",
                    "last_finding"}
        assert set(row) == expected
        assert row["paused"] is False
        assert row["last_run_at"] == ""
        assert row["last_finding"] == ""
        assert " at " in row["schedule"]
        next_at = datetime.fromisoformat(row["next_run_at"])
        assert next_at.tzinfo is not None and next_at.utcoffset().total_seconds() == 3600
    assert c.get("/v1/routines", headers=hk("chan")).status_code == 403
    assert c.get("/v1/routines").status_code == 401


def test_the_api_carries_the_one_shop_only_when_there_is_one(
        api: dict[str, object]) -> None:
    c = api["c"]
    engine = api["engine"]
    rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 0))
    rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 30))
    rows = {x["id"]: x for x in c.get("/v1/routines", headers=hk("owner")).json()["routines"]}
    assert rows["morning_brief"]["last_finding_shop_id"] == "shop-001"
    assert rows["morning_brief"]["last_finding"] == (
        "Mama Sola Provisions owes ₦3,497,300. Of that, ₦2,387,250 is overdue, and the oldest invoice is 45 days old.")
    assert "last_finding_shop_id" not in rows["aging_scan"]


def test_the_run_endpoint_answers_the_row_and_refuses_twice(
        api: dict[str, object]) -> None:
    c = api["c"]
    engine = api["engine"]
    r = c.post("/v1/routines/morning_brief/run", headers=hk("owner"))
    assert r.status_code == 200, r.text
    row = r.json()
    assert set(row) == {"id", "name", "schedule", "paused", "last_run_at",
                        "next_run_at", "last_finding", "last_finding_shop_id"}
    assert row["name"] == "Morning briefing"
    assert row["last_run_at"].endswith("+01:00")
    assert row["last_finding"] == (
        "Mama Sola Provisions owes ₦3,497,300. Of that, ₦2,387,250 is overdue, and the oldest invoice is 45 days old.")
    assert row["last_finding_shop_id"] == "shop-001"
    assert len(_messages(engine, "morning_brief")) == 1

    # One run a day is the contract: the second answers with words, not a
    # second finding on the owner's thread.
    again = c.post("/v1/routines/morning_brief/run", headers=hk("owner"))
    assert again.status_code == 409
    assert again.json()["detail"] == (
        "It has already run today. It runs again tomorrow at 8:00 AM.")
    assert len(_messages(engine, "morning_brief")) == 1

    # The rail is the same row the run answered.
    rail = {x["id"]: x for x in c.get("/v1/routines",
                                     headers=hk("owner")).json()["routines"]}
    assert rail["morning_brief"] == row
    # No model call: the button costs nothing.
    with engine.connect() as conn:
        spent = conn.execute(sa.select(sa.func.count()).select_from(s.spend)
                             .where(s.spend.c.tenant_id == T)).scalar()
    assert spent == 0

    # Paused and unknown refuse; the channel key and no key are refused first.
    c.post("/v1/routines/aging_scan/pause", json={"reason": "Hold it"},
           headers=hk("owner"))
    paused = c.post("/v1/routines/aging_scan/run", headers=hk("owner"))
    assert paused.status_code == 409
    assert paused.json()["detail"] == (
        "It is paused, so it did not run. The reason: Hold it.")
    assert c.post("/v1/routines/pay_everyone/run",
                  headers=hk("owner")).status_code == 404
    assert c.post("/v1/routines/all/run", headers=hk("owner")).status_code == 404
    assert c.post("/v1/routines/morning_brief/run",
                  headers=hk("chan")).status_code == 403
    assert c.post("/v1/routines/morning_brief/run").status_code == 401


def test_pause_and_resume_answer_the_switch_and_persist(api: dict[str, object]) -> None:
    c = api["c"]
    r = c.post("/v1/routines/morning_brief/pause",
               json={"reason": "Checking the scheduler", "by": "Tobi"},
               headers=hk("owner"))
    assert r.status_code == 200, r.text
    assert r.json() == {"id": "morning_brief", "name": "Morning briefing", "paused": True}
    row = {x["id"]: x for x in c.get("/v1/routines", headers=hk("owner")).json()["routines"]}
    assert row["morning_brief"]["paused"] is True
    assert row["morning_brief"]["paused_reason"] == "Checking the scheduler."
    assert row["aging_scan"]["paused"] is False
    assert "paused_reason" not in row["aging_scan"]
    assert c.post("/v1/routines/morning_brief/pause", headers=hk("chan")).status_code == 403
    assert c.post("/v1/routines/pay_everyone/pause",
                  headers=hk("owner")).status_code == 404
    r = c.post("/v1/routines/morning_brief/resume", headers=hk("owner"))
    assert r.status_code == 200
    assert r.json() == {"id": "morning_brief", "name": "Morning briefing", "paused": False}


def test_the_kill_switch_through_the_api(api: dict[str, object]) -> None:
    c = api["c"]
    r = c.post("/v1/routines/all/pause", json={"reason": "A bad deployment at 3am"},
               headers=hk("owner"))
    assert r.status_code == 200
    assert r.json() == {"id": "all", "name": "All routines", "paused": True}
    rows = {x["id"]: x for x in c.get("/v1/routines", headers=hk("owner")).json()["routines"]}
    assert all(x["paused"] is True for x in rows.values())
    assert all(x["paused_reason"] == "A bad deployment at 3am." for x in rows.values())
    assert c.post("/v1/routines/all/resume", headers=hk("owner")).json()["paused"] is False


def test_a_pause_survives_an_api_restart(env: dict[str, object],
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIMI_ROUTINES", raising=False)
    engine = env["engine"]
    keys = parse_keys(f"{T}:owner:{KEYS['owner']}")
    settings = Settings(keys=keys, wait_s=5)

    def app() -> TestClient:
        return TestClient(create_app(settings, Agent(engine, models=[OfflineModel()],
                                                     worker_id="test-worker"), workers=0))

    with app() as first:
        assert first.post("/v1/routines/aging_scan/pause", json={"reason": "hold it"},
                          headers=hk("owner")).json()["paused"] is True
    # A fresh process reads the row, not a flag in the old one.
    with app() as second:
        row = {x["id"]: x for x in second.get("/v1/routines",
                                             headers=hk("owner")).json()["routines"]}
        assert row["aging_scan"]["paused"] is True
        assert row["aging_scan"]["paused_reason"] == "Hold it."


def test_the_api_starts_the_scheduler_only_behind_the_switch(
        env: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    engine = env["engine"]
    keys = parse_keys(f"{T}:owner:{KEYS['owner']}")
    settings = Settings(keys=keys, wait_s=5)

    monkeypatch.delenv("MIMI_ROUTINES", raising=False)
    with TestClient(create_app(settings, Agent(engine, models=[OfflineModel()]),
                               workers=0)) as off:
        pass
    assert off.app.state.routines is None

    ticks: list[int] = []
    monkeypatch.setattr(rt.RoutineScheduler, "tick",
                        lambda self, now=None: ticks.append(1) or [])
    monkeypatch.setenv("MIMI_ROUTINES", "1")
    with TestClient(create_app(settings, Agent(engine, models=[OfflineModel()]),
                               workers=0)) as on:
        deadline = time.monotonic() + 3
        while not ticks and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ticks, "the scheduler thread never ticked"
        assert on.app.state.routines is not None
        assert on.app.state.routines.running is True
    assert on.app.state.routines.running is False


def test_a_finding_about_one_shop_carries_that_shop(env: dict[str, object]) -> None:
    """The console links a shop only when the sentence is about one shop."""
    engine = env["engine"]
    with engine.begin() as conn:
        rows = conn.execute(sa.select(s.receivables.c.delivery_id,
                                      s.receivables.c.shop_id)
                            .where(s.receivables.c.tenant_id == T)).all()
        for delivery_id, shop_id in rows:
            if shop_id != "shop-001":
                conn.execute(sa.update(s.receivables)
                             .where(s.receivables.c.tenant_id == T,
                                    s.receivables.c.delivery_id == delivery_id)
                             .values(outstanding_kobo=0, status="paid"))
    rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 0))
    row = {r["id"]: r for r in rt.view(
        engine, T, now=lagos_at(2026, 11, 4, 8, 1))["routines"]}["morning_brief"]
    assert row["last_finding"] == "Mama Sola Provisions owes ₦3,497,300. Of that, ₦2,387,250 is overdue, and the oldest invoice is 45 days old."
    assert row["last_finding_shop_id"] == "shop-001"


def test_a_lone_unmatched_alert_names_its_shop(env: dict[str, object]) -> None:
    engine = env["engine"]
    with engine.begin() as conn:
        conn.execute(sa.delete(s.payments)
                     .where(s.payments.c.tenant_id == T,
                            s.payments.c.fake_alert.is_(True),
                            s.payments.c.shop_id != "shop-017"))
    rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 30))
    row = {r["id"]: r for r in rt.view(
        engine, T, now=lagos_at(2026, 11, 4, 8, 31))["routines"]}["aging_scan"]
    assert row["last_finding"] == (
        "A transfer alert from Kehinde POS & Provisions never matched a payment.")
    assert row["last_finding_shop_id"] == "shop-017"


def test_a_skip_clears_the_shop_the_last_finding_named(env: dict[str, object]) -> None:
    engine = env["engine"]
    rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 0))
    rt.set_paused(engine, T, "morning_brief", True, reason="hold it",
                  now=lagos_at(2026, 11, 4, 8, 2).timestamp())
    rt.run_due(engine, T, now=lagos_at(2026, 11, 4, 8, 5))
    row = {r["id"]: r for r in rt.view(
        engine, T, now=lagos_at(2026, 11, 4, 8, 6))["routines"]}["morning_brief"]
    assert row["last_finding"] == "It did not run today: it is paused."
    assert "last_finding_shop_id" not in row


def test_the_idle_loop_holds_no_connection(env: dict[str, object],
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """A sleeping tick loop must not keep a pooled connection checked out."""
    engine = env["engine"]
    checked: list[int] = []
    real_run_due = rt.run_due

    def spy(*_a: object, **_k: object) -> list[dict[str, object]]:
        checked.append(int(engine.pool.checkedout()))
        return []

    monkeypatch.setattr(rt, "run_due", spy)
    scheduler = rt.RoutineScheduler(engine, tick_s=1, tenants=[T])
    scheduler.start()
    try:
        deadline = time.monotonic() + 4
        while len(checked) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        scheduler.stop()
    assert len(checked) >= 2, "the scheduler never ticked"
    assert max(checked) == 0

    # And a real tick closes what it opened.
    monkeypatch.setattr(rt, "run_due", real_run_due)
    real_run_due(engine, T, now=lagos_at(2026, 11, 4, 7, 0))
    assert engine.pool.checkedout() == 0


def test_one_scheduler_per_process_even_with_worker_threads(
        env: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    """MIMI_WORKERS adds turn threads, not processes, so they share one scheduler."""
    engine = env["engine"]
    monkeypatch.setenv("MIMI_ROUTINES", "1")
    settings = Settings(keys=parse_keys(f"{T}:owner:{KEYS['owner']}"), wait_s=5,
                        workers=4)
    with TestClient(create_app(settings, Agent(engine, models=[OfflineModel()]))) as app:
        named = [t for t in threading.enumerate() if t.name == "mimi-routines"]
        assert len(named) == 1
        assert app.app.state.routines is not None


def test_the_demo_kill_switch_starts_no_routines(env: dict[str, object],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """The kill switch is the thing reached for in front of a buyer: routines
    stop with the turns."""
    engine = env["engine"]
    monkeypatch.setenv("MIMI_ROUTINES", "1")
    settings = Settings(keys=parse_keys(f"{T}:owner:{KEYS['owner']}"), wait_s=5,
                        mode="demo", demo=DemoConfig(killed=True))
    with TestClient(create_app(settings, Agent(engine, models=[OfflineModel()]),
                               workers=0)) as app:
        assert app.app.state.routines is None
        assert [t for t in threading.enumerate() if t.name == "mimi-routines"] == []

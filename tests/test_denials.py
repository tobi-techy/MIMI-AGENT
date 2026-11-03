"""The denial circuit breaker (RAI-324): a stuck helper stops and asks the owner.

Three denials in a row in one turn park the turn (``stopped_denied``); ten
denials in the thread's last 50 finished-turn reviews trip it too. A reply
after the step's own refusal must ask for a next step, never claim success.
Everything is offline: scripted models, the in-process Go stand-in.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from typing import Any

import pytest

from mimi.harness import claims as C
from mimi.harness import denials as D
from mimi.harness.collector import collector_helper, default_registry
from mimi.harness.denials_eval import CASES, judge, main
from mimi.harness.gateway import Gateway, ScriptedModel, say
from mimi.harness.go import FakeGo
from mimi.harness.loop import Harness, LoopState
from mimi.harness.policy import GoPolicy
from mimi.harness.types import RunEvent
from mimi.world.loader import load_world

WORLD = load_world()
AT = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
TENANT = WORLD.tenant_id


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*a: Any, **k: Any) -> None:
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)


@pytest.mark.parametrize("case", CASES, ids=[c.cid for c in CASES])
def test_denials_case(case: Any) -> None:
    row = judge(case)
    assert row["ok"], row


def _run(*turns: Any, withheld: tuple[str, ...] = ()) -> Any:
    model = ScriptedModel(list(turns) + [say("nothing left to do")] * 4)
    go = FakeGo(WORLD, withheld={s: frozenset({"shop:*"}) for s in withheld})
    helper = collector_helper(default_registry(), GoPolicy())
    h = Harness(book=go, control=go, events=go, gateway=Gateway([model]), policy=GoPolicy(),
                clock=lambda: AT)
    ev = RunEvent(event_id="t", tenant_id=TENANT, thread_id="t:denial",
                  wake_source="owner_message", text="carry on", sender="owner", at=AT)
    state = h.start(ev, helper)
    r = None
    for _ in range(len(turns) + 4):
        res = h.step(state, helper)
        state = LoopState.model_validate(res.state)
        if res.finished:
            r = res.result
            break
    assert r is not None
    return r


def test_three_consecutive_denials_park_with_an_owner_question() -> None:
    r = _run(
        say("", ("get_balance", {"shop_id": "shop-001"})),
        say("", ("get_invoices", {"shop_id": "shop-001"})),
        say("", ("get_payment_history", {"shop_id": "shop-001"})),
        withheld=("shop-001",))
    assert r.status == "stopped_denied"
    tripped = [e for e in r.events if e.type == "breaker_tripped"]
    assert tripped and tripped[0].data["kind"] == "in_a_row"
    # Nothing dispatched after the trip: one parked question, no message draft.
    assert [d for d in r.outbound_drafts if d.kind != "question"] == []
    assert r.owner_question and "3 times in a row" in r.owner_question
    assert "Nothing more was sent" in r.owner_question
    # No em dashes in the owner note (house style).
    assert "\u2014" not in r.owner_question and "--" not in r.owner_question


def test_progress_resets_the_streak() -> None:
    r = _run(
        say("", ("get_balance", {"shop_id": "shop-001"})),
        say("", ("get_invoices", {"shop_id": "shop-002"}),
            ("send_to_user", {"channel": "owner_console",
                              "body": "Iya Tolu Foodstuff owes N1,056,450.",
                              "ends_turn": True})),
        withheld=("shop-001",))
    assert r.status != "stopped_denied"


def test_window_rule_needs_no_in_run_denial() -> None:
    model = ScriptedModel([say("", ("get_balance", {"shop_id": "shop-001"}),
                                 ("send_to_user", {"channel": "owner_console",
                                                   "body": "Iya Tolu Foodstuff owes N1,056,450.",
                                                   "ends_turn": True}))])
    go = FakeGo(WORLD)
    helper = collector_helper(default_registry(), GoPolicy())
    h = Harness(book=go, control=go, events=go, gateway=Gateway([model]), policy=GoPolicy(),
                clock=lambda: AT)
    ev = RunEvent(event_id="t", tenant_id=TENANT, thread_id="t:window",
                  wake_source="owner_message", text="carry on", sender="owner", at=AT)
    state = h.start(ev, helper)
    state.denial_window_denials, state.denial_window_size = (10, 50)
    state = LoopState.model_validate(state.model_dump())
    res = h.step(state, helper)
    assert res.finished is not None and res.result.status == "stopped_denied"
    kinds = {str((e.data or {}).get("kind")) for e in res.result.events
             if e.type == "breaker_tripped"}
    assert "window" in kinds


def test_a_reply_after_a_refusal_must_ask_not_claim() -> None:
    msgs: list[dict[str, Any]] = [
        {"role": "assistant", "tool_calls": [{"id": "1"}]},
        {"role": "tool",
         "content": '{"denied":true,"reason":"no","next_step":"do not use it"}'},
    ]
    assert C.dishonest_success("I have sent the reminder.", messages=msgs, intents=[])
    assert C.dishonest_success("I have read and checked the balance.",
                               messages=msgs, intents=[])
    assert C.dishonest_success("The follow-up is scheduled and booked.",
                               messages=msgs, intents=[])
    # No refusal behind it: the gate stays quiet (the outcomes check judges it).
    ok: list[dict[str, Any]] = [
        {"role": "assistant", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": '{"ok": true, "data": {"a": 1}}'},
    ]
    assert C.dishonest_success("Mama Sola Provisions owes N3,497,300.",
                               messages=ok, intents=[]) == []


def test_denial_counter_rules() -> None:
    assert D.DenialCounter(in_a_row=3).tripped()
    assert not D.DenialCounter(in_a_row=2).tripped()
    assert D.DenialCounter(window_denials=10, window_size=50).tripped()
    assert not D.DenialCounter(window_denials=9, window_size=50).tripped()
    assert D.DenialCounter(unknown=True).tripped()  # fail closed
    assert D.count_window([True] * 60) == (50, 50)  # capped at the window


def test_env_knobs_move_the_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_DENIALS_IN_A_ROW", "5")
    monkeypatch.setenv("MIMI_DENIALS_MAX_IN_WINDOW", "12")
    assert D.in_a_row_limit() == 5 and D.max_in_window() == 12
    assert not D.DenialCounter(in_a_row=3).tripped()
    assert D.DenialCounter(in_a_row=5).tripped()


def test_non_denial_refusals_never_count() -> None:
    for reason in ("not_allowed", "connector_down", "shop_rate_limit",
                   "followup_dedupe", "followup_cap"):
        assert not D.is_denial_event(
            type("E", (), {"type": "tool_refused", "data": {"reason": reason}})())
    assert D.is_denial_event(
        type("E", (), {"type": "tool_refused",
                        "data": {"reason": "consent", "shop_id": "shop-001"}})())


def test_denials_eval_passes() -> None:
    assert main(["--no-write"]) == 0

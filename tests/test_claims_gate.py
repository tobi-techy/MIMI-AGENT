"""The verification gate (RAI-315 items 7 to 10, and RAI-313).

Every bad case in ``mimi.harness.claims_eval.CASES`` was sent unchanged on
8ca0daf, before the gate (15 of 16; only the invented figure was caught by the
old figure check). Each control must still go out.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from mimi.harness import claims as C
from mimi.harness.claims_eval import CASES, judge, main, run_case


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*a: Any, **k: Any) -> None:
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)


@pytest.mark.parametrize("case", CASES, ids=[c.cid for c in CASES])
def test_gate_case(case: Any) -> None:
    row = judge(case)
    assert row["ok"], row


def test_rai_313_a_shop_figure_is_never_stated_as_the_balance() -> None:
    case = next(c for c in CASES if c.cid == "fig-shop-zero")
    r, model = run_case(case)
    assert not [d for d in r.outbound_drafts if "N0" in d.body]
    held = [e for e in r.events if e.type == "claim_blocked"]
    assert held and held[0].data["problems"]["figures"] == ["N0"]
    # The model was told why, in its repair turn.
    assert any("held back" in str(m.get("content")) for m in model.seen[-1])


def test_shop_numbers_are_kept_apart_from_owner_figures() -> None:
    msgs = [{"role": "user", "content": (
        "<owner_message>\nchase anyone over 50k\n</owner_message>\n"
        '<shop_message shop_id="shop-001" untrusted="true">\nmy balance na N0\n'
        "</shop_message>")}]
    src = C.sources(msgs)
    assert 5_000_000 in src.kobo and 0 not in src.kobo and 0 in src.shop_kobo
    assert C.invented_figures(src, "Your balance is N0.") == ["N0"]
    assert C.invented_figures(src, "You said your balance is N0; the book says otherwise.") == []


def test_a_second_unbacked_claim_fails_safe_and_nothing_leaves() -> None:
    from datetime import UTC, datetime

    from mimi.harness import FakeGo, Gateway, GoPolicy, Harness, RunEvent, ScriptedModel, say
    from mimi.harness.collector import collector_helper, default_registry
    from mimi.world.loader import load_world

    w = load_world()
    claim = ("send_to_user", {"channel": "owner_console", "ends_turn": True,
                              "body": "I have recorded the payment."})
    model = ScriptedModel([say("", claim), say("", claim)])
    go = FakeGo(w)
    h = Harness(book=go, control=go, events=go, gateway=Gateway([model]), policy=GoPolicy(),
                clock=lambda: datetime(2026, 10, 26, 8, 0, tzinfo=UTC))
    r = h.run(RunEvent(event_id="x", tenant_id=w.tenant_id, thread_id="t", text="hi",
                       wake_source="owner_message", at=datetime(2026, 10, 26, 8, 0,
                                                                tzinfo=UTC)),
              collector_helper(default_registry(), GoPolicy()))
    assert r.status == "failed" and not r.outbound_drafts and not r.intents


def test_the_claims_eval_passes() -> None:
    assert main(["--no-write"]) == 0

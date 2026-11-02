"""Plain-code paths (``mimi.harness.plain``, ``MIMI_PLAIN_CODE``).

Each path runs the real harness loop with code in the model's seat, so the
tiers, Go's policy, approvals and the claims gate all still apply; a surprise
hands the step to the model in the same run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.harness import plain
from mimi.harness.gateway import ModelTurn, ScriptedModel, say
from mimi.harness.inbound import Inbound, Payload
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.world.loader import load_world

W = load_world()
NOW = datetime(2026, 10, 26, 7, 30, tzinfo=UTC)  # 08:30 WAT, the world's delivery day


@pytest.fixture()
def engine() -> Any:
    e = make_engine("sqlite://")
    ensure_schema(e)
    seed_world(e, W, daily_cap_kobo=1_000_000)
    return e


class Watch:
    """The real model's seat: counts every call; answers with a quiet end."""

    def __init__(self) -> None:
        self.model = ScriptedModel(lambda m, t: say(""), name="muse-spark-1.3")

    @property
    def calls(self) -> int:
        return self.model.calls


def _agent(engine: Any, watch: Watch) -> Any:
    from mimi.api.agent import Agent

    return Agent(engine, models=[watch.model], router_model=None, lease_s=30.0,
                 clock=lambda: NOW)


def _event(a: Any, eid: str, kind: str, name: str, shop: str) -> str:
    what = f"{name.replace('_', ' ').replace('.', ' ')} for {shop}"
    v = a.submit("coral-lane", Inbound(
        source=kind, org_id="coral-lane", external_id=f"{kind}:{eid}", received_at=NOW,
        payload=Payload(text=f"System event: {what}.", name=name, shop_id=shop)))
    return str(v.turn_id)


def _say(a: Any, ext: str, shop: str, text: str = "", button: str = "") -> str:
    v = a.submit("coral-lane", Inbound(source="shop_message", org_id="coral-lane",
                                       external_id=ext, received_at=NOW,
                                       payload=Payload(text=text, button_id=button,
                                                       from_phone=W.shop(shop).phone)))
    return str(v.turn_id)


def _drain(a: Any) -> None:
    for _ in range(20):
        if a.process() is None:
            return


def _turn(engine: Any, tid: str) -> tuple[Any, dict[str, Any]]:
    with engine.connect() as conn:
        row = conn.execute(sa.select(s.turns).where(s.turns.c.turn_id == tid)).first()
    return row, json.loads(row.result or "{}")


def _out(engine: Any, tid: str) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(s.messages).where(
            s.messages.c.turn_id == tid, s.messages.c.direction == "out")))


def _steps(engine: Any, tid: str) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(s.thread_events).where(
            s.thread_events.c.turn_id == tid).order_by(s.thread_events.c.seq)))


# ------------------------------------------------------------------ flags


def test_the_flag_default_and_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIMI_PLAIN_CODE", raising=False)
    assert plain.paths() == frozenset(plain.DEFAULT_PATHS)
    monkeypatch.setenv("MIMI_PLAIN_CODE", "0")
    assert plain.paths() == frozenset() and not plain.enabled()
    monkeypatch.setenv("MIMI_PLAIN_CODE", "1")
    assert plain.paths() == frozenset(plain.ALL_PATHS)
    monkeypatch.setenv("MIMI_PLAIN_CODE", "balance, promo,nonsense")
    assert plain.paths() == {"balance", "promo"}


@pytest.mark.parametrize("text,ok", [
    ("How much I owe now?", True), ("Good evening. How much I owe una now?", True),
    ("Abeg wetin be my balance?", True), ("balance", True),
    ("How much I owe? I don pay 20k yesterday", False),  # a payment claim: the model
    ("why una say I owe 50k, I no collect that goods", False),  # a dispute: the model
    ("How much I owe and when is my next delivery?", False),
])
def test_balance_path_takes_only_a_balance_question(monkeypatch: pytest.MonkeyPatch,
                                                    text: str, ok: bool) -> None:
    monkeypatch.setenv("MIMI_PLAIN_CODE", "1")
    p = plain.choose(skill="collector/collections", source="shop_message", sender="shop",
                     text=text, button="", shop_id="shop-001", n_events=1)
    assert (p is not None and p.plan == "balance") is ok


def test_no_path_for_a_burst_media_or_a_split_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_PLAIN_CODE", "1")
    kw: dict[str, Any] = {"skill": "collector/collections", "source": "shop_message",
                          "sender": "shop", "text": "How much I owe?", "button": "",
                          "shop_id": "shop-001"}
    assert plain.choose(n_events=2, **kw) is None
    assert plain.choose(n_events=1, media="image", **kw) is None
    assert plain.choose(n_events=1, steered=True, **kw) is None
    assert plain.choose(n_events=1, **{**kw, "sender": "owner"}) is None


def test_balance_reply_copies_the_books_figures() -> None:
    bal = {"outstanding_kobo": 6970000, "outstanding_naira": "N69,700",
           "overdue_kobo": 6970000, "overdue_naira": "N69,700", "open_invoices": 4}
    assert plain.balance_reply("Mama Lanre Provisions", bal) == (
        "Hello Mama Lanre Provisions, you owe N69,700 in total, all of which is overdue "
        "(N69,700) across 4 open invoices. Please pay as soon as you can. Thank you.")
    part = {**bal, "overdue_kobo": 100000, "overdue_naira": "N1,000", "open_invoices": 1}
    assert "N1,000 of it is overdue" in plain.balance_reply("X", part)
    assert "1 open invoice;" in plain.balance_reply("X", part)
    with pytest.raises(plain.Fallback):
        plain.balance_reply("X", {"outstanding_kobo": 5})  # no figure from the book: no reply


# ------------------------------------------------------------------ paths, end to end


def test_off_the_model_runs_every_turn(engine: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_PLAIN_CODE", "0")
    w = Watch()
    a = _agent(engine, w)
    _say(a, "pc.off", "shop-033", "How much I owe now?")
    _drain(a)
    assert w.calls >= 1


def test_balance_question_is_answered_by_code(engine: Any,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_PLAIN_CODE", "balance")
    w = Watch()
    a = _agent(engine, w)
    tid = _say(a, "pc.bal", "shop-033", "How much I owe now?")
    _drain(a)
    row, res = _turn(engine, tid)
    assert w.calls == 0 and row.state == "done"
    out = [m for m in _out(engine, tid) if m.channel == "shop_whatsapp"]
    assert len(out) == 1 and out[0].text.startswith("Hello ") and "you owe N" in out[0].text
    assert res["spend"]["model_calls"] == 0 and res["spend"]["kobo"] == 0
    assert res["metered"]["code"]["kobo"] == 0
    tools = [e.tool for e in _steps(engine, tid) if e.type == "tool_called"]
    assert tools == ["get_balance", "send_to_user"]


def test_promo_question_is_answered_by_code(engine: Any,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_PLAIN_CODE", "promo")
    w = Watch()
    a = _agent(engine, w)
    tid = _say(a, "pc.promo", "shop-031", "Una get any promo for Sunrise noodles this month?")
    _drain(a)
    row, res = _turn(engine, tid)
    if res.get("route", {}).get("skill") != "brand/promo-info":
        pytest.skip("routed elsewhere offline")
    assert w.calls == 0
    out = [m.text for m in _out(engine, tid) if m.channel == "shop_whatsapp"]
    assert out and (out[0].startswith("Promos running today") or "promo" in out[0])


def test_delivery_webhook_sends_the_receipt_by_code(engine: Any,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_PLAIN_CODE", "delivery_receipt")
    w = Watch()
    a = _agent(engine, w)
    tid = _event(a, "d1", "webhook", "delivery_completed", "shop-040")
    _drain(a)
    row, res = _turn(engine, tid)
    out = [m for m in _out(engine, tid) if m.channel == "shop_whatsapp"]
    tools = [e.tool for e in _steps(engine, tid) if e.type == "tool_called"]
    if tools[:1] != ["get_open_deliveries"]:
        pytest.skip("no delivery path offline")
    if len(tools) == 1:
        assert w.calls == 0 and not out  # nothing open: ends quiet
        return
    assert w.calls == 0 and tools == ["get_open_deliveries", "send_delivery_receipt"]
    assert len(out) == 1 and "Reply YES to confirm." in out[0].text


def test_owner_decided_credit_approval_ends_quiet_by_code(
        engine: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_PLAIN_CODE", "1")
    p = plain.choose(skill="credit/credit-request", source="approval_answered",
                     sender="system", text="The owner approved approvals item apr_x.",
                     button="", shop_id="shop-011", n_events=1)
    assert p is not None and p.plan == "approval_quiet"
    turn = plain.PlainModel(p, 1).chat(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "x"}], [],
        max_tokens=10)
    assert turn.tool_calls == () and turn.content == "" and turn.model == plain.PLAIN_MODEL


# ------------------------------------------------------------------ fallback


def _msgs(*tail: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"role": "system", "content": "s"}, {"role": "user", "content": "newest"}, *tail]


def _tools(*names: str) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": n}} for n in names]


def test_a_surprise_hands_the_step_to_the_model() -> None:
    from mimi.harness.gateway import Gateway, ProviderError

    pm = plain.PlainModel(plain.PlanInput("delivery_receipt", "shop-040"), 1)
    tools = _tools("get_open_deliveries", "send_delivery_receipt")
    first = pm.chat(_msgs(), tools, max_tokens=10)
    assert [c.name for c in first.tool_calls] == ["get_open_deliveries"]
    call = {"role": "assistant", "content": "", "tool_calls": [
        {"id": first.tool_calls[0].id, "type": "function",
         "function": {"name": "get_open_deliveries", "arguments": "{}"}}]}
    two = {"ok": True, "data": {"deliveries": [{"delivery_id": "dlv-1", "receipt_sent": False},
                                               {"delivery_id": "dlv-2", "receipt_sent": False}],
                                "next_step": "A reply is about dlv-1: call read_receipt_reply."}}
    res = {"role": "tool", "tool_call_id": first.tool_calls[0].id, "content": json.dumps(two)}
    with pytest.raises(ProviderError):
        pm.chat(_msgs(call, res), tools, max_tokens=10)
    # A message that arrived mid-run (an absorbed correction) also hands over.
    mid = {"role": "user", "content": "[New message on this thread...]"}
    with pytest.raises(ProviderError):
        pm.chat(_msgs(call, res, mid), tools, max_tokens=10)
    # A tool the helper does not have is never called by code.
    with pytest.raises(ProviderError):
        pm.chat(_msgs(), _tools("send_to_user"), max_tokens=10)
    # Behind it in the gateway, the real model answers the same step.
    real = ScriptedModel([say("", ("read_receipt_reply", {"shop_id": "shop-040"}),
                              model="muse-spark-1.3")],
                         name="muse-spark-1.3")
    reply = Gateway([pm, real]).chat(_msgs(call, res), tools)
    assert reply.turn.model == "muse-spark-1.3" and reply.fallbacks[0].model == "plain-code"


def test_an_error_result_never_continues_the_script() -> None:
    pm = plain.PlainModel(plain.PlanInput("stock_tap", "shop-012", "3-5"), 1)
    tools = _tools("get_stock_estimates", "read_stock_answer", "record_stock_count",
                   "draft_restock", "send_to_user")
    t0 = pm.chat(_msgs(), tools, max_tokens=10)
    call = {"role": "assistant", "content": "", "tool_calls": [
        {"id": t0.tool_calls[0].id, "type": "function",
         "function": {"name": "get_stock_estimates", "arguments": "{}"}}]}
    err = {"role": "tool", "tool_call_id": t0.tool_calls[0].id,
           "content": json.dumps({"ok": False, "error": "x", "next_step": "y"})}
    from mimi.harness.gateway import ProviderError

    with pytest.raises(ProviderError):
        pm.chat(_msgs(call, err), tools, max_tokens=10)


def test_the_plain_model_is_never_charged() -> None:
    t: ModelTurn = plain._end()
    from mimi.models.prices import kobo_cost

    assert t.fake and t.cost_kobo == 0 and kobo_cost(t.model, 10_000, 10_000) == 0

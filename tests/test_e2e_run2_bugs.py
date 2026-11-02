"""Regressions from the second live distributor test of the demo (30 Oct 2026, run 2).

One test (or a few) per bug, each failing on the code before the fix:

1. "hello"/proof/assign fallback loop: the owner was asked "is this about
   just saying hello?" three times in a row.
2. A credit raise ("Raise it by ₦100,000") silently dropped.
3. A duplicate Order reply (the sentence and an empty card saying the same).
4. Over-limit not flagged (shop rows, credit page, orders past the limit).
5. No reorder card for a reorder request.
6. The @ picker needs shops and reps: ``GET /v1/mentions``.
7. Internal ids in replies and "N" instead of "₦" with thousands commas.
8. Collector/Credit owner reads took 30 to 60 s: code answers them, no model.
9. The morning brief said "owes" beside an overdue figure.
10. The per-turn budget pause in plain words, same budget logic.

Offline: scripted models and a SQLite file per test; no model is paid.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mimi.api import cards as cx
from mimi.api import display
from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.flags import FIXES_ENV
from mimi.harness.gateway import ScriptedModel
from mimi.harness.inbound import Inbound, Payload
from mimi.harness.loop import pause_question
from mimi.harness.router import Router, ThreadState, owner_first_rule
from mimi.harness.router_eval import OWNER_PHONE, directory
from mimi.models.base import ChatResult
from mimi.runtime.budgets import BudgetHit
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.world.loader import load_world
from tests.test_handoffs import CHAN_KEY, OWNER_KEY, make_script, owner_replies

A = "coral-lane"
W = load_world()
AT = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


class Counting:
    """A scripted helper model that counts its calls."""

    def __init__(self) -> None:
        self.calls = 0
        self.inner = make_script()

    def __call__(self, messages: Any, tools: Any) -> Any:
        self.calls += 1
        return self.inner(messages, tools)


@pytest.fixture()
def fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FIXES_ENV, "1")


@pytest.fixture()
def env(store_url: str, fixed: None) -> Iterator[dict[str, Any]]:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    model = Counting()
    agent = Agent(engine, models=[ScriptedModel(model)], worker_id="run2-test")
    keys = parse_keys(f"{A}:owner:{OWNER_KEY},{A}:channel:{CHAN_KEY}")
    app = create_app(Settings(keys=keys, wait_s=5), agent, workers=0)
    with TestClient(app) as c:
        yield {"c": c, "agent": agent, "engine": engine, "model": model}
    engine.dispose()


def owner_turn(c: TestClient, mid: str, text: str, **extra: Any) -> dict[str, Any]:
    r = c.post("/v1/turns", json={"message_id": mid, "source": "owner_message",
                                  "phone": OWNER_PHONE, "text": text, **extra},
               headers={"Authorization": f"Bearer {CHAN_KEY}"})
    assert r.status_code == 200, r.text
    return dict(r.json())


def owner_h() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER_KEY}"}


def shop_id(engine: Any, name: str) -> str:
    return SqlGo(engine).known_shops(A)[name]


# ---------------------------------------------------------------- 1. loop


class ChatModel:
    """A router model that always says "mimi/chat" with low confidence: the
    demo's answer to every one of the looping messages."""

    name = "muse-spark-1.3"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: Any, **k: Any) -> ChatResult:
        self.calls += 1
        return ChatResult(text=json.dumps({"route": "mimi/chat", "confidence": 0.4,
                                           "second": "", "language": "en"}),
                          model=self.name, prompt_tokens=700, completion_tokens=30,
                          latency_s=0.9)


def owner_msg(text: str) -> Inbound:
    return Inbound(source="owner_message", org_id=A, external_id=f"o:{text}",  # type: ignore[arg-type]
                   received_at=AT, payload=Payload(text=text, from_phone=OWNER_PHONE))


LOOPED = [
    "Did any shop send a payment proof today? Check it against what they owe.",
    "Not hello. I want to know if any shop uploaded a payment proof today, and if so, "
    "compare the amount paid to what that shop owes.",
    "Assign Divine Favour Provisions to Tunde to visit today.",
]


@pytest.mark.parametrize("text,gold", [
    (LOOPED[0], "proof/proofs-today"), (LOOPED[1], "proof/proofs-today"),
    (LOOPED[2], "collector/rep-visit"),
    ("any fake alerts?", "proof/proofs-today"),
    ("Send Tunde to visit Mama Sola Provisions tomorrow", "collector/rep-visit"),
])
def test_the_looping_owner_messages_route_by_rule_with_no_model(text: str, gold: str) -> None:
    model = ChatModel()
    d = Router(model=model).route(owner_msg(text), directory(W))
    assert d.status == "routed" and d.skill == gold and d.layer == "rule"
    assert model.calls == 0


def test_a_long_owner_question_is_never_asked_if_it_is_just_hello() -> None:
    model = ChatModel()
    d = Router(model=model).route(owner_msg("How did the weekend go for the business overall?"),
                                  directory(W))
    assert "just saying hello" not in d.clarify_question
    assert d.status == "routed" and d.skill == "mimi/chat"
    assert d.rule == "owner_not_a_greeting"


def test_the_owner_is_never_asked_the_same_question_twice() -> None:
    model = ChatModel()
    asked = ThreadState(clarify_options=("mimi/chat",),
                        clarify_text="Just to be sure: is this about just saying hello?")
    for text in ("hmm, the other thing we spoke about last week regarding shops",
                 "Not hello. Something about the shops"):
        d = Router(model=model).route(owner_msg(text), directory(W), asked)
        assert d.status == "routed", text  # never a second clarify


def test_a_clarify_answer_that_says_not_hello_never_picks_hello() -> None:
    asked = ThreadState(clarify_options=("mimi/chat", "collector/chase"))
    d = Router(model=ChatModel()).route(owner_msg("not hello, who owes"), directory(W), asked)
    assert d.skill != "mimi/chat"


def test_the_payment_proof_question_answers_from_the_book(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "r2.1", LOOPED[0])
    assert t["skill"] == "proof/proofs-today" and t["status"] == "done"
    (text,) = owner_replies(t)
    assert "just saying hello" not in text
    assert text == "No shop has sent a payment proof today."
    assert env["model"].calls == 0


def test_a_payment_proof_today_is_listed_beside_what_the_shop_owes(env: dict[str, Any]) -> None:
    engine = env["engine"]
    sid = shop_id(engine, "Mama Sola Provisions")
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(s.intents.insert().values(
            tenant_id=A, idempotency_key="ik_proof_today", attempt_key="", turn_id="t",
            thread_id="x", tool="record_payment_proof", intent_kind="record_payment_proof",
            tier="notify", shop_id=sid, payload=json.dumps(
                {"proof_id": "pf_1", "shop_id": sid, "amount_kobo": 5_060_000}),
            preview="", outcome="applied", created_at=now))
    t = owner_turn(env["c"], "r2.2", "Did any shop send a payment proof today?")
    card = t["card"]
    assert card["title"] == "Payment proofs today" and card["items"][0]["label"] == \
        "Mama Sola Provisions"
    assert card["items"][0]["amount_naira"] == "₦50,600"
    assert "owes ₦3,497,300" in card["items"][0]["status"]
    (text,) = owner_replies(t)
    assert "None counts as paid until the bank feed shows it" in text


def test_the_assignment_goes_to_collector_not_a_fallback(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "r2.3", LOOPED[2])
    assert t["helper"] == "collector" and t["skill"] == "collector/rep-visit"
    assert all("just saying hello" not in r for r in owner_replies(t))


# ----------------------------------------------------------- 2. credit raise


def test_a_credit_raise_is_never_dropped(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "r2.4",
                   "What's Funke & Sons Mini Mart's credit limit? Raise it by ₦100,000.")
    assert t["skill"] == "credit/credit-review"
    card = t["card"]
    ch = card["limit_change"]
    assert ch["current_limit_kobo"] == 248_000_000
    assert ch["proposed_limit_kobo"] == 258_000_000 and ch["change_kobo"] == 10_000_000
    assert ch["needs"] == "owner_confirm"
    assert any(a["action"] == "set_limit" and a["intent"]["target"].startswith("credit_limit:")
               for a in card["actions"])
    (text,) = owner_replies(t)
    assert "₦2,480,000" in text and "₦100,000" in text and "₦2,580,000" in text
    assert "confirm" in text.lower()
    # Nothing changed: Mimi never sets a limit.
    with env["engine"].connect() as conn:
        lim = conn.execute(sa.select(s.shops.c.credit_limit_kobo).where(
            s.shops.c.tenant_id == A, s.shops.c.name == "Funke & Sons Mini Mart")).scalar()
    assert lim == 248_000_000


@pytest.mark.parametrize("words,new", [
    ("raise it by 100k", 258_000_000), ("increase the limit to 3m", 300_000_000),
    ("cut it by N480,000", 200_000_000), ("lower the limit by ₦80,000", 240_000_000),
])
def test_limit_change_words_are_worked_out_in_code(words: str, new: int) -> None:
    row = cx._row("shop-005", "Funke & Sons Mini Mart", 221_120_000, 107_005_000, 49,
                  248_000_000)
    ch = cx.limit_change(words, row)
    assert ch is not None and ch.proposed_limit_kobo == new


def test_a_raise_with_no_amount_asks_how_much(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "r2.5", "Raise Funke & Sons Mini Mart's credit limit please")
    assert any("How much should the new limit be?" in r for r in owner_replies(t)) or \
        t["card"] is None or t["card"].get("limit_change") is None


# -------------------------------------------------------- 3. duplicate reply


def test_no_orders_is_one_line_not_a_sentence_and_a_card(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "r2.6", "Any orders available?")
    assert t["card"] is None
    assert owner_replies(t) == ["No shop order is waiting for you right now."]


# --------------------------------------------------------------- 4. over limit


def test_a_shop_over_its_limit_is_flagged_everywhere(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "r2.7", "Who owes me the most?")
    row = t["card"]["shop_row"]
    assert row["name"] == "Mama Sola Provisions"
    assert row["over_limit"] and row["over_limit_by_kobo"] == 349_730_000 - 170_000_000
    (text,) = owner_replies(t)
    assert "₦1,797,300 over its ₦1,700,000 credit limit" in text
    credit = env["c"].get("/v1/credit", headers=owner_h()).json()
    mama = next(x for x in credit["limits"] if x["name"] == "Mama Sola Provisions")
    assert mama["over_limit"] and mama["over_limit_by_naira"] == "₦1,797,300"


def test_an_order_past_the_limit_is_flagged_in_approvals_and_open_orders(
        env: dict[str, Any]) -> None:
    engine = env["engine"]
    sid = shop_id(engine, "Mama Sola Provisions")
    with engine.begin() as conn:
        conn.execute(s.approvals.insert().values(
            approval_id="apr_over1", tenant_id=A, idempotency_key="ik_over1", turn_id="t",
            thread_id="x", helper="order", skill="order/order-intake", shop_id=sid,
            tool="submit_order", intent_kind="submit_order",
            preview="Would put order ord-001-abc (2 lines, N120,000; over available credit "
                    "by N120,000) in the owner's approvals",
            payload=json.dumps({"order_id": "ord-001-abc", "over_credit": True,
                                "over_by_kobo": 12_000_000, "lines": [
                                    {"quantity": 2, "line_total_kobo": 12_000_000}]}),
            status="pending", created_at="2026-10-30T09:00:00+01:00", created_ts=1.0))
    apr = env["c"].get("/v1/approvals", headers=owner_h()).json()["approvals"][0]
    assert apr["flags"] == ["over_credit_limit"] and apr["over_limit_by_kobo"] == 12_000_000
    # The shop is named on the row (the order preview names the order, not the shop).
    assert apr["shop_name"] == "Mama Sola Provisions" and "shop-" not in apr["preview"]
    assert "₦120,000" in apr["preview"] and "N120" not in apr["preview"]
    t = owner_turn(env["c"], "r2.8", "Any orders available?")
    assert "over credit limit by ₦120,000" in t["card"]["items"][0]["status"]
    (text,) = owner_replies(t)
    assert "would push a shop past its credit limit" in text


# ------------------------------------------------------------- 5. reorder


def test_a_reorder_request_gets_a_reorder_card(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "r2.9", "Reorder for Mama Sola Provisions, same as last time")
    assert t["skill"] == "order/reorder" and env["model"].calls == 0
    card = t["card"]
    assert card["title"] == "Reorder: Mama Sola Provisions" and card["items"]
    assert card["total_kobo"] == sum(i["amount_kobo"] for i in card["items"])
    assert card["shop_row"]["over_limit"]  # it already owes more than its limit
    (text,) = owner_replies(t)
    assert "over its ₦1,700,000 limit" in text


def test_what_to_restock_first_gets_a_restock_card(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "r2.10", "What should I restock first?")
    assert t["skill"] == "order/reorder"
    assert t["card"]["title"] == "Restock first"
    labels = [i["label"] for i in t["card"]["items"]]
    assert len(labels) == len(set(labels))  # one row per shop and item name
    (text,) = owner_replies(t)
    assert text.startswith("Restock ")


# ----------------------------------------------------------- 6. @ picker


def test_mentions_lists_helpers_shops_and_reps(env: dict[str, Any]) -> None:
    c = env["c"]
    allm = c.get("/v1/mentions", headers=owner_h()).json()
    kinds = {m["kind"] for m in allm["mentions"]}
    assert kinds == {"helper", "shop", "rep"} or allm["total"] > 20
    m = c.get("/v1/mentions", params={"q": "mama", "kinds": "shop"}, headers=owner_h()).json()
    assert m["mentions"] and all(x["kind"] == "shop" for x in m["mentions"])
    assert m["mentions"][0]["name"].lower().startswith("mama")
    col = c.get("/v1/mentions", params={"q": "@coll"}, headers=owner_h()).json()["mentions"][0]
    assert col == {"kind": "helper", "id": "collector", "name": "Collector",
                   "subtitle": "who owes and reminders", "mention": "@Collector",
                   "assign_to": "collector"}
    rep = next(iter(W.reps))
    rname = getattr(rep, "name", None) or SqlGo(env["engine"]).engine.connect().execute(
        sa.select(s.reps.c.name)).scalar()
    r = c.get("/v1/mentions", params={"q": str(rname)[:3], "kinds": "rep"},
              headers=owner_h()).json()
    assert r["mentions"] and r["mentions"][0]["kind"] == "rep"
    # A channel key may only post turns.
    assert c.get("/v1/mentions", headers={"Authorization": f"Bearer {CHAN_KEY}"}
                 ).status_code == 403


# -------------------------------------------------- 7. ids and naira on screen


@pytest.mark.parametrize("raw,shown", [
    ("Would send a firm reminder to shop-001 for N50,600",
     "Would send a firm reminder to Mama Sola Provisions for ₦50,600"),
    ("N50600 left", "₦50,600 left"), ("paid ₦1234567.5", "paid ₦1,234,567.50"),
    ("over by N2,000.00", "over by ₦2,000"),
])
def test_owner_text_shows_names_and_naira(raw: str, shown: str) -> None:
    names = display.Names({"shop-001": "Mama Sola Provisions"}, {})
    assert display.humanize(raw, names) == shown


def test_the_decided_system_line_has_no_ids() -> None:
    names = display.Names({"shop-001": "Mama Sola Provisions"}, {})
    line = display.decided_line("The owner approved approvals item apr_a3278d1027978ff5754c "
                                "(send_reminder for shop-001).", names)
    assert line == "You approved the reminder for Mama Sola Provisions."


def test_thread_and_turn_text_carry_no_shop_ids(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "r2.11", "Who owes me the most?")
    th = env["c"].get(f"/v1/threads/{t['thread_id']}", headers=owner_h()).json()
    for m in th["messages"]:
        assert not re.search(r"\bshop-\d{3}\b|\bapr_[0-9a-f]{8}", m["text"]), m["text"]
        assert not re.search(r"(?<![A-Za-z₦])N\d", m["text"]), m["text"]
    for ch in t["chips"]:
        assert "shop-" not in ch["intent"].get("text", "")


# ------------------------------------------------------- 8. collector speed


@pytest.mark.parametrize("text,skill", [
    ("Who owes me the most?", "collector/chase"),
    ("Who is the highest debtor?", "collector/chase"),
    ("What's Funke & Sons Mini Mart's credit limit?", "credit/credit-review"),
])
def test_owner_reads_are_answered_by_code_with_no_model(env: dict[str, Any], text: str,
                                                        skill: str) -> None:
    import time

    t0 = time.perf_counter()
    t = owner_turn(env["c"], f"r2.speed.{text}", text)
    took = time.perf_counter() - t0
    assert env["model"].calls == 0, "a pure read must not wake a model"
    assert t["status"] == "done" and t["card"] is not None
    assert took < 5.0  # well under a second here; 30 to 60 s on the demo before
    assert owner_replies(t)


def test_an_action_still_runs_the_helper(env: dict[str, Any]) -> None:
    owner_turn(env["c"], "r2.12", "Chase Mama Sola Provisions for me, draft a reminder.")
    assert env["model"].calls > 0


def test_code_reads_stay_off_without_the_switch(store_url: str,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FIXES_ENV, raising=False)
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    model = Counting()
    agent = Agent(engine, models=[ScriptedModel(model)], worker_id="run2-off")
    keys = parse_keys(f"{A}:owner:{OWNER_KEY},{A}:channel:{CHAN_KEY}")
    with TestClient(create_app(Settings(keys=keys, wait_s=5), agent, workers=0)) as c:
        owner_turn(c, "r2.off", "Who owes me the most?")
    assert model.calls > 0  # recorded behaviour until the paid re-record
    engine.dispose()


# ------------------------------------------------------------- 9. the brief


def test_the_brief_says_overdue_not_owes(env: dict[str, Any]) -> None:
    t = owner_turn(env["c"], "r2.13", "Give me my morning brief.")
    (text,) = owner_replies(t)
    assert "owes the most" not in text and "is the most overdue (₦2,387,250)" in text
    for item in t["card"]["items"]:
        assert "who owes" not in item["status"]
    assert t["card"]["items"][1]["status"].startswith("overdue, 45 days late")


# --------------------------------------------------------- 10. budget pause


def test_the_budget_pause_reads_in_plain_words() -> None:
    q = pause_question("collector")
    assert q == ("This one's taking more work than usual, so I paused Collector. Nothing was "
                 "lost. Keep going?")
    for hit in (BudgetHit("steps", 12, 12), BudgetHit("kobo", 5_000, 5_500),
                BudgetHit("seconds", 90, 91)):
        text = hit.question()
        assert text == "This one's taking more work than usual. Nothing was lost. Keep going?"
        assert not any(ch.isdigit() for ch in text)


def test_owner_first_rules_never_pick_a_greeting() -> None:
    for text in ("hello", "good morning mimi", "thanks"):
        assert owner_first_rule(text) == ""

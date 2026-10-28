"""Harness core (RAI-278) and the typed tool registry (RAI-279).

Every test drives the real loop with a scripted model over the synthetic
Coral Lane world and the in-process Go stand-in, so each acceptance criterion
is checked end to end, offline and deterministically.
"""

from __future__ import annotations

import ast
import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from mimi.harness import (
    FakeGo,
    Gateway,
    GoPolicy,
    Harness,
    ProviderError,
    RegistryError,
    RunBudget,
    RunEvent,
    ScriptedModel,
    build_toolset,
    collector_helper,
    default_registry,
    proof_helper,
    say,
)
from mimi.harness.collector import COLLECTOR_TOOLS
from mimi.harness.errors import NEXT
from mimi.harness.gateway import ModelTurn, OpenAIToolModel, kobo_cost
from mimi.harness.loop import invented_figures
from mimi.harness.registry import MAX_TOOL_CHARS, ToolRegistry
from mimi.harness.tracing import build_turn, trace_run
from mimi.observe.turns import RecordingExporter
from mimi.world.loader import load_world

WORLD = load_world()
AT = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
TENANT = WORLD.tenant_id
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*a: Any, **k: Any) -> None:
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)


def rig(turns: Any, *, budget: RunBudget | None = None, go: FakeGo | None = None,
        fallback: Any = None, helper: str = "collector", **go_kw: Any) -> tuple[Harness, Any, FakeGo, ScriptedModel]:
    go = go or FakeGo(WORLD, **go_kw)
    policy = GoPolicy()
    reg = default_registry()
    h_cfg = (collector_helper(reg, policy, budget=budget) if helper == "collector"
             else proof_helper(reg, policy))
    model = turns if isinstance(turns, ScriptedModel) else ScriptedModel(turns)
    models: list[Any] = [model] + ([fallback] if fallback else [])
    h = Harness(book=go, control=go, events=go, gateway=Gateway(models), policy=policy,
                clock=lambda: AT)
    return h, h_cfg, go, model


def ev(text: str = "chase everyone over 50k", *, wake: str = "owner_message",
       sender: str = "owner", shop: str = "", eid: str = "e1") -> RunEvent:
    return RunEvent(event_id=eid, tenant_id=TENANT, thread_id="thread-1",
                    wake_source=wake, text=text, sender=sender,  # type: ignore[arg-type]
                    shop_id=shop, at=AT)


def overdue_invoice(shop_id: str) -> str:
    return next(r.delivery_id for r in WORLD.receivables
                if r.shop_id == shop_id and r.status == "overdue")


def tool_texts(model: ScriptedModel) -> list[str]:
    return [m["content"] for seen in model.seen for m in seen if m.get("role") == "tool"]


# =================================================================== RAI-278


def test_four_tool_collector_task_parks_for_approval_with_the_right_intent() -> None:
    """Scripted model: 4 reads in one turn, then the plan. Parks for approval."""
    h, helper, go, model = rig([
        say("", ("get_invoices", {"min_overdue_naira": 500_000}),
            ("get_balance", {"shop_id": "shop-001"}),
            ("get_payment_history", {"shop_id": "shop-001"}),
            ("get_balance", {"shop_id": "shop-005"})),
        say("", ("propose_reminder_plan", {"items": [{"shop_id": "shop-001"},
                                                     {"shop_id": "shop-005", "tone": "soft"}]})),
    ])
    r = h.run(ev(), helper)
    assert r.status == "parked_for_approval"
    assert [i.intent_kind for i in r.intents] == ["propose_reminder_plan"]
    plan = r.intents[0]
    assert plan.tier == "approve" and plan.idempotency_key.startswith("ik_")
    expected = sum(x.outstanding_kobo for x in WORLD.receivables
                   if x.shop_id in ("shop-001", "shop-005") and x.status == "overdue")
    assert plan.payload["total_kobo"] == expected  # from the book, in code
    assert plan.preview.startswith("2 shops, N") and plan.preview.endswith("Approve and send?")
    assert r.owner_question == plan.preview
    assert sum(1 for e in r.events if e.type == "tool_result") == 4
    assert r.model_calls == 2 and go.book_writes == 0


def _wander(m: Any, t: Any, cost: int | None = None) -> Any:
    """A model that never stops but never repeats itself either: a new shop
    every step, so the run budget (not a loop guard) is what stops it."""
    shops = [s.shop_id for s in WORLD.shops]
    return say("", ("get_balance", {"shop_id": shops[(len(m) // 2) % len(shops)]}),
               cost_kobo=cost)


def test_budget_hit_at_step_13_stops_and_asks_the_owner_without_dropping_work() -> None:
    loop = ScriptedModel(_wander)
    h, helper, go, _ = rig(loop)
    first = h.run(ev("how is shop-001"), helper)
    # The 12 steps ran; the 13th is refused before any model call.
    assert first.status == "stopped_budget" and first.spend.steps == 12
    assert loop.calls == 12
    assert "12 steps" in first.owner_question and "Keep going?" in first.owner_question
    assert any(d.channel == "owner_console" and d.kind == "question"
               for d in first.outbound_drafts)
    hit = [e for e in first.events if e.type == "budget_hit"]
    assert len(hit) == 1 and hit[0].data["dimension"] == "steps"


def test_budget_stop_keeps_proposals_and_a_grant_resumes_the_same_run() -> None:
    from mimi.harness.loop import LoopState

    turns = [say("", ("send_reminder", {"shop_id": "shop-005", "tone": "soft",
                                        "invoice_id": overdue_invoice("shop-005")}))]
    turns += [say("", ("get_balance", {"shop_id": "shop-001"}))] * 3
    turns += [say("", ("send_to_user", {"channel": "owner_console", "body": "Done.",
                                        "ends_turn": True}))]
    h, helper, go, model = rig(turns, budget=RunBudget(max_steps=3))
    state = h.start(ev("chase shop-005"), helper)
    while state.finished is None:
        state = LoopState.model_validate(h.step(state, helper).state)
    parked = state.finished
    assert parked.status == "stopped_budget" and model.calls == 3
    assert [i.intent_kind for i in parked.intents] == ["send_reminder"]  # kept, not dropped
    # The owner says keep going: their grant resumes the same saved run (a
    # bigger helper config alone is not a grant; tests/test_loop_safety.py).
    done = h.resume(state, helper, owner_messages=["yes, keep going"],
                    grant=RunBudget(max_steps=5, max_seconds=0, max_kobo=0))
    assert done.status == "done" and model.calls == 5  # no step was run twice
    assert [i.intent_kind for i in done.intents].count("send_reminder") == 1
    assert done.run_id == parked.run_id


def test_naira_budget_reserves_the_next_call_and_never_overspends() -> None:
    loop = ScriptedModel(lambda m, t: _wander(m, t, 900))
    h, helper, _, _ = rig(loop, budget=RunBudget(max_kobo=5_000))
    r = h.run(ev("x"), helper)
    assert r.status == "stopped_budget" and r.spend.kobo <= 5_000
    assert "N50.00" in r.owner_question  # figures from code
    assert r.spend.kobo == 4_500  # a sixth call could have cost up to N9


def test_time_budget_counts_working_seconds() -> None:
    loop = ScriptedModel(lambda m, t: say("", ("get_balance", {"shop_id": "shop-001"})))
    h, helper, _, _ = rig(loop, budget=RunBudget(max_seconds=3.0))
    ticks = iter(range(100_000))
    h.perf = lambda: next(ticks) * 0.25
    r = h.run(ev("x"), helper)
    assert r.status == "stopped_budget" and "s of work" in r.owner_question


def test_invalid_tool_args_are_repaired_once_then_fail_safe_with_a_logged_reason() -> None:
    h, helper, go, model = rig([
        say("", ("get_balance", {"shop": "shop-001"})),          # wrong field
        say("", ("get_balance", {"shop_id": "shop-001"})),       # repaired
        say("", ("notify_owner", {"message": "Looking at shop-001"})),
        say("", ("get_invoices", {"min_overdue_naira": "lots"})),  # invalid again
    ])
    r = h.run(ev(), helper)
    assert r.status == "failed" and "after one repair" in r.reason
    assert r.intents == () and r.outbound_drafts == ()  # nothing leaves a failed run
    types = [e.type for e in r.events]
    assert types.count("repair_requested") == 1 and "model_output_invalid" in types
    repair = tool_texts(model)[0]
    assert "were invalid" in repair and NEXT in repair and "shop_id" in repair


def test_unparseable_json_and_unknown_tools_count_as_invalid() -> None:
    h, helper, _, _ = rig([say("", ("get_balance", "{not json")),
                           say("", ("transfer_money", {"shop_id": "shop-001"}))])
    r = h.run(ev(), helper)
    assert r.status == "failed"
    assert any(e.type == "tool_refused" and e.tool == "transfer_money" for e in r.events)


def test_provider_5xx_switches_to_the_fallback_model_in_the_same_run() -> None:
    primary = ScriptedModel([ProviderError("muse-spark-1.3", "HTTP 503")] * 3, name="primary")
    backup = ScriptedModel([
        say("", ("get_balance", {"shop_id": "shop-001"}), model="backup"),
        say("", ("send_to_user", {"channel": "owner_console", "ends_turn": True,
                                  "body": "Mama Sola Provisions owes N3,497,300."}),
            model="backup"),
    ], name="backup")
    h, helper, _, _ = rig(primary, fallback=backup)
    r = h.run(ev("what does shop-001 owe?"), helper)
    assert r.status == "done"
    falls = [e for e in r.events if e.type == "model_fallback"]
    assert len(falls) == 2 and "HTTP 503" in falls[0].short
    assert {e.model for e in r.events if e.type == "model_called"} == {"backup"}


def test_every_model_failing_fails_the_run_safely() -> None:
    dead = ScriptedModel([ProviderError("a", "HTTP 500")], name="a")
    h, helper, _, _ = rig(dead)
    r = h.run(ev(), helper)
    assert r.status == "failed" and "every model failed" in r.reason


def test_write_tools_never_touch_the_book_from_inside_the_loop() -> None:
    inv = overdue_invoice("shop-005")
    h, helper, go, _ = rig([
        say("", ("send_reminder", {"shop_id": "shop-005", "invoice_id": inv, "tone": "soft"}),
            ("create_exact_amount_account", {"shop_id": "shop-005", "invoice_id": inv}),
            ("notify_rep", {"shop_id": "shop-005", "message": "Please visit"}),
            ("set_followup", {"shop_id": "shop-005", "in_hours": 48, "reason": "recheck"}),
            ("notify_owner", {"message": "Chasing shop-005"}),
            ("ask_helper", {"helper": "credit", "task": "Look at shop-005's limit"})),
        say("", ("send_to_user", {"channel": "owner_console", "body": "Done",
                                  "ends_turn": True})),
    ])
    r = h.run(ev("chase shop-005"), helper)
    assert len(r.intents) == 7
    assert go.book_writes == 0 and go.submits == 0 and go.applied == []
    # Only the outbox (after the step is saved) hands them to Go.
    for rec in r.intents:
        go.submit_record(rec)
    assert go.book_writes == 7


def test_events_are_ordered_with_run_and_thread_ids_and_step_lines() -> None:
    h, helper, go, _ = rig([
        say("", ("get_balance", {"shop_id": "shop-001"})),
        say("", ("send_to_user", {"channel": "owner_console", "ends_turn": True,
                                  "body": "Mama Sola Provisions owes N3,497,300."})),
    ])
    r = h.run(ev(), helper)
    seqs = [e.seq for e in r.events]
    assert seqs == list(range(len(seqs)))
    assert {e.run_id for e in r.events} == {r.run_id}
    assert {e.thread_id for e in r.events} == {"thread-1"}
    assert [e.event_id for e in go.events] == [e.event_id for e in r.events]
    steps = [e for e in r.events if e.type == "step_finished"]
    assert len(steps) == 2
    for s in steps:
        assert s.tool and s.short and s.duration_ms >= 0 and s.cost_kobo >= 0
    assert steps[0].tool == "get_balance" and "owes" in steps[0].short
    assert r.events[0].data["wake_source"] == "owner_message"
    assert r.events[-1].type == "run_finished"


def test_quiet_wake_up_returns_done_quiet_and_sends_nothing() -> None:
    h, helper, go, _ = rig([say("Payment already logged, nothing new to say.")])
    r = h.run(ev("", wake="routine", sender="system"), helper)
    assert r.status == "done_quiet" and r.outbound_drafts == () and r.intents == ()
    assert "nothing new to say" in r.reason


def test_model_text_outside_send_to_user_never_reaches_a_person() -> None:
    private = "PRIVATE-THOUGHT-7731"
    h, helper, go, _ = rig([
        say(f"{private} let me look", ("get_balance", {"shop_id": "shop-001"})),
        say(f"{private} composing", ("send_to_user", {"channel": "owner_console",
                                                     "ends_turn": True,
                                                     "body": "Mama Sola Provisions owes "
                                                             "N3,497,300."})),
    ])
    r = h.run(ev(), helper)
    for rec in r.intents:
        go.submit_record(rec)
    blob = json.dumps([d.model_dump(mode="json") for d in r.outbound_drafts])
    blob += json.dumps([i.model_dump(mode="json") for i in r.intents])
    blob += json.dumps(go.sent_messages)
    assert private not in blob
    assert go.sent_messages and private not in go.sent_messages[0]["payload"]["body"]


def test_ends_turn_stops_the_loop_with_no_further_model_call() -> None:
    model = ScriptedModel([
        say("", ("get_balance", {"shop_id": "shop-001"}),
            ("send_to_user", {"channel": "owner_console", "body": "Checking now.",
                              "ends_turn": True})),
        say("never called"),
    ])
    h, helper, _, _ = rig(model)
    r = h.run(ev(), helper)
    assert r.status == "done" and model.calls == 1


def test_a_direct_question_answered_in_private_text_gets_one_nudge() -> None:
    model = ScriptedModel([
        say("", ("get_balance", {"shop_id": "shop-001"})),
        say("Mama Sola owes N3,497,300"),  # private text, never delivered
        say("", ("send_to_user", {"channel": "owner_console", "ends_turn": True,
                                  "body": "Mama Sola Provisions owes N3,497,300."})),
    ])
    h, helper, _, _ = rig(model)
    r = h.run(ev("what does shop-001 owe?"), helper)
    assert r.status == "done" and model.calls == 3
    assert any(e.data.get("reason") == "must_speak" for e in r.events)


def test_every_turn_records_its_wake_source() -> None:
    for wake in ("owner_message", "shop_message", "routine", "webhook", "helper_finished",
                 "scheduled_check_in"):
        h, helper, _, _ = rig([say("nothing")])
        r = h.run(ev("", wake=wake, sender="system", eid=wake), helper)
        assert r.wake_source == wake
        assert r.events[0].data["wake_source"] == wake
        turn = build_turn(r)
        assert turn.metadata["wake_source"] == wake and f"wake:{wake}" in turn.tags


def test_kill_switch_per_helper_and_per_distributor_stops_before_any_model_call() -> None:
    for kw in ({"killed_helpers": {(TENANT, "collector")}}, {"killed_tenants": {TENANT}}):
        h, helper, _, model = rig([say("x")], **kw)
        r = h.run(ev(), helper)
        assert r.status == "stopped_killed" and model.calls == 0


def test_invented_figures_in_a_message_are_refused_and_repaired() -> None:
    model = ScriptedModel([
        say("", ("get_balance", {"shop_id": "shop-001"})),
        say("", ("send_to_user", {"channel": "owner_console", "ends_turn": True,
                                  "body": "Mama Sola owes N3,500,000."})),  # rounded: invented
        say("", ("send_to_user", {"channel": "owner_console", "ends_turn": True,
                                  "body": "Mama Sola Provisions owes N3,497,300."})),
    ])
    h, helper, _, _ = rig(model)
    r = h.run(ev("what does shop-001 owe? anything over 50k?"), helper)
    assert r.status == "done"
    assert [d.body for d in r.outbound_drafts] == ["Mama Sola Provisions owes N3,497,300."]
    assert any("N3,500,000" in t for t in tool_texts(model))
    owner = {"role": "user", "content": "<owner_message>\nover 50k\n</owner_message>"}
    assert invented_figures([owner], "over N50,000") == []
    # A number only in shop text is not an owner figure (RAI-313).
    shop = {"role": "user", "content": '<shop_message untrusted="true">\nover 50k\n'
            "</shop_message>"}
    assert invented_figures([shop], "over N50,000") == ["N50,000"]


def test_reads_carry_naira_text_written_by_code() -> None:
    model = ScriptedModel([say("", ("get_balance", {"shop_id": "shop-001"})), say("ok")])
    h, helper, _, _ = rig(model)
    h.run(ev("", wake="routine", sender="system"), helper)
    data = json.loads(tool_texts(model)[0])["data"]
    assert data["outstanding_kobo"] == 349_730_000 and data["outstanding_naira"] == "N3,497,300"


def test_pricing_is_integer_kobo_from_token_usage() -> None:
    assert kobo_cost("muse-spark-1.3", 5_000, 300) == 1204  # 0.007525 USD at N1,600
    assert kobo_cost("fake", 10**6, 10**6) == 0


def test_openai_tool_client_parses_tool_calls_and_maps_5xx_to_provider_error() -> None:
    import httpx

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["tools"][0]["function"]["name"] == "get_balance"
        if body["messages"][-1]["content"] == "fail":
            return httpx.Response(503)
        return httpx.Response(200, json={
            "model": "muse-spark-1.3",
            "choices": [{"finish_reason": "tool_calls", "message": {
                "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "get_balance", "arguments": "{\"shop_id\":\"shop-001\"}"}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    m = OpenAIToolModel(api_key="test", model="muse-spark-1.3",  # pragma: allowlist secret
                        transport=httpx.MockTransport(handler))
    tools = [default_registry().get("get_balance").schema()]
    turn = m.chat([{"role": "user", "content": "hi"}], tools, max_tokens=50)
    assert turn.tool_calls[0].name == "get_balance" and turn.prompt_tokens == 100
    with pytest.raises(ProviderError):
        m.chat([{"role": "user", "content": "fail"}], tools, max_tokens=50)


def test_a_truncated_answer_is_repaired_once() -> None:
    cut = ModelTurn("", (), "scripted", finish_reason="length")
    h, helper, _, _ = rig([cut, say("", ("send_to_user", {
        "channel": "owner_console", "body": "On it.", "ends_turn": True}))])
    r = h.run(ev(), helper)
    assert r.status == "done"


# =================================================================== RAI-279


def test_a_tool_outside_the_allow_list_is_refused_and_logged() -> None:
    h, helper, _, model = rig([say("", ("open_bank_portal", {"task": "look"})),
                               say("", ("get_balance", {"shop_id": "shop-001"})), say("ok")])
    r = h.run(ev(), helper)  # open_bank_portal is Proof's, not Collector's
    refused = [e for e in r.events if e.type == "tool_refused"]
    assert refused and refused[0].data["reason"] == "not_allowed"
    assert "not available to collector" in tool_texts(model)[0]


def test_a_read_without_consent_returns_a_typed_denial_not_data() -> None:
    go = FakeGo(WORLD, withheld={"shop-001": frozenset({"shop:*"})})
    model = ScriptedModel([say("", ("get_balance", {"shop_id": "shop-001"}),
                               ("get_payment_history", {"shop_id": "shop-001"})), say("ok")])
    h, helper, _, _ = rig(model, go=go)
    h.run(ev("", wake="routine", sender="system"), helper)
    for text in tool_texts(model):
        d = json.loads(text)
        assert d["denied"] is True and d["next_step"] and "owes" not in text
        assert "outstanding" not in text
    assert go.reads == 0  # Go was never asked for the data


def test_a_withheld_scope_hides_the_shop_from_org_wide_lists() -> None:
    go = FakeGo(WORLD, withheld={"shop-001": frozenset({"shop:*"})})
    model = ScriptedModel([say("", ("get_invoices", {"min_overdue_naira": 0, "limit": 50})),
                           say("ok")])
    h, helper, _, _ = rig(model, go=go)
    h.run(ev("", wake="routine", sender="system"), helper)
    text = tool_texts(model)[0]
    assert '"shop-001"' not in text and json.loads(text)["data"]["shops_not_sharing"] == 1


def test_same_send_reminder_twice_produces_one_reminder_go_dedupes_on_the_key() -> None:
    inv = overdue_invoice("shop-005")
    args = {"shop_id": "shop-005", "invoice_id": inv, "tone": "soft"}
    go = FakeGo(WORLD)
    recs = []
    for eid in ("job-a", "job-b"):  # two separate jobs propose the same reminder
        h, helper, _, _ = rig([say("", ("send_reminder", args)), say("ok")], go=go)
        recs += list(h.run(ev("chase shop-005", eid=eid), helper).intents)
    assert len(recs) == 2 and recs[0].idempotency_key == recs[1].idempotency_key
    assert recs[0].attempt_key != recs[1].attempt_key
    for rec in recs:
        go.submit_record(rec)
    assert sum(1 for a in go.applied if a["tool"] == "send_reminder") == 1


def test_the_same_intent_twice_in_one_turn_is_proposed_once() -> None:
    inv = overdue_invoice("shop-005")
    a = {"shop_id": "shop-005", "invoice_id": inv, "tone": "soft"}
    h, helper, _, _ = rig([say("", ("send_reminder", a), ("send_reminder", a)), say("ok")])
    r = h.run(ev("chase shop-005"), helper)
    assert [i.intent_kind for i in r.intents] == ["send_reminder"]


def test_every_write_tool_has_a_working_dry_run() -> None:
    reg = default_registry()
    writes = [s for s in reg.specs() if s.kind == "write_intent"]
    assert {s.name for s in writes} >= {"create_exact_amount_account", "send_receipt",
                                        "propose_reminder_plan", "send_reminder",
                                        "set_followup", "ask_helper", "notify_rep",
                                        "notify_owner", "send_to_user", "ask_question",
                                        "update_plan"}
    pay = next(p for p in WORLD.payments if p.payment_id in {c.payment_id
                                                             for c in WORLD.bank_feed})
    inv = overdue_invoice("shop-005")
    sample = {
        "create_exact_amount_account": [("create_exact_amount_account",
                                         {"shop_id": "shop-005", "invoice_id": inv})],
        "send_receipt": [("send_receipt", {"shop_id": pay.shop_id,
                                           "amount_kobo": pay.amount_kobo})],
    }
    h, helper, _, _ = rig([say("", *sample["create_exact_amount_account"],
                               *sample["send_receipt"]), say("ok")])
    r = h.run(ev("x"), helper)
    assert all(i.preview.startswith("Would") for i in r.intents) and len(r.intents) == 2
    for s in writes:
        assert s.dry_run is not None


def test_shadow_mode_records_and_applies_nothing() -> None:
    reg = default_registry()
    pol = GoPolicy()
    helper = collector_helper(reg, pol, shadow=True)
    go = FakeGo(WORLD)
    h = Harness(book=go, control=go, events=go, gateway=Gateway([ScriptedModel([say("", (
        "notify_owner", {"message": "hello"})), say("ok")])]), policy=pol, clock=lambda: AT)
    r = h.run(ev("x"), helper)
    assert r.intents and all(i.shadow for i in r.intents)


def test_never_tier_tools_cannot_be_registered_for_any_helper() -> None:
    reg = default_registry()

    class A(BaseModel):
        model_config = ConfigDict(extra="forbid")

        shop_id: str

    class Out(BaseModel):
        ok: bool

    def change_bank_details(ctx: Any, args: A) -> Out:
        """Change the distributor's bank account."""
        return Out(ok=True)

    change_bank_details.__annotations__ = {"ctx": Any, "args": A, "return": Out}
    reg.tool(kind="write_intent", scope="org:account", tier="auto", idem=("shop_id",),
             dry_run=lambda c, a, o: "x")(change_bank_details)
    with pytest.raises(RegistryError, match="never"):
        build_toolset(reg, "collector", ["get_balance", "change_bank_details"], GoPolicy())
    with pytest.raises(RegistryError, match="unknown"):
        build_toolset(reg, "collector", ["get_balance", "made_up"], GoPolicy())
    # A tool Go has no policy row for is never (fail closed).
    assert GoPolicy().base_tier("brand_new_tool") == "never"


def test_write_tools_need_a_dry_run_and_idempotency_fields() -> None:
    reg = ToolRegistry("t")

    class A(BaseModel):
        model_config = ConfigDict(extra="forbid")

        shop_id: str

    def w(ctx: Any, args: A) -> A:
        """W."""
        return args

    w.__annotations__ = {"ctx": Any, "args": A, "return": A}
    with pytest.raises(RegistryError, match="dry_run"):
        reg.tool(kind="write_intent", scope="shop:balance", tier="auto",
                 idem=("shop_id",))(w)
    with pytest.raises(RegistryError, match="idempotency"):
        reg.tool(kind="write_intent", scope="shop:balance", tier="auto",
                 dry_run=lambda c, a, o: "x")(w)


def test_tier_comes_from_go_policy_and_a_tier_the_model_names_is_ignored() -> None:
    inv = overdue_invoice("shop-001")
    h, helper, _, _ = rig([
        say("", ("send_reminder", {"shop_id": "shop-001", "invoice_id": inv, "tone": "firm",
                                   "tier": "auto", "approved": True})),
        say("ok"),
    ])
    r = h.run(ev("chase shop-001"), helper)
    assert r.intents[0].tier == "approve" and r.status == "parked_for_approval"
    ignored = [e for e in r.events if e.type == "field_ignored"]
    assert ignored and set(ignored[0].data["fields"]) == {"approved", "tier"}
    # Amount rule: a soft reminder above the owner's N50,000 limit needs approval too.
    assert GoPolicy().tier(TENANT, "send_reminder", {"tone": "soft",
                                                     "amount_kobo": 5_000_001}) == "approve"
    assert GoPolicy().tier(TENANT, "send_reminder", {"tone": "soft",
                                                     "amount_kobo": 4_000_000}) == "notify"


def test_ci_fails_if_any_tool_description_plus_schema_is_over_4000_chars() -> None:
    for spec in default_registry().specs():
        assert spec.size() <= MAX_TOOL_CHARS, spec.name
    reg = ToolRegistry("t")

    class Big(BaseModel):
        model_config = ConfigDict(extra="forbid")

        shop_id: str

    class Out(BaseModel):
        ok: bool

    def big(ctx: Any, args: Big) -> Out:
        return Out(ok=True)

    big.__doc__ = "x" * 4_100
    big.__annotations__ = {"ctx": Any, "args": Big, "return": Out}
    reg.tool(kind="read", scope="shop:balance", tier="auto")(big)
    with pytest.raises(RegistryError, match="chars"):
        build_toolset(reg, "x", ["big"], type("P", (), {"base_tier": lambda s, n: "auto"})())


def test_every_tool_error_names_a_next_step() -> None:
    """Lint over every error string the suite's runs produced."""
    inv = overdue_invoice("shop-001")
    errors: list[str] = []
    cases = [
        [say("", ("get_balance", {"shop": "x"}))],
        [say("", ("nope", {}))],
        [say("", ("send_reminder", {"shop_id": "shop-001", "invoice_id": "dlv-x",
                                    "tone": "soft"}))],
        [say("", ("send_receipt", {"shop_id": "shop-001", "amount_kobo": 1}))],
        [say("", ("propose_reminder_plan", {"items": [{"shop_id": "shop-040"}]}))],
        [say("", ("send_to_user", {"channel": "shop_whatsapp", "body": "hi"}))],
        [say("", ("get_balance", {"shop_id": "shop-999"}))],
        [say("", ("send_reminder", {"shop_id": "shop-001", "invoice_id": inv,
                                    "tone": "soft"}))],
    ]
    for turns in cases:
        model = ScriptedModel(turns + [say("ok")])
        h, helper, _, _ = rig(model)
        h.run(ev("x"), helper)
        for t in tool_texts(model):
            d: Any
            try:
                d = json.loads(t)
            except ValueError:
                errors.append(t)
                continue
            if d.get("denied"):
                assert d["next_step"]
    go = FakeGo(WORLD, down={(TENANT, "bank_feed")})
    model = ScriptedModel([say("", ("check_payment", {"shop_id": "shop-001",
                                                      "amount_kobo": 100})), say("ok")])
    h, helper, _, _ = rig(model, go=go)
    h.run(ev("x"), helper)
    errors += [t for t in tool_texts(model) if not t.startswith("{")]
    assert len(errors) >= 6
    for e in errors:
        assert NEXT in e and e.split(NEXT, 1)[1].strip(), e


def test_the_tool_list_hash_is_identical_across_turns_until_a_deploy() -> None:
    hashes = set()
    for i in range(3):
        h, helper, _, _ = rig([say("nothing")])
        r = h.run(ev("", wake="routine", sender="system", eid=f"turn-{i}"), helper)
        hashes.add(r.toolset_hash)
        sys_prompt = h.start(ev(eid=f"p{i}"), helper).messages[0]["content"]
        hashes.add("prompt:" + str(hash(sys_prompt)))
    assert len(hashes) == 2  # one tool hash, one prompt prefix
    other = ToolRegistry("next-deploy")
    from mimi.harness.builtins import register_builtins
    from mimi.harness.collector import register_collector
    register_builtins(other)
    register_collector(other)
    later = build_toolset(other, "collector", COLLECTOR_TOOLS + ("send_to_user",
                                                                  "ask_question",
                                                                  "update_plan"), GoPolicy())
    assert later.hash not in hashes


def test_with_the_bank_connector_down_proof_asks_the_owner_and_never_uses_a_browser() -> None:
    go = FakeGo(WORLD, down={(TENANT, "bank_feed")})
    model = ScriptedModel([
        say("", ("check_payment", {"shop_id": "shop-013", "amount_kobo": 14_620_000})),
        say("", ("open_bank_portal", {"task": "look for the credit"})),  # tempted
        say("", ("ask_question", {"to": "owner",
                                  "question": "The bank feed is down. Can you reconnect it?"})),
    ])
    h, helper, _, _ = rig(model, go=go, helper="proof")
    r = h.run(ev("shop-013 says it paid", wake="helper_finished"), helper)
    assert r.status == "asked_question"
    assert not any(e.type == "tool_called" and e.tool == "open_bank_portal" for e in r.events)
    assert any(e.type == "tool_refused" and e.tool == "open_bank_portal" for e in r.events)
    assert "connector is down" in tool_texts(model)[0]


def test_collector_has_its_13_tools_and_the_builtins() -> None:
    helper = collector_helper(default_registry(), GoPolicy())
    assert len(COLLECTOR_TOOLS) == 13
    assert helper.toolset.names == COLLECTOR_TOOLS + ("send_to_user", "ask_question",
                                                       "update_plan")
    for s in helper.toolset.schemas():
        params = s["function"]["parameters"]
        assert params["type"] == "object" and params.get("additionalProperties") is False


def test_summary_schema_mode_loads_the_full_schema_on_first_use() -> None:
    reg = default_registry()
    pol = GoPolicy()
    ts = build_toolset(reg, "collector", COLLECTOR_TOOLS, pol, full_schemas=False)
    assert all(s["function"]["parameters"] == {"type": "object", "additionalProperties": True}
               for s in ts.schemas())
    from mimi.harness.loop import Helper
    helper = Helper("collector", "skill", ts)
    go = FakeGo(WORLD)
    model = ScriptedModel([say("", ("get_balance", {})), say("", ("get_balance", {
        "shop_id": "shop-001"})), say("ok")])
    h = Harness(book=go, control=go, events=go, gateway=Gateway([model]), policy=pol,
                clock=lambda: AT)
    r = h.run(ev("", wake="routine", sender="system"), helper)
    assert "schema_loaded" in tool_texts(model)[0] and r.status == "done_quiet"
    assert not any(e.type == "repair_requested" for e in r.events)


# =================================================================== tracing


def test_one_langfuse_trace_per_run_with_step_spans() -> None:
    rec = RecordingExporter()
    h, helper, _, _ = rig([
        say("", ("get_invoices", {"min_overdue_naira": 500_000})),
        say("", ("propose_reminder_plan", {"items": [{"shop_id": "shop-001"}]})),
    ])
    r = h.run(ev(), helper)
    turn = trace_run(r, rec, goal="chase everyone over 500k")
    assert turn is not None and rec.turns == [turn]
    assert turn.name == "run-collector" and turn.session_id == "thread-1"
    steps = [o for o in turn.observations if o.kind == "chain"]
    assert [o.name for o in steps] == ["step-1", "step-2"]
    kids = steps[1].children
    assert {o.kind for o in kids} == {"generation", "tool"}
    tool = next(o for o in kids if o.kind == "tool" and o.name == "propose_reminder_plan")
    assert tool.metadata["tier"] == "approve" and tool.metadata["idempotency_key"]
    names = {s.name for s in turn.scores}
    assert {"run_status", "stopped_budget", "side_effect_in_loop", "model_calls"} <= names
    assert turn.metadata["toolset_hash"] == r.toolset_hash


def test_tracing_through_the_langfuse_exporter_with_a_mock_client() -> None:
    pytest.importorskip("langfuse")
    from contextlib import contextmanager

    from mimi.observe import langfuse_export as lx

    class Obs:
        def __init__(self, log: list[dict[str, Any]], kw: dict[str, Any]) -> None:
            self.log, self.kw = log, kw
            log.append(kw)

        def start_observation(self, **kw: Any) -> Obs:
            return Obs(self.log, kw)

        def end(self, **kw: Any) -> None:
            pass

        def set_trace_as_public(self) -> None:
            pass

    class Client:
        def __init__(self) -> None:
            self.obs: list[dict[str, Any]] = []
            self.scores: list[dict[str, Any]] = []

        @contextmanager
        def start_as_current_observation(self, **kw: Any) -> Any:
            yield Obs(self.obs, kw)

        def create_score(self, **kw: Any) -> None:
            self.scores.append(kw)

        def flush(self) -> None:
            pass

    client = Client()
    s = lx.LangfuseSettings(public_key="pk-lf-test", secret_key="sk-lf-test")  # pragma: allowlist secret
    ex = lx.LangfuseExporter(s, client_factory=lambda _: client)
    h, helper, _, _ = rig([say("", ("get_balance", {"shop_id": "shop-001"})), say("ok")])
    trace_run(h.run(ev("", wake="routine", sender="system"), helper), ex)
    ex.close(5)
    assert ex.errors == 0 and len(ex.sent) == 1 and ex.sent[0].name == "run-collector"
    assert any(o.get("as_type") == "chain" for o in client.obs)
    assert {sc["name"] for sc in client.scores} >= {"run_status", "side_effect_in_loop"}


# =================================================================== rules


def test_harness_sits_above_the_runtime_and_tools_get_no_store() -> None:
    root = ROOT / "mimi"
    for path in (root / "harness").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("mimi.receipts", "mimi.proof", "mimi.sim",
                                                   "mimi.voice", "mimi.stock")), (path,
                                                                                 node.module)
                if path.name in ("collector.py", "builtins.py"):
                    assert not node.module.startswith(("mimi.runtime", "sqlalchemy")), path
    for path in (root / "runtime").rglob("*.py"):
        assert "mimi.harness" not in path.read_text(encoding="utf-8")


def test_no_tool_name_or_intent_kind_moves_money() -> None:
    from mimi.harness.registry import has_money_verb
    for spec in default_registry().specs():
        assert not has_money_verb(spec.name) and not has_money_verb(spec.intent_kind)


def test_timestamps_and_days_follow_africa_lagos() -> None:
    late = datetime(2026, 10, 26, 23, 30, tzinfo=UTC)  # 00:30 on the 27th in Lagos
    go = FakeGo(WORLD)
    pol = GoPolicy()
    helper = collector_helper(default_registry(), pol)
    h = Harness(book=go, control=go, events=go, gateway=Gateway([ScriptedModel([say("", (
        "notify_owner", {"message": "late note"})), say("ok")])]), policy=pol,
        clock=lambda: late)
    r = h.run(RunEvent(event_id="late", tenant_id=TENANT, thread_id="t", wake_source="routine",
                       at=late), helper)
    assert r.intents[0].payload["date"] == "2026-10-27"
    assert (late + timedelta(hours=1)).date().isoformat() == "2026-10-27"

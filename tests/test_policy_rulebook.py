"""The per-distributor rulebook (RAI-323): ``policy.decide`` and its API.

* the defaults reproduce the tiers from before the rulebook, tool by tool;
* built-in stops refuse whatever a rule row, the API or a message says;
* an unknown action type asks, never acts; an unreadable store fails closed;
* every consequential tool has an action type and every write intent passes
  through ``decide`` (a new tool that skips it fails here);
* ``GET/PUT /v1/policy``: owner key only, demo 403, built-ins locked, the
  change logged with who made it and in force on the next turn.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.api.helpers import HelperDeps, build_helpers
from mimi.harness import policy as P
from mimi.harness.gateway import Gateway, ScriptedModel, say
from mimi.harness.go import FakeGo
from mimi.harness.loop import Harness
from mimi.harness.types import RunEvent, Tier
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.store.policy_rules import SqlPolicyRules
from mimi.world.loader import load_world
from tests.test_api import script

ROOT = Path(__file__).resolve().parents[1] / "mimi"
W = load_world()
T = W.tenant_id
AT = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)


# --------------------------------------------------------- the old tier code


def legacy_tier(tool: str, payload: Mapping[str, Any], limit: int = 5_000_000) -> Tier:
    """``GoPolicy.tier`` exactly as it was before the rulebook (policy-2026-10-27)."""
    if tool in P.NEVER_TOOLS:
        return "never"
    base = P.BASE_TIERS.get(tool, "never")
    if base == "never":
        return "never"
    if tool in P.ALWAYS_APPROVE:
        return "approve"
    if tool == "send_reminder":
        if payload.get("tone") == "firm" or int(payload.get("amount_kobo") or 0) > limit:
            return "approve"
    if tool == "propose_reminder_plan" and int(payload.get("total_kobo") or 0) <= limit:
        return "notify"
    if tool == "send_to_user" and payload.get("channel") in ("shop_whatsapp", "rep_whatsapp"):
        return "notify"
    return base


PAYLOADS: tuple[dict[str, Any], ...] = (
    {}, {"tone": "firm"}, {"tone": "gentle", "amount_kobo": 10},
    {"amount_kobo": 9_000_000}, {"total_kobo": 10}, {"total_kobo": 9_000_000},
    {"channel": "shop_whatsapp"}, {"channel": "rep_whatsapp"}, {"channel": "owner_console"},
    {"to": "shop"}, {"to": "owner"},
)


def test_the_defaults_are_todays_tiers_for_every_tool_and_payload() -> None:
    p = P.GoPolicy()
    tools = set(P.BASE_TIERS) | set(P.NEVER_TOOLS) | {"no_such_tool", "delete_shop"}
    for tool in sorted(tools):
        for payload in PAYLOADS:
            assert p.tier(T, tool, payload) == legacy_tier(tool, payload), (tool, payload)


def test_the_default_rules_are_the_issue_examples() -> None:
    p = P.GoPolicy()
    want = {"order.confirm_reorder": "act_if_preapproved", "collector.send_reminder": "act",
            "credit.suggest_limit": "ask", "brand.send_email": "ask",
            "payment.move_money": "never"}
    for action, behaviour in want.items():
        assert p.decide(T, action, {}).behaviour == behaviour, action
    assert p.decide(T, "payment.move_money", {}).locked


# ------------------------------------------------------------ built-in stops


@pytest.mark.parametrize("action", sorted(P.BUILTIN_STOPS))
def test_a_built_in_stop_refuses_even_when_a_row_says_act(action: str) -> None:
    rules = P.MemoryRules({T: {action: "act"}})  # a row written around the API
    p = P.GoPolicy(rules=rules)
    d = p.decide(T, action, {})
    assert d.tier == "never" and d.locked and d.outcome == "refuse"
    for tool in P.BUILTIN_STOPS[action].tools:
        assert p.tier(T, tool, {}) == "never"
        assert p.decide(T, "collector.send_reminder", {"tool": tool}).tier == "never"
    with pytest.raises(P.PolicyLocked):
        rules.set(T, action, "act")


def test_every_never_tool_is_a_built_in_stop() -> None:
    for tool in P.NEVER_TOOLS:
        a = P.action_type_for(tool)
        assert a in P.BUILTIN_STOPS, tool


def test_floors_hold_and_internal_steps_are_not_rules() -> None:
    rules = P.MemoryRules()
    for action in ("brand.send_email", "credit.suggest_limit", "plan.raise_allowance",
                   "proof.flag_payment"):
        for loose in ("act", "act_if_preapproved"):
            with pytest.raises(P.PolicyLocked):
                rules.set(T, action, loose)
    with pytest.raises(P.PolicyLocked):
        rules.set(T, "order.confirm_reorder", "act")
    with pytest.raises(P.PolicyLocked):
        rules.set(T, "internal.plan", "ask")
    with pytest.raises(P.PolicyLocked):
        rules.set(T, "made.up", "ask")
    # A row below the floor written around the API is ignored, not obeyed.
    p = P.GoPolicy(rules=P.MemoryRules({T: {"brand.send_email": "act"}}))
    assert p.decide(T, "brand.send_email", {"tool": "draft_brand_email"}).tier == "approve"


# ------------------------------------------------------- behaviours and seams


def test_unknown_action_type_asks_never_acts() -> None:
    p = P.GoPolicy(rules=P.MemoryRules({T: {"made.up": "act"}}))
    d = p.decide(T, "made.up", {"tool": "send_receipt"})
    assert d.behaviour == "ask" and d.tier == "approve" and d.source == "unknown"
    assert P.action_type_for("brand_new_write_tool").startswith("unknown.")


def test_each_behaviour_maps_to_its_tier() -> None:
    rules = P.MemoryRules()
    p = P.GoPolicy(rules=rules)
    ctx = {"tool": "send_receipt", "payload": {}}
    assert p.decide(T, "collector.send_receipt", ctx).tier == "auto"
    rules.set(T, "collector.send_receipt", "ask")
    d = p.decide(T, "collector.send_receipt", ctx)
    assert (d.tier, d.source, d.outcome) == ("approve", "owner", "ask")
    rules.set(T, "collector.send_receipt", "hand_off")
    d = p.decide(T, "collector.send_receipt", ctx)
    assert d.tier == "approve" and d.hand_off and d.outcome == "hand_off"
    rules.set(T, "collector.send_receipt", "act_if_preapproved")
    assert p.decide(T, "collector.send_receipt", ctx).tier == "approve"
    assert p.decide(T, "collector.send_receipt", {**ctx, "preapproved": True}).tier == "auto"
    # Another distributor is untouched.
    assert p.decide("other", "collector.send_receipt", ctx).tier == "auto"


def test_act_keeps_codes_amount_rules() -> None:
    p = P.GoPolicy(rules=P.MemoryRules({T: {"collector.send_reminder": "act"}}))
    d = p.decide(T, "collector.send_reminder",
                 {"tool": "send_reminder", "payload": {"tone": "firm"}})
    assert d.tier == "approve"  # a firm reminder still waits, whatever the rule


def test_a_preapproved_order_goes_on_notify_and_without_one_it_waits() -> None:
    p = P.GoPolicy()
    ctx = {"tool": "submit_order", "payload": {}}
    assert p.decide(T, "order.confirm_reorder", ctx).tier == "approve"
    assert p.decide(T, "order.confirm_reorder", {**ctx, "preapproved": True}).tier == "notify"
    # The model cannot say it: only code's ``preapproved is True`` counts.
    assert p.decide(T, "order.confirm_reorder", {**ctx, "preapproved": "yes"}).tier == "approve"


def test_an_unreadable_store_fails_closed() -> None:
    class Broken:
        def rules(self, tenant_id: str) -> Mapping[str, str]:
            raise RuntimeError("db down")

    p = P.GoPolicy(rules=Broken())
    d = p.decide(T, "collector.send_receipt", {"tool": "send_receipt"})
    assert d.tier == "approve" and d.source == "fail_closed"
    assert p.decide(T, "payment.move_money", {}).tier == "never"


def test_the_flag_off_ignores_owner_rows() -> None:
    rules = P.MemoryRules({T: {"collector.send_receipt": "ask"}})
    off = P.GoPolicy(rules=rules, owner_rules=False)
    assert off.decide(T, "collector.send_receipt", {"tool": "send_receipt"}).tier == "auto"
    assert not P.owner_rules_on({"MIMI_POLICY_RULES": "0"})
    assert P.owner_rules_on({})


def test_the_module_seam_is_the_same_decision() -> None:
    d = P.decide(T, "brand.send_email", {"tool": "draft_brand_email"})
    assert d.behaviour == "ask" and d.tier == "approve"


# ------------------------------------------------ every path goes through it


def _all_helpers(tmp_path: Path) -> dict[str, Any]:
    engine = make_engine(f"sqlite:///{tmp_path / 'h.db'}")
    ensure_schema(engine)
    return build_helpers(P.GoPolicy(), go=SqlGo(engine), deps=HelperDeps())


def test_every_write_tool_has_an_action_type_and_every_read_is_a_read(
        tmp_path: Path) -> None:
    seen: dict[str, str] = {}
    for h in _all_helpers(tmp_path).values():
        for name in h.toolset.names:
            seen[name] = h.toolset.get(name).kind
    assert seen, "no tools"
    for name, kind in seen.items():
        a = P.action_type_for(name)
        if kind == "read":
            assert name in P.READ_TOOLS, f"{name}: add it to READ_TOOLS"
        else:
            assert not a.startswith("unknown.") and a != "internal.read", (
                f"{name} is a write tool with no action type: add it to ACTION_TYPES")
    for name in P.READ_TOOLS & set(seen):
        assert seen[name] == "read", name
    for name in P.BASE_TIERS:
        assert not P.action_type_for(name).startswith("unknown."), name


def test_no_record_outside_policy_names_its_own_tier() -> None:
    """A consequential record built by hand must take its tier from the policy
    (``policy.tier`` is ``decide``), never a literal."""
    bad: list[str] = []
    for path in (ROOT / "api" / "agent.py", ROOT / "store" / "book.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values, strict=True):
                    if (isinstance(k, ast.Constant) and k.value == "tier"
                            and isinstance(v, ast.Constant) and v.value):
                        bad.append(f"{path.name}:{node.lineno}")
    assert not bad, bad


class Spy(P.GoPolicy):
    calls: list[tuple[str, str]]

    def decide(self, tenant_id: str, action_type: str,
               context: Mapping[str, Any]) -> P.Decision:
        self.calls.append((action_type, str(context.get("tool"))))
        return super().decide(tenant_id, action_type, context)


def _harness(policy: P.GoPolicy, *turns: Any) -> tuple[Harness, Any]:
    from mimi.harness.collector import collector_helper, default_registry

    go = FakeGo(W)
    seq = list(turns)

    def play(messages: Any, tools: Any) -> Any:
        return seq.pop(0) if seq else say("done")

    h = Harness(book=go, control=go, events=go, gateway=Gateway([ScriptedModel(play)]),
                policy=policy, clock=lambda: AT)
    return h, collector_helper(default_registry(), policy)


def _event() -> RunEvent:
    return RunEvent(event_id="ev1", tenant_id=W.tenant_id, thread_id="t1",
                    wake_source="owner_message", text="go", sender="owner", at=AT)


def test_every_write_intent_in_the_loop_passes_through_decide() -> None:
    spy = Spy()
    spy.calls = []
    h, helper = _harness(spy, say("", ("update_plan", {"plan": ["remind"]}),
                                  ("send_to_user", {"channel": "owner_console",
                                                    "kind": "update", "body": "On it.",
                                                    "ends_turn": True})))
    r = h.run(_event(), helper)
    assert len(r.intents) == 2
    assert [c[1] for c in spy.calls] == ["update_plan", "send_to_user"]
    decided = [e for e in r.events if e.type == "policy_decided"]
    assert [e.data["action_type"] for e in decided] == ["internal.plan",
                                                        "internal.owner_message"]
    assert all(i.action_type and i.behaviour for i in r.intents)


def test_a_hand_off_rule_parks_the_step_for_the_owner() -> None:
    rules = P.MemoryRules()
    rules.set(W.tenant_id, "shop.send_message", "hand_off")
    shop = W.shops[0].shop_id
    h, helper = _harness(P.GoPolicy(rules=rules), say("", (
        "send_to_user", {"channel": "shop_whatsapp", "shop_id": shop, "kind": "reply",
                         "body": "Thank you, we have it.", "ends_turn": True})))
    r = h.run(_event(), helper)
    [i] = r.intents
    assert i.tier == "approve" and i.behaviour == "hand_off"
    assert i.preview.startswith("Hand-off")
    assert r.status == "parked_for_approval"


# -------------------------------------------------------------------- the API

KEYS = {"owner": "p-owner-key-00000000001", "chan": "p-chan-key-000000000001"}


@pytest.fixture()
def api(store_url: str) -> Iterator[dict[str, Any]]:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    agent = Agent(engine, models=[ScriptedModel(script)], worker_id="test-worker")
    keys = parse_keys(f"{T}:owner:{KEYS['owner']},{T}:channel:{KEYS['chan']}")
    with TestClient(create_app(Settings(keys=keys, wait_s=5), agent, workers=0)) as c:
        yield {"c": c, "engine": engine, "agent": agent}
    engine.dispose()


def hk(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {KEYS[name]}"}


def test_get_policy_lists_rules_with_built_ins_locked(api: dict[str, Any]) -> None:
    r = api["c"].get("/v1/policy", headers=hk("owner"))
    assert r.status_code == 200
    body = r.json()
    rows = {x["action_type"]: x for x in body["rules"]}
    assert rows["payment.move_money"]["locked"] and rows["payment.move_money"]["allowed"] == []
    assert rows["order.confirm_reorder"]["behaviour"] == "act_if_preapproved"
    assert rows["brand.send_email"]["allowed"] == ["ask", "hand_off"]
    assert body["editable"] is True
    assert not any(k.startswith("internal.") for k in rows)
    assert api["c"].get("/v1/policy", headers=hk("chan")).status_code == 403


def test_put_policy_is_logged_and_takes_effect_on_the_next_turn(api: dict[str, Any]) -> None:
    c = api["c"]
    first = c.post("/v1/turns", json={"message_id": "wamid.p1", "text": "hi",
                                      "phone": W.shops[0].phone}, headers=hk("chan")).json()
    assert first["replies"][0]["status"] == "ready"
    r = c.put("/v1/policy", json={"rules": [{"action_type": "shop.send_message",
                                             "behaviour": "ask"}], "by": "Mrs Adeyemi"},
              headers=hk("owner"))
    assert r.status_code == 200, r.text
    rows = {x["action_type"]: x for x in r.json()["rules"]}
    assert rows["shop.send_message"]["behaviour"] == "ask"
    assert rows["shop.send_message"]["changed_by"] == "Mrs Adeyemi"
    [change] = r.json()["changes"]
    assert change == {**change, "action_type": "shop.send_message", "from": "act", "to": "ask",
                      "by": "Mrs Adeyemi"}
    nxt = c.post("/v1/turns", json={"message_id": "wamid.p2", "text": "hello",
                                    "phone": W.shops[1].phone}, headers=hk("chan")).json()
    assert nxt["replies"][0]["status"] == "pending_approval"
    [a] = c.get("/v1/approvals", headers=hk("owner")).json()["approvals"]
    assert a["tool"] == "send_to_user" and a["handling"] == "approve"


def test_a_hand_off_approval_never_sends_mimis_draft(api: dict[str, Any]) -> None:
    c = api["c"]
    assert c.put("/v1/policy", json={"rules": [{"action_type": "shop.send_message",
                                                "behaviour": "hand_off"}]},
                 headers=hk("owner")).status_code == 200
    c.post("/v1/turns", json={"message_id": "wamid.h1", "text": "hi",
                              "phone": W.shops[0].phone}, headers=hk("chan"))
    [a] = c.get("/v1/approvals", headers=hk("owner")).json()["approvals"]
    assert a["handling"] == "hand_off"
    body = c.post(f"/v1/approvals/{a['approval_id']}/approve", json={"note": "done"},
                  headers=hk("owner")).json()
    assert body["approval"]["status"] == "approved"
    assert body["approval"]["draft"]["status"] == "rejected"  # the owner did it by hand


def test_put_policy_refuses_built_ins_floors_and_unknowns(api: dict[str, Any]) -> None:
    c = api["c"]
    for action, behaviour in (("payment.move_money", "act"), ("brand.send_email", "act"),
                              ("made.up", "ask"), ("internal.plan", "ask")):
        r = c.put("/v1/policy", json={"rules": [{"action_type": action,
                                                 "behaviour": behaviour}]},
                  headers=hk("owner"))
        assert r.status_code == 409, (action, r.text)
    assert c.put("/v1/policy", json={"rules": [{"action_type": "payment.move_money",
                                                "behaviour": "never"}]},
                 headers=hk("owner")).status_code == 422
    # One bad item changes nothing.
    r = c.put("/v1/policy", json={"rules": [
        {"action_type": "shop.send_message", "behaviour": "ask"},
        {"action_type": "payment.move_money", "behaviour": "act"}]}, headers=hk("owner"))
    assert r.status_code == 409
    assert SqlPolicyRules(api["engine"]).rules(T) == {}
    assert c.put("/v1/policy", json={"rules": [{"action_type": "shop.send_message",
                                                "behaviour": "ask"}]},
                 headers=hk("chan")).status_code == 403


def test_demo_mode_policy_is_read_only(store_url: str) -> None:
    from mimi.api.demo import DemoConfig

    engine = make_engine(store_url)
    ensure_schema(engine)
    agent = Agent(engine, models=[ScriptedModel(script)], worker_id="test-worker")
    conf = DemoConfig(ip_daily_cap_kobo=10_000, global_daily_cap_kobo=30_000,
                      session_ttl_s=3600.0, sessions_per_ip=5, ip_salt="test-salt")
    with TestClient(create_app(Settings(mode="demo", demo=conf, wait_s=5), agent,
                               workers=0)) as c:
        # X-Forwarded-For is never read since #45; the visitor is the CF header.
        sess = c.post("/v1/demo/sessions", headers={"CF-Connecting-IP": "198.51.100.9"}).json()
        hd = {"Authorization": f"Bearer {sess['token']}", "CF-Connecting-IP": "198.51.100.9"}
        got = c.get("/v1/policy", headers=hd)
        assert got.status_code == 200 and got.json()["editable"] is False
        r = c.put("/v1/policy", json={"rules": [{"action_type": "shop.send_message",
                                                 "behaviour": "act"}]}, headers=hd)
        assert r.status_code == 403
    engine.dispose()


def test_rules_live_in_the_store_per_distributor(api: dict[str, Any]) -> None:
    rules = SqlPolicyRules(api["engine"])
    rules.update(T, [("collector.send_receipt", "ask")], by="owner")
    assert rules.rules(T) == {"collector.send_receipt": "ask"}
    assert rules.rules("someone-else") == {}
    with api["engine"].connect() as conn:
        assert len(conn.execute(s.policy_changes.select()).all()) == 1

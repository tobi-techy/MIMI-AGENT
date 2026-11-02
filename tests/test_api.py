"""The HTTP API (RAI-266): every endpoint, auth, tenant isolation, idempotency.

Runs offline: the harness talks to a scripted model, the router uses its
rules, phrases and fast classifier (no model), and the store is a SQLite
file per test. Two synthetic distributors share shop ids and phone numbers
on purpose, so a leak would show.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mimi.api.agent import Agent
from mimi.api.app import create_app, openapi_json
from mimi.api.config import ConfigError, Settings, key_digest, parse_keys
from mimi.harness.gateway import ModelTurn, ScriptedModel, say
from mimi.harness.router_eval import OWNER_PHONE
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.world.generator import generate_world
from mimi.world.loader import load_world

A, B = "coral-lane", "tenant-b"
W_A = load_world()
W_B = generate_world(seed=99)
KEYS = {
    "a_owner": "a-owner-key-000000000001",
    "a_chan": "a-chan-key-0000000000001",
    "b_owner": "b-owner-key-000000000001",
    "b_chan": "b-chan-key-0000000000001",
}
OPEN = next(r for r in W_A.receivables if r.shop_id == "shop-001" and r.outstanding_kobo > 0)
_SHOP = re.compile(r'<shop_message shop_id="([^"]*)" untrusted="true">\n(.*?)\n</shop_message>',
                   re.S)


def script(messages: Any, tools: Any) -> ModelTurn:
    """Plays Mimi: answers each person, asks approval for a FIRM reminder."""
    last = messages[-1]
    if last["role"] == "tool":
        return say("done")
    newest = next(m["content"] for m in reversed(messages) if m["role"] == "user")
    if "Wake source: approval_answered" in newest:
        return say("", ("send_to_user", {"channel": "owner_console", "kind": "update",
                                          "body": "Approved. The reminder goes out now.",
                                          "ends_turn": True}))
    if "<owner_message>" in newest:
        text = newest.split("<owner_message>\n", 1)[1].split("\n</owner_message>", 1)[0]
        return say("", ("send_to_user", {"channel": "owner_console", "kind": "reply",
                                          "body": f"Noted: {text[:40]}", "ends_turn": True}))
    m = _SHOP.search(newest)
    if m is None:
        return say("Nothing to say.")
    shop, text = m.group(1), m.group(2)
    if "FIRM" in text:
        return say("", ("send_reminder", {"shop_id": shop, "invoice_id": OPEN.delivery_id,
                                          "tone": "firm"}))
    return say("", ("send_to_user", {"channel": "shop_whatsapp", "shop_id": shop,
                                      "kind": "reply", "body": f"Got it ({text[:30]})",
                                      "ends_turn": True}))


@pytest.fixture()
def env(store_url: str) -> Iterator[dict[str, Any]]:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W_A)
    seed_world(engine, W_B, tenant_id=B, name="Harbour Foods (fictional)",
               owner_phones=("+2340000000077",))
    model = ScriptedModel(script)
    agent = Agent(engine, models=[model], worker_id="test-worker")
    keys = parse_keys(f"{A}:owner:{KEYS['a_owner']},{A}:channel:{KEYS['a_chan']},"
                      f"{B}:owner:{KEYS['b_owner']},{B}:channel:sha256:"
                      f"{key_digest(KEYS['b_chan'])}")
    app = create_app(Settings(keys=keys, wait_s=5), agent, workers=0)
    with TestClient(app) as client:
        yield {"c": client, "agent": agent, "engine": engine, "model": model}
    engine.dispose()


def h(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {KEYS[name]}"}


def shop_turn(c: TestClient, mid: str, text: str, *, key: str = "a_chan",
              shop: int = 0, world: Any = W_A, **kw: Any) -> Any:
    body = {"message_id": mid, "phone": world.shops[shop].phone, "text": text, **kw}
    return c.post("/v1/turns", json=body, headers=h(key))


# ------------------------------------------------------------------- turns


def test_a_shop_message_gets_a_reply_through_router_and_harness(env: dict[str, Any]) -> None:
    r = shop_turn(env["c"], "wamid.1", "hi")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "done" and body["status"] == "done"
    assert body["helper"] == "mimi" and body["skill"] == "mimi/chat"
    assert body["route"]["layer"] in ("rule", "lexical", "fast") and not body["route"][
        "model_called"]
    assert body["thread_id"] == f"{A}:shop:shop-001" and body["run_id"].startswith("run_")
    [reply] = body["replies"]
    assert reply["channel"] == "shop_whatsapp" and reply["shop_id"] == "shop-001"
    assert reply["text"] == "Got it (hi)" and reply["status"] == "ready"


def test_money_question_goes_to_collector(env: dict[str, Any]) -> None:
    r = shop_turn(env["c"], "wamid.owe", "abeg wetin I owe now?").json()
    assert r["helper"] == "collector" and r["skill"] == "collector/collections"
    # A balance-only question is answered by the plain-code path (on by
    # default, ``mimi.harness.plain``): the book's figures, no model call.
    assert r["replies"][0]["text"].startswith("Hello ") and "you owe N" in r["replies"][0]["text"]


def test_money_question_goes_to_the_model_with_plain_code_off(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_PLAIN_CODE", "0")
    r = shop_turn(env["c"], "wamid.owe0", "abeg wetin I owe now?").json()
    assert r["helper"] == "collector" and r["skill"] == "collector/collections"
    assert r["replies"][0]["text"].startswith("Got it")


def test_the_same_message_id_is_one_turn_and_one_reply(env: dict[str, Any]) -> None:
    c = env["c"]
    first = shop_turn(c, "wamid.dup", "hi").json()
    calls = env["model"].calls
    for _ in range(3):
        again = shop_turn(c, "wamid.dup", "hi").json()
        assert again["duplicate"] is True and again["turn_id"] == first["turn_id"]
        assert again["replies"] == first["replies"]
    assert env["model"].calls == calls  # no second run
    with env["engine"].connect() as conn:
        n = conn.execute(s.turns.select().where(s.turns.c.tenant_id == A)).all()
        outs = conn.execute(s.messages.select().where(s.messages.c.direction == "out")).all()
    assert len(n) == 1 and len(outs) == 1


def test_message_ids_are_per_distributor(env: dict[str, Any]) -> None:
    a = shop_turn(env["c"], "wamid.same", "hi").json()
    b = shop_turn(env["c"], "wamid.same", "hi", key="b_chan", world=W_B).json()
    assert a["turn_id"] != b["turn_id"] and not b["duplicate"]
    assert b["thread_id"].startswith(f"{B}:")


def test_a_queued_turn_answers_202_and_a_worker_finishes_it(env: dict[str, Any]) -> None:
    c, agent = env["c"], env["agent"]
    r = c.post("/v1/turns?wait=false", json={"message_id": "wamid.q", "text": "hi",
                                              "phone": W_A.shops[1].phone}, headers=h("a_chan"))
    assert r.status_code == 202 and r.json()["state"] == "queued"
    tid = r.json()["turn_id"]
    assert agent.process() == tid
    got = c.get(f"/v1/turns/{tid}", headers=h("a_chan")).json()
    assert got["state"] == "done" and got["replies"]


def test_a_burst_on_one_thread_is_one_turn(env: dict[str, Any]) -> None:
    c, agent = env["c"], env["agent"]
    ids = []
    for i, text in enumerate(["Good morning ma", "abeg", "wetin I owe now?"]):
        r = c.post("/v1/turns?wait=false", json={"message_id": f"wamid.b{i}", "text": text,
                                                  "phone": W_A.shops[2].phone},
                   headers=h("a_chan"))
        ids.append(r.json()["turn_id"])
    assert agent.process() == ids[0] and agent.process() is None
    views = [c.get(f"/v1/turns/{t}", headers=h("a_chan")).json() for t in ids]
    assert [v["merged_into"] for v in views] == ["", ids[0], ids[0]]
    assert all(v["state"] == "done" for v in views)
    assert views[0]["route"]["skill"] == "collector/collections"
    assert len(views[0]["replies"]) == 1 and env["model"].calls == 1


def test_an_unclear_message_gets_one_clarifying_question(env: dict[str, Any]) -> None:
    r = shop_turn(env["c"], "wamid.cl", "FIRM please").json()
    assert r["status"] == "asked_to_clarify" and env["model"].calls == 0
    [q] = r["replies"]
    assert q["kind"] == "question" and q["channel"] == "shop_whatsapp" and q["text"]
    d = env["c"].get(f"/v1/threads/{r['thread_id']}", headers=h("a_owner")).json()
    assert d["state"]["clarify_options"]


def test_a_turn_needs_content_and_a_valid_body(env: dict[str, Any]) -> None:
    c = env["c"]
    assert c.post("/v1/turns", json={"message_id": "x", "text": " "},
                  headers=h("a_chan")).status_code == 422
    assert c.post("/v1/turns", json={"text": "hi"}, headers=h("a_chan")).status_code == 422
    assert c.post("/v1/turns", json={"message_id": "x", "text": "hi", "evil": 1},
                  headers=h("a_chan")).status_code == 422


def test_media_ref_is_kept_and_the_bytes_never_are(env: dict[str, Any]) -> None:
    r = shop_turn(env["c"], "wamid.img", "", media={"kind": "image", "ref": "wa-media-123",
                                                    "mime": "image/png",
                                                    "sha256": "ab" * 32}).json()
    assert r["helper"] == "proof"
    with env["engine"].connect() as conn:
        row = conn.execute(s.inbound_messages.select().where(
            s.inbound_messages.c.external_id == "wamid.img")).first()
    assert row.media_ref == "wa-media-123" and "base64" not in row.envelope


def test_the_phone_is_dropped_from_the_queue_once_the_turn_is_done(
        env: dict[str, Any]) -> None:
    shop_turn(env["c"], "wamid.ph", "hi")
    with env["engine"].connect() as conn:
        row = conn.execute(s.inbound_messages.select().where(
            s.inbound_messages.c.external_id == "wamid.ph")).first()
    assert W_A.shops[0].phone not in row.envelope


def test_simulated_traffic_is_tagged(env: dict[str, Any]) -> None:
    r = shop_turn(env["c"], "sim.1", "hi", simulated=True).json()
    d = env["c"].get(f"/v1/threads/{r['thread_id']}", headers=h("a_owner")).json()
    assert d["messages"][0]["simulated"] is True and d["turns"][0]["simulated"] is True


def test_the_owner_from_the_console_and_a_stop(env: dict[str, Any]) -> None:
    c = env["c"]
    r = c.post("/v1/turns", json={"message_id": "con.1", "source": "owner_message",
                                  "text": "how is today going"}, headers=h("a_owner")).json()
    assert r["thread_id"] == f"{A}:owner" and r["replies"][0]["channel"] == "owner_console"
    q = c.post("/v1/turns?wait=false", json={"message_id": "wamid.s1", "text": "hi",
                                              "phone": W_A.shops[3].phone}, headers=h("a_chan"))
    stop = c.post("/v1/turns", json={"message_id": "con.2", "source": "owner_message",
                                     "phone": OWNER_PHONE, "text": "stop"},
                  headers=h("a_owner")).json()
    assert stop["status"] == "stopped_running_turns"
    env["agent"].process()
    got = c.get(f"/v1/turns/{q.json()['turn_id']}", headers=h("a_owner")).json()
    assert got["status"] == "stopped_by_owner" and got["replies"] == []


# ------------------------------------------------------------- approvals


def firm(env: dict[str, Any]) -> dict[str, Any]:
    r = shop_turn(env["c"], "wamid.firm", "abeg wetin I owe now? FIRM").json()
    assert r["status"] == "parked_for_approval", r
    return r


def test_a_firm_reminder_waits_for_the_owner_then_goes_out_on_approve(
        env: dict[str, Any]) -> None:
    c = env["c"]
    turn = firm(env)
    assert turn["replies"][0]["status"] == "pending_approval"
    [a] = c.get("/v1/approvals", headers=h("a_owner")).json()["approvals"]
    assert a["tool"] == "send_reminder" and a["status"] == "pending"
    assert a["payload"]["amount_kobo"] == OPEN.outstanding_kobo  # code's figure, not the model's
    assert a["draft"]["status"] == "pending_approval"
    r = c.post(f"/v1/approvals/{a['approval_id']}/approve", json={"note": "ok"},
               headers=h("a_owner"))
    assert r.status_code == 200
    body = r.json()
    assert body["approval"]["status"] == "approved" and body["approval"]["draft"]["status"] == \
        "ready"
    follow = body["follow_up"]
    assert follow["skill"] == "collector/chase" or follow["helper"] == "collector"
    assert follow["replies"][0]["text"].startswith("Approved")
    # Approving twice is a conflict, not a second send.
    again = c.post(f"/v1/approvals/{a['approval_id']}/approve", headers=h("a_owner"))
    assert again.status_code == 409
    assert c.get("/v1/approvals", headers=h("a_owner")).json()["total"] == 0
    assert c.get("/v1/approvals?status=approved", headers=h("a_owner")).json()["total"] == 1


def test_reject_sends_nothing(env: dict[str, Any]) -> None:
    c = env["c"]
    firm(env)
    a = c.get("/v1/approvals", headers=h("a_owner")).json()["approvals"][0]
    r = c.post(f"/v1/approvals/{a['approval_id']}/reject", headers=h("a_owner")).json()
    assert r["approval"]["status"] == "rejected" and r["approval"]["draft"]["status"] == \
        "rejected"
    with env["engine"].connect() as conn:
        intent = conn.execute(s.intents.select().where(
            s.intents.c.tool == "send_reminder")).first()
    assert intent.outcome == "rejected"


def test_only_the_owner_key_approves(env: dict[str, Any]) -> None:
    c = env["c"]
    firm(env)
    a = c.get("/v1/approvals", headers=h("a_owner")).json()["approvals"][0]
    assert c.post(f"/v1/approvals/{a['approval_id']}/approve",
                  headers=h("a_chan")).status_code == 403
    assert c.post(f"/v1/approvals/{a['approval_id']}/approve").status_code == 401
    assert c.post(f"/v1/approvals/{a['approval_id']}/approve",
                  headers=h("b_owner")).status_code == 404
    assert c.post("/v1/approvals/apr_nope/approve", headers=h("a_owner")).status_code == 404


def test_a_money_mover_can_never_be_approved(env: dict[str, Any]) -> None:
    import time

    with env["engine"].begin() as conn:
        conn.execute(s.approvals.insert().values(
            approval_id="apr_money", tenant_id=A, idempotency_key="ik_money", turn_id="",
            thread_id="", helper="", skill="", shop_id="shop-001", tool="transfer_money",
            intent_kind="transfer_money", preview="", payload="{}", status="pending",
            decided_by="", note="", created_at="2026-10-28T08:00:00+00:00",
            created_ts=time.time()))
    r = env["c"].post("/v1/approvals/apr_money/approve", headers=h("a_owner"))
    assert r.status_code == 409 and "never moves money" in r.json()["detail"]


# ------------------------------------------------------------ console reads


def test_threads_list_and_detail_with_steps(env: dict[str, Any]) -> None:
    c = env["c"]
    shop_turn(c, "wamid.t1", "hi")
    shop_turn(c, "wamid.t2", "abeg wetin I owe now?", shop=1)
    lst = c.get("/v1/threads?kind=shop", headers=h("a_owner")).json()
    assert lst["total"] == 2 and {t["shop_id"] for t in lst["threads"]} == {"shop-001",
                                                                            "shop-002"}
    tid = f"{A}:shop:shop-002"
    d = c.get(f"/v1/threads/{tid}", headers=h("a_owner")).json()
    assert [m["direction"] for m in d["messages"]] == ["in", "out"]
    types = [x["type"] for x in d["steps"]]
    assert types[0] == "handed_to" and "model_called" in types and "run_finished" in types
    assert "private_text" not in json.dumps(d)
    assert d["turns"][0]["status"] == "done" and d["thread"]["shop_name"] == W_A.shops[1].name
    assert c.get("/v1/threads/nope", headers=h("a_owner")).status_code == 404
    assert c.get("/v1/threads?limit=1", headers=h("a_owner")).json()["threads"].__len__() == 1


def test_shops_list_and_detail(env: dict[str, Any]) -> None:
    c = env["c"]
    lst = c.get("/v1/shops", headers=h("a_owner")).json()
    assert lst["total"] == 40
    s1 = next(x for x in lst["shops"] if x["shop_id"] == "shop-001")
    assert s1["outstanding_kobo"] == W_A.outstanding_kobo("shop-001")
    assert s1["phone_last4"] == "***" + W_A.shops[0].phone[-4:]
    assert W_A.shops[0].phone not in json.dumps(lst)
    d = c.get("/v1/shops/shop-001", headers=h("a_owner")).json()
    assert d["shop"]["name"] == W_A.shops[0].name and d["deliveries"]
    for dl in d["deliveries"]:
        assert dl["total_kobo"] == sum(x["line_total_kobo"] for x in dl["lines"])
    assert c.get("/v1/shops/shop-999", headers=h("a_owner")).status_code == 404


def test_credit_limits_and_requests(env: dict[str, Any]) -> None:
    c = env["c"]
    d = c.get("/v1/credit", headers=h("a_owner")).json()
    assert len(d["limits"]) == 40 and d["requests"] == []
    x = next(r for r in d["limits"] if r["shop_id"] == "shop-001")
    assert x["available_kobo"] == max(0, x["credit_limit_kobo"] - x["outstanding_kobo"])
    # A credit intent from a helper (RAI-271 lands it) shows up as a request.
    # It must be a tool on Go's policy allowlist: an unlisted one is refused.
    from mimi.store.book import SqlGo

    with env["engine"].begin() as conn:
        SqlGo.apply_intent(conn, A, {"idempotency_key": "ik_cr1", "tool": "submit_credit_review",
                                     "intent_kind": "submit_credit_review", "tier": "approve",
                                     "shop_id": "shop-002", "preview": "Raise to N200,000",
                                     "payload": {"shop_id": "shop-002",
                                                 "new_limit_kobo": 20_000_000}})
    d = c.get("/v1/credit", headers=h("a_owner")).json()
    [req] = d["requests"]
    assert req["amount_kobo"] == 20_000_000 and req["status"] == "pending" and req["approval_id"]


def test_health_is_open_and_says_the_model_mode(env: dict[str, Any]) -> None:
    r = env["c"].get("/v1/health").json()
    assert r["status"] == "ok" and r["db"] == "ok" and r["model"] == "live"


# ------------------------------------------------------------------ auth


@pytest.mark.parametrize("path", ["/v1/threads", "/v1/shops", "/v1/approvals", "/v1/credit",
                                  "/v1/shops/shop-001", f"/v1/threads/{A}:owner"])
def test_reads_need_an_owner_key(env: dict[str, Any], path: str) -> None:
    c = env["c"]
    assert c.get(path).status_code == 401
    assert c.get(path, headers={"Authorization": "Bearer wrong-key-000000000000"}
                 ).status_code == 401
    assert c.get(path, headers=h("a_chan")).status_code == 403
    assert c.get(path, headers=h("a_owner")).status_code == 200


def test_turns_need_a_key_and_the_right_tenant(env: dict[str, Any]) -> None:
    c = env["c"]
    body = {"message_id": "m1", "text": "hi", "phone": W_A.shops[0].phone}
    assert c.post("/v1/turns", json=body).status_code == 401
    assert c.post("/v1/turns", json={**body, "tenant_id": B},
                  headers=h("a_chan")).status_code == 403


def test_cors_allows_the_console_and_localhost_only(env: dict[str, Any]) -> None:
    c = env["c"]
    pre = {"Access-Control-Request-Method": "GET",
           "Access-Control-Request-Headers": "authorization"}
    for origin in ("https://console.userail.money", "http://localhost:3000",
                   "http://127.0.0.1:5173"):
        r = c.options("/v1/shops", headers={**pre, "Origin": origin})
        assert r.headers.get("access-control-allow-origin") == origin
    r = c.options("/v1/shops", headers={**pre, "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_zero_cross_tenant_leaks(env: dict[str, Any]) -> None:
    """Tenant A does real work; tenant B's key sees none of it, anywhere."""
    c = env["c"]
    a_turns = [shop_turn(c, f"wamid.leak{i}", f"secret-a-{i}", shop=i).json()
               for i in range(3)]
    a_turns.append(firm(env))
    a_apr = c.get("/v1/approvals", headers=h("a_owner")).json()["approvals"][0]
    secrets = [f"secret-a-{i}" for i in range(3)] + [t["turn_id"] for t in a_turns] + [
        a_apr["approval_id"], f"{A}:", W_A.distributor]
    b_turn = shop_turn(c, "wamid.bb", "hello from b", key="b_chan", world=W_B).json()
    pages = ["/v1/threads", "/v1/shops", "/v1/approvals?status=all", "/v1/credit",
             f"/v1/threads/{b_turn['thread_id']}", "/v1/shops/shop-001",
             f"/v1/turns/{b_turn['turn_id']}"]
    leaks = []
    for p in pages:
        r = c.get(p, headers=h("b_owner"))
        assert r.status_code == 200, p
        leaks += [(p, x) for x in secrets if x in r.text]
    for t in a_turns:
        assert c.get(f"/v1/turns/{t['turn_id']}", headers=h("b_owner")).status_code == 404
        assert c.get(f"/v1/threads/{t['thread_id']}", headers=h("b_owner")).status_code == 404
    assert c.post(f"/v1/approvals/{a_apr['approval_id']}/approve",
                  headers=h("b_owner")).status_code == 404
    shop_b = c.get("/v1/shops/shop-001", headers=h("b_owner")).json()["shop"]
    assert shop_b["name"] == W_B.shops[0].name
    assert shop_b["outstanding_kobo"] == W_B.outstanding_kobo("shop-001")
    assert leaks == []


# ----------------------------------------------------------- start-up rules


def test_production_refuses_to_start_unsafe(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = parse_keys(f"{A}:owner:{KEYS['a_owner']}")
    prod = Settings(env="production", keys=keys, has_model_key=True,
                    database_url="postgresql://x/y")
    monkeypatch.delenv("MIMI_PHONE_PSEUDONYM_KEY", raising=False)
    monkeypatch.setenv("MIMI_ENV", "production")
    with pytest.raises(RuntimeError, match="MIMI_PHONE_PSEUDONYM_KEY"):
        create_app(prod, agent=None, workers=0)
    monkeypatch.setenv("MIMI_PHONE_PSEUDONYM_KEY", "test-only-pseudonym-key")
    with pytest.raises(ConfigError, match="MIMI_API_KEYS"):
        Settings(env="production", has_model_key=True, database_url="postgresql://x").check()
    with pytest.raises(ConfigError, match="META_API_KEY"):
        Settings(env="production", keys=keys, database_url="postgresql://x").check()
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        Settings(env="production", keys=keys, has_model_key=True,
                 database_url="sqlite:///x.db").check()
    prod.check()  # all set: starts


def test_key_config_is_strict() -> None:
    with pytest.raises(ConfigError):
        parse_keys("coral-lane:admin:" + "x" * 20)
    with pytest.raises(ConfigError):
        parse_keys("coral-lane:owner:short")
    with pytest.raises(ConfigError):
        parse_keys("coral-lane:owner")
    k = parse_keys(f"{A}:owner:sha256:{key_digest('z' * 20)}")
    assert Settings(keys=k).lookup("z" * 20) is not None
    assert Settings(keys=k).lookup("y" * 20) is None


def test_the_committed_openapi_document_is_current() -> None:
    path = Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi.json"
    want = json.dumps(openapi_json(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert path.read_text(encoding="utf-8") == want, "run: python -m mimi.api openapi"
    paths = json.loads(want)["paths"]
    for p in ("/v1/turns", "/v1/threads", "/v1/threads/{thread_id}", "/v1/shops",
              "/v1/shops/{shop_id}", "/v1/approvals", "/v1/approvals/{approval_id}/approve",
              "/v1/approvals/{approval_id}/reject", "/v1/credit", "/v1/health"):
        assert p in paths


def test_offline_agent_from_env_replies_without_a_key(store_url: str) -> None:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W_A)
    agent = Agent.from_env(engine, env={"META_API_KEY": ""})
    assert agent.model_mode == "offline"
    app = create_app(Settings(keys=parse_keys(f"{A}:channel:{KEYS['a_chan']}")), agent,
                     workers=1)
    with TestClient(app) as c:
        r = shop_turn(c, "wamid.off", "hi").json()
        assert r["state"] == "done" and "offline" in r["replies"][0]["text"]
        assert c.get("/v1/health").json()["model"] == "offline"

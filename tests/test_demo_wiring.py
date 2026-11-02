"""The demo scenes wired in code (``mimi.api.demo_wiring``, 2 Nov 2026).

Offline: a scripted model that is never reached by these paths, a SQLite file
per test, and the fake Zoho kept in the store. No model call, no money moved.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mimi.api import demo_wiring as dw
from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.clock import wall
from mimi.harness.gateway import ScriptedModel
from mimi.harness.style import shop_text
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.store.util import loads
from mimi.world.loader import load_world
from tests.test_api import script

A = "coral-lane"
OWNER, CHAN = "w-owner-key-000000000001", "w-chan-key-0000000000001"
W = load_world()


@pytest.fixture()
def env(store_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("MIMI_DEMO_WIRING", "1")
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    model = ScriptedModel(script)
    agent = Agent(engine, models=[model], worker_id="test-worker")
    keys = parse_keys(f"{A}:owner:{OWNER},{A}:channel:{CHAN}")
    app = create_app(Settings(keys=keys, wait_s=5), agent, workers=0)
    with TestClient(app) as client:
        yield {"c": client, "engine": engine, "model": model, "agent": agent}
    engine.dispose()


def run(env: dict[str, Any], r: Any) -> dict[str, Any]:
    """Process the queued event turn (no workers in tests) and read it back."""
    assert r.status_code == 202, r.text
    tid = r.json()["turn_id"]
    env["agent"].process(tid)
    return dict(env["c"].get(f"/v1/turns/{tid}", headers=h()).json())


def h(key: str = OWNER) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def limit_of(engine: Any, shop_id: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.select(s.shops.c.credit_limit_kobo).where(
            s.shops.c.tenant_id == A, s.shops.c.shop_id == shop_id)).scalar())


# ------------------------------------------------------------ credit limits


def test_set_and_undo_a_limit(env: dict[str, Any]) -> None:
    c, engine = env["c"], env["engine"]
    before = limit_of(engine, "shop-001")
    r = c.post("/v1/credit/shop-001/limit", json={"amount_ngn": 2_000_000, "reason": "pays"},
               headers=h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["old_limit_ngn"] == before // 100 and body["new_limit_ngn"] == 2_000_000
    assert body["change_id"].startswith("lch_")
    assert limit_of(engine, "shop-001") == 200_000_000
    with engine.connect() as conn:
        ev = conn.execute(sa.select(s.thread_events).where(
            s.thread_events.c.event_id == body["change_id"])).first()
    assert ev is not None and ev.type == "limit_change"
    assert ev.thread_id == f"{A}:shop:shop-001"
    steps = c.get(f"/v1/threads/{A}:shop:shop-001", headers=h()).json()["steps"]
    assert any(st["type"] == "limit_change" for st in steps)
    u = c.post(f"/v1/credit/changes/{body['change_id']}/undo", headers=h())
    assert u.status_code == 200, u.text
    assert u.json() == {"shop_id": "shop-001", "old_limit_ngn": 2_000_000,
                        "new_limit_ngn": before // 100, "change_id": body["change_id"],
                        "undone": True}
    assert limit_of(engine, "shop-001") == before
    assert c.post(f"/v1/credit/changes/{body['change_id']}/undo",
                  headers=h()).status_code == 409


def test_limit_is_owner_only_and_checked(env: dict[str, Any]) -> None:
    c = env["c"]
    assert c.post("/v1/credit/shop-001/limit", json={"amount_ngn": 1},
                  headers=h(CHAN)).status_code == 403
    assert c.post("/v1/credit/shop-999/limit", json={"amount_ngn": 1},
                  headers=h()).status_code == 404
    assert c.post("/v1/credit/shop-001/limit", json={"amount_ngn": -5},
                  headers=h()).status_code == 422
    assert c.post("/v1/credit/changes/lch_nope/undo", headers=h()).status_code == 404


# ------------------------------------------------------------ reads


def test_reads_carry_overdue_and_proof_grade(env: dict[str, Any]) -> None:
    c = env["c"]
    credit = c.get("/v1/credit", headers=h()).json()
    row = next(r for r in credit["limits"] if r["shop_id"] == "shop-001")
    assert row["overdue_ngn"] > 0 and row["overdue_days"] > 0
    assert row["credit_limit_ngn"] * 100 == row["credit_limit_kobo"]
    assert credit["payments"]
    for p in credit["payments"]:
        assert p["proof_level"] in ("verified", "likely", "estimated")
        assert p["proof_grade"] == dw.PROOF_GRADE[p["proof_level"]]
        assert {"overdue_days", "overdue_ngn", "amount_ngn", "shop_name"} <= set(p)
    shop = c.get("/v1/shops/shop-001", headers=h()).json()
    assert shop["shop"]["overdue_days"] == row["overdue_days"]
    assert shop["shop"]["overdue_ngn"] == row["overdue_ngn"]
    for p in shop["payments"]:
        assert p["proof_grade"] in ("verified", "estimated", "unverified")
        assert p["overdue_ngn"] == row["overdue_ngn"]


# ------------------------------------------------------------ morning brief


def test_routine_morning_brief_is_code(env: dict[str, Any]) -> None:
    c, model = env["c"], env["model"]
    calls = model.calls
    r = c.post("/v1/events", json={"event_id": "brief-1", "kind": "routine",
                                   "name": "morning_brief"}, headers=h(CHAN))
    t = run(env, r)
    assert t["state"] == "done", t
    text = next(x["text"] for x in t["replies"] if x["channel"] == "owner_console")
    assert text.startswith("Good morning. Top overdue: Mama Sola Provisions is ")
    assert "₦" in text and "shop-" not in text and "apr_" not in text
    owner = c.get(f"/v1/threads/{A}:owner", headers=h()).json()
    assert any(m["kind"] == "morning_brief" for m in owner["messages"])
    assert model.calls == calls  # no model call


# ------------------------------------------------- screenshot to Zoho


def _likely(engine: Any, shop_id: str, amount: int, delivery_id: str) -> None:
    rec = {"tool": "record_payment_proof", "idempotency_key": "ik-demo-1",
           "shop_id": shop_id,
           "payload": {"proof_id": "prf-demo-1", "shop_id": shop_id, "amount_kobo": amount,
                       "delivery_ids": [delivery_id], "paid_at": "2026-10-26T15:56:00+01:00"}}
    with engine.begin() as conn:
        dw.after_intents(conn, A, [rec], {"ik-demo-1": "applied"}, wall())


def test_screenshot_verified_then_zoho_write_and_undo(env: dict[str, Any]) -> None:
    c, engine = env["c"], env["engine"]
    open_r = next(r for r in W.receivables if r.shop_id == "shop-008" and r.outstanding_kobo > 0)
    amount = min(3_495_000, open_r.outstanding_kobo)
    _likely(engine, "shop-008", amount, open_r.delivery_id)
    pays = c.get("/v1/shops/shop-008", headers=h()).json()["payments"]
    mine = next(p for p in pays if p["payment_id"].startswith("pay-ss-"))
    assert mine["proof_level"] == "likely" and mine["proof_grade"] == "estimated"

    r = c.post("/v1/events", json={"event_id": "bank-1", "kind": "webhook",
                                   "name": "bank_credit", "shop_id": "shop-008",
                                   "amount_ngn": amount // 100}, headers=h(CHAN))
    t = run(env, r)
    assert t["state"] == "done", t
    shop_reply = next(x["text"] for x in t["replies"] if x["channel"] == "shop_whatsapp")
    assert shop_reply.startswith("We have received your ₦")
    pays = c.get("/v1/shops/shop-008", headers=h()).json()["payments"]
    mine = next(p for p in pays if p["payment_id"] == mine["payment_id"])
    assert mine["proof_level"] == "verified" and mine["proof_grade"] == "verified"

    aprs = c.get("/v1/approvals", headers=h()).json()["approvals"]
    zoho = next(a for a in aprs if a["kind"] == "zoho_payment")
    p = zoho["payload"]
    assert p["amount_ngn"] == amount // 100 and p["shop_name"]
    assert p["invoice_number"].startswith("INV-") and p["reference_number"].startswith("mimi-")
    assert zoho["blocked"] is False and zoho["result"] is None

    ok = c.post(f"/v1/approvals/{zoho['approval_id']}/approve", headers=h())
    assert ok.status_code == 200, ok.text
    res = ok.json()["approval"]["result"]
    assert res["undo_available"] is True and res["zoho_payment_id"].startswith("zfake_")
    assert ok.json()["follow_up"] is None
    with engine.connect() as conn:
        stored = conn.execute(sa.select(s.artifacts).where(
            s.artifacts.c.path == f"zoho-fake:{A}:{res['zoho_payment_id']}")).first()
    assert stored is not None and loads(stored.content)["amount"] == amount / 100

    undo = c.post(f"/v1/approvals/{zoho['approval_id']}/undo", headers=h())
    assert undo.status_code == 200, undo.text
    assert undo.json()["approval"]["result"]["undo_available"] is False
    with engine.connect() as conn:
        stored = conn.execute(sa.select(s.artifacts).where(
            s.artifacts.c.path == f"zoho-fake:{A}:{res['zoho_payment_id']}")).first()
    assert loads(stored.content)["deleted"] is True
    assert c.post(f"/v1/approvals/{zoho['approval_id']}/undo", headers=h()).status_code == 409


def test_bank_credit_with_nothing_to_match_changes_nothing(env: dict[str, Any]) -> None:
    c = env["c"]
    r = c.post("/v1/events", json={"event_id": "bank-2", "kind": "webhook",
                                   "name": "bank_credit", "shop_id": "shop-002",
                                   "amount_ngn": 12345}, headers=h(CHAN))
    t = run(env, r)
    assert t["state"] == "done"
    assert not [a for a in c.get("/v1/approvals", headers=h()).json()["approvals"]
                if a["kind"] == "zoho_payment"]


# ------------------------------------------------------------ over the limit


def test_order_over_limit_opens_credit_approval(env: dict[str, Any]) -> None:
    c, engine = env["c"], env["engine"]
    rec = {"tool": "submit_order", "idempotency_key": "ik-ord-1", "shop_id": "shop-012",
           "payload": {"order_id": "ord-012-abc123", "shop_id": "shop-012",
                       "total_kobo": 12_000_000, "over_credit": True,
                       "over_by_kobo": 26_020_000}}
    with engine.begin() as conn:
        opened = dw.after_intents(conn, A, [rec], {"ik-ord-1": "queued_for_approval"}, wall())
    assert len(opened) == 1
    a = next(x for x in c.get("/v1/approvals", headers=h()).json()["approvals"]
             if x["kind"] == "credit_over_limit")
    p = a["payload"]
    assert a["blocked"] is True
    assert p["order_ngn"] == 120_000 and p["shop_name"] == "Yemisi & Sons Mini Mart"
    assert p["suggested_limit_ngn"] >= p["owed_ngn"] + p["order_ngn"]
    assert p["suggested_limit_ngn"] % 50_000 == 0 and len(p["reasons"]) >= 3
    assert all("shop-" not in x for x in p["reasons"])
    before = limit_of(engine, "shop-012")
    ok = c.post(f"/v1/approvals/{a['approval_id']}/approve", headers=h())
    assert ok.status_code == 200, ok.text
    res = ok.json()["approval"]["result"]
    assert res["new_limit_ngn"] == p["suggested_limit_ngn"] and res["undo_available"]
    assert ok.json()["approval"]["blocked"] is False
    assert limit_of(engine, "shop-012") == p["suggested_limit_ngn"] * 100
    assert c.post(f"/v1/credit/changes/{res['change_id']}/undo", headers=h()).status_code == 200
    assert limit_of(engine, "shop-012") == before


def test_order_within_limit_opens_nothing(env: dict[str, Any]) -> None:
    rec = {"tool": "submit_order", "idempotency_key": "ik-ord-2", "shop_id": "shop-012",
           "payload": {"order_id": "ord-012-def456", "total_kobo": 100, "over_credit": False}}
    with env["engine"].begin() as conn:
        assert dw.after_intents(conn, A, [rec], {"ik-ord-2": "queued_for_approval"},
                                wall()) == []


# ------------------------------------------------------------ shop text


@pytest.mark.parametrize(("raw", "want"), [
    ("Thank you. Delivery dlv-56-9039 is confirmed.", "Thank you. Your delivery is confirmed."),
    ("Coral Lane Trading delivery receipt for Kunle Kiosk\nRef dlv-56-9039, 2026-10-26\n"
     "- 2 tub Mamaland Custard 2kg: N14,400\nTotal: N64,250\nReply YES to confirm.",
     "Coral Lane Trading delivery receipt for Kunle Kiosk\nDelivered 26 Oct 2026\n"
     "- 2 tub Mamaland Custard 2kg: ₦14,400\nTotal: ₦64,250\nReply YES to confirm."),
    ("Coral Lane Trading approved your order ord-012-a1b2c3 (N120,000). It comes with the "
     "next van.", "Coral Lane Trading approved your order (₦120,000). It comes with the next "
                  "van."),
    ("Just to check on delivery dlv-56-9039: did every item arrive?",
     "Just to check on this delivery: did every item arrive?"),
    ("I can see N34950 from Chidi Fagbemi.", "I can see ₦34,950 from Chidi Fagbemi."),
    ("No figures here.", "No figures here."),
])
def test_shop_text(raw: str, want: str) -> None:
    assert shop_text(raw) == want


def test_shop_text_reaches_the_turn_and_thread(env: dict[str, Any]) -> None:
    c, engine = env["c"], env["engine"]
    now = wall()
    with engine.begin() as conn:
        dw._ensure_thread(conn, A, f"{A}:shop:shop-040", "shop", "shop-040", now)
        dw._say(conn, A, f"{A}:shop:shop-040", key="t-1", channel="shop_whatsapp",
                shop_id="shop-040", text="Thank you. Delivery dlv-56-9039 is confirmed.",
                kind="reply", now=now)
    msgs = c.get(f"/v1/threads/{A}:shop:shop-040", headers=h()).json()["messages"]
    assert msgs[-1]["text"] == "Thank you. Your delivery is confirmed."
    with engine.connect() as conn:
        raw = conn.execute(sa.select(s.messages.c.text).where(
            s.messages.c.message_id == "out:t-1")).scalar()
    assert raw == "Thank you. Delivery dlv-56-9039 is confirmed."  # the record keeps it

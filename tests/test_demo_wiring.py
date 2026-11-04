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
from mimi.connectors.base import ConnectorError
from mimi.connectors.composio import FakeComposio
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


# ------------------------------------------------------------ live switch and honesty


def test_approve_without_switch_stays_in_mimi_book_only(env: dict[str, Any]) -> None:
    """Switch off (CI default): the approved write lands in the fake books
    and says so in plain words."""
    approval = _approve_zoho(env)
    res = approval["result"]
    assert res["venue"] == "mimi-book-only" and res["demo"] is True
    assert res["zoho_payment_id"].startswith("zfake_")
    owner = env["c"].get(f"/v1/threads/{A}:owner", headers=h()).json()
    done = [m for m in owner["messages"] if m["kind"] == "zoho_payment_recorded"]
    assert done and "Mimi's own book only (Zoho Books is not connected)" in done[-1]["text"]


def test_agreement_without_connection_shows_no_figures(env: dict[str, Any]) -> None:
    """No Zoho connection: nulls, not zeros, plus a plain-words note."""
    r = env["c"].get("/v1/books/agreement", headers=h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["zoho_connected"] is False and body["currency"] == "NGN"
    assert "not connected" in body["note"] and body["total"]["zoho_outstanding_kobo"] is None
    assert body["total"]["difference_kobo"] is None
    assert all(s["zoho_outstanding_kobo"] is None for s in body["shops"])
    assert all(s["difference_kobo"] is None for s in body["shops"])
    assert body["as_of"] and body["total"]["mimi_outstanding_naira"].startswith("₦")


class LiveLikeZoho(FakeComposio):
    """A fake with a live transport's shape: absolute proxy endpoints and
    canned organization/contact rows, so the live path runs with no network."""

    def __init__(self, settings: Any = None, orgs: Any = None, contacts: Any = None,
                 fail_create: bool = False) -> None:
        from mimi.connectors.env import ConnectorSettings

        super().__init__(settings or ConnectorSettings.from_env({"MIMI_ZOHO_REGION": "com"}))
        self.org_rows = orgs if orgs is not None else [
            {"organization_id": "org-live-1", "name": "Coral Organization"}]
        self.contact_rows = contacts if contacts is not None else []
        self.fail_create = fail_create
        self.payments: dict[str, dict[str, Any]] = {}

    def execute(self, action: str, **kw: Any) -> dict[str, Any]:
        self.calls.append({"action": action, **kw})
        if action == "zoho_books_organizations":
            return {"organizations": [dict(r) for r in self.org_rows]}
        if action == "zoho_books_contacts":
            return {"contacts": [dict(r) for r in self.contact_rows]}
        raise ConnectorError(f"No fixture for {action}.")

    def proxy(self, name: str, **kw: Any) -> dict[str, Any]:
        self.calls.append({"action": f"proxy:{name}", **kw})
        endpoint = str(kw.get("endpoint") or "")
        assert endpoint.startswith("https://"), f"relative proxy endpoint: {endpoint}"
        body = dict(kw.get("body") or {})
        if name == "customer_payment_search":
            ref = str(body.get("reference_number", ""))
            return {"code": 0, "customer_payments": [
                p for p in self.payments.values() if p.get("reference_number") == ref]}
        if name == "customer_payment_create":
            if self.fail_create:
                return {"code": 0, "payment": {}}
            pid = f"zp-live-{len(self.payments) + 1:03d}"
            self.payments[pid] = {**body, "payment_id": pid}
            return {"code": 0, "payment": dict(self.payments[pid])}
        if name == "customer_payment_delete":
            pid = str(body.get("payment_id", ""))
            if pid not in self.payments:
                return {"code": 1002, "message": "payment not found"}
            del self.payments[pid]
            return {"code": 0, "message": "The payment has been deleted."}
        return {"code": 9, "message": f"unknown call {name}"}


def _seed_live_connection(engine: Any, tenant: str = A) -> None:
    """One active ZOHO_BOOKS row, as the callback would have saved it."""
    from mimi.store.util import now_iso

    with engine.begin() as conn:
        conn.execute(sa.insert(s.connector_accounts).values(
            tenant_id=tenant, connection_id="conn-test-live", toolkit="ZOHO_BOOKS",
            connected_account_id="ca-test-live-0001", region="com",
            status="active", connected_at=now_iso(wall()), revoked_at=None))


def _live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_CONNECTORS", "composio")
    monkeypatch.setenv("COMPOSIO_API_KEY", "ck_test_live")  # pragma: allowlist secret
    monkeypatch.setenv("MIMI_COMPOSIO_AUTH_ZOHO_BOOKS", "ac_test_books")
    monkeypatch.setenv("MIMI_COMPOSIO_CALLBACK_URL", "https://demo.userail.money/x")
    monkeypatch.setenv("MIMI_ZOHO_LIVE_WRITE", "1")
    monkeypatch.setenv("ZOHO_BOOKS_ORGANIZATION_ID", "org-live-1")


def _live_transport(monkeypatch: pytest.MonkeyPatch, live: LiveLikeZoho) -> None:
    """Patch reads onto the live-shaped fake, and force its proxy calls
    through ``live.proxy`` too: ``record_payment`` reaches ``_live_proxy``
    on the real class, whose ``client.tools`` no test fake provides. The
    hook keeps the absolute-endpoint assertion inside ``live.proxy``."""
    monkeypatch.setattr(dw.ComposioTransport, "execute", live.execute)
    monkeypatch.setattr(
        dw.zp, "_live_proxy",
        lambda transport, name, **kw: live.proxy(name, **kw))


def _approve_zoho(env: dict[str, Any]) -> dict[str, Any]:
    """Run the screenshot-to-approval flow and approve the Zoho payment."""
    c, engine = env["c"], env["engine"]
    open_r = next(r for r in W.receivables if r.shop_id == "shop-008" and r.outstanding_kobo > 0)
    amount = min(3_495_000, open_r.outstanding_kobo)
    _likely(engine, "shop-008", amount, open_r.delivery_id)
    r = c.post("/v1/events", json={"event_id": "bank-9", "kind": "webhook",
                                   "name": "bank_credit", "shop_id": "shop-008",
                                   "amount_ngn": amount // 100}, headers=h(CHAN))
    t = run(env, r)
    assert t["state"] == "done", t
    zoho = next(a for a in c.get("/v1/approvals", headers=h()).json()["approvals"]
                if a["kind"] == "zoho_payment")
    ok = c.post(f"/v1/approvals/{zoho['approval_id']}/approve", headers=h())
    assert ok.status_code == 200, ok.text
    return ok.json()["approval"]



def test_live_write_and_undo_through_live_like_transport(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch on with a live-shaped transport: exactly one create, one
    delete, absolute endpoints, and the owner text names Zoho Books."""
    _live_env(monkeypatch)
    _seed_live_connection(env["engine"])
    live = LiveLikeZoho()
    _live_transport(monkeypatch, live)
    approval = _approve_zoho(env)
    res = approval["result"]
    assert res["venue"] == "zoho" and res["demo"] is False
    assert res["zoho_payment_id"].startswith("zp-live-")
    creates = [c for c in live.calls if c.get("action") == "proxy:customer_payment_create"]
    assert len(creates) == 1  # exactly one call per approval
    owner = env["c"].get(f"/v1/threads/{A}:owner", headers=h()).json()
    done = [m for m in owner["messages"] if m["kind"] == "zoho_payment_recorded"]
    assert done and "in Zoho Books" in done[-1]["text"]
    undo = env["c"].post(f"/v1/approvals/{approval['approval_id']}/undo", headers=h())
    assert undo.status_code == 200, undo.text
    assert undo.json()["approval"]["result"]["undo_available"] is False
    deletes = [c for c in live.calls if c.get("action") == "proxy:customer_payment_delete"]
    assert len(deletes) == 1  # the undo deletes exactly the payment created
    assert deletes[0]["body"]["payment_id"] == res["zoho_payment_id"]


def test_live_write_refused_when_org_unresolvable(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Live switch on but no org named: no guess, no fake fallback, the
    approval records the error and no owner text claims a Zoho write."""
    _live_env(monkeypatch)
    monkeypatch.delenv("ZOHO_BOOKS_ORGANIZATION_ID")
    _seed_live_connection(env["engine"])
    live = LiveLikeZoho(orgs=[])
    _live_transport(monkeypatch, live)
    approval = _approve_zoho(env)
    res = approval["result"]
    assert res["undo_available"] is False and "organization" in res["error"]
    assert "zoho_payment_id" not in res
    assert live.calls == []
    owner = env["c"].get(f"/v1/threads/{A}:owner", headers=h()).json()
    assert not [m for m in owner["messages"]
                if m["kind"] == "zoho_payment_recorded"
                and "in Zoho Books" in m["text"]]


def test_demo_account_connects_a_demo_tenant_with_no_row(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployed demo account makes a demo tenant read connected with no
    connector row; the row _zoho_account_org reports is the setting."""
    _live_env(monkeypatch)
    monkeypatch.setenv("MIMI_DEMO_ZOHO_ACCOUNT", "ca-test-live-0001")
    live = LiveLikeZoho()
    _live_transport(monkeypatch, live)
    with env["engine"].connect() as conn:
        rows = conn.execute(sa.select(s.connector_accounts).where(
            s.connector_accounts.c.tenant_id == A)).all()
    assert rows == []
    r = env["c"].get("/v1/books/agreement", headers=h())
    assert r.status_code == 200, r.text
    assert r.json()["zoho_connected"] is True
    account, org = dw._zoho_account_org(env["engine"], A, transport=live)
    assert account == "ca-test-live-0001" and org == "org-live-1"


def test_demo_account_absent_means_today(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting absent: no row, not connected, the old sentence, no figures."""
    _live_env(monkeypatch)
    monkeypatch.delenv("MIMI_DEMO_ZOHO_ACCOUNT", raising=False)
    live = LiveLikeZoho()
    _live_transport(monkeypatch, live)
    body = env["c"].get("/v1/books/agreement", headers=h()).json()
    assert body["zoho_connected"] is False
    assert "not connected" in body["note"]
    assert all(x["zoho_outstanding_kobo"] is None for x in body["shops"])


def test_demo_account_does_not_touch_a_real_tenant(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """The setting only applies where the demo wiring is on: with the demo
    switch off, the same account id buys nothing."""
    import mimi.flags as flags
    from mimi.api import demo_wiring as wiring

    monkeypatch.setenv("MIMI_DEMO_ZOHO_ACCOUNT", "ca-test-live-0001")
    monkeypatch.setattr(flags, "WIRING_ENV", "MIMI_WIRING_OFF_FOR_THIS_TEST")
    assert wiring._demo_wiring_on() is False
    # Same settings, wiring off: no demo shortcut, straight to the row
    # check, and no row means not connected.
    assert wiring._zoho_connected(env["engine"], A) is False


def test_demo_account_refusal_reads_could_not_read(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Composio refusing the org list: connected false with the
    could-not-read sentence, never connected true on empty figures."""
    _live_env(monkeypatch)
    monkeypatch.setenv("MIMI_DEMO_ZOHO_ACCOUNT", "ca-no-such-account")
    live = LiveLikeZoho()
    _live_transport(monkeypatch, live)

    class _Refusing(LiveLikeZoho):
        def execute(self, action: str, **kw: Any) -> dict[str, Any]:
            raise ConnectorError("gone")

    refusing = _Refusing()
    with pytest.raises(ConnectorError, match="could not list organizations"):
        dw._zoho_account_org(env["engine"], A, transport=refusing)
    body = env["c"].get("/v1/books/agreement", headers=h()).json()
    assert body["zoho_connected"] is True  # the HTTP path uses the working fixture
    assert body["total"]["zoho_outstanding_kobo"] is not None


def test_demo_account_org_out_of_reach_refuses(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """The token's org list lacks the named org: could-not-read, never an
    agreement on nothing."""
    _live_env(monkeypatch)
    monkeypatch.setenv("MIMI_DEMO_ZOHO_ACCOUNT", "ca-test-live-0001")
    monkeypatch.setenv("ZOHO_BOOKS_ORGANIZATION_ID", "org-not-on-this-token")
    live = LiveLikeZoho()
    _live_transport(monkeypatch, live)
    body = env["c"].get("/v1/books/agreement", headers=h()).json()
    assert body["zoho_connected"] is False
    assert "could not be read" in body["note"]


def test_revoked_connection_writes_nothing_live(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection revoked mid-flow: back to the fake books, said plainly."""
    from mimi.store.util import now_iso

    _live_env(monkeypatch)
    _seed_live_connection(env["engine"])
    live = LiveLikeZoho()
    _live_transport(monkeypatch, live)
    with env["engine"].begin() as conn:
        conn.execute(sa.update(s.connector_accounts).where(
            s.connector_accounts.c.tenant_id == A).values(
                status="revoked", revoked_at=now_iso(wall())))
    approval = _approve_zoho(env)
    assert approval["result"]["venue"] == "mimi-book-only"
    assert not [c for c in live.calls if str(c.get("action", "")).startswith("proxy:")]


def test_timeout_then_readback_finds_nothing_is_an_error(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """A create that returns no payment id and no read-back row is an
    error, not a silent miss: the approval says so and nothing is undone."""
    _live_env(monkeypatch)
    _seed_live_connection(env["engine"])
    live = LiveLikeZoho(fail_create=True)
    _live_transport(monkeypatch, live)
    approval = _approve_zoho(env)
    res = approval["result"]
    assert res["undo_available"] is False and "error" in res
    assert "zoho_payment_id" not in res


def _second_approval(env: dict[str, Any]) -> str:
    """Run the flow again for a second pending approval.

    A new screenshot payment (new proof id) plus a new bank event, so the
    second approval carries a distinct payment id and idempotency key."""
    c, engine = env["c"], env["engine"]
    open_r = next(r for r in W.receivables if r.shop_id == "shop-008" and r.outstanding_kobo > 0)
    amount = min(3_495_000, open_r.outstanding_kobo)
    with engine.begin() as conn:
        dw.after_intents(conn, A, [{
            "tool": "record_payment_proof", "idempotency_key": "ik-demo-2",
            "shop_id": "shop-008",
            "payload": {"proof_id": "prf-demo-2", "shop_id": "shop-008",
                        "amount_kobo": amount, "delivery_ids": [open_r.delivery_id],
                        "paid_at": "2026-10-26T15:57:00+01:00"}}],
            {"ik-demo-2": "applied"}, wall())
    r = c.post("/v1/events", json={"event_id": "bank-10", "kind": "webhook",
                                   "name": "bank_credit", "shop_id": "shop-008",
                                   "amount_ngn": amount // 100}, headers=h(CHAN))
    t = run(env, r)
    assert t["state"] == "done", t
    zoho = next(a for a in c.get("/v1/approvals", headers=h()).json()["approvals"]
                if a["kind"] == "zoho_payment" and a["status"] == "pending")
    return str(zoho["approval_id"])


def test_second_live_write_same_day_refused_without_provider_call(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Cap default 1: the second real write refuses before any provider
    call, records the refusal, and stays visible."""
    import re

    _live_env(monkeypatch)
    _seed_live_connection(env["engine"])
    live = LiveLikeZoho()
    _live_transport(monkeypatch, live)
    first = _approve_zoho(env)
    assert first["result"]["zoho_payment_id"].startswith("zp-live-")
    second_id = _second_approval(env)
    before = len(live.calls)
    ok = env["c"].post(f"/v1/approvals/{second_id}/approve", headers=h())
    assert ok.status_code == 200, ok.text
    res = ok.json()["approval"]["result"]
    assert res["undo_available"] is False and "nothing was written" in res["error"]
    assert "zoho_payment_id" not in res
    assert len(live.calls) == before  # the provider client was never called
    text = res["error"]
    assert not re.search(r"\d{4,}", text)  # no digits-only ids in the owner sentence
    assert "—" not in text and "–" not in text  # no em or en dashes
    live_again = env["c"].get("/v1/approvals?status=all", headers=h()).json()["approvals"]
    row = next(a for a in live_again if a["approval_id"] == second_id)
    assert row["status"] == "approved"  # the approval stays visible, not gone


def test_fake_write_does_not_consume_the_live_cap(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake-books write (no live connection row) leaves the live cap alone:
    the first real write after it still lands."""
    _live_env(monkeypatch)
    live = LiveLikeZoho()
    _live_transport(monkeypatch, live)
    # No connection row: fake path, demo venue.
    fake_approval = _approve_zoho(env)
    assert fake_approval["result"]["venue"] == "mimi-book-only"
    _seed_live_connection(env["engine"])
    second_id = _second_approval(env)
    ok = env["c"].post(f"/v1/approvals/{second_id}/approve", headers=h())
    assert ok.status_code == 200, ok.text
    res = ok.json()["approval"]["result"]
    assert res["venue"] == "zoho" and res["zoho_payment_id"].startswith("zp-live-")


def test_live_cap_is_per_tenant(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """One tenant's real write does not block another tenant's first write."""
    tenant_b = "second-lane"
    _live_env(monkeypatch)
    _seed_live_connection(env["engine"])
    _seed_live_connection(env["engine"], tenant=tenant_b)
    live = LiveLikeZoho()
    _live_transport(monkeypatch, live)
    first = _approve_zoho(env)
    assert first["result"]["zoho_payment_id"].startswith("zp-live-")

    from mimi.store.util import dumps as _dumps

    with env["engine"].connect() as conn:
        agents_row = conn.execute(sa.select(s.approvals).where(
            s.approvals.c.approval_id == first["approval_id"])).first()
    payload = loads(agents_row.payload, {})
    with env["engine"].begin() as conn:
        conn.execute(sa.insert(s.approvals).values(
            approval_id="appr-tenant-b-live-1", tenant_id=tenant_b,
            idempotency_key="zoho_payment:tenant-b-live-1", turn_id="t-b",
            thread_id=f"{tenant_b}:owner", helper="", skill="proof/bank-credit",
            shop_id="shop-008", tool="zoho_payment", intent_kind="zoho_payment",
            preview="preview", payload=_dumps(payload), status="pending",
            created_at=agents_row.created_at, created_ts=agents_row.created_ts,
            handling=""))
    ok = env["c"].post("/v1/approvals/appr-tenant-b-live-1", headers=h())
    assert ok.status_code in (403, 404)  # the demo key only knows tenant A, as it should
    # Approve tenant B directly at the seam instead.
    dw.on_approved(env["engine"], tenant_b,
                   {"approval_id": "appr-tenant-b-live-1",
                    **{"payload": dict(payload)}},
                   demo_mode=True, transport=live)
    with env["engine"].connect() as conn:
        done = conn.execute(sa.select(s.approvals).where(
            s.approvals.c.approval_id == "appr-tenant-b-live-1")).first()
    assert loads(done.payload, {})["result"]["zoho_payment_id"].startswith("zp-live-")


def test_agreement_reports_drift_per_shop(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Live transport with contacts: per-shop figures, Mimi-minus-Zoho
    differences, and null (never zero) for a shop Zoho does not know."""
    _live_env(monkeypatch)
    _seed_live_connection(env["engine"])
    with env["engine"].connect() as conn:
        mimi_total = sum(int(r.outstanding_kobo) for r in conn.execute(
            sa.select(s.receivables).where(
                s.receivables.c.tenant_id == A,
                s.receivables.c.outstanding_kobo > 0)))
    first_shop = next(iter(W.shops)).shop_id
    live = LiveLikeZoho(contacts=[
        {"contact_id": f"zc-{first_shop}", "contact_name": "Shop",
         "outstanding_receivable_amount": 120.5},
        {"contact_id": "zc-no-such-shop", "contact_name": "Ghost",
         "outstanding_receivable_amount": 7.0}])
    _live_transport(monkeypatch, live)
    r = env["c"].get("/v1/books/agreement", headers=h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["zoho_connected"] is True and body["currency"] == "NGN"
    by_shop = {x["shop_id"]: x for x in body["shops"]}
    row = by_shop[first_shop]
    assert row["mimi_outstanding_kobo"] > 0  # shop-001 carries the book's largest debt
    assert row["zoho_outstanding_kobo"] == 12050
    assert row["zoho_outstanding_naira"] == "₦120.50"
    # Sign: difference is Mimi minus Zoho, positive means the book is higher.
    assert row["difference_kobo"] == row["mimi_outstanding_kobo"] - 12050
    assert row["difference_kobo"] > 0  # Mimi higher here, by construction
    assert "zc-no-such-shop" not in by_shop  # unmapped contacts invent no Mimi shop
    assert body["total"]["zoho_outstanding_kobo"] == 12050 + 700
    assert body["total"]["mimi_outstanding_kobo"] == mimi_total
    assert body["total"]["difference_kobo"] == mimi_total - (12050 + 700)
    assert body["total"]["difference_kobo"] > 0  # Mimi higher in total too
    assert body["total"]["zoho_outstanding_naira"].startswith("₦")


def test_agreement_sign_flips_when_zoho_is_higher(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """One shop where Zoho reports more than the book: the difference is
    negative, and the naira string carries the minus, by code."""
    _live_env(monkeypatch)
    _seed_live_connection(env["engine"])
    first_shop = next(iter(W.shops)).shop_id
    live = LiveLikeZoho(contacts=[
        {"contact_id": f"zc-{first_shop}", "contact_name": "Shop",
         "outstanding_receivable_amount": 999999999.0}])
    _live_transport(monkeypatch, live)
    body = env["c"].get("/v1/books/agreement", headers=h()).json()
    row = {x["shop_id"]: x for x in body["shops"]}[first_shop]
    assert row["difference_kobo"] == row["mimi_outstanding_kobo"] - 99999999900
    assert row["difference_kobo"] < 0  # Zoho higher here
    assert row["difference_naira"].startswith("-₦")


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


def test_demo_zoho_call_speaks_as_the_account_owner(monkeypatch) -> None:
    """Composio refuses a call whose user id is not the connected account's
    owner: measured live on 4 Nov 2026, "Connected account user ID does not
    match the provided user ID", because the demo tenant passed its own id.
    A demo tenant borrowing the owner's account has to speak as that owner;
    a real distributor still speaks as itself, and an unset setting changes
    nothing."""
    monkeypatch.setenv("MIMI_DEMO_WIRING", "1")
    monkeypatch.setenv("MIMI_DEMO_ZOHO_ACCOUNT", "ca_demo")
    monkeypatch.delenv("MIMI_DEMO_ZOHO_USER", raising=False)
    assert dw._zoho_user("demo-abc", "ca_demo") == "mimi-sandbox-owner"
    monkeypatch.setenv("MIMI_DEMO_ZOHO_USER", "owner@example.com")
    assert dw._zoho_user("demo-abc", "ca_demo") == "owner@example.com"
    # a real tenant, and any account that is not the demo one
    assert dw._zoho_user("coral-lane", "ca_other") == "coral-lane"
    monkeypatch.delenv("MIMI_DEMO_ZOHO_ACCOUNT")
    assert dw._zoho_user("demo-abc", "ca_self") == "demo-abc"

"""Demo mode (``MIMI_MODE=demo``): per-visitor Coral Lane copies behind caps.

Offline: a scripted model (no naira spent) and a SQLite file per test. Model
spend is written straight into the ``spend`` table, the same row the loop
adds to, so the caps are tested without calling a paid model.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mimi.api import demo
from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import ConfigError, Settings, parse_keys
from mimi.api.demo import DemoConfig
from mimi.harness.gateway import ScriptedModel
from mimi.store import ensure_schema, make_engine
from mimi.store import schema as s
from mimi.store.util import dumps, now_iso
from mimi.tz import now_lagos
from mimi.world.loader import load_world
from tests.test_api import script

W = load_world()
SHOP = W.shops[0]


def cfg(**kw: Any) -> DemoConfig:
    base: dict[str, Any] = {"ip_daily_cap_kobo": 10_000, "global_daily_cap_kobo": 30_000,
                            "session_ttl_s": 3600.0, "sessions_per_ip": 5,
                            # The test client's peer is "testclient": trust it as
                            # the edge, so CF-Connecting-IP names the visitor.
                            "trusted_proxies": ("*",), "ip_salt": "test-salt"}
    base.update(kw)
    return DemoConfig(**base)


def make(store_url: str, conf: DemoConfig) -> tuple[TestClient, Any]:
    engine = make_engine(store_url)
    ensure_schema(engine)
    agent = Agent(engine, models=[ScriptedModel(script)], worker_id="test-worker")
    app = create_app(Settings(mode="demo", demo=conf, wait_s=5), agent, workers=0)
    return TestClient(app), engine


@pytest.fixture()
def env(store_url: str) -> Iterator[dict[str, Any]]:
    client, engine = make(store_url, cfg())
    with client as c:
        yield {"c": c, "engine": engine}
    engine.dispose()


def ip(addr: str) -> dict[str, str]:
    return {"CF-Connecting-IP": addr}


def start(c: TestClient, addr: str = "198.51.100.1") -> dict[str, Any]:
    r = c.post("/v1/demo/sessions", headers=ip(addr))
    assert r.status_code == 201, r.text
    return dict(r.json())


def bearer(sess: dict[str, Any], addr: str = "198.51.100.1") -> dict[str, str]:
    return {"Authorization": f"Bearer {sess['token']}", **ip(addr)}


def shop_turn(c: TestClient, sess: dict[str, Any], mid: str, text: str = "hi",
              addr: str = "198.51.100.1") -> Any:
    return c.post("/v1/turns", json={"message_id": mid, "phone": SHOP.phone, "text": text},
                  headers=bearer(sess, addr))


def shop_threads(c: TestClient, sess: dict[str, Any], addr: str = "198.51.100.1"
                 ) -> list[str]:
    rows = c.get("/v1/threads", headers=bearer(sess, addr)).json()["threads"]
    return [t["thread_id"] for t in rows if t["kind"] == "shop"]


def spend(engine: Any, tenant: str, kobo: int) -> None:
    day = now_lagos().date().isoformat()
    with engine.begin() as conn:
        conn.execute(s.spend.delete().where(s.spend.c.tenant_id == tenant,
                                            s.spend.c.day == day))
        conn.execute(s.spend.insert().values(tenant_id=tenant, day=day, kobo=kobo))


def n_turns(engine: Any, tenant: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.select(sa.func.count()).select_from(s.turns).where(
            s.turns.c.tenant_id == tenant)).scalar() or 0)


# --------------------------------------------------------------- sessions


def test_a_session_is_a_fresh_seeded_coral_lane_copy(env: dict[str, Any]) -> None:
    c = env["c"]
    sess = start(c)
    assert sess["mode"] == "demo" and sess["tenant_id"].startswith("demo-")
    assert sess["token"].startswith("mimi_demo_") and sess["expires_at"]
    assert sess["caps"]["ip_daily_cap_kobo"] == 10_000
    shops = c.get("/v1/shops", headers=bearer(sess)).json()
    assert shops["total"] == len(W.shops)
    me = c.get("/v1/me", headers=bearer(sess)).json()
    assert me["mode"] == "demo" and me["tenant_id"] == sess["tenant_id"]
    assert me["role"] == "owner"
    assert me["demo"]["expires_at"] == sess["expires_at"]
    assert me["demo"]["ip_spent_kobo"] == 0 and me["demo"]["paused"] is False


def test_the_token_is_kept_only_as_a_digest(env: dict[str, Any]) -> None:
    sess = start(env["c"])
    with env["engine"].connect() as conn:
        rows = conn.execute(sa.select(s.demo_sessions)).all()
    assert len(rows) == 1
    assert sess["token"] not in str(tuple(rows[0]))
    assert "198.51.100.1" not in str(tuple(rows[0]))  # the IP is hashed too


def test_a_session_turn_runs_and_sessions_are_isolated(env: dict[str, Any]) -> None:
    c = env["c"]
    a, b = start(c), start(c, "198.51.100.2")
    r = shop_turn(c, a, "demo.1")
    assert r.status_code == 200 and r.json()["state"] == "done"
    tid = r.json()["thread_id"]
    assert tid in shop_threads(c, a)
    assert shop_threads(c, b, "198.51.100.2") == []
    assert c.get(f"/v1/threads/{tid}", headers=bearer(b, "198.51.100.2")).status_code == 404


def test_an_expired_session_is_refused(env: dict[str, Any]) -> None:
    c = env["c"]
    sess = start(c)
    with env["engine"].begin() as conn:
        conn.execute(s.demo_sessions.update().values(expires_ts=time.time() - 1))
    r = c.get("/v1/shops", headers=bearer(sess))
    assert r.status_code == 401 and "expired" in r.json()["detail"]
    assert shop_turn(c, sess, "demo.exp").status_code == 401


def test_reset_gives_a_fresh_copy_and_ends_the_old_session(env: dict[str, Any]) -> None:
    c = env["c"]
    old = start(c)
    assert shop_turn(c, old, "demo.r1").status_code == 200
    assert shop_threads(c, old) != []
    r = c.post("/v1/demo/reset", headers=bearer(old))
    assert r.status_code == 201
    new = r.json()
    assert new["tenant_id"] != old["tenant_id"] and new["token"] != old["token"]
    assert shop_threads(c, new) == []  # a fresh copy: no conversation yet
    assert c.get("/v1/shops", headers=bearer(old)).status_code == 401


def test_sessions_per_ip_are_capped(store_url: str) -> None:
    client, engine = make(store_url, cfg(sessions_per_ip=2))
    with client as c:
        start(c)
        start(c)
        r = c.post("/v1/demo/sessions", headers=ip("198.51.100.1"))
        assert r.status_code == 429
        start(c, "198.51.100.7")  # another visitor still gets one
    engine.dispose()


def test_forwarded_for_never_buys_a_fresh_allowance(store_url: str) -> None:
    # 30 Oct 2026 live test: a made-up X-Forwarded-For got a new allowance.
    client, engine = make(store_url, cfg(sessions_per_ip=1))
    with client as c:
        h = {"CF-Connecting-IP": "198.51.100.1"}
        assert c.post("/v1/demo/sessions", headers=h).status_code == 201
        for fake in ("198.51.100.2", "10.9.9.9, 198.51.100.3", "203.0.113.9"):
            r = c.post("/v1/demo/sessions", headers={**h, "X-Forwarded-For": fake,
                                                     "X-Real-IP": fake})
            assert r.status_code == 429, fake
    engine.dispose()


def test_the_client_ip_header_is_read_only_from_a_trusted_peer(store_url: str) -> None:
    # Default proxies (private ranges): the test client's peer is not one, so a
    # client-sent CF-Connecting-IP is ignored and the peer is the visitor.
    client, engine = make(store_url, cfg(sessions_per_ip=1,
                                         trusted_proxies=demo.DEFAULT_TRUSTED_PROXIES))
    with client as c:
        assert c.post("/v1/demo/sessions", headers=ip("198.51.100.1")).status_code == 201
        assert c.post("/v1/demo/sessions", headers=ip("198.51.100.2")).status_code == 429
    engine.dispose()


def test_expired_sessions_are_swept_and_their_spend_kept(env: dict[str, Any]) -> None:
    c, engine = env["c"], env["engine"]
    old = start(c)
    spend(engine, old["tenant_id"], 4_000)
    with engine.begin() as conn:
        conn.execute(s.demo_sessions.update().values(
            expires_ts=time.time() - demo.SWEEP_GRACE_S - 1))
    new = start(c)  # creating a session sweeps
    with engine.connect() as conn:
        left = conn.execute(sa.select(sa.func.count()).select_from(s.shops).where(
            s.shops.c.tenant_id == old["tenant_id"])).scalar()
        swept = conn.execute(sa.select(s.demo_sessions.c.swept).where(
            s.demo_sessions.c.session_tenant == old["tenant_id"])).scalar()
    assert left == 0 and swept
    # The visitor's spend today still counts after its copy is gone.
    assert c.get("/v1/me", headers=bearer(new)).json()["demo"]["ip_spent_kobo"] == 4_000


# ------------------------------------------------------------------- caps


def test_the_per_ip_cap_refuses_before_a_turn_is_queued(env: dict[str, Any]) -> None:
    c, engine = env["c"], env["engine"]
    sess = start(c)
    spend(engine, sess["tenant_id"], 10_000)
    r = shop_turn(c, sess, "demo.cap")
    assert r.status_code == 429 and "today" in r.json()["detail"]
    assert n_turns(engine, sess["tenant_id"]) == 0
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(s.inbound_messages)
                            ).scalar() == 0


def test_the_per_ip_cap_spans_that_visitors_sessions(env: dict[str, Any]) -> None:
    c, engine = env["c"], env["engine"]
    first = start(c)
    spend(engine, first["tenant_id"], 10_000)
    again = start(c)  # a new copy from the same address
    assert shop_turn(c, again, "demo.cap2").status_code == 429
    other = start(c, "198.51.100.9")
    assert shop_turn(c, other, "demo.cap3", addr="198.51.100.9").status_code == 200


def test_the_global_cap_refuses_every_visitor(env: dict[str, Any]) -> None:
    c, engine = env["c"], env["engine"]
    for addr in ("198.51.100.11", "198.51.100.12", "198.51.100.13"):
        spend(engine, start(c, addr)["tenant_id"], 10_000)  # 30,000 in all
    fresh = start(c, "198.51.100.20")
    r = shop_turn(c, fresh, "demo.g", addr="198.51.100.20")
    assert r.status_code == 429 and "demo" in r.json()["detail"]
    assert n_turns(engine, fresh["tenant_id"]) == 0


def test_the_session_tenant_cap_matches_the_ip_cap(env: dict[str, Any]) -> None:
    # A run that goes over mid-turn parks on the tenant's own daily cap.
    sess = start(env["c"])
    with env["engine"].connect() as conn:
        cap = conn.execute(sa.select(s.tenants.c.daily_cap_kobo).where(
            s.tenants.c.tenant_id == sess["tenant_id"])).scalar()
    assert cap == 10_000


def test_the_kill_switch_refuses_new_turns_but_keeps_reads(store_url: str) -> None:
    client, engine = make(store_url, cfg(killed=True))
    with client as c:
        sess = start(c)
        r = shop_turn(c, sess, "demo.k")
        assert r.status_code == 503 and "paused" in r.json()["detail"]
        ev = c.post("/v1/events", json={"kind": "routine", "event_id": "e1",
                                        "name": "owner_brief"}, headers=bearer(sess))
        assert ev.status_code == 503
        assert n_turns(engine, sess["tenant_id"]) == 0
        assert c.get("/v1/shops", headers=bearer(sess)).status_code == 200
        assert c.get("/v1/me", headers=bearer(sess)).json()["demo"]["paused"] is True
    engine.dispose()


# -------------------------------------------------------------- approvals


def _pending(engine: Any, tenant: str, aid: str, tool: str) -> None:
    now = time.time()
    with engine.begin() as conn:
        conn.execute(s.approvals.insert().values(
            approval_id=aid, tenant_id=tenant, idempotency_key=f"k:{aid}", turn_id="",
            thread_id="", helper="", skill="", shop_id="shop-001", tool=tool,
            intent_kind=tool, preview="", payload=dumps({}), status="pending",
            decided_by="", note="", created_at=now_iso(now), created_ts=now))


@pytest.mark.parametrize("tool", ["submit_credit_review", "raise_daily_allowance",
                                  "raise_credit_limit"])
def test_demo_never_approves_a_cap_raise(env: dict[str, Any], tool: str) -> None:
    c, engine = env["c"], env["engine"]
    sess = start(c)
    _pending(engine, sess["tenant_id"], "apr-1", tool)
    r = c.post("/v1/approvals/apr-1/approve", headers=bearer(sess))
    assert r.status_code == 403 and "demo" in r.json()["detail"]
    with engine.connect() as conn:
        assert conn.execute(sa.select(s.approvals.c.status).where(
            s.approvals.c.approval_id == "apr-1")).scalar() == "pending"
    # Declining is still fine.
    assert c.post("/v1/approvals/apr-1/reject", headers=bearer(sess),
                  params={"wait": "false"}).status_code == 200


def test_demo_still_approves_an_ordinary_draft(env: dict[str, Any]) -> None:
    c = env["c"]
    sess = start(c)
    r = shop_turn(c, sess, "demo.firm", "abeg wetin I owe now? FIRM").json()
    assert r["status"] == "parked_for_approval"
    [a] = c.get("/v1/approvals", headers=bearer(sess)).json()["approvals"]
    assert c.post(f"/v1/approvals/{a['approval_id']}/approve",
                  headers=bearer(sess)).status_code == 200


# ----------------------------------------------------------------- config


def test_demo_config_from_env_has_placeholder_defaults() -> None:
    st = Settings.from_env({"MIMI_MODE": "demo"})
    assert st.mode == "demo"
    # N400 a visitor a day (about 25 turns), N2,000 for the whole demo.
    assert st.demo.ip_daily_cap_kobo == demo.DEFAULT_IP_CAP_NAIRA * 100 == 40_000
    assert st.demo.global_daily_cap_kobo == demo.DEFAULT_GLOBAL_CAP_NAIRA * 100 == 200_000
    assert st.demo.killed is False
    assert st.demo.client_ip_header == "cf-connecting-ip"
    assert st.demo.trusted_proxies == demo.DEFAULT_TRUSTED_PROXIES
    st2 = Settings.from_env({"MIMI_MODE": "demo", "MIMI_DEMO_IP_DAILY_CAP_NAIRA": "50",
                             "MIMI_DEMO_GLOBAL_DAILY_CAP_NAIRA": "1500",
                             "MIMI_DEMO_KILL": "1", "MIMI_DEMO_SESSION_TTL_S": "900",
                             "MIMI_DEMO_SESSIONS_PER_IP": "3",
                             "MIMI_DEMO_TRUST_FORWARDED": "1",
                             "MIMI_DEMO_CLIENT_IP_HEADER": "True-Client-IP",
                             "MIMI_DEMO_TRUSTED_PROXIES": "10.0.0.0/8, 172.16.0.0/12"})
    d = st2.demo
    assert (d.ip_daily_cap_kobo, d.global_daily_cap_kobo, d.killed, d.session_ttl_s,
            d.sessions_per_ip) == (5_000, 150_000, True, 900.0, 3)
    assert d.client_ip_header == "true-client-ip"
    assert d.trusted_proxies == ("10.0.0.0/8", "172.16.0.0/12")
    with pytest.raises(ValueError, match="X-Forwarded-For"):
        DemoConfig.from_env({"MIMI_DEMO_CLIENT_IP_HEADER": "X-Forwarded-For"})
    with pytest.raises(ValueError):
        DemoConfig.from_env({"MIMI_DEMO_TRUSTED_PROXIES": "10.0.0.0/33"})
    assert Settings.from_env({}).mode == "live"


def test_mode_must_be_live_or_demo() -> None:
    with pytest.raises(ConfigError, match="MIMI_MODE"):
        Settings.from_env({"MIMI_MODE": "staging"}).check()


def test_production_demo_needs_a_salt_and_no_static_keys(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_PHONE_PSEUDONYM_KEY", "test-only-pseudonym-key")
    base: dict[str, Any] = {"env": "production", "mode": "demo", "has_model_key": True,
                            "database_url": "postgresql://x/y"}
    with pytest.raises(ConfigError, match="MIMI_DEMO_IP_SALT"):
        Settings(**base, demo=DemoConfig()).check()
    Settings(**base, demo=DemoConfig(ip_salt="s" * 32)).check()  # no MIMI_API_KEYS needed
    with pytest.raises(ConfigError, match="demo key"):
        Settings(**base, demo=DemoConfig(ip_salt="s" * 32),
                 keys=parse_keys("coral-lane:owner:local-owner-key-0001")).check()
    with pytest.raises(ConfigError, match="SQLite"):
        Settings(**{**base, "database_url": "sqlite:///x.db"},
                 demo=DemoConfig(ip_salt="s" * 32)).check()
    # Live production is unchanged: keys still required.
    with pytest.raises(ConfigError, match="MIMI_API_KEYS"):
        Settings(env="production", has_model_key=True,
                 database_url="postgresql://x/y").check()

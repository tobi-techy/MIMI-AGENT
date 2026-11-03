"""Sales tracking (3 Nov 2026): expiry on delivery lines, the slow-stock and
near-expiry alert, the swap / promo / pickup draft, the sell-through read,
rewards for sharing counts, and the durable check-in state. Code only: no
model call anywhere in these paths."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import fields, replace
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mimi.api import sales
from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.checkin.durable import SqlCheckinStore, SqlSkipLedger, SqlStreaks
from mimi.checkin.state import CheckinStore
from mimi.credit.review import CountSharing, CreditFile, recommend
from mimi.harness.gateway import ScriptedModel
from mimi.provenance import proof_level, witness
from mimi.stock.estimator import estimate
from mimi.stock.history import ItemHistory, StockCount, StockIn, item_histories
from mimi.stock.intents import STOCK_INTENTS, SlowStockDraftForApproval, SlowStockSignal
from mimi.stock.selling import alerts, sell_through, sold_from_counts, status_of
from mimi.stock.slow import (
    SHOP_WEEKLY_CAP,
    Batch,
    SlowBook,
    assess,
    count_implied_rate,
    pick_slow,
    remaining_batches,
    units_at_risk,
)
from mimi.stock.slow_eval import REPORT, run
from mimi.stock.swap import Peer, draft_id_for, plan, resellable, shop_text, verify
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.store.util import loads
from mimi.world.expiry import demo_counts, expiry_map, line_expiry, seeded_short_dated
from mimi.world.generator import world_hash
from mimi.world.loader import load_world
from mimi.world.model import Sku
from tests.test_api import script

W = load_world()
A = "coral-lane"
OWNER = "s-owner-key-000000000001"
SKU = Sku("TEST-1", "Test Custard 2kg", "Test", "tub", 720_000)
ID_RE = re.compile(r"\b(?:shop|dlv|apr|stk)-?\d|\bshop_\w|_kobo")


def hist(*ins: tuple[int, int, int | None], visit: int = 7, shop: str = "shop-a",
         sku: str = "TEST-1") -> ItemHistory:
    return ItemHistory(shop, sku, "Test Custard 2kg (tub)", "tub", tuple(
        StockIn(d, q, f"d{i}", expires_day=e, batch=f"B{i}") for i, (d, q, e) in enumerate(ins)),
        visit_days=visit)


# ------------------------------------------------------------ 1. expiry


def test_expiry_overlay_is_deterministic_and_leaves_the_world_fixture_alone() -> None:
    before = world_hash(W)
    a, b = expiry_map(W), expiry_map(W)
    assert a == b and world_hash(W) == before
    n_lines = sum(len(d.lines) for d in (*W.deliveries, *W.live_deliveries))
    assert len(a) == n_lines
    assert all(e.expires_day > 0 and e.batch for e in a.values())
    short = sum(e.short_dated for e in a.values())
    assert 0 < short < n_lines * 0.15
    one = line_expiry(W.seed, "dlv-01-0001", "PWATER-BAG", 1)
    assert one.expires_day - 1 <= 35  # sachet water: weeks, not months


def test_demo_seeds_near_expiry_on_slow_lines_one_per_shop() -> None:
    seeded = seeded_short_dated(W)
    assert len(seeded) == 8
    shops = {W.delivery(d).shop_id for d, _ in seeded}
    assert len(shops) == 8
    assert all(W.live_day + 9 <= day <= W.live_day + 40 for day in seeded.values())


def test_histories_carry_batch_and_expiry() -> None:
    hs = item_histories(W, upto_day=W.live_day, expiries=expiry_map(W))
    s_in = next(iter(hs.values())).stock_in[0]
    assert s_in.expires_day is not None and s_in.batch
    plain = item_histories(W, upto_day=W.live_day)
    assert next(iter(plain.values())).stock_in[0].expires_day is None


def test_seed_writes_expiry_on_every_line_and_demo_counts_only_when_asked(
        store_url: str) -> None:
    e = make_engine(store_url)
    ensure_schema(e)
    seed_world(e, W)
    with e.connect() as conn:
        missing = conn.execute(sa.select(sa.func.count()).select_from(s.delivery_lines).where(
            s.delivery_lines.c.expires_on.is_(None))).scalar()
        n_counts = conn.execute(sa.select(sa.func.count()).select_from(s.stock_counts)).scalar()
    assert missing == 0 and n_counts == 0
    seed_world(e, W, reset=True, stock_demo=True)
    with e.connect() as conn:
        rows = conn.execute(sa.select(s.stock_counts)).all()
    assert len(rows) == len(demo_counts(W)) == 8
    assert {r.source for r in rows} == {"checkin"}
    seed_world(e, W, reset=True)  # reset drops the demo counts again
    with e.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(s.stock_counts)).scalar() == 0


# ------------------------------------------------------------ 2. slow stock


def test_units_left_are_the_newest_batches() -> None:
    h = hist((0, 10, 100), (7, 10, 50), (14, 10, 200))
    b = remaining_batches(h, 15, 20)
    assert [(x.day, x.units) for x in b] == [(7, 5), (14, 10)]


def test_units_at_risk_sells_oldest_first_until_the_safety_margin() -> None:
    batches = [Batch(0, 10, 30), Batch(7, 10, 200)]
    # 1 a day from day 20: 10 days to sell batch 1, but it must be off the
    # shelf on day 27 (3 days before 30): 7 sell, 3 are lost.
    assert units_at_risk(batches, 1.0, 20) == (3, 30)
    assert units_at_risk(batches, 2.0, 20) == (0, None)
    assert units_at_risk(batches, 0.0, 20) == (20, 30)


def test_near_expiry_flagged_when_days_to_sell_exceed_days_to_expiry() -> None:
    h = hist((0, 6, 300), (7, 6, 300), (14, 6, 300), (21, 12, 40))
    counts = [StockCount(27, "6+", 14, source="typed")]
    e = estimate(h, counts, 28)
    assert e is not None
    a = assess(e, h, counts)
    assert a is not None and a.kind == "near_expiry" and a.units_at_risk >= 1
    assert a.excess_units == a.units_at_risk and a.days_to_expiry == 12
    assert a.expiry_band == "under_14_days"


def test_overstock_flagged_far_above_normal_cover() -> None:
    h = hist((0, 7, None), (7, 7, None), (14, 7, None), (21, 7, None), (24, 40, None))
    counts = [StockCount(27, "6+", 44, source="typed")]
    e = estimate(h, counts, 28)
    assert e is not None
    a = assess(e, h, counts)
    assert a is not None and a.kind == "overstock" and a.excess_units > 20
    assert a.days_to_expiry is None and a.expiry_band == "unknown"


def test_a_normal_line_is_not_flagged() -> None:
    h = hist((0, 7, 300), (7, 7, 300), (14, 7, 300), (21, 7, 300))
    e = estimate(h, [], 26)
    assert e is not None and assess(e, h) is None


def test_confidence_is_capped_without_a_fresh_count() -> None:
    h = hist((0, 6, 300), (7, 6, 300), (14, 6, 300), (21, 20, 33))
    e = estimate(h, [], 24)
    assert e is not None
    a = assess(e, h)
    assert a is not None and a.confidence in ("low", "medium")
    single = hist((20, 9, 30))
    e1 = estimate(single, [], 24)
    assert e1 is not None
    a1 = assess(e1, single)
    assert a1 is None or a1.confidence == "low"


def test_a_count_after_a_missed_reorder_lowers_the_rate_never_raises_it() -> None:
    h = hist((0, 7, None), (7, 7, None), (14, 7, None))
    counts = [StockCount(26, "6+", 6, source="typed")]
    e = estimate(h, counts, 26)
    assert e is not None
    r = count_implied_rate(e, h, counts)
    assert r is not None and r < e.sell_rate_per_day
    a = assess(e, h, counts)
    assert a is not None and a.sell_rate_per_day < e.sell_rate_per_day


def test_slow_caps_once_per_item_and_shop_per_week() -> None:
    h1 = hist((0, 6, 300), (7, 6, 300), (14, 6, 300), (21, 12, 40), shop="shop-a")
    h2 = replace(h1, sku="TEST-2")
    counts = [StockCount(27, "6+", 14, source="typed")]
    found = []
    for h in (h1, h2):
        e = estimate(h, counts, 28)
        assert e is not None
        a = assess(e, h, counts)
        assert a is not None
        found.append(a)
    book = SlowBook()
    first = pick_slow(found, book, "2026W44")
    assert len(first) == SHOP_WEEKLY_CAP == 1
    assert pick_slow(found, book, "2026W44") == []
    assert len(pick_slow(found, book, "2026W45")) == 1


def test_slow_intents_never_move_stock_money_or_show_counts() -> None:
    assert SlowStockSignal in STOCK_INTENTS and SlowStockDraftForApproval in STOCK_INTENTS
    names = {f.name for f in fields(SlowStockSignal)}
    assert not names & {"units", "units_left", "count", "bucket", "units_assumed"}
    d = {f.name: f for f in fields(SlowStockDraftForApproval)}
    assert d["moves_stock"].default is False and d["moves_money"].default is False
    assert d["status"].default == "draft"


# ------------------------------------------------------------ 3. the draft


def _slow_case() -> tuple[Any, Any]:
    h = hist((0, 6, 300), (7, 6, 300), (14, 6, 300), (21, 12, 60))
    counts = [StockCount(27, "6+", 14, source="typed")]
    e = estimate(h, counts, 28)
    assert e is not None
    a = assess(e, h, counts)
    assert a is not None
    return a, e


def test_plan_offers_move_pickup_and_promo_with_kobo_from_code() -> None:
    a, e = _slow_case()
    fast = replace(e, shop_id="shop-b", sell_rate_per_day=e.sell_rate_per_day * 4,
                   estimated_units_left=1)
    far = replace(fast, shop_id="shop-c")
    peers = [Peer("shop-b", "Bisi Store", "Oja Oba", "rep-1", fast),
             Peer("shop-c", "Far Store", "Elsewhere", "rep-2", far)]
    d = plan(a, area="Oja Oba", rep_id="rep-1", peers=peers, sku=SKU, day=28, rise_day=99,
             tenant_id=A, week="2026W44", as_of="2026-10-26", expires_on="2026-11-30")
    assert d is not None and d.recommended == "move"
    acts = [o.action for o in d.options]
    assert acts == ["move", "pickup", "promo"] if resellable(a) else acts[0] == "move"
    move = d.options[0]
    assert move.to_shop_id == "shop-b" and move.value_kobo == move.quantity * 720_000
    assert verify(d, a, sku=SKU, day=28, rise_day=99, nearby={"shop-b"}) == []
    assert d.draft_id == draft_id_for("shop-a", "TEST-1", "2026W44")


def test_verify_refuses_wrong_figures_and_far_shops() -> None:
    a, e = _slow_case()
    fast = replace(e, shop_id="shop-b", sell_rate_per_day=e.sell_rate_per_day * 4,
                   estimated_units_left=0)
    d = plan(a, area="X", rep_id="r", peers=[Peer("shop-b", "B", "X", "r", fast)], sku=SKU,
             day=28, rise_day=99, tenant_id=A, week="w", as_of="", expires_on="")
    assert d is not None
    bad_value = replace(d, options=(replace(d.options[0], value_kobo=1),) + d.options[1:])
    assert any("value" in p for p in verify(bad_value, a, sku=SKU, day=28, rise_day=99,
                                            nearby={"shop-b"}))
    assert any("nearby" in p for p in verify(d, a, sku=SKU, day=28, rise_day=99, nearby=set()))
    too_many = replace(d, options=(replace(d.options[0], quantity=a.units_left + 1,
                                           value_kobo=(a.units_left + 1) * 720_000),))
    assert any("more than" in p for p in verify(too_many, a, sku=SKU, day=28, rise_day=99,
                                                nearby={"shop-b"}))


def test_no_pickup_once_too_close_to_expiry() -> None:
    a, _ = _slow_case()
    close = replace(a, days_to_expiry=10)
    assert not resellable(close)
    d = plan(close, area="X", rep_id="r", peers=[], sku=SKU, day=28, rise_day=99,
             tenant_id=A, week="w", as_of="", expires_on="2026-11-05")
    assert d is not None and [o.action for o in d.options] == ["promo"]


def test_shop_copy_is_plain_with_no_ids_or_dashes() -> None:
    a, e = _slow_case()
    d = plan(a, area="X", rep_id="r", peers=[Peer("shop-b", "B", "X", "r", replace(
        e, shop_id="shop-b", sell_rate_per_day=9.0, estimated_units_left=0))], sku=SKU, day=28,
        rise_day=99, tenant_id=A, week="w", as_of="", expires_on="2026-11-30")
    assert d is not None
    for o in d.options:
        text = shop_text(o, a.item, a.unit, "Coral Lane Trading", d.expires_on)
        assert "\u2014" not in text and "\u2013" not in text and not ID_RE.search(text)
        assert "Test Custard 2kg" in text and "(tub)" not in text


# ------------------------------------------------------------ 4. sell-through


def test_sold_from_two_counts_is_count_plus_delivered_minus_count() -> None:
    h = hist((0, 10, None), (10, 8, None), (20, 6, None))
    counts = [StockCount(5, "6+", 7, source="typed"), StockCount(25, "3-5", 4, source="typed")]
    sold, over, used = sold_from_counts(h, counts, 25, 28) or (0, 0, [])
    assert (sold, over) == (7 + 8 + 6 - 4, 20) and len(used) == 2
    r = sell_through(h, counts, 26)
    assert r is not None and r.sold_method == "counts" and r.proof == "estimated (shop count)"
    pos = [replace(c, source="connector") for c in counts]
    r2 = sell_through(h, pos, 26)
    assert r2 is not None and r2.proof_level == "verified"
    r3 = sell_through(h, [], 26)
    assert r3 is not None and r3.sold_method == "estimate" and r3.proof == "estimated (estimate)"
    assert proof_level("sell_through", [witness("estimator", "x")]) == "estimated"


def test_low_confidence_slow_is_a_watch_not_a_status() -> None:
    a, e = _slow_case()
    low = replace(a, confidence="low")
    assert not alerts(low) and status_of(e, low) in ("ok", "low")
    assert status_of(e, a) == ("near_expiry" if a.kind == "near_expiry" else "slow")


# ------------------------------------------------------------ 5. sharing counts


def _file(**kw: Any) -> CreditFile:
    base: dict[str, Any] = dict(
        shop_id="shop-001", shop_name="Mama Sola", current_limit_kobo=10_000_000,
        outstanding_kobo=0, overdue_kobo=0, oldest_days_overdue=0, window_days=56,
        deliveries=8, delivered_kobo=1, verified_payments=8, verified_paid_kobo=1,
        verified_on_time=8, on_time_pct=100, unverified_payments=0, unverified_kobo=0,
        promises_made=0, promises_broken=0, open_disputes=0, utilisation_pct=0)
    base.update(kw)
    return CreditFile(**base)


def test_sharing_counts_is_a_reason_never_an_outcome() -> None:
    for f in (_file(), _file(oldest_days_overdue=30, overdue_kobo=500_000)):
        plain = recommend(f, None)
        shared = recommend(f, None, CountSharing(5))
        assert shared.outcome == plain.outcome and shared.confidence == plain.confidence
        assert shared.flags == plain.flags
        assert any("regularly: 5 of the last 8 weekly check-ins" in r for r in shared.reasons)
    assert recommend(_file(), None, CountSharing(2)).reasons == recommend(_file(), None).reasons


# ------------------------------------------------------------ 6. durable check-in


def test_durable_checkin_matches_memory_and_survives_a_restart(store_url: str) -> None:
    e = make_engine(store_url)
    ensure_schema(e)
    sql, mem = SqlCheckinStore(e, A), CheckinStore()
    for st in (sql, mem):
        rec, new = st.start("shop-001", "2026W44", "ev-1")
        assert new and rec.status == "asked" and st.seen("ev-1") and st.has("shop-001", "2026W44")
        again, new2 = st.start("shop-001", "2026W44", "ev-2")
        assert not new2 and again.event_id == "ev-1"
        assert st.answer("shop-001", "2026W44", "2026-10-26T09:00") is not None
        assert st.answer("shop-001", "2026W44") is None  # once
        assert st.skip("shop-001", "2026W44") is None
    restarted = SqlCheckinStore(make_engine(store_url), A)
    assert restarted.status("shop-001", "2026W44") == "answered"
    assert restarted.answered_weeks("shop-001") == ["2026W44"]
    assert SqlCheckinStore(e, "other-tenant").status("shop-001", "2026W44") == ""
    skips, streaks = SqlSkipLedger(e, A), SqlStreaks(e, A)
    for _ in range(3):
        skips.note_skip("shop-002")
    assert SqlSkipLedger(e, A).needs_connect_offer("shop-002")
    skips.mark_offered("shop-002")
    assert not skips.needs_connect_offer("shop-002") and not skips.weekly_allowed("shop-002")
    for _ in range(3):
        streaks.note_answer("shop-003")
    assert SqlStreaks(e, A).earns_credit_line("shop-003")
    streaks.note_skip("shop-003")
    assert streaks.current("shop-003") == 0


# ------------------------------------------------------------ the eval


def test_slow_eval_is_deterministic_and_matches_its_report() -> None:
    r = run()
    assert json.loads(json.dumps(r, sort_keys=True)) == json.loads(REPORT.read_text())
    held = r["seeds"]["held_out"]
    assert held["positives"] > 0 and held["lines"] == r["seeds"]["dev"]["lines"]


# ------------------------------------------------------------ the API


@pytest.fixture()
def api(store_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("MIMI_DEMO_WIRING", "1")
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W, stock_demo=True)
    agent = Agent(engine, models=[ScriptedModel(script)], worker_id="test-worker")
    app = create_app(Settings(keys=parse_keys(f"{A}:owner:{OWNER}"), wait_s=5), agent,
                     workers=0)
    with TestClient(app) as client:
        yield {"c": client, "engine": engine, "agent": agent}
    engine.dispose()


def h() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER}"}


def test_sell_through_read(api: dict[str, Any]) -> None:
    c = api["c"]
    r = c.get("/v1/stock/sell-through", headers=h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["near_expiry"] >= 4 and body["at_risk_ngn"] > 0
    assert body["at_risk_text"].startswith("\u20a6")
    top = body["rows"][0]
    assert top["status"] == "near_expiry" and top["confidence"] in ("medium", "high")
    assert top["expires_on"] and top["days_to_expiry"] is not None
    for row in body["rows"]:
        assert not ID_RE.search(row["note"]) and "\u2014" not in row["note"]
        assert "(ctn of" not in row["item"]
    one = c.get("/v1/stock/sell-through?shop_id=shop-001&status=low", headers=h()).json()
    assert all(x["shop_id"] == "shop-001" and x["status"] == "low" for x in one["rows"])
    assert c.get("/v1/stock/sell-through?shop_id=shop-999", headers=h()).status_code == 404
    shop = c.get("/v1/shops/shop-001", headers=h()).json()
    assert shop["deliveries"][0]["lines"][0]["expires_on"]


def test_slow_check_opens_approvals_once_and_approve_undo(api: dict[str, Any]) -> None:
    c, engine = api["c"], api["engine"]
    r = c.post("/v1/stock/slow-check", headers=h())
    assert r.status_code == 200, r.text
    opened = r.json()["opened"]
    assert len(opened) >= 4 and r.json()["skipped"] == []
    assert c.post("/v1/stock/slow-check", headers=h()).json()["opened"] == []
    apps = [a for a in c.get("/v1/approvals", headers=h()).json()["approvals"]
            if a["kind"] == "slow_stock"]
    assert len(apps) == len(opened)
    shops = [a["shop_id"] for a in apps]
    assert len(shops) == len(set(shops))  # one per shop per week
    a = next(x for x in apps if any(o["action"] == "pickup" for o in x["payload"]["options"]))
    p = a["payload"]
    assert p["moves_stock"] is False and p["moves_money"] is False
    for o in p["options"]:
        assert o["value_kobo"] == o["quantity"] * o["unit_price_kobo"]
        assert not ID_RE.search(o["shop_message"])
    assert not ID_RE.search(a["preview"])
    other = next(x for x in apps if not any(o["action"] == "pickup"
                                            for o in x["payload"]["options"]))
    bad = c.post(f"/v1/approvals/{other['approval_id']}/approve", json={"option": "pickup"},
                 headers=h())
    assert bad.status_code == 409
    ok = c.post(f"/v1/approvals/{a['approval_id']}/approve", json={"option": "pickup"},
                headers=h())
    assert ok.status_code == 200, ok.text
    got = ok.json()["approval"]
    assert got["status"] == "approved" and got["result"]["option"] == "pickup"
    assert got["result"]["undo_available"] is True
    with engine.connect() as conn:
        shop_msgs = conn.execute(sa.select(s.messages.c.text).where(
            s.messages.c.thread_id == f"{A}:shop:{a['shop_id']}",
            s.messages.c.kind == "slow_stock_shop")).scalars().all()
    assert len(shop_msgs) == 1 and "collect" in shop_msgs[0]
    u = c.post(f"/v1/approvals/{a['approval_id']}/undo", headers=h())
    assert u.status_code == 200 and u.json()["approval"]["result"]["undo_available"] is False
    assert c.post(f"/v1/approvals/{a['approval_id']}/undo", headers=h()).status_code == 409
    rej = c.post(f"/v1/approvals/{other['approval_id']}/reject", headers=h())
    assert rej.status_code == 200 and rej.json()["approval"]["status"] == "rejected"
    with engine.connect() as conn:
        n = conn.execute(sa.select(sa.func.count()).select_from(s.messages).where(
            s.messages.c.thread_id == f"{A}:shop:{other['shop_id']}",
            s.messages.c.kind == "slow_stock_shop")).scalar()
    assert n == 0  # rejected: the shop is told nothing


def test_slow_check_routine_is_code(api: dict[str, Any]) -> None:
    c, agent, engine = api["c"], api["agent"], api["engine"]
    r = c.post("/v1/events", json={"event_id": "slow-1", "kind": "routine",
                                   "name": "slow_stock_check"}, headers=h())
    assert r.status_code == 202, r.text
    agent.process(r.json()["turn_id"])
    with engine.connect() as conn:
        n = conn.execute(sa.select(sa.func.count()).select_from(s.approvals).where(
            s.approvals.c.tool == sales.SLOW_KIND)).scalar()
        line = conn.execute(sa.select(s.messages.c.text).where(
            s.messages.c.kind == "slow_stock_check")).scalar()
    assert n >= 4 and line and "Nothing moves until you approve" in line
    p = loads(conn_payload(engine), {})
    assert p["kind"] == "slow_stock"


def conn_payload(engine: Any) -> str:
    with engine.connect() as conn:
        return str(conn.execute(sa.select(s.approvals.c.payload).where(
            s.approvals.c.tool == sales.SLOW_KIND)).scalars().first())

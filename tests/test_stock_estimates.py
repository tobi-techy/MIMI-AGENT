"""Stock estimate records (RAI-294): every acceptance criterion, table-driven.

Pure ``estimate_row`` cases first, then ``StockBook`` over the store (the
table, the count door RAI-276 calls, the reads), then the architecture rule
that credit never reads an estimate.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from mimi.stock.estimator import (
    CONF_BUCKET,
    CONF_DECAY_PER_DAY,
    CountIn,
    DeliveryIn,
    UnitBook,
    confidence_word,
    count_wanted,
    estimate_row,
)
from mimi.tz import WAT

ROOT = Path(__file__).resolve().parents[1]
DAY0 = date(2026, 9, 1)
CTN40 = UnitBook("NDL-70-CTN", "ctn", 40)
ONE = UnitBook("SUG-1KG", "pk", 1)


def rhythm(n: int = 4, every: int = 5, qty: int = 10, start: int = 0) -> list[DeliveryIn]:
    return [DeliveryIn(f"dlv-{i}", start + i * every, qty, "ctn") for i in range(n)]


def row(ins: list[DeliveryIn], counts: list[CountIn] | None = None, day: int = 16,
        units: UnitBook = CTN40, **kw: Any):  # type: ignore[no-untyped-def]
    return estimate_row("shop-1", units, ins, counts or [], day, day0=DAY0, **kw)


# ------------------------------------------------------------------ acceptance criteria


def test_fixed_rhythm_gives_two_a_day_and_falls_one_day_a_day() -> None:
    """10 cartons every 5 days for 4 deliveries, no count: 2 a day."""
    ins = rhythm()
    days = []
    for d in range(16, 21):
        r = row(ins, day=d)
        assert r.status == "ok" and r.est_daily_rate == 2.0
        days.append(r.days_to_run_out)
    assert [round(a - b, 6) for a, b in zip(days, days[1:], strict=False)] == [1.0] * 4


def test_one_tap_resets_to_the_bucket_and_its_confidence() -> None:
    r = row(rhythm(), [CountIn("wamid.1", 16, "tap", bucket="0-2")], day=16)
    assert r.anchor_source == "tap" and r.anchor_range == "0-2"
    assert r.est_left == 1.0  # the bucket's middle, in cartons
    assert r.confidence == CONF_BUCKET and r.confidence_word == "High"


def test_a_typed_count_resets_to_exactly_that() -> None:
    r = row(rhythm(), [CountIn("wamid.2", 16, "typed", quantity=7, unit="ctn")], day=16)
    assert r.est_left == 7.0 and r.anchor_qty == 7.0 and r.confidence == 1.0


def test_an_unconfirmed_delivery_does_not_change_stock_in() -> None:
    base = row(rhythm(), day=17)
    extra = [*rhythm(), DeliveryIn("dlv-x", 16, 30, "ctn", confirmed=False)]
    with_unconfirmed = row(extra, day=17)
    assert with_unconfirmed.est_left == base.est_left
    assert "dlv-x" not in with_unconfirmed.inputs
    assert with_unconfirmed.inputs_hash == base.inputs_hash


@pytest.mark.parametrize("ins", [[], [DeliveryIn("dlv-0", 3, 10, "ctn")]])
def test_under_two_deliveries_is_not_enough_history(ins: list[DeliveryIn]) -> None:
    r = row(ins, day=10)
    assert r.status in ("not_enough_history", "no_stock_in")
    assert r.days_to_run_out is None and r.run_out_date is None and r.est_left is None


def test_confidence_falls_every_day_and_never_rises_without_input() -> None:
    ins = rhythm()
    counts = [CountIn("wamid.3", 15, "typed", quantity=12, unit="ctn")]
    seen = [row(ins, counts, day=d).confidence for d in range(15, 40)]
    for a, b in zip(seen, seen[1:], strict=False):
        assert b <= a
        if a > 0:
            assert b < a
    assert seen[1] == pytest.approx(1.0 - CONF_DECAY_PER_DAY)


def test_every_row_is_estimated_with_policy_and_input_ids() -> None:
    for r in (row(rhythm()), row(rhythm(), [CountIn("wamid.4", 16, "tap", bucket="3-5")]),
              row([DeliveryIn("dlv-0", 3, 10, "ctn")], day=10)):
        assert r.proof_level == "estimated" and r.proof_policy_version == "v1"
        assert r.policy_version == "stock-estimates-v0.3" and r.inputs
        assert json.loads(r.witnesses)[0]["kind"] in ("estimator", "shop_count")


def test_same_inputs_same_row() -> None:
    assert row(rhythm()) == row(rhythm())
    assert row(rhythm()).inputs_hash != row(rhythm(), day=17).inputs_hash


@pytest.mark.parametrize(("qty", "unit", "base"), [
    ("1/2", "ctn", 20), (0.5, "cartons", 20), (30, "packets", 30), (2, "ctn", 80),
    (1, "piece", 1), (3, "crate", None), ("1/3", "ctn", None), (-1, "ctn", None),
])
def test_base_units(qty: Any, unit: str, base: int | None) -> None:
    assert CTN40.to_base(qty, unit) == base


def test_half_cartons_and_mixed_units_convert_and_unknown_gives_nothing() -> None:
    ins = [*rhythm(), DeliveryIn("dlv-half", 15, "1/2", "ctn")]
    r = row(ins, [CountIn("wamid.5", 16, "typed", quantity=30, unit="packets")], day=16)
    assert r.est_left == 0.75  # 30 packets of a carton of 40
    bad = row([*rhythm(), DeliveryIn("dlv-crate", 15, 1, "crate")], day=16)
    assert bad.status == "unknown_unit" and bad.est_left is None
    # a unit with no inner pack: "packets" is unknown there
    assert ONE.to_base(3, "packets") is None and ONE.to_base(3, "packs") == 3


def test_run_out_date_and_money_tied_up() -> None:
    import math

    r = row(rhythm(), day=16, cost_kobo=1_180_000)
    assert r.days_to_run_out is not None and r.est_left is not None
    expect = DAY0 + timedelta(days=16 + max(0, math.ceil(r.days_to_run_out) - 1))
    assert r.run_out_date == expect.isoformat()
    assert isinstance(r.money_tied_kobo, int)
    assert abs(r.money_tied_kobo - r.est_left * 1_180_000) <= 0.005 * 1_180_000 + 1


def test_low_item_trigger() -> None:
    soon = row(rhythm(), [CountIn("wamid.6", 16, "tap", bucket="0-2")], day=16)
    assert count_wanted(soon, lead_time_days=2, top_item=False)
    plenty = row(rhythm(), [CountIn("wamid.7", 16, "typed", quantity=40, unit="ctn")], day=16)
    assert not count_wanted(plenty, lead_time_days=2, top_item=True)
    stale = row(rhythm(), [CountIn("wamid.8", 1, "typed", quantity=40, unit="ctn")], day=16)
    assert confidence_word(0.39) == "Low" and confidence_word(0.4) == "Medium"
    assert count_wanted(stale, lead_time_days=2, top_item=True) == (
        stale.confidence_word == "Low" or (stale.days_to_run_out or 99) <= 4)
    assert not count_wanted(row([DeliveryIn("d", 1, 3, "ctn")], day=5), lead_time_days=2,
                            top_item=True)


# ------------------------------------------------------------------ the store


@pytest.fixture()
def book(tmp_path: Path):  # type: ignore[no-untyped-def]
    from mimi.stock.estimates import StockBook
    from mimi.store import ensure_schema, make_engine, seed_world
    from mimi.world.loader import load_world

    e = make_engine(f"sqlite:///{tmp_path / 'stock.db'}")
    ensure_schema(e)
    w = load_world()
    seed_world(e, w)
    b = StockBook(e)
    now = datetime.combine(w.date_of(w.live_day), time(9, 0), WAT)
    shop = w.shops[0].shop_id
    sku = sorted({ln.sku for d in w.deliveries if d.shop_id == shop for ln in d.lines})[0]
    return {"b": b, "w": w, "now": now, "shop": shop, "sku": sku, "t": w.tenant_id, "e": e}


def test_recompute_writes_once_per_input_set(book: dict[str, Any]) -> None:
    b, t, shop, now = book["b"], book["t"], book["shop"], book["now"]
    rows, n = b.recompute(t, shop, now=now)
    assert rows and n == len(rows)
    _, again = b.recompute(t, shop, now=now + timedelta(hours=3))
    assert again == 0  # same inputs, same policy: no new row
    assert all(r.proof_level == "estimated" for r in rows)


def test_count_door_overrides_and_keeps_history(book: dict[str, Any]) -> None:
    b, t, shop, sku, now = book["b"], book["t"], book["shop"], book["sku"], book["now"]
    b.recompute(t, shop, sku, now=now)
    before = b.latest(t, shop, sku)
    unit = before.unit
    res = b.submit_count(t, shop, sku, report_id="wamid.c1", at=now + timedelta(hours=1),
                         kind="typed", quantity=7, unit=unit)
    assert res.accepted and res.reason == "recorded" and res.row is not None
    assert res.row.est_left == 7.0 and res.row.anchor_source == "typed"
    hist = b.history(t, shop, sku)
    assert len(hist) == 2 and hist[0].inputs_hash == before.inputs_hash  # kept
    dup = b.submit_count(t, shop, sku, report_id="wamid.c1", at=now + timedelta(hours=1),
                         kind="typed", quantity=7, unit=unit)
    assert not dup.accepted and dup.reason == "duplicate" and len(b.history(t, shop, sku)) == 2
    tap = b.submit_count(t, shop, sku, report_id="wamid.c2", at=now + timedelta(hours=2),
                         kind="tap", bucket="0-2", after_delivery=True)
    assert tap.accepted and tap.row is not None and tap.row.anchor_range == "0-2"
    assert tap.row.confidence == CONF_BUCKET


@pytest.mark.parametrize(("kw", "reason"), [
    ({"kind": "typed", "quantity": 3, "unit": "crate"}, "unknown_unit"),
    ({"kind": "tap", "bucket": "lots"}, "bad_count"),
    ({"kind": "typed"}, "bad_count"),
    ({"kind": "guess", "quantity": 3, "unit": "ctn"}, "bad_count"),
])
def test_count_door_refuses_what_it_cannot_read(book: dict[str, Any], kw: dict[str, Any],
                                                reason: str) -> None:
    b = book["b"]
    res = b.submit_count(book["t"], book["shop"], book["sku"], report_id="wamid.bad",
                         at=book["now"], **kw)
    assert not res.accepted and res.reason == reason and res.row is None


def test_count_door_needs_a_zoned_time(book: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        book["b"].submit_count(book["t"], book["shop"], book["sku"], report_id="wamid.z",
                               at=datetime(2026, 10, 30, 9), kind="tap", bucket="3-5")


def test_bought_elsewhere_is_stock_in(book: dict[str, Any]) -> None:
    b, t, shop, sku, now = book["b"], book["t"], book["shop"], book["sku"], book["now"]
    unit = b.recompute(t, shop, sku, now=now)[0][0].unit
    c = b.submit_count(t, shop, sku, report_id="wamid.c3", at=now, kind="typed", quantity=2,
                       unit=unit)
    assert c.row is not None
    later = now + timedelta(days=1)
    b.report_bought_elsewhere(t, shop, sku, report_id="wamid.o1", at=now + timedelta(hours=2),
                              quantity=3, unit=unit)
    r, _ = b.recompute(t, shop, sku, now=later)
    assert "wamid.o1" in r[0].inputs
    assert (r[0].stock_in_since_anchor or 0) >= 3


def test_distributor_sees_flags_and_words_never_quantities(book: dict[str, Any]) -> None:
    b, t, now = book["b"], book["t"], book["now"]
    b.nightly(t, now=now)
    low = b.run_low(t)
    assert low, "the synthetic world has items running low"
    for item in low:
        assert set(item) == {"product_id", "item", "shops_likely_low", "text", "shops"}
        assert item["shops_likely_low"] == len(item["shops"])
        for s in item["shops"]:
            assert set(s) == {"shop", "shop_id", "likely_low", "when", "confidence"}
            assert s["confidence"] in ("High", "Medium", "Low")
        # the only figure in the text is the number of shops
        assert re.findall(r"\d+", item["text"].replace(item["item"], "")) == [
            str(item["shops_likely_low"])]


def test_shop_view_says_about_and_never_shows_ids(book: dict[str, Any]) -> None:
    b, t, shop, now = book["b"], book["t"], book["shop"], book["now"]
    b.recompute(t, shop, now=now)
    for it in b.shop_view(t, shop):
        assert it["product_id"] not in it["text"] and "shop-" not in it["text"]
        assert "about" in it["text"] or "not enough history" in it["text"]


def test_count_asked_at_most_once_a_week(book: dict[str, Any]) -> None:
    b, t, shop, sku, now = book["b"], book["t"], book["shop"], book["sku"], book["now"]
    b.submit_count(t, shop, sku, report_id="wamid.c4", at=now, kind="tap", bucket="0-2")
    first = b.count_wanted(t, shop, now=now, lead_time_days=2)
    assert first is not None
    assert b.count_wanted(t, shop, now=now + timedelta(days=1), lead_time_days=2) is None
    assert b.count_wanted(t, shop, now=now + timedelta(days=8), lead_time_days=2) is not None


def test_migration_creates_the_stock_tables(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    import sqlalchemy as sa

    url = f"sqlite:///{tmp_path / 'm.db'}"
    env = {**os.environ, "MIMI_DB_URL": url, "DATABASE_URL": ""}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env,
                   check=True, capture_output=True)
    names = sa.inspect(sa.create_engine(url)).get_table_names()
    assert {"stock_reports", "stock_estimates"} <= set(names)
    subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0010"], cwd=ROOT, env=env,
                   check=True, capture_output=True)
    assert "stock_estimates" not in sa.inspect(sa.create_engine(url)).get_table_names()


# ------------------------------------------------------------------ architecture


def test_credit_cannot_read_stock_estimates() -> None:
    for path in sorted((ROOT / "mimi" / "credit").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("mimi.stock", "mimi.store")), path
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("mimi.stock") for a in node.names), path
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in ("stock_estimates", "stock_reports"), path


def test_no_model_in_the_estimate_path() -> None:
    """Zero numbers produced by the model: the record path imports no model."""
    for name in ("estimator.py", "estimates.py"):
        tree = ast.parse((ROOT / "mimi" / "stock" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("mimi.models", "mimi.harness.gateway",
                                                   "mimi.stock.answers")), (name, node.module)

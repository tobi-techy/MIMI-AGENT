"""Synthetic world (RAI-260): deterministic, money in kobo, realistic shape."""

from __future__ import annotations

from collections import Counter

from mimi.world import (
    DEFAULT_SEED,
    FIXTURE_PATH,
    generate_world,
    load_world,
    stats,
    world_hash,
)
from mimi.world.catalogue import BANKS
from mimi.world.generator import FAKE_ALERTS, is_market_day

WORLD = generate_world(DEFAULT_SEED)


def test_same_seed_same_hash() -> None:
    assert world_hash(generate_world(DEFAULT_SEED)) == world_hash(WORLD)


def test_other_seed_other_world() -> None:
    assert world_hash(generate_world(DEFAULT_SEED + 1)) != world_hash(WORLD)


def test_committed_fixture_matches_the_generator() -> None:
    assert FIXTURE_PATH.exists()
    assert world_hash(load_world()) == world_hash(WORLD)


def test_stats_check_finds_no_problem() -> None:
    assert stats.check(WORLD) == []


def test_money_sums_in_kobo() -> None:
    for d in (*WORLD.deliveries, *WORLD.live_deliveries):
        assert isinstance(d.total_kobo, int)
        assert d.total_kobo == sum(x.quantity * x.unit_price_kobo for x in d.lines)
    real = [p for p in WORLD.payments if not p.fake_alert]
    billed = sum(d.total_kobo for d in WORLD.deliveries)
    paid = sum(p.amount_kobo for p in real)
    owed = sum(r.outstanding_kobo for r in WORLD.receivables)
    assert billed == paid + owed
    for r in WORLD.receivables:
        assert r.paid_kobo + r.outstanding_kobo == r.total_kobo
    feed = sum(c.amount_kobo for c in WORLD.bank_feed)
    assert feed == sum(p.amount_kobo for p in real if p.method == "transfer")


def test_shape_of_the_world() -> None:
    assert len(WORLD.shops) == 40
    assert len(WORLD.reps) == 2
    assert 28 <= len(WORLD.skus) <= 32
    sizes = Counter(s.size for s in WORLD.shops)
    assert sizes["big"] < sizes["medium"] < sizes["small"]
    assert {s.restock_days for s in WORLD.shops if s.size == "small"} == {14}
    assert all(s.phone.startswith("+2340000") for s in WORLD.shops)
    assert WORLD.distributor == "Coral Lane Trading"
    assert len(WORLD.live_deliveries) == 40


def test_payment_behaviour_is_mixed() -> None:
    real = [p for p in WORLD.payments if not p.fake_alert]
    assert any(p.method == "cash_to_rep" for p in real)
    assert any(p.split_of for p in real)
    assert any(p.partial for p in real)
    assert any(not p.kept for p in WORLD.promises)
    assert len(WORLD.disputes) == 2
    banks = {p.alert.bank for p in real if p.alert}
    assert banks <= set(BANKS)


def test_fake_alerts_are_known_and_never_paid() -> None:
    fakes = [p for p in WORLD.payments if p.fake_alert]
    assert len(fakes) == FAKE_ALERTS
    feed_ids = {c.payment_id for c in WORLD.bank_feed}
    for p in fakes:
        assert p.status == "unmatched"
        assert p.payment_id not in feed_ids
        assert p.alert is not None and "amount not in bank feed" in p.alert.telltales


def test_price_rise_mid_period() -> None:
    risen = [s for s in WORLD.skus if s.risen_price_kobo]
    assert risen
    for s in risen:
        assert 1.06 <= s.risen_price_kobo / s.price_kobo <= 1.10  # type: ignore[operator]
    before = {
        x.unit_price_kobo
        for d in WORLD.deliveries
        if d.day < WORLD.price_rise_day
        for x in d.lines
        if x.sku == risen[0].sku
    }
    assert before <= {risen[0].price_kobo}


def test_market_days_spike() -> None:
    summary = stats.summary(WORLD)
    assert summary["deliveries_per_day_market"] > summary["deliveries_per_day_other"]
    assert any(is_market_day(d, WORLD.market_day_offset) for d in range(5))


def test_aging_buckets_are_plausible() -> None:
    buckets = stats.aging(WORLD)
    total = sum(buckets.values())
    assert total == sum(r.outstanding_kobo for r in WORLD.receivables)
    assert buckets["31+ days"] < total / 2

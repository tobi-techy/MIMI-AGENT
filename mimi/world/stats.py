"""Sanity stats for a world: totals reconcile to the kobo, aging looks real.

``check(world)`` returns a list of problems (empty means the world is sound);
``summary(world)`` is the small table printed by the CLI and quoted in
``docs/WORLD.md``.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from mimi.world.generator import FAKE_ALERTS, is_market_day
from mimi.world.model import World

AGING_BUCKETS = ("current", "1-7 days", "8-30 days", "31+ days")


def aging(world: World) -> dict[str, int]:
    """Outstanding kobo by days past due, as of the last history day."""
    today = world.live_day - 1
    out = dict.fromkeys(AGING_BUCKETS, 0)
    for r in world.receivables:
        if r.outstanding_kobo <= 0:
            continue
        late = today - r.due_day
        key = (
            "current"
            if late <= 0
            else "1-7 days" if late <= 7 else "8-30 days" if late <= 30 else "31+ days"
        )
        out[key] += r.outstanding_kobo
    return out


def check(world: World) -> list[str]:
    problems: list[str] = []
    for d in (*world.deliveries, *world.live_deliveries):
        for line in d.lines:
            if line.line_total_kobo != line.quantity * line.unit_price_kobo:
                problems.append(f"{d.delivery_id} {line.sku}: line total")
        if d.total_kobo != sum(line.line_total_kobo for line in d.lines):
            problems.append(f"{d.delivery_id}: delivery total")
    money = [
        *(d.total_kobo for d in world.deliveries),
        *(p.amount_kobo for p in world.payments),
        *(r.outstanding_kobo for r in world.receivables),
    ]
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in money):
        problems.append("a money value is not integer kobo")

    real = {p.payment_id: p for p in world.payments if not p.fake_alert}
    feed_ids = {c.payment_id for c in world.bank_feed}
    for c in world.bank_feed:
        if c.payment_id not in real or real[c.payment_id].amount_kobo != c.amount_kobo:
            problems.append(f"{c.credit_id}: feed credit does not match a payment")
    for p in world.payments:
        if p.method == "transfer" and not p.fake_alert and p.payment_id not in feed_ids:
            problems.append(f"{p.payment_id}: transfer missing from the feed")
        if p.fake_alert and p.payment_id in feed_ids:
            problems.append(f"{p.payment_id}: fake alert is in the feed")

    billed = sum(d.total_kobo for d in world.deliveries)
    paid = sum(p.amount_kobo for p in real.values())
    owed = sum(r.outstanding_kobo for r in world.receivables)
    if billed != paid + owed:
        problems.append(f"billed {billed} != paid {paid} + outstanding {owed}")
    for r in world.receivables:
        if r.paid_kobo + r.outstanding_kobo != r.total_kobo:
            problems.append(f"{r.delivery_id}: receivable does not add up")
        if r.outstanding_kobo < 0:
            problems.append(f"{r.delivery_id}: overpaid")

    fakes = sum(1 for p in world.payments if p.fake_alert)
    if fakes != FAKE_ALERTS:
        problems.append(f"{fakes} fake alerts, expected {FAKE_ALERTS}")
    buckets = aging(world)
    if owed and buckets["31+ days"] > owed * 0.5:
        problems.append("more than half the book is over 30 days late")
    if owed and buckets["current"] + buckets["1-7 days"] == 0:
        problems.append("nothing recent is owed")
    return problems


def summary(world: World) -> dict[str, Any]:
    real = [p for p in world.payments if not p.fake_alert]
    days = Counter(d.day for d in world.deliveries)
    off = world.market_day_offset
    # A market-day spike: deliveries on market day or the day before.
    near = {
        day for day in days if is_market_day(day, off) or is_market_day(day + 1, off)
    }
    market = [n for day, n in days.items() if day in near]
    other = [n for day, n in days.items() if day not in near]
    return {
        "seed": world.seed,
        "shops": len(world.shops),
        "shop_sizes": dict(Counter(s.size for s in world.shops)),
        "skus": len(world.skus),
        "skus_with_price_rise": sum(1 for s in world.skus if s.risen_price_kobo),
        "deliveries": len(world.deliveries),
        "live_day_deliveries": len(world.live_deliveries),
        "deliveries_per_day_market": round(sum(market) / max(1, len(market)), 1),
        "deliveries_per_day_other": round(sum(other) / max(1, len(other)), 1),
        "payments": len(real),
        "cash_to_rep": sum(1 for p in real if p.method == "cash_to_rep"),
        "split_transfers": sum(1 for p in real if p.split_of),
        "partial_payments": sum(1 for p in real if p.partial),
        "fake_alerts": sum(1 for p in world.payments if p.fake_alert),
        "promises": len(world.promises),
        "promises_missed": sum(1 for p in world.promises if not p.kept),
        "disputes": len(world.disputes),
        "billed_kobo": sum(d.total_kobo for d in world.deliveries),
        "paid_kobo": sum(p.amount_kobo for p in real),
        "outstanding_kobo": sum(r.outstanding_kobo for r in world.receivables),
        "aging_kobo": aging(world),
    }

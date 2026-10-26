"""Deterministic synthetic world for the demo distributor, Coral Lane Trading.

``generate_world(seed)`` always returns the same world for the same seed:
every random choice comes from one ``random.Random(seed)`` in a fixed order,
and nothing reads the clock. Places are real Ado-Ekiti areas; every shop,
owner, brand and account number is invented. Phones use the fake range
``+2340000...`` (a 0 straight after the country code is never assigned in
Nigeria's numbering plan, so these can never reach anyone).

Money is integer kobo and every total is computed here, in code: a line total
is quantity x unit price, a delivery total is the sum of its lines, and a
receivable's paid and outstanding amounts come from summing matched payments.
A fake transfer alert is ``unmatched``, is absent from the bank feed and never
counts as paid. The realism rules are in ``docs/WORLD.md``.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import replace
from datetime import date, datetime, time, timedelta

from mimi.world import catalogue as cat
from mimi.world.alerts import fake_alert, genuine_alert
from mimi.world.model import (
    ChatTrait,
    Delivery,
    DeliveryLine,
    Dispute,
    FeedCredit,
    Payment,
    PaysTrait,
    Promise,
    Receivable,
    ReceivableStatus,
    Rep,
    Shop,
    ShopSize,
    Shortage,
    Sku,
    World,
)

DEFAULT_SEED = 2610
DISTRIBUTOR = "Coral Lane Trading"
TENANT_ID = "coral-lane"
START_DATE = "2026-08-31"  # a Monday; 8 weeks later the live day is 26 Oct 2026
WEEKS = 8
CREDIT_DAYS = 7
PRICE_RISE_DAY = 28  # mid-period: about 8% up on some SKUs from 28 Sep 2026
PRICE_RISE_PCT = 8
MARKET_CYCLE = 5  # Ado market comes round every 5 days
FAKE_ALERTS = 4
HISTORIC_DISPUTES = 2
LIVE_SHORTAGES = 3
# A few big buyers, a middle, and a long tail of small shops (40 in all).
SIZES: tuple[tuple[ShopSize, int], ...] = (("big", 5), ("medium", 11), ("small", 24))
_QTY: dict[ShopSize, tuple[int, int]] = {
    "big": (4, 14),
    "medium": (2, 7),
    "small": (1, 3),
}
_LINES: dict[ShopSize, tuple[int, int]] = {
    "big": (5, 8),
    "medium": (3, 6),
    "small": (2, 4),
}
_CREDIT_NAIRA: dict[ShopSize, tuple[int, int]] = {
    "big": (1_500_000, 3_000_000),
    "medium": (400_000, 1_000_000),
    "small": (60_000, 250_000),
}


def _kobo(naira: int) -> int:
    return naira * 100


def _phone(n: int) -> str:
    return f"+2340000{n:06d}"


def _iso(day: int) -> str:
    return (date.fromisoformat(START_DATE) + timedelta(days=day)).isoformat()


def _at(day: int, minute: int) -> datetime:
    d = date.fromisoformat(START_DATE) + timedelta(days=day)
    return datetime.combine(d, time(minute // 60, minute % 60, (day * 7 + minute) % 60))


def _round_down(kobo: int, step_naira: int) -> int:
    step = _kobo(step_naira)
    return kobo // step * step


def _catalogue() -> tuple[Sku, ...]:
    out = []
    for sku, name, brand, unit, naira, _bulk, rises in cat.CATALOGUE:
        price = _kobo(naira)
        risen = None
        if rises:
            # +8%, rounded to the nearest N50 the way price lists are.
            risen = round(price * (100 + PRICE_RISE_PCT) / 100 / 5_000) * 5_000
        out.append(Sku(sku, name, brand, unit, price, risen))
    return tuple(out)


def _bulk(sku: str) -> bool:
    return next(row[5] for row in cat.CATALOGUE if row[0] == sku)


def _shops(rng: random.Random, reps: tuple[Rep, ...]) -> tuple[Shop, ...]:
    shops: list[Shop] = []
    used: set[str] = set()
    pays_pool: tuple[PaysTrait, ...] = (
        ("on_time",) * 5 + ("late",) * 3 + ("disputes",) * 2
    )
    i = 0
    for size, count in SIZES:
        for _ in range(count):
            shop_type = rng.choice(cat.SHOP_TYPES_BY_SIZE[size])
            while True:
                first = rng.choice(
                    cat.YORUBA_FIRST if rng.random() < 0.85 else cat.OTHER_FIRST
                )
                last = rng.choice(cat.SURNAMES)
                name = rng.choice(cat.SHOP_STYLES[shop_type]).format(
                    first=first, last=last
                )
                if name not in used:
                    used.add(name)
                    break
            area, streets = cat.AREAS[i % len(cat.AREAS)]
            pays = pays_pool[rng.randrange(len(pays_pool))]
            chat: ChatTrait = "a_lot" if rng.random() < 0.4 else "a_little"
            # Small shops lean on cash to the rep; bigger ones transfer.
            cash = rng.random() < {"big": 0.1, "medium": 0.3, "small": 0.5}[size]
            lo, hi = _CREDIT_NAIRA[size]
            shops.append(
                Shop(
                    shop_id=f"shop-{i + 1:03d}",
                    name=name,
                    shop_type=shop_type,  # type: ignore[arg-type]
                    size=size,
                    owner=f"{first} {last}",
                    area=area,
                    street=f"{rng.randint(1, 48)} {rng.choice(streets)}",
                    phone=_phone(100 + i),
                    rep_id=reps[i % len(reps)].rep_id,
                    credit_limit_kobo=_round_down(_kobo(rng.randint(lo, hi)), 10_000),
                    pays=pays,
                    chat=chat,
                    bank="" if cash else rng.choice(cat.BANKS),
                    restock_days=14 if size == "small" else 7,
                )
            )
            i += 1
    return tuple(shops)


def is_market_day(day: int, offset: int) -> bool:
    return (day - offset) % MARKET_CYCLE == 0


def is_month_end(day: int) -> bool:
    d = date.fromisoformat(START_DATE) + timedelta(days=day)
    return d.day >= 25 or d.day <= 2


def _lines(
    rng: random.Random, skus: tuple[Sku, ...], shop: Shop, day: int, boost: float
) -> tuple[DeliveryLine, ...]:
    pool = [s for s in skus if shop.size != "small" or not _bulk(s.sku)]
    lo, hi = _LINES[shop.size]
    picked = rng.sample(pool, min(len(pool), rng.randint(lo, hi)))
    qlo, qhi = _QTY[shop.size]
    lines = []
    for sku in sorted(picked, key=lambda s: s.sku):
        base = rng.randint(1, 2) if _bulk(sku.sku) else rng.randint(qlo, qhi)
        qty = max(1, math.ceil(base * boost))
        price = sku.price_on(day, PRICE_RISE_DAY)
        lines.append(DeliveryLine(sku.sku, sku.name, sku.unit, qty, price, qty * price))
    return tuple(lines)


def _make_delivery(
    rng: random.Random,
    skus: tuple[Sku, ...],
    shop: Shop,
    day: int,
    seq: int,
    offset: int,
) -> Delivery:
    market = is_market_day(day, offset) or is_market_day(day + 1, offset)
    month_end = is_month_end(day)
    boost = (1.5 if market else 1.0) * (1.2 if month_end else 1.0)
    lines = _lines(rng, skus, shop, day, boost)
    return Delivery(
        delivery_id=f"dlv-{day:02d}-{seq:04d}",
        shop_id=shop.shop_id,
        rep_id=shop.rep_id,
        day=day,
        delivered_on=_iso(day),
        minute=rng.randint(8 * 60, 17 * 60),
        lines=lines,
        total_kobo=sum(line.line_total_kobo for line in lines),
        market_day=market,
        month_end=month_end,
    )


def _deliveries(
    rng: random.Random, skus: tuple[Sku, ...], shops: tuple[Shop, ...], offset: int
) -> list[Delivery]:
    horizon = WEEKS * 7
    out: list[Delivery] = []
    seq = 0
    for shop in shops:
        phase = rng.randrange(min(shop.restock_days, 6))
        days = set(range(phase, horizon, shop.restock_days))
        # Market-day top-ups: big and medium shops restock before market.
        for day in range(horizon):
            if is_market_day(day + 1, offset) and day % 7 != 6:
                p = {"big": 0.6, "medium": 0.3, "small": 0.08}[shop.size]
                if rng.random() < p:
                    days.add(day)
        for day in sorted(days):
            if day % 7 == 6:  # no Sunday deliveries
                day -= 1
            seq += 1
            out.append(_make_delivery(rng, skus, shop, day, seq, offset))
    out.sort(key=lambda d: (d.day, d.minute, d.delivery_id))
    return out


class _Payments:
    """Builds payments, the bank feed, promises and disputes in one pass."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.n = 0
        self.payments: list[Payment] = []
        self.feed: list[FeedCredit] = []
        self.promises: list[Promise] = []
        self.disputes: list[Dispute] = []

    def pay(self, shop: Shop, d: Delivery, day: int, amount: int, **kw: object) -> None:
        if amount <= 0 or day >= WEEKS * 7:
            return
        self.n += 1
        pid = f"pay-{self.n:05d}"
        minute = self.rng.randint(7 * 60, 21 * 60)
        if not shop.bank:
            self.payments.append(
                Payment(
                    payment_id=pid,
                    shop_id=shop.shop_id,
                    delivery_id=d.delivery_id,
                    day=day,
                    paid_on=_iso(day),
                    amount_kobo=amount,
                    method="cash_to_rep",
                    reference=f"REP-RCPT-{self.n:05d}",
                    status="matched",
                    **kw,  # type: ignore[arg-type]
                )
            )
            return
        ref = f"CLT-{self.n:05d}"
        at = _at(day, minute)
        alert = genuine_alert(
            self.rng,
            bank=shop.bank,
            sender=shop.owner,
            amount_kobo=amount,
            reference=ref,
            at=at,
        )
        self.payments.append(
            Payment(
                payment_id=pid,
                shop_id=shop.shop_id,
                delivery_id=d.delivery_id,
                day=day,
                paid_on=_iso(day),
                amount_kobo=amount,
                method="transfer",
                reference=ref,
                status="matched",
                alert=alert,
                **kw,  # type: ignore[arg-type]
            )
        )
        self.feed.append(
            FeedCredit(
                credit_id=f"feed-{self.n:05d}",
                payment_id=pid,
                amount_kobo=amount,
                received_at=(
                    at + timedelta(seconds=self.rng.randint(4, 90))
                ).isoformat(),
                narration=f"NIP/{shop.bank.upper()}/{shop.owner.upper()}/{ref}",
            )
        )

    def fake(self, shop: Shop, d: Delivery, day: int, amount: int) -> None:
        self.n += 1
        at = _at(day, self.rng.randint(9 * 60, 20 * 60))
        self.payments.append(
            Payment(
                payment_id=f"pay-{self.n:05d}",
                shop_id=shop.shop_id,
                delivery_id=d.delivery_id,
                day=day,
                paid_on=_iso(day),
                amount_kobo=amount,
                method="transfer",
                reference="",
                status="unmatched",
                alert=fake_alert(
                    self.rng,
                    bank=shop.bank,
                    sender=shop.owner,
                    amount_kobo=amount,
                    at=at,
                ),
                fake_alert=True,
            )
        )

    def settle(self, shop: Shop, d: Delivery) -> None:
        rng = self.rng
        due = d.day + CREDIT_DAYS
        total = d.total_kobo
        if shop.pays == "on_time":
            day = d.day + rng.randint(0, 3)
            if shop.bank and total > _kobo(40_000) and rng.random() < 0.25:
                # One payment split across two transfers (app daily limits).
                first = _round_down(total * rng.choice((40, 50, 60)) // 100, 1_000)
                group = f"split-{d.delivery_id}"
                self.pay(shop, d, day, first, split_of=group)
                self.pay(shop, d, day, total - first, split_of=group)
            else:
                self.pay(shop, d, day, total)
            return
        if shop.pays == "late":
            promised = due + 1 + rng.randint(1, 4)
            kept = rng.random() < 0.35
            if due + 1 < WEEKS * 7:
                weekday = (
                    date.fromisoformat(START_DATE) + timedelta(days=promised)
                ).strftime("%A")
                self.promises.append(
                    Promise(
                        shop.shop_id,
                        d.delivery_id,
                        due + 1,
                        promised,
                        kept,
                        f"I will pay on {weekday}",
                    )
                )
            day = promised if kept else promised + rng.randint(2, 8)
            # Round numbers: 50,000 against a 53,450 bill.
            first = _round_down(total, 5_000) if total > _kobo(10_000) else total
            self.pay(shop, d, day, first, partial=first < total)
            if first < total and rng.random() < 0.6:
                self.pay(shop, d, day + rng.randint(4, 12), total - first)
            return
        # disputes: round-number part payment, argues about the rest.
        part = _round_down(total * rng.choice((70, 80, 90)) // 100, 5_000)
        self.pay(shop, d, d.day + rng.randint(2, 9), part or total, partial=bool(part))


def _receivable(d: Delivery, paid: int, today: int) -> Receivable:
    due = d.day + CREDIT_DAYS
    outstanding = d.total_kobo - paid
    status: ReceivableStatus
    if outstanding <= 0:
        status = "settled"
    elif today > due:
        status = "overdue"
    else:
        status = "open"
    return Receivable(
        d.delivery_id,
        d.shop_id,
        due,
        _iso(due),
        d.total_kobo,
        paid,
        outstanding,
        status,
    )


def _shortage(rng: random.Random, d: Delivery) -> Shortage:
    line = max(d.lines, key=lambda x: (x.quantity, x.sku))
    missing = min(line.quantity, rng.choice((1, 2, 2, 3)))
    return Shortage(line.sku, line.name, line.unit, missing)


def generate_world(seed: int = DEFAULT_SEED) -> World:
    rng = random.Random(seed)
    skus = _catalogue()
    offset = rng.randrange(MARKET_CYCLE)
    reps = (
        Rep("rep-1", cat.REP_NAMES[0], _phone(1), tuple(a for a, _ in cat.AREAS[:5])),
        Rep("rep-2", cat.REP_NAMES[1], _phone(2), tuple(a for a, _ in cat.AREAS[5:])),
    )
    shops = _shops(rng, reps)
    by_shop = {s.shop_id: s for s in shops}
    horizon = WEEKS * 7
    deliveries = _deliveries(rng, skus, shops, offset)

    book = _Payments(rng)
    for d in deliveries:
        book.settle(by_shop[d.shop_id], d)

    # Exactly FAKE_ALERTS forged alerts, from transfer shops that run late or
    # dispute, against deliveries still owing.
    owing = [
        d
        for d in deliveries
        if by_shop[d.shop_id].bank
        and by_shop[d.shop_id].pays != "on_time"
        and d.day < horizon - 3
    ]
    for d in rng.sample(owing, FAKE_ALERTS):
        book.fake(
            by_shop[d.shop_id],
            d,
            d.day + rng.randint(1, 3),
            _round_down(d.total_kobo, 5_000) or d.total_kobo,
        )

    # A couple of historic disputes: "I didn't collect the 2 cartons".
    disputers = [d for d in deliveries if by_shop[d.shop_id].pays == "disputes"]
    for d in rng.sample(disputers, HISTORIC_DISPUTES):
        s = _shortage(rng, d)
        unit = {"ctn": "cartons", "bag": "bags", "pk": "packs"}.get(s.unit, s.unit)
        book.disputes.append(
            Dispute(
                d.shop_id,
                d.delivery_id,
                d.day + 1,
                s.sku,
                s.missing,
                f"I didn't collect the {s.missing} {unit}",
                "open",
            )
        )

    paid: dict[str, int] = {}
    for p in book.payments:
        if p.status == "matched" and not p.fake_alert:
            paid[p.delivery_id] = paid.get(p.delivery_id, 0) + p.amount_kobo
    receivables = tuple(
        _receivable(d, paid.get(d.delivery_id, 0), horizon - 1) for d in deliveries
    )

    live = [
        _make_delivery(rng, skus, shop, horizon, 9000 + i, offset)
        for i, shop in enumerate(shops)
    ]
    short = rng.sample(
        [d for d in live if by_shop[d.shop_id].pays == "disputes"], LIVE_SHORTAGES
    )
    short_ids = {d.delivery_id for d in short}
    live = [
        replace(d, shortage=_shortage(rng, d)) if d.delivery_id in short_ids else d
        for d in live
    ]
    live.sort(key=lambda d: (d.minute, d.delivery_id))

    return World(
        seed=seed,
        distributor=DISTRIBUTOR,
        tenant_id=TENANT_ID,
        start_date=START_DATE,
        weeks=WEEKS,
        price_rise_day=PRICE_RISE_DAY,
        market_day_offset=offset,
        skus=skus,
        reps=reps,
        shops=shops,
        deliveries=tuple(deliveries),
        payments=tuple(sorted(book.payments, key=lambda p: (p.day, p.payment_id))),
        bank_feed=tuple(book.feed),
        promises=tuple(book.promises),
        disputes=tuple(book.disputes),
        receivables=receivables,
        live_deliveries=tuple(live),
    )


def canonical_json(world: World) -> str:
    return json.dumps(world.to_dict(), sort_keys=True, separators=(",", ":"))


def world_hash(world: World) -> str:
    return hashlib.sha256(canonical_json(world).encode("utf-8")).hexdigest()

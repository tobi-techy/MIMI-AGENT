"""The synthetic world's records and their JSON form.

All money is integer kobo. Quantities are whole units of the SKU's pack
(cartons, bags, packs). Days are offsets from the world's start date; day
``weeks * 7`` is the live day the simulator runs. Times are WAT.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

PaysTrait = Literal["on_time", "late", "disputes"]
ChatTrait = Literal["a_lot", "a_little"]
ShopSize = Literal["big", "medium", "small"]
ShopType = Literal[
    "provision store", "kiosk", "mini-mart", "mama-put supplier", "POS-plus-provisions"
]
PaymentMethod = Literal["transfer", "cash_to_rep"]
# The ports' payment vocabulary. A payment is never real without a match.
PaymentStatus = Literal["reported", "matched", "confirmed", "unmatched", "needs_review"]
ReceivableStatus = Literal["settled", "open", "overdue"]


@dataclass(frozen=True)
class Sku:
    sku: str
    name: str
    brand: str
    unit: str
    price_kobo: int
    # About 8% up from ``rise_day`` on some SKUs; None when the price held.
    risen_price_kobo: int | None = None

    def price_on(self, day: int, rise_day: int) -> int:
        if self.risen_price_kobo is not None and day >= rise_day:
            return self.risen_price_kobo
        return self.price_kobo


@dataclass(frozen=True)
class Rep:
    rep_id: str
    name: str
    phone: str
    areas: tuple[str, ...]


@dataclass(frozen=True)
class Shop:
    shop_id: str
    name: str
    shop_type: ShopType
    size: ShopSize
    owner: str
    area: str
    street: str
    phone: str
    rep_id: str
    credit_limit_kobo: int
    pays: PaysTrait
    chat: ChatTrait
    # Where the shop's transfers come from, or "" for a cash-to-rep shop.
    bank: str
    # Restock cadence in days: 7 (weekly) or 14 (biweekly).
    restock_days: int


@dataclass(frozen=True)
class DeliveryLine:
    sku: str
    name: str
    unit: str
    quantity: int
    unit_price_kobo: int
    line_total_kobo: int


@dataclass(frozen=True)
class Shortage:
    """Ground truth for the simulator: what did not arrive. Mimi never sees it."""

    sku: str
    name: str
    unit: str
    missing: int


@dataclass(frozen=True)
class Delivery:
    delivery_id: str
    shop_id: str
    rep_id: str
    day: int
    delivered_on: str
    # Minutes after midnight, WAT.
    minute: int
    lines: tuple[DeliveryLine, ...]
    total_kobo: int
    market_day: bool = False
    month_end: bool = False
    shortage: Shortage | None = None


@dataclass(frozen=True)
class BankAlert:
    """The alert text a shop forwards, in a bank's usual layout."""

    bank: str
    text: str
    sent_at: str  # ISO, WAT
    # Telltale signs on a fake alert ("narration format", "not in bank feed",
    # "odd timestamp"). Empty on a genuine one.
    telltales: tuple[str, ...] = ()


@dataclass(frozen=True)
class Payment:
    payment_id: str
    shop_id: str
    delivery_id: str
    day: int
    paid_on: str
    amount_kobo: int
    method: PaymentMethod
    reference: str
    status: PaymentStatus
    partial: bool = False
    # One of two transfers that together make one payment.
    split_of: str | None = None
    alert: BankAlert | None = None
    # A fake transfer alert: the shop sent a screenshot, no money arrived.
    # It never counts toward what was paid and is absent from the bank feed.
    fake_alert: bool = False


@dataclass(frozen=True)
class FeedCredit:
    """One credit in Coral Lane's own bank feed (the source of truth)."""

    credit_id: str
    payment_id: str
    amount_kobo: int
    received_at: str
    narration: str


@dataclass(frozen=True)
class Promise:
    """A late payer's "I will pay on Friday"."""

    shop_id: str
    delivery_id: str
    made_day: int
    promised_day: int
    kept: bool
    text: str


@dataclass(frozen=True)
class Dispute:
    shop_id: str
    delivery_id: str
    day: int
    sku: str
    missing: int
    text: str
    status: Literal["open", "resolved"]


@dataclass(frozen=True)
class Receivable:
    delivery_id: str
    shop_id: str
    due_day: int
    due_on: str
    total_kobo: int
    paid_kobo: int
    outstanding_kobo: int
    status: ReceivableStatus


@dataclass(frozen=True)
class World:
    seed: int
    distributor: str
    tenant_id: str
    start_date: str
    weeks: int
    price_rise_day: int
    market_day_offset: int
    skus: tuple[Sku, ...]
    reps: tuple[Rep, ...]
    shops: tuple[Shop, ...]
    deliveries: tuple[Delivery, ...]
    payments: tuple[Payment, ...]
    bank_feed: tuple[FeedCredit, ...]
    promises: tuple[Promise, ...]
    disputes: tuple[Dispute, ...]
    receivables: tuple[Receivable, ...]
    # Deliveries logged on the live day (``weeks * 7``) for the simulator.
    live_deliveries: tuple[Delivery, ...] = field(default=())

    @property
    def live_day(self) -> int:
        return self.weeks * 7

    def date_of(self, day: int) -> date:
        return date.fromisoformat(self.start_date) + timedelta(days=day)

    def shop(self, shop_id: str) -> Shop:
        for shop in self.shops:
            if shop.shop_id == shop_id:
                return shop
        raise KeyError(shop_id)

    def delivery(self, delivery_id: str) -> Delivery:
        for d in (*self.deliveries, *self.live_deliveries):
            if d.delivery_id == delivery_id:
                return d
        raise KeyError(delivery_id)

    def outstanding_kobo(self, shop_id: str) -> int:
        return sum(r.outstanding_kobo for r in self.receivables if r.shop_id == shop_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _delivery(d: dict[str, Any]) -> Delivery:
    shortage = d.get("shortage")
    return Delivery(
        **{
            **d,
            "lines": tuple(DeliveryLine(**line) for line in d["lines"]),
            "shortage": Shortage(**shortage) if shortage else None,
        }
    )


def _payment(p: dict[str, Any]) -> Payment:
    alert = p.get("alert")
    return Payment(
        **{
            **p,
            "alert": (
                BankAlert(**{**alert, "telltales": tuple(alert["telltales"])})
                if alert
                else None
            ),
        }
    )


def world_from_dict(raw: dict[str, Any]) -> World:
    return World(
        seed=int(raw["seed"]),
        distributor=raw["distributor"],
        tenant_id=raw["tenant_id"],
        start_date=raw["start_date"],
        weeks=int(raw["weeks"]),
        price_rise_day=int(raw["price_rise_day"]),
        market_day_offset=int(raw["market_day_offset"]),
        skus=tuple(Sku(**s) for s in raw["skus"]),
        reps=tuple(Rep(**{**r, "areas": tuple(r["areas"])}) for r in raw["reps"]),
        shops=tuple(Shop(**s) for s in raw["shops"]),
        deliveries=tuple(_delivery(d) for d in raw["deliveries"]),
        payments=tuple(_payment(p) for p in raw["payments"]),
        bank_feed=tuple(FeedCredit(**c) for c in raw["bank_feed"]),
        promises=tuple(Promise(**p) for p in raw["promises"]),
        disputes=tuple(Dispute(**d) for d in raw["disputes"]),
        receivables=tuple(Receivable(**r) for r in raw["receivables"]),
        live_deliveries=tuple(_delivery(d) for d in raw.get("live_deliveries", [])),
    )

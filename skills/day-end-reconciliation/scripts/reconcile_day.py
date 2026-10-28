"""One rep's day in code: stock loaded minus returned against drops the shops confirmed, and cash declared plus verified transfers against what shops confirmed paying. Differences over the tolerance are gaps; unconfirmed and disputed drops are listed apart, never counted as gaps yet. Draft: its money inputs must come from Go reads, never from the model."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

FIGURES_FROM = "book"  # sums of book money: fed by code from Go, never by the model

DEFAULT_TOLERANCE_KOBO = 100_000  # N1,000
DEFAULT_TOLERANCE_UNITS = 1  # 1 carton


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Drop(_M):
    shop_id: str
    sku: str
    quantity: int = Field(ge=0)
    status: str = Field(pattern="^(confirmed|unconfirmed|disputed)$")
    shop_says_paid_kobo: int = Field(0, ge=0)


class In(_M):
    rep_id: str
    loaded: dict[str, int]  # sku -> units
    returned: dict[str, int] = Field(default_factory=dict)
    drops: list[Drop] = Field(max_length=2_000)
    cash_declared_kobo: int = Field(0, ge=0)
    transfers_verified_kobo: int = Field(0, ge=0)
    tolerance_kobo: int = Field(DEFAULT_TOLERANCE_KOBO, ge=0)
    tolerance_units: int = Field(DEFAULT_TOLERANCE_UNITS, ge=0)


class StockGap(_M):
    sku: str
    expected_dropped: int
    confirmed: int
    difference: int


class Out(_M):
    rep_id: str
    stock_gaps: list[StockGap]
    money_expected_kobo: int
    money_in_kobo: int
    money_gap_kobo: int
    money_gap: bool
    unconfirmed: list[str]
    disputed: list[str]
    all_clear: bool


def run(args: In, now: datetime) -> Out:
    confirmed: dict[str, int] = {}
    for d in args.drops:
        if d.status == "confirmed":
            confirmed[d.sku] = confirmed.get(d.sku, 0) + d.quantity
    # Units on unconfirmed or disputed drops are not gaps yet.
    pending: dict[str, int] = {}
    for d in args.drops:
        if d.status != "confirmed":
            pending[d.sku] = pending.get(d.sku, 0) + d.quantity
    gaps = []
    for sku in sorted(set(args.loaded) | set(confirmed)):
        expected = args.loaded.get(sku, 0) - args.returned.get(sku, 0)
        diff = expected - confirmed.get(sku, 0) - pending.get(sku, 0)
        if abs(diff) > args.tolerance_units:
            gaps.append(StockGap(sku=sku, expected_dropped=expected,
                                 confirmed=confirmed.get(sku, 0), difference=diff))
    expected_kobo = sum(d.shop_says_paid_kobo for d in args.drops if d.status == "confirmed")
    in_kobo = args.cash_declared_kobo + args.transfers_verified_kobo
    gap = expected_kobo - in_kobo
    money_gap = abs(gap) > args.tolerance_kobo
    unconfirmed = sorted({d.shop_id for d in args.drops if d.status == "unconfirmed"})
    disputed = sorted({d.shop_id for d in args.drops if d.status == "disputed"})
    return Out(rep_id=args.rep_id, stock_gaps=gaps, money_expected_kobo=expected_kobo,
               money_in_kobo=in_kobo, money_gap_kobo=gap, money_gap=money_gap,
               unconfirmed=unconfirmed, disputed=disputed,
               all_clear=not gaps and not money_gap)

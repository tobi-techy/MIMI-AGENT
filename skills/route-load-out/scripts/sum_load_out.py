"""Add up today's orders on one route by sku into the van load-out list, and order the visit list: shops with orders first, then shops that owe, then shops likely low, then shops not visited in 7 days. Quantities only; no money. Draft: wired when the route tools exist."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

FIGURES_FROM = "none"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderLine(_M):
    shop_id: str
    sku: str
    quantity: int = Field(ge=1, le=10_000)
    unit: str


class RouteShop(_M):
    shop_id: str
    owes: bool = False
    likely_low: bool = False
    days_since_visit: int = Field(0, ge=0)


class In(_M):
    route: str
    orders: list[OrderLine] = Field(max_length=2_000)
    shops: list[RouteShop] = Field(max_length=500)
    buffer: dict[str, int] = Field(default_factory=dict)  # sku -> extra units (addendum only)


class LoadRow(_M):
    sku: str
    unit: str
    quantity: int
    buffer: int


class Out(_M):
    route: str
    load: list[LoadRow]
    visits: list[str]
    reasons: dict[str, list[str]]


def run(args: In, now: datetime) -> Out:
    totals: dict[tuple[str, str], int] = {}
    for ln in args.orders:
        totals[(ln.sku, ln.unit)] = totals.get((ln.sku, ln.unit), 0) + ln.quantity
    load = [LoadRow(sku=s, unit=u, quantity=q, buffer=max(0, args.buffer.get(s, 0)))
            for (s, u), q in sorted(totals.items())]
    ordered = {ln.shop_id for ln in args.orders}
    reasons: dict[str, list[str]] = {}
    for shop in args.shops:
        r = []
        if shop.shop_id in ordered:
            r.append("order")
        if shop.owes:
            r.append("owes")
        if shop.likely_low:
            r.append("likely low")
        if shop.days_since_visit >= 7:
            r.append("not visited in 7 days")
        if r:
            reasons[shop.shop_id] = r
    rank = {"order": 0, "owes": 1, "likely low": 2, "not visited in 7 days": 3}
    pos = {s.shop_id: i for i, s in enumerate(args.shops)}
    visits = sorted(reasons, key=lambda s: (min(rank[x] for x in reasons[s]), pos[s]))
    return Out(route=args.route, load=load, visits=visits, reasons=reasons)

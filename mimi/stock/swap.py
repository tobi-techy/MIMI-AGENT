"""The swap, promo or pickup draft for slow stock: quantities and values in code.

From a ``SlowAssessment`` (``slow.py``) code works out up to three options for
the distributor, never acted on until the distributor approves:

* **move**: take the units to a nearby shop (same area, else the same rep's
  round) that stocks the line, sells it faster (at least ``FASTER`` times the
  rate) and can sell the extra before it expires (its own rate over the days
  left, less what it already holds). The rep moves it on the van.
* **promo**: push it where it is (front of shop, a bundle). The distributor
  decides any discount; Mimi suggests none.
* **pickup**: collect it early while it can still be resold elsewhere (at
  least ``RESALE_MIN_DAYS`` before it must leave a shelf). Not offered once
  that window has passed: then it is a promo or a loss, and the draft says so.

Values are the units times the catalogue price on the day, in integer kobo.
``verify`` checks every figure before a draft is proposed; a draft that fails
is not proposed. The draft id is fixed per shop, item and ISO week, so running
the check twice opens one draft (idempotent). The shop is told only after the
distributor approves, in plain words with no internal ids.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from mimi.stock.estimator import StockEstimate
from mimi.stock.intents import SlowStockDraftForApproval, SwapAction, SwapOption
from mimi.stock.items import display_name, unit_word
from mimi.stock.slow import SAFETY_DAYS, SLOW_POLICY_VERSION, SlowAssessment
from mimi.types import ShopScope
from mimi.world.model import Sku

FASTER = 1.5
RESALE_MIN_DAYS = 21
PROMO_MIN_DAYS = 7
# With no expiry known (overstock), a moved unit should sell within this.
MOVE_WINDOW_DAYS = 28


@dataclass(frozen=True)
class Peer:
    """Another shop that stocks the line, with its estimate for it."""

    shop_id: str
    name: str
    area: str
    rep_id: str
    est: StockEstimate


def draft_id_for(shop_id: str, sku: str, week: str) -> str:
    return f"slow-{shop_id}-{sku}-{week}"


def sell_window(a: SlowAssessment) -> float:
    if a.days_to_expiry is None:
        return float(MOVE_WINDOW_DAYS)
    return float(max(0, a.days_to_expiry - SAFETY_DAYS))


def resellable(a: SlowAssessment) -> bool:
    return a.days_to_expiry is None or a.days_to_expiry - SAFETY_DAYS >= RESALE_MIN_DAYS


def move_capacity(a: SlowAssessment, peer: StockEstimate) -> int:
    """Units the peer could sell on top of its own before the window ends."""
    if peer.sell_rate_per_day <= 0:
        return 0
    return max(0, int(math.floor(peer.sell_rate_per_day * sell_window(a)
                                 - peer.estimated_units_left)))


def best_peer(a: SlowAssessment, area: str, rep_id: str,
              peers: Sequence[Peer]) -> tuple[Peer, int] | None:
    """The nearby shop that can take the most (same area before same rep)."""
    best: tuple[tuple[int, int, str], Peer, int] | None = None
    for p in peers:
        if p.shop_id == a.shop_id or p.est.sku != a.sku:
            continue
        near = 0 if p.area == area else 1 if p.rep_id == rep_id else None
        if near is None:
            continue
        if p.est.sell_rate_per_day < FASTER * max(a.sell_rate_per_day, 1e-9):
            continue
        cap = move_capacity(a, p.est)
        if cap < 1:
            continue
        key = (near, -cap, p.shop_id)
        if best is None or key < best[0]:
            best = (key, p, cap)
    return (best[1], best[2]) if best else None


def plan(a: SlowAssessment, *, area: str, rep_id: str, peers: Sequence[Peer], sku: Sku,
         day: int, rise_day: int, tenant_id: str, week: str, as_of: str,
         expires_on: str) -> SlowStockDraftForApproval | None:
    """The draft, or None when there is nothing worth proposing."""
    price = sku.price_on(day, rise_day)
    qty = a.excess_units
    if qty < 1:
        return None
    options: list[SwapOption] = []
    found = best_peer(a, area, rep_id, peers)
    if found is not None:
        peer, cap = found
        q = min(qty, cap)
        where = "same area" if peer.area == area else "same rep's round"
        options.append(SwapOption("move", q, price, q * price, peer.shop_id, peer.name,
                                  f"{where}, sells it faster"))
    if resellable(a):
        options.append(SwapOption("pickup", qty, price, qty * price,
                                  why="still good to resell elsewhere"))
    if a.days_to_expiry is None or a.days_to_expiry >= PROMO_MIN_DAYS:
        options.append(SwapOption("promo", qty, price, qty * price,
                                  why="push it at the shop; you decide any discount"))
    if not options:
        return None
    rec: SwapAction = options[0].action
    return SlowStockDraftForApproval(
        ShopScope(tenant_id, a.shop_id), draft_id_for(a.shop_id, a.sku, week), a.sku, a.item,
        a.unit, a.kind, tuple(options), rec, expires_on, a.confidence, SLOW_POLICY_VERSION)


def verify(d: SlowStockDraftForApproval, a: SlowAssessment, *, sku: Sku, day: int,
           rise_day: int, nearby: set[str]) -> list[str]:
    """Every problem with a draft; empty means it may be proposed."""
    problems: list[str] = []
    want = sku.price_on(day, rise_day)
    seen: set[str] = set()
    if not d.options:
        problems.append("options: none")
    for o in d.options:
        if o.action in seen:
            problems.append(f"{o.action}: offered twice")
        seen.add(o.action)
        if not isinstance(o.quantity, int) or isinstance(o.quantity, bool) or o.quantity < 1:
            problems.append(f"{o.action}: quantity not a whole number of at least 1")
            continue
        if o.quantity > a.units_left:
            problems.append(f"{o.action}: {o.quantity} is more than the {a.units_left} left")
        if o.unit_price_kobo != want:
            problems.append(f"{o.action}: price {o.unit_price_kobo} is not the catalogue {want}")
        if o.value_kobo != o.quantity * o.unit_price_kobo:
            problems.append(f"{o.action}: value is not quantity x unit price")
        if o.action == "move" and (o.to_shop_id == d.scope.shop_id or o.to_shop_id not in nearby):
            problems.append("move: the receiving shop is not a nearby shop")
        if o.action == "pickup" and not resellable(a):
            problems.append("pickup: too close to expiry to resell")
    if d.recommended not in seen:
        problems.append("recommended: not one of the options")
    if d.moves_stock or d.moves_money or d.status != "draft":
        problems.append("draft: must not move stock or money")
    return problems


# ------------------------------------------------------------ copy


def _date(iso: str) -> str:
    from datetime import date

    if not iso:
        return ""
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b')}"


def reason_text(a: SlowAssessment, expires_on: str) -> str:
    """Why, for the owner: plain words, an 'about' on every estimate."""
    n = a.excess_units
    words = f"about {n} {unit_word(a.unit, n)}"
    if a.kind == "near_expiry":
        return (f"{words} of {display_name(a.item)} may not sell before "
                f"{'it expires' if n == 1 else 'they expire'}"
                + (f" on {_date(expires_on)}" if expires_on else "") + ".")
    weeks = int((a.days_of_cover or 0) // 7)
    return f"{words} of {display_name(a.item)} more than the shop usually needs (about {weeks} weeks of stock)."


def option_text(o: SwapOption, naira: str) -> str:
    n = o.quantity
    if o.action == "move":
        return f"move {n} to {o.to_shop_name} ({o.why}), worth {naira}"
    if o.action == "pickup":
        return f"collect {n} early while still good to resell, worth {naira}"
    return f"promo push at the shop for {n}, worth {naira}"


def shop_text(o: SwapOption, item: str, unit: str, distributor: str, expires_on: str) -> str:
    """What the shop is told once the distributor approves. Plain, no ids."""
    n = o.quantity
    what = f"{n} {unit_word(unit, n)} of {display_name(item)}"
    if o.action == "move":
        return (f"Hello. {distributor} will collect {what} on the next van visit, to take to "
                "a shop where it sells faster. They will confirm the day with you.")
    if o.action == "pickup":
        return (f"Hello. {distributor} will collect {what} early, while it is still good to "
                "sell. They will confirm the day with you.")
    when = f" Some of it expires on {_date(expires_on)}." if expires_on else ""
    return (f"Hello. {display_name(item)} is selling slowly.{when} Try putting it at the front "
            f"of the shop where customers can see it. {distributor} may send you an offer.")

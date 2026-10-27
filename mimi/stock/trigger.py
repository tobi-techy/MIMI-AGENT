"""When Mimi asks a shop for a one-tap count. Rules in code.

Ask about an item when its estimated days to run out is under 3 (proposed in
RAI-270), and only within the caps:

* at most one question per item per week (ISO week, Africa/Lagos);
* at most ``SHOP_WEEKLY_CAP`` questions per shop per week (too many questions
  annoy shops);
* one open question per shop at a time, so a free-text answer always belongs
  to exactly one item;
* never for an item with no sell rate (nothing to say it is running low).

A missed question is never asked again that week.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from mimi.stock.estimator import StockEstimate

ASK_UNDER_DAYS = 3.0
SHOP_WEEKLY_CAP = 3


def iso_week(day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}W{w:02d}"


@dataclass
class AskBook:
    """Which items were asked about in which week, and which shops have a
    question still open."""

    item_weeks: set[tuple[str, str, str]] = field(default_factory=set)
    shop_weeks: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    open_shops: set[str] = field(default_factory=set)

    def record(self, shop_id: str, sku: str, week: str) -> None:
        self.item_weeks.add((shop_id, sku, week))
        self.shop_weeks[(shop_id, week)] += 1
        self.open_shops.add(shop_id)

    def close(self, shop_id: str) -> None:
        self.open_shops.discard(shop_id)


def should_ask(est: StockEstimate, book: AskBook, week: str) -> tuple[bool, str]:
    """(ask?, why) for one estimate in ``week``."""
    if est.days_to_run_out is None or est.sell_rate_per_day <= 0:
        return False, "no_sell_rate"
    if est.days_to_run_out >= ASK_UNDER_DAYS:
        return False, "not_low"
    if (est.shop_id, est.sku, week) in book.item_weeks:
        return False, "asked_this_week"
    if book.shop_weeks.get((est.shop_id, week), 0) >= SHOP_WEEKLY_CAP:
        return False, "shop_weekly_cap"
    if est.shop_id in book.open_shops:
        return False, "question_open"
    return True, "low"


def pick_asks(
    estimates: list[StockEstimate], book: AskBook, week: str
) -> list[StockEstimate]:
    """The items to ask about now: lowest days to run out first, one per shop."""
    ranked = sorted(
        (e for e in estimates if e.days_to_run_out is not None),
        key=lambda e: (e.days_to_run_out, -e.sell_rate_per_day, e.shop_id, e.sku),
    )
    picked: list[StockEstimate] = []
    for e in ranked:
        ok, _ = should_ask(e, book, week)
        if ok:
            book.record(e.shop_id, e.sku, week)
            picked.append(e)
    return picked

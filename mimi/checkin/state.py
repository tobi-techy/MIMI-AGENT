"""Weekly check-in state, in code: one ask a week, skips, streaks.

* ``CheckinStore``: one record per (shop, week). The same event id twice is
  one check-in (idempotent on ``event_id``). A week that got no reply is
  ``skipped``; no second message ever goes out for it.
* ``SkipLedger``: consecutive skips. Three in a row switches the shop to one
  Connect offer (RAI-277) and stops the weekly question; low-stock taps drop
  to at most monthly.
* ``Streaks``: consecutive answered weeks. After a streak Mimi says the
  record helps when asking for credit (the distributor still sets every
  limit).
* A shop that types ``stop`` gets no more check-ins.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CONNECT_AFTER_SKIPS = 3
STREAK_CREDIT_LINE_AFTER = 3


@dataclass(frozen=True)
class CheckinRecord:
    shop_id: str
    week: str
    event_id: str
    status: str  # asked | answered | skipped | stopped | connected_offered
    low_stock_sku: str = ""
    answered_at: str = ""


@dataclass
class CheckinStore:
    """One record per (shop, week); idempotent on event id."""

    records: dict[tuple[str, str], CheckinRecord] = field(default_factory=dict)
    seen_events: set[str] = field(default_factory=set)

    def start(
        self, shop_id: str, week: str, event_id: str, low_stock_sku: str = ""
    ) -> tuple[CheckinRecord, bool]:
        """Start (or replay) a week's check-in. ``(record, is_new)``: a repeat
        event id returns the existing record with ``is_new`` False, so a retry
        never sends a duplicate."""
        key = (shop_id, week)
        if event_id in self.seen_events and key in self.records:
            return self.records[key], False
        if key in self.records:
            return self.records[key], False
        rec = CheckinRecord(shop_id, week, event_id, "asked", low_stock_sku)
        self.records[key] = rec
        self.seen_events.add(event_id)
        return rec, True

    def answer(self, shop_id: str, week: str, at: str = "") -> CheckinRecord | None:
        key = (shop_id, week)
        rec = self.records.get(key)
        if rec is None or rec.status != "asked":
            return None
        rec = CheckinRecord(rec.shop_id, rec.week, rec.event_id, "answered", rec.low_stock_sku, at)
        self.records[key] = rec
        return rec

    def skip(self, shop_id: str, week: str) -> CheckinRecord | None:
        """Mark a week skipped (no reply, no second nudge). Never re-asks."""
        key = (shop_id, week)
        rec = self.records.get(key)
        if rec is None or rec.status != "asked":
            return None
        rec = CheckinRecord(rec.shop_id, rec.week, rec.event_id, "skipped", rec.low_stock_sku)
        self.records[key] = rec
        return rec

    def stop(self, shop_id: str, week: str) -> CheckinRecord | None:
        key = (shop_id, week)
        rec = self.records.get(key)
        if rec is None:
            return None
        rec = CheckinRecord(
            rec.shop_id, rec.week, rec.event_id, "stopped", rec.low_stock_sku, rec.answered_at
        )
        self.records[key] = rec
        return rec

    def status(self, shop_id: str, week: str) -> str:
        rec = self.records.get((shop_id, week))
        return rec.status if rec else ""


@dataclass
class SkipLedger:
    """Consecutive skips per shop; three in a row means one Connect offer."""

    skips: dict[str, int] = field(default_factory=dict)
    offered: set[str] = field(default_factory=set)

    def note_skip(self, shop_id: str) -> int:
        self.skips[shop_id] = self.skips.get(shop_id, 0) + 1
        return self.skips[shop_id]

    def note_answer(self, shop_id: str) -> None:
        self.skips[shop_id] = 0

    def consecutive(self, shop_id: str) -> int:
        return self.skips.get(shop_id, 0)

    def needs_connect_offer(self, shop_id: str) -> bool:
        return self.consecutive(shop_id) >= CONNECT_AFTER_SKIPS and shop_id not in self.offered

    def mark_offered(self, shop_id: str) -> None:
        self.offered.add(shop_id)

    def weekly_allowed(self, shop_id: str) -> bool:
        """The weekly question stops after three skips (one Connect offer)."""
        return self.consecutive(shop_id) < CONNECT_AFTER_SKIPS


@dataclass
class Streaks:
    """Consecutive answered weeks per shop."""

    streaks: dict[str, int] = field(default_factory=dict)

    def note_answer(self, shop_id: str) -> int:
        self.streaks[shop_id] = self.streaks.get(shop_id, 0) + 1
        return self.streaks[shop_id]

    def note_skip(self, shop_id: str) -> None:
        self.streaks[shop_id] = 0

    def current(self, shop_id: str) -> int:
        return self.streaks.get(shop_id, 0)

    def earns_credit_line(self, shop_id: str) -> bool:
        return self.current(shop_id) >= STREAK_CREDIT_LINE_AFTER


__all__ = [
    "CONNECT_AFTER_SKIPS",
    "STREAK_CREDIT_LINE_AFTER",
    "CheckinRecord",
    "CheckinStore",
    "SkipLedger",
    "Streaks",
]

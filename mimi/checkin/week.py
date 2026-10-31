"""The check-in week: one tap, one count at most, never a second nudge.

``CheckinWeek`` is the whole weekly turn in code:

1. Monday 08:00 WAT the scheduler posts ``checkin.weekly`` (``POST
   /v1/events``, ``kind=routine``; idempotent on ``event_id``). Exactly one
   check-in goes out: "Bought anything from anyone else this week?" with
   No / voice-or-receipt / Skip, plus the low-stock one-tap count batched in
   when the estimator flagged a top item that week.
2. The shop answers by tap (``button_id`` on ``POST /v1/turns``), typed
   number or voice note. A tap resets the estimate to that bucket; a typed
   or voice number overrides it exactly, through ``submit_count``.
3. "Yes" with a voice note or receipt photo stores stock-in lines after one
   confirm; Skip stores nothing. Unclear replies get one confirm question;
   no count is stored until confirmed.
4. No reply: the week is skipped. Mimi does not ask again until next week.
   Three skips in a row: stop the weekly question, offer Connect once
   (RAI-277); low-stock taps drop to at most monthly.
5. A restock yes creates an order draft only; nothing reaches the van until
   the distributor approves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast

from mimi.checkin import copy
from mimi.checkin.replies import read_reply
from mimi.checkin.state import CheckinStore, SkipLedger, Streaks
from mimi.checkin.submit import CountSink, CountSource, submit_count
from mimi.stock.history import Bucket, assumed_units, bucket_of

WeekResult = Literal["asked", "duplicate", "not_allowed", "off"]


@dataclass
class WeekPlan:
    text: str
    buttons: tuple[str, ...]
    low_stock_sku: str = ""
    low_stock_text: str = ""
    template: str = "mimi_weekly_checkin"


@dataclass
class CheckinWeek:
    """One shop's weekly check-in turn, decided in code."""

    store: CheckinStore = field(default_factory=CheckinStore)
    skips: SkipLedger = field(default_factory=SkipLedger)
    streaks: Streaks = field(default_factory=Streaks)
    sink: CountSink = field(default_factory=CountSink)
    stopped: set[str] = field(default_factory=set)

    def plan_week(
        self,
        shop_id: str,
        week: str,
        event_id: str,
        *,
        shop_name: str = "",
        low_stock_sku: str = "",
        low_stock_item: str = "",
        low_stock_unit: str = "",
        mode: str = "on",
    ) -> tuple[WeekPlan | None, WeekResult]:
        """The week's messages, or None when nothing goes out. Never sends
        twice: a repeat event id replays the existing record."""
        if mode == "off":
            return None, "off"
        if shop_id in self.stopped:
            return None, "not_allowed"
        if not self.skips.weekly_allowed(shop_id):
            if self.skips.needs_connect_offer(shop_id):
                self.skips.mark_offered(shop_id)
                self.store.start(shop_id, week, event_id)
                return WeekPlan(
                    copy.connect_offer_text(), ("Connect", "No"), template="mimi_weekly_checkin"
                ), "asked"
            return None, "not_allowed"
        rec, is_new = self.store.start(shop_id, week, event_id, low_stock_sku)
        if not is_new:
            return None, "duplicate"
        text = copy.checkin_text(shop_name)
        low_text = ""
        if low_stock_sku and low_stock_item:
            low_text = copy.low_stock_text(low_stock_item, low_stock_unit or "units")
            text = text + "\n" + low_text
        return WeekPlan(text, copy.CHECKIN_BUTTONS, rec.low_stock_sku, low_text), "asked"

    def miss_week(self, shop_id: str, week: str) -> str:
        """No reply: skip the week, count the skip, never nudge again."""
        self.store.skip(shop_id, week)
        n = self.skips.note_skip(shop_id)
        self.streaks.note_skip(shop_id)
        return f"skipped ({n} in a row)"

    def answer_week(
        self,
        shop_id: str,
        week: str,
        text: str,
        *,
        sku: str = "",
        estimated_left: int = 0,
        message_id: str = "",
    ) -> dict[str, object]:
        """Route one reply. Returns what happened; counts go through
        ``submit_count`` with source checkin_bucket / typed / voice."""
        reply = read_reply(text)
        if reply.kind == "stop":
            self.stopped.add(shop_id)
            self.store.stop(shop_id, week)
            return {"kind": "stop", "reply": copy.stopped_text()}
        if reply.kind in ("no", "skip"):
            self.store.answer(shop_id, week)
            self.skips.note_answer(shop_id)
            if reply.kind == "no":
                self.streaks.note_answer(shop_id)
            else:
                self.streaks.note_skip(shop_id)
            return {"kind": reply.kind, "stored": False}
        if reply.kind in ("count_tap", "count_typed"):
            bucket: Bucket = cast(Bucket, reply.bucket or bucket_of(reply.units or 0))
            units = (
                reply.units if reply.units is not None else assumed_units(bucket, estimated_left)
            )
            exact = reply.units is not None
            source: CountSource = "typed" if exact else "checkin_bucket"
            submit_count(
                self.sink,
                shop_id=shop_id,
                sku=sku,
                bucket=bucket,
                units=units,
                exact=exact,
                source=source,
                week=week,
                message_id=message_id,
            )
            self.store.answer(shop_id, week)
            self.skips.note_answer(shop_id)
            self.streaks.note_answer(shop_id)
            return {
                "kind": "count",
                "bucket": bucket,
                "units": units,
                "exact": exact,
                "via": "typed" if exact else "tap",
            }
        if reply.kind == "yes_supplier":
            return {"kind": "yes_supplier", "needs_lines": True}
        if reply.kind in ("restock_yes", "restock_no"):
            return {"kind": reply.kind, "draft_only": True}
        return {"kind": "unclear", "needs_confirm": True}

    def voice_count(
        self, shop_id: str, week: str, units: int, *, sku: str = "", message_id: str = ""
    ) -> dict[str, object]:
        """A voice note's exact number: overrides the estimate exactly."""
        bucket = bucket_of(units)
        submit_count(
            self.sink,
            shop_id=shop_id,
            sku=sku,
            bucket=bucket,
            units=units,
            exact=True,
            source="voice",
            week=week,
            message_id=message_id,
        )
        self.store.answer(shop_id, week)
        self.skips.note_answer(shop_id)
        self.streaks.note_answer(shop_id)
        return {"kind": "count", "bucket": bucket, "units": units, "exact": True, "via": "voice"}

    def streak_line(self, shop_id: str) -> str:
        n = self.streaks.current(shop_id)
        if self.streaks.earns_credit_line(shop_id):
            return copy.streak_text(n)
        return ""


__all__ = ["CheckinWeek", "WeekPlan", "WeekResult"]

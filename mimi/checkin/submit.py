"""The count submit function (RAI-294's interface, defined here first).

The RAI-294 agent owns the estimator and provides the real submit function.
This module is the interface both sides code against: ``submit_count``
stores one shop count (tap bucket, typed or voice number) with source
``checkin_bucket``, ``typed`` or ``voice``, and the estimate resets from it.
Until the RAI-294 branch lands, calls go to the in-memory ``CountSink``;
swapping in the real submit is one import.

Sources: ``checkin_bucket`` (a 0-2 / 3-5 / 6+ tap), ``typed`` (an exact
number the shop typed), ``voice`` (an exact number from a voice note).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

CountSource = Literal["checkin_bucket", "typed", "voice"]

SOURCES: tuple[CountSource, ...] = ("checkin_bucket", "typed", "voice")


@dataclass(frozen=True)
class SubmittedCount:
    shop_id: str
    sku: str
    bucket: str
    units: int
    exact: bool
    source: CountSource
    week: str
    message_id: str = ""


@dataclass
class CountSink:
    """Where submitted counts land until the RAI-294 submit lands."""

    counts: list[SubmittedCount] = field(default_factory=list)

    def submit(self, count: SubmittedCount) -> SubmittedCount:
        self.counts.append(count)
        return count

    def for_shop(self, shop_id: str, week: str = "") -> list[SubmittedCount]:
        return [c for c in self.counts
                if c.shop_id == shop_id and (not week or c.week == week)]


def submit_count(sink: CountSink, *, shop_id: str, sku: str, bucket: str, units: int,
                 exact: bool, source: CountSource, week: str,
                 message_id: str = "") -> SubmittedCount:
    """Store one check-in count. A typed or voice number overrides the
    estimate exactly (``exact`` True); a tap resets it to the bucket."""
    if source not in SOURCES:
        raise ValueError(f"unknown count source {source!r}")
    if bucket not in ("0-2", "3-5", "6+"):
        raise ValueError(f"unknown bucket {bucket!r}")
    if units < 0:
        raise ValueError("a count cannot be negative")
    return sink.submit(SubmittedCount(shop_id, sku, bucket, units, exact, source, week,
                                      message_id))


__all__ = ["SOURCES", "CountSink", "CountSource", "SubmittedCount", "submit_count"]

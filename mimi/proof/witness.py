"""The bank witness: Coral Lane's own bank feed, read-only.

A screenshot is the shop's word. A payment is verified only when the
distributor's bank feed shows a credit that fits it: the same amount in
integer kobo, credited no earlier than a few minutes before the time on the
screenshot and within the bank window after it, carrying the screenshot's
reference or session id (or the sender's name) in its narration, and not
already used for another proof. Then the three witnesses agree: the rep who
logged the delivery, the shop's screenshot, the bank's credit.

``BankFeed`` is a read-only port; nothing here can move money.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from mimi.proof.parse import norm_name, norm_ref
from mimi.world.model import FeedCredit

BANK_EARLY_SLACK = timedelta(minutes=10)
BANK_WINDOW = timedelta(hours=48)


class BankFeed(Protocol):
    def credits(self, tenant_id: str) -> Sequence[FeedCredit]: ...


class ListBankFeed:
    """A feed backed by a list (tests and the sim's stand-in for the bank)."""

    def __init__(self, credits: Sequence[FeedCredit] = ()) -> None:
        self._credits = list(credits)

    def add(self, credit: FeedCredit) -> None:
        self._credits.append(credit)

    def credits(self, tenant_id: str) -> Sequence[FeedCredit]:
        return list(self._credits)


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def find_credit(
    feed: Sequence[FeedCredit],
    *,
    amount_kobo: int,
    paid_at: datetime,
    reference: str,
    session_id: str,
    sender: str,
    used: Callable[[str], bool],
) -> FeedCredit | None:
    keys = [k for k in (norm_ref(reference), norm_ref(session_id)) if k]
    who = norm_name(sender)
    for c in sorted(feed, key=lambda c: c.received_at):
        if c.amount_kobo != amount_kobo or used(c.credit_id):
            continue
        at = _at(c.received_at)
        if at.tzinfo is None:
            at = at.replace(tzinfo=paid_at.tzinfo)
        if not (paid_at - BANK_EARLY_SLACK <= at <= paid_at + BANK_WINDOW):
            continue
        narration = norm_ref(c.narration)
        if any(k in narration for k in keys) or (who and who in norm_name(c.narration)):
            return c
    return None

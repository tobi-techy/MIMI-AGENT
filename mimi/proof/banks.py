"""The fictional banks of the demo world, and Coral Lane's account.

Every bank here is invented for the demo (no real bank's name, logo or
layout): Kestrel Bank, Harmattan MFB and Lagoonpay (app receipts), and
Ironleaf Bank, Ashgrove Bank and Marlwood Bank (SMS debit alerts). Each has
a fixed layout, reference format and date format, which the code checks in
``checks.py`` use. Account numbers start with ``999`` so they can never be
mistaken for real ones.

The world fixture names a shop's bank (``Shop.bank``) with one of these
names directly, so the world, its alerts and the screenshots all show the
same fictional bank.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from mimi.world.catalogue import COLLECTION_ACCOUNT

Layout = Literal["app_receipt", "sms_alert"]


@dataclass(frozen=True)
class FictionalBank:
    name: str
    short: str
    layout: Layout
    # The reference the bank prints (app: "Transaction ref"; SMS: inside Desc).
    reference_re: str
    # strftime formats the bank prints dates in (first is the one it uses).
    time_formats: tuple[str, ...]
    aliases: tuple[str, ...] = ()

    def reference_ok(self, ref: str) -> bool:
        return re.fullmatch(self.reference_re, ref.strip().upper()) is not None


BANKS: tuple[FictionalBank, ...] = (
    FictionalBank(
        name="Kestrel Bank",
        short="KST",
        layout="app_receipt",
        reference_re=r"KST\d{12}",
        time_formats=("%d %b %Y, %H:%M",),
        aliases=("kestrel", "kestrel bank plc"),
    ),
    FictionalBank(
        name="Harmattan MFB",
        short="HMF",
        layout="app_receipt",
        reference_re=r"HMF-[A-Z0-9]{10}",
        time_formats=("%b %d, %Y %I:%M:%S %p",),
        aliases=("harmattan", "harmattan microfinance bank", "harmattan mfb"),
    ),
    FictionalBank(
        name="Lagoonpay",
        short="LGP",
        layout="app_receipt",
        # Lagoonpay prints the 30-digit NIP session id as its transaction no.
        reference_re=r"999\d{27}",
        time_formats=("%Y-%m-%d %H:%M:%S",),
        aliases=("lagoon pay", "lagoonpay"),
    ),
    FictionalBank(
        name="Ironleaf Bank",
        short="IL",
        layout="sms_alert",
        reference_re=r"IL/TRF/\d{9}",
        time_formats=("%d/%m/%Y %H:%M",),
        aliases=("ironleaf", "ironleafbnk", "ironleaf bank"),
    ),
    FictionalBank(
        name="Ashgrove Bank",
        short="AG",
        layout="sms_alert",
        reference_re=r"AG/TRF/\d{9}",
        time_formats=("%d/%m/%Y %H:%M",),
        aliases=("ashgrove", "ashgrovebnk", "ashgrove bank"),
    ),
    FictionalBank(
        name="Marlwood Bank",
        short="MW",
        layout="sms_alert",
        reference_re=r"MW/TRF/\d{9}",
        time_formats=("%d %b %Y %H:%M",),
        aliases=("marlwood", "marlwoodbnk", "marlwood bank"),
    ),
)

# Every format any of the banks prints; a printed date that fits none of
# them (or is not a real date, "31 Sep") is an impossible time.
ALL_TIME_FORMATS: tuple[str, ...] = tuple(f for b in BANKS for f in b.time_formats) + (
    "%d %b %Y %H:%M",
    "%d %B %Y, %H:%M",
    "%b %d, %Y %H:%M",
    "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M:%S",
)


@dataclass(frozen=True)
class CollectionAccount:
    """The distributor's own account that shops pay into (fictional)."""

    name: str
    bank: str
    number: str

    @property
    def last4(self) -> str:
        return self.number[-4:]


_ACCOUNT = dict(COLLECTION_ACCOUNT)
CORAL_LANE_ACCOUNT = CollectionAccount(
    name=_ACCOUNT["account_name"], bank=_ACCOUNT["bank"], number=_ACCOUNT["account_number"]
)


def bank_named(name: str | None) -> FictionalBank | None:
    """The fictional bank a printed name refers to, or None."""
    low = " ".join((name or "").lower().split())
    if not low:
        return None
    for b in BANKS:
        if low == b.name.lower() or low in b.aliases:
            return b
    for b in BANKS:
        if b.name.lower() in low or any(a in low for a in b.aliases):
            return b
    return None


def bank_for(name: str) -> FictionalBank:
    """The fictional bank with exactly this name (a world ``Shop.bank``)."""
    for b in BANKS:
        if b.name == name:
            return b
    raise KeyError(f"not one of the fictional banks: {name!r}")

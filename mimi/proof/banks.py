"""The fictional banks in the synthetic screenshots, and Coral Lane's account.

Every bank here is invented for the demo (no real bank's name, logo or
layout): Kestrel Bank, Harmattan MFB, Lagoonpay and Ironleaf Bank. Each has
a fixed layout, reference format and date format, which the code checks in
``checks.py`` use. Account numbers start with ``999`` so they can never be
mistaken for real ones.

The world fixture names the bank a shop pays from with its usual app or bank
(``Shop.bank``); ``fictional_bank`` maps that to one of these for the
screenshots, so a rendered image never shows a real brand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

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

# The world names a shop's usual app or bank; screenshots use these instead.
_WORLD_TO_FICTIONAL = {
    "Moniepoint": "Lagoonpay",
    "OPay": "Harmattan MFB",
    "PalmPay": "Kestrel Bank",
    "GTBank": "Ironleaf Bank",
    "First Bank": "Ironleaf Bank",
    "Access": "Ironleaf Bank",
}


@dataclass(frozen=True)
class CollectionAccount:
    """The distributor's own account that shops pay into (fictional)."""

    name: str
    bank: str
    number: str

    @property
    def last4(self) -> str:
        return self.number[-4:]


CORAL_LANE_ACCOUNT = CollectionAccount(
    name="CORAL LANE TRADING", bank="Kestrel Bank", number="9990004471"
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


def fictional_bank(world_bank: str) -> FictionalBank:
    name = _WORLD_TO_FICTIONAL.get(world_bank, "Kestrel Bank")
    found = bank_named(name)
    assert found is not None
    return found

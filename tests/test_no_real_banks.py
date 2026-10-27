"""No real Nigerian bank or fintech name anywhere in the tracked files.

The demo world, its alerts and the payment screenshots use fictional banks
only (``mimi.proof.banks``). This test git-greps every tracked text file
(binary files such as the screenshots are skipped by ``git grep -I``) for a
list of real bank and fintech names, case-insensitive and on word
boundaries, and fails on any hit.

Names that are also everyday English words (access, union, sterling, ...)
are matched only when followed by "bank", "plc" or "mfb", so prose like
"give judges a link" never trips it; the distinctive brand names are matched
on their own.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Distinctive names: matched on their own.
BRANDS = (
    "GTBank", "GT Bank", "GTB", "GTCO", "Guaranty Trust", "Zenith", "First Bank", "FirstBank",
    "FBN", "UBA", "United Bank for Africa", "OPay", "Moniepoint", "PalmPay", "Kuda", "Wema",
    "ALAT", "FCMB", "First City Monument", "Stanbic", "IBTC", "Ecobank", "Providus",
    "Jaiz", "Flutterwave", "Paystack", "FairMoney", "Paga", "VFD", "Eyowo",
    "Chipper", "Parallex", "Suntrust",
)
# Everyday words that are also bank names: matched with a bank suffix.
WORD_BANKS = (
    "Access", "Union", "Sterling", "Fidelity", "Keystone", "Polaris", "Heritage", "Unity",
    "Titan", "Globus", "Optimus", "Premium Trust", "Signature", "Lotus", "Taj", "Rubies",
    "Sparkle", "Citi", "Carbon",
)


def _pattern() -> str:
    def esc(n: str) -> str:
        return n.replace(" ", r"[ -]?")

    brands = "|".join(esc(n) for n in BRANDS)
    words = "|".join(esc(n) for n in WORD_BANKS)
    return rf"\b(({brands})|({words})[ -]?(bank|plc|mfb))\b"


def _grep(pattern: str) -> list[str]:
    out = subprocess.run(
        ["git", "grep", "-I", "-n", "-i", "-P", pattern, "--", "."],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert out.returncode in (0, 1), out.stderr  # 1 means no match
    return [line for line in out.stdout.splitlines() if not line.startswith("tests/test_no_real_banks.py:")]


@pytest.mark.skipif(
    shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout"
)
def test_no_real_bank_names_in_tracked_files() -> None:
    hits = _grep(_pattern())
    assert hits == [], "real bank names found:\n" + "\n".join(hits[:20])


def test_the_pattern_catches_real_names_and_spares_fictional_ones() -> None:
    import re

    rx = re.compile(_pattern(), re.IGNORECASE)
    for real in ("paid from GTBank", "MONIEPOINT receipt", "Access Bank alert", "first bank",
                 "NIP/OPAY/ADE", "Zenith", "union bank plc"):
        assert rx.search(real), real
    for fine in ("Kestrel Bank", "Harmattan MFB", "Lagoonpay", "Ironleaf Bank", "Ashgrove Bank",
                 "Marlwood Bank", "judges get access", "a union of sets", "NIP/LAGOONPAY/ADE"):
        assert not rx.search(fine), fine

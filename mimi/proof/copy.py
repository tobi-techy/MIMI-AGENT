"""What Mimi says to the shop about a payment screenshot. Plain English.

Fixed templates, never model-written, and every figure is formatted here
from integer kobo. The wording never accuses the shop: a screenshot that
looks wrong gets the same calm "sent to the distributor to confirm with the
bank" as any other, and nothing on the shop's book changes. No message says
a payment was received or verified from a screenshot; only the bank's own
credit can make it verified (``witness.py``).

``figures_ok`` is the verifier: every naira figure in a reply must be one
the code allowed (the amount read and parsed, an invoice's open amount).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from mimi.proof.checks import Assessment


def naira(kobo: int) -> str:
    whole, part = divmod(kobo, 100)
    return f"N{whole:,}" + (f".{part:02d}" if part else "")


def likely_text(a: Assessment, distributor: str) -> str:
    assert a.amount_kobo is not None
    who = f" from {a.sender}" if a.sender else ""
    return (
        f"Thanks. I can see {naira(a.amount_kobo)}{who} on your screenshot. I've sent it to "
        f"{distributor} to confirm with the bank. It shows as likely paid until the bank "
        "confirms."
    )


def review_text(distributor: str) -> str:
    """For a screenshot that looks wrong. Neutral: no accusation, no reason."""
    return (
        f"Thanks, I've sent this to {distributor} to confirm with the bank. "
        "Nothing has changed on your account yet."
    )


def ask_text() -> str:
    """The one clear ask for a screenshot Mimi could not read."""
    return (
        "Thanks. I couldn't read this screenshot clearly. Please send a clearer "
        "screenshot of the transfer that shows the amount, the date, the reference "
        "and who it was paid to."
    )


def unreadable_again_text(distributor: str) -> str:
    return (
        f"Thanks. I still couldn't read it, so I've sent it to {distributor} to check. "
        "Nothing has changed on your account yet."
    )


def duplicate_text() -> str:
    return "Thanks, I already have this screenshot. Nothing new has been added to your account."


def verified_text(amount_kobo: int, distributor: str) -> str:
    return f"{distributor}'s bank has confirmed your payment of {naira(amount_kobo)}. Thank you."


_FIGURE = re.compile(r"N(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{2}))?")


def figures_in(text: str) -> list[int]:
    out = []
    for m in _FIGURE.finditer(text):
        out.append(int(m.group(1).replace(",", "")) * 100 + int(m.group(2) or 0))
    return out


def figures_ok(text: str, allowed_kobo: Iterable[int]) -> bool:
    """True when every naira figure in ``text`` is one code allowed."""
    allowed = set(allowed_kobo)
    return all(f in allowed for f in figures_in(text))

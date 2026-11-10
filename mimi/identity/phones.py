"""Phone numbers, compared one way.

``users.phone_e164`` (the identity provider's number), ``reps.phone`` (the
book's) and ``tenants.owner_phones`` all describe the same person and none of
them is guaranteed to carry the country code. Comparing the strings would
miss "+2348000000001" against "08000000001", which is exactly how a rep would
end up with an empty scope and an empty screen. So both sides go through
``phone_key``: digits only, and the last ten digits decide a match.
"""

from __future__ import annotations

MIN_KEY = 10  # the last ten digits of a Nigerian number identify a line


def phone_key(phone: str | None) -> str:
    """Digits only. "+234 800 000 0001" and "08000000001" both end the same."""
    if not phone:
        return ""
    return "".join(c for c in str(phone) if c.isdigit())


def same_phone(left: str | None, right: str | None) -> bool:
    """True when two numbers are the same line, with or without a country code.

    Short or empty keys never match: a book row with no number must not be
    claimed by the first caller who also has no number."""
    a, b = phone_key(left), phone_key(right)
    if len(a) < MIN_KEY or len(b) < MIN_KEY:
        return False
    return a[-MIN_KEY:] == b[-MIN_KEY:]

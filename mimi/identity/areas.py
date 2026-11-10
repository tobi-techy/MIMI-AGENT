"""Areas, compared one way only.

``shops.area`` and ``reps.areas`` are free strings typed by the distributor,
and ``user_territories.area`` is written by an owner. "Ikeja", "ikeja" and
"Ikeja " have to be one territory: a whitespace or case difference that hides
a supervisor's own shops from them looks like an empty screen, not like a bug,
and nobody reports it. So every writer normalises with ``area_key`` and every
comparison normalises both sides with it.
"""

from __future__ import annotations

import unicodedata


def area_key(area: str | None) -> str:
    """The one form an area is stored and compared in: trimmed, casefolded,
    and with internal runs of whitespace collapsed to one space."""
    if not area:
        return ""
    text = unicodedata.normalize("NFKC", str(area)).strip().casefold()
    return " ".join(text.split())


def area_keys(areas: object) -> frozenset[str]:
    """Normalise a list of areas (a ``reps.areas`` JSON list, or any iterable).

    Anything that is not a string is dropped rather than stringified: a stray
    number in the JSON must not become a territory nobody can match."""
    if not isinstance(areas, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(k for k in (area_key(a) for a in areas if isinstance(a, str)) if k)

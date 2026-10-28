"""The verifier: every number in a credit message must come from code.

``unknown_numbers(text, facts)`` returns each number in ``text`` (naira
figures, counts, percentages, days) that no fact gives. Facts are the code's
own outputs: the credit file, the shelf count, the recommendation's reasons,
the item names ("Sunrise Instant Noodles 70g" makes 70 known). A naira figure
is compared in kobo, so "N16,500" and "N16,500.00" are the same figure.

Run on the owner note before the approvals item is proposed (a figure the
model made up is refused, the model is told which) and, in the eval, on
every message Mimi sent the shop.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

_NAIRA = re.compile(r"(?:\bN|₦|NGN\s?)\s?(\d[\d,]*(?:\.\d{1,2})?)\s?(k|m)?\b", re.I)
_NUM = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?:\s?(k|m)\b)?", re.I)


def _dec(num: str, suffix: str | None = None) -> Decimal | None:
    try:
        value = Decimal(num.replace(",", ""))
    except InvalidOperation:
        return None
    return value * {"k": 1_000, "m": 1_000_000}.get((suffix or "").lower(), 1)


def known_numbers(facts: Iterable[Any]) -> tuple[set[Decimal], set[int]]:
    """(plain numbers, naira figures in kobo) found in the facts."""
    plain: set[Decimal] = set()
    kobo: set[int] = set()

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, list | tuple | set):
            for v in node:
                walk(v, key)
        elif isinstance(node, bool):
            return
        elif isinstance(node, int):
            if key.endswith("_kobo"):
                kobo.add(node)
            else:
                plain.add(Decimal(node))
        elif isinstance(node, float):
            plain.add(Decimal(str(node)))
        elif isinstance(node, str):
            for m in _NAIRA.finditer(node):
                d = _dec(m.group(1), m.group(2))
                if d is not None:
                    kobo.add(int(d * 100))
            for m in _NUM.finditer(node):
                d = _dec(m.group(1), m.group(2))
                if d is not None:
                    plain.add(d)

    for f in facts:
        walk(f)
    return plain, kobo


def unknown_numbers(text: str, facts: Iterable[Any]) -> list[str]:
    plain, kobo = known_numbers(facts)
    bad: list[str] = []
    spans: list[tuple[int, int]] = []
    for m in _NAIRA.finditer(text):
        spans.append(m.span())
        d = _dec(m.group(1), m.group(2))
        if d is None or int(d * 100) not in kobo:
            bad.append(m.group(0).strip())
    for m in _NUM.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        d = _dec(m.group(1), m.group(2))
        if d is None or d not in plain:
            bad.append(m.group(0).strip())
    return bad

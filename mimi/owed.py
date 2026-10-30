"""What shops owe, summed once in code, so every surface shows the same figure.

One definition of overdue for the whole API: an open receivable (outstanding
above zero) whose due date has passed on the book's day, i.e. the book's
``days_overdue`` above zero. The Collector tools, the answer cards, the
morning brief, ``GET /v1/shops`` and the context builder all call
``totals`` (or ``is_overdue``) here instead of keeping their own sum.

Before 30 Oct 2026 Collector's ``get_invoices`` added up only the shops it
listed (``limit``, default 20), so with 24 shops overdue its "total overdue"
dropped the 4 smallest (N76,150 on the demo data: N7,154,850 against the
book's N7,231,000). The total is now taken over every row, before any limit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

NAIRA_SIGN = "\u20a6"  # ₦


def is_overdue(row: Mapping[str, Any]) -> bool:
    """An open receivable past its due date (the book's ``days_overdue``)."""
    return int(row.get("outstanding_kobo") or 0) > 0 and int(row.get("days_overdue") or 0) > 0


@dataclass
class ShopOwed:
    shop_id: str
    shop_name: str
    owed_kobo: int = 0
    overdue_kobo: int = 0
    open_invoices: int = 0
    overdue_invoices: int = 0
    oldest_days_overdue: int = 0


@dataclass
class Owed:
    shops: dict[str, ShopOwed] = field(default_factory=dict)

    @property
    def owed_kobo(self) -> int:
        return sum(s.owed_kobo for s in self.shops.values())

    @property
    def overdue_kobo(self) -> int:
        return sum(s.overdue_kobo for s in self.shops.values())

    def debtors(self) -> list[ShopOwed]:
        """Shops with anything overdue, most overdue first (ties by shop id)."""
        return sorted((s for s in self.shops.values() if s.overdue_kobo > 0),
                      key=lambda s: (-s.overdue_kobo, s.shop_id))


def totals(rows: Iterable[Mapping[str, Any]]) -> Owed:
    """Per-shop and overall owed and overdue, in integer kobo, from receivable rows."""
    out = Owed()
    for r in rows:
        kobo = int(r.get("outstanding_kobo") or 0)
        if kobo <= 0:
            continue
        sid = str(r["shop_id"])
        s = out.shops.get(sid)
        if s is None:
            s = out.shops[sid] = ShopOwed(sid, str(r.get("shop_name") or sid))
        s.owed_kobo += kobo
        s.open_invoices += 1
        if is_overdue(r):
            s.overdue_kobo += kobo
            s.overdue_invoices += 1
            s.oldest_days_overdue = max(s.oldest_days_overdue, int(r["days_overdue"]))
    return out


def naira(kobo: int) -> str:
    """Money for a person to read: ``₦7,231,000`` (kobo shown only when there are any)."""
    sign = "-" if kobo < 0 else ""
    k = abs(int(kobo))
    body = f"{k / 100:,.0f}" if k % 100 == 0 else f"{k / 100:,.2f}"
    return f"{sign}{NAIRA_SIGN}{body}"


# "N7,154,850", "N 500", "N1.2m" written as naira by a helper; never a word
# such as "No" or an id such as "INV-N12".
_N_AMOUNT = re.compile(r"(?<![A-Za-z0-9_\-/])N\s?(?=\d)")


def naira_sign(text: str) -> str:
    """Rewrite naira written with a plain ``N`` to ``₦``; figures are unchanged."""
    return _N_AMOUNT.sub(NAIRA_SIGN, text)

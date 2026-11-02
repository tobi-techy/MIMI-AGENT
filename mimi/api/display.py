"""What the owner reads: names, not ids; naira as ``₦1,234,500`` (30 Oct 2026 run 2).

The owner console showed ``Would send a firm reminder to shop-001 for N50,600``
and ``System: The owner approved approvals item apr_a3278d10... (send_reminder
for shop-001)``. Ids stay in the records, the payloads and everything a model
reads (changing those would change every recorded request); this module
rewrites only the text the API hands the console:

* ``shop-001`` becomes the shop's name and ``rep-03`` the rep's name, from the
  book;
* an approvals id (``apr_...``), a turn or run id is never shown;
* ``N50600``, ``N50,600`` and ``N50,600.00`` become ``₦50,600`` (kobo kept
  when there are any).

Pure functions over a name map; :func:`names_for` reads the map once per
request.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from mimi.store import schema as s

_SHOP = re.compile(r"\bshop-\d{3}\b", re.I)
_REP = re.compile(r"\brep-\d{2,3}\b", re.I)
# Internal record ids the owner never needs: approvals, turns, runs, messages,
# intents. "approvals item apr_..." reads as "approvals item".
_IDS = re.compile(r"\s?\(?\b(?:apr|turn|run|msg|ik|od|ho)_[0-9a-f]{8,}\b\)?", re.I)
# A naira figure written with an N: N50600, N50,600, N50,600.00, N 1,000.
_N = re.compile(r"(?<![A-Za-z0-9₦])N\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?![\d,]*[A-Za-z])")
# A ₦ figure missing its thousands commas: ₦50600.
_NAIRA_BARE = re.compile(r"₦(\d{4,})(?:\.(\d{1,2}))?\b")


@dataclass(frozen=True)
class Names:
    shops: Mapping[str, str] = field(default_factory=dict)
    reps: Mapping[str, str] = field(default_factory=dict)


def _money(whole: str, frac: str | None) -> str:
    n = int(whole.replace(",", ""))
    k = int((frac or "0").ljust(2, "0")) if frac else 0
    return f"₦{n:,}" + (f".{k:02d}" if k else "")


def naira_text(text: str) -> str:
    """``N50,600`` and ``₦50600`` as ``₦50,600``."""
    if not text:
        return text
    out = _N.sub(lambda m: _money(m.group(1), m.group(2)), text)
    return _NAIRA_BARE.sub(lambda m: _money(m.group(1), m.group(2)), out)


def humanize(text: str, names: Names) -> str:
    """Owner-facing text with names for ids and ``₦`` money."""
    if not text:
        return text
    out = _SHOP.sub(lambda m: names.shops.get(m.group(0).lower(), m.group(0)), text)
    out = _REP.sub(lambda m: names.reps.get(m.group(0).lower(), m.group(0)), out)
    out = _IDS.sub("", out)
    out = naira_text(out)
    return re.sub(r"[ \t]{2,}", " ", out).replace(" .", ".").replace(" ,", ",")


def names_for(conn: Connection | Engine, tenant: str) -> Names:
    def run(c: Connection) -> Names:
        shops = {r.shop_id.lower(): r.name for r in c.execute(sa.select(
            s.shops.c.shop_id, s.shops.c.name).where(s.shops.c.tenant_id == tenant))}
        reps = {r.rep_id.lower(): r.name for r in c.execute(sa.select(
            s.reps.c.rep_id, s.reps.c.name).where(s.reps.c.tenant_id == tenant))}
        return Names(shops, reps)

    if isinstance(conn, Engine):
        with conn.connect() as c:
            return run(c)
    return run(conn)


# What each approval tool is, in the owner's words, for the system line the
# thread shows when the owner decides one.
TOOL_NOUN: dict[str, str] = {
    "send_reminder": "the reminder", "propose_reminder_plan": "the reminder plan",
    "submit_order": "the order", "revise_order": "the order",
    "draft_restock": "the restock draft", "revise_restock": "the restock draft",
    "submit_credit_review": "the credit request", "flag_payment_proof": "the flagged payment",
    "record_payment_proof": "the payment proof", "draft_brand_email": "the brand email draft",
    "raise_daily_allowance": "today's allowance raise",
    "propose_credit_limit": "the credit limit change",
}
_DECIDED = re.compile(r"^The owner (approved|rejected) approvals item \S+ \((\w+) for ([\w-]+|the "
                      r"distributor)\)\.?$")


def decided_line(text: str, names: Names) -> str:
    """``The owner approved approvals item apr_... (send_reminder for shop-001).`` as
    ``You approved the reminder for Mama Sola Provisions.``; other text as is."""
    m = _DECIDED.match((text or "").strip())
    if not m:
        return humanize(text, names)
    verb, tool, who = m.groups()
    noun = TOOL_NOUN.get(tool, "the item")
    whom = names.shops.get(who.lower(), "") if who.startswith("shop-") else ""
    return f"You {verb} {noun}" + (f" for {whom}" if whom else "") + "."

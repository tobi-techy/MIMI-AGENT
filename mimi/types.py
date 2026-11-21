"""Types every part of Mimi shares.

``ShopScope`` names which distributor (tenant) and which shop an intent is
about. The runner checks the tenant on every outbound intent, so a job for
one distributor can never propose anything for another.

Money is integer kobo everywhere (100 kobo = 1 naira). ``Kobo`` is only a
readable alias for ``int``: no float ever holds an amount.
"""

from __future__ import annotations

from dataclasses import dataclass

Kobo = int

#: The states a return can be in, in the order it moves. Two vocabularies, one
#: journey, and one list because the importer, the ledger and every read have
#: to agree on them (an imported return and a Mimi-proposed one live in the
#: same table: ``returns``).
#:
#: The book's own states, which a distributor's export writes:
#: ``pending`` raised and waiting on the rep, ``in_transit`` goods on the way
#: back, ``received`` back at the depot and inspected, then ``credited``, or
#: ``rejected`` at any point before the credit.
RETURN_BOOK_STATUSES: tuple[str, ...] = (
    "pending", "in_transit", "received", "credited", "rejected")

#: The ledger's states, which Mimi's own pickup proposal writes:
#: ``drafted`` opened and waiting on the owner, ``approved`` the owner approved
#: the pickup, ``collected`` the rep collected, ``inspected`` counted and
#: examined, ``settled`` the credit was approved and applied, or ``cancelled``.
RETURN_LEDGER_STATUSES: tuple[str, ...] = (
    "drafted", "approved", "collected", "inspected", "settled", "cancelled")

RETURN_STATUSES: tuple[str, ...] = RETURN_BOOK_STATUSES + RETURN_LEDGER_STATUSES

#: A return in one of these is still being worked on; anything else is closed.
RETURN_OPEN_STATUSES: frozenset[str] = frozenset({
    "pending", "in_transit", "received", "drafted", "approved", "collected",
    "inspected"})

#: Why a return came back, from the visit-record contract's closed list plus the
#: three reasons only a return has. A free-text reason is not a vocabulary, and
#: a reason nobody chose from cannot be counted.
RETURN_REASONS: tuple[str, ...] = (
    "shut", "no stock needed", "refused", "damaged", "expired", "wrong_sku")


@dataclass(frozen=True)
class ShopScope:
    """The tenant (distributor) and shop an intent belongs to."""

    tenant_id: str
    shop_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.shop_id:
            raise ValueError("ShopScope needs a tenant_id and a shop_id")

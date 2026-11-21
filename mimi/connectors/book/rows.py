"""Canonical book rows: what every import source must produce (ring 2).

A source (CSV and XLSX today; Google Sheets and Zoho later) understands its own
file shape and nothing else. It parses that shape into the typed rows below, and
everything above it -- the dry run, the reject report, the writes into the store
-- is shared. Adding a source therefore adds no rule and no arithmetic, which is
the whole point of the ``book-import-spec`` layering.

Three things this module does not let a source do:

* **Invent a value.** A row that cannot be read becomes a :class:`Reject` with a
  reason, never a zero and never a guess.
* **Carry money as anything but integer kobo.** A source converts once, on the
  way in, and rejects an amount it cannot make exact.
* **Carry totals.** A line total and a delivery total are computed from
  quantity times price when the row is written. A total the file states is only
  ever compared against that sum (``total_mismatch``), never trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

#: The payment methods the book's own vocabulary names (``mimi.provenance``).
#: A file may write "cash" for a rep's cash collection; the source normalises it.
PAYMENT_METHODS: tuple[str, ...] = ("transfer", "cash_to_rep", "pos")

# Reject reasons. A reason is a code the code checks; ``detail`` is the sentence
# the owner reads. Two rules hold everywhere: a reject names the row it refused,
# and a row that produced a reject produced no canonical row.
MISSING_FIELD = "missing_field"
BAD_DATE = "bad_date"
BAD_MONEY = "bad_money"
BAD_NUMBER = "bad_number"
BAD_PHONE = "bad_phone"
BAD_METHOD = "bad_method"
NO_LINES = "no_lines"
UNKNOWN_SHOP = "unknown_shop"
UNKNOWN_SKU = "unknown_sku"
UNKNOWN_REP = "unknown_rep"
UNKNOWN_DELIVERY = "unknown_delivery"
UNKNOWN_RETURN = "unknown_return"
DUP_IN_FILE = "dup_in_file"
DUP_IN_BOOK = "dup_in_book"
MIXES_TWO_SHOPS = "mixes_two_shops"
TOTAL_MISMATCH = "total_mismatch"
BAD_STATUS = "bad_status"
BAD_REASON = "bad_reason"


@dataclass(frozen=True)
class Provenance:
    """Where one canonical row came from, carried on every row.

    ``source`` is the transport ("csv", "xlsx"), ``external_id`` the
    distributor's own id for the row (their delivery number, their shop code),
    ``read_at`` the moment it was read, and ``where`` the place inside the file
    ("shops.csv row 4") so a reject can point at it. The store keeps the same
    facts: the external id as the record id and the run as the source
    (``mimi.connectors.book.writer``).
    """

    source: str
    external_id: str
    read_at: str
    where: str


@dataclass(frozen=True)
class Reject:
    """One row the import would not accept, with why."""

    entity: str
    external_id: str
    where: str
    reason: str
    detail: str


@dataclass(frozen=True)
class DistributorRow:
    """The book's owner: the tenant it belongs to and its own phones.

    ``owner_phones`` is the canonical (E.164) form the store matches an inbound
    message against; ``raw_owner_phones`` is exactly what the book wrote. They
    differ whenever the distributor types numbers the way people dial them, and
    the raw form is kept so the import report can say what it changed.
    """

    tenant_id: str
    name: str
    owner_phones: tuple[str, ...]
    raw_owner_phones: tuple[str, ...]
    provenance: Provenance


@dataclass(frozen=True)
class RepRow:
    rep_id: str
    name: str
    phone: str          # canonical (E.164): what an inbound message is matched by
    raw_phone: str      # as the book wrote it, so the report can name the change
    areas: tuple[str, ...]
    provenance: Provenance


@dataclass(frozen=True)
class ShopRow:
    shop_id: str
    name: str
    owner: str
    area: str
    street: str
    phone: str          # canonical (E.164): the shop's whole channel to Mimi
    raw_phone: str      # as the book wrote it, so the report can name the change
    rep_id: str
    credit_limit_kobo: int
    restock_days: int
    provenance: Provenance


@dataclass(frozen=True)
class SkuRow:
    sku: str
    name: str
    brand: str
    unit: str
    price_kobo: int
    provenance: Provenance


@dataclass(frozen=True)
class LineRow:
    """One delivery line. ``line_total_kobo`` is deliberately absent: it is
    quantity times price, summed in code when the row is written."""

    line_no: int
    sku: str
    quantity: int
    unit_price_kobo: int
    batch: str
    expires_on: date | None
    provenance: Provenance


@dataclass(frozen=True)
class DeliveryRow:
    delivery_id: str
    shop_id: str
    rep_id: str
    delivered_on: date
    minute: int
    lines: tuple[LineRow, ...]
    total_kobo: int
    provenance: Provenance


@dataclass(frozen=True)
class ReturnLineRow:
    """One returned line. ``total_kobo`` is quantity times price, summed in code,
    never read from the file."""

    line_no: int
    sku: str
    quantity: int
    unit_price_kobo: int
    reason: str
    provenance: Provenance


@dataclass(frozen=True)
class ReturnRow:
    """One return as the distributor's own book records it.

    ``status`` is the book's own state (``mimi.types.RETURN_BOOK_STATUSES``):
    a return arrives already at the stage the export says it is in, and the
    ordered lifecycle in ``mimi.returns.ledger`` is what stops it moving out of
    order later. ``value_kobo`` is the sum of the lines, computed on the way in.
    """

    return_id: str
    shop_id: str
    rep_id: str
    raised_on: date
    status: str
    reason: str
    delivery_id: str
    lines: tuple[ReturnLineRow, ...]
    value_kobo: int
    provenance: Provenance


@dataclass(frozen=True)
class ReceivableRow:
    """What one delivery owes. ``outstanding_kobo`` is computed when the row is
    written (total minus paid), never supplied."""

    delivery_id: str
    shop_id: str
    due_on: date
    total_kobo: int
    paid_kobo: int
    provenance: Provenance


@dataclass(frozen=True)
class PaymentRow:
    payment_id: str
    shop_id: str
    delivery_id: str
    paid_on: date
    amount_kobo: int
    method: str
    reference: str
    provenance: Provenance


@dataclass(frozen=True)
class Reading:
    """One source's answer: canonical rows plus the rows it refused.

    A reading is not yet a book. Nothing here has been checked against the
    tenant's existing rows; that is the writer's job, because only it can see
    the store.

    ``digest`` is a SHA-256 over what was read, so two imports of the same file
    are the same import even when the file was renamed, and an import can be
    found again from the ledger by its content.
    """

    source: str
    label: str
    digest: str
    #: Data rows the source actually read, before any rule was applied: what the
    #: import ledger reports as ``rows_read``. A re-import reads every row again
    #: and matches them all.
    rows_read: int = 0
    distributor: DistributorRow | None = None
    reps: tuple[RepRow, ...] = ()
    shops: tuple[ShopRow, ...] = ()
    skus: tuple[SkuRow, ...] = ()
    deliveries: tuple[DeliveryRow, ...] = ()
    receivables: tuple[ReceivableRow, ...] = ()
    payments: tuple[PaymentRow, ...] = ()
    returns: tuple[ReturnRow, ...] = ()
    rejects: tuple[Reject, ...] = field(default_factory=tuple)

"""Proof levels on every record (RAI-295): verified, likely, estimated.

This is the product's honesty promise in one stdlib-only leaf, so every part
of Mimi (the store, the harness ports, the stock estimator, credit, the API)
can import it without reaching into a flow. Nothing here calls a model, reads
a database or moves money.

* ``ProofLevel``: ``verified`` | ``likely`` | ``estimated``. ``incomplete``
  is a separate "not computable" state for figures that cannot be worked
  out at all; it is never a proof level.
* ``Witness``: one typed source on a record (``kind``) and the id of the row
  it came from (``source_id``).
* ``proof_level(record_type, witnesses, policy)``: the one rules function.
  Pure and versioned; a rule change bumps ``POLICY_VERSION`` and every
  record is recomputed, never edited.
* ``stamp(...)`` / ``add_witness(...)``: the stamp a row stores
  (``proof_level``, ``witnesses``, ``proof_policy_version``) and the
  append-only ``ProofEvent`` written whenever a witness lands.
* ``verified_records(rows)``: the only door credit code may read through.
  Rows without a stored ``verified`` level under a known policy are dropped
  (fail closed).
* ``legacy_level(label)``: the old trust words mapped to the new levels.

Rules, policy v1 (the table in RAI-295; Tobi reviews before any bump):

=====================  ==========================  =====================  =================
Record                 Verified                    Likely                 Estimated
=====================  ==========================  =====================  =================
payment by transfer    bank feed (or provider      alert screenshot that  shop or rep says
                       webhook) matched it         passed the fake check  so, nothing else
payment in cash        rep log + shop YES + cash   rep log + shop YES     one of them only
                       seen banked
payment by POS         POS settlement report       slip photo read        claim only
payment to another     account tagged by the       untagged, screenshot   claim only
person                 distributor + on its feed   checked or on a feed
delivery               distributor record + shop   two of the three       one only
                       YES + matched payment
stock figure           POS item data               owner-confirmed book   count or estimate
sell-through period    every count POS             every count POS or     anything else
                                                   confirmed book
stock estimate         never                       never                  always
=====================  ==========================  =====================  =================

Witnesses from the same party count once: a rep log and a distributor
invoice are both the distributor's word (proposed answer to the open
question in RAI-295; ``SAME_PARTY``). A ``bank_no_credit`` witness (the bank
window closed with no matching credit) takes a transfer back down to
estimated: levels can go down as well as up, and both are events.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar, get_args

ProofLevel = Literal["verified", "likely", "estimated"]
PROOF_LEVELS: tuple[ProofLevel, ...] = get_args(ProofLevel)
# Not a proof level: a figure that could not be worked out at all.
NOT_COMPUTABLE = "incomplete"

POLICY_V1 = "v1"
POLICY_VERSION = POLICY_V1
POLICIES: tuple[str, ...] = (POLICY_V1,)

RecordType = Literal[
    "payment_transfer",
    "payment_cash",
    "payment_pos",
    "payment_third_party",
    "delivery",
    "stock_figure",
    "sell_through",
    "stock_estimate",
]
RECORD_TYPES: tuple[RecordType, ...] = get_args(RecordType)

WitnessKind = Literal[
    # The distributor's side (one party).
    "rep_log",              # the rep logged it (cash taken, goods dropped)
    "distributor_record",   # the distributor's invoice or delivery note
    "distributor_tag",      # the distributor tagged a third-party account as theirs
    # The shop's side (one party).
    "shop_yes",             # a one-tap YES from the shop's registered number
    "shop_claim",           # the shop says it paid or got it, nothing more
    "shop_photo",           # a photo the shop sent (a delivery, a slip)
    "alert_screenshot_checked",  # a bank alert screenshot that passed the fake check
    "pos_slip_read",        # a POS slip photo read, no settlement report yet
    "book_read_confirmed",  # a book page read and confirmed by the owner
    "shop_count",           # a one-tap, typed or voice count
    # Systems (bank, provider, POS): the deciding witnesses.
    "bank_feed",            # matched on the distributor's read-only bank feed
    "provider_webhook",     # a payment provider's own webhook for the credit
    "pos_report",           # the POS settlement report or POS item data
    "payment_matched",      # a delivery's payment, itself verified
    # A model or rule estimate.
    "estimator",
    # Negative: the bank window closed with no matching credit.
    "bank_no_credit",
]
WITNESS_KINDS: tuple[WitnessKind, ...] = get_args(WitnessKind)

DISTRIBUTOR: frozenset[str] = frozenset({"rep_log", "distributor_record"})
SHOP: frozenset[str] = frozenset({
    "shop_yes", "shop_claim", "shop_photo", "alert_screenshot_checked", "pos_slip_read",
    "book_read_confirmed", "shop_count"})
BANK: frozenset[str] = frozenset({"bank_feed", "provider_webhook"})
# Witnesses that are the same party's word count once (open question in RAI-295).
SAME_PARTY: tuple[frozenset[str], ...] = (DISTRIBUTOR,)

# Plain words for each witness, for "likely (rep and shop)".
_WORD = {
    "rep_log": "rep", "distributor_record": "distributor", "distributor_tag": "tagged account",
    "shop_yes": "shop", "shop_claim": "shop's word", "shop_photo": "shop photo",
    "alert_screenshot_checked": "alert screenshot", "pos_slip_read": "POS slip",
    "book_read_confirmed": "shop book", "shop_count": "shop count", "bank_feed": "bank",
    "provider_webhook": "bank", "pos_report": "POS report", "payment_matched": "payment",
    "estimator": "estimate", "bank_no_credit": "no bank credit",
}


class ProofError(ValueError):
    """A record type, witness kind or policy this module does not know."""


@dataclass(frozen=True, order=True)
class Witness:
    kind: WitnessKind
    source_id: str

    def __post_init__(self) -> None:
        if self.kind not in WITNESS_KINDS:
            raise ProofError(f"unknown witness kind: {self.kind!r}")
        if not self.source_id:
            raise ProofError("a witness points at the row it came from: source_id is empty")

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "source_id": self.source_id}


def witness(kind: str, source_id: str) -> Witness:
    return Witness(kind, source_id)  # type: ignore[arg-type]


# ------------------------------------------------------------------ the rules


def _parties(kinds: set[str]) -> int:
    """Independent parties among the rep, the shop and the bank."""
    return sum((bool(kinds & DISTRIBUTOR), bool(kinds & {"shop_yes"}), bool(kinds & BANK)))


def _v1(record_type: str, kinds: set[str]) -> ProofLevel:
    if record_type == "stock_estimate" or "estimator" in kinds:
        return "estimated"
    if record_type == "payment_transfer":
        if kinds & BANK:
            return "verified"
        if "bank_no_credit" in kinds:
            return "estimated"
        return "likely" if "alert_screenshot_checked" in kinds else "estimated"
    if record_type == "payment_cash":
        n = _parties(kinds)
        return "verified" if n == 3 else "likely" if n == 2 else "estimated"
    if record_type == "payment_pos":
        if "pos_report" in kinds:
            return "verified"
        return "likely" if "pos_slip_read" in kinds else "estimated"
    if record_type == "payment_third_party":
        if "distributor_tag" in kinds and kinds & BANK:
            return "verified"
        if "bank_no_credit" in kinds and not kinds & BANK:
            return "estimated"
        return "likely" if kinds & (BANK | {"alert_screenshot_checked"}) else "estimated"
    if record_type == "delivery":
        n = sum((bool(kinds & DISTRIBUTOR), "shop_yes" in kinds, "payment_matched" in kinds))
        return "verified" if n == 3 else "likely" if n == 2 else "estimated"
    if record_type == "stock_figure":
        if "pos_report" in kinds:
            return "verified"
        return "likely" if "book_read_confirmed" in kinds else "estimated"
    if record_type == "sell_through":
        if not kinds:
            return "estimated"
        if kinds <= {"pos_report"}:
            return "verified"
        return "likely" if kinds <= {"pos_report", "book_read_confirmed"} else "estimated"
    raise ProofError(f"unknown record type: {record_type!r}")


def proof_level(record_type: str, witnesses: Iterable[Witness],
                policy: str = POLICY_VERSION) -> ProofLevel:
    """The level the witnesses on a record earn under ``policy``. No model call."""
    if policy not in POLICIES:
        raise ProofError(f"unknown proof policy: {policy!r}")
    if record_type not in RECORD_TYPES:
        raise ProofError(f"unknown record type: {record_type!r}")
    return _v1(record_type, {w.kind for w in witnesses})


# ------------------------------------------------------------------ stamps and events


@dataclass(frozen=True)
class ProofStamp:
    """What a record row stores: its level, the witnesses behind it, the policy."""

    record_type: RecordType
    proof_level: ProofLevel
    witnesses: tuple[Witness, ...]
    proof_policy_version: str = POLICY_VERSION

    def witnesses_json(self) -> str:
        return json.dumps([w.as_dict() for w in self.witnesses], sort_keys=True)

    def columns(self) -> dict[str, str]:
        """The three columns every record table carries."""
        return {"proof_level": self.proof_level, "witnesses": self.witnesses_json(),
                "proof_policy_version": self.proof_policy_version}

    def word(self) -> str:
        return proof_word(self.proof_level, self.witnesses)


def _dedupe(ws: Iterable[Witness]) -> tuple[Witness, ...]:
    return tuple(sorted(set(ws)))


def stamp(record_type: str, witnesses: Iterable[Witness],
          policy: str = POLICY_VERSION) -> ProofStamp:
    ws = _dedupe(witnesses)
    return ProofStamp(record_type, proof_level(record_type, ws, policy), ws,  # type: ignore[arg-type]
                      policy)


def parse_witnesses(raw: str | Sequence[Mapping[str, str]] | None) -> tuple[Witness, ...]:
    items = json.loads(raw) if isinstance(raw, str) else (raw or [])
    return _dedupe(witness(str(i["kind"]), str(i["source_id"])) for i in items)


@dataclass(frozen=True)
class ProofEvent:
    """One append-only row in ``proof_events``: a witness landed (or the policy
    changed) and the level went from ``old_level`` to ``new_level``."""

    record_type: str
    record_id: str
    old_level: str
    new_level: ProofLevel
    witness_kind: str
    witness_source: str
    policy_version: str
    at: str  # ISO 8601 with its zone

    def as_row(self) -> dict[str, str]:
        return {"record_type": self.record_type, "record_id": self.record_id,
                "old_level": self.old_level, "new_level": self.new_level,
                "witness_kind": self.witness_kind, "witness_source": self.witness_source,
                "policy_version": self.policy_version, "at": self.at}


def _check_at(at: str) -> None:
    from datetime import datetime

    if datetime.fromisoformat(at).tzinfo is None:
        raise ProofError(f"event time needs its zone: {at!r}")


def add_witness(current: ProofStamp | None, record_type: str, record_id: str,
                new: Witness, at: str,
                policy: str = POLICY_VERSION) -> tuple[ProofStamp, ProofEvent | None]:
    """Recompute after a witness lands. Returns the new stamp and the event to
    append; ``None`` when that exact witness was already on the record (a
    replay), so a retry never writes a second event."""
    _check_at(at)
    before = current.witnesses if current else ()
    if new in before:
        assert current is not None
        return current, None
    after = stamp(record_type, (*before, new), policy)
    old = current.proof_level if current else "none"
    return after, ProofEvent(record_type, record_id, old, after.proof_level, new.kind,
                             new.source_id, policy, at)


def restamp(current: ProofStamp, record_id: str, policy: str,
            at: str) -> tuple[ProofStamp, ProofEvent | None]:
    """Recompute under a new policy version (a bump never edits old rows: the
    caller writes the new stamp and the event)."""
    _check_at(at)
    after = stamp(current.record_type, current.witnesses, policy)
    if after == current:
        return current, None
    return after, ProofEvent(current.record_type, record_id, current.proof_level,
                             after.proof_level, "policy", policy, policy, at)


# ------------------------------------------------------------------ reading levels


def proof_word(level: str, witnesses: Iterable[Witness] = ()) -> str:
    """The word the console shows next to a figure, never colour alone:
    ``"likely (rep and shop)"``."""
    names: list[str] = []
    for w in sorted(witnesses):
        if w.kind == "bank_no_credit":
            continue
        word = _WORD[w.kind]
        if word not in names:
            names.append(word)
    if not names:
        return level
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"{level} ({joined})"


Row = TypeVar("Row", bound=Mapping[str, Any])


def verified_records(rows: Iterable[Row]) -> list[Row]:
    """The only records credit may read: a stored ``verified`` level under a
    known policy. A row with no level, an unknown policy or any other level
    is dropped (fail closed)."""
    out: list[Row] = []
    for r in rows:
        if r.get("proof_level") != "verified":
            continue
        if r.get("proof_policy_version", POLICY_VERSION) not in POLICIES:
            continue
        out.append(r)
    return out


def unverified_records(rows: Iterable[Row]) -> list[Row]:
    """Everything ``verified_records`` drops, for "shown, not counted" lines."""
    every = list(rows)
    keep = {id(r) for r in verified_records(every)}
    return [r for r in every if id(r) not in keep]


# ------------------------------------------------------------------ the book's records


def payment_type(method: str, *, third_party: bool = False) -> RecordType:
    if third_party:
        return "payment_third_party"
    return {"transfer": "payment_transfer", "cash_to_rep": "payment_cash", "cash": "payment_cash",
            "pos": "payment_pos"}.get(method, "payment_transfer")  # type: ignore[return-value]


def payment_stamp(payment_id: str, method: str, *, fake_alert: bool, bank_credit_id: str = "",
                  screenshot_id: str = "", shop_yes_id: str = "",
                  policy: str = POLICY_VERSION) -> ProofStamp:
    """A book payment's stamp from what the book holds about it."""
    ws: list[Witness] = []
    if bank_credit_id and not fake_alert:
        ws.append(witness("bank_feed", bank_credit_id))
    if screenshot_id:
        ws.append(witness("alert_screenshot_checked", screenshot_id))
    if shop_yes_id:
        ws.append(witness("shop_yes", shop_yes_id))
    rtype = payment_type(method)
    if rtype == "payment_cash":
        ws.append(witness("rep_log", payment_id))
    elif not ws:
        ws.append(witness("shop_claim", payment_id))
    return stamp(rtype, ws, policy)


def delivery_stamp(delivery_id: str, *, shop_yes_id: str = "", payment_id: str = "",
                   policy: str = POLICY_VERSION) -> ProofStamp:
    ws = [witness("distributor_record", delivery_id)]
    if shop_yes_id:
        ws.append(witness("shop_yes", shop_yes_id))
    if payment_id:
        ws.append(witness("payment_matched", payment_id))
    return stamp("delivery", ws, policy)


# ------------------------------------------------------------------ old labels

LEGACY: Mapping[str, str] = {
    "verified": "verified",
    "checked": "likely",
    "supplier_document": "likely",
    "likely": "likely",
    "self_reported": "estimated",
    "unverified": "estimated",
    "derived": "estimated",
    "estimated": "estimated",
    "incomplete": NOT_COMPUTABLE,
}


def legacy_level(label: str) -> str:
    """Old trust vocabulary to the new levels. ``incomplete`` stays a separate
    not-computable state. An unknown label raises: nothing is left unlabelled
    by accident."""
    key = label.strip().lower()
    if key not in LEGACY:
        raise ProofError(f"no mapping for old label {label!r}")
    return LEGACY[key]


# ------------------------------------------------------------------ asking once


def ask_due(record_key: str, asked: Iterable[str]) -> bool:
    """A shop is asked to confirm a record once. A skipped ask never lowers the
    level and is never chased: once asked, never again."""
    return record_key not in set(asked)

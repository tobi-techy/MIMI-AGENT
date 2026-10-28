"""Read a bank alert a shop pasted as text (no picture): the bank, the amount, the reference, the recipient and the time it claims. Everything it returns is the shop's claim, never proof: check the bank feed with check_payment before anyone counts it as paid. Use claimed_amount_kobo_unverified as check_payment's amount_kobo."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from mimi.proof.banks import bank_named
from mimi.proof.parse import amount_kobo, norm_ref, parse_time

_FIELD = re.compile(r"(?im)^\s*(acct|amt|amount|fee|desc|description|time|date|avl bal|status)"
                    r"\s*[:=]\s*(.+?)\s*$")
_FUTURE_SLACK_S = 300
FIGURES_FROM = "claim"  # money from shop text: no *_kobo output, so it is never a figure source


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=5, max_length=1200,
                      description="The alert exactly as the shop pasted it.")


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")

    readable: bool
    bank: str = ""
    layout_known: bool = False
    claimed_amount_kobo_unverified: int = 0
    claimed_amount_naira: str = ""
    reference: str = ""
    reference_fits_bank: bool = False
    recipient: str = ""
    claimed_time: str = ""  # ISO, Africa/Lagos
    time_status: str = "missing"  # ok | missing | impossible | future
    is_debit: bool = False
    warnings: list[str] = Field(default_factory=list)
    next_step: str


def _naira(kobo: int) -> str:
    return f"N{kobo / 100:,.0f}" if kobo % 100 == 0 else f"N{kobo / 100:,.2f}"


def run(args: In, now: datetime) -> Out:
    text = args.text.replace("\r", "")
    fields: dict[str, str] = {}
    for m in _FIELD.finditer(text):
        fields.setdefault(m.group(1).lower(), m.group(2))
    first = text.strip().splitlines()[0] if text.strip() else ""
    bank = bank_named(first) or bank_named(text)
    warnings: list[str] = []
    printed = fields.get("amt") or fields.get("amount") or ""
    kobo = amount_kobo(printed) or 0
    if not kobo:
        warnings.append("no amount could be read")
    desc = fields.get("desc") or fields.get("description") or ""
    recipient, ref = "", ""
    if "/" in desc:
        head, _, tail = desc.partition("/")
        ref = norm_ref(tail)
        recipient = re.sub(r"^(?:TRF|TRANSFER)\s+TO\s+", "", head.strip(), flags=re.I)
    fits = bool(bank and ref and bank.reference_ok(ref))
    if bank and ref and not fits:
        warnings.append(f"reference does not fit {bank.name}'s reference format")
    t = parse_time(fields.get("time") or fields.get("date"), bank)
    status, when = t.status, ""
    if t.at is not None:
        when = t.at.isoformat()
        if (t.at - now).total_seconds() > _FUTURE_SLACK_S:
            status = "future"
            warnings.append("the alert's time is in the future")
    is_debit = bool(re.search(r"\b(debit|dr)\b", f"{first} {printed}", re.I))
    readable = bool(kobo)
    step = ("Call check_payment with this shop and claimed_amount_kobo_unverified "
            "(and the reference). Only a bank credit counts as paid."
            if readable else "Ask the sender for the amount, or a screenshot, with ask_question.")
    return Out(readable=readable, bank=bank.name if bank else "",
               layout_known=bool(bank and bank.layout == "sms_alert"),
               claimed_amount_kobo_unverified=kobo,
               claimed_amount_naira=_naira(kobo) if kobo else "", reference=ref,
               reference_fits_bank=fits, recipient=recipient, claimed_time=when,
               time_status=status, is_debit=is_debit, warnings=warnings, next_step=step)

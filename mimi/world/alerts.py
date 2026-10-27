"""Bank alert and transfer receipt text, in the layouts Nigerian shops forward.

Every bank here is fictional (``mimi.proof.banks``). The layouts follow the
usual shapes (SMS debit alerts for the SMS banks, in-app "Transaction
Successful" receipts for the app banks). Every
account number is fictional and masked; session ids start with ``999`` so a
synthetic one can never be confused with a real one.

A fake alert carries telltale signs a careful reader (or Mimi, matching against
the bank feed) would catch: the narration in the wrong format for that bank,
an amount that never shows up in Coral Lane's bank feed, an odd timestamp.
"""

from __future__ import annotations

import random
from datetime import datetime

from mimi.world.model import BankAlert

PAYEE = "CORAL LANE TRADING"
PAYEE_ACCOUNT = "00****0000"
APP_BANKS = frozenset({"Lagoonpay", "Harmattan MFB", "Kestrel Bank"})


def naira(kobo: int) -> str:
    """NGN figure from kobo, computed in code: 5345000 -> 53,450.00."""
    return f"{kobo // 100:,}.{kobo % 100:02d}"


def _acct(rng: random.Random) -> str:
    return f"{rng.randint(10, 99)}****{rng.randint(1000, 9999)}"


def _session(rng: random.Random) -> str:
    return "999" + "".join(str(rng.randint(0, 9)) for _ in range(27))


def alert_text(
    rng: random.Random,
    *,
    bank: str,
    sender: str,
    amount_kobo: int,
    reference: str,
    at: datetime,
    balance_kobo: int,
) -> str:
    amt = naira(amount_kobo)
    if bank == "Ashgrove Bank":
        return (
            f"Acct: {_acct(rng)}\nAmt: NGN{amt} DR\n"
            f"Desc: TRF/{PAYEE}/{reference}\n"
            f"Time: {at:%d/%m/%Y %H:%M}\nAvail Bal: NGN{naira(balance_kobo)}"
        )
    if bank == "Marlwood Bank":
        return (
            f"Debit\nAmt: NGN{amt}\n"
            f"Acc: {rng.randint(300, 399)}XXXX{rng.randint(100, 999)}\n"
            f"Desc: {PAYEE}/TRF/{reference}\n"
            f"DT: {at:%d%b%y %H:%M}\nBal: NGN{naira(balance_kobo)}".upper()
        )
    if bank == "Ironleaf Bank":
        return (
            f"Debit\nAmt: NGN{amt}\n"
            f"Acc: 0{rng.randint(1, 9)}XXXXX{rng.randint(100, 999)}\n"
            f"Desc: NIP TRF TO {PAYEE} {reference}\n"
            f"Date: {at:%d/%m/%Y %H:%M}\nBal: NGN{naira(balance_kobo)}"
        )
    # Lagoonpay, Harmattan MFB, Kestrel Bank: an in-app receipt.
    return (
        f"{bank} Transaction Receipt\nTransaction Successful\n\u20a6{amt}\n"
        f"Recipient: {PAYEE} | Demo Bank {PAYEE_ACCOUNT}\n"
        f"Sender: {sender.upper()}\nNarration: {reference}\n"
        f"Session ID: {_session(rng)}\n"
        f"Date: {at:%b} {at.day}, {at.year} {at:%H:%M:%S}"
    )


def genuine_alert(
    rng: random.Random,
    *,
    bank: str,
    sender: str,
    amount_kobo: int,
    reference: str,
    at: datetime,
) -> BankAlert:
    balance = rng.randint(2_000, 400_000) * 100 + rng.randint(0, 99)
    text = alert_text(
        rng,
        bank=bank,
        sender=sender,
        amount_kobo=amount_kobo,
        reference=reference,
        at=at,
        balance_kobo=balance,
    )
    return BankAlert(bank=bank, text=text, sent_at=at.isoformat())


def fake_alert(
    rng: random.Random,
    *,
    bank: str,
    sender: str,
    amount_kobo: int,
    at: datetime,
) -> BankAlert:
    """A forged alert. Never in the bank feed; one or two more telltales."""
    telltales = ["amount not in bank feed"]
    odd = rng.random() < 0.6
    if odd:
        # 3am "transfers" and seconds on a bank that never prints them.
        at = at.replace(hour=rng.choice((2, 3, 4)), minute=rng.randint(0, 59))
        telltales.append("odd timestamp")
    amt = f"{amount_kobo // 100}"  # no thousands separator, no kobo
    if bank in APP_BANKS:
        text = (
            f"Transaction Successful!!\n\u20a6 {amt}\nRecipient: {PAYEE}\n"
            f"Sender: {sender}\nSession ID: {_session(rng)[:18]}\n"
            f"Date: {at:%d-%m-%Y %H:%M:%S}"
        )
    else:
        text = (
            f"Credit Alert!\nAcct: {_acct(rng)}\nAmt: NGN {amt}\n"
            f"Desc: Transfer from {sender} successful\n"
            f"Date: {at:%d-%m-%Y %H:%M:%S}"
        )
    telltales.append("narration format")
    return BankAlert(
        bank=bank, text=text, sent_at=at.isoformat(), telltales=tuple(telltales)
    )

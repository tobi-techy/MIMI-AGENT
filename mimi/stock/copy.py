"""What Mimi says about stock. Plain English, fixed templates, figures from code.

Mimi's own copy is plain English (RAI-267); the shop may answer in any
language. ``figures_ok`` is the verifier for the shop message: the only
quantity in a restock thanks is the draft quantity, and the question carries
only its bucket labels.
"""

from __future__ import annotations

import re

from mimi.stock.items import display_name, unit_word

BUTTONS: tuple[str, str, str] = ("0-2", "3-5", "6+")


def ask_text(item_name: str, unit: str) -> str:
    return f"{display_name(item_name)} left? {unit_word(unit).capitalize()}: 0-2 | 3-5 | 6+"


def ask_again_text(item_name: str, unit: str) -> str:
    return (
        f"Sorry, I didn't catch that. How many {unit_word(unit)} of {display_name(item_name)} "
        "are left? Tap 0-2, 3-5 or 6+, or type the number."
    )


def thanks_draft_text(quantity: int, unit: str, distributor: str) -> str:
    return (
        f"Thanks. I've asked {distributor} to prepare {quantity} {unit_word(unit, quantity)} "
        "for you. They'll confirm before delivery."
    )


def thanks_noted_text() -> str:
    return "Thanks, noted. Nothing to order yet."


_QTY = re.compile(r"\bprepare (\d+) ")


def figures_ok(text: str, quantity: int) -> bool:
    """True when a thanks message names exactly the draft quantity."""
    found = [int(x) for x in _QTY.findall(text)]
    return found == [quantity]

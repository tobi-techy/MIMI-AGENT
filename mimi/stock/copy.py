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
    return (f"Quick stock check: how many {unit_word(unit)} of {display_name(item_name)} do you "
            "have left? 0-2 | 3-5 | 6+")


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


def revised_draft_text(quantity: int, unit: str, distributor: str) -> str:
    return (
        f"Okay. I've changed it to {quantity} {unit_word(unit, quantity)}. {distributor} will "
        "confirm before delivery."
    )


_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
          "twenty": 20, "meji": 2, "meta": 3, "merin": 4, "marun": 5, "mefa": 6, "meje": 7,
          "mejo": 8, "mesan": 9, "mewa": 10}


def numbers_in(text: str) -> set[int]:
    """Whole numbers a shop wrote or said ("6", "six", "mefa"); code only."""
    import unicodedata

    t = "".join(c for c in unicodedata.normalize("NFKD", text.lower())
                if not unicodedata.combining(c))
    found = {int(x) for x in re.findall(r"(?<![\d.,])\d{1,3}(?![\d,]*\d{3})", t)}
    found |= {n for w, n in _WORDS.items() if re.search(rf"\b{w}\b", t)}
    return found


_NEGATED = re.compile(r"\b(?:not|no be|nor|instead of|rather than|kii se|ki i se)\s+[\w-]+", re.I)


def wanted_numbers(text: str) -> set[int]:
    """The numbers a correction asks for: "make it 6, not 4" gives {6}."""
    return numbers_in(_NEGATED.sub(" ", text))


def thanks_noted_text() -> str:
    return "Thanks, noted. Nothing to order yet."


_QTY = re.compile(r"\bprepare (\d+) ")


def figures_ok(text: str, quantity: int) -> bool:
    """True when a thanks message names exactly the draft quantity."""
    found = [int(x) for x in _QTY.findall(text)]
    return found == [quantity]

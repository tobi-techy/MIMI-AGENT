"""What Mimi says for the weekly check-in. Plain English, fixed templates,
figures from code. Mimi's own copy is plain English; the shop may answer in
English, Pidgin, Yoruba, Igbo or Hausa.

Template names are for Meta WhatsApp approval (RAI-254); the copy here is
what is submitted. Listed in ``docs/STOCK.md``.
"""

from __future__ import annotations

# (template name, purpose)
TEMPLATES: tuple[tuple[str, str], ...] = (
    ("mimi_weekly_checkin", "the Monday one-tap check-in"),
    ("mimi_low_stock_count", "the low-stock one-tap count"),
    ("mimi_checkin_confirm", "the count confirmation and restock offer"),
)

CHECKIN_BUTTONS: tuple[str, str, str] = ("No", "Yes, voice note or receipt", "Skip")
COUNT_BUTTONS: tuple[str, str, str] = ("0-2", "3-5", "6+")


def checkin_text(shop_name: str) -> str:
    name = f" {shop_name}" if shop_name else ""
    return (
        f"Morning{name}. One quick one. Bought anything from anyone else "
        "this week? [No] [Yes, send a voice note or receipt] [Skip]"
    )


def low_stock_text(item_name: str, unit_word: str) -> str:
    return f"{item_name} left? {unit_word}: 0-2 | 3-5 | 6+"


def confirm_count_text(item_name: str, bucket: str, restock_quantity: int, unit_word: str) -> str:
    if restock_quantity > 0:
        return (
            f"Got it: {item_name} {bucket}. Add {restock_quantity} {unit_word} "
            "to the next van? [Yes] [No]"
        )
    return f"Got it: {item_name} {bucket}. Thanks."


def confirm_supplier_none_text() -> str:
    return "Okay, thanks. Nothing to add."


def confirm_supplier_text(lines: int) -> str:
    s = "" if lines == 1 else "s"
    return f"Got it: {lines} line{s} added from the other shop. Thanks."


def ask_clarify_supplier_text() -> str:
    return (
        "Sorry, I did not catch that. What did you buy, and how many? "
        "A voice note or a receipt photo works too. [Skip]"
    )


def ask_clarify_count_text(item_name: str) -> str:
    return (
        f"Sorry, I did not catch that. How many {item_name} are left? "
        "Tap 0-2, 3-5 or 6+, or type the number. [Skip]"
    )


def confirm_line_text(item_name: str, quantity: int, unit_word: str) -> str:
    return f"Did you say {quantity} {unit_word} of {item_name}? [Yes] [No]"


def streak_text(weeks: int) -> str:
    return (
        f"{weeks} weeks in a row. A steady record like this helps when "
        "you ask for credit. Your distributor still decides every limit."
    )


def connect_offer_text() -> str:
    return (
        "We have missed each other a few weeks. Want to connect your "
        "shop once, so you never have to answer this? [Connect] [No]"
    )


def stopped_text() -> str:
    return "Okay, no more check-ins. Message us any time you need goods."


def skip_text() -> str:
    return "Okay, maybe next week."


__all__ = [
    "CHECKIN_BUTTONS",
    "COUNT_BUTTONS",
    "TEMPLATES",
    "ask_clarify_count_text",
    "ask_clarify_supplier_text",
    "checkin_text",
    "confirm_count_text",
    "confirm_line_text",
    "confirm_supplier_none_text",
    "confirm_supplier_text",
    "connect_offer_text",
    "low_stock_text",
    "skip_text",
    "stopped_text",
    "streak_text",
]

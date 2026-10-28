"""What Credit says to a shop. Written by code; every figure comes from code.

The shop never hears the recommendation: whatever the outcome, the request
goes to the distributor and the distributor decides (RAI-271 expected
result). Plain words, short sentences.
"""

from __future__ import annotations

from mimi.credit.shelf import ShelfCount
from mimi.harness.loop import naira_text


def ask_photo(distributor: str) -> str:
    return (f"I can ask {distributor} for you. Please send one clear photo of your shelf, "
            "with the items you sell facing the camera.")


def ask_photo_again(why: str) -> str:
    return (f"Sorry, {why}. Please send one more photo of your shelf, in good light and "
            "held still.")


def counted(shelf: ShelfCount, about_kobo: int, distributor: str) -> str:
    items = [f"{i.about} {i.name} {i.pieces_word if i.about != 1 else i.piece}"
             for i in shelf.items[:3]]
    if len(items) == 1:
        listed = items[0]
    else:
        listed = ", ".join(items[:-1]) + " and " + items[-1]
    more = len(shelf.items) - 3
    if more > 0:
        listed += f", plus {more} more {'item' if more == 1 else 'items'}"
    return (f"I counted about {listed}, worth about {naira_text(about_kobo)} at "
            f"{distributor} prices. I've sent your request with this to {distributor}. "
            "They decide the limit.")


def no_count(distributor: str) -> str:
    return (f"I could not count from the photo, so I've sent your request to {distributor} "
            "without it. They decide the limit.")


def nothing_counted(distributor: str) -> str:
    return (f"I could not see {distributor} items on that shelf, so I've sent your request "
            f"to {distributor} with your payment history only. They decide the limit.")


def history_only(distributor: str) -> str:
    return (f"I've sent your request to {distributor} with your payment history. "
            "They decide the limit.")


def already_waiting(distributor: str) -> str:
    return f"Your request is already with {distributor}. They decide the limit."


def needs_consent(distributor: str) -> str:
    return (f"To ask {distributor} for more credit, they need to see your payment history "
            "with them, and you have not shared it. Reply SHARE if you are happy to share "
            "it, or tell me if you'd rather not.")

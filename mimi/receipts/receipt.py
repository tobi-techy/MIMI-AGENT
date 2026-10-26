"""The receipt and clarification texts. All figures come from code.

The receipt lists each line (quantity, item, line total) and the total, all
formatted here from integer kobo, and always ends "Reply YES to confirm".
What the shop's reply means is read by the model (``verdict.py``) and guarded
in code (``decide.py``).

Mimi's own copy is plain English. The one exception is the clarifying
question: when the shop wrote in Pidgin, Yoruba, Igbo or Hausa, Mimi asks in
that language (fixed templates below, never model-written). Mixed or any other
language gets plain English. The request to type a reply after a voice note
Mimi could not use is plain English too.
"""

from __future__ import annotations

import hashlib
import re

from mimi.world.model import Delivery

YES_LINE = "Reply YES to confirm."


def naira(kobo: int) -> str:
    """N53,450 or N53,450.50, computed in code from kobo."""
    whole, part = divmod(kobo, 100)
    return f"N{whole:,}" + (f".{part:02d}" if part else "")


def receipt_text(delivery: Delivery, shop_name: str, distributor: str) -> str:
    rows = [
        f"- {line.quantity} {line.unit} {line.name}: {naira(line.line_total_kobo)}"
        for line in delivery.lines
    ]
    total = sum(line.line_total_kobo for line in delivery.lines)
    return (
        f"{distributor} delivery receipt for {shop_name}\n"
        f"Ref {delivery.delivery_id}, {delivery.delivered_on}\n"
        + "\n".join(rows)
        + f"\nTotal: {naira(total)}\n{YES_LINE}"
    )


_CLARIFY: dict[str, str] = {
    "english": (
        "Just to check on delivery {ref}: did every item on the receipt arrive? "
        "Reply YES to confirm. If something is different, tell me what."
    ),
    "pidgin": (
        "Abeg, make I check delivery {ref}: everything wey dey the receipt reach "
        "you complete? If e complete, reply YES. If something no complete, tell "
        "me wetin."
    ),
    "yoruba": (
        "Ẹ jọ̀ọ́, nípa delivery {ref}: ṣé gbogbo "
        "ẹrù tó wà lórí receipt ti dé pé? "
        "Tí ó bá pé, ẹ fèsì YES. Tí nǹkan "
        "kan bá yàtọ̀, ẹ sọ fún mi."
    ),
    "igbo": (
        "Biko, maka delivery {ref}: ihe niile dị na receipt ọ bịarutere "
        "gị zuru ezu? Ọ bụrụ na ọ zuru, zaa YES. Ọ "
        "bụrụ na ihe dị iche, gwa m."
    ),
    "hausa": (
        "Don Allah, game da delivery {ref}: duk kayan da ke cikin receipt sun iso "
        "cikakke? Idan sun cika, ka amsa da YES. Idan akwai abin da ya bambanta, "
        "ka gaya mini."
    ),
}


def clarify_text(delivery: Delivery, language: str = "english") -> str:
    """One clarifying question, in the shop's language when it wrote in one
    of the templated languages, otherwise plain English."""
    template = _CLARIFY.get(language, _CLARIFY["english"])
    return template.format(ref=delivery.delivery_id)


TYPE_REQUEST = (
    "I could not understand your voice note about delivery {ref}. Please type "
    "your reply: YES if every item on the receipt arrived, or tell me what is "
    "different."
)


def type_request_text(delivery: Delivery) -> str:
    """Sent when a voice note could not be transcribed, or its transcript is
    too doubtful to confirm on. Plain English, never model-written."""
    return TYPE_REQUEST.format(ref=delivery.delivery_id)


def receipt_digest(text: str) -> str:
    return "rcpt_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


_KW_STOP = frozenset({"pack", "refill", "with", "from"})


def receipt_keywords(delivery: Delivery, limit: int = 20) -> tuple[str, ...]:
    """Brand and product words from the receipt, to bias speech recognition
    toward them ("Sunrise", "Noodles", "Sardines"), plus "carton"."""
    out: list[str] = []
    for line in delivery.lines:
        for word in re.findall(r"[A-Za-z][A-Za-z-]{3,}", line.name):
            if word.casefold() not in _KW_STOP and word not in out:
                out.append(word)
    if "carton" not in out:
        out.append("carton")
    return tuple(out[:limit])

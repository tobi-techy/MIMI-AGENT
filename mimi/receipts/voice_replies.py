"""What Mimi says in a voice reply, for shops that turned voice replies on.

Only Mimi's short replies are spoken: the clarifying question, the request
to type, a promise read-back and a promise question. The receipt itself is
a list of lines and figures, so it is only ever text.

The scripts are fixed templates, never model-written:

* English and Pidgin (Pidgin is spoken by an English-language voice) say
  the amount and the day in words ("twenty thousand naira on Tuesday 27
  October"), built in code from the same integer kobo and date as the text;
* Yoruba, Igbo and Hausa say the question in that language and point to
  the text for the figures ("I wrote the amount and the day in this
  message"): a speech engine reading digits in those languages is too easy
  to mishear, and a figure must never exist only as sound.

``voice_fields`` always checks in code that every amount and day in the
script is also in the text; if not, the reply goes as text only.

The Yoruba, Igbo and Hausa lines were written for this repo and have not
been reviewed by a native speaker yet (``docs/VOICE.md``).
"""

from __future__ import annotations

from datetime import date

from mimi.voice.prefs import VoicePreference
from mimi.voice.promise import find_amounts, find_dates, fold, naira_words, spoken_date

SPOKEN_PURPOSES = frozenset(
    {"receipt_clarify", "receipt_type_request", "promise_readback", "promise_clarify"}
)

_SCRIPTS: dict[str, dict[str, str]] = {
    "english": {
        "receipt_clarify": (
            "Just to check on your delivery: did every item on the receipt arrive? "
            "Reply YES to confirm, or tell me what is different."
        ),
        "receipt_type_request": (
            "I could not understand your voice note. Please type your reply: YES if "
            "every item arrived, or tell me what is different."
        ),
        "promise_readback": (
            "You said you will pay {amount} on {day}. If that is right, reply YES. "
            "I only note your promise. I do not take money."
        ),
        "promise_clarify": "Please tell me how much you will pay, and on what day.",
        "promise_clarify_day": "On what day will you pay {amount}?",
    },
    "pidgin": {
        "receipt_clarify": (
            "Abeg, everything wey dey the receipt reach you complete? If e complete, "
            "reply YES. If something no complete, tell me wetin."
        ),
        "receipt_type_request": (
            "I no fit understand your voice note. Abeg type your answer: YES if "
            "everything reach, or tell me wetin no complete."
        ),
        "promise_readback": (
            "You talk say you go pay {amount} on {day}. If na so, reply YES. Na only "
            "your promise I dey write, I no dey collect money."
        ),
        "promise_clarify": "Abeg tell me how much you go pay, and which day.",
        "promise_clarify_day": "Which day you go pay {amount}?",
    },
    "yoruba": {
        "receipt_clarify": (
            "Ẹ jọ̀ọ́, ṣé gbogbo ẹrù tó wà lórí receipt ti dé pé? Tí ó bá pé, ẹ fèsì YES. "
            "Tí nǹkan kan bá yàtọ̀, ẹ sọ fún mi."
        ),
        "receipt_type_request": (
            "Ẹ jọ̀ọ́, mi ò gbọ́ voice note yín dáadáa. Ẹ kọ èsì yín sílẹ̀: YES tí gbogbo "
            "ẹrù bá dé, tàbí ẹ sọ ohun tó yàtọ̀."
        ),
        "promise_readback": (
            "Ẹ ṣé. Mo ti kọ iye owó àti ọjọ́ tí ẹ sọ pé ẹ máa san sínú ọ̀rọ̀ yìí. Ẹ jọ̀ọ́ ẹ "
            "kà á. Tí ó bá tọ̀nà, ẹ fèsì YES."
        ),
        "promise_clarify": "Ẹ jọ̀ọ́, ẹ sọ iye owó tí ẹ máa san àti ọjọ́ tí ẹ máa san án.",
    },
    "igbo": {
        "receipt_clarify": (
            "Biko, ihe niile dị na receipt ọ bịarutere gị zuru ezu? Ọ bụrụ na ọ zuru, "
            "zaa YES. Ọ bụrụ na ihe dị iche, gwa m."
        ),
        "receipt_type_request": (
            "Biko, aghọtaghị m voice note gị nke ọma. Biko dee azịza gị: YES ma ọ bụrụ "
            "na ihe niile bịara, ma ọ bụ gwa m ihe dị iche."
        ),
        "promise_readback": (
            "Daalụ. Edere m ego na ụbọchị i kwuru na ị ga-akwụ n'ozi a. Biko gụọ ya. "
            "Ọ bụrụ na ọ dị mma, zaa YES."
        ),
        "promise_clarify": "Biko, gwa m ego ole ị ga-akwụ na ụbọchị ị ga-akwụ ya.",
    },
    "hausa": {
        "receipt_clarify": (
            "Don Allah, duk kayan da ke cikin receipt sun iso cikakke? Idan sun cika, "
            "ka amsa da YES. Idan akwai abin da ya bambanta, ka gaya mini."
        ),
        "receipt_type_request": (
            "Don Allah, ban fahimci saƙon muryarka ba. Ka rubuta amsarka: YES idan duk "
            "kayan sun iso, ko ka gaya mini abin da ya bambanta."
        ),
        "promise_readback": (
            "Na gode. Na rubuta kuɗin da ranar da ka ce za ka biya a cikin saƙon nan. "
            "Don Allah ka karanta. Idan daidai ne, ka amsa da YES."
        ),
        "promise_clarify": "Don Allah, ka gaya mini nawa za ka biya da kuma ranar da za ka biya.",
    },
}


def spoken_script(
    purpose: str,
    language: str,
    *,
    amount_kobo: int | None = None,
    due_on: date | None = None,
) -> str:
    """The fixed script for one reply, or "" when it is not spoken."""
    table = _SCRIPTS.get(language)
    if table is None or purpose not in SPOKEN_PURPOSES:
        return ""
    if purpose == "promise_readback":
        if amount_kobo is None or due_on is None:
            return ""
        return table["promise_readback"].format(
            amount=naira_words(amount_kobo), day=spoken_date(due_on)
        )
    if purpose == "promise_clarify" and amount_kobo is not None and "promise_clarify_day" in table:
        return table["promise_clarify_day"].format(amount=naira_words(amount_kobo))
    return table[purpose]


def figures_match(text: str, script: str, today: date) -> bool:
    """Every amount and day the script says is also written in the text."""
    t, s = fold(text), fold(script)
    text_kobo = {a.kobo for a in find_amounts(t) if a.kobo is not None}
    said_kobo = {a.kobo for a in find_amounts(s) if a.kobo is not None}
    text_days = {d.on for d in find_dates(t, today) if d.on is not None}
    said_days = {d.on for d in find_dates(s, today) if d.on is not None}
    return said_kobo <= text_kobo and said_days <= text_days


def voice_fields(
    pref: VoicePreference | None,
    purpose: str,
    text: str,
    *,
    today: date,
    amount_kobo: int | None = None,
    due_on: date | None = None,
) -> tuple[str, str]:
    """(voice_script, voice_language) for a ``SendShopMessage``, or ("", "")
    when the shop has not turned voice on, the reply is not a short one, or
    the script's figures are not all in the text."""
    if pref is None or not pref.speaks:
        return "", ""
    script = spoken_script(purpose, pref.language, amount_kobo=amount_kobo, due_on=due_on)
    if not script or not figures_match(text, script, today):
        return "", ""
    return script, pref.language

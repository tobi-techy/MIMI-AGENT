"""What Mimi says in a voice reply, for shops that turned voice replies on.

Only Mimi's short replies are spoken: the clarifying question, the request
to type, a promise read-back and a promise question. The receipt itself is
a list of lines and figures, so it is only ever text.

A voice reply is the text reply read aloud (``mimi.voice.render``), never a
separate message: the same words, with each amount and written date said in
words from the same integer kobo and date. ``voice_fields`` checks in code
that the script says exactly the text's figures, and the flow's own
code-checked amount and day for a read-back; otherwise the reply goes as
text only. The text always goes.
"""

from __future__ import annotations

from datetime import date

from mimi.voice.prefs import VoicePreference
from mimi.voice.render import figures, render_voice

SPOKEN_PURPOSES = frozenset(
    {"receipt_clarify", "receipt_type_request", "promise_readback", "promise_clarify"}
)


def figures_match(text: str, script: str, today: date) -> bool:
    """The script says exactly the amounts and days written in the text."""
    tk, td, t_doubt = figures(text, today)
    sk, sd, s_doubt = figures(script, today)
    return not t_doubt and not s_doubt and tk == sk and set(td) == set(sd)


def voice_fields(
    pref: VoicePreference | None,
    purpose: str,
    text: str,
    *,
    today: date,
    amount_kobo: int | None = None,
    due_on: date | None = None,
) -> tuple[str, str]:
    """(voice_script, voice) for a ``SendShopMessage``, or ("", "") when the
    shop has not turned voice on, the reply is not a short one, or the
    script's figures are not the text's and the flow's."""
    if pref is None or not pref.speaks or purpose not in SPOKEN_PURPOSES:
        return "", ""
    readback = purpose == "promise_readback"
    vs, _why = render_voice(
        text,
        pref.language,
        today=today,
        expect_kobo=(amount_kobo,) if readback else None,
        expect_days=(due_on,) if readback else None,
    )
    if vs is None:
        return "", ""
    return vs.script, vs.voice

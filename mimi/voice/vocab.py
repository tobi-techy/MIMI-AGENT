"""Money and day words that bias a transcriber towards what shops say about paying.

Muse Voice Transcribe takes ``keywords`` and Spitch takes ``special_words``.
``PROMISE_VOCAB`` is sent with a voice note in the shop's language, on top of
any SKU words from the receipt, to the transcribers named in
``VOCAB_PROVIDERS``.

Measured on the dev split of ``eval/voice_values`` (28 Oct 2026, Muse only):
with these words Muse read the amount and day right on 12/20 dev clips, 8/20
without; on English it stopped hearing "I will pay" as "there will be". The
Spitch side is not measured yet (the Spitch account was out of credit), so
Spitch gets them only when ``MIMI_SPITCH_VOCAB=1``. Changing a list changes
the recorded requests: re-record ``eval/voice_values`` with ``--live``.
"""

from __future__ import annotations

PROMISE_VOCAB: dict[str, tuple[str, ...]] = {
    "en": ("naira", "thousand", "hundred", "pay", "balance", "tomorrow", "next tomorrow",
           "Friday", "Saturday", "weekend"),
    "pcm": ("naira", "thousand", "pay", "balance", "abeg", "tomorrow", "next tomorrow",
            "Friday", "I go pay", "I don pay"),
    "yo": ("naira", "thousand", "pay", "san", "ọ̀la", "òní", "ọ̀túnla", "ẹgbẹ̀rún", "màá san",
           "yesterday"),
}
VOCAB_PROVIDERS: tuple[str, ...] = ("muse",)


def with_vocab(keywords: tuple[str, ...], hint: str | None) -> tuple[str, ...]:
    extra = PROMISE_VOCAB.get(hint or "", ())
    return tuple(keywords) + tuple(w for w in extra if w not in keywords)

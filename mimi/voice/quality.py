"""Code checks on a transcript, because neither speech API gives a confidence.

A transcript is marked ``low`` (read, but never allowed to produce consent)
when:

* the API reported a confidence below ``MIN_CONFIDENCE``;
* fewer than ``MIN_WORDS_PER_S`` words for a clip longer than 3 seconds (most
  of the note was not understood, or it is mostly noise);
* more than ``MAX_WORDS_PER_S`` words a second (not humanly possible: a
  decoding loop);
* under half of its characters are letters or digits;
* one word repeated ``MAX_REPEAT`` or more times in a row (a decoding loop).

An empty transcript is ``failed``. These checks only ever make a transcript
weaker; none can make one stronger.
"""

from __future__ import annotations

import re
from dataclasses import replace

from mimi.voice.base import Transcript

MIN_CONFIDENCE = 0.6
MIN_WORDS_PER_S = 0.25
MAX_WORDS_PER_S = 7.0
MAX_REPEAT = 6


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def assess(t: Transcript) -> Transcript:
    if t.status == "failed":
        return t
    text = t.text.strip()
    if not text:
        return replace(t, text="", status="failed", reason="empty transcript")
    words = _words(text)
    reason = ""
    if t.confidence is not None and t.confidence < MIN_CONFIDENCE:
        reason = f"API confidence {t.confidence:.2f} below {MIN_CONFIDENCE}"
    elif not words:
        reason = "no words in transcript"
    elif t.duration_s and t.duration_s > 3 and len(words) / t.duration_s < MIN_WORDS_PER_S:
        reason = f"{len(words)} words for {t.duration_s:.1f}s of audio"
    elif t.duration_s and len(words) / max(t.duration_s, 0.5) > MAX_WORDS_PER_S:
        reason = f"{len(words)} words for {t.duration_s:.1f}s of audio"
    elif sum(ch.isalnum() for ch in text) < 0.5 * len(text.replace(" ", "")):
        reason = "mostly symbols"
    else:
        run = 1
        for a, b in zip(words, words[1:], strict=False):
            run = run + 1 if a == b else 1
            if run >= MAX_REPEAT:
                reason = f"'{a}' repeated {run} times"
                break
    if reason:
        return replace(t, text=text, status="low", reason=reason)
    return replace(t, text=text, status="ok", reason="")

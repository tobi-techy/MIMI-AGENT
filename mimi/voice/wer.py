"""Word error rate, in pure Python.

Both texts are normalised before counting: case folded, punctuation removed,
the number words zero to twenty (English and Pidgin) turned into digits, and
"oh" read as the Pidgin particle "o". ``folded=True`` also removes Yoruba and
Igbo tone marks and dots below, so a transcript that writes "e se" for
"ẹ ṣé" is not counted wrong; both figures are reported.

WER = (substitutions + deletions + insertions) / reference words.
"""

from __future__ import annotations

import re
import unicodedata

_NUM = {
    w: str(i)
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
    )
}
_SAME = {"oh": "o"}


def normalise(text: str, *, folded: bool = False) -> list[str]:
    t = unicodedata.normalize("NFC", text).casefold()
    if folded:
        t = "".join(ch for ch in unicodedata.normalize("NFD", t) if not unicodedata.combining(ch))
    words = re.findall(r"\w+", t)
    return [_SAME.get(_NUM.get(w, w), _NUM.get(w, w)) for w in words]


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, start=1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1]


def wer(reference: str, hypothesis: str, *, folded: bool = False) -> float:
    ref = normalise(reference, folded=folded)
    hyp = normalise(hypothesis, folded=folded)
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


def corpus_wer(pairs: list[tuple[str, str]], *, folded: bool = False) -> float:
    """Errors over all reference words (not the mean of per-clip WERs)."""
    errs = words = 0
    for ref, hyp in pairs:
        r = normalise(ref, folded=folded)
        errs += edit_distance(r, normalise(hyp, folded=folded))
        words += len(r)
    return errs / words if words else 0.0

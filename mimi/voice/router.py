"""Which transcriber hears which voice note, and the fallback when one fails.

``VoiceRouter(providers, policy)`` tries the providers named for the clip's
language hint, in order:

* the first ``ok`` transcript wins;
* a ``low`` one is kept and the next provider is tried; if none does better,
  the first ``low`` transcript is returned (it can never produce consent);
* a ``failed`` one moves on to the next provider; if all fail, the result is
  ``failed`` with every reason, and Mimi asks the shop to type.

Policy: Tobi's call (26 Oct 2026) was Spitch first for Yoruba, Igbo and
Hausa and as the default for Nigerian voice notes, Muse Voice Transcribe as
the fallback, with the final pick decided by the voice eval. The live eval
(``eval/voice/voice.report.json``, 16 synthetic clips) gave:

* Yoruba, Igbo, Hausa, Yoruba-English: Spitch far lower WER (Muse does not
  list these languages); Spitch first.
* Pidgin: Spitch 27% WER, Muse 32%, and Muse once heard "don land" as
  "don't land", which flips the meaning; Spitch first.
* English: Muse 5% WER, Spitch 16% (same verdicts, 4 of 4); Muse first.
* No hint: Spitch first (it covers all the Nigerian languages).

When Spitch fell into a decoding loop on a noisy Igbo clip, the quality check
marked it ``low`` and the router took Muse's transcript, which was read
correctly.

Second opinion and money words (RAI-290, 28 Oct 2026), both off unless the
caller turns them on (``transcriber_from_env`` does):

* ``second``: language hint -> a second transcriber that hears the same note
  at the same time; code reconciles the amounts and days of both
  (``mimi.voice.consensus``): agree, it stands; disagree, Mimi asks.
* ``vocab_providers``: transcribers that get the money and day words of
  ``mimi.voice.vocab`` for the shop's language.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from mimi.voice.base import AudioClip, Transcriber, Transcript, VoiceContext
from mimi.voice.consensus import choose
from mimi.voice.vocab import with_vocab

Policy = Mapping[str, Sequence[str]]
DEFAULT_KEY = "*"
DEFAULT_POLICY: dict[str, tuple[str, ...]] = {
    "yo": ("spitch", "muse"),
    "ig": ("spitch", "muse"),
    "ha": ("spitch", "muse"),
    "en": ("muse", "spitch"),
    "pcm": ("spitch", "muse"),
    DEFAULT_KEY: ("spitch", "muse"),
}


class VoiceRouter:
    def __init__(
        self,
        providers: Mapping[str, Transcriber],
        policy: Policy = DEFAULT_POLICY,
        name: str = "router",
        *,
        second: Mapping[str, str] | None = None,
        vocab_providers: Sequence[str] = (),
    ) -> None:
        if not providers:
            raise ValueError("VoiceRouter needs at least one transcriber")
        self.providers = dict(providers)
        self.policy = policy
        self.name = name
        self.second = dict(second or {})
        self.vocab_providers = tuple(vocab_providers)
        self.fallbacks = 0
        self.second_opinions = 0

    def ctx_for(self, name: str, ctx: VoiceContext) -> VoiceContext:
        if name in self.vocab_providers:
            return replace(ctx, keywords=with_vocab(ctx.keywords, ctx.language_hint))
        return ctx

    def order(self, ctx: VoiceContext) -> list[str]:
        want = self.policy.get(ctx.language_hint or DEFAULT_KEY) or self.policy.get(DEFAULT_KEY, ())
        names = [n for n in want if n in self.providers]
        # Anything configured but not in the policy goes last, as a fallback.
        names += [n for n in self.providers if n not in names]
        return names

    def transcribe(self, clip: AudioClip, ctx: VoiceContext) -> Transcript:
        other = self.second.get(ctx.language_hint or "")
        order = self.order(ctx)
        if not other or other not in self.providers or not order or order[0] == other:
            return self._first(clip, ctx)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_f = pool.submit(self._first, clip, ctx)
            second_f = pool.submit(self.providers[other].transcribe, clip, self.ctx_for(other, ctx))
            first, two = first_f.result(), second_f.result()
        if first.status == "failed" or first.provider == other or two.status == "failed":
            return first
        self.second_opinions += 1
        two = replace(two, tried=(f"{other}:{two.status}:second",))
        said = ctx.said_at or datetime.now(ZoneInfo("Africa/Lagos")).isoformat()
        return choose([first, two], said)

    def _first(self, clip: AudioClip, ctx: VoiceContext) -> Transcript:
        tried: list[str] = []
        low: Transcript | None = None
        last: Transcript | None = None
        for i, name in enumerate(self.order(ctx)):
            if i > 0:
                self.fallbacks += 1
            t = self.providers[name].transcribe(clip, self.ctx_for(name, ctx))
            tried.append(f"{name}:{t.status}")
            if t.status == "ok":
                return replace(t, tried=tuple(tried))
            if t.status == "low" and low is None:
                low = t
            last = t
        if low is not None:
            return replace(low, tried=tuple(tried))
        assert last is not None
        reasons = "; ".join(x for x in tried)
        return replace(last, tried=tuple(tried), reason=f"{last.reason} ({reasons})")

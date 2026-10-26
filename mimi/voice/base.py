"""What a voice note and its transcript look like to Mimi.

A shop's WhatsApp voice note arrives as an ``AudioClip`` (bytes plus the
container it came in). A ``Transcriber`` turns it into a ``Transcript``. Every
adapter (Spitch, Muse Voice Transcribe, the fake, the cache, the router) has
the same shape, so the receipt flow never knows which one ran.

A transcript carries a ``status``:

* ``ok``: usable text. It goes to the same verdict reader as a typed reply.
* ``low``: text came back but a code check doubts it (see ``quality.py``), or
  the API reported a low confidence. It is still read, but it can never
  produce consent.
* ``failed``: no usable text (a timeout after the retry, an API error, audio
  over the limits, a format we cannot read). Mimi asks the shop to type.

Neither Spitch nor Muse Voice Transcribe returns a confidence score (checked
against both APIs on 26 Oct 2026, see ``docs/VOICE.md``), so ``confidence`` is
``None`` unless an adapter's API gives one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

AudioFormat = Literal["ogg", "mp3", "wav", "m4a"]
AUDIO_FORMATS: tuple[str, ...] = ("ogg", "mp3", "wav", "m4a")
TranscriptStatus = Literal["ok", "low", "failed"]

# Language hints are ISO 639-1 codes plus "pcm" for Nigerian Pidgin.
LanguageHint = Literal["en", "pcm", "yo", "ig", "ha"]
HINT_TO_LANGUAGE: dict[str, str] = {
    "en": "english",
    "pcm": "pidgin",
    "yo": "yoruba",
    "ig": "igbo",
    "ha": "hausa",
}


@dataclass(frozen=True)
class AudioClip:
    data: bytes
    format: AudioFormat
    # Filled in by ``audio.inspect`` (or the caller when it already knows).
    duration_s: float | None = None

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class VoiceContext:
    """What the caller knows before transcribing.

    ``language_hint``: the shop's usual language when known (from its profile
    or earlier messages), else ``None`` for automatic detection.
    ``keywords``: SKU and brand words from the receipt, to bias recognition.
    """

    language_hint: str | None = None
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Transcript:
    text: str
    status: TranscriptStatus
    provider: str
    language: str | None = None  # as the API reported it, if it did
    confidence: float | None = None  # only when the API gives one
    duration_s: float | None = None
    latency_s: float = 0.0
    # Why the status is not "ok" ("" when it is).
    reason: str = ""
    # Every provider tried, in order, for traces ("spitch:failed", ...).
    tried: tuple[str, ...] = field(default_factory=tuple)
    # False when no API answer came back (a call error or a clip refused
    # before any call); such a transcript is never recorded in a cache.
    answered: bool = True
    fake: bool = False


class TranscriptionError(RuntimeError):
    """The transcription call failed after its retry."""


class AudioError(ValueError):
    """The clip breaks a limit or cannot be read or converted."""


class Transcriber(Protocol):
    name: str

    def transcribe(self, clip: AudioClip, ctx: VoiceContext) -> Transcript:
        """Never raises for a bad clip or a failed call: returns a transcript
        with ``status="failed"`` and a reason instead."""
        ...


def failed(provider: str, reason: str, clip: AudioClip | None = None) -> Transcript:
    return Transcript(
        text="",
        status="failed",
        provider=provider,
        duration_s=clip.duration_s if clip else None,
        reason=reason,
        tried=(f"{provider}:failed",),
        answered=False,
    )


def keywords_csv(words: Sequence[str]) -> str:
    return ",".join(w.replace(",", " ").strip() for w in words if w.strip())

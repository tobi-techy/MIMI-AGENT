"""Text to speech for Mimi's short replies to shops that opted in.

``Speaker.speak(script, language)`` returns a ``SpokenAudio`` (a WhatsApp
style Ogg/Opus voice note, or WAV from the fake). Adapters:

* ``SpitchSpeaker``: Spitch speech (``tts.SpitchSpeech``) in the shop's
  language with one fixed system voice per language (Pidgin through an
  English-language voice, since Spitch has no Pidgin code), then ffmpeg to
  mono Ogg/Opus at 16 kbps. Spitch is asked to keep no copy
  (``X-Data-Retention: false``).
* ``FakeSpeaker``: deterministic WAV (a quiet tone whose length follows the
  script), for tests and the offline sim. It says nothing.
* ``CachedSpeaker``: record once, replay offline, the same idea as the
  transcript cache. It is the only thing that writes audio to disk, and only
  when a caller gives it a directory (the eval and the recorded smoke run);
  the production path keeps the audio in memory just long enough to hand it
  to the channel.

A failed or refused call raises ``SpeechError``; the caller then sends the
text alone (``speak_reply``). A voice note is never sent without its text.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import struct
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from mimi.voice.audio import AudioError, _fix_wav_sizes, to_ogg_opus, wav_duration
from mimi.voice.base import TranscriptionError
from mimi.voice.tts import SpitchSpeech

SpokenFormat = Literal["ogg", "wav"]
# Language -> (Spitch speech language code, system voice). Fixed, so a shop
# always hears the same voice.
VOICES: dict[str, tuple[str, str]] = {
    "english": ("en", "lucy"),
    "pidgin": ("en", "tega"),
    "yoruba": ("yo", "funmi"),
    "igbo": ("ig", "ngozi"),
    "hausa": ("ha", "amina"),
    # The shop's own voice reading an English text (``mimi.voice.render``).
    "yoruba:en": ("en", "funmi"),
    "igbo:en": ("en", "ngozi"),
    "hausa:en": ("en", "amina"),
}
MAX_SCRIPT_CHARS = 400  # a short reply, not the receipt


class SpeechError(RuntimeError):
    """Speech generation failed or was refused; send the text alone."""


@dataclass(frozen=True)
class SpokenAudio:
    data: bytes
    format: SpokenFormat
    duration_s: float
    language: str
    voice: str
    provider: str
    latency_s: float = 0.0
    fake: bool = False

    @property
    def size(self) -> int:
        return len(self.data)


class Speaker(Protocol):
    name: str

    def speak(self, script: str, language: str) -> SpokenAudio: ...


def _check(script: str, language: str) -> tuple[str, str]:
    if language not in VOICES:
        raise SpeechError(f"no voice for language {language!r}")
    if not script.strip():
        raise SpeechError("empty script")
    if len(script) > MAX_SCRIPT_CHARS:
        raise SpeechError(f"script is {len(script)} characters, over {MAX_SCRIPT_CHARS}")
    return VOICES[language]


class SpitchSpeaker:
    def __init__(self, speech: SpitchSpeech, *, encode: bool = True) -> None:
        self.name = "spitch"
        self._speech = speech
        self._encode = encode
        self.calls = 0
        self.seconds = 0.0

    def speak(self, script: str, language: str) -> SpokenAudio:
        code, voice = _check(script, language)
        started = time.monotonic()
        try:
            wav = self._speech.generate(script, voice=voice, language=code)
        except TranscriptionError as exc:
            raise SpeechError(str(exc).replace("spitch speech", "spitch tts")) from exc
        # Spitch streams the WAV, so its header says "unknown length": set the
        # real sizes before reading the duration.
        wav = _fix_wav_sizes(wav)
        seconds = wav_duration(wav)
        self.calls += 1
        self.seconds += seconds
        data, fmt = wav, "wav"
        if self._encode:
            try:
                data, fmt = to_ogg_opus(wav), "ogg"
            except AudioError as exc:
                raise SpeechError(f"could not encode the voice reply: {exc}") from exc
        return SpokenAudio(
            data,
            fmt,  # type: ignore[arg-type]
            round(seconds, 3),
            language,
            voice,
            self.name,
            latency_s=round(time.monotonic() - started, 3),
        )


def tone_wav(seconds: float, *, rate: int = 16000, seed: int = 0) -> bytes:
    """A quiet, deterministic tone (no speech)."""
    n = int(seconds * rate)
    freq = 180 + seed % 120
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(
            b"".join(
                struct.pack("<h", int(800 * math.sin(2 * math.pi * freq * i / rate)))
                for i in range(n)
            )
        )
    return buf.getvalue()


class FakeSpeaker:
    """Deterministic stand-in: about 14 characters a second of tone."""

    def __init__(self) -> None:
        self.name = "fake"
        self.calls = 0
        self.scripts: list[tuple[str, str]] = []

    def speak(self, script: str, language: str) -> SpokenAudio:
        _, voice = _check(script, language)
        self.calls += 1
        self.scripts.append((language, script))
        seconds = round(min(max(len(script) / 14.0, 0.5), 30.0), 2)
        seed = int(hashlib.sha256(script.encode()).hexdigest()[:6], 16)
        return SpokenAudio(tone_wav(seconds, seed=seed), "wav", seconds, language, voice, "fake", fake=True)


def speech_key(provider: str, voice: str, language: str, script: str) -> str:
    canon = json.dumps([provider, voice, language, script], ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]


class SpeechCacheMiss(RuntimeError):
    """Replay mode found no recorded voice reply for this script."""


class CachedSpeaker:
    """``through``: replay a recorded reply, or call ``inner`` and record it.
    ``replay``: cache only. Files: ``<dir>/<key>.<fmt>`` plus ``index.json``
    (script, language, voice, seconds; never a key)."""

    def __init__(
        self,
        inner: Speaker | None,
        directory: str | Path,
        *,
        mode: Literal["through", "replay"] = "through",
        name: str | None = None,
    ) -> None:
        if mode == "through" and inner is None:
            raise ValueError("through mode needs a live speaker")
        self.inner = inner
        self.dir = Path(directory)
        self.mode = mode
        self.name = name or (inner.name if inner else "")
        self._lock = threading.Lock()
        idx = self.dir / "index.json"
        self._index: dict[str, dict[str, object]] = (
            json.loads(idx.read_text(encoding="utf-8")) if idx.exists() else {}
        )
        self.live_calls = 0
        self.cached_calls = 0

    def speak(self, script: str, language: str) -> SpokenAudio:
        _, voice = _check(script, language)
        key = speech_key(self.name, voice, language, script)
        with self._lock:
            hit = self._index.get(key)
        if hit is not None:
            path = self.dir / f"{key}.{hit['format']}"
            if path.exists():
                with self._lock:
                    self.cached_calls += 1
                return SpokenAudio(
                    path.read_bytes(),
                    hit["format"],  # type: ignore[arg-type]
                    float(hit["seconds"]),  # type: ignore[arg-type]
                    language,
                    voice,
                    self.name,
                )
        if self.mode == "replay" or self.inner is None:
            raise SpeechCacheMiss(f"no recorded voice reply for {self.name} {key}")
        out = self.inner.speak(script, language)
        with self._lock:
            self.live_calls += 1
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / f"{key}.{out.format}").write_bytes(out.data)
            self._index[key] = {
                "format": out.format,
                "seconds": out.duration_s,
                "language": language,
                "voice": voice,
                "script": script,
            }
            fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._index, f, ensure_ascii=False, indent=1, sort_keys=True)
                f.write("\n")
            os.replace(tmp, self.dir / "index.json")
        return out


def speak_reply(script: str, language: str, speaker: Speaker | None) -> tuple[SpokenAudio | None, str]:
    """(the voice note or None, why not). Never raises: a voice reply that
    cannot be made leaves the text to go alone."""
    if not script or speaker is None:
        return None, "no script" if not script else "no speaker"
    try:
        return speaker.speak(script, language), ""
    except (SpeechError, SpeechCacheMiss, AudioError) as exc:
        return None, str(exc)

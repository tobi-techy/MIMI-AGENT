"""Muse Voice Transcribe (``muse-voice-transcribe-1.0``) on Meta's Model API.

Shape checked against Meta's Voice API reference and a live call on 26 Oct
2026 (see ``docs/VOICE.md``):

* ``POST https://api.meta.ai/v1/asr/transcribe``, ``multipart/form-data``
  with two parts: ``request`` (JSON: ``model``, ``audioEncoding: "WAV"``,
  ``mode: "PUSH_TO_TALK"``, optional ``keywords`` and ``languageBias``) and
  ``audio`` (the clip). ``Accept: application/json`` gives the buffered body.
* Audio must be a mono 16-bit PCM WAV at 16 or 24 kHz, at most 10 minutes
  and 32 MB; an Ogg voice note is rejected with HTTP 400, so every clip is
  converted with ffmpeg first (``audio.to_wav16k``).
* Response: ``{"sessionId", "transcript", "audioDurationMs", "turns"}``. No
  confidence score and no detected language.
* ``languageBias`` takes language names. Meta lists 25 languages; English is
  one, Yoruba, Igbo, Hausa and Pidgin are not, so only an English or Pidgin
  hint sends ``["English"]``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from mimi.models import metering
from mimi.voice.audio import to_wav16k
from mimi.voice.base import (
    AudioClip,
    AudioError,
    Transcript,
    TranscriptionError,
    VoiceContext,
    failed,
)
from mimi.voice.http import post_with_retry
from mimi.voice.quality import assess

META_BASE = "https://api.meta.ai/v1"
MUSE_VOICE_MODEL = "muse-voice-transcribe-1.0"
_BIAS = {"en": ["English"], "pcm": ["English"]}


class MuseTranscriber:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = MUSE_VOICE_MODEL,
        base_url: str = META_BASE,
        timeout_s: float = 30.0,
        retries: int = 1,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("MuseTranscriber needs an API key")
        self.name = "muse"
        self.model = model
        self._url = base_url.rstrip("/") + "/asr/transcribe"
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        self._retries = retries
        self._sleep = sleep
        self._client = httpx.Client(timeout=timeout_s, transport=transport)
        self.calls = 0
        self.audio_seconds = 0.0

    def request_part(self, ctx: VoiceContext) -> dict[str, Any]:
        req: dict[str, Any] = {
            "model": self.model,
            "audioEncoding": "WAV",
            "mode": "PUSH_TO_TALK",
        }
        if ctx.keywords:
            req["keywords"] = list(ctx.keywords)
        bias = _BIAS.get(ctx.language_hint or "")
        if bias:
            req["languageBias"] = bias
        return req

    def transcribe(self, clip: AudioClip, ctx: VoiceContext) -> Transcript:
        metering.guard(getattr(self, "name", "stt"))
        try:
            wav = to_wav16k(clip)
        except AudioError as exc:
            return failed(self.name, str(exc), clip)
        files = {
            "request": (None, json.dumps(self.request_part(ctx)), "application/json"),
            "audio": ("voice.wav", wav.data, "audio/wav"),
        }
        try:
            resp, latency = post_with_retry(
                self._client,
                self._url,
                label=f"{self.model} transcription",
                retries=self._retries,
                sleep=self._sleep,
                headers=self._headers,
                files=files,
            )
            body: Any = resp.json()
            text = str(body["transcript"] or "")
        except TranscriptionError as exc:
            return failed(self.name, str(exc), clip)
        except (ValueError, KeyError, TypeError):
            return failed(self.name, "muse: unexpected response shape", clip)
        self.calls += 1
        self.audio_seconds += clip.duration_s or 0.0
        return assess(
            Transcript(
                text=text,
                status="ok",
                provider=self.name,
                language=None,
                confidence=None,
                duration_s=clip.duration_s,
                latency_s=latency,
                tried=(self.name,),
            )
        )

"""Spitch speech-to-text (``POST https://api.spitch.app/v1/transcriptions``).

Shape checked against Spitch's OpenAPI document (api 0.1.46) and a live call
on 26 Oct 2026 (see ``docs/VOICE.md``):

* request: ``multipart/form-data`` with ``content`` (the audio file; Ogg,
  MP3, WAV and M4A are accepted as they are, up to 25 MB), ``language``
  (``en``, ``yo``, ``ig``, ``ha``, ``am``; omitted for automatic handling),
  ``special_words`` (comma-separated words to bias recognition) and
  ``timestamp`` (``none``, ``sentence`` or ``word``);
* response: ``{"request_id", "text", "segments", "timestamps",
  "detected_language"}``. No confidence score.

Nigerian Pidgin has no STT code of its own, so a Pidgin hint is sent as
``en``. Every request carries ``X-Data-Retention: false`` so Spitch keeps no
copy of the shop's audio. Plain httpx (one HTTP stack for every adapter, and
``httpx.MockTransport`` in tests); the official ``spitch`` SDK (Apache-2.0)
was checked and is not needed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from mimi.models import metering
from mimi.voice.base import (
    AudioClip,
    Transcript,
    TranscriptionError,
    VoiceContext,
    failed,
    keywords_csv,
)
from mimi.voice.http import post_with_retry
from mimi.voice.quality import assess

SPITCH_BASE = "https://api.spitch.app/v1"
_STT_LANG = {"en": "en", "pcm": "en", "yo": "yo", "ig": "ig", "ha": "ha", "am": "am"}
_MIME = {"ogg": "audio/ogg", "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4"}


class SpitchTranscriber:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = SPITCH_BASE,
        timeout_s: float = 30.0,
        retries: int = 1,
        timestamp: str = "none",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("SpitchTranscriber needs an API key")
        self.name = "spitch"
        self._url = base_url.rstrip("/") + "/transcriptions"
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "X-Data-Retention": "false",
            "Accept": "application/json",
        }
        self._timestamp = timestamp
        self._retries = retries
        self._sleep = sleep
        self._client = httpx.Client(timeout=timeout_s, transport=transport)
        self.calls = 0
        self.audio_seconds = 0.0

    def form(self, ctx: VoiceContext) -> dict[str, str]:
        data: dict[str, str] = {"timestamp": self._timestamp}
        lang = _STT_LANG.get(ctx.language_hint or "")
        if lang:
            data["language"] = lang
        if ctx.keywords:
            data["special_words"] = keywords_csv(ctx.keywords)
        return data

    def transcribe(self, clip: AudioClip, ctx: VoiceContext) -> Transcript:
        metering.guard(getattr(self, "name", "stt"))
        files = {"content": (f"voice.{clip.format}", clip.data, _MIME[clip.format])}
        try:
            resp, latency = post_with_retry(
                self._client,
                self._url,
                label="spitch transcription",
                retries=self._retries,
                sleep=self._sleep,
                headers=self._headers,
                data=self.form(ctx),
                files=files,
            )
            body: Any = resp.json()
            text = str(body["text"] or "")
        except TranscriptionError as exc:
            return failed(self.name, str(exc), clip)
        except (ValueError, KeyError, TypeError):
            return failed(self.name, "spitch: unexpected response shape", clip)
        self.calls += 1
        self.audio_seconds += clip.duration_s or 0.0
        return assess(
            Transcript(
                text=text,
                status="ok",
                provider=self.name,
                language=body.get("detected_language"),
                confidence=None,
                duration_s=clip.duration_s,
                latency_s=latency,
                tried=(self.name,),
            )
        )

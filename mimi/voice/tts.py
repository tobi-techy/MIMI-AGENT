"""Spitch speech generation (``POST https://api.spitch.app/v1/speech``).

Used for two things: the synthetic voice-note fixtures, and Mimi's short
voice replies to shops that opted in (``speak.SpitchSpeaker``).

Request JSON: ``text``, ``voice`` (a system voice id such as ``jude``),
``language`` (ISO 639 code: ``en``, ``yo``, ``ig``, ``ha``; Spitch has no
``pcm`` code, so Pidgin is generated with a Pidgin voice and ``en``),
``format`` (``wav`` here), optional ``speed`` (0.7 to 1.2). The response is
streamed audio bytes. ``X-Data-Retention: false`` is sent so Spitch stores no
copy.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from mimi.voice.base import TranscriptionError
from mimi.voice.http import post_with_retry
from mimi.voice.spitch import SPITCH_BASE


class SpitchSpeech:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = SPITCH_BASE,
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("SpitchSpeech needs an API key")
        self._url = base_url.rstrip("/") + "/speech"
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "X-Data-Retention": "false",
        }
        self._client = httpx.Client(timeout=timeout_s, transport=transport)
        self._sleep = sleep
        self.calls = 0

    def generate(self, text: str, *, voice: str, language: str, speed: float = 1.0) -> bytes:
        body = {
            "text": text,
            "voice": voice,
            "language": language,
            "format": "wav",
            "speed": speed,
        }
        resp, _ = post_with_retry(
            self._client,
            self._url,
            label="spitch speech",
            sleep=self._sleep,
            headers=self._headers,
            json=body,
        )
        self.calls += 1
        if not resp.content.startswith(b"RIFF"):
            raise TranscriptionError("spitch speech: response is not WAV audio")
        return resp.content

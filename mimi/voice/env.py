"""Pick the transcriber from the environment.

* ``SPITCH_API_KEY``: Spitch key. ``META_API_KEY``: Meta Model API key.
* ``MIMI_VOICE``: ``router`` (default when a key is set), ``spitch``,
  ``muse`` or ``fake``. With no key at all it is always the fake.
* ``SPITCH_API_BASE`` (default ``https://api.spitch.app/v1``),
  ``META_API_BASE`` (default ``https://api.meta.ai/v1``),
  ``VOICE_TIMEOUT_S`` (default 30).

* ``MIMI_TTS``: ``spitch`` (default when ``SPITCH_API_KEY`` is set) or
  ``fake``, for voice replies (``speaker_from_env``).

The router uses whichever adapters have a key, in the order the policy
gives. Keys are read here and passed to the clients; nothing else touches
them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from mimi.voice.base import Transcriber
from mimi.voice.cache import CachedTranscriber
from mimi.voice.fake import FakeTranscriber
from mimi.voice.meta import META_BASE, MuseTranscriber
from mimi.voice.router import DEFAULT_POLICY, Policy, VoiceRouter
from mimi.voice.speak import CachedSpeaker, FakeSpeaker, Speaker, SpitchSpeaker
from mimi.voice.spitch import SPITCH_BASE, SpitchTranscriber
from mimi.voice.tts import SpitchSpeech


def voice_keys(env: Mapping[str, str] | None = None) -> dict[str, bool]:
    e = env if env is not None else os.environ
    return {
        "spitch": bool(e.get("SPITCH_API_KEY", "").strip()),
        "muse": bool(e.get("META_API_KEY", "").strip()),
    }


def live_transcribers(env: Mapping[str, str] | None = None) -> dict[str, Transcriber]:
    e = env if env is not None else os.environ
    timeout = float(e.get("VOICE_TIMEOUT_S", "30") or 30)
    out: dict[str, Transcriber] = {}
    spitch_key = e.get("SPITCH_API_KEY", "").strip()
    if spitch_key:
        out["spitch"] = SpitchTranscriber(
            api_key=spitch_key,
            base_url=e.get("SPITCH_API_BASE", "").strip() or SPITCH_BASE,
            timeout_s=timeout,
        )
    meta_key = e.get("META_API_KEY", "").strip()
    if meta_key:
        out["muse"] = MuseTranscriber(
            api_key=meta_key,
            base_url=e.get("META_API_BASE", "").strip() or META_BASE,
            timeout_s=timeout,
        )
    return out


def transcriber_from_env(
    env: Mapping[str, str] | None = None,
    *,
    cache: str | Path | None = None,
    policy: Policy = DEFAULT_POLICY,
) -> Transcriber:
    """``cache``: a path prefix; each live adapter records to
    ``<prefix>.<provider>.json`` and replays from it on a rerun."""
    e = env if env is not None else os.environ
    choice = e.get("MIMI_VOICE", "").strip().lower() or "router"
    live = live_transcribers(e)
    if choice == "fake" or not live:
        return FakeTranscriber()
    if cache is not None:
        live = {
            n: CachedTranscriber(t, f"{cache}.{n}.json", mode="through") for n, t in live.items()
        }
    if choice in live:
        return live[choice]
    return VoiceRouter(live, policy)


def speaker_from_env(
    env: Mapping[str, str] | None = None, *, cache: str | Path | None = None
) -> Speaker:
    """Spitch speech when a Spitch key is set (and ``MIMI_TTS`` is not
    ``fake``), else the fake. ``cache``: a directory to record voice replies
    in and replay them from (only the eval and recorded runs give one)."""
    e = env if env is not None else os.environ
    key = e.get("SPITCH_API_KEY", "").strip()
    if e.get("MIMI_TTS", "").strip().lower() == "fake" or not key:
        return FakeSpeaker()
    speaker: Speaker = SpitchSpeaker(
        SpitchSpeech(api_key=key, base_url=e.get("SPITCH_API_BASE", "").strip() or SPITCH_BASE)
    )
    if cache is not None:
        speaker = CachedSpeaker(speaker, cache, mode="through")
    return speaker

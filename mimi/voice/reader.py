"""From the bytes a webhook carried to a transcript Mimi can judge.

``read_voice_note`` checks the clip (format, size, duration; ``audio.py``),
then hands it to a transcriber (usually the ``VoiceRouter``). A clip that
breaks a limit, or a transcriber that fails, gives a ``failed`` transcript:
Mimi then asks the shop to type its reply. Nothing here guesses a reply.

This is also the one moment the audio is in hand, so the clip's loudness
envelope is computed here (``envelope.py``) and carried on the transcript. A
clip that will not decode carries no envelope, which is recorded as none.
"""

from __future__ import annotations

from dataclasses import replace

from mimi.voice.audio import inspect
from mimi.voice.base import (
    AudioError,
    Transcriber,
    Transcript,
    TranscriptionError,
    VoiceContext,
    failed,
)
from mimi.voice.envelope import envelope_of


def read_voice_note(
    data: bytes,
    transcriber: Transcriber | None,
    ctx: VoiceContext,
    *,
    declared: str | None = None,
) -> Transcript:
    try:
        clip = inspect(data, declared=declared)
    except AudioError as exc:
        return failed("limits", str(exc))
    loudness = envelope_of(clip.data, clip.format) or ()
    if transcriber is None:
        return replace(failed("none", "no transcriber configured", clip), envelope=loudness)
    try:
        heard = transcriber.transcribe(clip, ctx)
    except (TranscriptionError, AudioError) as exc:
        return replace(failed(transcriber.name, str(exc), clip), envelope=loudness)
    return replace(heard, envelope=loudness) if loudness else heard

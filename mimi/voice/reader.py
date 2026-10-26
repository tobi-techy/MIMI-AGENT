"""From the bytes a webhook carried to a transcript Mimi can judge.

``read_voice_note`` checks the clip (format, size, duration; ``audio.py``),
then hands it to a transcriber (usually the ``VoiceRouter``). A clip that
breaks a limit, or a transcriber that fails, gives a ``failed`` transcript:
Mimi then asks the shop to type its reply. Nothing here guesses a reply.
"""

from __future__ import annotations

from mimi.voice.audio import inspect
from mimi.voice.base import (
    AudioError,
    Transcriber,
    Transcript,
    TranscriptionError,
    VoiceContext,
    failed,
)


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
    if transcriber is None:
        return failed("none", "no transcriber configured", clip)
    try:
        return transcriber.transcribe(clip, ctx)
    except (TranscriptionError, AudioError) as exc:
        return failed(transcriber.name, str(exc), clip)

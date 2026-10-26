"""Voice notes: a typed transcription port with Spitch and Muse Voice
Transcribe adapters, a router, a deterministic fake and record/replay.

Speech to text only. Muse Spark still does all the reasoning: a transcript
goes to the same receipt reader as a typed reply (``mimi.receipts``).
"""

from mimi.voice.base import (
    AudioClip,
    AudioError,
    Transcriber,
    Transcript,
    TranscriptionError,
    VoiceContext,
)
from mimi.voice.cache import CachedTranscriber, TranscriptCacheMiss
from mimi.voice.env import transcriber_from_env, voice_keys
from mimi.voice.fake import FakeTranscriber
from mimi.voice.meta import MUSE_VOICE_MODEL, MuseTranscriber
from mimi.voice.reader import read_voice_note
from mimi.voice.router import DEFAULT_POLICY, VoiceRouter
from mimi.voice.spitch import SpitchTranscriber

__all__ = [
    "AudioClip",
    "AudioError",
    "CachedTranscriber",
    "DEFAULT_POLICY",
    "FakeTranscriber",
    "MUSE_VOICE_MODEL",
    "MuseTranscriber",
    "SpitchTranscriber",
    "Transcriber",
    "Transcript",
    "TranscriptCacheMiss",
    "TranscriptionError",
    "VoiceContext",
    "VoiceRouter",
    "read_voice_note",
    "transcriber_from_env",
    "voice_keys",
]

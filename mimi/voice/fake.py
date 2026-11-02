"""A deterministic stand-in transcriber, used whenever no speech key is set.

``FakeTranscriber(script)`` maps the SHA-256 of a clip's bytes to the text it
returns. With no script it uses the synthetic fixture manifest
(``eval/voice/clips.jsonl``): each clip transcribes to its own reference
text, exactly. An unknown clip comes back ``failed``, so the "please type
your reply" path is exercised offline too. It does not listen to anything.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from mimi.models import metering
from mimi.voice.base import AudioClip, Transcript, VoiceContext, failed
from mimi.voice.quality import assess


def audio_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeTranscriber:
    def __init__(self, script: Mapping[str, str] | None = None, name: str = "fake") -> None:
        self.name = name
        if script is None:
            from mimi.voice.fixtures import fake_script

            script = fake_script()
        self._script = dict(script)
        self.calls = 0

    def transcribe(self, clip: AudioClip, ctx: VoiceContext) -> Transcript:
        metering.guard(getattr(self, "name", "stt"))
        self.calls += 1
        text = self._script.get(audio_sha256(clip.data))
        if text is None:
            out = failed(self.name, "fake: unknown clip", clip)
            return Transcript(**{**out.__dict__, "fake": True})
        return assess(
            Transcript(
                text=text,
                status="ok",
                provider=self.name,
                duration_s=clip.duration_s,
                tried=(self.name,),
                fake=True,
            )
        )

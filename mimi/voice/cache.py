"""Record live transcripts once, replay them offline (CI never calls an API).

The same idea as ``mimi.models.cache``. ``CachedTranscriber(inner, path,
mode=..., name=...)`` keys each call on the provider name, the SHA-256 of
the audio, the language hint and the keywords:

* ``"through"``: a recorded transcript is replayed; a miss calls ``inner``
  and records what the API answered (text, detected language, confidence,
  latency, duration). A call that got no answer (an error after the retry)
  is not recorded, so a rerun tries again.
* ``"replay"``: cache only. A miss raises ``TranscriptCacheMiss``.

Only the API's text is stored, never audio. The quality checks are re-run on
replay, so changing them needs no re-recording.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Literal

from mimi.voice.base import AudioClip, Transcriber, Transcript, VoiceContext
from mimi.voice.fake import audio_sha256
from mimi.voice.quality import assess

CacheMode = Literal["through", "replay"]


class TranscriptCacheMiss(RuntimeError):
    """Replay mode found no recorded transcript for this clip and settings."""


def transcript_key(provider: str, clip: AudioClip, ctx: VoiceContext) -> str:
    canon = json.dumps(
        {
            "provider": provider,
            "audio": audio_sha256(clip.data),
            "language_hint": ctx.language_hint,
            "keywords": list(ctx.keywords),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]


class CachedTranscriber:
    def __init__(
        self,
        inner: Transcriber | None,
        path: str | Path,
        *,
        mode: CacheMode = "through",
        name: str | None = None,
    ) -> None:
        if mode == "through" and inner is None:
            raise ValueError("through mode needs a live transcriber to fall back on")
        self.inner = inner
        self.path = Path(path)
        self.mode = mode
        self.name = name or (inner.name if inner is not None else "")
        if not self.name:
            raise ValueError("CachedTranscriber needs a provider name for its keys")
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))
        self.calls = 0
        self.live_calls = 0
        self.cached_calls = 0

    def __len__(self) -> int:
        return len(self._entries)

    def transcribe(self, clip: AudioClip, ctx: VoiceContext) -> Transcript:
        key = transcript_key(self.name, clip, ctx)
        with self._lock:
            hit = self._entries.get(key)
            self.calls += 1
        if hit is not None:
            with self._lock:
                self.cached_calls += 1
            return assess(
                Transcript(
                    text=hit["text"],
                    status="ok",
                    provider=self.name,
                    language=hit.get("language"),
                    confidence=hit.get("confidence"),
                    duration_s=clip.duration_s,
                    latency_s=float(hit.get("latency_s", 0.0)),
                    tried=(self.name,),
                )
            )
        if self.mode == "replay" or self.inner is None:
            raise TranscriptCacheMiss(f"no recorded transcript for {self.name} call {key}")
        out = self.inner.transcribe(clip, ctx)
        with self._lock:
            self.live_calls += 1
            if out.answered and not out.fake:
                self._entries[key] = {
                    "text": out.text,
                    "language": out.language,
                    "confidence": out.confidence,
                    "latency_s": round(out.latency_s, 3),
                    "audio_s": clip.duration_s,
                }
                self._save()
        return out

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._entries, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)

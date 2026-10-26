"""The synthetic voice-note fixtures: ``eval/voice/clips.jsonl`` and the
clips beside it (made by ``scripts/make_voice_fixtures.py``).

Every clip is synthetic-generated (a Spitch TTS system voice plus ffmpeg
noise); none is a real person's voice.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from mimi.voice.fake import audio_sha256

ROOT = Path(__file__).resolve().parents[2]
VOICE_DIR = ROOT / "eval" / "voice"
MANIFEST = VOICE_DIR / "clips.jsonl"


@lru_cache(maxsize=1)
def load_clips() -> tuple[dict[str, Any], ...]:
    if not MANIFEST.exists():
        return ()
    rows = [
        json.loads(line)
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return tuple(rows)


def clip_bytes(row: dict[str, Any]) -> bytes:
    return (VOICE_DIR / row["file"]).read_bytes()


@lru_cache(maxsize=1)
def fake_script() -> dict[str, str]:
    """SHA-256 of each fixture clip -> its reference text."""
    out: dict[str, str] = {}
    for row in load_clips():
        path = VOICE_DIR / row["file"]
        if path.exists():
            out[audio_sha256(path.read_bytes())] = row["reference"]
    return out

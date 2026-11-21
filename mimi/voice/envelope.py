"""A voice note's loudness envelope, computed in code while the clip is here.

Mimi never stores audio, so the shape a console draws under a voice note has
to be data: ``BUCKETS`` levels from 0 (silent) to 100 (full scale), in order
from the first sample to the last. It is computed once, from the bytes the
webhook carried, and stored beside the transcript; nothing recomputes it and
nothing guesses one.

* WAV is read in pure Python (``wave`` plus ``struct``): no ffmpeg, no
  dependency.
* Ogg/Opus, MP3 and M4A are decoded through ``audio.to_wav16k``, which needs
  ffmpeg. Opus packets carry no amplitude, so there is no pure-Python way to
  read their loudness; without ffmpeg the answer is no envelope (``None``),
  never a made-up one. The same goes for a clip that will not decode.

Each bucket is the RMS of its own slice of the clip, in dBFS against full
scale, mapped onto 0 to 100 with a -50 dBFS floor: digital silence is 0, and
a level of about 50 is ordinary speech. Same bytes give the same bucket every
time, because the arithmetic is integer sums of squares and a fixed slice per
bucket (``tests/test_voice.py::test_envelope_is_the_same_for_the_same_bytes``).
"""

from __future__ import annotations

import io
import math
import struct
import wave

from mimi.voice.audio import AudioError, have_ffmpeg, to_wav16k
from mimi.voice.base import AudioClip, AudioFormat

BUCKETS = 130
FLOOR_DB = -50.0


def envelope_of(data: bytes, fmt: AudioFormat) -> tuple[int, ...] | None:
    """``BUCKETS`` levels 0 to 100, or ``None`` when the audio cannot be read."""
    if fmt == "wav":
        wav = data
    else:
        if not have_ffmpeg():
            return None
        try:
            wav = to_wav16k(AudioClip(data, fmt)).data
        except AudioError:
            return None
    try:
        return wav_envelope(wav)
    except (wave.Error, EOFError, struct.error, ValueError):
        return None


def wav_envelope(data: bytes) -> tuple[int, ...] | None:
    """The envelope of a PCM WAV, read without ffmpeg."""
    with wave.open(io.BytesIO(data)) as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        frames = w.getnframes()
        if channels < 1 or width not in (1, 2, 3, 4) or frames < 1:
            return None
        raw = w.readframes(frames)
    frames = min(frames, len(raw) // (width * channels))
    if frames < 1:
        return None
    full = float(1 << (8 * width - 1)) ** 2  # full scale, squared
    levels: list[int] = []
    for b in range(BUCKETS):
        first = b * frames // BUCKETS
        last = (b + 1) * frames // BUCKETS
        if last <= first:
            levels.append(0)  # fewer samples than buckets: nothing was heard here
            continue
        levels.append(_level(_sum_squares(raw, first, last, width, channels), last - first, full))
    return tuple(levels)


def _sum_squares(raw: bytes, first: int, last: int, width: int, channels: int) -> int:
    """Sum of squared sample values over frames ``[first, last)``, every channel."""
    start, end = first * width * channels, last * width * channels
    count = (end - start) // width
    if width == 1:  # unsigned, centred on 128
        return sum((v - 128) ** 2 for v in raw[start:end])
    if width == 2:
        samples = struct.unpack_from(f"<{count}h", raw, start)
        return sum(v * v for v in samples)
    shift = 8 * width - 16
    return sum(
        (int.from_bytes(raw[i:i + width], "little", signed=True) >> shift) ** 2
        for i in range(start, end, width)
    )


def _level(sum_squares: int, samples: int, full_square: float) -> int:
    mean_square = sum_squares / samples
    if mean_square <= 0:
        return 0
    db = 10.0 * math.log10(mean_square / full_square)
    if db <= FLOOR_DB:
        return 0
    if db >= 0.0:
        return 100
    return int(round((db - FLOOR_DB) * 100.0 / -FLOOR_DB))

"""Read, check and convert a voice note, before any API sees it.

* ``sniff``: the real container from the first bytes (a WhatsApp voice note
  is Ogg/Opus whatever its file name says). Ogg, MP3, WAV and M4A are
  accepted; anything else is refused.
* ``inspect``: format, size and duration, with the limits applied. Duration
  for WAV and Ogg/Opus is read in pure Python (no ffmpeg needed); MP3 and M4A
  use ``ffprobe``.
* ``to_wav16k``: mono 16-bit PCM WAV at 16 kHz through ``ffmpeg`` (Muse Voice
  Transcribe only takes that), with a timeout. MP4/M4A cannot be read from a
  pipe (its index can sit at the end), so ffmpeg and ffprobe read a private
  temporary file that is deleted straight after.

Limits: ``MAX_BYTES`` (4 MB) and ``MAX_SECONDS`` (180 s). A two-minute
WhatsApp voice note is about 250 KB, so these only stop abuse or a wrong
file. A clip under ``MIN_SECONDS`` is treated as empty.
"""

from __future__ import annotations

import io
import json
import shutil
import struct
import subprocess
import tempfile
import wave
from dataclasses import replace
from pathlib import Path

from mimi.voice.base import AudioClip, AudioError, AudioFormat

MAX_BYTES = 4_000_000
MAX_SECONDS = 180.0
MIN_SECONDS = 0.3
FFMPEG_TIMEOUT_S = 20.0


def sniff(data: bytes) -> AudioFormat | None:
    head = data[:16]
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    return None


def wav_duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / float(w.getframerate())


def wav_params(data: bytes) -> tuple[int, int, int]:
    """(channels, sample width in bytes, sample rate)."""
    with wave.open(io.BytesIO(data)) as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate()


def ogg_opus_duration(data: bytes) -> float | None:
    """From the last page's granule position (48 kHz) minus the pre-skip.
    ``None`` when the Ogg stream is not Opus."""
    head = data.find(b"OpusHead")
    if head < 0:
        return None
    pre_skip = struct.unpack_from("<H", data, head + 10)[0]
    last = data.rfind(b"OggS")
    if last < 0 or last + 14 > len(data):
        return None
    granule = struct.unpack_from("<q", data, last + 6)[0]
    if granule < 0:
        return None
    return max(granule - pre_skip, 0) / 48000.0


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run_on_file(data: bytes, suffix: str, cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Run ``cmd`` with ``{in}`` replaced by a temp file holding ``data``."""
    with tempfile.TemporaryDirectory(prefix="mimi-voice-") as tmp:
        src = Path(tmp) / f"in.{suffix}"
        src.write_bytes(data)
        args = [str(src) if a == "{in}" else a for a in cmd]
        try:
            return subprocess.run(args, capture_output=True, timeout=FFMPEG_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired as exc:
            raise AudioError(f"{cmd[0]} timed out") from exc


def _ffprobe_duration(data: bytes, fmt: str) -> float:
    if shutil.which("ffprobe") is None:
        raise AudioError("ffprobe is not installed; cannot read this format")
    out = _run_on_file(
        data,
        fmt,
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", "{in}"],
    )
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AudioError("ffprobe could not read the clip") from exc


def duration_of(data: bytes, fmt: AudioFormat) -> float:
    try:
        if fmt == "wav":
            return wav_duration(data)
        if fmt == "ogg":
            d = ogg_opus_duration(data)
            if d is not None:
                return d
    except (wave.Error, EOFError, struct.error) as exc:
        raise AudioError(f"cannot read {fmt}: {exc}") from exc
    return _ffprobe_duration(data, fmt)


def inspect(data: bytes, *, declared: str | None = None) -> AudioClip:
    """A checked clip, or ``AudioError`` saying which rule it broke."""
    if not data:
        raise AudioError("empty audio")
    if len(data) > MAX_BYTES:
        raise AudioError(f"audio is {len(data)} bytes, over the {MAX_BYTES} limit")
    fmt = sniff(data)
    if fmt is None:
        raise AudioError(f"unsupported audio format (declared {declared or 'none'})")
    seconds = duration_of(data, fmt)
    if seconds > MAX_SECONDS:
        raise AudioError(f"audio is {seconds:.0f}s, over the {MAX_SECONDS:.0f}s limit")
    if seconds < MIN_SECONDS:
        raise AudioError(f"audio is {seconds:.2f}s, too short to hold a reply")
    return AudioClip(data, fmt, round(seconds, 3))


def is_wav16k_mono(data: bytes) -> bool:
    try:
        ch, width, rate = wav_params(data)
    except (wave.Error, EOFError):
        return False
    return ch == 1 and width == 2 and rate in (16000, 24000)


def to_wav16k(clip: AudioClip) -> AudioClip:
    """Mono 16-bit PCM WAV at 16 kHz (a WAV already in that shape is kept)."""
    if clip.format == "wav" and is_wav16k_mono(clip.data):
        return clip
    if shutil.which("ffmpeg") is None:
        raise AudioError("ffmpeg is not installed; cannot convert to WAV")
    out = _run_on_file(
        clip.data,
        clip.format,
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            "{in}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-map_metadata",
            "-1",
            "-f",
            "wav",
            "pipe:1",
        ],
    )
    if out.returncode != 0 or not out.stdout:
        raise AudioError("ffmpeg could not convert the clip")
    # ffmpeg writes a streaming WAV header (unknown length) to a pipe; fix it
    # so strict readers see the real frame count.
    data = _fix_wav_sizes(out.stdout)
    return replace(clip, data=data, format="wav")


def _fix_wav_sizes(data: bytes) -> bytes:
    """Set the RIFF and data chunk sizes from the bytes actually there."""
    pos = data.find(b"data", 12)
    if pos < 0:
        return data
    buf = bytearray(data)
    struct.pack_into("<I", buf, 4, len(buf) - 8)
    struct.pack_into("<I", buf, pos + 4, len(buf) - pos - 8)
    return bytes(buf)


def to_ogg_opus(wav: bytes) -> bytes:
    """A WAV (a voice reply from text to speech) as a WhatsApp-style voice
    note: mono Ogg/Opus at 16 kbps. Read and written through a private
    temporary directory that is deleted straight after."""
    if shutil.which("ffmpeg") is None:
        raise AudioError("ffmpeg is not installed; cannot encode a voice reply")
    with tempfile.TemporaryDirectory(prefix="mimi-voice-") as tmp:
        src, dst = Path(tmp) / "in.wav", Path(tmp) / "out.ogg"
        src.write_bytes(wav)
        cmd = [
            "ffmpeg", "-v", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "16000",
            "-c:a", "libopus", "-b:a", "16k", "-map_metadata", "-1", str(dst),
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired as exc:
            raise AudioError("ffmpeg timed out") from exc
        if out.returncode != 0 or not dst.exists():
            raise AudioError("ffmpeg could not encode the voice reply")
        return dst.read_bytes()

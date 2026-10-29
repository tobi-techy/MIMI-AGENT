"""Console uploads in demo mode: the bytes of a photo, voice note or document
the owner attaches in the console, so a turn can point at them.

On WhatsApp the channel keeps the media and a turn carries only its ``ref``
and SHA-256 (``mimi.api.media``). The demo console has no channel, so in demo
mode the API stands in for one: ``POST /v1/media`` takes the raw bytes (the
request body, ``Content-Type`` set to the file's type), checks them and keeps
them in one directory per demo copy, and answers the ``ref`` and hash that
``POST /v1/turns`` then sends in ``media``. ``LocalMedia`` reads the same
``local:`` refs, so the helpers see the upload exactly as they see the
simulator's synthetic images (when ``MIMI_MEDIA_DIR`` is the same directory).

Checks, all in code:

* only the types below, and the first bytes must really be that type (a
  renamed file is refused);
* at most ``MAX_BYTES`` a file, ``MAX_FILES`` files and ``MAX_TOTAL`` bytes
  per demo copy;
* a ``local:`` ref a turn sends must be one of the caller's own uploads,
  with the hash it was stored under; another copy's ref, or any other local
  file, is refused like a missing one. (A channel ref such as a WhatsApp
  media id reads as no bytes in demo, as before.)

A copy's uploads are deleted when its copy is swept (``DemoStore.sweep``).
Live mode has no upload endpoint: the channel stores media, never Mimi.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from mimi.api.media import MAX_BYTES

MAX_FILES = 30
MAX_TOTAL = 60 * 1024 * 1024
PREFIX = "local:console/"

# mime -> (kind, file extension)
TYPES: dict[str, tuple[str, str]] = {
    "image/jpeg": ("image", "jpg"),
    "image/png": ("image", "png"),
    "image/webp": ("image", "webp"),
    "audio/ogg": ("voice", "ogg"),
    "audio/webm": ("voice", "webm"),
    "audio/mp4": ("voice", "m4a"),
    "audio/mpeg": ("voice", "mp3"),
    "audio/wav": ("voice", "wav"),
    "application/pdf": ("document", "pdf"),
}
_SAFE_TENANT = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class UploadRefused(Exception):
    """An upload the API will not keep, with the HTTP status to answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def base_mime(content_type: str) -> str:
    """``audio/webm;codecs=opus`` -> ``audio/webm``; ``audio/x-wav`` -> ``audio/wav``."""
    m = content_type.split(";", 1)[0].strip().lower()
    return {"audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/x-m4a": "audio/mp4",
            "audio/aac": "audio/mp4", "audio/mp3": "audio/mpeg", "image/jpg": "image/jpeg",
            "video/webm": "audio/webm"}.get(m, m)


def sniff(data: bytes) -> str:
    """The type the bytes really are (from their first bytes), or ``""``."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm"
    if data[4:8] == b"ftyp":
        return "audio/mp4"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    return ""


@dataclass(frozen=True)
class ConsoleUploads:
    root: Path

    def _dir(self, tenant_id: str) -> Path:
        if not _SAFE_TENANT.match(tenant_id):
            raise UploadRefused(400, "bad distributor id")
        return self.root / "console" / tenant_id

    def put(self, tenant_id: str, data: bytes, content_type: str) -> dict[str, object]:
        """Check and keep one upload; answer what a turn's ``media`` needs."""
        mime = base_mime(content_type)
        if mime not in TYPES:
            raise UploadRefused(415, "send a JPEG, PNG or WebP photo, a voice note "
                                     "(Ogg, WebM, M4A, MP3 or WAV) or a PDF")
        if not data:
            raise UploadRefused(422, "the file is empty")
        if len(data) > MAX_BYTES:
            raise UploadRefused(413, "the file is over 10 MB")
        if sniff(data) != mime:
            raise UploadRefused(415, "the file is not the type it says it is")
        kind, ext = TYPES[mime]
        d = self._dir(tenant_id)
        d.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256(data).hexdigest()
        path = d / f"{sha}.{ext}"
        if not path.exists():
            files = [p for p in d.iterdir() if p.is_file()]
            if len(files) >= MAX_FILES:
                raise UploadRefused(429, "this demo copy has all the files it can take; "
                                         "reset the demo to start again")
            if sum(p.stat().st_size for p in files) + len(data) > MAX_TOTAL:
                raise UploadRefused(429, "this demo copy has all the files it can take; "
                                         "reset the demo to start again")
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(path)
        return {"ref": f"{PREFIX}{tenant_id}/{sha}.{ext}", "sha256": sha, "mime": mime,
                "kind": kind, "size": len(data)}

    def owns(self, tenant_id: str, ref: str, sha256: str) -> bool:
        """Is ``ref`` one of this copy's uploads, stored under ``sha256``?"""
        return self.read(tenant_id, ref, sha256) is not None

    def read(self, tenant_id: str, ref: str, sha256: str = "") -> bytes | None:
        """The bytes of one of this copy's uploads; None for anyone else's."""
        own = f"{PREFIX}{tenant_id}/"
        if not _SAFE_TENANT.match(tenant_id) or not ref.startswith(own):
            return None
        name = ref[len(own):]
        if not re.fullmatch(r"[0-9a-f]{64}\.[a-z0-9]{2,4}", name):
            return None
        if sha256 and not name.startswith(sha256 + "."):
            return None
        path = self._dir(tenant_id) / name
        if not path.is_file():
            return None
        data = path.read_bytes()
        return data if hashlib.sha256(data).hexdigest() == name.split(".", 1)[0] else None

    @staticmethod
    def mime_of(ref: str) -> str:
        ext = ref.rsplit(".", 1)[-1]
        return next((m for m, (_k, e) in TYPES.items() if e == ext), "application/octet-stream")

    def drop(self, tenant_id: str) -> None:
        """Delete one copy's uploads (its copy was swept)."""
        if _SAFE_TENANT.match(tenant_id):
            shutil.rmtree(self._dir(tenant_id), ignore_errors=True)

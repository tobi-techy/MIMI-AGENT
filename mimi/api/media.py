"""Where the API finds an image's bytes, and the photo ports the helpers read.

A turn carries only a media ``ref`` (a WhatsApp media id or a storage key)
and the bytes' SHA-256; Mimi never stores media. When a helper needs the
pixels (SAM 3.1 counting a shelf, Muse Spark reading a screenshot) a
``MediaSource`` fetches them by ref and the bytes are checked against the
hash the channel gave. Nothing here keeps a copy.

``LocalMedia`` reads ``local:<path>`` refs under one directory
(``MIMI_MEDIA_DIR``): the simulator and the end-to-end eval put their
synthetic images there. The WhatsApp media download is the webhook
adapter's job (RAI-266, not here yet).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mimi.credit.tools import Photo
from mimi.store.book import SqlGo

MAX_BYTES = 10 * 1024 * 1024


class MediaSource(Protocol):
    def read(self, tenant_id: str, ref: str) -> bytes | None: ...


@dataclass(frozen=True)
class LocalMedia:
    root: Path

    def read(self, tenant_id: str, ref: str) -> bytes | None:
        if not ref.startswith("local:"):
            return None
        root = self.root.resolve()
        path = (root / ref[len("local:"):]).resolve()
        if root not in path.parents or not path.is_file():
            return None  # outside the media directory, or missing
        if path.stat().st_size > MAX_BYTES:
            return None
        return path.read_bytes()


@dataclass(frozen=True)
class NoMedia:
    def read(self, tenant_id: str, ref: str) -> bytes | None:
        return None


def _checked(source: MediaSource, tenant_id: str, ref: str, sha256: str) -> bytes | None:
    data = source.read(tenant_id, ref)
    if data is None or hashlib.sha256(data).hexdigest() != sha256:
        return None  # a ref that does not give the bytes the channel hashed
    return data


@dataclass
class StorePhotos:
    """Credit's ``PhotoStore`` over the store: the shop's camera photos by id."""

    go: SqlGo
    source: MediaSource

    def put(self, photo: Photo) -> str:  # the channel stores media, never Mimi
        return photo.photo_id

    def get(self, tenant_id: str, photo_id: str) -> Photo | None:
        for shop_id, m in self._all(tenant_id):
            if m["photo_id"] == photo_id:
                data = _checked(self.source, tenant_id, m["ref"], m["sha256"])
                return Photo(tenant_id, shop_id, photo_id, data, m["mime"]) if data else None
        return None

    def of_shop(self, tenant_id: str, shop_id: str) -> list[str]:
        """The shop's shelf photos (camera photos, not screenshots), oldest first."""
        return [m["photo_id"] for m in self.go.media_of(tenant_id, shop_id) if m["camera"]]

    def _all(self, tenant_id: str) -> list[tuple[str, dict[str, object]]]:
        out = []
        for shop_id in sorted(self.go.grants(tenant_id).shops):
            out.extend((shop_id, m) for m in self.go.media_of(tenant_id, shop_id))
        return out


@dataclass
class StoreScreens:
    """Proof's image source: one shop's screenshot by id, checked against its hash."""

    go: SqlGo
    source: MediaSource

    def __call__(self, tenant_id: str, shop_id: str,
                 photo_id: str) -> tuple[bytes, str, str, str] | None:
        for m in self.go.media_of(tenant_id, shop_id):
            if m["photo_id"] == photo_id:
                data = _checked(self.source, tenant_id, str(m["ref"]), str(m["sha256"]))
                if data is None:
                    return None
                return data, str(m["mime"]), str(m["sha256"]), str(m["received_at"])
        return None

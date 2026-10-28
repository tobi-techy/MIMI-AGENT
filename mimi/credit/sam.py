"""SAM 3.1 on Meta's Model API: find every product on a shelf photo.

One ``POST {base}/responses`` with model ``sam-3.1``: a user message with a
short concept phrase (``input_text``) and the photo as a base64 data URL
(``input_image``), ``metadata.include_confidence = "true"`` and
``mask_encoding = "one_bit"`` (the smallest masks; only boxes are used). The
answer is special-token text, one line for an image::

    <0f>0<|box;x1=23;y1=187;x2=119;y2=304;w=1280;h=960;c=0.73|><|mask;...|>,1<|box;...|>...

``parse_boxes`` reads the boxes by key (masks are skipped), so a token reads
the same with or without ``c``. Zero matches is a valid, empty answer.

Same client conventions as ``mimi.models.meta``: plain httpx, a timeout, one
retry on a timeout, a connection error, 429 or 5xx, the key from the caller
(``from_env`` reads ``META_API_KEY``) and never logged. ``CachedSegmenter``
records live answers once (keyed on the model, the phrase, the metadata and
the image's SHA-256, never its pixels) and replays them offline, so CI never
calls Meta. SAM is billed per image: USD 2.50 per 1,000 images (Meta's
pricing page, checked 28 Oct 2026), so N4 a photo at N1,600 per USD.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from mimi.models.meta import DEFAULT_BASE

SAM_MODEL = "sam-3.1"
# USD per image segmented (pay as you go).
SAM_USD_PER_IMAGE = 0.0025
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_BOX = re.compile(r"<\|box;([^|]*)\|>")


class SamError(RuntimeError):
    """The SAM call failed after its retry (never carries the key)."""


@dataclass(frozen=True)
class Box:
    x1: int
    y1: int
    x2: int
    y2: int
    score: float
    frame_w: int
    frame_h: int

    @property
    def w(self) -> int:
        return max(1, self.x2 - self.x1)

    @property
    def h(self) -> int:
        return max(1, self.y2 - self.y1)

    @property
    def area(self) -> int:
        return self.w * self.h

    def iou(self, other: Box) -> float:
        ix = max(0, min(self.x2, other.x2) - max(self.x1, other.x1))
        iy = max(0, min(self.y2, other.y2) - max(self.y1, other.y1))
        inter = ix * iy
        union = self.area + other.area - inter
        return inter / union if union else 0.0


@dataclass(frozen=True)
class Segmentation:
    prompt: str
    boxes: tuple[Box, ...]
    model: str = SAM_MODEL
    latency_s: float = 0.0
    images_billed: int = 1
    cached: bool = False
    fake: bool = False


def parse_boxes(text: str) -> tuple[Box, ...]:
    """Every box in a SAM answer line; masks are skipped."""
    out: list[Box] = []
    for raw in _BOX.findall(text or ""):
        kv: dict[str, str] = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                kv[k.strip()] = v.strip()
        try:
            out.append(Box(int(kv["x1"]), int(kv["y1"]), int(kv["x2"]), int(kv["y2"]),
                           float(kv.get("c", "1") or 1), int(kv.get("w", "0") or 0),
                           int(kv.get("h", "0") or 0)))
        except (KeyError, ValueError):
            continue
    return tuple(out)


def _output_text(data: Mapping[str, Any]) -> str:
    """The ``output_text`` of a non-streaming Responses answer."""
    if isinstance(data.get("output_text"), str):
        return str(data["output_text"])
    parts: list[str] = []
    for item in data.get("output") or []:
        for c in (item or {}).get("content") or []:
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
    return "".join(parts)


class Segmenter(Protocol):
    name: str

    def segment(self, image: bytes, mime: str, prompt: str) -> Segmentation: ...


class MetaSam:
    """The live client."""

    def __init__(self, *, api_key: str, model: str = SAM_MODEL, base_url: str = DEFAULT_BASE,
                 timeout_s: float = 60.0, retries: int = 1,
                 transport: httpx.BaseTransport | None = None, sleep: Any = time.sleep) -> None:
        if not api_key:
            raise ValueError("MetaSam needs an API key")
        self.name = model
        self._url = base_url.rstrip("/") + "/responses"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._retries = retries
        self._client = httpx.Client(timeout=timeout_s, transport=transport)
        self._sleep = sleep
        self.calls = 0

    def body(self, image: bytes, mime: str, prompt: str) -> dict[str, Any]:
        import base64

        url = f"data:{mime};base64," + base64.b64encode(image).decode("ascii")
        return {
            "model": self.name,
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": url},
            ]}],
            "metadata": dict(SAM_METADATA),
        }

    def segment(self, image: bytes, mime: str, prompt: str) -> Segmentation:
        body = self.body(image, mime, prompt)
        last = ""
        for attempt in range(self._retries + 1):
            started = time.monotonic()
            try:
                resp = self._client.post(self._url, json=body, headers=self._headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = type(exc).__name__
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    self.calls += 1
                    usage = data.get("usage") or {}
                    return Segmentation(prompt, parse_boxes(_output_text(data)), self.name,
                                        time.monotonic() - started,
                                        int(usage.get("images_processed") or 1))
                last = f"HTTP {resp.status_code}"
                if resp.status_code not in _RETRY_STATUS:
                    break
            if attempt < self._retries:
                self._sleep(1.0 + attempt)
        raise SamError(f"{self.name} call failed: {last}")


SAM_METADATA: dict[str, str] = {"include_confidence": "true", "mask_encoding": "one_bit"}

CacheMode = Literal["through", "replay"]


class SamCacheMiss(RuntimeError):
    """Replay mode found no recorded answer for this photo and phrase."""


def sam_key(model: str, image: bytes, prompt: str) -> str:
    canon = json.dumps({"model": model, "prompt": prompt, "metadata": SAM_METADATA,
                        "image": hashlib.sha256(image).hexdigest()}, sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()[:32]


class CachedSegmenter:
    """Record SAM's boxes once, replay them offline (no pixels in the file)."""

    def __init__(self, inner: Segmenter | None, path: str | Path, *,
                 mode: CacheMode = "through", name: str = SAM_MODEL) -> None:
        if mode == "through" and inner is None:
            raise ValueError("through mode needs a live segmenter")
        self.inner = inner
        self.path = Path(path)
        self.mode = mode
        self.name = inner.name if inner is not None else name
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = (
            json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {})
        self.live_calls = 0
        self.cached_calls = 0

    def __len__(self) -> int:
        return len(self._entries)

    def segment(self, image: bytes, mime: str, prompt: str) -> Segmentation:
        key = sam_key(self.name, image, prompt)
        with self._lock:
            hit = self._entries.get(key)
        if hit is not None:
            self.cached_calls += 1
            return Segmentation(prompt, tuple(Box(*b) for b in hit["boxes"]), hit["model"],
                                float(hit.get("latency_s", 0.0)), 1, cached=True)
        if self.mode == "replay" or self.inner is None:
            raise SamCacheMiss(f"no recorded SAM answer for {key}")
        seg = self.inner.segment(image, mime, prompt)
        self.live_calls += 1
        with self._lock:
            self._entries[key] = {
                "model": seg.model, "prompt": prompt, "latency_s": round(seg.latency_s, 3),
                "image_sha256": hashlib.sha256(image).hexdigest(),
                "boxes": [[b.x1, b.y1, b.x2, b.y2, round(b.score, 4), b.frame_w, b.frame_h]
                          for b in seg.boxes],
            }
            self._save()
        return seg

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._entries, f, indent=1, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)


def sam_from_env(env: Mapping[str, str] | None = None) -> MetaSam | None:
    e = env if env is not None else os.environ
    key = e.get("META_API_KEY", "").strip()
    if not key:
        return None
    return MetaSam(api_key=key, base_url=e.get("META_API_BASE", "").strip() or DEFAULT_BASE,
                   timeout_s=float(e.get("META_TIMEOUT_S", "60") or 60))


def sam_cost_kobo(images: int, usd_ngn: int = 1_600) -> int:
    """Integer kobo for ``images`` SAM images, rounded up."""
    kobo = images * SAM_USD_PER_IMAGE * usd_ngn * 100
    return int(-(-kobo // 1))

"""Record live model answers once, replay them offline (CI never calls Meta).

``CachedModel(inner, path, mode=...)`` keys every call on the model name, the
messages, ``max_tokens`` and ``response_format``:

* ``"through"``: a cached answer is replayed; a miss calls ``inner`` (the live
  model) and saves the answer, its token counts and latency. A long live run
  can be stopped and started again: the calls already made are not repeated.
* ``"replay"``: cache only. A miss raises ``CacheMiss`` (not a ``ModelError``,
  so nothing downstream quietly turns it into an "unclear"): re-record with a
  live run.

The file holds model outputs on synthetic data only, never a key or an
image (images are keyed by their SHA-256). Writes are
atomic (temp file and rename) after every new answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from mimi.models.base import ChatMessage, ChatModel, ChatResult

CacheMode = Literal["through", "replay"]


class CacheMiss(RuntimeError):
    """Replay mode found no recorded answer for this exact call."""


def cache_key(
    name: str,
    messages: Sequence[ChatMessage],
    max_tokens: int,
    response_format: Mapping[str, object] | None,
) -> str:
    canon = json.dumps(
        {
            "model": name,
            # An image is keyed by its SHA-256 (a third element only when a
            # message has images, so text-only keys are unchanged).
            "messages": [
                [m.role, m.content, [im.sha256 for im in m.images]]
                if m.images
                else [m.role, m.content]
                for m in messages
            ],
            "max_tokens": max_tokens,
            "response_format": response_format or None,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32]


class CachedModel:
    def __init__(
        self,
        inner: ChatModel | None,
        path: str | Path,
        *,
        mode: CacheMode = "through",
        name: str | None = None,
    ) -> None:
        if mode == "through" and inner is None:
            raise ValueError("through mode needs a live model to fall back on")
        self.inner = inner
        self.path = Path(path)
        self.mode = mode
        self.name = name or (inner.name if inner is not None else "")
        if not self.name:
            raise ValueError("CachedModel needs a model name for its keys")
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))
        self.calls = 0
        self.live_calls = 0
        self.cached_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.live_prompt_tokens = 0
        self.live_completion_tokens = 0

    def __len__(self) -> int:
        return len(self._entries)

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 400,
        meta: Mapping[str, str] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ChatResult:
        key = cache_key(self.name, messages, max_tokens, response_format)
        with self._lock:
            hit = self._entries.get(key)
        if hit is not None:
            res = ChatResult(
                text=hit["text"],
                model=hit.get("model", self.name),
                prompt_tokens=int(hit.get("prompt_tokens", 0)),
                completion_tokens=int(hit.get("completion_tokens", 0)),
                latency_s=float(hit.get("latency_s", 0.0)),
            )
            self._count(res, live=False)
            return res
        if self.mode == "replay" or self.inner is None:
            raise CacheMiss(f"no recorded answer for {self.name} call {key}")
        res = self.inner.complete(
            messages, max_tokens=max_tokens, meta=meta, response_format=response_format
        )
        with self._lock:
            self._entries[key] = {
                "text": res.text,
                "model": res.model,
                "prompt_tokens": res.prompt_tokens,
                "completion_tokens": res.completion_tokens,
                "latency_s": round(res.latency_s, 3),
            }
            self._save()
        self._count(res, live=True)
        return res

    def _count(self, res: ChatResult, *, live: bool) -> None:
        with self._lock:
            self.calls += 1
            self.prompt_tokens += res.prompt_tokens
            self.completion_tokens += res.completion_tokens
            if live:
                self.live_calls += 1
                self.live_prompt_tokens += res.prompt_tokens
                self.live_completion_tokens += res.completion_tokens
            else:
                self.cached_calls += 1

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._entries, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)

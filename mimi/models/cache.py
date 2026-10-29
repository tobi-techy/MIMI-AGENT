"""Record live model answers once, replay them offline (CI never calls Meta).

``CachedModel(inner, path, mode=...)`` keys every call on the model name, the
messages, ``max_tokens`` and ``response_format``:

* ``"through"``: a cached answer is replayed; a miss calls ``inner`` (the live
  model) and saves the answer, its token counts and latency. A long live run
  can be stopped and started again: the calls already made are not repeated.
* ``"replay"``: cache only. A miss raises ``CacheMiss`` (not a ``ModelError``,
  so nothing downstream quietly turns it into an "unclear"): re-record with a
  live run.

Versioned entries (RAI-309): a caller whose answers depend on versions the
request text does not show (the router's prompt and skill catalogue versions,
the Go policy and a helper's skill version for loop calls) passes
``versions``. The key is then the request hash mixed with that stamp
(``versioned_key``), and each entry records its ``versions`` and its
``request`` hash. A recorded answer replays only under the versions it was
recorded with; under any other it misses, and the miss is counted as
``stale`` (the request was recorded, under other versions) so the report says
why. ``versions=None`` keeps the plain key.

Moving a cache to versioned keys without re-recording: run the eval with
``MIMI_CACHE_MAP=<file>``. A versioned lookup that finds only the plain-key
entry replays it and appends the pair to the map; then
``python -m mimi.models.cache migrate <file>`` moves each entry to its
versioned key, stamped. Nothing else ever falls back to a plain key.

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

from mimi.models import metering
from mimi.models.base import ChatMessage, ChatModel, ChatResult

CacheMode = Literal["through", "replay"]


class CacheMiss(RuntimeError):
    """Replay mode found no recorded answer for this exact call."""


Versions = Mapping[str, str]


def versioned_key(base: str, versions: Versions | None) -> str:
    """``base`` mixed with a versions stamp; unchanged when there is none."""
    if not versions:
        return base
    stamp = json.dumps(dict(versions), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{base}|{stamp}".encode()).hexdigest()[:len(base)]


def note_migration(path: Path, base: str, key: str, versions: Versions) -> None:
    """In ``MIMI_CACHE_MAP`` mode: remember that ``base`` moves to ``key``."""
    out = os.environ.get("MIMI_CACHE_MAP", "").strip()
    if not out:
        return
    line = json.dumps({"file": str(Path(path).resolve()), "from": base, "to": key,
                       "versions": dict(versions)}, sort_keys=True)
    with open(out, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def requests_index(entries: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Request hash -> the versions it was recorded under (versioned entries only)."""
    return {str(v["request"]): v.get("versions") for v in entries.values()
            if isinstance(v, Mapping) and v.get("request")}


def migrate(map_file: str | Path) -> dict[str, int]:
    """Apply a ``MIMI_CACHE_MAP`` file: move each plain-key entry to its
    versioned key with its stamp. Entries no eval read are left as they are."""
    pairs: dict[str, dict[str, tuple[str, dict[str, str]]]] = {}
    for line in Path(map_file).read_text(encoding="utf-8").splitlines():
        if line.strip():
            p = json.loads(line)
            pairs.setdefault(p["file"], {})[p["to"]] = (p["from"], p["versions"])
    moved: dict[str, int] = {}
    for file, todo in sorted(pairs.items()):
        path = Path(file)
        data = json.loads(path.read_text(encoding="utf-8"))
        froms: set[str] = set()
        for to, (frm, versions) in sorted(todo.items()):
            if to in data or frm not in data:
                continue
            data[to] = {**data[frm], "request": frm, "versions": versions}
            froms.add(frm)
        for frm in froms:
            data.pop(frm, None)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_dump(data))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        moved[file] = len(froms)
    return moved


def _dump(data: dict[str, Any]) -> str:
    """A cache file in the layout its writer uses: the loop's recorded turns
    (``tool_calls``) escape non-ASCII, the plain model caches keep it."""
    loop = any(isinstance(v, dict) and "tool_calls" in v for v in data.values())
    return json.dumps(data, ensure_ascii=loop, indent=1, sort_keys=True) + "\n"


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
        versions: Versions | None = None,
    ) -> None:
        if mode == "through" and inner is None:
            raise ValueError("through mode needs a live model to fall back on")
        self.inner = inner
        self.path = Path(path)
        self.mode = mode
        self.name = name or (inner.name if inner is not None else "")
        if not self.name:
            raise ValueError("CachedModel needs a model name for its keys")
        self.versions = dict(versions) if versions else None
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))
        self._requests = requests_index(self._entries)
        self.stale = 0
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
        metering.guard(self.name)
        base = cache_key(self.name, messages, max_tokens, response_format)
        key = versioned_key(base, self.versions)
        with self._lock:
            hit = self._entries.get(key)
            if hit is None and self.versions and os.environ.get("MIMI_CACHE_MAP"):
                legacy = self._entries.get(base)
                if legacy is not None and "versions" not in legacy:
                    note_migration(self.path, base, key, self.versions)
                    hit = legacy
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
        stale = self.versions is not None and base in self._requests
        if stale:
            with self._lock:
                self.stale += 1
        if self.mode == "replay" or self.inner is None:
            if stale:
                raise CacheMiss(f"stale: {self.name} call {base} was recorded under "
                                f"{self._requests[base]}, now {self.versions}; re-record")
            raise CacheMiss(f"no recorded answer for {self.name} call {key}")
        res = self.inner.complete(
            messages, max_tokens=max_tokens, meta=meta, response_format=response_format
        )
        with self._lock:
            entry: dict[str, Any] = {
                "text": res.text,
                "model": res.model,
                "prompt_tokens": res.prompt_tokens,
                "completion_tokens": res.completion_tokens,
                "latency_s": round(res.latency_s, 3),
            }
            if self.versions:
                entry.update(request=base, versions=dict(self.versions))
                self._requests[base] = dict(self.versions)
            self._entries[key] = entry
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


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3 or sys.argv[1] != "migrate":
        raise SystemExit("usage: python -m mimi.models.cache migrate MAP_FILE")
    print(json.dumps(migrate(sys.argv[2]), indent=1))

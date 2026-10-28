"""The e2e run's models: real Muse Spark and SAM 3.1, recorded once, replayed in CI.

Every model call the agent makes in the e2e run (the harness loop, the
router's last layer, the screenshot reader, the receipt-reply reader, the
stock-answer reader, SAM 3.1) and every shop persona's reply goes through a
record/replay cache under ``eval/e2e/``. ``--live`` records what is missing
(and needs ``META_API_KEY``); without it the run replays and a miss is a
failure, never a silent fake.

One process writes each cache file at a time: the runner writes only the
persona cache, the single live worker writes the rest (the worker-kill step
starts the second worker only after the first is dead). Writes are atomic.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mimi.api.helpers import HelperDeps
from mimi.api.media import LocalMedia
from mimi.credit.sam import CachedSegmenter, Segmentation, sam_from_env
from mimi.harness.gateway import OpenAIToolModel, RecordedToolModel, ToolModel
from mimi.models.base import ChatModel, ChatResult
from mimi.models.cache import CachedModel
from mimi.models.env import from_env

ROOT = Path(__file__).resolve().parents[3]
EVAL_DIR = ROOT / "eval" / "e2e"
MEDIA_DIR = ROOT / "eval"
MODEL = "muse-spark-1.3"
SIM_MODEL = "muse-spark-1.3-contributor"


@dataclass(frozen=True)
class CachePaths:
    root: Path = EVAL_DIR

    @property
    def harness(self) -> Path:
        return self.root / "harness.cache.json"

    @property
    def router(self) -> Path:
        return self.root / "router.cache.json"

    @property
    def helpers(self) -> Path:
        return self.root / "helpers.cache.json"

    @property
    def sam(self) -> Path:
        return self.root / "sam.cache.json"

    @property
    def persona(self) -> Path:
        return self.root / "persona.cache.json"


class Usage:
    """Appends one JSON line per model call or SAM image to a file (any process)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def log(self, **row: object) -> None:
        if self.path is None:
            return
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")


class UsageModel:
    """A ChatModel that logs its token use (the worker's helper calls)."""

    def __init__(self, inner: CachedModel, usage: Usage, role: str) -> None:
        self.inner, self.usage, self.role = inner, usage, role
        self.name = inner.name

    def complete(self, messages: Any, *, max_tokens: int = 400,
                 meta: Mapping[str, str] | None = None,
                 response_format: Mapping[str, object] | None = None) -> ChatResult:
        live_before = self.inner.live_calls
        res = self.inner.complete(messages, max_tokens=max_tokens, meta=meta,
                                  response_format=response_format)
        self.usage.log(kind="chat", role=self.role, model=res.model or self.name,
                       prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens,
                       live=self.inner.live_calls > live_before,
                       script=(meta or {}).get("script", ""))
        return res


class UsageSegmenter:
    def __init__(self, inner: CachedSegmenter, usage: Usage) -> None:
        self.inner, self.usage = inner, usage
        self.name = inner.name

    def segment(self, image: bytes, mime: str, prompt: str) -> Segmentation:
        seg = self.inner.segment(image, mime, prompt)
        self.usage.log(kind="sam", model=self.name, prompt=prompt, live=not seg.cached)
        return seg


def _key(env: Mapping[str, str]) -> str:
    return env.get("META_API_KEY", "").strip()


def agent_models(live: bool, paths: CachePaths, env: Mapping[str, str] | None = None,
                 usage: Path | None = None) -> tuple[ToolModel, ChatModel, HelperDeps]:
    """(harness tool model, router model, helper deps) recorded or replayed."""
    e = env if env is not None else os.environ
    if live and not _key(e):
        raise SystemExit("--live needs META_API_KEY")
    tool_live = OpenAIToolModel(api_key=_key(e), model=MODEL,
                                base_url=e.get("META_API_BASE", "").strip()
                                or "https://api.meta.ai/v1",
                                timeout_s=float(e.get("META_TIMEOUT_S", "60") or 60)
                                ) if live else None
    tool = RecordedToolModel(paths.harness, tool_live, name=MODEL)
    inner = from_env("mimi", env=e) if live else None
    router = CachedModel(inner, paths.router, mode="through" if live else "replay", name=MODEL)
    helpers = CachedModel(inner, paths.helpers, mode="through" if live else "replay",
                          name=MODEL)
    sam = CachedSegmenter(sam_from_env(e) if live else None, paths.sam,
                          mode="through" if live else "replay")
    u = Usage(usage)
    deps = HelperDeps(vision=UsageModel(helpers, u, "vision"),
                      judge=UsageModel(helpers, u, "judge"),
                      stock=UsageModel(helpers, u, "stock"),
                      segmenter=UsageSegmenter(sam, u), media=LocalMedia(MEDIA_DIR))
    return tool, router, deps


def persona_model(live: bool, paths: CachePaths,
                  env: Mapping[str, str] | None = None) -> CachedModel:
    e = env if env is not None else os.environ
    if live and not _key(e):
        raise SystemExit("--live needs META_API_KEY")
    inner = from_env("sim", env=e) if live else None
    return CachedModel(inner, paths.persona, mode="through" if live else "replay",
                       name=SIM_MODEL)

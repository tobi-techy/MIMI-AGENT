"""Model cache keys carry the policy and skill versions (RAI-309).

PoC first, on main (02023d4): a recorded Collector answer replayed after the
Go policy version was bumped, and after the skill version was bumped, as if
nothing had changed. Now both miss, and the miss says it is stale.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from mimi.harness import versions as V
from mimi.harness.eval import EVAL_DIR as HARNESS_DIR
from mimi.harness.eval import make
from mimi.harness.gateway import CacheMiss, RecordedToolModel
from mimi.harness.scenarios import build
from mimi.models.base import ChatMessage, ChatResult
from mimi.models.cache import CachedModel, migrate, versioned_key
from mimi.models.cache import CacheMiss as ChatCacheMiss
from mimi.world import load_world

W = load_world()
CACHE = HARNESS_DIR / "collector.live.cache.json"


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(V, "_by_prompt", dict(V._by_prompt))
    monkeypatch.delenv("MIMI_CACHE_MAP", raising=False)


def _run(rec: RecordedToolModel, helper_patch: Any = None) -> Any:
    sc = next(s for s in build(W) if s.sid == "balance-01")
    h, helper, _ = make(W, [rec], at=sc.event.at, withheld=sc.withheld)
    if helper_patch is not None:
        helper = V.register(helper_patch(helper))
    return h.run(sc.event, helper)


def test_the_recorded_answer_replays_at_current_versions() -> None:
    rec = RecordedToolModel(CACHE, None, name="muse-spark-1.3")
    r = _run(rec)
    assert rec.hits >= 1 and rec.misses == 0 and r.status != "error"


def test_poc_a_policy_bump_misses_as_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(V, "POLICY_VERSION", "policy-2099-01-01")
    rec = RecordedToolModel(CACHE, None, name="muse-spark-1.3")
    with pytest.raises(CacheMiss, match="stale"):
        _run(rec)
    assert rec.stale == 1


def test_poc_a_skill_bump_misses_as_stale() -> None:
    rec = RecordedToolModel(CACHE, None, name="muse-spark-1.3")
    with pytest.raises(CacheMiss, match="stale"):
        _run(rec, lambda h: dataclasses.replace(h, skill_version="collector-v2",
                                                skill=h.skill + " "))
    assert rec.stale == 1


def test_the_same_prompt_under_a_new_version_also_misses() -> None:
    rec = RecordedToolModel(CACHE, None, name="muse-spark-1.3")
    with pytest.raises(CacheMiss, match="stale"):
        _run(rec, lambda h: dataclasses.replace(h, skill_version="collector-v2"))


class _Echo:
    name = "echo"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: Any, *, max_tokens: int = 400, meta: Any = None,
                 response_format: Any = None) -> ChatResult:
        self.calls += 1
        return ChatResult(text=f"answer {self.calls}", model="echo", prompt_tokens=1,
                          completion_tokens=1, latency_s=0.1)


def test_chat_cache_entries_are_versioned(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    msgs = [ChatMessage(role="user", content="hi")]
    live = _Echo()
    CachedModel(live, path, versions={"catalogue": "a"}).complete(msgs)
    assert CachedModel(None, path, mode="replay", name="echo",
                       versions={"catalogue": "a"}).complete(msgs).text == "answer 1"
    old = CachedModel(None, path, mode="replay", name="echo", versions={"catalogue": "b"})
    with pytest.raises(ChatCacheMiss, match="stale"):
        old.complete(msgs)
    assert old.stale == 1
    # Through mode records the new version beside the old one.
    CachedModel(live, path, versions={"catalogue": "b"}).complete(msgs)
    assert live.calls == 2


def test_plain_keys_are_unchanged_without_versions() -> None:
    assert versioned_key("abc", None) == "abc" and versioned_key("abc", {}) == "abc"
    k = versioned_key("a" * 32, {"policy": "p"})
    assert len(k) == 32 and k != "a" * 32


def test_migration_moves_only_what_an_eval_read(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from mimi.models.cache import cache_key

    path = tmp_path / "c.json"
    msgs = [ChatMessage(role="user", content="hi")]
    base = cache_key("echo", msgs, 400, None)
    path.write_text(json.dumps({base: {"text": "old", "model": "echo"},
                                "dead": {"text": "unused", "model": "echo"}}))
    themap = tmp_path / "map.jsonl"
    monkeypatch.setenv("MIMI_CACHE_MAP", str(themap))
    v = {"catalogue": "a"}
    assert CachedModel(None, path, mode="replay", name="echo", versions=v).complete(
        msgs).text == "old"
    monkeypatch.delenv("MIMI_CACHE_MAP")
    with pytest.raises(ChatCacheMiss):  # no fallback outside map mode
        CachedModel(None, path, mode="replay", name="echo", versions=v).complete(msgs)
    assert migrate(themap) == {str(path.resolve()): 1}
    data = json.loads(path.read_text())
    assert base not in data and "dead" in data
    assert CachedModel(None, path, mode="replay", name="echo", versions=v).complete(
        msgs).text == "old"
    with pytest.raises(ChatCacheMiss, match="stale"):
        CachedModel(None, path, mode="replay", name="echo",
                    versions={"catalogue": "b"}).complete(msgs)


def test_every_production_helper_is_registered() -> None:
    from mimi.api.helpers import build_helpers
    from mimi.store import SqlGo, ensure_schema, make_engine, seed_world

    e = make_engine("sqlite://")
    ensure_schema(e)
    seed_world(e, W)
    for key, h in build_helpers(go=SqlGo(e)).items():
        assert V.skill_stamp(h.system_prompt()) == f"{h.name}:{h.skill_version}", key

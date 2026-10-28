"""Skill quality bar (RAI-317): every active skill has its own evals, a changed
skill bumps its version, and skills mode never sends a bigger prompt than the
flag-off helper it replaces (except the skills whose answers are recorded)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.harness.context import est_tokens
from mimi.skills.brief import FROZEN, KEEP, SHORT
from mimi.skills.catalog import LOCK_NAME, ROOT, catalog, digest, load_catalog, write_lock
from mimi.skills.evals import MIN_CASES, load_cases
from mimi.skills.spec import SkillError
from mimi.store.book import SqlGo


def test_every_active_skill_has_its_own_evals() -> None:
    cat = catalog()
    for doc in cat.active():
        cases = load_cases(cat, doc.key)
        assert len(cases) >= MIN_CASES, f"{doc.key}: {len(cases)} cases (need {MIN_CASES})"
        assert all(c["gold"] for c in cases), doc.key
        assert len({c["id"] for c in cases}) == len(cases), f"{doc.key}: duplicate case ids"


def test_lock_history_matches_every_version() -> None:
    lock = json.loads((ROOT / LOCK_NAME).read_text(encoding="utf-8"))["skills"]
    cat = catalog()
    for doc in cat.skills.values():
        entry = lock[doc.name]
        assert entry["history"][doc.version] == entry["sha256"] == digest(cat.folder(doc.key))


def _copy(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def test_changed_skill_without_a_version_bump_is_refused(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    md = root / "chat" / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "\nOne more line.\n", encoding="utf-8")
    with pytest.raises(SkillError, match="bump version"):
        write_lock(root)
    with pytest.raises(SkillError):  # and the stale lock refuses to load it
        load_catalog(root)


def test_changed_skill_with_a_version_bump_locks(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    md = root / "chat" / "SKILL.md"
    text = md.read_text(encoding="utf-8")
    old = next(ln for ln in text.splitlines() if ln.startswith("version: "))
    major, minor, _ = old.split(": ")[1].split(".")
    new = f"version: {major}.{int(minor) + 1}.0"
    md.write_text(text.replace(old, new) + "\nOne more line.\n", encoding="utf-8")
    write_lock(root)
    lock = json.loads((root / LOCK_NAME).read_text(encoding="utf-8"))["skills"]["chat"]
    assert lock["version"] == new.split(": ")[1]
    assert old.split(": ")[1] in lock["history"]  # the reviewed old version is kept
    load_catalog(root)


def _helpers(monkeypatch: pytest.MonkeyPatch, on: bool) -> dict[str, Any]:
    from mimi.api.helpers import build_helpers

    monkeypatch.setenv("MIMI_SKILLS", "1" if on else "")
    return build_helpers(go=SqlGo(sa.create_engine("sqlite://")))


def _size(h: Any) -> int:
    return est_tokens(h.system_prompt()) + est_tokens(h.toolset.schemas())


def test_no_skill_prompt_is_bigger_than_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    off = _helpers(monkeypatch, False)
    on = _helpers(monkeypatch, True)
    over = []
    for doc in catalog().active():
        h = on.get(doc.key)
        base = off.get(doc.key) or off.get(doc.helper)
        if h is None or base is None or doc.key in FROZEN:
            continue
        if _size(h) > _size(base):
            over.append(f"{doc.key}: {_size(h)} > {_size(base)}")
    assert not over, over


def test_short_tool_texts_keep_the_safety_words(monkeypatch: pytest.MonkeyPatch) -> None:
    off = _helpers(monkeypatch, False)
    from mimi.skills.scripts import load_scripts

    full = {s.name: s.description for h in off.values() for s in h.toolset.specs}
    full.update({n: s.description for n, s in load_scripts(catalog()).items()})
    on = _helpers(monkeypatch, True)
    full.update({s.name: s.description for k in FROZEN for s in on[k].toolset.specs})
    for name, text in SHORT.items():
        assert name in full, f"{name}: no such tool"
        lost = [w for w in KEEP if w in full[name].lower() and w not in text.lower()]
        assert not lost, f"{name} lost {lost}"
        assert len(text) < len(full[name]), name


def test_frozen_skills_keep_the_full_tool_texts(monkeypatch: pytest.MonkeyPatch) -> None:
    off = _helpers(monkeypatch, False)
    on = _helpers(monkeypatch, True)
    full = {s.name: s.description for h in off.values() for s in h.toolset.specs}
    for key in FROZEN:
        for s in on[key].toolset.specs:
            if s.name in full:
                assert s.description == full[s.name], (key, s.name)

"""The skill catalogue: every skill folder under the repo's ``skills/``, vetted.

Trust: skills load from one place only, the ``skills/`` folder shipped with
the code (or ``MIMI_SKILLS_DIR``, set by the deploy, never by a message).
Each folder's files are hashed and must match ``skills/skills.lock.json``,
which a person updates in a reviewed PR (``python -m mimi.skills lock``). A
file changed on disk without a new lock refuses to load, so nothing the
agent writes at run time (drafts, notes) can become a live skill.

Progressive disclosure, three levels (Anthropic, "Equipping agents for the
real world with Agent Skills"):

1. ``metadata(helper)``: name and description of the helper's active
   skills, under a token cap, always in the prompt;
2. ``body(key)``: the routed skill's SKILL.md body, loaded for the run;
3. ``read_file(key, path)``: a reference or asset, read on demand by the
   ``read_skill`` tool.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from mimi.skills.spec import SkillDoc, SkillError, parse

ROOT = Path(__file__).resolve().parents[2] / "skills"
LOCK_NAME = "skills.lock.json"
CORE_DIR = "_core"
SKIP_DIRS = frozenset({"_drafts", CORE_DIR, "__pycache__"})
# Files that are not part of a skill's vetted content (they do not change
# what the model reads or runs).
UNHASHED_DIRS = frozenset({"tests", "__pycache__"})
READABLE_DIRS = ("references", "assets")
READABLE_RE = re.compile(r"^(references|assets)/[a-z0-9][a-z0-9._-]{0,60}\.(md|json|jsonl|txt|csv)$")
MAX_FILE_CHARS = 8_000
CHARS_PER_TOKEN = 3.2  # same estimate as the context builder
# Level 1 for every skill together, and for one helper's prompt.
METADATA_CAP_TOKENS = 2_000
HELPER_METADATA_CAP_TOKENS = 600
FILE_REF_RE = re.compile(r"\b(?:references|assets)/[a-z0-9][a-z0-9._-]*\.(?:md|json|jsonl|txt|csv)\b")
PLACEHOLDER_RE = re.compile(r"\{\{([a-z_]+)\}\}")


def est_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def skills_root(env: dict[str, str] | None = None) -> Path:
    e = os.environ if env is None else env
    raw = e.get("MIMI_SKILLS_DIR", "").strip()
    return Path(raw).resolve() if raw else ROOT


def _files(folder: Path) -> list[Path]:
    out = []
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder)
        if p.is_symlink():
            raise SkillError(f"{folder.name}/{rel}: symlinks are not allowed in a skill")
        if not p.is_file() or rel.parts[0] in UNHASHED_DIRS or "__pycache__" in rel.parts:
            continue
        if p.name.startswith("."):  # .gitkeep and the like: not content
            continue
        if p.suffix in (".pyc",):
            continue
        out.append(p)
    return out


def digest(folder: Path) -> str:
    h = hashlib.sha256()
    for p in _files(folder):
        h.update(p.relative_to(folder).as_posix().encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


@dataclass
class Catalog:
    root: Path
    skills: dict[str, SkillDoc] = field(default_factory=dict)  # key -> doc
    cores: dict[str, str] = field(default_factory=dict)  # helper -> header text
    script_owner: dict[str, str] = field(default_factory=dict)  # script -> skill key

    # ---------------------------------------------------------- lookups

    def by_name(self, name: str) -> SkillDoc | None:
        return next((s for s in self.skills.values() if s.name == name), None)

    def for_helper(self, helper: str, *, drafts: bool = False) -> list[SkillDoc]:
        return sorted((s for s in self.skills.values()
                       if s.helper == helper and (drafts or s.active)), key=lambda s: s.name)

    def active(self) -> list[SkillDoc]:
        return sorted((s for s in self.skills.values() if s.active), key=lambda s: s.key)

    def folder(self, key: str) -> Path:
        return self.root / self.skills[key].name

    @property
    def version(self) -> str:
        """One hash over every active skill's tag: changes with any skill."""
        tags = ",".join(s.tag for s in self.active())
        return "skills-" + hashlib.sha256(tags.encode()).hexdigest()[:12]

    # ---------------------------------------------------------- level 1

    def metadata(self, helper: str) -> str:
        lines = [s.metadata_line() for s in self.for_helper(helper)]
        return "\n".join(lines)

    def metadata_tokens(self) -> int:
        return est_tokens("\n".join(s.metadata_line() for s in self.active()))

    # ---------------------------------------------------------- level 2

    def body(self, key: str, values: dict[str, str] | None = None) -> str:
        doc = self.skills[key]
        text = doc.body
        vals = values or {}

        def fill(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in vals:
                raise SkillError(f"{key}: no value for {{{{{name}}}}}")
            return vals[name]

        return PLACEHOLDER_RE.sub(fill, text)

    # ---------------------------------------------------------- level 3

    def read_file(self, key: str, rel: str) -> str:
        """A reference or asset of one skill. Never a script, never outside the folder."""
        if key not in self.skills:
            raise SkillError(f"no skill {key!r}")
        if not READABLE_RE.match(rel) or ".." in rel:
            raise SkillError("only references/ or assets/ files (.md, .json, .jsonl, .txt, "
                             ".csv) can be read")
        folder = self.folder(key).resolve()
        path = (folder / rel).resolve()
        if folder not in path.parents or path.is_symlink() or not path.is_file():
            raise SkillError(f"{rel} is not a file of {key}")
        text = path.read_text(encoding="utf-8")
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + "\n[cut: file longer than the read limit]"
        return text

    def files(self, key: str) -> list[str]:
        folder = self.folder(key)
        return [p.relative_to(folder).as_posix() for p in _files(folder)
                if p.relative_to(folder).parts[0] in READABLE_DIRS]


def load_catalog(root: Path | None = None, *, check_lock: bool = True) -> Catalog:
    root = (root or skills_root()).resolve()
    if not root.is_dir():
        raise SkillError(f"no skills folder at {root}")
    lock: dict[str, dict[str, str]] = {}
    lock_path = root / LOCK_NAME
    if check_lock:
        if not lock_path.is_file():
            raise SkillError(f"{LOCK_NAME} is missing: run python -m mimi.skills lock")
        lock = json.loads(lock_path.read_text(encoding="utf-8"))["skills"]
    cat = Catalog(root=root)
    core = root / CORE_DIR
    if core.is_dir():
        for p in sorted(core.glob("*.md")):
            cat.cores[p.stem] = p.read_text(encoding="utf-8").strip()
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        if folder.name in SKIP_DIRS or folder.name.startswith("."):
            continue
        md = folder / "SKILL.md"
        if not md.is_file():
            raise SkillError(f"{folder.name}: no SKILL.md")
        doc = parse(md.read_text(encoding="utf-8"), where=f"{folder.name}/SKILL.md")
        if doc.name != folder.name:
            raise SkillError(f"{folder.name}: name {doc.name!r} must match its folder")
        d = digest(folder)
        if check_lock:
            want = lock.get(doc.name, {}).get("sha256")
            if want != d:
                raise SkillError(f"{doc.name}: files changed since the lock was reviewed "
                                 f"(run python -m mimi.skills lock in a PR)")
            if lock[doc.name].get("version") != doc.version:
                raise SkillError(f"{doc.name}: version {doc.version} is not the locked one")
        doc = SkillDoc(**{**doc.__dict__, "digest": d})
        if doc.key in cat.skills:
            raise SkillError(f"{doc.key} defined twice")
        cat.skills[doc.key] = doc
        scripts_dir = folder / "scripts"
        found = sorted(p.stem for p in scripts_dir.glob("*.py")
                       if not p.name.startswith("_")) if scripts_dir.is_dir() else []
        for script in found:
            if script in cat.script_owner:
                raise SkillError(f"script {script} is defined twice")
            cat.script_owner[script] = doc.key
    for s in cat.skills.values():
        for h in s.handoffs or ():
            other = cat.skills.get(f"{s.helper}/{h}")
            if other is None:
                raise SkillError(f"{s.name}: handoff {h} is not a {s.helper} skill")
        for script in s.scripts:
            if script not in cat.script_owner:
                raise SkillError(f"{s.name}: script {script} has no file in any skill's scripts/")
    if check_lock:
        extra = set(lock) - {s.name for s in cat.skills.values()}
        if extra:
            raise SkillError(f"lock lists skills with no folder: {sorted(extra)}")
    return cat


def write_lock(root: Path | None = None) -> Path:
    """Re-hash every skill (the reviewed step a person runs in a PR)."""
    cat = load_catalog(root, check_lock=False)
    data = {"comment": "Reviewed skill contents. Regenerate with python -m mimi.skills lock "
                       "in the PR that changes a skill; CI refuses a skill that does not match.",
            "skills": {s.name: {"version": s.version, "sha256": s.digest}
                       for s in sorted(cat.skills.values(), key=lambda s: s.name)}}
    path = cat.root / LOCK_NAME
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


_CACHE: dict[Path, Catalog] = {}


def catalog(root: Path | None = None) -> Catalog:
    """The process-wide catalogue (loaded once per root)."""
    r = (root or skills_root()).resolve()
    if r not in _CACHE:
        _CACHE[r] = load_catalog(r)
    return _CACHE[r]

"""A skill on disk: one folder, a ``SKILL.md`` with front matter, and its files.

    skills/<name>/
      SKILL.md       front matter (below) + the body the model reads when the skill runs
      scripts/       vetted pure Python, registered as typed read tools
      references/    facts the model reads on demand (read_skill)
      assets/        reply templates the model reads on demand
      tests/         unit tests for the scripts and routing cases for the eval

Front matter is a strict subset of YAML, read by this module (no YAML
dependency, no surprises): one ``key: value`` per line, lists as
``[a, b, c]``, ``true``/``false``. Unknown keys and missing required keys
are errors, so a typo never silently drops a rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Tier = Literal["auto", "notify", "approve"]
TIER_ORDER: dict[str, int] = {"auto": 0, "notify": 1, "approve": 2}
STATUSES = ("active", "draft")

REQUIRED = ("name", "description", "helper", "version", "status", "tier", "consent", "tools")
LISTS = ("consent", "tools", "scripts", "senders", "planned_tools", "handoffs")
BOOLS = ("money",)
OPTIONAL = ("scripts", "senders", "money", "planned_tools", "handoffs")
KNOWN = frozenset(REQUIRED + OPTIONAL)

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
TOOL_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")
SCOPE_RE = re.compile(r"^(shop|org):[a-z_*]+$")
MAX_DESCRIPTION = 400
MAX_BODY = 6_000


class SkillError(ValueError):
    """A skill that must not load. Raised at startup, never mid-run."""


@dataclass(frozen=True)
class SkillDoc:
    name: str
    description: str
    helper: str
    version: str
    status: str
    tier: Tier
    consent: tuple[str, ...]
    tools: tuple[str, ...]
    scripts: tuple[str, ...]
    senders: tuple[str, ...]
    money: bool
    planned_tools: tuple[str, ...]
    body: str
    # The sibling skills (same helper) this one may hand a run to. None (the
    # key left out) lists every sibling; [] lists none, and then read_skill is
    # offered only when the body names one of the skill's own files.
    handoffs: tuple[str, ...] | None = None
    digest: str = ""  # sha256 of the folder's vetted files (catalog fills it in)

    @property
    def key(self) -> str:
        return f"{self.helper}/{self.name}"

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def tag(self) -> str:
        """What goes on traces and in ``Helper.skill_version``."""
        return f"skill:{self.key}@{self.version}+{self.digest[:8]}"

    def metadata_line(self) -> str:
        return f"- {self.name}: {self.description}"


def _value(key: str, raw: str) -> object:
    raw = raw.strip()
    if key in LISTS:
        if not (raw.startswith("[") and raw.endswith("]")):
            raise SkillError(f"{key}: expected a [list]")
        inner = raw[1:-1].strip()
        return tuple(x.strip() for x in inner.split(",") if x.strip()) if inner else ()
    if key in BOOLS:
        if raw not in ("true", "false"):
            raise SkillError(f"{key}: expected true or false")
        return raw == "true"
    return raw


def parse(text: str, *, where: str = "SKILL.md") -> SkillDoc:
    """Parse one SKILL.md. Raises ``SkillError`` on anything unexpected."""
    if not text.startswith("---\n"):
        raise SkillError(f"{where}: must start with front matter (---)")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise SkillError(f"{where}: front matter is not closed")
    head, body = text[4:end], text[end + 5:].strip()
    data: dict[str, object] = {}
    for n, line in enumerate(head.splitlines(), 2):
        if not line.strip():
            continue
        key, sep, rest = line.partition(":")
        key = key.strip()
        if not sep or key not in KNOWN:
            raise SkillError(f"{where}:{n}: unknown front matter line {line[:40]!r}")
        if key in data:
            raise SkillError(f"{where}:{n}: {key} given twice")
        try:
            data[key] = _value(key, rest)
        except SkillError as exc:
            raise SkillError(f"{where}:{n}: {exc}") from None
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        raise SkillError(f"{where}: missing {', '.join(missing)}")
    doc = SkillDoc(
        name=str(data["name"]), description=str(data["description"]),
        helper=str(data["helper"]), version=str(data["version"]),
        status=str(data["status"]), tier=str(data["tier"]),  # type: ignore[arg-type]
        consent=tuple(data["consent"]),  # type: ignore[arg-type]
        tools=tuple(data["tools"]),  # type: ignore[arg-type]
        scripts=tuple(data.get("scripts", ())),  # type: ignore[arg-type]
        senders=tuple(data.get("senders", ())),  # type: ignore[arg-type]
        money=bool(data.get("money", False)),
        planned_tools=tuple(data.get("planned_tools", ())),  # type: ignore[arg-type]
        body=body,
        handoffs=(tuple(data["handoffs"])  # type: ignore[arg-type]
                  if "handoffs" in data else None))
    _check(doc, where)
    return doc


def _check(d: SkillDoc, where: str) -> None:
    if not NAME_RE.match(d.name):
        raise SkillError(f"{where}: bad name {d.name!r}")
    if not VERSION_RE.match(d.version):
        raise SkillError(f"{where}: version must be MAJOR.MINOR.PATCH")
    if d.status not in STATUSES:
        raise SkillError(f"{where}: status must be one of {STATUSES}")
    if d.tier not in TIER_ORDER:
        raise SkillError(f"{where}: tier must be auto, notify or approve (never is not a tier)")
    if not d.description or len(d.description) > MAX_DESCRIPTION:
        raise SkillError(f"{where}: description must be 1 to {MAX_DESCRIPTION} characters")
    if not d.body or len(d.body) > MAX_BODY:
        raise SkillError(f"{where}: body must be 1 to {MAX_BODY} characters")
    for t in d.tools + d.scripts + d.planned_tools:
        if not TOOL_RE.match(t):
            raise SkillError(f"{where}: bad tool name {t!r}")
    for h in d.handoffs or ():
        if not NAME_RE.match(h) or h == d.name:
            raise SkillError(f"{where}: bad handoff {h!r}")
    for s in d.consent:
        if not SCOPE_RE.match(s):
            raise SkillError(f"{where}: bad consent scope {s!r}")
    missing = [s for s in d.scripts if s not in d.tools and d.active]
    if missing:
        raise SkillError(f"{where}: scripts {missing} must also be listed in tools")
    if d.active and d.planned_tools:
        raise SkillError(f"{where}: an active skill cannot list planned_tools")

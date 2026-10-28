"""Scripts as tools, safely.

A skill's ``scripts/<name>.py`` is deterministic code the model used to
redo by hand (counting days to a promised Friday, reading a pasted alert).
It runs as a typed read tool; the model never writes or runs code.

What a script must be, checked before it is imported (``audit``) and in CI:

* **Pure**: imports only from ``ALLOWED_IMPORTS`` (stdlib maths, dates and
  text, pydantic, and named pure Mimi modules); no ``open``, ``eval``,
  ``exec``, ``__import__``, ``getattr`` games or dunder reach-arounds. No
  network, no files, no writes.
* **Typed**: ``In`` and ``Out`` Pydantic models with ``extra="forbid"`` and
  ``run(args: In, now: datetime) -> Out``; the module docstring is the tool
  description (description plus schema under the registry's 4,000 chars).
* **Honest about figures** (``FIGURES_FROM``): ``none`` (no money), ``claim``
  (money read from shop text: no ``*_kobo`` output key, so its figures can
  never become a source for the figure check) or ``book`` (sums of book money:
  must be fed by code from Go reads, so it is never a model-callable tool).
* **Vetted**: its bytes are in the skill's locked digest; a changed script
  does not load until a reviewed PR re-locks it.

Registered tools are ``kind="read"``, Go-policy tier ``auto``, scope
``org:plan``: a script can only compute. Anything that acts is still a
proposed intent from a harness tool, through policy and Go.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import BaseModel

from mimi.skills.spec import SkillError

ALLOWED_IMPORTS = frozenset({
    "__future__", "calendar", "collections.abc", "dataclasses", "datetime", "decimal",
    "math", "re", "typing", "unicodedata", "zoneinfo", "pydantic",
    # Pure Mimi modules a script may reuse instead of duplicating them.
    "mimi.voice.promise", "mimi.proof.parse", "mimi.proof.banks", "mimi.orders.words",
    "mimi.orders.catalogue",
})
BANNED_NAMES = frozenset({"open", "eval", "exec", "compile", "__import__", "input", "globals",
                          "locals", "vars", "getattr", "setattr", "delattr", "breakpoint",
                          "exit", "quit", "memoryview"})
FIGURE_MODES = ("none", "claim", "book")
CALLABLE_MODES = ("none", "claim")


@dataclass(frozen=True)
class Script:
    name: str
    skill_key: str
    path: Path
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    run: Callable[[Any, datetime], BaseModel]
    figures_from: str

    @property
    def model_callable(self) -> bool:
        return self.figures_from in CALLABLE_MODES


def audit(source: str, where: str) -> list[str]:
    """Every rule the script breaks (empty when it is pure)."""
    problems: list[str] = []
    tree = ast.parse(source, filename=where)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS:
                    problems.append(f"imports {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "") not in ALLOWED_IMPORTS:
                problems.append(f"imports from {node.module or '.'}")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            problems.append(f"uses {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") \
                and node.attr not in ("__name__",):
            problems.append(f"reaches {node.attr}")
        elif isinstance(node, ast.Global | ast.Nonlocal):
            problems.append("uses global state")
    if ast.get_docstring(tree) is None:
        problems.append("has no module docstring (it is the tool description)")
    return problems


def _kobo_keys(schema: Any, path: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(schema, dict):
        for k, v in (schema.get("properties") or {}).items():
            if k.endswith("_kobo") or k == "preview":
                out.append(f"{path}{k}")
            out.extend(_kobo_keys(v, f"{path}{k}."))
        for k in ("items", "additionalProperties"):
            if isinstance(schema.get(k), dict):
                out.extend(_kobo_keys(schema[k], path))
        for d in (schema.get("$defs") or {}).values():
            out.extend(_kobo_keys(d, path))
        for k in ("anyOf", "allOf", "oneOf"):
            for v in schema.get(k) or ():
                out.extend(_kobo_keys(v, path))
    return out


def load_script(name: str, skill_key: str, path: Path) -> Script:
    source = path.read_text(encoding="utf-8")
    problems = audit(source, str(path))
    if problems:
        raise SkillError(f"script {name} is not pure: {'; '.join(sorted(set(problems)))}")
    mod_name = f"mimi_skill_scripts.{name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise SkillError(f"script {name} cannot be imported")
    mod: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    for attr in ("In", "Out", "run", "FIGURES_FROM"):
        if not hasattr(mod, attr):
            raise SkillError(f"script {name} has no {attr}")
    inp, outp = mod.In, mod.Out
    for label, model in (("In", inp), ("Out", outp)):
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise SkillError(f"script {name}: {label} must be a Pydantic model")
        if model.model_config.get("extra") != "forbid":
            raise SkillError(f"script {name}: {label} must set extra='forbid'")
    mode = str(mod.FIGURES_FROM)
    if mode not in FIGURE_MODES:
        raise SkillError(f"script {name}: FIGURES_FROM must be one of {FIGURE_MODES}")
    if mode in CALLABLE_MODES:
        bad = _kobo_keys(outp.model_json_schema())
        if bad:
            raise SkillError(f"script {name}: FIGURES_FROM={mode} but its output has "
                             f"figure-source keys {bad}")
    desc = " ".join((mod.__doc__ or "").split())
    return Script(name=name, skill_key=skill_key, path=path, description=desc,
                  input_model=inp, output_model=outp, run=mod.run, figures_from=mode)


def load_scripts(cat: Any) -> dict[str, Script]:
    """Every script in the catalogue, audited and imported."""
    out: dict[str, Script] = {}
    for name, key in sorted(cat.script_owner.items()):
        path = cat.folder(key) / "scripts" / f"{name}.py"
        out[name] = load_script(name, key, path)
    return out

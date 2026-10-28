"""The skill tools: ``read_skill`` and every vetted script.

All are read tools (Go-policy tier ``auto``, scope ``org:plan``): they hand
the model trusted text from the repo or a computed answer, and change
nothing. A skill tool reads only skills of the run's own helper, so a shop
message cannot pull another helper's instructions into the run, and reading
another skill never adds a tool to the run.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mimi.harness.errors import ToolError
from mimi.harness.registry import ToolContext, ToolRegistry, ToolSpec
from mimi.skills.catalog import Catalog
from mimi.skills.scripts import Script, load_scripts
from mimi.skills.spec import SkillError

READ_SKILL = "read_skill"
SKILL_TOOLS = (READ_SKILL,)
SCOPE = "org:plan"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadIn(_In):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,40}$", description="A skill name.")
    file: str = Field("", max_length=80,
                      description="Empty for its instructions, or references/... or assets/...")


class ReadOut(_Out):
    skill: str
    version: str = ""
    file: str = ""
    text: str
    files: list[str] = Field(default_factory=list)


def _own(cat: Catalog, ctx: ToolContext, name: str) -> Any:
    doc = cat.by_name(name)
    if doc is None or not doc.active or doc.helper != ctx.helper:
        raise ToolError(f"No skill {name!r} for the {ctx.helper} helper.",
                        "Use a skill name from your instructions.")
    return doc


def _walk_kobo(node: Any) -> bool:
    if isinstance(node, dict):
        return any(k.endswith("_kobo") or k == "preview" or _walk_kobo(v)
                   for k, v in node.items())
    if isinstance(node, list):
        return any(_walk_kobo(v) for v in node)
    return False


def register_skill_tools(registry: ToolRegistry, cat: Catalog,
                         values: dict[str, str] | None = None) -> dict[str, Script]:
    """Register the skill tools and every model-callable script. Returns the scripts."""
    vals = dict(values or {})

    def read_skill(ctx: ToolContext, args: ReadIn) -> ReadOut:
        doc = _own(cat, ctx, args.name)
        if not args.file or args.file == "SKILL.md":
            return ReadOut(skill=doc.name, version=doc.version, text=cat.body(doc.key, vals),
                           files=cat.files(doc.key))
        try:
            text = cat.read_file(doc.key, args.file)
        except SkillError as exc:
            raise ToolError(str(exc), f"Files of {doc.name}: "
                            + (", ".join(cat.files(doc.key)) or "none")) from None
        return ReadOut(skill=doc.name, version=doc.version, file=args.file, text=text)

    read_skill.__doc__ = ("Read one of your skills: its instructions (file empty) or a file "
                          "they name (references/... or assets/...). Text only; adds no tools.")
    if READ_SKILL not in registry:
        registry.tool(kind="read", scope=SCOPE, tier="auto", cost_tier="knowledge")(read_skill)

    scripts = load_scripts(cat)
    used = {n for d in cat.active() for n in d.scripts}
    for s in scripts.values():
        # Only scripts an active skill lists, and never a book-figures script.
        if s.name not in used or not s.model_callable or s.name in registry:
            continue
        registry.register(_script_spec(s))
    return scripts


def _script_spec(s: Script) -> ToolSpec:
    def fn(ctx: ToolContext, args: Any) -> BaseModel:
        out = s.run(args, ctx.now)
        if _walk_kobo(out.model_dump(mode="json")):
            # Never reached for an audited script; fail safe if it ever is.
            raise ToolError(f"{s.name} returned a figure-source field.",
                            "Do not use this result; tell the owner it needs a look.")
        return out

    return ToolSpec(name=s.name, description=s.description, input_model=s.input_model,
                    output_model=s.output_model, kind="read", scope=SCOPE, tier="auto",
                    idem=(), fn=fn, dry_run=None, cost_tier="knowledge",
                    intent_kind=s.name, summary=s.description.split(". ")[0][:120])

"""Bind skills to helpers: one ``Helper`` config per routed skill (skills mode).

The router already picks ``helper/skill`` for every wake-up. In skills mode
``build_helpers`` adds one entry per active skill, keyed by that skill, and
the agent's existing lookup (``helpers.get(skill) or helpers.get(helper)``)
picks it up. That entry is the load step: its prompt carries

1. the helper's header (``skills/_core/<helper>.md``),
2. the routed skill's SKILL.md body (level 2),
3. the names and descriptions of the helper's other active skills (level 1),
   or only those named in the skill's ``handoffs`` when it gives that key,

and its tools are the skill's own list, cut down from the helper's toolset,
plus the skill's vetted scripts, plus ``read_skill`` when there is something
to read: a file the body names, or a sibling skill listed in its prompt. A
skill with neither (``handoffs: []`` and no file named) pays for no
``read_skill`` schema and no sibling list.

What a skill can never do (checked here, at startup):

* add a harness tool its helper does not have: anything outside the base
  toolset is refused;
* raise its tier: every tool's tier is read from Go policy, and a tool whose
  policy tier is above the skill's declared ``tier`` is refused; a skill
  that declares a higher tier than its helper's tools carry is refused;
* load from anywhere but the vetted catalogue (``mimi.skills.catalog``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from mimi.harness.loop import Helper
from mimi.harness.policy import RiskPolicy
from mimi.harness.registry import ToolRegistry, build_toolset
from mimi.skills.catalog import FILE_REF_RE, HELPER_METADATA_CAP_TOKENS, Catalog, est_tokens
from mimi.skills.spec import TIER_ORDER, SkillDoc, SkillError
from mimi.skills.tools import SKILL_TOOLS

log = logging.getLogger(__name__)


def siblings(cat: Catalog, doc: SkillDoc) -> list[SkillDoc]:
    """The other skills this one's prompt lists (all, or only its handoffs)."""
    return [s for s in cat.for_helper(doc.helper) if s.key != doc.key
            and (doc.handoffs is None or s.name in doc.handoffs)]


def offers_read(cat: Catalog, doc: SkillDoc) -> bool:
    """Whether a run of this skill has anything for read_skill to read."""
    return bool(FILE_REF_RE.search(doc.body) or siblings(cat, doc))


def skill_text(cat: Catalog, doc: SkillDoc, values: Mapping[str, str] | None = None) -> str:
    """The helper prompt for one routed skill (after the core rules)."""
    if est_tokens(cat.metadata(doc.helper)) > HELPER_METADATA_CAP_TOKENS:
        raise SkillError(f"{doc.helper}: skill metadata over {HELPER_METADATA_CAP_TOKENS} tokens")
    header = cat.cores.get(doc.helper, "")
    parts = [header] if header else []
    body = cat.body(doc.key, dict(values or {}))
    files = (f"\n(Files named here: read_skill name={doc.name} with the file.)"
             if FILE_REF_RE.search(body) else "")
    parts.append(f"# Skill: {doc.name} {doc.version}+{doc.digest[:8]}\n{body}{files}")
    others = [s.metadata_line() for s in siblings(cat, doc)]
    if others:
        parts.append("<skills>\nYour other skills (read_skill gives one's instructions if "
                     "the message turns out to need it):\n" + "\n".join(others) + "\n</skills>")
    return "\n\n".join(parts)


def skill_tools(doc: SkillDoc, base: Helper, policy: RiskPolicy, registry: ToolRegistry,
                script_names: set[str], *, read: bool = True) -> tuple[str, ...]:
    """The tools one skill gets: never more than its helper has, never a higher tier."""
    have = set(base.toolset.names)
    granted: list[str] = []
    ceiling = TIER_ORDER[doc.tier]
    for name in doc.tools:
        if name in script_names:
            if name not in doc.scripts:
                raise SkillError(f"{doc.key}: {name} is a script; list it under scripts")
            if name in registry:
                granted.append(name)
            continue
        if name not in have:
            # Not in the helper's toolset (or not registered in this build):
            # a skill can only narrow its helper, never add to it.
            log.info("skill %s lists %s, which the %s helper does not have: not granted",
                     doc.key, name, base.name)
            continue
        tier = policy.base_tier(name)
        if tier == "never" or TIER_ORDER.get(tier, 99) > ceiling:
            raise SkillError(f"{doc.key}: {name} is tier {tier} in Go policy, above the "
                             f"skill's tier {doc.tier}")
        granted.append(name)
    top = max((TIER_ORDER.get(policy.base_tier(n), 0) for n in have), default=0)
    if ceiling > top:
        raise SkillError(f"{doc.key}: declares tier {doc.tier}, above anything the "
                         f"{base.name} helper can propose")
    if read:
        granted.extend(t for t in SKILL_TOOLS if t in registry and t not in granted)
    return tuple(granted)


def bind_all(helpers: dict[str, Helper], registry: ToolRegistry, policy: RiskPolicy,
             cat: Catalog, *, values: Mapping[str, str] | None = None,
             scripts: set[str] | None = None, routed: set[str] | None = None) -> dict[str, Helper]:
    """``helpers`` plus one entry per active skill (keyed ``helper/skill``)."""
    from mimi.harness.skills import BY_KEY

    keys = routed if routed is not None else set(BY_KEY)
    script_names = scripts if scripts is not None else set(cat.script_owner)
    out = dict(helpers)
    for doc in cat.active():
        if doc.key not in keys:
            continue
        base = helpers.get(doc.key) or helpers.get(doc.helper)
        if base is None:
            continue
        tools = skill_tools(doc, base, policy, registry, script_names,
                            read=offers_read(cat, doc))
        ts = build_toolset(registry, base.name, tools, policy)
        out[doc.key] = replace(base, skill=skill_text(cat, doc, values), toolset=ts,
                               skill_version=doc.tag)
    return out


def describe(h: Any) -> dict[str, Any]:
    """What a bound helper carries (for the eval and the docs)."""
    return {"skill_version": h.skill_version, "tools": list(h.toolset.names),
            "prompt_chars": len(h.system_prompt())}

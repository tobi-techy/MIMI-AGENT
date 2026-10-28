"""Mimi's skills: folders of instructions, scripts and references (RAI-314).

See ``docs/SKILLS.md``. The pieces:

* ``spec``: one SKILL.md and its front matter;
* ``catalog``: the vetted catalogue under ``skills/`` (locked by hash), and
  the three levels of progressive disclosure;
* ``scripts``: pure scripts as typed read tools (audited before import);
* ``tools``: ``read_skill`` and the script tools;
* ``bind``: one helper config per routed skill, behind ``MIMI_SKILLS``;
* ``learn``: repeated reasoning and owner corrections become draft skill
  changes for a person to review, never live ones;
* the skills eval is ``mimi.api.skills_eval`` (it builds the API's helpers).
"""

from mimi.skills.catalog import Catalog, catalog, load_catalog
from mimi.skills.flag import enabled
from mimi.skills.spec import SkillDoc, SkillError

__all__ = ["Catalog", "SkillDoc", "SkillError", "catalog", "enabled", "load_catalog"]

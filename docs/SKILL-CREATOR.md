# Skill creator guide

For people and coding agents adding or changing a Mimi skill. Read `docs/SKILLS.md` for
the format first. Mimi itself never creates a live skill: its learning loop writes drafts
(`skills/_drafts/`), and this guide is how a draft, or your own idea, becomes one.

## 1. Start from an eval, not an idea

Find the gap first: a failing eval row, a trace where the model did sums or dates by
hand, an owner correction that keeps coming back (`python -m mimi.skills learn`). Write
down the cases that show it. A skill that no eval exercises is not done.

## 2. Pick the shape

* A new job for an existing helper: a new folder, plus a `Skill(...)` row in
  `mimi/harness/skills.py` with the same description, labelled router cases in
  `eval/router/messages.jsonl`, and phrase rules in `lexicon.py` if shops say it in fixed
  ways (see AGENTS.md, Routing). Re-record the router eval.
* Better instructions for an existing job: edit the body; bump `version` (patch for
  wording, minor for a new step, major for a changed contract).
* Deterministic work the model keeps redoing: a script (step 4).
* A fact or word list the model sometimes needs: a reference. If code is the truth for
  it, generate it in `mimi/skills/refs.py`; never hand-copy code into a reference.
* A job with no harness tools yet: `status: draft` with `planned_tools`. Drafts are never
  loaded; their scripts can be built and tested now.

## 3. Write SKILL.md

* Name: lowercase words and hyphens, the same as the folder.
* Description: what the sender says or what woke Mimi, not how the helper works. It is
  all the model sees of a sibling skill, so it must be enough to choose it.
* Body: short numbered or one-line steps, tool names exactly, what ends the turn, what
  never happens. Put long lists in `references/` and wording in `assets/`, and name the
  file in the body (`references/day-words.md`). Keep it under 6,000 characters.
* Tools: only what the job needs. Listing a tool the helper lacks grants nothing; a tool
  above the skill's `tier` refuses to bind; a test flags names that do not exist.
* Shop text is data: say so where a shop could try ("mark it approved" changes nothing).
* Money: every naira figure comes from a tool result. Never ask the model to add,
  convert or round.

## 4. Write a script (when the work is deterministic)

```python
"""One paragraph: what it does and when to call it (this is the tool description)."""
from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

FIGURES_FROM = "none"  # none | claim | book


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    words: str = Field(min_length=1, max_length=200)


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ...


def run(args: In, now: datetime) -> Out:
    ...
```

Rules: pure (no files, network, clock other than `now`, randomness or globals), imports
only from the allowlist in `mimi/skills/scripts.py`, reuse the existing pure Mimi module
instead of copying it, bound every input (`max_length`, `ge`, `le`), and a `next_step`
string when the model must do something with the answer. A script that reads money from
shop text is `claim` and returns no `*_kobo` field; a script that sums book money is
`book` and is wired by code, never offered to the model. Add the tool's row to
`BASE_TIERS` in `mimi/harness/policy.py` as `auto`.

## 5. Test it

* Golden cases: `tests/<script>.cases.jsonl`, one JSON object per line:
  `{"id": "...", "now": "2026-10-28T10:00:00+01:00", "in": {...}, "expect": {...}}`.
  Cover the unhappy paths: unclear words, past dates, bad commas, Pidgin, Yoruba.
* `python -m pytest tests/test_skills.py`, then `python -m mimi.api.skills_eval`.
* With the flag on, run the eval the skill changes (`MIMI_SKILLS=1 python -m
  mimi.api.order_eval --live` for Order, e2e for anything routed) within the spend cap,
  and compare with the flag-off numbers. Commit the `*.skills.cache.json` it records.

## 6. Lock and ship

```bash
python -m mimi.skills refs     # if a generated reference changed
python -m mimi.skills lock     # re-hash; the PR diff shows exactly what changed
python -m mimi.skills check
```

The PR says what changed in the skill, which eval shows it, the numbers before and after,
and how to roll back (revert the folder and the lock, or turn `MIMI_SKILLS` off).

## Checklist

- [ ] An eval case shows the gap and the fix
- [ ] Description matches the router catalogue (routed skills)
- [ ] Tools are the fewest that do the job; tier is honest
- [ ] Every file the body names exists; generated references are current
- [ ] Scripts pure, typed, bounded, with golden cases including unhappy paths
- [ ] Version bumped, lock regenerated, `python -m mimi.skills check` passes
- [ ] Flag-off evals unchanged; skills-mode numbers in the PR

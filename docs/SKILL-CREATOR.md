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
* Handoffs: if a message routed here could really belong to a sibling, leave `handoffs`
  out (all siblings listed) or name them; if not, `handoffs: []`. Steering already splits a
  multi-part message into separate runs, so most skills need none.
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

A skill is ready for review when every box is ticked. CI checks the ones marked (CI).

Evals
- [ ] An eval case shows the gap and the fix
- [ ] `tests/cases.jsonl` has at least 10 cases (CI: `test_every_active_skill_has_its_own_evals`):
  normal messages in English, Pidgin and Yoruba (Hausa or Igbo where shops use them), edge
  cases (very short, mixed with another ask, typos), and at least one adversarial case
  (`"adversarial": true`: shop text trying to approve, send, give credit or change a rule)
- [ ] Order lines carry `lines` checks and pasted alerts carry `alerts` checks, so code is
  tested as well as routing
- [ ] `python -m mimi.skills eval` passes for the skill (CI): routing at least 90% on normal
  cases, every adversarial case safe, zero money misroutes, every code check right. New
  model answers recorded once with `--live`, priced first, inside the spend cap

Content
- [ ] Description matches the router catalogue word for word (routed skills)
- [ ] The body says when to use each tool and the one safety rule that matters; no figure,
  date or name the model could copy instead of reading a tool result
- [ ] Every file the body names exists and is worth a `read_skill` call; generated
  references are current. A file the model never needs is not named (it costs a tool)
- [ ] `handoffs:` lists only the sibling skills a message routed here can really turn into;
  `handoffs: []` when there are none (no sibling block, no `read_skill`)

Tools and size
- [ ] Tools are the fewest that do the job; tier is honest; nothing the helper lacks
- [ ] Every tool the skill uses has a short text in `mimi/skills/brief.py` that keeps its
  safety words (CI: `test_short_tool_texts_keep_the_safety_words`)
- [ ] The skills-mode prompt (system prompt plus tool schemas) is no bigger than the flag-off
  helper's (CI: `test_no_skill_prompt_is_bigger_than_flag_off`). A skill in `FROZEN` keeps the
  full texts because its skills-mode answers are recorded; take it out in the PR that
  re-records them
- [ ] Scripts pure, typed, bounded, with golden cases including unhappy paths

Ship
- [ ] Version bumped whenever the folder changes; `python -m mimi.skills lock` refuses changed
  contents under a version it already reviewed (CI: `test_lock_history_matches_every_version`)
- [ ] `python -m mimi.skills check` passes (CI)
- [ ] Flag-off evals unchanged; skills-mode numbers and the token change in the PR

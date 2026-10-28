# Skills

Mimi's skills follow Anthropic's "Don't Build Agents, Build Skills Instead" (Barry Zhang
and Mahesh Murag, AI Engineer) and "Equipping agents for the real world with Agent Skills":
a skill is a folder of instructions, scripts and references that the agent loads only
when a message needs it. One harness runs every helper; skills carry what each job needs.

Status: behind `MIMI_SKILLS` (default off). With the flag off nothing changes and every
eval replays its recorded answers. Linear: RAI-314.

## The format

```
skills/
  skills.lock.json          reviewed hashes of every skill folder
  _core/<helper>.md         one helper's header (who it is, what it never does)
  _drafts/                  the learning loop's review queue (never loaded)
  <name>/
    SKILL.md                front matter + the instructions the model reads
    scripts/<tool>.py       vetted pure Python, run as a typed read tool
    references/*.md         facts the model reads on demand
    assets/*.md             reply wording the model reads on demand
    tests/                  golden cases for the scripts (<tool>.cases.jsonl)
```

`SKILL.md` front matter (a strict subset of YAML; unknown keys are errors):

```
---
name: promise-to-pay
description: The shop says when it will pay or asks for more time: ...
helper: collector
version: 1.0.0
status: active                 # or draft (not loaded into any prompt)
tier: notify                   # the highest Go-policy tier its tools may carry
consent: [shop:balance, shop:invoices, shop:promises, shop:messages, org:plan, org:owner]
tools: [get_balance, get_invoices, set_followup, followup_hours, say_date, send_to_user, ask_question, update_plan]
scripts: [followup_hours, say_date]
senders: [shop, rep]
money: true
---
```

* `description` is what the router reads. For a routed skill it must match
  `mimi/harness/skills.py` word for word (a test checks), so the router behaves the same
  with the flag on or off.
* `tools` is everything the skill may call: harness tools, plus its scripts. It can only
  narrow the helper's toolset.
* `tier` is a ceiling, not a grant. Every tool's tier is read from Go policy at bind
  time and again for every call.
* `consent` names the scopes its tools read. Go still checks consent on every read.
* `planned_tools` (drafts only) lists tools the skill needs but the harness does not have.
* `handoffs` (optional) names the sibling skills a run of this one may plausibly turn
  into. Left out, the `<skills>` block lists every sibling; `handoffs: []` lists none.
  `read_skill` is offered only when there is something to read: a file the body names or
  a sibling in the block. A skill whose runs never change job (a stock count, a delivery
  reply, a bank credit) sets `handoffs: []` and saves the block and the tool schema.

## Progressive disclosure

| Level | What | When it is in the prompt |
|---|---|---|
| 1 | `name: description` of the helper's other active skills (or only its `handoffs`) | every run of that helper (`<skills>` block) |
| 2 | the routed skill's `SKILL.md` body | the run the router sent to that skill |
| 3 | a reference or asset | only when the model calls `read_skill` |

The router already picks `helper/skill` for each wake-up, so the load step is in code:
`build_helpers` (with the flag on) adds one helper config per routed skill, keyed by the
skill, and the agent's existing lookup (`helpers.get(skill) or helpers.get(helper)`)
takes it. Its prompt is the core rules, the helper header, the skill body and the
metadata of its siblings; its tools are the skill's list. The prefix is pinned per thread
and skill: a run on the same thread routed to another skill re-pins.

`read_skill(name, file)` is the one skill tool: a sibling skill's instructions (file
empty) or a `references/` or `assets/` file. It reads only skills of the run's own helper,
never a draft, never a script or test, never outside the folder, and it adds no tools.

Caps (tests): metadata for every skill together under 2,000 tokens; one helper's metadata
under 600.

## Scripts as tools, safely

When the model kept doing deterministic work by hand, the work becomes a script. The
model never writes or runs code: scripts are written by people (or proposed by the
learning loop and reviewed), committed, locked by hash and audited before import.

A script is `scripts/<tool>.py` with a module docstring (the tool description), `In` and
`Out` Pydantic models with `extra="forbid"`, `run(args: In, now: datetime) -> Out` and
`FIGURES_FROM`:

| `FIGURES_FROM` | Meaning | Model-callable |
|---|---|---|
| `none` | no money in or out | yes |
| `claim` | money read from shop text; no `*_kobo` output, so it can never become a source for the figure check | yes |
| `book` | sums of book money; must be fed by code from Go reads | no |

The audit (`mimi.skills.scripts.audit`, run at load and in CI) allows only stdlib maths,
dates and text, pydantic, and named pure Mimi modules; it refuses `open`, `eval`, `exec`,
`__import__`, `getattr` and dunder reach-arounds. Scripts are registered as `kind="read"`,
policy tier `auto`, scope `org:plan`. Anything that acts is still a proposed intent from a
harness tool, through policy and Go, and every message still passes the figure check.

Where the harness already does the work in code, the skill points at it instead of
copying it:

| Work | Where it is done |
|---|---|
| Pricing an order list, totals, credit headroom | `mimi.orders.order` (`price_line`, `credit_view`) via `submit_order` / `price_order` |
| Overdue ageing and the ladder | `mimi.harness.collector.ladder`, `get_invoices` |
| Credit recommendation | `mimi.credit.review.recommend` via `prepare_credit_review` |
| Screenshot checks | `mimi.proof.checks.assess` via `check_payment_screenshot` |
| Naira text | `*_naira` beside every `*_kobo` in tool results (`mimi.harness.loop.with_naira`) |

The scripts added here:

| Skill | Script | What it does | Status |
|---|---|---|---|
| promise-to-pay | `followup_hours` | shop's words for a day (English, Pidgin, Yoruba, Hausa, Igbo) to `in_hours` for `set_followup` (09:00 WAT the day after), using `mimi.voice.promise.find_dates` | active |
| collections | `say_date` | an ISO date from a tool as words (en, pcm, yo) with today/tomorrow | active |
| fake-alert-check | `parse_bank_alert` | a pasted SMS alert to bank, claimed amount, reference, time (claims only), using `mimi.proof.parse` | active |
| route-load-out | `sum_load_out` | orders summed by sku, visit order | draft |
| day-end-reconciliation | `reconcile_day` | stock and cash against confirmations, gaps over tolerance | draft (book figures) |
| stock-book-reading | `check_stock_sums` | two reads compared, totals checked, at most 5 questions | draft |

References written from code (`python -m mimi.skills refs`; a test fails on drift):
product nicknames and unit words, number words, day words, bank alert formats, the
overdue ladder, how the credit review works.

## Trust

Skill text is trusted only from the repo. Skills load from `skills/` (or
`MIMI_SKILLS_DIR`, set by the deploy) and must match `skills/skills.lock.json`; a changed
or new file refuses to load until a reviewed PR re-locks it. Symlinks are refused. Shop
text is data: it can name a skill, but it cannot load one from outside the run's helper,
change a body, add a tool or raise a tier.

## Versions

Each skill has a semantic `version` and a content digest. `Helper.skill_version` is
`skill:<helper>/<name>@<version>+<digest8>`: it is on every trace, in the context
manifest, and the body (with its version line) is in the request, so any change to a
skill changes the recorded-answer key and shows as a cache miss. `Catalog.versions()`
gives every skill's version for the cache-key work in RAI-309.

## The learning loop

`python -m mimi.skills learn <events.jsonl | --db URL>` reads the thread event log and
writes drafts to `skills/_drafts/queue.jsonl` (or `MIMI_SKILL_DRAFTS_DIR`) when, in at
least three different runs:

* the model filled an argument a script owns (`set_followup.in_hours` without
  `followup_hours`, `check_payment.amount_kobo` without `parse_bank_alert`): an
  instruction tweak or a proposed script;
* the owner or a shop corrected the same word the same way: a reference row (a product
  nickname, then `refs`) or a reply template tweak.

Drafts are never merged or turned on by Mimi. A person reads one with
`python -m mimi.skills learn --pr <id>` (a ready PR body), makes the change in a branch,
re-locks and bumps the version, and CI decides. Patterns on approval or money tools are
never drafted; shop words in a draft are defanged and cut short.

## Commands

```bash
python -m mimi.skills check      # catalogue, lock, references, script audit
python -m mimi.skills list       # every skill, its version, tier, tools and scripts
python -m mimi.skills refs       # rewrite the references generated from code
python -m mimi.skills lock       # re-hash the skills (in the PR that changes one)
python -m mimi.api.skills_eval   # the skills eval (selection, load, scripts, safety, tokens)
MIMI_SKILLS=1 python -m mimi.api.order_eval --no-write   # Order held-out in skills mode
MIMI_SKILLS=1 python -m mimi.api.e2e --no-write          # e2e in skills mode
```

Skills mode records model answers in `*.skills.cache.json` beside the flag-off caches,
and writes `*.skills.report.json`, so the two modes never mix.

## Adding a skill

See [SKILL-CREATOR.md](SKILL-CREATOR.md).

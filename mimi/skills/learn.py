"""The learning loop: experience becomes a DRAFT skill change for a person.

The talk's point ("Claude on day 30 is better than on day 1") applies, with
one rule for Mimi: nothing learned goes live without review. This module
reads what runs did (the thread event log, or an export of it as JSONL),
finds patterns that repeat across runs, and writes draft changes to a
review queue (``skills/_drafts/`` in the repo, or ``MIMI_SKILL_DRAFTS_DIR``).
A person turns a draft into a PR (``python -m mimi.skills learn --pr ID``
prints the patch and the PR body); CI and review decide. The catalogue never
reads ``_drafts/``, and the lock refuses any skill file that changed without
a reviewed re-lock, so a draft cannot run.

What counts as a pattern (each needs ``MIN_RUNS`` different runs):

* **repeated reasoning**: the model worked out an argument a script could
  compute (``COMPUTED_ARGS``: the hours to a promised day for set_followup);
  the draft proposes the script (or, when the skill already has one, a
  sharper instruction to use it);
* **owner or shop corrections**: the same correction made again and again
  ("sunrise" means noodles; "not 4, 6"); the draft proposes a reference row
  (a product nickname) or a reply template tweak.

Drafts never touch money: a pattern on a money-moving or approval tool is
dropped, and shop words in a draft are cut short and defanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from mimi.harness.untrusted import defang
from mimi.runtime.outbox import MONEY_VERBS

MIN_RUNS = 3
MAX_QUOTE = 80
DraftKind = Literal["reference_row", "template_tweak", "proposed_script", "instruction_tweak"]

# Arguments the model computes that a script should own: tool.arg -> what a
# script would compute it from, and which skill would hold it.
COMPUTED_ARGS: dict[str, tuple[str, str, str]] = {
    "set_followup.in_hours": ("collector/promise-to-pay", "followup_hours",
                              "hours from now to the day after a promised day"),
    "check_payment.amount_kobo": ("proof/fake-alert-check", "parse_bank_alert",
                                  "kobo from a pasted alert"),
}
# Tools whose patterns are never drafted (they decide money or need the owner).
NO_DRAFT_TOOLS = frozenset({"submit_order", "revise_order", "submit_credit_review",
                            "propose_reminder_plan", "flag_payment_proof",
                            "raise_daily_allowance"})


def drafts_dir(env: Mapping[str, str] | None = None) -> Path:
    e = os.environ if env is None else env
    raw = e.get("MIMI_SKILL_DRAFTS_DIR", "").strip()
    from mimi.skills.catalog import skills_root

    return Path(raw) if raw else skills_root(dict(e)) / "_drafts"


@dataclass
class Draft:
    id: str
    kind: DraftKind
    skill: str  # helper/skill
    target: str  # file in the skill folder the change would touch
    summary: str
    proposal: str  # the text a reviewer would add (a table row, a template, a script outline)
    evidence: list[str] = field(default_factory=list)  # run ids
    examples: list[str] = field(default_factory=list)  # defanged, cut short
    status: str = "draft"  # draft -> (a person) proposed_pr | rejected
    created_at: str = ""
    auto_merge: bool = False  # always False; kept explicit for the test

    def pr_body(self) -> str:
        ex = "\n".join(f"- {e}" for e in self.examples) or "- (none)"
        return (f"Draft skill change from Mimi's learning loop ({self.kind}).\n\n"
                f"Skill: {self.skill}\nFile: {self.target}\n\nWhat repeated: {self.summary}\n\n"
                f"Seen in {len(self.evidence)} runs: {', '.join(self.evidence[:10])}\n\n"
                f"Examples (shop words, defanged):\n{ex}\n\nProposed change:\n\n"
                f"{self.proposal}\n\nA person reviews this. It was not merged or turned on "
                "by Mimi; after review, re-lock the skill (python -m mimi.skills lock) and "
                "bump its version.")


def _quote(text: str) -> str:
    t = defang(" ".join(str(text).split()))
    return t[:MAX_QUOTE] + ("..." if len(t) > MAX_QUOTE else "")


def _did(*parts: str) -> str:
    return "d-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:10]


def _money_tool(name: str) -> bool:
    low = name.lower()
    return name in NO_DRAFT_TOOLS or any(v in low for v in MONEY_VERBS)


def mine(events: Iterable[Mapping[str, Any]], *, min_runs: int = MIN_RUNS,
         known_nicknames: Mapping[str, str] | None = None) -> list[Draft]:
    """Draft changes from an event log.

    Events are dicts with at least ``run_id`` and ``type``. Used here:

    * ``tool_called`` with ``tool``, ``skill`` (or ``data.skill``) and
      ``data.args``: a COMPUTED_ARGS argument the model filled itself;
    * ``owner_correction`` / ``shop_correction`` with ``data.said`` (the
      word) and ``data.meant`` (what it turned out to mean: a product family
      or a corrected template), and ``skill``.
    """
    computed_by_run: dict[str, set[str]] = defaultdict(set)  # key -> runs
    scripts_by_run: dict[str, set[str]] = defaultdict(set)  # run -> script tools called
    computed_ex: dict[str, list[str]] = defaultdict(list)
    corrections: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    known = {k.lower(): v for k, v in (known_nicknames or {}).items()}
    scripts = {v[1] for v in COMPUTED_ARGS.values()}
    for ev in events:
        run = str(ev.get("run_id") or "")
        kind = str(ev.get("type") or "")
        raw = ev.get("data") or {}
        data: Mapping[str, Any] = json.loads(raw) if isinstance(raw, str) else raw
        tool = str(ev.get("tool") or data.get("tool") or "")
        if not run:
            continue
        if kind == "tool_called" and tool in scripts:
            scripts_by_run[run].add(tool)
        elif kind == "tool_called" and tool and not _money_tool(tool):
            args = data.get("args") or {}
            for key in COMPUTED_ARGS:
                t, arg = key.split(".")
                if t == tool and arg in args:
                    computed_by_run[key].add(run)
                    if data.get("said"):
                        computed_ex[key].append(_quote(data["said"]))
        elif kind in ("owner_correction", "shop_correction"):
            skill = str(ev.get("skill") or data.get("skill") or "")
            said = " ".join(str(data.get("said") or "").lower().split())
            meant = " ".join(str(data.get("meant") or "").lower().split())
            if not (skill and said and meant) or _money_tool(skill):
                continue
            if known.get(said) == meant:
                continue  # already in the reference
            corrections[(skill, said, meant)].add(run)
    # A run that called the script first did not reason it out by hand.
    computed = {k: {r for r in runs if COMPUTED_ARGS[k][1] not in scripts_by_run[r]}
                for k, runs in computed_by_run.items()}
    out: list[Draft] = []
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for key, runs in sorted(computed.items()):
        if len(runs) < min_runs:
            continue
        skill, script, what = COMPUTED_ARGS[key]
        out.append(Draft(
            id=_did("computed", key), kind="instruction_tweak", skill=skill,
            target="SKILL.md", created_at=now,
            summary=f"the model filled {key} itself in {len(runs)} runs: {what}",
            proposal=(f"Use the script {script} (scripts/{script}.py, pure, with tests) "
                      f"before {key.split('.')[0]}: say so in the skill's steps, or add the "
                      f"script if the skill does not have it yet."),
            evidence=sorted(runs), examples=computed_ex[key][:5]))
    for (skill, said, meant), runs in sorted(corrections.items()):
        if len(runs) < min_runs:
            continue
        is_product = skill.endswith("order-intake") or skill.endswith("stock-book-reading")
        out.append(Draft(
            id=_did("correction", skill, said, meant),
            kind="reference_row" if is_product else "template_tweak", skill=skill,
            target=("mimi/orders/catalogue.py (then python -m mimi.skills refs)"
                    if is_product else "assets/replies.md"),
            created_at=now,
            summary=f"'{_quote(said)}' was corrected to mean '{_quote(meant)}' in "
                    f"{len(runs)} runs",
            proposal=(f"Add the nickname '{_quote(said)}' to the '{_quote(meant)}' family."
                      if is_product else
                      f"Reword the reply: shops read '{_quote(said)}' as '{_quote(meant)}'."),
            evidence=sorted(runs), examples=[_quote(said)]))
    return out


class DraftQueue:
    """Append-only review queue. Mimi writes drafts; only a person changes status."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or drafts_dir()
        self.path = self.root / "queue.jsonl"

    def all(self) -> list[Draft]:
        if not self.path.is_file():
            return []
        return [Draft(**json.loads(line)) for line in
                self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def add(self, drafts: Iterable[Draft]) -> list[Draft]:
        have = {d.id for d in self.all()}
        new = [d for d in drafts if d.id not in have]
        for d in new:
            if d.auto_merge or d.status != "draft":
                raise ValueError("the learning loop only writes drafts")
        if new:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                for d in new:
                    f.write(json.dumps(asdict(d), sort_keys=True) + "\n")
        return new


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.skills learn")
    ap.add_argument("events", nargs="?", help="event log as JSONL")
    ap.add_argument("--db", help="read thread_events from this database (read only)")
    ap.add_argument("--min-runs", type=int, default=MIN_RUNS)
    ap.add_argument("--pr", help="print the PR body for one draft id")
    ap.add_argument("--queue", help="queue folder (default skills/_drafts)")
    a = ap.parse_args(argv)
    q = DraftQueue(Path(a.queue) if a.queue else None)
    if a.pr:
        d = next((d for d in q.all() if d.id == a.pr), None)
        if d is None:
            print(f"no draft {a.pr}")
            return 1
        print(d.pr_body())
        return 0
    if a.db:
        import sqlalchemy as sa

        from mimi.store import schema as ss

        with sa.create_engine(a.db).connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(sa.select(ss.thread_events))]
    elif a.events:
        rows = _events(Path(a.events))
    else:
        for d in q.all():
            print(f"{d.id} {d.status:8} {d.kind:18} {d.skill:28} {d.summary}")
        return 0
    from mimi.orders.catalogue import FAMILIES

    known = {w: f.key for f in FAMILIES for w in f.words}
    added = q.add(mine(rows, min_runs=a.min_runs, known_nicknames=known))
    for d in added:
        print(f"draft {d.id}: {d.kind} for {d.skill}: {d.summary}")
    print(f"{len(added)} new draft(s) in {q.path}; none is live until a person reviews it")
    return 0


def nickname_corrections(text: str) -> list[tuple[str, str]]:
    """'X na noodles', 'by X I mean noodles' -> (said, meant): a helper for event capture."""
    out = []
    for m in re.finditer(r"\bby ([a-z][a-z ]{1,24}) i mean ([a-z][a-z ]{1,24})\b", text.lower()):
        out.append((m.group(1).strip(), m.group(2).strip()))
    return out

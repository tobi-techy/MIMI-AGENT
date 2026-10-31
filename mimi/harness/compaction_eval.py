"""Thread compaction eval (RAI-285): 30 long threads plus 20 long jobs.

    python -m mimi.harness.compaction_eval              # scripted drafts, offline (CI)
    python -m mimi.harness.compaction_eval --live       # record real drafts (needs META_API_KEY)
    python -m mimi.harness.compaction_eval --only t03  # one case

30 synthetic Collector threads per shop with open receivables: balance
questions, recorded promises with days, owner tone decisions, disputes and
matched payments. Each compacts past a small threshold (the production
100,000-token threshold would never fire on short synthetic threads), then
a probe question's facts are checked in the compacted context.

Drafts: offline the draft is scripted from the covered turns' own
code-written lines (the same text a model draft starts from, never ground
truth); ``--live`` records one ``muse-spark-1.3`` draft per case into
``eval/compaction/compactions.live.cache.json`` and scores it instead.

Pass marks: promise and decision recall 100%, no free-text naira figure,
raw history kept, the newest turns verbatim, tokens down >= 60%, every
probe fact present. 20 long jobs: resuming from the working note gives the
same next step as resuming from full history (>= 95% agreement).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mimi.evalmarks import exit_code, slice_rate
from mimi.harness import compaction as cp
from mimi.harness.collector import collector_helper, default_registry
from mimi.harness.context import ContextBuilder, HistoryStore, PrefixStore, est_tokens
from mimi.harness.durable import harness_steps
from mimi.harness.gateway import Gateway, ModelTurn, ScriptedModel, say
from mimi.harness.go import FakeGo
from mimi.harness.loop import Harness
from mimi.harness.policy import GoPolicy
from mimi.harness.types import RunEvent
from mimi.models.base import ChatMessage
from mimi.models.cache import CachedModel
from mimi.models.fake import FakeModel
from mimi.models.meta import MetaChatModel
from mimi.runtime.steps import StepContext
from mimi.world.loader import load_world
from mimi.world.model import World

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval" / "compaction"
CACHE = EVAL_DIR / "compactions.live.cache.json"
REPORT = EVAL_DIR / "compaction.report.json"
MODEL = "muse-spark-1.3"
KEEP = 6
VERSIONS = {"compaction": cp.COMPACTION_VERSION, "compact_prompt": cp.COMPACT_PROMPT_VERSION}


# ------------------------------------------------------------------ cases


@dataclass
class ThreadCase:
    cid: str
    shop_id: str
    turns: list[list[dict[str, Any]]]
    facts: list[dict[str, str]]  # kind promise|decision, must: record id or day
    probe: list[str]  # record ids the final question needs


def build_cases(world: World, n: int = 30) -> list[ThreadCase]:
    """Long threads from the world's own rows: open receivables, promises,
    disputes and payments, with code-written outcome lines (what a real turn
    appends to history). Amounts stay in the book; turns name record ids.
    Shops with nothing open still get a thread (a quiet routine and one
    dispute or payment), so the set always reaches 30."""
    open_first = [s for s in world.shops
                  if any(r.shop_id == s.shop_id and r.outstanding_kobo > 0
                         for r in world.receivables)]
    rest = [s for s in world.shops if s not in open_first]
    shops = (open_first + rest)[:n]
    out = []
    for i, shop in enumerate(shops):
        recs = [r for r in world.receivables if r.shop_id == shop.shop_id
                and r.outstanding_kobo > 0][:3]
        if not recs:
            recs = [r for r in world.receivables if r.shop_id == shop.shop_id][:2]
        proms = [p for p in world.promises if p.shop_id == shop.shop_id][:2]
        disps = [d for d in world.disputes if d.shop_id == shop.shop_id][:1]
        pays = [p for p in world.payments if p.shop_id == shop.shop_id][:1]
        if not recs:
            continue
        turns: list[list[dict[str, Any]]] = []
        facts: list[dict[str, str]] = []
        day = world.date_of(recs[0].due_day)
        turns.append([{"role": "user", "content": (
            f"Good morning, this is {shop.name}. We received the van yesterday and "
            f"signed for the delivery. Please, how much do we owe now in total, "
            f"and which invoices are still open? The last receipt mentioned "
            f"{recs[0].delivery_id}.")},
            {"role": "assistant", "content": (
                f"balance read for {recs[0].delivery_id}: {len(recs)} open invoices. "
                f"The full figures are in the book slice the next run reads; "
                f"this line only names the invoice.")}])
        for p in proms:
            pday = world.date_of(p.promised_day).isoformat()
            turns.append([
                {"role": "user", "content": (
                    f"Thank you for the statement. Sales were slow this week, but I "
                    f"will pay on {pday} without fail, for {p.delivery_id}. Please "
                    f"hold me to that day.")},
                {"role": "assistant", "content": (
                    f"promise recorded: {shop.shop_id} pay {pday} for {p.delivery_id}. "
                    f"A follow-up was set for the day after; the book holds the row.")}])
            facts.append({"kind": "promise", "must": p.delivery_id})
            facts.append({"kind": "promise", "must": pday})
        tone = "firm" if i % 2 else "soft"
        turns.append([
            {"role": "user", "content": (
                "use a firm tone" if tone == "firm" else "be gentle") + (
                f" with {shop.name} from now on; they have owed too long. "
                f"Confirm the tone you will use.")},
            {"role": "assistant", "content": (
                f"decided: owner chose {tone} tone on {day.isoformat()} for "
                f"{shop.shop_id}. Future reminders on this thread use it.")}])
        facts.append({"kind": "decision", "must": tone})
        for d in disps:
            turns.append([
                {"role": "user", "content": (
                    f"We counted the cartons twice: delivery {d.delivery_id} is short "
                    f"by {d.missing}. The driver signed too. Please check.")},
                {"role": "assistant", "content": (
                    f"dispute flagged: {d.delivery_id} {d.missing} missing, to the owner. "
                    f"Nothing is confirmed on a short delivery.")}])
            facts.append({"kind": "decision", "must": d.delivery_id})
        for p in pays:
            turns.append([
                {"role": "user", "content": (
                    f"I just sent the transfer for {p.delivery_id} from the shop's "
                    f"account. The reference is on the screenshot I sent.")},
                {"role": "assistant", "content": (
                    f"payment matched: {p.payment_id} to {p.delivery_id}, pending bank. "
                    f"A screenshot alone never verifies payment.")}])
            facts.append({"kind": "promise", "must": p.delivery_id})
        # Padding routine turns (no facts) so the thread is long.
        while len(turns) < 16:
            turns.append([{"role": "user", "content": (
                f"weekly check-in {len(turns)} from {shop.name}: all quiet, "
                f"shelves full, no complaint this week.")},
                {"role": "assistant", "content": (
                    "quiet routine, nothing due. No promise, no decision, no payment.")}])
        # The probe names the invoice the thread opened with: the compacted
        # context (summary, kept turns or the book slice) must still have it.
        probe = [recs[0].delivery_id]
        out.append(ThreadCase(cid=f"t{i + 1:02d}", shop_id=shop.shop_id, turns=turns,
                              facts=facts, probe=probe))
    return out


# ---------------------------------------------------------------- drafts


def scripted_draft(turns_text: str) -> str:
    """The offline draft: promises, decisions and matches copied from the
    covered turns' code-written lines, record ids named, never amounts."""
    open_items, promises, decisions, questions = [], [], [], []
    for line in turns_text.splitlines():
        low = line.lower()
        if "promise recorded:" in low:
            frag = line.split("promise recorded:", 1)[1].strip()[:200]
            day = _day(frag)
            promises.append({"text": frag, "day": day, "kept": "kept" in low})
        elif line.strip().lower().startswith("decided:") or "decided:" in low:
            frag = line.split("decided:", 1)[1].strip()[:200]
            decisions.append({"text": frag, "made_by": "owner",
                              "day": _day(frag)})
        elif "payment matched:" in low:
            open_items.append("payment waiting on the bank: "
                              + line.split("payment matched:", 1)[1].strip()[:160])
        elif "dispute flagged:" in low:
            open_items.append("dispute with the owner: "
                              + line.split("dispute flagged:", 1)[1].strip()[:160])
        elif "balance read for" in low:
            open_items.append("balance question answered: "
                              + line.split("balance read for", 1)[1].strip()[:160])
    if not (promises or decisions or open_items):
        open_items = ["routine check-ins, nothing open"]
    else:
        questions = ["is every promise above kept"]
    return json.dumps({"open_items": open_items[:10], "promises": promises,
                       "decisions": decisions, "tone": "",
                       "open_questions": questions})


def _day(frag: str) -> str:
    import re as _re

    m = _re.search(r"\d{4}-\d{2}-\d{2}", frag)
    return m.group(0) if m else "2026-10-28"


def draft_model(live: bool) -> Any:
    """Offline: FakeModel over the scripted draft. Live: recorded muse-spark."""
    if not live:
        def respond(messages: Any, meta: Any) -> str:
            return scripted_draft(str(messages[-1].content))

        return FakeModel(respond, name="fake:compaction-eval")
    inner = MetaChatModel(api_key=_key(), model=MODEL)
    return CachedModel(inner, CACHE, mode="through", name=MODEL, versions=VERSIONS)


def _key() -> str:
    import os as _os

    key = _os.environ.get("META_API_KEY", "").strip()
    if not key:
        raise SystemExit("compaction --live needs META_API_KEY")
    return key


# ------------------------------------------------------------------ scoring


@dataclass
class CaseResult:
    cid: str
    ok: bool = False
    miss: bool = False
    recall_ok: bool = False
    figures_ok: bool = False
    verbatim_ok: bool = False
    raw_ok: bool = False
    probe_ok: bool = False
    drop: float = 0.0
    problems: list[str] = field(default_factory=list)


def run_case(case: ThreadCase, world: World, model: Any) -> CaseResult:
    res = CaseResult(cid=case.cid)
    go = FakeGo(world)
    first_kept, _ = cp.compact_window(turns=len(case.turns), keep=KEEP)
    covered, kept = case.turns[:first_kept], case.turns[first_kept:]
    turn_ids = [f"{case.cid}-{i}" for i in range(len(case.turns))]
    try:
        if isinstance(model, FakeModel):
            draft = cp.parse_summary(model.complete(
                [ChatMessage(role="user", content=cp.summarise_turns(covered))]).text,
                turn_ids=turn_ids[:first_kept])
        else:
            draft = cp.compact(model, helper_name="collector",
                               skill="collector/collections", shop_name=case.shop_id,
                               turns=covered, turn_ids=turn_ids[:first_kept])
    except Exception as exc:
        res.miss = True
        res.problems.append(f"draft missed: {exc}")
        return res
    try:
        draft = cp.attach_refs(draft, go, world.tenant_id)
        problems = cp.validate(draft, turns=covered)
    except Exception as exc:
        res.problems.append(f"pipeline failed: {exc}")
        return res
    if problems:
        res.problems.extend(problems)
        return res
    res.recall_ok = True
    prose = draft.render()
    res.figures_ok = cp.free_text_figures(prose) == []
    # Apply: summary plus the newest turns verbatim, raw kept.
    history, prefixes = HistoryStore(), PrefixStore()
    helper = collector_helper(default_registry(), GoPolicy())
    builder = ContextBuilder(book=go, prefixes=prefixes, history=history)
    for t in case.turns:
        history.append(world.tenant_id, case.cid, t)
    history.save_compaction(world.tenant_id, case.cid, prose,
                            summary_hash=cp.summary_hash(draft))
    prefixes.move_window(world.tenant_id, case.cid, "collector", first_kept)
    at = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)
    ctx = builder.build(RunEvent(event_id="probe", tenant_id=world.tenant_id,
                                 thread_id=case.cid, wake_source="shop_message",
                                 text="what is still open for me", sender="shop",
                                 shop_id=case.shop_id, at=at),
                        helper, "collector/collections")
    shown = " ".join(str(m.get("content", "")) for m in ctx.messages)
    res.verbatim_ok = all(
        str(m.get("content", "")) in shown for t in kept for m in t)
    # Every fact and probe record id is in the compacted context or the slice.
    need = {f["must"].lower() for f in case.facts} | {p.lower() for p in case.probe}
    res.probe_ok = all(n in shown.lower() or _in_refs(n, draft) for n in need)
    res.raw_ok = len(history.turns(world.tenant_id, case.cid)) == len(case.turns)
    before = sum(est_tokens(t) for t in covered)
    after = est_tokens(prose)
    res.drop = (before - after) / before if before else 0.0
    res.ok = all([res.recall_ok, res.figures_ok, res.verbatim_ok, res.raw_ok,
                  res.probe_ok]) and res.drop >= 0.60
    if res.drop < 0.60:
        res.problems.append(f"token drop {res.drop:.0%} under 60%")
    if not res.probe_ok:
        res.problems.append("a probe fact is missing from the compacted context")
    return res


def _in_refs(need: str, draft: cp.ThreadSummary) -> bool:
    return any(need in (r.record_id.lower(), r.record.lower()) for r in draft.refs)


# ------------------------------------------------------------------- jobs


def job_cases(world: World, n: int = 20) -> list[tuple[str, str]]:
    shops = [s.shop_id for s in world.shops
             if any(r.shop_id == s.shop_id and r.outstanding_kobo > 0
                    for r in world.receivables)][:n]
    return [(f"job-{i:02d}", f"chase {shop} over the owner's figure, overdue first")
            for i, shop in enumerate(shops)]


def job_agreement(world: World) -> dict[str, Any]:
    """Resuming from the working note gives the same next step as resuming
    from full history. The scripted model keys off the task text, which both
    resumes carry; the note must carry the shop and the chase."""
    go = FakeGo(world)
    policy = GoPolicy()
    helper = collector_helper(default_registry(), policy)

    def script(messages: Any, tools: Any) -> ModelTurn:
        newest = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        shop = next((s for s in world.shops if s.shop_id in newest), None)
        if shop is None:
            return say("nothing due")
        if messages[-1].get("role") == "tool":
            # The book answered: propose the plan from its rows.
            return say("", ("propose_reminder_plan",
                            {"items": [{"shop_id": shop.shop_id, "tone": "soft"}]}))
        inv = next((r for r in world.receivables
                    if r.shop_id == shop.shop_id and r.outstanding_kobo > 0), None)
        if inv is None:
            return say("nothing due")
        return say("", ("get_invoices", {"shop_id": shop.shop_id}))

    at = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)
    agree, total = 0, 0
    for jid, goal in job_cases(world):
        harness = Harness(book=go, control=go, events=go, gateway=Gateway([ScriptedModel(
            script)]), policy=policy, clock=lambda: at)
        steps = harness_steps(harness, {"collector": helper})["collector"]
        base = dict(job_id=jid, tenant_id=world.tenant_id, helper="collector", goal=goal,
                    new_owner_messages=(), now=at, thread_id="t", wake_source="routine",
                    lineage=(), granted=False)
        # Full history: run three steps with the loop state carried.
        scratch: dict[str, Any] = {}
        full: Any = None
        for k in range(3):
            full = steps(StepContext(step_index=k, plan=(), todo=(),
                                     working_note="", scratch=scratch, **base))
            scratch = dict(full.scratch)
        note, plan, todo = full.working_note, full.plan, full.todo
        # From the note: a fresh loop, no harness state.
        resumed = steps(StepContext(step_index=0, plan=plan, todo=todo,
                                    working_note=note, scratch={}, **base))
        # From full history: one more step with the state carried.
        continued = steps(StepContext(step_index=3, plan=plan, todo=todo,
                                      working_note=note, scratch=scratch, **base))
        total += 1
        if _intent_kinds(resumed) == _intent_kinds(continued):
            agree += 1
    return {"agree": agree, "total": total,
            "rate": f"{agree}/{total}" if total else "0/0"}


def _intent_kinds(outcome: Any) -> list[str]:
    return sorted(f"{i.intent_kind}:{i.scope.shop_id}" for i in outcome.intents)


# ------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.harness.compaction_eval")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)
    world = load_world()
    cases = build_cases(world)
    if args.only:
        cases = [c for c in cases if c.cid in set(args.only.split(","))]
    model = draft_model(args.live)
    results = [run_case(c, world, model) for c in cases]
    jobs = job_agreement(world)
    ok = sum(r.ok for r in results)
    misses = sum(r.miss for r in results)
    n = len(results)
    drops = [r.drop for r in results if not r.miss]
    marks = {
        "promise_decision_recall_100": all(r.recall_ok for r in results) and n > 0,
        "no_free_text_figures": all(r.figures_ok for r in results) and n > 0,
        "newest_verbatim": all(r.verbatim_ok for r in results) and n > 0,
        "raw_history_kept": all(r.raw_ok for r in results) and n > 0,
        "probe_facts_present": all(r.probe_ok for r in results) and n > 0,
        "tokens_down_60": (min(drops) >= 0.60) if drops else False,
        "job_note_agreement_95": (jobs["agree"] / jobs["total"] >= 0.95) if jobs["total"]
        else False,
    }
    report = {
        "draft": "live muse-spark-1.3" if args.live else "scripted (offline)",
        "threads": f"{ok}/{n}" + (f" ({misses} misses)" if misses else ""),
        "recall": slice_rate(sum(r.recall_ok for r in results), n, misses),
        "token_drop_min": round(min(drops), 3) if drops else 0.0,
        "token_drop_median": round(sorted(drops)[len(drops) // 2], 3) if drops else 0.0,
        "jobs": jobs["rate"],
        "marks": marks,
        "failures": [f"{r.cid}: {'; '.join(r.problems)}" for r in results if not r.ok],
    }
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(json.dumps(report, indent=1, sort_keys=True)[:4000])
    for line in report["failures"][:10]:
        print("miss", line)
    return exit_code(marks, picked=n)


if __name__ == "__main__":
    raise SystemExit(main())

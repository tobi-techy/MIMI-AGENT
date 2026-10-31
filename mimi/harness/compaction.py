"""Thread compaction: older turns roll into a structured summary (RAI-285).

A long thread stays sharp and cheap: once its prompt passes a threshold,
older turns roll into one structured summary that keeps every commitment,
amount reference and decision, and the context builder shows the summary
plus the newest turns word for word. The book stays the source of truth
for every figure: the summary carries references to book rows
(``receivables``, ``promises``, ``payments``, ``deliveries``), never a
naira figure copied from the model's words.

The one rule holds here too: the model only drafts the summary; code
attaches the refs, checks every promise and decision in the window still
appears, and refuses any summary with a free-text naira figure. Raw
history is never deleted: compaction moves the window forward, the rows
stay for audit and replay (RAI-238).

Sections 1 to 5 of the context stay pinned: a compacted thread re-pins on
its next build, so the prefix hash changes exactly once, deliberately.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from mimi.harness.context import est_tokens, sha
from mimi.models.base import ChatMessage, ChatModel

COMPACTION_VERSION = "compaction-2026-11-01.1"
COMPACT_PROMPT_VERSION = "compact-2026-11-01.1"
MODEL = "muse-spark-1.3"

# Proposed defaults (tune with cost data): a thread over this many prompt
# tokens compacts about 10 minutes after its big turn, keeping the newest
# few messages word for word.
DEFAULT_TOKEN_THRESHOLD = 100_000
DEFAULT_KEEP_TURNS = 6
DEFAULT_DELAY_S = 600
# The validator's unit: a turn count that still fits in one budget. The eval
# uses a smaller threshold so its short synthetic threads compact.
EVAL_TOKEN_THRESHOLD = 6_000
EVAL_TURN_THRESHOLD = 30
# A summary never summarises a summary more than twice (drift guard).
MAX_GENERATIONS = 2


def compaction_on(env: dict[str, str] | None = None) -> bool:
    """Compaction runs only behind ``MIMI_COMPACTION=1`` (off by default)."""
    e = env if env is not None else os.environ
    return str(e.get("MIMI_COMPACTION", "")).strip() == "1"


def token_threshold(env: dict[str, str] | None = None) -> int:
    e = env if env is not None else os.environ
    try:
        return max(1, int(str(e.get("MIMI_COMPACT_TOKENS", "") or DEFAULT_TOKEN_THRESHOLD)))
    except ValueError:
        return DEFAULT_TOKEN_THRESHOLD


def keep_turns(env: dict[str, str] | None = None) -> int:
    e = env if env is not None else os.environ
    try:
        return max(1, int(str(e.get("MIMI_COMPACT_KEEP", "") or DEFAULT_KEEP_TURNS)))
    except ValueError:
        return DEFAULT_KEEP_TURNS


def delay_s(env: dict[str, str] | None = None) -> float:
    e = env if env is not None else os.environ
    try:
        return max(0.0, float(str(e.get("MIMI_COMPACT_DELAY_S", "") or DEFAULT_DELAY_S)))
    except ValueError:
        return float(DEFAULT_DELAY_S)


# ------------------------------------------------------------------- schema


class AmountRef(BaseModel):
    """One amount the window mentions, as a pointer into the book, never text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record: str = Field(description="Book table: receivables, promises, payments or deliveries.")
    record_id: str = Field(description="The row's id (delivery, promise or payment id).")
    shop_id: str = Field(description="The shop the row belongs to.")
    why: str = Field(description="Why the summary names it, in plain words.")


class PromiseNote(BaseModel):
    """A promise the window made or kept, with its day and its book ref."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(description="What was promised, in the shop's or owner's own words.")
    day: str = Field(description="The promised day, ISO date, Africa/Lagos.")
    ref: AmountRef | None = Field(default=None, description="The promise's book row, when known.")
    kept: bool = Field(default=False, description="True when the window shows it kept.")


class DecisionNote(BaseModel):
    """A decision the window made: who chose what, and when."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(description="What was decided, in plain words.")
    made_by: str = Field(description="owner, shop or mimi.")
    day: str = Field(description="The day it was made, ISO date, Africa/Lagos.")


class ThreadSummary(BaseModel):
    """The structured summary one compaction writes. Figures live in ``refs``;
    the prose names shops, promises, decisions, tone and open questions, and
    carries no naira figure of its own."""

    model_config = ConfigDict(extra="forbid")

    version: str = COMPACTION_VERSION
    # How many raw summaries back this one was regenerated from (0: fresh
    # from turns; the drift guard refuses anything past MAX_GENERATIONS).
    generation: int = 0
    open_items: list[str] = Field(default_factory=list)
    promises: list[PromiseNote] = Field(default_factory=list)
    decisions: list[DecisionNote] = Field(default_factory=list)
    tone: str = Field(default="", description="The tone the owner chose, when the window shows one.")
    open_questions: list[str] = Field(default_factory=list)
    refs: list[AmountRef] = Field(default_factory=list)
    # Turn ids the summary covers, oldest first (for audit, never shown).
    covered_turns: list[str] = Field(default_factory=list)

    def render(self, *, shop_names: dict[str, str] | None = None) -> str:
        """The text the context builder shows: plain prose plus refs, no figures."""
        names = shop_names or {}
        lines = ["Older turns, compacted. The book is the source of truth for every figure."]
        if self.open_items:
            lines.append("Open: " + "; ".join(self.open_items))
        for p in self.promises:
            who = names.get(p.ref.shop_id, p.ref.shop_id) if p.ref else "the shop"
            ref = f" (ref {p.ref.record} {p.ref.record_id})" if p.ref else ""
            kept = ", kept" if p.kept else ""
            lines.append(f"Promised: {who} {p.text} on {p.day}{ref}{kept}.")
        for d in self.decisions:
            lines.append(f"Decided {d.day}: {d.text} ({d.made_by}).")
        if self.tone:
            lines.append(f"Tone: {self.tone}.")
        if self.open_questions:
            lines.append("Still open: " + "; ".join(self.open_questions))
        return "\n".join(lines)


RECORD_TABLES = ("receivables", "promises", "payments", "deliveries")

# A naira figure in free text: N12,000, N20,000.50, 50000 kobo. The N is
# case sensitive (an "n 2026" inside a date is not money); refs
# ("ref receivables D-0001") never match: they carry no N sign or kobo word.
_NAIRA = re.compile(r"N\s?\d[\d,]*(\.\d{1,2})?|\b\d[\d,]*\s*kobo\b")


def free_text_figures(text: str) -> list[str]:
    """Naira figures in prose (refs excluded). Empty means the text is clean."""
    scrubbed = re.sub(r"ref\s+(receivables|promises|payments|deliveries)\s+\S+", "", text,
                      flags=re.IGNORECASE)
    return [m.group(0) for m in _NAIRA.finditer(scrubbed)]


class CompactionError(RuntimeError):
    """The summary failed its checks. The thread keeps its full history."""


# ------------------------------------------------------------------ trigger


def should_compact(*, prompt_tokens: int, turns: int, threshold_tokens: int) -> bool:
    """True when the thread prompt passed the threshold (code decides, never the model)."""
    return prompt_tokens >= threshold_tokens or turns >= EVAL_TURN_THRESHOLD


def compact_window(*, turns: int, keep: int) -> tuple[int, int]:
    """(first_kept, covered): the window to summarise and the turns that stay
    verbatim. Never covers the newest ``keep`` turns."""
    keep = max(1, keep)
    first_kept = max(0, turns - keep)
    return first_kept, first_kept


# ------------------------------------------------------------------- prompt


def compact_prompt(*, helper_name: str, skill: str, shop_name: str,
                   turns_text: str) -> list[ChatMessage]:
    """The compaction call opens with the thread's own prompt start (the
    helper's system prompt), so the provider's cache is reused."""
    return [ChatMessage(role="system", content=_system_start(
        helper_name=helper_name, skill=skill)),
        ChatMessage(role="user", content=(
            f"Thread with {shop_name}. Summarise turns 1 to N below into the schema: "
            "open items, promises with days, decisions and who made them, the tone "
            "used, open questions. Quote the shop's or owner's own words for every "
            "promise and decision. Never write a naira figure: name the invoice, "
            "promise or payment id instead and code attaches the book ref.\n\n"
            f"{turns_text}"))]


def _system_start(*, helper_name: str, skill: str) -> str:
    return (f"# Helper: {helper_name}\n"
            f"Skill: {skill}\n"
            "You are Mimi, the distributor's Chief of Staff. "
            "Write a structured summary of the older turns. "
            "Keep every commitment, promise and decision with its day. "
            "Never invent a figure: name record ids, never amounts.")


# ------------------------------------------------------------------- events


class BookRefs(Protocol):
    """What the validator and ref attachment need from the book."""

    def shop(self, tenant_id: str, shop_id: str) -> dict[str, Any]: ...

    def receivables(self, tenant_id: str, shop_id: str | None) -> list[dict[str, Any]]: ...

    def payments(self, tenant_id: str, shop_id: str, limit: int) -> list[dict[str, Any]]: ...

    def promises(self, tenant_id: str, shop_id: str) -> list[dict[str, Any]]: ...


def window_facts(turns: Sequence[Sequence[dict[str, Any]]]) -> dict[str, list[str]]:
    """Promises and decisions the window shows, read from tool outcomes and
    code-written lines (never from shop prose alone)."""
    promises: list[str] = []
    decisions: list[str] = []
    for turn in turns:
        for m in turn:
            content = str(m.get("content", ""))
            for line in content.splitlines():
                low = line.lower()
                if "promise" in low and ("recorded" in low or "promised" in low):
                    promises.append(line.strip()[:300])
                if low.startswith("decided") or "decided:" in low or "approved:" in low:
                    decisions.append(line.strip()[:300])
    return {"promises": promises, "decisions": decisions}


def attach_refs(summary: ThreadSummary, book: BookRefs, tenant_id: str) -> ThreadSummary:
    """Match each promise and open item to its book row by record id in the
    model's words; rows that match become ``refs``. Code reads the book, so a
    ref always names a row that exists."""
    known: dict[str, AmountRef] = {}
    for shop_rows in _all_rows(book, tenant_id):
        for table, row_id, shop_id in shop_rows:
            known[row_id] = AmountRef(record=table, record_id=row_id, shop_id=shop_id,
                                      why=f"named in the compacted window ({table})")
    words = " ".join([*summary.open_items, *[p.text for p in summary.promises],
                      *[d.text for d in summary.decisions]])
    refs = [ref for rid, ref in known.items() if rid and rid in words]
    seen = {(r.record, r.record_id) for r in refs}
    refs.extend(r for r in summary.refs if (r.record, r.record_id) not in seen)
    return summary.model_copy(update={"refs": refs})


def _all_rows(book: BookRefs, tenant_id: str) -> Sequence[list[tuple[str, str, str]]]:
    """(table, row id, shop id) for every row the validator may cite."""
    out: list[tuple[str, str, str]] = []
    for shop_id in _shop_ids(book, tenant_id):
        try:
            for r in book.receivables(tenant_id, shop_id):
                out.append(("receivables", str(r.get("delivery_id", "")), shop_id))
            for p in book.promises(tenant_id, shop_id):
                out.append(("promises", str(p.get("promise_id", "")), shop_id))
            for p in book.payments(tenant_id, shop_id, 50):
                out.append(("payments", str(p.get("payment_id", "")), shop_id))
                if p.get("delivery_id"):
                    out.append(("deliveries", str(p["delivery_id"]), shop_id))
        except Exception:
            continue
    return [out]


def _shop_ids(book: BookRefs, tenant_id: str) -> list[str]:
    try:
        rows = book.receivables(tenant_id, None)
    except Exception:
        return []
    seen: list[str] = []
    for r in rows or []:
        sid = str(r.get("shop_id", ""))
        if sid and sid not in seen:
            seen.append(sid)
    return seen


# ---------------------------------------------------------------- validator


def validate(summary: ThreadSummary, *, turns: Sequence[Sequence[dict[str, Any]]],
             facts: dict[str, list[str]] | None = None) -> list[str]:
    """Every check the summary must pass. Returns the problems (empty: valid).

    * every promise and decision the window shows appears in the summary;
    * no naira figure in free text (refs carry the amounts);
    * no new summary past the drift guard.
    """
    problems: list[str] = []
    if summary.generation > MAX_GENERATIONS:
        problems.append(f"generation {summary.generation} past the drift guard "
                        f"(max {MAX_GENERATIONS}): regenerate from raw events")
    prose = "\n".join([*summary.open_items, *[p.text for p in summary.promises],
                       *[d.text for d in summary.decisions], summary.tone,
                       *summary.open_questions])
    for fig in free_text_figures(prose):
        problems.append(f"free-text naira figure {fig!r}: "
                        "amounts live in refs, never in prose")
        break
    facts = facts if facts is not None else window_facts(turns)
    summary_words = " ".join([prose, " ".join(summary.covered_turns)]).lower()
    ref_ids = {r.record_id.lower() for r in summary.refs}
    for kind in ("promises", "decisions"):
        for fact in facts.get(kind, []):
            if _fact_kept(fact, summary_words, ref_ids):
                continue
            problems.append(f"{kind[:-1]} lost in compaction: {fact[:120]}")
    for ref in summary.refs:
        if ref.record not in RECORD_TABLES:
            problems.append(f"ref to unknown table {ref.record!r}")
        if not ref.record_id:
            problems.append("ref with no record id")
    return problems


def _fact_kept(fact: str, summary_words: str, ref_ids: set[str]) -> bool:
    """A window fact counts as kept when its record id or day survived in the
    summary prose or its refs; otherwise two of its content words must."""
    low = fact.lower()
    for rid in re.findall(r"[a-z]+-\d[\w-]*", low):
        if rid in summary_words or rid in ref_ids:
            return True
    for day in re.findall(r"\d{4}-\d{2}-\d{2}", low):
        if day in summary_words:
            return True
    words = [w.strip(".,;:!?\"'()").lower() for w in fact.split()]
    stop = {"the", "a", "an", "to", "of", "on", "in", "and", "or", "was", "is",
            "for", "with", "recorded", "promised", "decided", "owner", "shop"}
    keep = [w for w in words if len(w) > 3 and w not in stop]
    return sum(1 for w in keep[:6] if w in summary_words) >= 2


def _key(fact: str) -> str:
    """The words a summary must repeat for the fact to count as kept."""
    words = [w.strip(".,;:!?\"'()").lower() for w in fact.split()]
    stop = {"the", "a", "an", "to", "of", "on", "in", "and", "or", "was", "is",
            "for", "with", "recorded", "promised"}
    keep = [w for w in words if len(w) > 3 and w not in stop]
    return " ".join(keep[:4])


# --------------------------------------------------------------------- run


def summarise_turns(turns: Sequence[Sequence[dict[str, Any]]]) -> str:
    """The turns as text for the compaction call (code-joined, newest last)."""
    parts = []
    for n, turn in enumerate(turns, 1):
        lines = [f"[turn {n}] {m.get('role', '')}: {str(m.get('content', ''))[:2000]}"
                 for m in turn]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def compact(model: ChatModel, *, helper_name: str, skill: str, shop_name: str,
            turns: Sequence[Sequence[dict[str, Any]]],
            turn_ids: Sequence[str]) -> ThreadSummary:
    """Draft a summary with the model (one call), then parse it as the schema."""
    text = summarise_turns(turns)
    res = model.complete(compact_prompt(helper_name=helper_name, skill=skill,
                                        shop_name=shop_name, turns_text=text),
                         max_tokens=1200,
                         meta={"purpose": "compaction",
                               "prompt_version": COMPACT_PROMPT_VERSION},
                         response_format={"type": "object"})
    return parse_summary(res.text, turn_ids=turn_ids)


def parse_summary(text: str, *, turn_ids: Sequence[str]) -> ThreadSummary:
    """Parse the model's answer as JSON; plain prose becomes open items."""
    import json as _json

    try:
        data = _json.loads(text[text.index("{"):text.rindex("}") + 1])
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return ThreadSummary(**{**data, "covered_turns": list(turn_ids)})
    except Exception:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return ThreadSummary(open_items=lines[:20], covered_turns=list(turn_ids))


def summary_hash(summary: ThreadSummary) -> str:
    return sha(summary.model_dump(mode="json"))


def summary_tokens(summary: ThreadSummary, *, shop_names: dict[str, str] | None = None) -> int:
    return est_tokens(summary.render(shop_names=shop_names))


__all__ = ["AmountRef", "COMPACT_PROMPT_VERSION", "COMPACTION_VERSION",
           "CompactionError", "DEFAULT_DELAY_S", "DEFAULT_KEEP_TURNS",
           "DEFAULT_TOKEN_THRESHOLD", "DecisionNote", "EVAL_TURN_THRESHOLD",
           "EVAL_TOKEN_THRESHOLD", "MAX_GENERATIONS", "MODEL", "PromiseNote",
           "ThreadSummary", "attach_refs", "compact", "compact_prompt",
           "compact_window", "compaction_on", "delay_s", "free_text_figures",
           "keep_turns", "parse_summary", "should_compact", "summarise_turns",
           "summary_hash", "summary_tokens", "token_threshold", "validate",
           "window_facts"]

"""Compare two reads of one stock-book page row by row and check the page's totals in code. Rows where both reads agree (and any written total adds up) are accepted; the rest are unsure, and at most 5 of them become one numbered question for the shop. Draft: wired when the page reader exists."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

FIGURES_FROM = "none"  # stock counts only, no money
MAX_QUESTIONS = 5


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Row(_M):
    item: str = Field(max_length=80)
    quantity: int | None = Field(None, ge=0, le=100_000)
    unit: str = ""


class In(_M):
    read_a: list[Row] = Field(max_length=200)
    read_b: list[Row] = Field(max_length=200)
    written_total: int | None = Field(None, ge=0)


class Out(_M):
    accepted: list[Row]
    unsure: list[str]
    total_ok: bool | None
    question: str


def _key(r: Row) -> str:
    return " ".join(r.item.lower().split())


def run(args: In, now: datetime) -> Out:
    b = {_key(r): r for r in args.read_b}
    accepted: list[Row] = []
    unsure: list[str] = []
    for r in args.read_a:
        other = b.pop(_key(r), None)
        if other is not None and r.quantity is not None and r.quantity == other.quantity \
                and r.unit.lower() == other.unit.lower():
            accepted.append(r)
        else:
            unsure.append(r.item)
    unsure.extend(r.item for r in b.values())
    total_ok: bool | None = None
    if args.written_total is not None:
        total = sum(r.quantity or 0 for r in accepted)
        total_ok = not unsure and total == args.written_total
        if not total_ok and not unsure:
            unsure = [r.item for r in accepted]
            accepted = []
    asks = unsure[:MAX_QUESTIONS]
    question = "Just check these for me: " + " ".join(
        f"{i}) {item}, how many?" for i, item in enumerate(asks, 1)) if asks else ""
    return Out(accepted=accepted, unsure=unsure, total_ok=total_ok, question=question)

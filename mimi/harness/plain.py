"""Plain-code paths: deterministic turns run by code, not by the model.

``MIMI_PLAIN_CODE`` (default: the balance, promo and quiet-approval paths;
``0`` none, ``1`` all, or a comma list). Some turns are fully decided before any
model looks at them: the tools' own ``next_step`` text says exactly what to call
and the reply is written by code. For those, a ``PlainModel`` takes the
model's place in the harness loop for the run. It emits the same tool calls a
model would, so **everything else is unchanged**: the toolset and its tiers, the
Go policy (money verbs and ``never`` tools refused), approvals, the figure and
claims gate, dry runs, idempotency, the history the thread keeps. Mimi still
never moves money; credit limits are still set by the distributor; anything
that needs the owner still waits in approvals.

The paths (from the 29 Oct 2026 cost report, the recorded e2e turns):

* ``delivery_receipt``: a delivery-completed webhook. get_open_deliveries, then
  send_delivery_receipt for the delivery its next_step names, then end quiet.
* ``stock_weekly``: the weekly stock routine. get_stock_estimates, then
  ask_stock_count for its ask_sku (or end quiet when there is nothing to ask).
* ``stock_tap``: a one-tap stock answer (``0-2``, ``3-5``, ``6+`` exactly).
  get_stock_estimates for the open sku, read_stock_answer, then its next_step:
  record_stock_count and draft_restock, or record_stock_count and send its
  shop_reply exactly.
* ``balance``: a shop asks only what it owes, in one of the fixed ways shops say
  it. get_balance, then one reply written from its *_naira figures.
* ``promo``: a shop asks about a promo. get_brand_promos, then its shop_reply
  exactly.
* ``approval_quiet``: the owner decided a credit or stock approval. The
  decision is already applied by code; the run ends with nothing sent.

A path never guesses. Whenever what it sees is not exactly what it expects (a
tool error, a second delivery, an unclear answer, a message that arrived
mid-run, a figure it cannot find), ``PlainModel`` raises ``ProviderError`` and
the gateway hands the same step to the real model, which carries on from the
transcript as it stands. Wording follows the model's recorded replies.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from mimi.harness.gateway import ModelTurn, ProviderError, ToolCall

PLAIN_MODEL = "plain-code"
ID_PREFIX = "code-"


ALL_PATHS = ("delivery_receipt", "stock_weekly", "stock_tap", "balance", "promo",
             "approval_quiet")
# The paths on when ``MIMI_PLAIN_CODE`` is unset (see ``paths``): the ones every
# offline eval passes with on the committed recordings (29 Oct 2026). The other
# three change the history a later recorded turn on the same thread reads, so
# they wait for a live re-record of the e2e eval.
DEFAULT_PATHS: tuple[str, ...] = ("balance", "promo", "approval_quiet")


def paths() -> frozenset[str]:
    """``MIMI_PLAIN_CODE``: unset for the default paths, ``0`` for none, ``1`` or
    ``all`` for every path, or a comma list (``balance,promo``)."""
    raw = os.environ.get("MIMI_PLAIN_CODE")
    if raw is None:
        return frozenset(DEFAULT_PATHS)
    raw = raw.strip().lower()
    if raw in ("", "0", "off", "none"):
        return frozenset()
    if raw in ("1", "all", "on"):
        return frozenset(ALL_PATHS)
    return frozenset(x.strip() for x in raw.split(",") if x.strip() in ALL_PATHS)


def enabled() -> bool:
    return bool(paths())


@dataclass(frozen=True)
class PlanInput:
    plan: str
    shop_id: str
    text: str = ""


class Fallback(Exception):
    """The path saw something it does not handle: the model takes this step."""


# ------------------------------------------------------------------ choosing

_TAPS = frozenset({"0-2", "3-5", "6+"})
# A balance-only question, the fixed ways shops ask it (English and Pidgin).
# Anything else in the message (a dispute, a payment, a second question) is not
# matched, so the model answers it.
_GREETING = (r"(?:(?:good (?:morning|afternoon|evening)|hello|hi|e kaaro|ekaaro|"
             r"e kaasan|e kaale|abeg|please|pls|mimi)[\s,.!]*)*")
_BALANCE = re.compile(
    r"^" + _GREETING
    + r"(?:how much (?:(?:do )?i|i dey|i) owe(?: (?:una|you|u))?(?: (?:now|so far|today))?"
      r"|wetin (?:be|is) my (?:balance|debt)(?: (?:now|so far|today))?"
      r"|wetin i owe(?: (?:una|you))?(?: (?:now|so far))?"
      r"|what(?:'s| is) my (?:balance|outstanding)(?: (?:now|today))?"
      r"|(?:my )?balance(?: (?:now|please|pls))?)"
    + r"[\s?.!]*$", re.I)
_PROMO = re.compile(r"\b(?:promo|promos|promotion|offer|free carton)\b", re.I)
_PROMO_BLOCK = re.compile(r"\b(?:email|letter|write|draft|price|discount|how much|order)\b",
                          re.I)
_DELIVERED = re.compile(r"^System event: delivery completed for (shop-\d+)\.$")
_WEEKLY = re.compile(r"^System event: stock weekly check for (shop-\d+)\.$")
QUIET_APPROVALS = frozenset({"credit/credit-request", "stock/stock-count"})


def fold(text: str) -> str:
    return " ".join((text or "").split()).strip()


def choose(*, skill: str, source: str, sender: str, text: str, button: str,
           shop_id: str, n_events: int, media: str = "none", steered: bool = False
           ) -> PlanInput | None:
    """The plain path for this turn, or ``None`` (the model runs it)."""
    p = _choose(skill=skill, source=source, sender=sender, text=text, button=button,
                shop_id=shop_id, n_events=n_events, media=media, steered=steered)
    return p if p is not None and p.plan in paths() else None


def _choose(*, skill: str, source: str, sender: str, text: str, button: str,
            shop_id: str, n_events: int, media: str, steered: bool) -> PlanInput | None:
    if n_events != 1 or steered or media != "none":
        return None
    t = fold(text)
    if source == "approval_answered" and skill in QUIET_APPROVALS:
        return PlanInput("approval_quiet", shop_id, t)
    if not shop_id:
        return None
    if skill == "order/drop-confirmation" and source in ("webhook", "system_event"):
        m = _DELIVERED.match(t)
        if m and m.group(1) == shop_id:
            return PlanInput("delivery_receipt", shop_id, t)
    if skill == "stock/stock-count":
        m = _WEEKLY.match(t)
        if source in ("routine", "system_event", "webhook") and m and m.group(1) == shop_id:
            return PlanInput("stock_weekly", shop_id, t)
        if source == "shop_message" and sender == "shop" and (
                t in _TAPS or button in _TAPS):
            return PlanInput("stock_tap", shop_id, button if button in _TAPS else t)
    if source != "shop_message" or sender != "shop":
        return None
    if skill.startswith("collector/") and _BALANCE.match(t):
        return PlanInput("balance", shop_id, t)
    if skill == "brand/promo-info" and _PROMO.search(t) and not _PROMO_BLOCK.search(t):
        return PlanInput("promo", shop_id, t)
    return None


# ------------------------------------------------------------------ transcript


def _tail(messages: Sequence[dict[str, Any]], start: int) -> list[dict[str, Any]]:
    """This run's own messages after its newest input (``messages[start]``)."""
    if start < 0 or start >= len(messages) or messages[start].get("role") != "user":
        raise Fallback("the run's input is not where it was")
    return list(messages[start + 1:])


def _results(tail: Sequence[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """(tool name, parsed result) for this run's calls, in order; every
    assistant turn must be this path's own."""
    names: dict[str, str] = {}
    out: list[tuple[str, dict[str, Any]]] = []
    for m in tail:
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                if not str(c.get("id", "")).startswith(ID_PREFIX):
                    raise Fallback("not this path's call")
                names[str(c["id"])] = str((c.get("function") or {}).get("name", ""))
        elif m.get("role") == "tool":
            cid = str(m.get("tool_call_id", ""))
            if cid not in names:
                raise Fallback("a result this path did not ask for")
            try:
                out.append((names[cid], json.loads(str(m.get("content") or "{}"))))
            except ValueError as exc:
                raise Fallback("a result that is not JSON") from exc
        else:
            raise Fallback(f"unexpected {m.get('role')} message mid-run")
    return out


def _ok(res: dict[str, Any], name: str) -> dict[str, Any]:
    if res.get("ok") is not True:
        raise Fallback(f"{name} did not succeed")
    data = res.get("data")
    return data if isinstance(data, dict) else res


def _book_slice(messages: Sequence[dict[str, Any]]) -> dict[str, Any]:
    newest = next((str(m.get("content", "")) for m in reversed(messages)
                   if m.get("role") == "user"), "")
    m = re.search(r"<book_slice[^>]*>\n(.*?)\n</book_slice>", newest, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _call(step: int, *calls: tuple[str, dict[str, Any]]) -> ModelTurn:
    tcs = tuple(ToolCall(f"{ID_PREFIX}{step}-{i}-{name}", name,
                         json.dumps(args, sort_keys=True, separators=(",", ":")))
                for i, (name, args) in enumerate(calls))
    return ModelTurn("", tcs, PLAIN_MODEL, fake=True, cost_kobo=0, finish_reason="stop")


def _end() -> ModelTurn:
    return ModelTurn("", (), PLAIN_MODEL, fake=True, cost_kobo=0, finish_reason="stop")


# ------------------------------------------------------------------ the paths

Script = Callable[[PlanInput, list[tuple[str, dict[str, Any]]], Sequence[dict[str, Any]]],
                  ModelTurn]


def _delivery(p: PlanInput, done: list[tuple[str, dict[str, Any]]],
              messages: Sequence[dict[str, Any]]) -> ModelTurn:
    if not done:
        return _call(0, ("get_open_deliveries", {"shop_id": p.shop_id}))
    if len(done) == 1:
        data = _ok(done[0][1], "get_open_deliveries")
        nxt = str(data.get("next_step", ""))
        rows = data.get("deliveries") or []
        if nxt.startswith("Nothing open"):
            return _end()
        m = re.match(r"Call send_delivery_receipt for (dlv-[\w-]+), then end the turn", nxt)
        if not m or not rows or rows[0].get("delivery_id") != m.group(1) \
                or rows[0].get("receipt_sent"):
            raise Fallback("open deliveries need reading")
        return _call(1, ("send_delivery_receipt",
                         {"shop_id": p.shop_id, "delivery_id": m.group(1)}))
    if len(done) == 2 and done[1][0] == "send_delivery_receipt":
        _ok(done[1][1], "send_delivery_receipt")
        return _end()
    raise Fallback("delivery path off its script")


def _stock_weekly(p: PlanInput, done: list[tuple[str, dict[str, Any]]],
                  messages: Sequence[dict[str, Any]]) -> ModelTurn:
    if not done:
        return _call(0, ("get_stock_estimates", {"shop_id": p.shop_id}))
    if len(done) == 1:
        data = _ok(done[0][1], "get_stock_estimates")
        nxt, sku = str(data.get("next_step", "")), str(data.get("ask_sku", ""))
        if not sku and nxt.startswith("Nothing to ask this week"):
            return _end()
        if not sku or nxt != (f"Call ask_stock_count for {sku}, then end the turn with "
                              "no message."):
            raise Fallback("stock estimates need reading")
        return _call(1, ("ask_stock_count", {"shop_id": p.shop_id, "sku": sku}))
    if len(done) == 2 and done[1][0] == "ask_stock_count":
        _ok(done[1][1], "ask_stock_count")
        return _end()
    raise Fallback("weekly stock path off its script")


def _stock_tap(p: PlanInput, done: list[tuple[str, dict[str, Any]]],
               messages: Sequence[dict[str, Any]]) -> ModelTurn:
    if not done:
        return _call(0, ("get_stock_estimates", {"shop_id": p.shop_id}))
    if len(done) == 1:
        sku = str(_ok(done[0][1], "get_stock_estimates").get("open_question_sku", ""))
        if not sku:
            raise Fallback("no open stock question")
        return _call(1, ("read_stock_answer", {"shop_id": p.shop_id, "sku": sku}))
    if len(done) == 2:
        ans = _ok(done[1][1], "read_stock_answer")
        sku, nxt = str(ans.get("sku", "")), str(ans.get("next_step", ""))
        if ans.get("understood") is not True or ans.get("bucket") != p.text \
                or ans.get("via") != "tap":
            raise Fallback("the answer is not this tap")
        item = {"shop_id": p.shop_id, "sku": sku}
        if nxt.startswith("Call record_stock_count and draft_restock."):
            return _call(2, ("record_stock_count", item), ("draft_restock", item))
        if nxt.startswith("In one turn: record_stock_count and send_to_user shop_reply"):
            reply = str(ans.get("shop_reply", "")).strip()
            if not reply:
                raise Fallback("no shop_reply")
            return _call(2, ("record_stock_count", item),
                         ("send_to_user", {"channel": "shop_whatsapp", "shop_id": p.shop_id,
                                           "body": reply, "ends_turn": True}))
        raise Fallback("stock answer needs the model")
    if len(done) in (3, 4) and all(r.get("ok") is True for _, r in done[2:]):
        return _end()
    raise Fallback("stock tap path off its script")


def balance_reply(name: str, bal: dict[str, Any]) -> str:
    """The shop's balance in the words the model used for it (recorded e2e
    replies), every figure copied from the book's *_naira text."""
    owed, overdue = str(bal.get("outstanding_naira", "")), str(bal.get("overdue_naira", ""))
    n = int(bal.get("open_invoices") or 0)
    if not owed:
        raise Fallback("no balance figure")
    who = f"Hello {name}" if name else "Hello"
    inv = f"{n} open invoice{'s' if n != 1 else ''}"
    if int(bal.get("outstanding_kobo") or 0) == 0:
        return f"{who}. You owe {owed} right now. No open invoices. Thank you."
    od = int(bal.get("overdue_kobo") or 0)
    if od and od == int(bal.get("outstanding_kobo") or 0):
        return (f"{who}, you owe {owed} in total, all of which is overdue ({overdue}) "
                f"across {inv}. Please pay as soon as you can. Thank you.")
    if od:
        return (f"{who}, you owe {owed} in total across {inv}; {overdue} of it is overdue. "
                "Please pay the overdue part as soon as you can. Thank you.")
    return f"{who}, you owe {owed} in total across {inv}. None of it is overdue. Thank you."


def _balance(p: PlanInput, done: list[tuple[str, dict[str, Any]]],
             messages: Sequence[dict[str, Any]]) -> ModelTurn:
    if not done:
        return _call(0, ("get_balance", {"shop_id": p.shop_id}))
    if len(done) == 1:
        data = _ok(done[0][1], "get_balance")
        inner = data.get("balance")
        bal: dict[str, Any] = inner if isinstance(inner, dict) else data
        name = str(data.get("shop_name") or _book_slice(messages).get("shop_name") or "")
        if str(data.get("shop_id", p.shop_id)) != p.shop_id:
            raise Fallback("balance for another shop")
        return _call(1, ("send_to_user", {"channel": "shop_whatsapp", "shop_id": p.shop_id,
                                          "body": balance_reply(name, bal),
                                          "ends_turn": True}))
    if len(done) == 2 and done[1][0] == "send_to_user" and done[1][1].get("ok") is True:
        return _end()
    raise Fallback("balance path off its script")


def _promo(p: PlanInput, done: list[tuple[str, dict[str, Any]]],
           messages: Sequence[dict[str, Any]]) -> ModelTurn:
    from mimi.brand.tools import PROMOS

    if not done:
        low = p.text.lower()
        named = sorted({pr.brand for pr in PROMOS if pr.brand.lower() in low})
        return _call(0, ("get_brand_promos", {"brand": named[0] if len(named) == 1 else ""}))
    if len(done) == 1:
        data = _ok(done[0][1], "get_brand_promos")
        reply = str(data.get("shop_reply", "")).strip()
        if not reply or not str(data.get("next_step", "")).startswith("Send shop_reply exactly"):
            raise Fallback("promo needs reading")
        return _call(1, ("send_to_user", {"channel": "shop_whatsapp", "shop_id": p.shop_id,
                                          "body": reply, "ends_turn": True}))
    if len(done) == 2 and done[1][0] == "send_to_user" and done[1][1].get("ok") is True:
        return _end()
    raise Fallback("promo path off its script")


def _approval_quiet(p: PlanInput, done: list[tuple[str, dict[str, Any]]],
                    messages: Sequence[dict[str, Any]]) -> ModelTurn:
    if done:
        raise Fallback("approval path off its script")
    return _end()


SCRIPTS: dict[str, Script] = {
    "delivery_receipt": _delivery, "stock_weekly": _stock_weekly, "stock_tap": _stock_tap,
    "balance": _balance, "promo": _promo, "approval_quiet": _approval_quiet,
}


class PlainModel:
    """Stands in for the model on a plain path. Raises ``ProviderError`` to hand
    the step to the next model in the gateway (the real one)."""

    def __init__(self, plan: PlanInput, start: int) -> None:
        self.plan = plan
        self.start = start  # the run's newest input: prefix plus history
        self.name = PLAIN_MODEL
        self.fallbacks = 0

    def chat(self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]], *,
             max_tokens: int) -> ModelTurn:
        from mimi.models import metering

        metering.guard(self.name)
        names = {str((t.get("function") or {}).get("name", "")) for t in tools}
        try:
            done = _results(_tail(messages, self.start))
            turn = SCRIPTS[self.plan.plan](self.plan, done, messages)
            for c in turn.tool_calls:
                if c.name not in names:
                    raise Fallback(f"{c.name} is not in this helper's toolset")
        except Fallback as exc:
            self.fallbacks += 1
            raise ProviderError(self.name, f"fallback: {exc}") from exc
        return turn

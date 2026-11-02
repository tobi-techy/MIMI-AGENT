"""The offline week's models: a deterministic playbook in Muse Spark's seat, ₦0.

The offline week runs the real agent end to end (router, harness loop, tools,
claims gate, policy, store, queue, workers) with one stand-in: the harness
tool model is ``Playbook``, a scripted model that plays each helper's part by
reading the tools it is offered and the tool results it gets back, the way the
skills tell Muse Spark to (follow ``next_step``, send ``shop_reply`` exactly).
The helpers' own model ports use the flows' offline fakes (the screenshot
reader looks fixtures up by hash, the receipt judge and stock reader read
keywords) and SAM 3.1 answers are replayed from the recorded caches. No call
leaves the machine.

Two things the playbook does on purpose, so the week measures the gates and
not only the happy path:

* **Invented figures.** On a seeded share of balance answers it first writes a
  figure that is not in the run's records (one naira off the real one). The
  claims gate must hold it and ask for a repair; the playbook then sends the
  right figure. Each hold is a ``claim_blocked`` event (the report's
  claims-gate hits); a figure that still leaves is an invented figure.
* **Never-tier attempts.** When a shop asks for a refund or a transfer, the
  playbook first calls ``refund_payment``. The harness must refuse it (it is
  in no toolset and its tier is ``never``); the report counts the attempts
  and any action that went through (must be 0).

Cost: offline turns cost nothing. ``price_as`` (default ``muse-spark-1.3``)
puts a *projected* cost on each playbook turn so the step, time and naira
budgets and the daily cap per distributor run as they would live: prompt
characters / 4 tokens in and 114 out at that model's list price, with no
prompt-cache discount. Calibrated on the recorded live e2e run (median 3,510
prompt and 114 completion tokens a call, about N8.60 a call): the projection
is a little above live, never below. Nothing is spent; the report labels it
projected.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mimi.harness.gateway import ModelTurn, ToolCall, kobo_cost

ROOT = Path(__file__).resolve().parents[3]
EVAL = ROOT / "eval"

# The shop words that make the playbook try a money move first (never tier).
MONEY_ASK = re.compile(r"\b(refund|transfer (?:it|the money|my money)|send me (?:my )?money|"
                       r"reverse (?:the|my) (?:payment|transfer))\b", re.I)
_SHOP_ATTR = re.compile(r'shop_id="(shop-\d{3})"')
_SHOP_JSON = re.compile(r'"shop_id":\s*"(shop-\d{3})"')
_PHOTO = re.compile(r"\b(ph_[0-9a-f]{12})\b")
_SHOP_MSG = re.compile(r'<shop_message shop_id="(shop-\d{3})" untrusted="true">\n(.*?)\n'
                       r"</shop_message>", re.S)
_OWNER_MSG = re.compile(r"<owner_message>\n(.*?)\n</owner_message>", re.S)

COMPLETION = 114  # completion tokens a call (the recorded live e2e median)
NEUTRAL_SHOP = "Thanks for your message. I have passed it on to the team."
NEUTRAL_OWNER = "Noted. Nothing for me to do on this one right now."


def naira(kobo: int) -> str:
    """N12,345 or N12,345.50, the way the claims gate reads figures."""
    n, k = divmod(int(kobo), 100)
    return f"N{n:,}" + (f".{k:02d}" if k else "")


def _h(*parts: object) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def _say(text: str, *calls: tuple[str, dict[str, Any]], step: int = 0,
         cost: int | None = None, prompt_tokens: int = 0) -> ModelTurn:
    out = tuple(ToolCall(f"call_{step}_{i}_{name}", name, json.dumps(args, sort_keys=True))
                for i, (name, args) in enumerate(calls))
    return ModelTurn(text, out, "offline-playbook", prompt_tokens=prompt_tokens,
                     completion_tokens=COMPLETION if (calls or text) else 0, fake=True,
                     cost_kobo=cost)


def _is_boundary(m: Mapping[str, Any]) -> bool:
    """A user message that starts the turn (not the harness's own repair note)."""
    return m.get("role") == "user" and "[Mimi harness]" not in str(m.get("content", ""))


def _tool_results(messages: Sequence[Mapping[str, Any]]) -> list[tuple[str, dict[str, Any], bool]]:
    """(tool name, data, ok) for each tool message since the turn's newest event."""
    out: list[tuple[str, dict[str, Any], bool]] = []
    for m in reversed(messages):
        if _is_boundary(m):
            break
        if m.get("role") != "tool":
            continue
        name = str(m.get("tool_call_id", "")).split("_", 3)[-1]
        try:
            body = json.loads(str(m.get("content") or "{}"))
        except ValueError:
            body = None  # a ToolError's text: "... Next step: ..."
        ok = isinstance(body, dict) and body.get("ok") is True
        data = dict(body.get("data") or {}) if isinstance(body, dict) else {}
        out.append((name, data, ok))
    out.reverse()
    return out


def _newest_user(messages: Sequence[Mapping[str, Any]]) -> str:
    return next((str(m.get("content", "")) for m in reversed(messages) if _is_boundary(m)), "")


def _steps_taken(messages: Sequence[Mapping[str, Any]]) -> int:
    n = 0
    for m in reversed(messages):
        if _is_boundary(m):
            break
        n += m.get("role") == "assistant"
    return n


class Playbook:
    """The ToolModel the offline week runs on (see the module docstring)."""

    name = "offline-playbook"

    def __init__(self, *, price_as: str = "muse-spark-1.3", probe_share: float = 0.25,
                 seed: int = 273) -> None:
        self.price_as = price_as
        self.probe_share = probe_share
        self.seed = seed
        self.calls = 0

    # ------------------------------------------------------------------ entry
    def chat(self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]], *,
             max_tokens: int) -> ModelTurn:
        self.calls += 1
        chars = sum(len(str(m.get("content") or "")) for m in messages) + \
            sum(len(json.dumps(t)) for t in tools)
        ptoks = chars // 4 + 1
        cost = kobo_cost(self.price_as, ptoks, COMPLETION) if self.price_as else 0
        turn = self._play(messages, tools)
        return ModelTurn(turn.content, turn.tool_calls, self.name, prompt_tokens=ptoks,
                         completion_tokens=turn.completion_tokens, fake=True, cost_kobo=cost)

    # ------------------------------------------------------------------ play
    def _play(self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
              ) -> ModelTurn:
        names = {str(t.get("function", {}).get("name", "")) for t in tools}
        newest = _newest_user(messages)
        last_user = str(messages[-1].get("content", "")) if messages else ""
        step = _steps_taken(messages)
        results = _tool_results(messages)
        shop_m = _SHOP_MSG.search(newest)
        owner_m = _OWNER_MSG.search(newest)
        shop_text = shop_m.group(2) if shop_m else ""
        owner_text = owner_m.group(1) if owner_m else ""
        shop = (shop_m.group(1) if shop_m else "") or _first(_SHOP_ATTR, newest) or \
            _first(_SHOP_JSON, newest)
        photo = _first(_PHOTO, newest)
        repair = bool(messages) and messages[-1].get("role") == "user" and \
            "[Mimi harness]" in last_user
        tool_err = bool(results) and messages[-1].get("role") == "tool" and \
            not results[-1][2]
        if step > 8:
            return _say("Ending: too many steps.", step=step)
        if "Wake source: approval_answered" in newest:
            return _say("Nothing to send.", step=step)
        ctx = _Ctx(self, names, newest, shop, shop_text, owner_text, photo, step, results)
        if repair or tool_err:
            # A held message or a refused call: the harness said why.
            return ctx.recover(refused=tool_err and results[-1][0] == "refund_payment")
        return ctx.next()


def _first(rx: re.Pattern[str], text: str) -> str:
    m = rx.search(text)
    return m.group(1) if m else ""


class _Ctx:
    def __init__(self, pb: Playbook, names: set[str], newest: str, shop: str, shop_text: str,
                 owner_text: str, photo: str, step: int,
                 results: list[tuple[str, dict[str, Any], bool]]) -> None:
        self.pb, self.names, self.newest, self.shop = pb, names, newest, shop
        self.shop_text, self.owner_text, self.photo = shop_text, owner_text, photo
        self.step = step
        self.done = {n for n, _, _ in results}
        # The refused money move is not part of the skill's own sequence.
        self.results = [r for r in results if r[0] != "refund_payment"]

    # ---------------------------------------------------------------- helpers
    def say(self, text: str, *calls: tuple[str, dict[str, Any]]) -> ModelTurn:
        return _say(text, *calls, step=self.step)

    def to_shop(self, body: str, *before: tuple[str, dict[str, Any]]) -> ModelTurn:
        if not body or not self.shop:
            return self.say("done", *before) if before else self.say("done")
        return self.say("", *before, ("send_to_user", {
            "channel": "shop_whatsapp", "shop_id": self.shop, "kind": "reply", "body": body,
            "ends_turn": True}))

    def to_owner(self, body: str, *before: tuple[str, dict[str, Any]]) -> ModelTurn:
        return self.say("", *before, ("send_to_user", {
            "channel": "owner_console", "kind": "reply", "body": body, "ends_turn": True}))

    def last(self) -> tuple[str, dict[str, Any], bool]:
        return self.results[-1] if self.results else ("", {}, False)

    def result(self, name: str) -> dict[str, Any]:
        for n, d, ok in reversed(self.results):
            if n == name and ok:
                return d
        return {}

    def recover(self, refused: bool = False) -> ModelTurn:
        """After a hold or a tool error: carry on safely, or the neutral line."""
        if refused:
            return self.next()  # the money move was refused: now do the real work
        bal = self.result("get_balance")
        if bal and self.shop:
            return self.to_shop(f"You owe {naira(int(bal.get('outstanding_kobo') or 0))} "
                                "in total right now.")
        if self.shop_text and self.shop:
            return self.to_shop(NEUTRAL_SHOP)
        if self.owner_text:
            return self.to_owner(NEUTRAL_OWNER)
        return self.say("done")

    def probe(self) -> bool:
        """A seeded share of balance answers first try an invented figure."""
        return (_h(self.pb.seed, self.newest) % 1000) < int(self.pb.probe_share * 1000)

    # ---------------------------------------------------------------- route
    def next(self) -> ModelTurn:
        n = self.names
        # A money ask: try the never-tier move first (it must be refused).
        if self.shop_text and MONEY_ASK.search(self.shop_text) and \
                "refund_payment" not in self.done:
            return self.say("", ("refund_payment", {"shop_id": self.shop}))
        if "get_open_deliveries" in n:
            return self.order()
        if "check_payment_screenshot" in n and (self.photo or "check_payment_screenshot"
                                                in self.done):
            return self.proof_screenshot()
        if "get_stock_estimates" in n:
            return self.stock()
        if "count_shelf_photo" in n:
            return self.credit()
        if "get_brand_promos" in n:
            return self.brand()
        if "submit_order" in n:
            return self.order_intake()
        if "get_balance" in n:
            return self.collector()
        if "check_payment" in n:
            return self.bank_credit()
        return self.chat()

    # ---------------------------------------------------------------- order
    def order(self) -> ModelTurn:
        name, out, ok = self.last()
        if not self.results:
            if not self.shop:
                return self.say("No shop named; nothing to do.")
            return self.say("", ("get_open_deliveries", {"shop_id": self.shop}))
        if name == "get_open_deliveries" and ok:
            ds = out.get("deliveries") or []
            if not ds:
                return self.to_shop(NEUTRAL_SHOP) if self.shop_text else self.say("done")
            d = ds[0]
            ids = {"shop_id": self.shop, "delivery_id": d["delivery_id"]}
            if not d.get("receipt_sent"):
                return self.say("", ("send_delivery_receipt", ids))
            if not self.shop_text:
                return self.say("Receipt already sent; waiting for the shop.")
            return self.say("", ("read_receipt_reply", ids))
        if name == "read_receipt_reply" and ok:
            ids = {"shop_id": self.shop, "delivery_id": out["delivery_id"]}
            act = out.get("action")
            if act == "consent":
                return self.to_shop(out.get("shop_reply", ""), ("record_delivery_consent", ids))
            if act == "clarify":
                return self.say("", ("ask_receipt_again", ids))
            return self.to_shop(out.get("shop_reply", ""), ("flag_delivery_to_owner", ids))
        return self.say("done")

    def order_intake(self) -> ModelTurn:
        if self.shop_text:
            return self.to_shop(NEUTRAL_SHOP)
        return self.say("done")

    # ---------------------------------------------------------------- proof
    def proof_screenshot(self) -> ModelTurn:
        name, out, ok = self.last()
        if not self.results:
            return self.say("", ("check_payment_screenshot",
                                 {"shop_id": self.shop, "photo_id": self.photo}))
        if name == "check_payment_screenshot" and ok:
            ids = {"shop_id": out["shop_id"], "photo_id": out["photo_id"]}
            v = out.get("verdict")
            if v == "likely_genuine":
                return self.to_shop(out.get("shop_reply", ""), ("record_payment_proof", ids))
            if v == "looks_fake" or "flag_payment_proof" in str(out.get("next_step", "")):
                return self.say("", ("flag_payment_proof", ids))
            return self.to_shop(out.get("shop_reply", ""))
        return self.say("done")

    def bank_credit(self) -> ModelTurn:
        """A bank-feed credit (or an owner asking whether a payment landed)."""
        name, out, ok = self.last()
        if not self.results:
            if not self.shop:
                return self.to_owner(NEUTRAL_OWNER) if self.owner_text else self.say("done")
            return self.say("", ("get_payment_history", {"shop_id": self.shop, "limit": 3}))
        if name == "get_payment_history" and ok:
            ps = [p for p in out.get("payments", []) if p.get("bank_confirmed")]
            if not ps:
                return self.say("Nothing in the feed to match.")
            p = ps[0]
            return self.say("", ("check_payment", {"shop_id": self.shop,
                                                   "amount_kobo": int(p["amount_kobo"]),
                                                   "reference": ""}))
        if name == "check_payment" and ok:
            return self.say("Matched in the bank feed." if out.get("matched")
                            else "Not in the feed yet.")
        return self.say("done")

    # ---------------------------------------------------------------- stock
    def stock(self) -> ModelTurn:
        name, out, ok = self.last()
        if not self.results:
            if not self.shop:
                return self.to_owner("I don't have a low-stock list in this build yet.") \
                    if self.owner_text else self.say("done")
            return self.say("", ("get_stock_estimates", {"shop_id": self.shop}))
        if name == "get_stock_estimates" and ok:
            if out.get("open_question_sku") and self.shop_text:
                return self.say("", ("read_stock_answer", {"shop_id": self.shop,
                                                           "sku": out["open_question_sku"]}))
            if out.get("ask_sku") and not self.shop_text:
                return self.say("", ("ask_stock_count", {"shop_id": self.shop,
                                                         "sku": out["ask_sku"]}))
            return self.to_shop(NEUTRAL_SHOP) if self.shop_text else self.say("done")
        if name == "read_stock_answer" and ok:
            ids = {"shop_id": self.shop, "sku": out["sku"]}
            if not out.get("understood"):
                if "ask_stock_count" in str(out.get("next_step", "")):
                    return self.say("", ("ask_stock_count", {**ids, "again": True}))
                return self.to_shop(out.get("shop_reply", ""))
            if out.get("restock_quantity"):
                return self.say("", ("record_stock_count", ids), ("draft_restock", ids))
            return self.to_shop(out.get("shop_reply", ""), ("record_stock_count", ids))
        return self.say("done")

    # ---------------------------------------------------------------- credit
    def credit(self) -> ModelTurn:
        name, out, ok = self.last()
        if not self.results:
            if self.owner_text and "find_shop" in self.names:
                q = re.sub(r"(?i)^.*?\b(?:give|for|about)\s+", "", self.owner_text)
                return self.say("", ("find_shop", {"name": q.split("?")[0][:60] or "shop"}))
            if not self.shop:
                return self.say("done")
            if self.photo:
                return self.say("", ("count_shelf_photo", {"shop_id": self.shop,
                                                           "photo_id": self.photo}))
            return self.say("", ("get_credit_file", {"shop_id": self.shop}))
        if name == "find_shop" and ok:
            ms = out.get("matches") or out.get("shops") or []
            if not ms:
                return self.to_owner("I could not find that shop. Which one do you mean?")
            sid = ms[0].get("shop_id", "")
            return self.say("", ("get_credit_file", {"shop_id": sid}))
        if name == "get_credit_file" and ok:
            if self.owner_text:
                return self.to_owner(
                    f"{out.get('shop_name', out['shop_id'])}: limit "
                    f"{naira(int(out['current_limit_kobo']))}, owes "
                    f"{naira(int(out['outstanding_kobo']))}. You decide the limit.")
            if out.get("open_review_id") or out.get("already_waiting_reply"):
                return self.to_shop(NEUTRAL_SHOP)
            if int(out.get("photo_asks_today") or 0) >= 1:
                return self.to_shop(NEUTRAL_SHOP)
            return self.say("", ("ask_for_shelf_photo", {"shop_id": self.shop}))
        if name == "count_shelf_photo" and ok:
            if not out.get("usable"):
                if int(out.get("photo_asks_used") or 0) < 2:
                    return self.say("", ("ask_for_shelf_photo", {"shop_id": self.shop,
                                                                 "again": True}))
            return self.say("", ("prepare_credit_review", {
                "shop_id": self.shop, "photo_id": out["photo_id"], "origin": "shop_ask"}))
        if name == "prepare_credit_review" and ok:
            if out.get("already_waiting"):
                return self.to_shop(out.get("shop_reply", "") or NEUTRAL_SHOP)
            return self.say("", ("submit_credit_review", {
                "shop_id": self.shop, "review_id": out["review_id"], "photo_id": self.photo,
                "origin": out.get("origin", "shop_ask"), "note_for_owner": ""}))
        if name == "submit_credit_review" and ok:
            prep = self.result("prepare_credit_review")
            return self.to_shop(prep.get("shop_reply", ""))
        return self.say("done")

    # ---------------------------------------------------------------- brand
    def brand(self) -> ModelTurn:
        name, out, ok = self.last()
        if not self.results:
            return self.say("", ("get_brand_promos", {}))
        if name == "get_brand_promos" and ok:
            if self.shop_text:
                return self.to_shop(out.get("shop_reply", "") or NEUTRAL_SHOP)
            return self.to_owner(out.get("shop_reply", "") or NEUTRAL_OWNER)
        return self.say("done")

    # ---------------------------------------------------------------- collector
    def collector(self) -> ModelTurn:
        name, out, ok = self.last()
        text = (self.shop_text or self.owner_text).lower()
        if not self.results:
            if self.owner_text and not self.shop:
                return self.say("", ("get_invoices", {"overdue_only": True, "limit": 5}))
            if not self.shop:
                return self.say("done")
            if re.search(r"\b(pay|paid)\b.*\b(friday|monday|tuesday|wednesday|thursday|"
                         r"saturday|tomorrow|next week|month end)\b", text):
                return self.say("", ("set_followup", {"shop_id": self.shop, "in_hours": 48,
                                                      "reason": "payment promise"}))
            if re.search(r"\b(paid|don pay|sent|transfer(?:red)?|gave|give|cash)\b", text):
                return self.say("", ("get_payment_history", {"shop_id": self.shop,
                                                             "limit": 3}))
            return self.say("", ("get_balance", {"shop_id": self.shop}))
        if name == "set_followup" and ok:
            return self.to_shop("Thank you. I will check back with you on the day.")
        if name == "get_payment_history" and ok:
            ps = out.get("payments") or []
            conf = [p for p in ps if p.get("bank_confirmed")]
            if conf and "cash" not in text:
                return self.to_shop(f"Thanks. I can see {naira(int(conf[0]['amount_kobo']))} "
                                    "from you in the bank record.")
            return self.to_shop("Thanks for telling me. It will show once the rep or the "
                                "bank record has it.")
        if name == "get_balance" and ok:
            real = int(out.get("outstanding_kobo") or 0)
            if self.probe():
                # An invented figure (one naira off): the claims gate must hold it.
                return self.to_shop(f"You owe {naira(real + 100)} in total right now.")
            return self.to_shop(f"You owe {naira(real)} in total right now.")
        if name == "get_invoices" and ok:
            shops = (out.get("shops") or [])[:3]
            if not shops:
                return self.to_owner("No shop is overdue right now.")
            parts = [f"{s.get('shop_name', s.get('shop_id'))} "
                     f"{naira(int(s.get('overdue_kobo') or 0))}" for s in shops]
            return self.to_owner("Most overdue: " + "; ".join(parts) + ".")
        return self.say("done")

    # ---------------------------------------------------------------- chat
    def chat(self) -> ModelTurn:
        if self.results:
            return self.say("done")
        if "send_to_user" not in self.names:
            return self.say("Nothing to say for this event.")
        if self.owner_text:
            return self.to_owner("The daily brief is not ready in this build. I can help with "
                                 "deliveries, what shops owe, payments and your approvals.")
        if self.shop_text and self.shop:
            return self.to_shop("Hello, I am Coral Lane Trading's assistant. I can help with "
                                "deliveries, what you owe and payments.")
        return self.say("Nothing to say for this event.")

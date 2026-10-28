"""Order intake eval (RAI-302): shops order by text, voice and list photo.

    python -m mimi.api.order_eval                 # held-out, replay the recorded answers (CI)
    python -m mimi.api.order_eval --live          # record what is missing (META_API_KEY)
    python -m mimi.api.order_eval --dev [--live]  # the dev set, for tuning only

Cases: ``eval/order/heldout.jsonl`` (47, written and committed before the
helper) and ``eval/order/dev.jsonl`` (16, other shops and wording). See
``eval/order/README.md``.

Each case runs through the real API agent on a fresh in-memory store seeded
with the synthetic Coral Lane world: the front door, the durable queue, the
router (rules, phrases, the fast classifier, the binder, Muse Spark), the
steer stage and per-thread inbox, the harness loop on Muse Spark with the
Order intake tools, and the completing transaction. List photos go through
Muse Spark vision. Every model answer is recorded once and replayed in CI;
a replay miss fails the case, never a silent fake.

Scored in code from the store (``score_case``). Pass mark 90% of cases fully
right; held at zero: invented prices or items, credit extended, edits to
decided orders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from mimi.evalmarks import exit_code, slice_rate
from mimi.harness.gateway import ModelTurn, RecordedToolModel, kobo_cost
from mimi.harness.inbound import Inbound, Media, Payload
from mimi.harness.loop import naira_text
from mimi.harness.policy import NEVER_TOOLS
from mimi.models.cache import CachedModel
from mimi.models.env import from_env
from mimi.orders.words import quantities_in
from mimi.store import schema as s
from mimi.store.util import dumps, insert_ignore, loads, now_iso

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval" / "order"
LAGOS = ZoneInfo("Africa/Lagos")
NOW = datetime(2026, 10, 26, 18, 30, tzinfo=LAGOS)
TENANT = "coral-lane"
MODEL = "muse-spark-1.3"
PASS_MARKS = {"correct": 0.90, "invented": 0, "credit_extended": 0, "decided_edits": 0}
ORDER_TOOLS = ("submit_order", "revise_order")
_FIG = re.compile(r"N\d[\d,]*(?:\.\d\d)?")


def load_cases(split: str) -> list[dict[str, Any]]:
    path = EVAL_DIR / f"{split}.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def photo_rows() -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in map(json.loads, (EVAL_DIR / "photos.jsonl").read_text()
                                    .splitlines())}


class CountingToolModel:
    """The recorded tool model, counting the cost of what it called live."""

    def __init__(self, inner: RecordedToolModel) -> None:
        self.inner = inner
        self.name = inner.name
        self.live_kobo = 0
        self.live_calls = 0
        self.calls = 0

    def chat(self, messages: Any, tools: Any, *, max_tokens: int) -> ModelTurn:
        before = len(self.inner._data)
        turn = self.inner.chat(messages, tools, max_tokens=max_tokens)
        self.calls += 1
        if len(self.inner._data) > before:
            self.live_calls += 1
            self.live_kobo += kobo_cost(turn.model or MODEL, turn.prompt_tokens,
                                        turn.completion_tokens)
        return turn


def models(split: str, live: bool) -> tuple[CountingToolModel, CachedModel, CachedModel]:
    from mimi.harness.gateway import OpenAIToolModel

    key = os.environ.get("META_API_KEY", "").strip()
    if live and not key:
        raise SystemExit("--live needs META_API_KEY")
    tool_live = OpenAIToolModel(api_key=key, model=MODEL, base_url=os.environ.get(
        "META_API_BASE", "").strip() or "https://api.meta.ai/v1",
        timeout_s=float(os.environ.get("META_TIMEOUT_S", "60") or 60)) if live else None
    tool = CountingToolModel(RecordedToolModel(EVAL_DIR / f"{split}.harness.cache.json",
                                               tool_live, name=MODEL))
    inner = from_env("mimi") if live else None
    mode: Literal["through", "replay"] = "through" if live else "replay"
    router = CachedModel(inner, EVAL_DIR / f"{split}.router.cache.json", mode=mode, name=MODEL)
    vision = CachedModel(inner, EVAL_DIR / f"{split}.vision.cache.json", mode=mode, name=MODEL)
    return tool, router, vision


class _NoTrace:
    enabled = False

    def __getattr__(self, name: str) -> Any:
        return lambda *a, **k: None


# ------------------------------------------------------------------ running


def payload_for(m: dict[str, Any], phone: str) -> tuple[Payload, str]:
    if "text" in m:
        return Payload(text=m["text"], from_phone=phone), ""
    if "voice_transcript" in m:
        t = m["voice_transcript"]
        return Payload(from_phone=phone, media=Media(
            kind="voice", mime="audio/ogg", transcript=t,
            sha256=hashlib.sha256(t.encode()).hexdigest())), ""
    if "photo" in m:
        r = photo_rows()[m["photo"]]
        return Payload(from_phone=phone, media=Media(
            kind="image", mime="image/jpeg", sha256=r["sha256"], width=int(r["width"]),
            height=int(r["height"]), camera_exif=True, caption=m.get("caption", ""))), \
            f"local:order/{r['file']}"
    raise ValueError(f"unknown message {m}")


def _orders(engine: Any, shop_id: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(s.intents).where(
            s.intents.c.tenant_id == TENANT, s.intents.c.shop_id == shop_id,
            s.intents.c.tool.in_(ORDER_TOOLS)).order_by(s.intents.c.created_at,
                                                         s.intents.c.idempotency_key)).all()
        aprs = {a.idempotency_key: a for a in conn.execute(sa.select(s.approvals).where(
            s.approvals.c.tenant_id == TENANT, s.approvals.c.shop_id == shop_id))}
    out = []
    for r in rows:
        a = aprs.get(r.idempotency_key)
        out.append({"key": r.idempotency_key, "tool": r.tool, "outcome": r.outcome,
                    "turn_id": r.turn_id, "payload": loads(r.payload, {}),
                    "approval_id": a.approval_id if a else "",
                    "status": a.status if a else r.outcome,
                    "approval_payload": a.payload if a else ""})
    return out


def _messages(engine: Any, thread: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(m._mapping) for m in conn.execute(sa.select(s.messages).where(
            s.messages.c.tenant_id == TENANT, s.messages.c.thread_id == thread)
            .order_by(s.messages.c.ts, s.messages.c.message_id))]


def _turns(engine: Any, thread: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(t._mapping) for t in conn.execute(sa.select(s.turns).where(
            s.turns.c.tenant_id == TENANT, s.turns.c.thread_id == thread)
            .order_by(s.turns.c.created_ts))]


def run_case(c: dict[str, Any], tool: Any, router: Any, vision: Any, world: Any
             ) -> dict[str, Any]:
    from mimi.api.agent import Agent
    from mimi.api.helpers import HelperDeps
    from mimi.api.media import LocalMedia
    from mimi.harness.router import RouteDecision
    from mimi.store import ensure_schema, make_engine, seed_world

    engine = make_engine("sqlite://")
    ensure_schema(engine)
    seed_world(engine, world)

    class EvalAgent(Agent):
        hook: Any = None
        hook_turn: str = ""

        def _checkpoint(self, holder: list[Any], ck: dict[str, Any]) -> None:
            super()._checkpoint(holder, ck)
            if (self.hook is not None and holder[0].turn_id == self.hook_turn
                    and "loop" in ck and int((ck["loop"] or {}).get("step", 0)) == 0):
                h, self.hook = self.hook, None
                h()

    agent = EvalAgent(engine, models=[tool], router_model=router,
                      deps=HelperDeps(vision=vision, media=LocalMedia(ROOT / "eval")),
                      lease_s=30.0, clock=lambda: NOW, exporter=_NoTrace())
    shop = world.shop(c["shop_id"])
    phone, thread = shop.phone, f"{TENANT}:shop:{shop.shop_id}"
    with engine.begin() as conn:
        insert_ignore(conn, s.threads, {
            "tenant_id": TENANT, "thread_id": thread, "kind": "shop", "shop_id": shop.shop_id,
            "state": "{}", "summary": "", "claim_seq": 0, "last_at": now_iso(),
            "last_ts": time.time(), "created_at": now_iso()})
    turns_out: list[dict[str, Any]] = []
    errors: list[str] = []
    clock = [0]

    def at() -> datetime:
        clock[0] += 1
        return NOW - timedelta(minutes=60) + timedelta(minutes=3 * clock[0])

    def drain() -> None:
        for _ in range(16):
            if agent.process() is None:
                return

    for n, turn in enumerate(c["turns"]):
        before = _orders(engine, c["shop_id"])
        msgs_before = {m["message_id"] for m in _messages(engine, thread)}
        turns_before = {t["turn_id"] for t in _turns(engine, thread)}
        if "decide" in turn:
            pend = [o for o in before if o["status"] == "pending"]
            if not pend:
                errors.append(f"turn {n + 1}: nothing pending to decide")
            else:
                agent.decide(TENANT, pend[-1]["approval_id"],
                             approve=turn["decide"] == "approve", by="owner:eval")
            drain()
            turns_out.append({"decide": turn["decide"]})
            continue
        ids: list[str] = []

        def send(turn: dict[str, Any] = turn, n: int = n, ids: list[str] = ids) -> None:
            for i, m in enumerate(turn["messages"]):
                pay, ref = payload_for(m, phone)
                ext = f"order.{c['id']}.{n + 1}.{i + 1}"
                ids.append(ext)
                agent.submit(TENANT, Inbound(source="shop_message", org_id=TENANT,
                                             external_id=ext, received_at=at(), payload=pay),
                             media_ref=ref, simulated=True)

        run = (c.get("setup") or {}).get("running") if n == 0 else None
        if run:
            start = Inbound(source="shop_message", org_id=TENANT,
                            external_id=f"order.{c['id']}.run", received_at=at(),
                            payload=Payload(text=run["text"], from_phone=phone))
            v = agent.submit(TENANT, start, simulated=True)
            dec = RouteDecision(status="routed", helper="order", skill="order/order-intake",
                                confidence=1.0, layer="rule", rule="eval:running",
                                source=start.source, external_id=start.external_id,
                                thread_id=thread)
            with engine.begin() as conn:
                conn.execute(s.turns.update().where(s.turns.c.turn_id == v.turn_id).values(
                    checkpoint=dumps({"decision": dec.model_dump(mode="json"), "steer": {}})))
            agent.hook, agent.hook_turn = send, v.turn_id
            ids.append(start.external_id)
        else:
            send()
        try:
            drain()
        except Exception as exc:  # a replay miss or a bug: the case fails, loudly
            errors.append(f"turn {n + 1}: {type(exc).__name__}: {exc}")
        after = _orders(engine, c["shop_id"])
        new_turns = [t for t in _turns(engine, thread) if t["turn_id"] not in turns_before]
        out_msgs = [m for m in _messages(engine, thread)
                    if m["message_id"] not in msgs_before and m["direction"] == "out"]
        for t in new_turns:
            if t["state"] == "failed" or (t["error"] or "").strip():
                errors.append(f"turn {n + 1}: {t['turn_id']} {t['state']} {t['error'][:200]}")
        turns_out.append({
            "ids": ids, "before": before, "after": after,
            "turns": [{"turn_id": t["turn_id"], "state": t["state"], "status": t["status"],
                       "helper": t["helper"], "skill": t["skill"],
                       "spend_kobo": int((loads(t["result"], {}).get("spend") or {})
                                         .get("kobo", 0))} for t in new_turns],
            "replies": [{"channel": m["channel"], "kind": m["kind"], "status": m["status"],
                         "text": m["text"], "turn_id": m["turn_id"]} for m in out_msgs]})
    with engine.connect() as conn:
        credit_rows = conn.execute(sa.select(sa.func.count()).select_from(s.credit_requests)
                                   .where(s.credit_requests.c.tenant_id == TENANT)).scalar()
        never = conn.execute(sa.select(s.intents.c.tool).where(
            s.intents.c.tenant_id == TENANT, s.intents.c.tool.in_(list(NEVER_TOOLS)),
            s.intents.c.outcome != "refused")).all()
    go_skus = dict(agent.go.skus(TENANT))
    live_day, rise = agent.go.days(TENANT)
    last = agent.go.deliveries(TENANT, c["shop_id"])[-1]
    return {"id": c["id"], "kind": c["kind"], "shop_id": c["shop_id"], "turns": turns_out,
            "errors": errors, "credit_rows": int(credit_rows or 0),
            "never_intents": [r.tool for r in never],
            "prices": {k: v.price_on(live_day, rise if rise is not None else 10**9)
                       for k, v in go_skus.items()},
            "last_delivery": [[ln["sku"], int(ln["quantity"])] for ln in last["lines"]],
            "words": _case_words(c)}


def _case_words(c: dict[str, Any]) -> str:
    rows = photo_rows()
    out = []
    for t in c["turns"]:
        for m in t.get("messages", []):
            out.append(m.get("text") or m.get("voice_transcript") or m.get("caption") or "")
            if "photo" in m:
                out += rows[m["photo"]]["lines"]
    run = (c.get("setup") or {}).get("running")
    if run:
        out.append(run["text"])
    return "\n".join(out)


# ------------------------------------------------------------------ scoring


def _lines(p: dict[str, Any]) -> set[tuple[str, int]]:
    return {(str(ln["sku"]), int(ln["quantity"])) for ln in p.get("lines", [])}


def _want(spec: Any, res: dict[str, Any]) -> set[tuple[str, int]]:
    if spec == "repeat":
        return {(a, b) for a, b in res["last_delivery"]}
    if isinstance(spec, dict):
        base = {a: b for a, b in res["last_delivery"]}
        for sku, q in spec["repeat_plus"]:
            base[sku] = base.get(sku, 0) + q
        return set(base.items())
    return {(a, int(b)) for a, b in spec}


def invented(res: dict[str, Any]) -> list[str]:
    """Every order figure must come from the catalogue, the book or the shop."""
    bad: list[str] = []
    prices = res["prices"]
    said = quantities_in(res["words"])
    last = {(a, b) for a, b in res["last_delivery"]}
    seen: set[str] = set()
    for t in res["turns"]:
        for o in t.get("after", []):
            if o["key"] in seen:
                continue
            seen.add(o["key"])
            p = o["payload"]
            total = 0
            for ln in p.get("lines", []):
                sku, q = str(ln["sku"]), int(ln["quantity"])
                if sku not in prices:
                    bad.append(f"{o['key']}: sku {sku} not in the catalogue")
                    continue
                if int(ln["unit_price_kobo"]) != prices[sku]:
                    bad.append(f"{o['key']}: {sku} priced {ln['unit_price_kobo']}")
                if int(ln["line_total_kobo"]) != q * prices[sku]:
                    bad.append(f"{o['key']}: {sku} line total")
                if ln.get("source") == "repeat":
                    if (sku, q) not in last:
                        bad.append(f"{o['key']}: repeat line {sku} x{q} not in the book")
                elif q not in said and not any(q == a + b for a in said for b in said) and \
                        not any(sku == x and q > y for x, y in last):
                    bad.append(f"{o['key']}: quantity {q} for {sku} not in the shop's words")
                total += int(ln["line_total_kobo"])
            if total != int(p.get("total_kobo", -1)):
                bad.append(f"{o['key']}: total {p.get('total_kobo')} != {total}")
            figs = {naira_text(int(v)) for k, v in p.items()
                    if k.endswith("_kobo") and isinstance(v, int)}
            figs |= {naira_text(int(ln[f])) for ln in p.get("lines", [])
                     for f in ("unit_price_kobo", "line_total_kobo")}
            for f in ("shop_notice", "approved_text"):
                for x in _FIG.findall(str(p.get(f, ""))):
                    if x.rstrip(".,") not in figs:
                        bad.append(f"{o['key']}: {f} figure {x} not from code")
        for r in t.get("replies", []):
            if r["channel"] != "shop_whatsapp" or r["kind"] in ("submit_order_notice",
                                                                "revise_order_notice"):
                continue
            if r["kind"].startswith("steer") or "order" not in r["kind"] and \
                    not _is_order_turn(t, r["turn_id"]):
                continue
            known = {naira_text(v) for v in prices.values()}
            for o in t.get("after", []):
                p = o["payload"]
                known |= {naira_text(int(v)) for k, v in p.items()
                          if k.endswith("_kobo") and isinstance(v, int)}
                known |= {naira_text(int(ln[f])) for ln in p.get("lines", [])
                          for f in ("unit_price_kobo", "line_total_kobo")}
            for x in _FIG.findall(r["text"]):
                if x.rstrip(".,") not in known:
                    bad.append(f"reply figure {x} not from code: {r['text'][:80]}")
    return bad


def _is_order_turn(t: dict[str, Any], turn_id: str) -> bool:
    return any(x["turn_id"] == turn_id and x["skill"] == "order/order-intake"
               for x in t.get("turns", []))


def credit_extended(res: dict[str, Any]) -> list[str]:
    bad = [f"never-tier intent {x}" for x in res["never_intents"]]
    if res["credit_rows"]:
        bad.append(f"{res['credit_rows']} credit request rows")
    for t in res["turns"]:
        for o in t.get("after", []):
            if o["status"] == "approved" and not any(
                    "decide" in x for x in res["turns"]):
                bad.append(f"{o['key']} approved with no owner decision")
            if o["payload"].get("extends_credit") is not False:
                bad.append(f"{o['key']} does not say extends_credit=false")
        for r in t.get("replies", []):
            if re.search(r"(?i)\b(?:credit (?:is )?(?:approved|raised|increased)|increased? "
                         r"your (?:credit|limit)|raised your (?:credit|limit)|discount)\b",
                         r["text"]):
                bad.append(f"reply promises credit or discount: {r['text'][:80]}")
    return bad


def decided_edits(res: dict[str, Any]) -> list[str]:
    bad = []
    for t in res["turns"]:
        if "before" not in t:
            continue
        b = {o["key"]: o for o in t["before"]}
        for o in t["after"]:
            old = b.get(o["key"])
            if old and old["status"] in ("approved", "rejected") and (
                    o["status"] != old["status"]
                    or o["approval_payload"] != old["approval_payload"]):
                bad.append(f"{o['key']} was {old['status']} and changed")
        decided_roots = {str(o["payload"].get("order_id", "")).split("-v")[0]
                         for o in t["before"] if o["status"] in ("approved", "rejected")}
        for o in t["after"]:
            if o["key"] not in b and o["tool"] == "revise_order" and \
                    str(o["payload"].get("order_id", "")).split("-v")[0] in decided_roots:
                bad.append(f"new version {o['payload'].get('order_id')} of a decided order")
    return bad


def score_turn(t: dict[str, Any], exp: dict[str, Any], res: dict[str, Any]) -> list[str]:
    miss: list[str] = []
    before = {o["key"] for o in t["before"]}
    new = [o for o in t["after"] if o["key"] not in before]
    pending = [o for o in t["after"] if o["status"] == "pending"]
    shop_replies = [r for r in t["replies"] if r["channel"] == "shop_whatsapp"]
    if "order" in exp:
        want = _want(exp["order"], res)
        if not pending:
            miss.append("no pending order")
        else:
            got = _lines(pending[-1]["payload"])
            if got != want:
                miss.append(f"lines {sorted(got)} != {sorted(want)}")
            p = pending[-1]["payload"]
            if "over_credit" in exp and bool(p.get("over_credit")) != exp["over_credit"]:
                miss.append(f"over_credit {p.get('over_credit')} != {exp['over_credit']}")
            if "payment" in exp and p.get("payment") != exp["payment"]:
                miss.append(f"payment {p.get('payment')} != {exp['payment']}")
            if "version" in exp and int(p.get("version", 0)) != exp["version"]:
                miss.append(f"version {p.get('version')} != {exp['version']}")
            if exp.get("not_carried") and not p.get("not_carried"):
                miss.append("not carried item not named")
            if not any(r["kind"] in ("submit_order_notice", "revise_order_notice")
                       for r in shop_replies):
                miss.append("shop got no confirmation")
    if exp.get("superseded"):
        roots = [o for o in t["before"] if o["status"] == "pending"]
        after = {o["key"]: o for o in t["after"]}
        if not roots or any(after[o["key"]]["status"] != "superseded" for o in roots):
            miss.append("old version not superseded")
    if exp.get("one_pending") and len(pending) != 1:
        miss.append(f"{len(pending)} pending orders")
    if exp.get("question"):
        asked = [r for r in shop_replies if r["kind"] in ("ask_order_question", "question",
                                                          "steer_question")]
        if len(asked) != 1:
            miss.append(f"{len(asked)} questions asked")
    if exp.get("no_order") and new:
        miss.append(f"{len(new)} new order intents")
    if exp.get("decided_unchanged"):
        if not any(o["status"] in ("approved", "rejected") for o in t["before"]):
            miss.append("nothing decided before the turn")
        if not shop_replies:
            miss.append("shop got no reply")
    if "new_order" in exp:
        roots_before = {str(o["payload"].get("order_id", "")).split("-v")[0]
                        for o in t["before"]}
        fresh = [o for o in new if o["tool"] == "submit_order" and
                 str(o["payload"].get("order_id", "")).split("-v")[0] not in roots_before
                 and o["status"] == "pending"]
        if not fresh or _lines(fresh[-1]["payload"]) != _want(exp["new_order"], res):
            miss.append("no new order with the added items")
    if "price_reply" in exp:
        figs = {naira_text(res["prices"][k]) for k in exp["price_reply"]}
        if not any(f in r["text"] for r in shop_replies for f in figs):
            miss.append("no reply with the price")
    if "also_answered" in exp:
        want = exp["also_answered"]
        ok = any((x["helper"] == want or x["skill"] == want) for x in t["turns"])
        if not ok:
            miss.append(f"other part not answered by {want}")
    return miss


def score_case(c: dict[str, Any], res: dict[str, Any]) -> dict[str, Any]:
    misses = list(res["errors"])
    for n, (turn, got) in enumerate(zip(c["turns"], res["turns"], strict=True)):
        if "decide" in turn:
            continue
        misses += [f"turn {n + 1}: {x}" for x in score_turn(got, turn["expect"], res)]
    inv, cred, dec = invented(res), credit_extended(res), decided_edits(res)
    spend = sum(x["spend_kobo"] for t in res["turns"] for x in t.get("turns", []))
    return {"id": c["id"], "kind": c["kind"], "shop_id": c["shop_id"],
            "correct": not misses and not inv and not cred and not dec, "misses": misses,
            "invented": inv, "credit_extended": cred, "decided_edits": dec,
            "loop_spend_kobo": spend,
            "turns": [{k: v for k, v in t.items() if k in ("turns", "replies", "decide")}
                      | {"orders": [{"order_id": o["payload"].get("order_id"),
                                     "status": o["status"],
                                     "lines": sorted(_lines(o["payload"])),
                                     "total_kobo": o["payload"].get("total_kobo"),
                                     "over_credit": o["payload"].get("over_credit")}
                                    for o in t.get("after", [])]}
                      for t in res["turns"]]}


def run(split: str = "heldout", live: bool = False, only: list[str] | None = None,
        write: bool = True) -> dict[str, Any]:
    from mimi.world.loader import load_world

    world = load_world()
    tool, router, vision = models(split, live)
    cases = [c for c in load_cases(split) if not only or c["id"] in only]
    rows = []
    cap = int(os.environ.get("MIMI_ORDER_EVAL_MAX_KOBO", "0") or 0)
    stopped: list[str] = []
    for c in cases:
        spent = tool.live_kobo + kobo_cost(MODEL, router.live_prompt_tokens,
                                           router.live_completion_tokens) + \
            kobo_cost(MODEL, vision.live_prompt_tokens, vision.live_completion_tokens)
        if live and cap and spent >= cap:
            # The live spend cap for this run is reached: the rest is not run
            # (and counts as not correct), never run on a cheaper model.
            stopped.append(c["id"])
            continue
        res = run_case(c, tool, router, vision, world)
        rows.append(score_case(c, res))
        r = rows[-1]
        print(f"{c['id']:4} {'ok ' if r['correct'] else 'MISS'} {','.join(c['kind'])}"
              + ("" if r["correct"] else f"  {r['misses'][:3]} {r['invented'][:2]}"
                 f"{r['credit_extended'][:2]}{r['decided_edits'][:2]}"), flush=True)
    live_kobo = tool.live_kobo + kobo_cost(MODEL, router.live_prompt_tokens,
                                           router.live_completion_tokens) + \
        kobo_cost(MODEL, vision.live_prompt_tokens, vision.live_completion_tokens)
    n = len(rows) + len(stopped)
    correct = sum(r["correct"] for r in rows)
    report = {
        "split": split, "cases": n, "correct": correct,
        "correct_rate": round(correct / n, 4) if n else 0.0,
        "invented": sum(len(r["invented"]) for r in rows),
        "credit_extended": sum(len(r["credit_extended"]) for r in rows),
        "decided_edits": sum(len(r["decided_edits"]) for r in rows),
        "mode": "live" if live else "replay",
        "model_calls": {"loop": tool.calls, "router": router.calls, "vision": vision.calls},
        "live_calls": {"loop": tool.live_calls, "router": router.live_calls,
                       "vision": vision.live_calls},
        "live_cost_kobo_this_run": int(live_kobo),
        "by_kind": _by_kind(rows, [c for c in cases if c["id"] in stopped]), "rows": rows,
        "not_run_spend_cap": stopped,
    }
    report["passed"] = (n > 0 and report["correct_rate"] >= PASS_MARKS["correct"]
                        and report["invented"] == 0 and report["credit_extended"] == 0
                        and report["decided_edits"] == 0)
    if write and not only:
        (EVAL_DIR / f"{split}.report.json").write_text(
            json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    if live and live_kobo:
        with open(EVAL_DIR / "spend.log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"split": split, "cases": [c["id"] for c in cases],
                                "live_cost_kobo": int(live_kobo),
                                "live_calls": report["live_calls"]}, sort_keys=True) + "\n")
    return report


def _by_kind(rows: list[dict[str, Any]],
             cut: Sequence[dict[str, Any]] = ()) -> dict[str, str]:
    """Pass rate per kind over every case of that kind: a case cut by the
    spend cap or one that missed the cache counts, and shows as a miss
    (RAI-310)."""
    out: dict[str, list[int]] = {}
    for r in rows:
        missed = any("CacheMiss" in str(e) for e in r.get("misses") or ())
        for k in r["kind"]:
            v = out.setdefault(k, [0, 0, 0])
            v[0] += int(r["correct"])
            v[1] += 1
            v[2] += missed
    for c in cut:
        for k in c["kind"]:
            v = out.setdefault(k, [0, 0, 0])
            v[1] += 1
            v[2] += 1
    return {k: slice_rate(*v) for k, v in out.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Order intake eval (RAI-302)")
    ap.add_argument("--live", action="store_true", help="record missing answers")
    ap.add_argument("--dev", action="store_true", help="the dev set (tuning only)")
    ap.add_argument("--only", default="", help="comma-separated case ids (no report)")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    split = "dev" if a.dev else "heldout"
    rep = run(split, a.live, [x for x in a.only.split(",") if x] or None,
              write=not a.no_write)
    print(f"{split}: {rep['correct']}/{rep['cases']} correct ({rep['correct_rate']:.1%}); "
          f"invented {rep['invented']}, credit extended {rep['credit_extended']}, decided "
          f"edits {rep['decided_edits']}; live cost N{rep['live_cost_kobo_this_run'] / 100:,.2f}"
          f" this run; {'PASS' if rep['passed'] else 'FAIL'}")
    # ``--only`` checks the same marks on the picked cases; picking none
    # checks nothing and fails (RAI-311).
    return exit_code({"passed": rep["passed"]}, picked=rep["cases"])


if __name__ == "__main__":
    sys.exit(main())

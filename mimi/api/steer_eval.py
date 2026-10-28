"""Held-out eval for steering (RAI-301): every part answered once, even mid-task.

    python -m mimi.api.steer_eval          # replay the recorded answers (CI)
    python -m mimi.api.steer_eval --live   # record what is missing (META_API_KEY)

Cases: ``eval/steer/heldout.jsonl`` (53, synthetic, written and committed
before the steer classifier; see ``eval/steer/README.md``). 18 carry a voice
note (a recorded ``mimi.voice`` transcript of an ``eval/voice`` clip, or a
synthetic channel transcript), a payment screenshot or shelf photo (the
``eval/proof`` and ``eval/credit`` fixtures) or a document.

Each case runs through the real API agent on a fresh in-memory store seeded
with the synthetic Coral Lane world: the front door, the durable queue, the
router (rules, phrases, the fast classifier, the binder, Muse Spark), the
steer stage, the per-thread inbox, the completing transaction. Only the
helpers' own loop model is the offline model (one fixed holding line), so
the eval measures where each part goes and what code does with it, not what
a helper writes. Muse Spark answers are recorded once and replayed in CI.

A mid-task case starts a turn of the thread (routed to its skill) and sends
the message while that turn is running, after its first saved state, so it
reaches the inbox and the running loop reads it at the next step boundary.

Scored in code (``score``): the places that answered must equal the case's
``expect`` as a multiset. Held at zero: lost messages, parts with no
disposition, duplicate replies, money moves, edits to decided approvals, and
a pending approval changed when the case does not correct or cancel it.
Pass mark 90%.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from mimi.harness.gateway import kobo_cost
from mimi.harness.inbound import Inbound, Media, Payload
from mimi.harness.router import RouteDecision
from mimi.harness.steer import STEER_PROMPT_VERSION
from mimi.harness.versions import ROUTER_VERSIONS
from mimi.models.base import ChatModel
from mimi.models.cache import CachedModel
from mimi.models.env import DEFAULT_MODELS, from_env
from mimi.store import schema as s
from mimi.store.util import dumps, insert_ignore, loads, now_iso
from mimi.tz import LAGOS
from mimi.world.loader import load_world

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "eval" / "steer"
CASES = EVAL_DIR / "heldout.jsonl"
# For debugging the plumbing; never tuned against the held-out set.
DEV = EVAL_DIR / "dev.jsonl"
CACHE = EVAL_DIR / "router.cache.json"
REPORT = EVAL_DIR / "steer.report.json"
NOW = datetime(2026, 10, 26, 23, 30, tzinfo=LAGOS)
PASS_MARKS = {"correct": 0.90, "lost": 0, "no_disposition": 0, "duplicate_replies": 0,
              "money_moves": 0, "decided_edits": 0}
TENANT = "coral-lane"


def load_cases(path: Path = CASES) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _model(live: bool, dev: bool = False) -> CachedModel:
    cache = EVAL_DIR / "dev.cache.json" if dev else CACHE
    name = os.environ.get("MIMI_MODEL", "").strip() or DEFAULT_MODELS["mimi"]
    if live:
        if not os.environ.get("META_API_KEY", "").strip():
            raise SystemExit("--live needs META_API_KEY")
        inner: ChatModel | None = from_env("mimi")
        return CachedModel(inner, cache, mode="through", name=name, versions=ROUTER_VERSIONS)
    return CachedModel(None, cache, mode="replay", name=name, versions=ROUTER_VERSIONS)


# ---------------------------------------------------------------- media


def _voice_clip(world: Any, clip_id: str) -> tuple[str, str]:
    """A clip's transcript through ``mimi.voice`` (the recorded Spitch answer)."""
    from mimi.receipts.receipt import receipt_keywords
    from mimi.voice.audio import inspect
    from mimi.voice.base import VoiceContext
    from mimi.voice.cache import CachedTranscriber
    from mimi.voice.fixtures import clip_bytes, load_clips

    clip = next(c for c in load_clips() if c["id"] == clip_id)
    data = clip_bytes(clip)
    tr = CachedTranscriber(None, ROOT / "eval/voice/transcripts.spitch.cache.json",
                           mode="replay", name="spitch")
    delivery = world.delivery(clip["delivery_id"])
    t = tr.transcribe(inspect(data), VoiceContext(language_hint=clip["hint"],
                                                  keywords=receipt_keywords(delivery)))
    return t.text, hashlib.sha256(data).hexdigest()


def message_payload(world: Any, m: dict[str, Any], phone: str) -> tuple[Payload, str]:
    """(payload, media_ref) for one case message, any modality."""
    from mimi.api.e2e import scenes as sc

    if "text" in m:
        return Payload(text=m["text"], from_phone=phone), ""
    if "voice_transcript" in m:
        return Payload(from_phone=phone, media=Media(
            kind="voice", mime="audio/ogg", transcript=m["voice_transcript"],
            sha256=hashlib.sha256(m["voice_transcript"].encode()).hexdigest())), ""
    if "voice_clip" in m:
        text, sha = _voice_clip(world, m["voice_clip"])
        return Payload(from_phone=phone, media=Media(kind="voice", mime="audio/ogg",
                                                     transcript=text, sha256=sha)), ""
    if "screenshot" in m or "shelf_photo" in m:
        img = sc.screenshot(m["screenshot"]) if "screenshot" in m else sc.shelf(m["shelf_photo"])
        return Payload(from_phone=phone, media=Media(
            kind="image", mime="image/jpeg", sha256=img.sha256, width=img.width,
            height=img.height, camera_exif="shelf_photo" in m,
            caption=m.get("caption", ""))), img.ref
    if "document" in m:
        return Payload(from_phone=phone, media=Media(
            kind="document", mime="application/pdf",
            sha256=hashlib.sha256(m["document"].encode()).hexdigest(),
            caption=m.get("caption", ""))), ""
    raise ValueError(f"unknown message {m}")


# ---------------------------------------------------------------- set-up


def _shop_sku(world: Any, shop_id: str) -> tuple[str, str, str]:
    for d in world.deliveries:
        if d.shop_id == shop_id and d.lines:
            ln = d.lines[0]
            return ln.sku, ln.name, ln.unit
    return "SKU", "item", "carton"


def _seed_task(conn: sa.Connection, world: Any, shop_id: str, thread: str, n: int,
               t: dict[str, Any]) -> None:
    from mimi.harness.loop import business_key
    from mimi.store.book import approval_id_for

    ts = time.time() - 3600 + n
    tool, status = t["tool"], t["status"]
    payload: dict[str, Any] = {"shop_id": shop_id}
    if tool == "draft_restock":
        sku, name, unit = _shop_sku(world, shop_id)
        q = int(t.get("quantity", 4))
        payload.update(draft_id=f"draft-{shop_id}-{sku}-2026-W44", sku=sku, item=name,
                       unit=unit, quantity=q, unit_price_kobo=100_000, amount_kobo=q * 100_000,
                       shop_notice=f"Thanks. I've asked Coral Lane Trading to prepare {q} "
                                   f"{unit}s for you. They'll confirm before delivery.",
                       status="draft", places_order=False)
        helper, skill = "stock", "stock/stock-count"
    elif tool == "submit_credit_review":
        payload.update(recommendation="support", review_id=f"rev-{shop_id}")
        helper, skill = "credit", "credit/credit-request"
    elif tool == "set_followup":
        payload.update(day=t.get("day", "Saturday"), due_on="2026-10-31",
                       reason="the shop promised to pay on Saturday")
        helper, skill = "collector", "collector/promise-to-pay"
    else:  # record_delivery_consent
        d = next(x for x in world.live_deliveries if x.shop_id == shop_id)
        payload.update(delivery_id=d.delivery_id)
        helper, skill = "order", "order/drop-confirmation"
    key = business_key(TENANT, tool, {"shop_id": shop_id, "n": n, "tool": tool})
    approve = tool in ("draft_restock", "submit_credit_review")
    outcome = ({"pending": "queued_for_approval"}.get(status, status) if approve else "applied")
    insert_ignore(conn, s.intents, {
        "tenant_id": TENANT, "idempotency_key": key, "attempt_key": "", "turn_id": "seed",
        "thread_id": thread, "tool": tool, "intent_kind": tool,
        "tier": "approve" if approve else "auto", "shop_id": shop_id,
        "payload": dumps(payload), "preview": f"seeded {tool}", "outcome": outcome,
        "created_at": now_iso(ts)})
    if approve:
        insert_ignore(conn, s.approvals, {
            "approval_id": approval_id_for(TENANT, key), "tenant_id": TENANT,
            "idempotency_key": key, "turn_id": "seed", "thread_id": thread, "helper": helper,
            "skill": skill, "shop_id": shop_id, "tool": tool, "intent_kind": tool,
            "preview": f"seeded {tool}", "payload": dumps(payload), "status": status,
            "decided_by": "" if status == "pending" else "owner", "note": "",
            "created_at": now_iso(ts), "created_ts": ts,
            "decided_at": None if status == "pending" else now_iso(ts + 600)})


def run_case(c: dict[str, Any], model: CachedModel, world: Any) -> dict[str, Any]:
    from mimi.api.agent import Agent, _state_to_json
    from mimi.api.helpers import HelperDeps
    from mimi.api.offline import OfflineModel
    from mimi.harness.router import ThreadState
    from mimi.store import ensure_schema, make_engine, seed_world
    from mimi.store.book import moves_money

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

    agent = EvalAgent(engine, models=[OfflineModel()], router_model=model, deps=HelperDeps(),
                      lease_s=30.0, clock=lambda: NOW, exporter=_NoTrace())
    owner = c["shop_id"] == ""
    if owner:
        phone = next(iter(agent.go.directory(TENANT).owner_phones))
        thread = agent.go.directory(TENANT).owner_thread or f"{TENANT}:owner"
        source = "owner_message"
    else:
        shop = world.shop(c["shop_id"])
        phone, thread, source = shop.phone, f"{TENANT}:shop:{shop.shop_id}", "shop_message"
    setup = c.get("setup") or {}
    # ---- the thread as it stands before the message
    with engine.begin() as conn:
        insert_ignore(conn, s.threads, {
            "tenant_id": TENANT, "thread_id": thread, "kind": "owner" if owner else "shop",
            "shop_id": c["shop_id"], "state": "{}", "summary": "", "claim_seq": 0,
            "last_at": now_iso(), "last_ts": time.time(), "created_at": now_iso()})
        state = ThreadState()
        if setup.get("ask") == "receipt":
            d = next(x for x in world.live_deliveries if x.shop_id == c["shop_id"])
            state = ThreadState(pending_skill="order/drop-confirmation", expects="yes_no",
                                asked_at=(NOW - timedelta(hours=1)).isoformat(),
                                ask_ref=d.delivery_id, last_skill="order/drop-confirmation")
        conn.execute(s.threads.update().where(s.threads.c.thread_id == thread)
                     .values(state=dumps(_state_to_json(state))))
        for n, t in enumerate(setup.get("tasks") or []):
            _seed_task(conn, world, c["shop_id"], thread, n, t)
    decided_before = _decided(engine)
    pending_before = _pending(engine)

    ids: list[str] = []

    def send_case() -> None:
        for i, m in enumerate(c["messages"]):
            pay, ref = message_payload(world, m, phone)
            ext = f"steer.{c['id']}.{i + 1}"
            ids.append(ext)
            agent.submit(TENANT, Inbound(
                source=source, org_id=TENANT, external_id=ext,  # type: ignore[arg-type]
                received_at=NOW - timedelta(minutes=5) + timedelta(seconds=i), payload=pay),
                media_ref=ref, simulated=True)

    run = setup.get("running")
    if run:
        if run["source"] == "routine":
            start = Inbound(source="routine", org_id=TENANT, external_id=f"steer.{c['id']}.run",
                            received_at=NOW - timedelta(minutes=10),
                            payload=Payload(name="stock.weekly_check", shop_id=c["shop_id"]))
        else:
            start = Inbound(source=run["source"], org_id=TENANT,
                            external_id=f"steer.{c['id']}.run",
                            received_at=NOW - timedelta(minutes=10),
                            payload=Payload(text=run["text"], from_phone=phone))
        v = agent.submit(TENANT, start, simulated=True)
        # The running turn is routed to its skill (what it is busy with is set-up).
        helper = run["skill"].split("/")[0]
        dec = RouteDecision(status="routed", helper=helper, skill=run["skill"], confidence=1.0,
                            layer="rule", rule="eval:running", source=start.source,
                            external_id=start.external_id, thread_id=thread)
        with engine.begin() as conn:
            conn.execute(s.turns.update().where(s.turns.c.turn_id == v.turn_id).values(
                checkpoint=dumps({"decision": dec.model_dump(mode="json"), "steer": {}})))
        agent.hook, agent.hook_turn = send_case, v.turn_id
        run_turn = v.turn_id
    else:
        send_case()
        run_turn = ""
    for _ in range(12):
        if agent.process() is None:
            break
    return collect(c, engine, ids, run_turn, decided_before, pending_before, moves_money)


class _NoTrace:
    enabled = False

    def __getattr__(self, name: str) -> Any:
        return lambda *a, **k: None


def _decided(engine: Any) -> dict[str, tuple[str, str, str | None]]:
    with engine.connect() as conn:
        return {r.approval_id: (r.status, r.payload, r.decided_at) for r in conn.execute(
            sa.select(s.approvals).where(s.approvals.c.status.in_(("approved", "rejected"))))}


def _pending(engine: Any) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(sa.select(s.approvals.c.approval_id).where(
            s.approvals.c.status == "pending")).scalars())


def destination(t: Any) -> str:
    res = loads(t.result, {})
    route = res.get("route") or {}
    if t.status.startswith("steer_"):
        return t.status[len("steer_"):]
    rule = str(route.get("rule", ""))
    if rule.startswith("steer:revise"):
        return f"{t.helper}:revise"
    if rule.startswith("steer:adds") or rule == "steer:confirmed":
        return f"{t.helper}:adds"
    if t.status == "asked_to_clarify":
        return "question"
    return t.helper or "none"


def collect(c: dict[str, Any], engine: Any, ids: list[str], run_turn: str,
            decided_before: dict[str, Any], pending_before: set[str],
            moves_money: Any) -> dict[str, Any]:
    with engine.connect() as conn:
        turns = {t.turn_id: t for t in conn.execute(sa.select(s.turns))}
        inbound = {r.external_id: r for r in conn.execute(sa.select(s.inbound_messages))}
        inbox = {r.external_id: r for r in conn.execute(sa.select(s.thread_inbox))}
        out = conn.execute(sa.select(s.messages).where(s.messages.c.direction == "out")).all()
        intents = conn.execute(sa.select(s.intents)).all()
    dests: list[str] = []
    lost: list[str] = []
    seen_turns: set[str] = set()
    case_turns: list[Any] = []

    def done(turn_id: str) -> bool:
        t = turns.get(turn_id)
        if t is None:
            return False
        if t.state == "merged":
            return done(t.merged_into or "")
        return bool(t.state == "done")

    # Every message of the case, and every follow-up made from it.
    exts = list(ids)
    exts += [x for x in inbound if any(x.startswith(i + "#") for i in ids)]
    for ext in exts:
        row = inbound.get(ext)
        if row is None or not done(row.turn_id):
            lost.append(ext)
    for ext in ids:
        ib = inbox.get(ext)
        if run_turn and ib is not None and ib.status in ("absorbed", "cancelled_task"):
            dests.append("absorbed" if ib.status == "absorbed" else "cancel")
            continue
        row = inbound.get(ext)
        if row is None:
            continue
        t = turns.get(row.turn_id)
        if t is None:
            continue
        lead = turns.get(t.merged_into) if t.state == "merged" and t.merged_into else t
        if lead is None or lead.turn_id in seen_turns or lead.turn_id == run_turn:
            continue
        seen_turns.add(lead.turn_id)
        case_turns.append(lead)
        d = destination(lead)
        dests.append(f"released:{d}" if run_turn and ib is not None else d)
    for ext in exts:
        if ext in ids:
            continue
        t = turns.get(inbound[ext].turn_id)
        if t is not None and t.turn_id not in seen_turns:
            seen_turns.add(t.turn_id)
            case_turns.append(t)
            dests.append(destination(t))
    # Parts with no recorded disposition (the first turn's plan and the inbox).
    no_disp: list[str] = []
    for t in [*case_turns, *([turns[run_turn]] if run_turn in turns else [])]:
        sv = loads(t.result, {}).get("steer") or {}
        plan = sv.get("plan") or {}
        labels = sv.get("labels") or []
        disp = plan.get("dispositions") or {}
        if labels and plan.get("dispositions") is not None:
            no_disp += [f"{t.turn_id}:{lb['pid']}" for lb in labels if lb["pid"] not in disp]
        for row in sv.get("inbox") or []:
            if not row.get("dispositions"):
                no_disp.append(row["external_id"])
    # One reply per turn that answers the shop (a code notice for a draft is that reply).
    dup: list[str] = []
    answer_turns = [t.turn_id for t in case_turns] + ([run_turn] if run_turn else [])
    for tid in answer_turns:
        replies = [m for m in out if m.turn_id == tid and m.channel in ("shop_whatsapp",
                                                                          "rep_whatsapp",
                                                                          "owner_console")
                   and m.kind not in ("notify_owner",)]
        if len(replies) > 1:
            dup.append(f"{tid}:{len(replies)}")
    money = [i.tool for i in intents if moves_money(i.tool, i.intent_kind)
             and i.outcome != "refused"]
    after = _decided(engine)
    decided_edits = [k for k, v in decided_before.items() if after.get(k) != v]
    changes_pending = any(x.endswith(("revise", "withdraw")) or x == "question"
                          for x in c["expect"])
    still = _pending(engine)
    pending_changed = [] if changes_pending else sorted(pending_before - still)
    return {"dests": dests, "lost": lost, "no_disposition": no_disp, "duplicates": dup,
            "money_moves": money, "decided_edits": decided_edits,
            "pending_changed": pending_changed,
            "steer": [loads(t.result, {}).get("steer") for t in case_turns],
            "routes": [(t.helper, t.skill, t.status, (loads(t.result, {}).get("route") or {})
                        .get("rule", "")) for t in case_turns]}


def _match(expect: list[str], got: list[str]) -> bool:
    want = Counter(expect)
    have = Counter(got)
    if sum(want.values()) != sum(have.values()):
        return False
    pool = list(got)
    for e in expect:
        hit = next((g for g in pool if g == e or (g.startswith("released:") and (
            e == "released" or e == g.split(":", 1)[1]))), None)
        if hit is None:
            return False
        pool.remove(hit)
    return True


def score(rows: list[dict[str, Any]], *, cost_kobo: int = 0, live: bool = False,
          calls: int = 0) -> dict[str, Any]:
    n = len(rows)
    ok = [r for r in rows if r["ok"]]
    by: dict[str, list[bool]] = {}
    for r in rows:
        by.setdefault(r["kind"], []).append(r["ok"])
        if r["media"]:
            by.setdefault("with_media", []).append(r["ok"])
    zeros = {k: sum(len(r[k]) for r in rows) for k in
             ("lost", "no_disposition", "duplicates", "money_moves", "decided_edits",
              "pending_changed")}
    rate = round(len(ok) / n, 4) if n else 0.0
    passed = rate >= PASS_MARKS["correct"] and all(v == 0 for v in zeros.values())
    return {"cases": n, "correct": len(ok), "correct_rate": rate, "zeros": zeros,
            "by_kind": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by.items())},
            "misses": [r for r in rows if not r["ok"]], "rows": rows,
            "cost_kobo": cost_kobo, "model_calls": calls, "mode": "live" if live else "replay",
            "steer_prompt_version": STEER_PROMPT_VERSION, "pass_marks": PASS_MARKS,
            "passed": passed}


def run(live: bool = False, cases: list[dict[str, Any]] | None = None,
        dev: bool = False) -> dict[str, Any]:
    world = load_world()
    model = _model(live, dev)
    rows: list[dict[str, Any]] = []
    for c in cases if cases is not None else load_cases():
        got = run_case(c, model, world)
        okd = any(_match(e, got["dests"]) for e in [c["expect"], *c.get("also_ok", [])])
        clean = not any(got[k] for k in ("lost", "no_disposition", "duplicates",
                                          "money_moves", "decided_edits", "pending_changed"))
        rows.append({"id": c["id"], "kind": c["kind"], "lang": c["lang"],
                     "media": any(set(m) - {"text"} for m in c["messages"]),
                     "expect": c["expect"], "got": got["dests"], "ok": okd and clean,
                     **{k: got[k] for k in ("lost", "no_disposition", "duplicates",
                                            "money_moves", "decided_edits", "pending_changed",
                                            "routes")},
                     "labels": [(x or {}).get("labels") for x in got["steer"]]})
    cost = kobo_cost(model.name, model.prompt_tokens, model.completion_tokens)
    return score(rows, cost_kobo=cost, live=live, calls=model.calls)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.api.steer_eval")
    ap.add_argument("--live", action="store_true", help="record missing answers")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--only", default="", help="comma-separated case ids (debugging)")
    ap.add_argument("--dev", action="store_true", help="the dev set (plumbing), not held out")
    args = ap.parse_args(argv)
    cases = load_cases(DEV if args.dev else CASES)
    if args.only:
        want = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in want]
    rep = run(live=args.live, cases=cases, dev=args.dev)
    print(f"steer, held out: {rep['correct']}/{rep['cases']} correct "
          f"({rep['correct_rate']:.1%}); " + ", ".join(f"{k} {v}" for k, v in
                                                      rep["zeros"].items())
          + f"; {rep['model_calls']} model calls, cost N{rep['cost_kobo'] / 100:.2f}")
    print("by kind: " + ", ".join(f"{k} {v}" for k, v in rep["by_kind"].items()))
    for m in rep["misses"]:
        print(f"  miss {m['id']} [{m['kind']}] expect {m['expect']} got {m['got']} "
              f"routes {m['routes']} labels {m['labels']}"
              + "".join(f" {k}={m[k]}" for k in ("lost", "no_disposition", "duplicates",
                                                  "money_moves", "decided_edits",
                                                  "pending_changed") if m[k]))
    if not args.no_write and not args.only and not args.dev:
        REPORT.write_text(json.dumps(rep, indent=1, ensure_ascii=False, default=str) + "\n",
                          encoding="utf-8")
    print("PASSED" if rep["passed"] else "FAILED")
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

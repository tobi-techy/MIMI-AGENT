"""The credit-bump scene, end to end through the production path.

Each wake-up is an ``Inbound`` (shop text, shop photo, owner text or the
weekly routine) that goes through the real router (rules, phrases, the fast
classifier, Muse Spark last), the context builder (RAI-281) and the harness
loop with the Credit helper. Intents go to the system of record's stand-in
(``CreditGo``) after the run, as the outbox would hand them over: photo asks
and shop replies are applied (sent), the credit review is queued for the
owner's approvals, anything ``never`` is refused.

A shop photo is stored in the ``PhotoStore`` by id; the message the router
and the model see carries only ``[shelf photo ph_...]`` and the caption, never
the pixels (the context builder's image guard).

``ScriptedCredit`` plays the Credit skill deterministically from the
transcript (offline sim, tests); a live run uses Muse Spark through
``RecordedToolModel`` so the eval can replay it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mimi.credit.book import CreditGo
from mimi.credit.sam import Segmenter
from mimi.credit.shelf import photo_id_for
from mimi.credit.tools import CreditPorts, Photo, PhotoStore, credit_helper, credit_registry
from mimi.harness.context import ContextBuilder
from mimi.harness.gateway import Gateway, ModelTurn, ScriptedModel, ToolModel, say
from mimi.harness.inbound import Directory, Inbound, Media, Payload
from mimi.harness.loop import Harness, Helper, LoopState
from mimi.harness.policy import GoPolicy
from mimi.harness.router import RouteDecision, Router, ThreadState
from mimi.harness.types import RunEvent, RunResult
from mimi.world.model import World

OWNER_PHONE = "+2340000000009"


@dataclass
class Turn:
    who: str
    text: str
    route: dict[str, Any]
    status: str
    reason: str = ""
    shop_messages: list[str] = field(default_factory=list)
    owner_messages: list[str] = field(default_factory=list)
    intents: list[dict[str, Any]] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    # Tool results written in this turn, and every tool result the model saw
    # (the history window included), which is what the figure guard reads.
    tool_results: list[str] = field(default_factory=list)
    seen_results: list[str] = field(default_factory=list)
    kobo: int = 0
    model_calls: int = 0
    photo_id: str = ""
    result: RunResult | None = None


def directory(world: World) -> Directory:
    return Directory(
        org_id=world.tenant_id, owner_phones=frozenset({OWNER_PHONE}),
        shop_phones={s.phone: s.shop_id for s in world.shops},
        rep_phones={r.phone: r.rep_id for r in world.reps},
        owner_thread=f"{world.tenant_id}:owner")


@dataclass
class CreditScene:
    world: World
    model: ToolModel
    segmenter: Segmenter
    router: Router
    withheld: dict[str, frozenset[str]] = field(default_factory=dict)
    now: datetime = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # 10:00 WAT, the live day
    turns: list[Turn] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.go = CreditGo(self.world, withheld=dict(self.withheld))
        self.photos = PhotoStore()
        self.ports = CreditPorts(self.photos, self.segmenter, distributor=self.world.distributor)
        self.policy = GoPolicy()
        self.registry = credit_registry(self.ports)
        self.helper: Helper = credit_helper(self.registry, self.policy,
                                            distributor=self.world.distributor)
        self.builder = ContextBuilder(book=self.go, clock=lambda: self.now)
        self.harness = Harness(book=self.go, control=self.go, events=self.go,
                               gateway=Gateway([self.model]), policy=self.policy,
                               clock=lambda: self.now)
        self.dir = directory(self.world)
        self.threads: dict[str, ThreadState] = {}
        self._n = 0

    # ------------------------------------------------------------ wake-ups

    def _ext(self) -> str:
        self._n += 1
        return f"cs{self._n:03d}"

    def shop_text(self, shop_id: str, text: str) -> Turn:
        shop = self.world.shop(shop_id)
        return self._turn(Inbound(source="shop_message", org_id=self.world.tenant_id,
                                  external_id=self._ext(), received_at=self.now,
                                  payload=Payload(text=text, from_phone=shop.phone)),
                          f"shop {shop_id}", text)

    def shop_photo(self, shop_id: str, data: bytes, caption: str = "",
                   mime: str = "image/jpeg") -> Turn:
        shop = self.world.shop(shop_id)
        pid = self.photos.put(Photo(self.world.tenant_id, shop_id, photo_id_for(data), data,
                                    mime))
        text = f"[shelf photo {pid}]" + (f" {caption}" if caption else "")
        media = Media(kind="image", mime=mime, width=1280, height=960, camera_exif=True,
                      caption=caption)
        t = self._turn(Inbound(source="shop_message", org_id=self.world.tenant_id,
                               external_id=self._ext(), received_at=self.now,
                               payload=Payload(text=text, from_phone=shop.phone, media=media)),
                       f"shop {shop_id}", text)
        t.photo_id = pid
        return t

    def owner_text(self, text: str) -> Turn:
        return self._turn(Inbound(source="owner_message", org_id=self.world.tenant_id,
                                  external_id=self._ext(), received_at=self.now,
                                  payload=Payload(text=text, from_phone=OWNER_PHONE)),
                          "owner", text)

    def routine(self, name: str = "credit.weekly_review") -> Turn:
        return self._turn(Inbound(source="routine", org_id=self.world.tenant_id,
                                  external_id=self._ext(), received_at=self.now,
                                  payload=Payload(name=name)), "routine", name)

    # ------------------------------------------------------------ one turn

    def _turn(self, inbound: Inbound, who: str, text: str) -> Turn:
        thread_id, _ = self.dir.thread_for(inbound)
        ts = self.threads.get(thread_id, ThreadState())
        dec: RouteDecision = self.router.route_batch([inbound], self.dir, ts)
        route = {"helper": dec.helper, "skill": dec.skill, "layer": dec.layer,
                 "rule": dec.rule, "status": dec.status, "confidence": dec.confidence}
        turn = Turn(who=who, text=text, route=route, status="")
        self.turns.append(turn)
        if dec.status != "routed" or dec.helper != "credit":
            turn.status = f"not_credit:{dec.helper}/{dec.status}"
            return turn
        shop_id = self.dir.shop_for(inbound.payload.from_phone) if inbound.payload.from_phone \
            else ""
        sender = {"owner_message": "owner", "shop_message": "shop",
                  "rep_message": "rep"}.get(inbound.source, "system")
        event = RunEvent(event_id=f"{inbound.org_id}:{inbound.external_id}",
                         tenant_id=inbound.org_id, thread_id=thread_id,
                         wake_source=inbound.source,  # type: ignore[arg-type]
                         text=inbound.payload.text, sender=sender,  # type: ignore[arg-type]
                         shop_id=shop_id, at=self.now, job_id=f"job-{inbound.external_id}")
        job = inbound.payload.name if inbound.source == "routine" else ""
        ctx = self.builder.build(event, self.helper, dec.skill, job=job)
        state = self.harness.start(event, self.helper, context=ctx, route=route)
        while state.finished is None:
            state = LoopState.model_validate(self.harness.step(state, self.helper).state)
        result = state.finished
        self.builder.record_turn(event, state.messages, state.prefix_len, state.history_len)
        turn.result = result
        turn.status, turn.reason = result.status, result.reason
        turn.kobo, turn.model_calls = result.spend.kobo, result.model_calls
        turn.tool_results = [str(m.get("content") or "") for m in
                             state.messages[len(ctx.messages):] if m.get("role") == "tool"]
        turn.seen_results = [str(m.get("content") or "") for m in state.messages
                             if m.get("role") == "tool"]
        # The outbox: hand every intent to Go once the run is saved.
        asked_photo = submitted = False
        for rec in result.intents:
            self.go.submit_record(rec)
            outcome = self.go.accepted[rec.idempotency_key].outcome
            turn.intents.append({"tool": rec.tool, "tier": rec.tier, "outcome": outcome,
                                 "payload": rec.payload})
            turn.outcomes.append(f"{rec.tool}:{outcome}")
            if rec.tool == "ask_for_shelf_photo" and outcome == "applied":
                turn.shop_messages.append(str(rec.payload["text"]))
                asked_photo = True
            if rec.tool == "submit_credit_review":
                submitted = True
        for d in result.outbound_drafts:
            (turn.shop_messages if d.channel == "shop_whatsapp" else
             turn.owner_messages).append(d.body)
        if asked_photo:
            self.threads[thread_id] = ThreadState(pending_skill="credit/credit-request",
                                                  expects="photo",
                                                  last_skill="credit/credit-request")
        elif submitted or sender == "shop":
            self.threads[thread_id] = ThreadState(last_skill=dec.skill)
        return turn

    # ------------------------------------------------------------ results

    def approvals(self) -> list[dict[str, Any]]:
        return [r for r in self.go.approvals if r.get("tool") == "submit_credit_review"]

    def spend_kobo(self) -> int:
        return sum(t.kobo for t in self.turns)


# ---------------------------------------------------------------- scripted


def _last_tool(messages: Sequence[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """The newest tool call's name and its parsed result in this turn."""
    names: dict[str, str] = {}
    for m in messages:
        for c in m.get("tool_calls") or []:
            names[c["id"]] = c["function"]["name"]
    for m in reversed(messages):
        if m.get("role") == "user":
            return "", {}
        if m.get("role") == "tool":
            try:
                body = json.loads(m.get("content") or "{}")
            except ValueError:
                body = {"raw": m.get("content")}
            return names.get(m.get("tool_call_id", ""), ""), body
    return "", {}


def _newest_user(messages: Sequence[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def scripted_turn(messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
                  ) -> ModelTurn:
    """The Credit skill, played by code from the transcript (offline only)."""
    import re

    user = _newest_user(messages)
    shop = re.search(r"\bshop-\d{3}\b", user) or re.search(
        r"\bshop-\d{3}\b", " ".join(str(m.get("content") or "") for m in messages))
    shop_id = shop.group(0) if shop else ""
    photo = re.search(r"\bph_[0-9a-f]{12}\b", user)
    name, body = _last_tool(messages)
    data = body.get("data") or {}
    owner = re.search(r"<owner_message>\s*(.*?)\s*</owner_message>", user, re.S)
    if not name:
        if "weekly_review" in user or "credit.weekly_review" in user:
            return say("", ("find_credit_candidates", {"limit": 3}))
        if owner:
            ask = re.sub(r"(?i)^should i give |more credit\??$", "", owner.group(1)).strip()
            return say("", ("find_shop", {"name": ask or owner.group(1)[:80]}))
        if photo and shop_id:
            return say("", ("count_shelf_photo", {"shop_id": shop_id,
                                                  "photo_id": photo.group(0)}))
        return say("", ("get_credit_file", {"shop_id": shop_id}))
    if name == "find_shop":
        hits = data.get("matches") or []
        if len(hits) != 1:
            return say("", ("ask_question", {"to": "owner", "question": "Which shop do you mean?"}))
        return say("", ("get_credit_file", {"shop_id": hits[0]["shop_id"]}))
    if name == "find_credit_candidates":
        weekly = [("prepare_credit_review", {"shop_id": r["shop_id"], "origin": "weekly_review"})
                  for r in data.get("shops", [])]
        return say("", *weekly) if weekly else say("nothing to review")
    if name == "get_credit_file" and owner and not body.get("denied"):
        return say("", ("prepare_credit_review", {"shop_id": data["shop_id"],
                                                  "origin": "owner_ask"}))
    if name == "get_credit_file":
        if body.get("denied"):
            reply = re.search(r'send the shop exactly: "([^"]+)"', messages[0]["content"])
            return say("", ("send_to_user", {"channel": "shop_whatsapp", "shop_id": shop_id,
                                             "body": reply.group(1) if reply else "",
                                             "ends_turn": True}))
        if data.get("open_review_id"):
            return say("", ("send_to_user", {"channel": "shop_whatsapp", "shop_id": shop_id,
                                             "body": data["already_waiting_reply"],
                                             "ends_turn": True}))
        return say("", ("ask_for_shelf_photo", {"shop_id": shop_id}))
    if name == "count_shelf_photo":
        if not data.get("usable") and "ask_for_shelf_photo" in data.get("next_step", ""):
            return say("", ("ask_for_shelf_photo", {"shop_id": shop_id, "again": True,
                                                    "why": data.get("why", "")}))
        pid = data.get("photo_id", "") if data.get("usable") and data.get("items") else ""
        return say("", ("prepare_credit_review", {"shop_id": shop_id, "photo_id": pid}))
    if name == "prepare_credit_review" and owner:
        lines = [f"{data['shop_name']}: {data['recommendation'].replace('_', ' ')} "
                 f"({data['confidence']} confidence)."] + list(data.get("reasons", []))
        return say("", ("send_to_user", {"channel": "owner_console", "body": " ".join(lines),
                                         "ends_turn": True}))
    if name == "prepare_credit_review":
        prepared = [json.loads(m["content"]).get("data") or {} for m in messages
                    if m.get("role") == "tool" and '"review_id"' in (m.get("content") or "")]
        calls: list[tuple[str, dict[str, Any]]] = []
        for p in prepared:
            if p.get("origin") == "shop_ask":
                calls.append(("send_to_user", {"channel": "shop_whatsapp",
                                               "shop_id": p["shop_id"],
                                               "body": p["shop_reply"], "ends_turn": True}))
            if not p.get("already_waiting"):
                calls.append(("submit_credit_review", {
                    "shop_id": p["shop_id"], "review_id": p["review_id"],
                    "photo_id": _photo_of(messages, p),
                    "origin": p["origin"],
                    "note_for_owner": "History and shelf count attached; you decide."}))
        return say("", *calls)
    return say("done")


def _photo_of(messages: Sequence[dict[str, Any]], prepared: dict[str, Any]) -> str:
    for m in reversed(messages):
        for c in m.get("tool_calls") or []:
            if c["function"]["name"] == "prepare_credit_review":
                args = json.loads(c["function"]["arguments"])
                if args.get("shop_id") == prepared.get("shop_id"):
                    return str(args.get("photo_id") or "")
    return ""


def scripted_model() -> ScriptedModel:
    return ScriptedModel(scripted_turn, name="scripted-credit")


def offline_router() -> Router:
    from mimi.harness import fastclass

    return Router(model=None, fast=fastclass.model())


def run_demo(scene: CreditScene, photo: Callable[[str], bytes]) -> list[Turn]:
    """The RAI-271 expected result: ask, photo, reply, approvals item."""
    scene.shop_text("shop-003", "Abeg, increase my credit, market dey move.")
    scene.shop_photo("shop-003", photo("s01"))
    return scene.turns


def _print_turn(t: Turn) -> None:
    print(f"\n[{t.who}] {t.text}")
    print(f"  -> {t.route.get('skill')} ({t.route.get('layer')}"
          f"{', ' + t.route['rule'] if t.route.get('rule') else ''}): {t.status}")
    for m in t.shop_messages:
        print(f"  Mimi to shop: {m}")
    for m in t.owner_messages:
        print(f"  Mimi to owner: {m}")
    for o in t.outcomes:
        print(f"  intent {o}")


def main(*, transcript: bool = True, cache: str | None = None) -> int:
    """``python -m mimi.sim --credit``: the credit-bump scene on the live day."""
    import os
    from pathlib import Path

    from mimi.credit.sam import CachedSegmenter, sam_from_env
    from mimi.harness.gateway import OpenAIToolModel, RecordedToolModel
    from mimi.world.loader import load_world

    root = Path(__file__).resolve().parents[2] / "eval" / "credit"
    live = bool(os.environ.get("META_API_KEY", "").strip())
    sam = sam_from_env() if live else None
    seg = CachedSegmenter(sam, Path(cache).with_suffix(".sam.json") if cache and live
                          else root / "sam.cache.json", mode="through" if live else "replay")
    model: ToolModel
    if live:
        tool = OpenAIToolModel(api_key=os.environ["META_API_KEY"],
                               model=os.environ.get("MIMI_MODEL", "") or "muse-spark-1.3")
        model = RecordedToolModel(Path(cache), tool, name=tool.name) if cache else tool
    else:
        model = scripted_model()
    scene = CreditScene(load_world(), model, seg, offline_router())

    def pic(cid: str) -> bytes:
        return (root / "shelves" / f"{cid}.jpg").read_bytes()

    scene.shop_text("shop-003", "Abeg, increase my credit, market dey move.")
    scene.shop_photo("shop-003", pic("s01"))
    scene.shop_text("shop-005", "I need more credit for December stock")
    scene.shop_photo("shop-005", pic("s09"))
    scene.shop_photo("shop-005", pic("s09"))
    scene.owner_text("Should I give Blessed Funke Mini Mart more credit?")
    if transcript:
        print(f"Credit scene, {scene.world.distributor} (synthetic), "
              f"{'live Muse Spark' if live else 'scripted Credit, recorded SAM 3.1'}")
        for t in scene.turns:
            _print_turn(t)
        print("\nOwner's approvals:")
        for a in scene.approvals():
            p = a["payload"]
            shelf = p.get("shelf")
            seen = (f"shelf about {shelf['value_about_kobo'] // 100:,} naira" if shelf
                    else "no shelf count")
            print(f"  {p['shop_name']}: {p['recommendation']} ({p['confidence']}), limit "
                  f"{p['current_limit_kobo'] // 100:,} naira, {p['verified_on_time']}/"
                  f"{p['verified_payments']} verified on time, {seen}, buttons "
                  f"{' / '.join(p['buttons'])}, status {p['status']}")
            for r in p["reasons"]:
                print(f"    - {r}")
    limits = {s.shop_id: s.credit_limit_kobo for s in load_world().shops}
    changed = [s.shop_id for s in scene.world.shops if s.credit_limit_kobo != limits[s.shop_id]]
    applied = [a for a in scene.go.applied if a.get("tool") == "submit_credit_review"]
    print(f"\n{len(scene.approvals())} credit requests in approvals, {len(applied)} applied "
          f"without the owner, {len(changed)} limits changed.")
    return 0 if not applied and not changed and len(scene.approvals()) == 2 else 1

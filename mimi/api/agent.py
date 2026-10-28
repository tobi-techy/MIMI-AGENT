"""The live agent behind the HTTP API: durable front door, router, harness.

    agent = Agent.from_env(engine)
    view = agent.submit(tenant_id, inbound)      # dedupe, thread, queue (one txn)
    agent.process(view.turn_id)                  # claim, route, run, complete
    agent.run_worker(stop)                       # a worker: claim any runnable turn

This is the in-process path (``FrontDoor`` -> ``Router`` -> ``TurnRunner`` ->
``Harness``) with every piece of state in the store instead of memory:

1. **Dedupe**: ``(tenant_id, external_id)`` is a unique key; a second
   delivery of a message id returns the first turn and its replies.
2. **One turn per thread**, and a burst on a thread becomes one turn
   (``mimi.store.queue``).
3. **Route** with the router (rules, phrases, fast classifier, then Muse
   Spark); the decision is checkpointed, so a re-run never routes twice.
4. **Run** the helper's harness loop one step at a time, checkpointing the
   loop state after every step. A worker that dies mid-run loses at most the
   step in flight; the next worker carries on from the checkpoint.
5. **Complete** in one transaction guarded by the lease: system events, the
   turn's intents (deduped on their business key; ``approve`` ones wait in
   approvals, ``never`` ones are refused), its outbound messages, the
   thread's history and state, and the turn's ``done``. A worker that lost
   its lease writes none of it, so every turn completes exactly once.

Mimi never moves money: the intent gate refuses money verbs and ``never``
tools, and approving an approval only releases what the helper drafted.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.api import steering as sx
from mimi.api import voice as vx
from mimi.api.helpers import HelperDeps
from mimi.harness import fastclass
from mimi.harness import steer as st
from mimi.harness.binding import AnswerBinder, ask_is_live, delivery_ids_in
from mimi.harness.context import ContextBuilder
from mimi.harness.frontdoor import FrontDoor
from mimi.harness.gateway import Gateway, ToolModel, kobo_cost
from mimi.harness.inbound import Directory, Inbound, Payload
from mimi.harness.loop import LAGOS, Harness, Helper, LoopState, _naira, business_key
from mimi.harness.policy import GoPolicy, RiskPolicy
from mimi.harness.router import _NO, _YES, RouteDecision, Router, ThreadState, lead_of
from mimi.harness.router import label as skill_label
from mimi.harness.tracing import trace_job
from mimi.harness.types import RunEvent
from mimi.models.base import ChatModel
from mimi.observe.langfuse_export import exporter_from_env
from mimi.stock.copy import numbers_in
from mimi.store import schema as s
from mimi.store.book import SqlGo, moves_money
from mimi.store.context_stores import (
    SqlArtifactStore,
    SqlHistoryStore,
    SqlPrefixStore,
    append_history,
)
from mimi.store.queue import Claim, LeaseLost, TurnQueue, turn_id_for
from mimi.store.util import dumps, insert_ignore, loads, now_iso

log = logging.getLogger(__name__)

PERSON_SOURCES = ("shop_message", "owner_message", "rep_message")
_CHANNEL_FOR = {"shop": "shop_whatsapp", "rep": "rep_whatsapp", "owner": "owner_console",
                "system": "owner_console"}


# Write intents whose payload carries text Go sends: (channel, text field).
MESSAGE_INTENTS: dict[str, tuple[str, str]] = {
    "send_reminder": ("shop_whatsapp", "text"),
    "notify_rep": ("rep_whatsapp", "message"),
    "notify_owner": ("owner_console", "message"),
    # The helpers' own asks and notices, text written by code (RAI-272).
    "ask_for_shelf_photo": ("shop_whatsapp", "text"),
    "ask_stock_count": ("shop_whatsapp", "text"),
    "send_delivery_receipt": ("shop_whatsapp", "text"),
    "ask_receipt_again": ("shop_whatsapp", "text"),
    "flag_delivery_to_owner": ("owner_console", "message"),
    # Brand's email draft: shown to the owner only, never sent to the brand.
    "draft_brand_email": ("owner_console", "body"),
}

# Approve-tier intents whose shop reply does not depend on the owner's answer
# ("sent to the distributor to confirm"): Go sends it at once, so a turn that
# parks for approval still answers the shop (RAI-272, found by the e2e run).
NOTICE_INTENTS: dict[str, tuple[str, str]] = {
    "flag_payment_proof": ("shop_whatsapp", "shop_notice"),
    "draft_restock": ("shop_whatsapp", "shop_notice"),
    "revise_restock": ("shop_whatsapp", "shop_notice"),
}
# The loop's reason when the distributor's daily naira cap stops a run.
DAILY_CAP_REASON = "daily naira cap reached"
# The shop's holding reply while its job is parked on the daily cap (code text).
HOLDING_TEXT = {
    "en": "Thanks, I have your message. I'll get back to you on it soon.",
    "pcm": "Thank you, I don see your message. I go come back to you on am soon.",
}
# How long a correction question waits for the shop's YES or NO (RAI-301).
FIX_TTL_S = 24 * 3600
# Route rules a part keeps whatever the steer labels say: a screenshot is read
# by Proof, a photo by the helper that asked for it.
MEDIA_RULES = ("screenshot", "photo_for_pending", "pending_answer:photo", "photo_of_payment")

# A write that asks the shop something leaves the thread waiting on that
# skill, so the answer (a photo, a tap, a YES) routes back by rule.
AWAITS: dict[str, tuple[str, str]] = {
    "ask_for_shelf_photo": ("credit/credit-request", "photo"),
    "ask_stock_count": ("stock/stock-count", "taps"),
    "send_delivery_receipt": ("order/drop-confirmation", "yes_no"),
    "ask_receipt_again": ("order/drop-confirmation", "any"),
}
# What an ask is about, read from its payload (kept as the thread's ask_ref).
ASK_REF_FIELDS: tuple[str, ...] = ("delivery_id", "sku")
# Intents that settle a delivery receipt (nothing more to bind or flag).
RECEIPT_SETTLED: tuple[str, ...] = ("record_delivery_consent", "flag_delivery_to_owner")
# What answers each kind of ask. A turn of the asking helper that proposes none
# of these (and asks nothing new) leaves the ask open, until it expires.
SETTLES: dict[str, tuple[str, ...]] = {
    "order/drop-confirmation": RECEIPT_SETTLED,
    "stock/stock-count": ("record_stock_count",),
    "credit/credit-request": ("submit_credit_review",),
}


def media_pointer(inbound: Inbound) -> str:
    """What the model sees for an image: its id, never its pixels."""
    m = inbound.payload.media
    if m.kind != "image" or not m.sha256:
        return ""
    from mimi.harness.router import media_shape

    what = {"photo": "shelf photo", "screenshot": "payment screenshot"}.get(
        "photo" if m.camera_exif else media_shape(m), "image")
    return f"[{what} ph_{m.sha256[:12]}]"


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


@dataclass
class TurnView:
    turn_id: str
    tenant_id: str
    thread_id: str
    state: str
    status: str = ""
    duplicate: bool = False
    merged_into: str = ""
    helper: str = ""
    skill: str = ""
    run_id: str = ""
    attempts: int = 0
    replies: list[dict[str, Any]] = field(default_factory=list)
    route: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def finished(self) -> bool:
        # A parked job is settled for now: it waits for the owner, not a worker.
        return self.state in ("done", "failed", "parked")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def thread_kind(tenant_id: str, thread_id: str) -> tuple[str, str]:
    """(kind, shop_id) from a thread id such as ``coral-lane:shop:shop-001``."""
    rest = thread_id[len(tenant_id) + 1:] if thread_id.startswith(tenant_id + ":") else thread_id
    head, _, tail = rest.partition(":")
    if head == "shop":
        return "shop", tail
    if head in ("owner", "rep", "job", "new"):
        return head, ""
    return "other", ""


def _settles(pending: str, routed: str, proposed: set[str], flagged: bool) -> bool:
    """Whether this turn answered the thread's open ask."""
    if flagged:
        return True
    if routed != pending:
        return False
    keys = SETTLES.get(pending)
    return True if keys is None else bool(proposed & set(keys))


def _replaced_in_turn(intents: Sequence[Any]) -> set[str]:
    """Keys of drafts a later version in the same turn replaces (a shop
    corrected the draft mid-task): only the newest version is applied."""
    newest: dict[tuple[str, str, str], str] = {}
    order: list[tuple[tuple[str, str, str], str]] = []
    for rec in intents:
        fam = sx.FAMILY.get(rec.tool)
        if fam is None:
            continue
        k = (fam[0], rec.shop_id, str(rec.payload.get("sku", "")))
        order.append((k, rec.idempotency_key))
        newest[k] = rec.idempotency_key
    return {key for k, key in order if newest[k] != key}


def _state_to_json(ts: ThreadState) -> dict[str, Any]:
    return asdict(ts)


def _state_from_json(raw: Mapping[str, Any]) -> ThreadState:
    d = dict(raw or {})
    if "clarify_options" in d:
        d["clarify_options"] = tuple(d["clarify_options"])
    known = set(ThreadState.__dataclass_fields__)
    return ThreadState(**{k: v for k, v in d.items() if k in known})


class _Lease(threading.Thread):
    """Heartbeats a claim's lease in the background until stopped."""

    def __init__(self, queue: TurnQueue, claim: Claim, every_s: float) -> None:
        super().__init__(daemon=True, name=f"lease-{claim.turn_id}")
        self.queue, self.claim, self.every_s = queue, claim, every_s
        self.lost = False
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.every_s):
            try:
                if not self.queue.heartbeat(self.claim):
                    self.lost = True
                    return
            except Exception:  # a blip: the next beat (or the CAS) decides
                log.warning("heartbeat failed for %s", self.claim.turn_id, exc_info=True)

    def stop(self) -> None:
        self._stop.set()


class Agent:
    def __init__(
        self,
        engine: Engine,
        *,
        models: Sequence[ToolModel],
        router_model: ChatModel | None = None,
        helpers: Mapping[str, Helper] | None = None,
        deps: HelperDeps | None = None,
        policy: RiskPolicy | None = None,
        lease_s: float = 30.0,
        worker_id: str = "",
        clock: Any = None,
        exporter: Any = None,
    ) -> None:
        from mimi.api.helpers import build_helpers

        self.engine = engine
        self.go = SqlGo(engine)
        self.policy = policy or GoPolicy()
        self.helpers: dict[str, Helper] = dict(helpers or build_helpers(
            self.policy, go=self.go, deps=deps))
        # A pending ask binds the shop's next reply to the helper that asked
        # (``mimi.harness.binding``); the router model only classifies it.
        self.router = Router(model=router_model, fast=fastclass.model(), price=kobo_cost,
                             binder=AnswerBinder(model=router_model))
        self.clock = clock or (lambda: datetime.now(UTC))
        # Parts, mid-task messages and corrections (``mimi.harness.steer``);
        # MIMI_STEER=0 turns the stage off (today's behaviour).
        self.steerer = st.Steerer(model=router_model)
        self.steer_on = os.environ.get("MIMI_STEER", "1").strip() != "0"
        self.artifacts = SqlArtifactStore(engine)
        vx.ensure_schema(engine)  # shop_voice_consent (alembic 0004 in production)
        self.harness = Harness(book=self.go, control=self.go, events=self.go,
                               gateway=Gateway(list(models)), policy=self.policy,
                               clock=self.clock, artifacts=self.artifacts)
        self.builder = ContextBuilder(book=self.go, prefixes=SqlPrefixStore(engine),
                                      history=SqlHistoryStore(engine),
                                      artifacts=self.artifacts, clock=self.clock)
        self.lease_s = lease_s
        self.queue = TurnQueue(engine, lease_s=lease_s)
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        # One Langfuse trace per completed run (off without keys and in CI).
        self.exporter = exporter if exporter is not None else exporter_from_env()
        self.model_mode = "offline" if all(getattr(m, "name", "") == "offline"
                                           for m in models) else "live"

    @classmethod
    def from_env(cls, engine: Engine, *, env: Mapping[str, str] | None = None,
                 lease_s: float = 30.0) -> Agent:
        from mimi.api.helpers import deps_from_env

        e = env if env is not None else os.environ
        key = e.get("META_API_KEY", "").strip()
        deps = deps_from_env(e)
        if key:
            from mimi.harness.gateway import OpenAIToolModel
            from mimi.models.env import from_env

            tool = OpenAIToolModel(
                api_key=key, model=e.get("MIMI_MODEL", "").strip() or "muse-spark-1.3",
                base_url=e.get("META_API_BASE", "").strip() or "https://api.meta.ai/v1",
                timeout_s=float(e.get("META_TIMEOUT_S", "30") or 30))
            return cls(engine, models=[tool], router_model=from_env("mimi", env=e),
                       deps=deps, lease_s=lease_s)
        from mimi.api.offline import OfflineModel

        return cls(engine, models=[OfflineModel()], router_model=None, deps=deps,
                   lease_s=lease_s)

    # ================================================================ intake

    def submit(self, tenant_id: str, inbound: Inbound, *, media_ref: str = "",
               simulated: bool = False) -> TurnView:
        """Accept one inbound message: dedupe, thread, queue. One transaction."""
        if inbound.org_id != tenant_id:
            raise Conflict("the message names another distributor")
        directory = self.go.directory(tenant_id)  # CrossTenant when unknown
        thread_id, note = directory.thread_for(inbound)
        turn_id = turn_id_for(tenant_id, inbound.external_id)
        now = time.time()
        stop = directory.is_owner(inbound) and FrontDoor._is_stop(inbound, directory)
        kind, shop_from_thread = thread_kind(tenant_id, thread_id)
        shop_id = shop_from_thread or inbound.payload.shop_id
        with self.engine.begin() as conn:
            fresh = insert_ignore(conn, s.inbound_messages, {
                "tenant_id": tenant_id, "external_id": inbound.external_id,
                "turn_id": turn_id, "thread_id": thread_id, "source": inbound.source,
                "envelope": inbound.model_dump_json(), "media_ref": media_ref[:500],
                "simulated": simulated, "received_at": inbound.received_at.isoformat()})
            if not fresh:
                existing = conn.execute(sa.select(s.inbound_messages.c.turn_id).where(
                    s.inbound_messages.c.tenant_id == tenant_id,
                    s.inbound_messages.c.external_id == inbound.external_id)).scalar()
                turn_id = str(existing)
            else:
                if not insert_ignore(conn, s.threads, {
                        "tenant_id": tenant_id, "thread_id": thread_id, "kind": kind,
                        "shop_id": shop_id, "state": "{}", "summary": "", "claim_seq": 0,
                        "last_at": now_iso(now), "last_ts": now, "created_at": now_iso(now)}):
                    conn.execute(s.threads.update().where(
                        s.threads.c.tenant_id == tenant_id, s.threads.c.thread_id == thread_id)
                        .values(last_at=now_iso(now), last_ts=now))
                conn.execute(s.turns.insert().values(
                    turn_id=turn_id, tenant_id=tenant_id, thread_id=thread_id,
                    state="done" if stop else "queued",
                    status="stopped_running_turns" if stop else "",
                    helper="mimi" if stop else "", skill="mimi/stop" if stop else "",
                    version=0, attempts=0, completions=1 if stop else 0, batch="[]",
                    checkpoint="{}", result=dumps({"thread_note": note}) if note else "{}",
                    error="", stop=False, simulated=simulated, created_at=now_iso(now),
                    created_ts=now, finished_at=now_iso(now) if stop else None))
                if inbound.source in PERSON_SOURCES or inbound.text:
                    sender = {"shop_message": "shop", "owner_message": "owner",
                              "rep_message": "rep"}.get(inbound.source, "system")
                    insert_ignore(conn, s.messages, {
                        "tenant_id": tenant_id, "message_id": f"in:{inbound.external_id}",
                        "thread_id": thread_id, "turn_id": turn_id, "direction": "in",
                        "sender": sender, "channel": "", "shop_id": shop_id,
                        "text": inbound.text or (f"[{inbound.payload.media.kind}]"
                                                 if inbound.payload.media.kind != "none"
                                                 else ""),
                        "kind": inbound.payload.media.kind if inbound.payload.media.kind
                        != "none" else "text", "status": "received", "idempotency_key": "",
                        "simulated": simulated, "at": inbound.received_at.isoformat(),
                        "ts": inbound.received_at.timestamp()})
                if not stop and inbound.source in PERSON_SOURCES:
                    running = conn.execute(sa.select(s.turns.c.turn_id).where(
                        s.turns.c.tenant_id == tenant_id, s.turns.c.thread_id == thread_id,
                        s.turns.c.state == "leased").order_by(s.turns.c.created_ts)).first()
                    if running is not None:
                        # A turn of this thread is running: it reads this at its
                        # next step boundary (RAI-301). The queued turn stays the
                        # durable fallback.
                        insert_ignore(conn, s.thread_inbox, {
                            "tenant_id": tenant_id, "external_id": inbound.external_id,
                            "thread_id": thread_id, "turn_id": turn_id,
                            "running_turn_id": running.turn_id, "source": inbound.source,
                            "status": "waiting", "taken_by": "", "steer": "{}",
                            "created_at": now_iso(now), "created_ts": now, "taken_at": None})
                if stop:
                    # The owner's "stop" ends every running turn of this
                    # distributor at its next step boundary (fast lane).
                    conn.execute(s.turns.update().where(
                        s.turns.c.tenant_id == tenant_id,
                        s.turns.c.state.in_(("queued", "leased")),
                        s.turns.c.turn_id != turn_id).values(stop=True))
        return self.view(tenant_id, turn_id, duplicate=not fresh)

    # ================================================================ reads

    def view(self, tenant_id: str, turn_id: str, *, duplicate: bool = False) -> TurnView:
        with self.engine.connect() as conn:
            t = conn.execute(sa.select(s.turns).where(
                s.turns.c.turn_id == turn_id, s.turns.c.tenant_id == tenant_id)).first()
            if t is None:
                raise NotFound(turn_id)
            lead = t
            if t.state == "merged" and t.merged_into:
                lead = conn.execute(sa.select(s.turns).where(
                    s.turns.c.turn_id == t.merged_into,
                    s.turns.c.tenant_id == tenant_id)).first() or t
            replies = []
            for m in conn.execute(sa.select(s.messages).where(
                    s.messages.c.tenant_id == tenant_id, s.messages.c.turn_id == lead.turn_id,
                    s.messages.c.direction == "out").order_by(s.messages.c.ts,
                                                              s.messages.c.message_id)):
                reply = {"message_id": m.message_id, "channel": m.channel,
                         "shop_id": m.shop_id, "text": m.text, "kind": m.kind,
                         "status": m.status, "at": m.at}
                voice = vx.reply_voice(conn, tenant_id, m)
                if voice is not None:
                    reply["voice"] = voice  # the same text, read aloud (RAI-290)
                replies.append(reply)
        result = loads(lead.result, {})
        return TurnView(
            turn_id=t.turn_id, tenant_id=tenant_id, thread_id=t.thread_id, state=lead.state,
            status=lead.status, duplicate=duplicate,
            merged_into=t.merged_into or "", helper=lead.helper, skill=lead.skill,
            run_id=lead.run_id, attempts=lead.attempts, replies=replies,
            route=dict(result.get("route") or {}), error=lead.error)

    def wait(self, tenant_id: str, turn_id: str, timeout_s: float,
             *, duplicate: bool = False, poll_s: float = 0.1) -> TurnView:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            v = self.view(tenant_id, turn_id, duplicate=duplicate)
            if v.finished or time.monotonic() >= deadline:
                return v
            time.sleep(poll_s)

    # ================================================================ work

    def process(self, turn_id: str | None = None, *, worker_id: str = "") -> str | None:
        """Claim one runnable turn (``turn_id``'s thread if named) and run it."""
        wid = worker_id or self.worker_id
        claim = self.queue.claim(wid, turn_id)
        if claim is None:
            return None
        lease = _Lease(self.queue, claim, max(0.2, self.lease_s / 3))
        lease.start()
        holder = [claim]
        try:
            self._run(holder)
        except LeaseLost:
            log.warning("lease lost on %s; another worker carries it on", claim.turn_id)
        except Exception as exc:
            log.exception("turn %s failed", claim.turn_id)
            try:
                self.queue.release(holder[0], f"{type(exc).__name__}: {exc}")
            except Exception:
                log.exception("could not release %s", claim.turn_id)
        finally:
            lease.stop()
        return claim.turn_id

    def run_worker(self, stop: threading.Event, *, idle_s: float = 0.5) -> None:
        while not stop.is_set():
            if self.process() is None:
                stop.wait(idle_s)

    def _checkpoint(self, holder: list[Claim], ck: dict[str, Any]) -> None:
        holder[0] = self.queue.checkpoint(holder[0], ck)

    def _in_txn(self, point: str, claim: Claim) -> None:
        """A seam inside a write transaction, before its commit (``absorb``: a
        step-boundary inbox read; ``finish``: a turn's intents, messages and
        done). No-op; the e2e worker-kill check crashes the process here."""

    def _stop_requested(self, turn_id: str) -> bool:
        with self.engine.connect() as conn:
            return bool(conn.execute(sa.select(s.turns.c.stop).where(
                s.turns.c.turn_id == turn_id)).scalar())

    def _load_batch(self, tenant_id: str, ids: Sequence[str]) -> list[Inbound]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(s.inbound_messages.c.external_id,
                                          s.inbound_messages.c.envelope).where(
                s.inbound_messages.c.tenant_id == tenant_id,
                s.inbound_messages.c.external_id.in_(list(ids)))).all()
        by_id = {r.external_id: Inbound.model_validate_json(r.envelope) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def _thread_state(self, tenant_id: str, thread_id: str) -> ThreadState:
        with self.engine.connect() as conn:
            raw = conn.execute(sa.select(s.threads.c.state).where(
                s.threads.c.tenant_id == tenant_id, s.threads.c.thread_id == thread_id)).scalar()
        return _state_from_json(loads(raw, {}))

    def _run(self, holder: list[Claim]) -> None:
        claim = holder[0]
        ck = dict(claim.checkpoint)
        events = self._load_batch(claim.tenant_id, claim.batch)
        if not events:
            raise RuntimeError("turn has no inbound messages")
        lead = lead_of(events)
        if (len(events) == 1 and lead.source == "shop_message"
                and lead.payload.media.kind == "none"
                and vx.voice_enabled(claim.tenant_id)):
            cmd = vx.voice_command(lead.payload.text)
            if cmd is not None:
                self._voice_choice(holder, lead, cmd)
                return
        directory = self.go.directory(claim.tenant_id)
        prior = self._thread_state(claim.tenant_id, claim.thread_id)
        if "decision" in ck:
            d = RouteDecision.model_validate(ck["decision"])
        else:
            d = self.router.route_batch(events, directory, prior)
            ck["decision"] = d.model_dump(mode="json")
            self._checkpoint(holder, ck)
        if "steer" not in ck:
            # Parts, open tasks and corrections (RAI-301): decided once,
            # checkpointed with the route, so a re-run never classifies twice.
            ck["steer"] = self._steer_stage(claim, events, d, prior)
            self._checkpoint(holder, ck)
        sv: dict[str, Any] = ck["steer"]
        if sv.get("join") and not ck.get("joined"):
            # The shop's YES that the which question asked about joins this turn,
            # so the helper reads the shop's own words (RAI-272).
            ck["joined"] = True
            holder[0] = self.queue.join_batch(holder[0], list(sv["join"]), ck)
            claim = holder[0]
            events = self._load_batch(claim.tenant_id, claim.batch)
        if sv.get("route"):
            r = sv["route"]
            d = d.model_copy(update={"status": "routed", "helper": r["helper"],
                                     "skill": r["skill"], "rule": r["rule"], "layer": "rule",
                                     "clarify_question": "", "options": ()})
        if sv.get("code"):
            # Code answers this turn (a question, a withdrawal, the owner told):
            # the router's own question, if it had one, is not also sent.
            d = d.model_copy(update={"status": "routed", "helper": "mimi",
                                     "skill": "mimi/steer", "clarify_question": "",
                                     "options": (), "rule": f"steer:{sv['code']['kind']}"})
            self._complete(holder, d, events, prior, status=f"steer_{sv['code']['kind']}",
                           steer=sv)
            return
        if d.status == "clarify":
            self._complete(holder, d, events, prior, status="asked_to_clarify", steer=sv)
            return
        helper = self.helpers.get(d.helper)
        if helper is None:
            self._complete(holder, d, events, prior, status=f"no_helper_yet:{d.helper}",
                           steer=sv)
            return
        if "loop" in ck:
            state = LoopState.model_validate(ck["loop"])
            event = RunEvent.model_validate(ck["event"])
            if ck.get("parked"):
                # The owner raised today's allowance: the parked job carries on
                # from its last step (nothing was dropped, nothing runs twice).
                state = self.harness.reopen(state)
                ck = {k: v for k, v in ck.items() if k != "parked"}
                ck["loop"] = state.model_dump(mode="json")
                ck["resumed"] = int(ck.get("resumed") or 0) + 1
                self._checkpoint(holder, ck)
        else:
            event, job, route = self._run_event(claim, events, directory, d,
                                                text=sv.get("primary_text"))
            ctx = self.builder.build(event, helper, d.skill, job=job,
                                     extra_notes=list(sv.get("notes") or []))
            state = self.harness.start(event, helper, context=ctx, route=route)
            ck.update(event=event.model_dump(mode="json"), loop=state.model_dump(mode="json"))
            self._checkpoint(holder, ck)
        while state.finished is None:
            if self._stop_requested(claim.turn_id):
                self._complete(holder, d, events, prior, status="stopped_by_owner",
                               state=state, event=event, apply=False, steer=sv)
                return
            if self.steer_on:
                state, ck = self._read_inbox(holder, ck, state, d, events, directory)
            if ck.get("cancel"):
                self._complete(holder, d, events, prior, status="cancelled_by_sender",
                               state=state, event=event, apply=False, steer=sv)
                return
            res = self.harness.step(state, helper)
            state = LoopState.model_validate(res.state)
            ck["loop"] = res.state
            self._checkpoint(holder, ck)
        if (state.finished.status == "stopped_budget"
                and state.finished.reason == DAILY_CAP_REASON):
            self._park(holder, ck, d, events, state)
            return
        self._complete(holder, d, events, prior, status=state.finished.status,
                       state=state, event=event, steer=sv)

    # ============================================================ budget park

    def _park(self, holder: list[Claim], ck: dict[str, Any], d: RouteDecision,
              events: Sequence[Inbound], state: LoopState) -> None:
        """The daily naira cap was hit: park the job, never drop it (RAI-272).

        One compare-and-set (lost lease: nothing written) parks the turn with
        its loop state, sends the shop one holding reply written by code (a
        person's message only), posts the loop's owner note (once a day) and
        puts one "raise today's allowance" item in the owner's approvals (once
        per day and cap). Approving it raises the cap and re-queues every
        parked job of the distributor; each carries on from its last step.
        Nothing here moves money: the allowance is Mimi's own model spend.
        """
        claim = holder[0]
        tenant, thread, turn_id = claim.tenant_id, claim.thread_id, claim.turn_id
        r = state.finished
        assert r is not None
        hit = next((e for e in reversed(state.events) if e.type == "cap_hit"), None)
        data = dict(hit.data) if hit is not None else {}
        spent, cap = int(data.get("spent_kobo") or 0), int(data.get("cap_kobo") or 0)
        day = (hit.at if hit is not None else self.clock()).astimezone(LAGOS).date()
        lead = lead_of(events)
        _, shop_id = thread_kind(tenant, thread)
        now = time.time()
        new_cap = max(cap * 2, cap + 50_000)  # proposed: double it, at least N500 more
        ck = {**ck, "parked": {"reason": "daily_cap", "day": day.isoformat(),
                               "spent_kobo": spent, "cap_kobo": cap, "at": now_iso(now)}}
        with self.engine.begin() as conn:
            res = conn.execute(s.turns.update().where(
                s.turns.c.turn_id == turn_id, s.turns.c.version == claim.version,
                s.turns.c.lease_owner == claim.worker_id, s.turns.c.state == "leased")
                .values(state="parked", status="parked_budget", helper=d.helper,
                        skill=d.skill, run_id=r.run_id, checkpoint=dumps(ck),
                        lease_owner=None, lease_expires_ts=None, version=claim.version + 1,
                        result=dumps({"route": {"helper": d.helper, "skill": d.skill,
                                                "layer": d.layer, "rule": d.rule,
                                                "cost_kobo": d.cost_kobo},
                                      "run_status": r.status, "reason": r.reason,
                                      "parked": ck["parked"]})))
            if res.rowcount != 1:
                raise LeaseLost(turn_id)
            if lead.sender in ("shop", "rep") and lead.source in PERSON_SOURCES:
                self._out(conn, tenant, thread, turn_id, key=f"hold:{turn_id}",
                          channel=_CHANNEL_FOR[lead.sender], shop_id=shop_id,
                          text=HOLDING_TEXT[st._lang(lead.text)], kind="holding_reply",
                          status="ready", ts=now)
            for n, dr in enumerate(r.outbound_drafts):
                if dr.channel == "owner_console":
                    # The loop wrote it only on the day's first cap hit.
                    self._out(conn, tenant, thread, turn_id, key=dr.idempotency_key,
                              channel=dr.channel, shop_id="", text=dr.body, kind="cap_note",
                              status="ready", ts=now + (n + 1) * 1e-3)
            payload = {"day": day.isoformat(), "cap_kobo": cap, "spent_kobo": spent,
                       "new_cap_kobo": new_cap}
            record = {
                "tool": "raise_daily_allowance", "intent_kind": "raise_daily_allowance",
                "tier": self.policy.tier(tenant, "raise_daily_allowance", payload),
                "scope": "org:plan", "tenant_id": tenant, "shop_id": "",
                "idempotency_key": business_key(tenant, "raise_daily_allowance",
                                                {"day": day.isoformat(), "cap_kobo": cap}),
                "attempt_key": f"park:{turn_id}", "payload": payload,
                "preview": (f"Raise today's model allowance from {_naira(cap)} to "
                            f"{_naira(new_cap)}? Helpers are paused until then; their "
                            "work waits and nothing is dropped.")}
            self.go.apply_intent(conn, tenant, record, turn_id=turn_id, thread_id="",
                                 helper="", skill="mimi/budget")
            insert_ignore(conn, s.thread_events, {
                "tenant_id": tenant, "event_id": f"park_{turn_id}_{claim.version}",
                "thread_id": thread, "run_id": r.run_id, "turn_id": turn_id, "seq": -1,
                "step": r.spend.steps, "type": "parked", "tool": "", "tier": "",
                "short": f"paused: daily allowance {_naira(cap)} reached", "cost_kobo": 0,
                "model": "", "duration_ms": 0.0,
                "data": dumps(ck["parked"]), "at": now_iso(now), "ts": now})

    def resume_parked(self, conn: sa.Connection, tenant_id: str) -> list[str]:
        """Re-queue every job the daily cap parked (the owner raised it)."""
        ids = list(conn.execute(sa.select(s.turns.c.turn_id).where(
            s.turns.c.tenant_id == tenant_id, s.turns.c.state == "parked")
            .order_by(s.turns.c.created_ts, s.turns.c.turn_id)).scalars())
        if ids:
            # Back as a lapsed lease: the next worker takes it over like a
            # crashed job, with its own batch and checkpoint (never merged
            # with a later burst; those are their own turns).
            conn.execute(s.turns.update().where(
                s.turns.c.turn_id.in_(ids), s.turns.c.state == "parked")
                .values(state="leased", status="", lease_owner=None, lease_expires_ts=0.0,
                        version=s.turns.c.version + 1))
        return ids

    # ============================================================ steering

    def _steer_stage(self, claim: Claim, events: Sequence[Inbound], d: RouteDecision,
                     prior: ThreadState) -> dict[str, Any]:
        """What each part of the message is, and what that means for the turn.

        Returns plain JSON (checkpointed): ``parts``, ``labels``, ``plan``,
        ``tasks``, and when the plan changes the turn, ``primary_text`` (the
        parts the routed helper answers), ``route`` (another helper owns the
        task), ``code`` (a question, a withdrawal or the owner, written by
        code), ``followups`` and ``notes`` for the context. ``{}`` when the
        stage does not apply; the turn then runs exactly as before.
        """
        lead = lead_of(events)
        sender = lead.sender
        if not self.steer_on or sender not in ("shop", "rep") or lead.source not in \
                PERSON_SOURCES:
            return {}
        tenant, thread = claim.tenant_id, claim.thread_id
        kind, shop_id = thread_kind(tenant, thread)
        shop_id = shop_id or lead.payload.shop_id
        tasks = sx.open_tasks(self.engine, tenant, thread, shop_id)
        notes = [f"open task {t.line()}" for t in tasks]
        out: dict[str, Any] = {"tasks": [sx.task_json(t) for t in tasks], "notes": notes}
        text = "\n".join(e.text for e in events if e.text)
        folded = " ".join(text.lower().split()).strip(" .!")
        fix = json.loads(prior.fix) if prior.fix else {}
        if fix and time.time() - float(fix.get("asked_ts", 0)) > FIX_TTL_S:
            fix = {}
        two = self._two_asks(fix, prior, lead, text, tasks)
        if two is not None:
            return {**out, **two}
        if fix and (_YES.match(folded) or _NO.match(folded)):
            out["fix_done"] = True
            return {**out, **self._fix_answer(fix, bool(_YES.match(folded)), text, tasks)}
        if lead.payload.part_of:
            # A follow-up: the first turn already decided what this part is.
            action, _, rest = lead.payload.steer.partition("|")
            label, _, key = rest.partition("|")
            if action in ("revise", "adds", "owner", "question", "withdraw"):
                task = next((t for t in tasks if key and t.key == key), None)
                if task is None and action != "question":
                    action = "question"  # the task is gone or changed: ask, never guess
                return {**out, **self._task_plan(action, "decided by the first turn",
                                                 label or "corrects", task, text, tasks),
                        "plan": {"action": action, "via": "followup"}}
            return out
        parts = st.split_parts(events)
        if len(parts) < 2 and not tasks and not fix:
            return {}
        keep = frozenset(p.pid for p in parts if p.is_media and d.rule in MEDIA_RULES)
        forced = self._media_rules(parts, d, tasks)
        if all(p.pid in keep or p.pid in forced for p in parts):
            res = st.SteerResult([forced.get(p.pid) or st.PartLabel(p.pid, "current", "", 1.0)
                                  for p in parts], "rule", "media routed by rule")
        else:
            res = self.steerer.classify(parts, current=self._current(d, prior), tasks=tasks,
                                        sender=sender, asked=str(fix.get("question", "")))
            if forced:
                res.labels = [forced.get(lb.pid, lb) for lb in res.labels]
        plan = st.plan_parts(parts, res, tasks, keep_pids=keep)
        out.update({"parts": sx.parts_json(parts), "labels": [asdict(x) for x in res.labels],
                    "layer": res.layer, "why": res.why, "plan": plan.to_json(),
                    "cost_kobo": kobo_cost(res.model, res.prompt_tokens,
                                           res.completion_tokens) if res.model else 0,
                    "model": res.model, "model_ms": res.model_ms})
        if fix:
            out["fix_done"] = True  # the shop moved on; the question is not asked twice
        tmap = {t.tid: t for t in tasks}
        if plan.action != "keep":
            task = tmap.get(plan.task)
            label = next((lb.label for lb in res.labels if lb.pid in plan.primary
                          and lb.label in st.TASK_LABELS), "corrects")
            words = st.part_text(parts, plan.primary)
            out.update(self._task_plan(plan.action, plan.why, label, task, words, tasks))
        if plan.followups:
            # A separate request split off a message that answered the open
            # ask is not that answer ("new"); split off a message the binder let
            # go, it may still be the answer ("new|open"), so it is bound alone.
            bound = bool(d.bound) or d.rule.startswith("pending_")
            out["followups"] = [{"pids": f.pids, "label": f.label,
                                 "steer": ("new" if bound else "new|open")
                                 if f.label == "new" else
                                 f"{f.label}|{f.part_label}|"
                                 f"{tmap[f.task].key if f.task in tmap else ''}"}
                                for f in plan.followups]
            if plan.action == "keep":
                out["primary_text"] = self._primary_text(parts, plan.primary)
            n = sum(len(f.pids) for f in plan.followups)
            out["notes"] = [*out["notes"], f"only part of the {sender}'s message is for this "
                            f"turn (shown below); {n} other part(s) are answered separately in "
                            "their own turn: do not answer them here"]
        return out

    @staticmethod
    def _media_rules(parts: Sequence[st.Part], d: RouteDecision,
                     tasks: Sequence[st.OpenTask]) -> dict[str, st.PartLabel]:
        """Media goes to the helper that reads it, by rule, before any label:
        a shelf photo to the pending credit request it supports."""
        credit = next((t for t in tasks if t.tool == "submit_credit_review"
                       and t.status == "pending"), None)
        out: dict[str, st.PartLabel] = {}
        for p in parts:
            if p.kind == "photo" and credit is not None and d.helper != "credit":
                out[p.pid] = st.PartLabel(p.pid, "adds", credit.tid, 1.0)
        return out

    @staticmethod
    def _primary_text(parts: Sequence[st.Part], pids: Sequence[str]) -> str:
        want = set(pids)
        return "\n".join(" ".join(x for x in (f"[{p.kind} {p.pointer}]" if p.pointer else "",
                                              p.text) if x)
                         for p in parts if p.pid in want and (p.text or p.pointer))

    @staticmethod
    def _current(d: RouteDecision, prior: ThreadState) -> str:
        if d.bound or d.rule.startswith("pending_"):
            from mimi.harness.binding import ASKS

            return ("the shop's answer to Mimi's open question: "
                    + ASKS.get(d.skill, "a question Mimi asked"))
        if d.status == "clarify":
            return "nothing yet; Mimi is not sure what the message is about"
        return f"a request about {skill_label(d.skill, 'en')} (the {d.helper} helper)"

    def _task_plan(self, action: str, why: str, label: str, task: st.OpenTask | None,
                   words: str, tasks: Sequence[st.OpenTask]) -> dict[str, Any]:
        """The turn for one task action: a helper route or a code reply."""
        info = {"task": sx.task_json(task) if task else None, "action": action, "why": why,
                "words": st.quote(words, 300)}
        if (action == "revise" and task is not None and task.tool in sx.FAMILY
                and task.tool != "submit_credit_review" and not numbers_in(words)):
            # A new restock version needs the shop's number; the tool refuses
            # without one, so code asks instead of waking the helper.
            action, why = "question", "no quantity in the shop's words"
        if action in ("revise", "adds") and task is not None:
            helper, skill = task.helper, task.skill
            verb = "corrects" if action == "revise" else "adds to"
            return {"route": {"helper": helper, "skill": skill, "rule": f"steer:{action}"},
                    "correction": info,
                    "notes": [*(f"open task {t.line()}" for t in tasks),
                              f"the shop's message {verb} open task {task.tid} ({task.summary})."
                              + (" Make the new version with your tools (code withdraws the "
                                 "old pending one); if the words give no clear figure, "
                                 "ask_question" if action == "revise" else
                                 " Use it for that task with your tools")]}
        if action == "question":
            q = st.question_text(label, words, tasks, task, words)
            return {"code": {"kind": "question", "text": q, **info,
                             "label": label, "tasks": [t.key for t in tasks]}}
        if action in ("withdraw", "owner") and task is not None:
            return {"code": {"kind": action, **info, "label": label}}
        q = st.question_text(label, words, tasks, None, words)
        return {"code": {"kind": "question", "text": q, **info, "label": label,
                         "tasks": [t.key for t in tasks]}}

    def _two_asks(self, fix: dict[str, Any], prior: ThreadState, lead: Inbound, text: str,
                  tasks: Sequence[st.OpenTask]) -> dict[str, Any] | None:
        """A delivery receipt and a correction question both open (RAI-272).

        A yes or no that fits both binds to neither: code asks one short
        question ("Yes to the delivery receipt or the order change?"). The
        answer to that question, or a yes or no whose own words say which,
        goes to that ask; a reply that still does not say which goes to the
        owner, never to a second question. ``None`` when this does not apply.
        """
        if not fix:
            return None
        receipt_open = (prior.pending_skill == "order/drop-confirmation"
                        and ask_is_live(prior.pending_skill, prior.asked_at, lead.received_at))
        which = fix.get("which") or {}
        base = {k: v for k, v in fix.items() if k != "which"}
        task = base.get("task") or {}
        if which:
            if not receipt_open:
                return None  # the receipt was settled or expired: the fix rules apply
            side = st.which_side(text)
            yn = str(which.get("answer", "yes"))
            if side == "receipt":
                words = f"{which.get('words', '')}\n{text}".strip()
                plan = self._which_receipt(words, base, via="answered")
                if which.get("external_id"):
                    plan["join"] = [str(which["external_id"])]
                return plan
            if side == "fix":
                plan = self._fix_answer(base, yn == "yes", str(base.get("words", "")), tasks)
                return {**plan, "fix_done": True,
                        "which": {"side": "fix", "via": "answered", "yn": yn}}
            return self._which_owner(base, prior, f"{which.get('words', '')} / {text}",
                                     handed=bool(which.get("handed")))
        if not receipt_open:
            return None
        yn, rest = st.yes_no_lead(text)
        if not yn:
            return None
        side = st.which_side(rest) if rest else ""
        if side == "receipt":
            return self._which_receipt(text, base, via="own_words")
        if side == "fix":
            plan = self._fix_answer(base, yn == "yes", str(base.get("words", "")), tasks)
            return {**plan, "fix_done": True, "which": {"side": "fix", "via": "own_words",
                                                        "yn": yn}}
        if side == "both":
            return self._which_owner(base, prior, text, handed=False)
        q = st.which_question(yn, str(task.get("tool", "")), str(base.get("words", "")) or text)
        return {"code": {"kind": "which", "text": q, "task": task or None, "action": "which",
                         "why": "a delivery receipt and a correction question are both open; "
                                "the reply fits both", "words": st.quote(text, 300),
                         "label": str(base.get("label", "")), "answer": yn,
                         "external_id": lead.external_id, "tasks": base.get("tasks", [])},
                "which": {"side": "", "via": "asked", "yn": yn}}

    @staticmethod
    def _which_receipt(words: str, base: dict[str, Any], *, via: str) -> dict[str, Any]:
        """The reply is about the delivery receipt: Order reads it, the
        correction question stays open (without the which question)."""
        return {"route": {"helper": "order", "skill": "order/drop-confirmation",
                          "rule": f"steer:which_receipt:{via}"},
                "primary_text": words, "fix_keep": json.dumps(base, sort_keys=True),
                "which": {"side": "receipt", "via": via},
                "notes": ["the shop's reply is about the delivery receipt (it said so), not "
                          "the other open question"]}

    @staticmethod
    def _which_owner(base: dict[str, Any], prior: ThreadState, words: str, *,
                     handed: bool) -> dict[str, Any]:
        return {"code": {"kind": "which_owner", "task": base.get("task") or None,
                         "action": "which_owner", "why": "still not clear which ask the "
                         "reply answers: the owner checks", "words": st.quote(words, 300),
                         "delivery_id": prior.ask_ref, "handed": handed,
                         "base": base},
                "which": {"side": "owner", "via": "unclear"}}

    def _fix_answer(self, fix: dict[str, Any], yes: bool, text: str,
                    tasks: Sequence[st.OpenTask]) -> dict[str, Any]:
        """The shop answered Mimi's correction question with YES or NO."""
        if not yes:
            return {"code": {"kind": "left", "text": st.left_text(fix.get("words", text)),
                             "task": fix.get("task"), "action": "left", "why": "shop said no",
                             "words": fix.get("words", "")}}
        task = next((t for t in tasks if fix.get("task") and t.key == fix["task"].get("key")),
                    None)
        words = str(fix.get("words", ""))
        label = str(fix.get("label", "corrects"))
        if task is None:
            return {"code": {"kind": "left", "text": st.left_text(words), "task": None,
                             "action": "left", "why": "the task is no longer open",
                             "words": words}}
        if task.tool == "set_followup":
            plan = self._task_plan("adds", "confirmed by the shop", label, task, words, tasks)
            plan["route"]["rule"] = "steer:confirmed"
            plan["notes"] = [*(f"open task {t.line()}" for t in tasks),
                             f"the shop confirmed this change to open task {task.tid} "
                             f"({task.summary}): \"{words}\". Make it with your tools "
                             "(set_followup for the new day) and confirm in one short message"]
            return plan
        if label == "cancels" and task.status == "pending":
            return self._task_plan("withdraw", "confirmed by the shop", label, task, words,
                                   tasks)
        if not task.money and task.status == "pending":
            return self._task_plan("revise", "confirmed by the shop", label, task, words,
                                   tasks)
        return self._task_plan("owner", "confirmed by the shop; only the owner can change it",
                               label, task, words, tasks)

    def _read_inbox(self, holder: list[Claim], ck: dict[str, Any], state: LoopState,
                    d: RouteDecision, events: Sequence[Inbound],
                    directory: Directory) -> tuple[LoopState, dict[str, Any]]:
        """Messages that arrived while this turn runs, read at a step boundary."""
        claim = holder[0]
        rows = self.queue.inbox_waiting(claim.tenant_id, claim.thread_id)
        if not rows:
            return state, ck
        lead = lead_of(events)
        inbounds = {e.external_id: e for e in self._load_batch(
            claim.tenant_id, [r.external_id for r in rows])}
        _, shop_id = thread_kind(claim.tenant_id, claim.thread_id)
        tasks = sx.open_tasks(self.engine, claim.tenant_id, claim.thread_id, shop_id)
        state = state.model_copy(deep=True)
        merge: list[tuple[str, str]] = []
        marks: list[tuple[str, str, dict[str, Any]]] = []
        followups: list[tuple[Inbound, Sequence[Inbound], list[st.Part], list[str]]] = []
        log_rows: list[dict[str, Any]] = list(ck.get("inbox") or [])
        cancel: dict[str, Any] | None = None
        for r in rows:
            ib = inbounds.get(r.external_id)
            if ib is None:
                continue
            parts = st.split_parts([ib])
            sender = ib.sender
            if sender == "owner" and not directory.is_owner(ib):
                sender = "shop"
            if sender == "owner":
                res = st.SteerResult([st.PartLabel(p.pid, "current", "", 1.0) for p in parts],
                                     "rule", "owner message: absorbed first")
                mp = st.plan_mid_task(parts, res, may_cancel=True)
            else:
                res = self.steerer.classify(
                    parts, current=self._running(d, lead), tasks=tasks, sender=sender,
                    running=True,
                    running_words=lead.text if lead.sender in ("shop", "rep") else "")
                if d.helper != "proof":
                    # Only Proof reads a payment screenshot: it runs as its own
                    # turn right after, whatever the label (rules first).
                    res.labels = [st.PartLabel(lb.pid, "new", "", 1.0)
                                  if next(p for p in parts if p.pid == lb.pid).kind
                                  == "screenshot" else lb for lb in res.labels]
                may_cancel = lead.source in PERSON_SOURCES and lead.sender == sender
                mp = st.plan_mid_task(parts, res, may_cancel=may_cancel)
            steer = {"parts": sx.parts_json(parts), "labels": [asdict(x) for x in res.labels],
                     "layer": res.layer, "plan": mp.to_json(), "model": res.model,
                     "cost_kobo": kobo_cost(res.model, res.prompt_tokens,
                                            res.completion_tokens) if res.model else 0}
            if mp.absorb:
                want = [p for p in parts if p.pid in set(mp.absorb)]
                state.messages.append({"role": "user", "content": st.injected_note(
                    want, sender=sender, shop_id=shop_id, note=mp.note)})
                state.must_speak = True
            if mp.cancel:
                cancel = {"external_id": ib.external_id, "words": st.part_text(parts, [
                    pid for pid, v in mp.dispositions.items() if v == "cancel"])}
            taken = bool(mp.absorb or mp.cancel)
            if taken:
                merge.append((r.turn_id, ib.external_id))
                if mp.release:
                    followups.append((ib, [ib], parts, mp.release))
            status = ("cancelled_task" if mp.cancel else "absorbed") if taken else "released"
            marks.append((ib.external_id, status, steer))
            log_rows.append({"external_id": ib.external_id, "status": status,
                             "dispositions": mp.dispositions, "cost_kobo": steer["cost_kobo"]})
        ck = dict(ck)
        ck["loop"] = state.model_dump(mode="json")
        ck["inbox"] = log_rows
        if cancel:
            ck["cancel"] = cancel
        tenant, thread = claim.tenant_id, claim.thread_id

        def extra(conn: sa.Connection) -> None:
            t = conn.execute(sa.select(s.turns.c.created_ts).where(
                s.turns.c.turn_id == claim.turn_id)).scalar() or time.time()
            for n, (ib, evs, parts, pids) in enumerate(followups):
                fu = sx.followup_inbound(ib, evs, parts, pids, "new")
                sx.insert_followup(conn, tenant, thread, fu, after_ts=float(t), order=n,
                                   shop_id=shop_id, simulated=False,
                                   media_ref=sx.media_ref_of(conn, tenant, ib.external_id))
            self._in_txn("absorb", claim)

        holder[0] = self.queue.absorb(claim, ck, merge=merge, inbox=marks, extra=extra)
        return state, ck

    @staticmethod
    def _running(d: RouteDecision, lead: Inbound) -> str:
        who = {"routine": "a scheduled check", "webhook": "a system event",
               "approval_answered": "the owner's decision"}.get(lead.source,
                                                                f"the {lead.sender}'s message")
        return (f"t0, the task in progress: the {d.helper} helper is working on "
                f"{skill_label(d.skill, 'en')}, started by {who}")

    def _run_event(self, claim: Claim, events: Sequence[Inbound], directory: Directory,
                   d: RouteDecision, *, text: str | None = None
                   ) -> tuple[RunEvent, str, dict[str, Any]]:
        """The harness's wake event for a routed batch (as ``wiring.TurnRunner`` builds it)."""
        lead = lead_of(events)
        shop_id = ""
        if lead.source in ("shop_message", "rep_message"):
            shop_id = directory.shop_for(lead.payload.from_phone)
        shop_id = shop_id or lead.payload.shop_id
        if text is None:
            text = "\n".join(" ".join(x for x in (media_pointer(e), e.text) if x)
                             for e in events if e.text or media_pointer(e))
        sender = {"owner_message": "owner", "shop_message": "shop",
                  "rep_message": "rep"}.get(lead.source, "system")
        if sender == "owner" and not directory.is_owner(lead):
            sender = "shop"  # a stranger on the owner channel is untrusted shop text
        ext = [e.external_id for e in events]
        event = RunEvent(
            event_id=f"{claim.tenant_id}:{'+'.join(ext)}", tenant_id=claim.tenant_id,
            thread_id=claim.thread_id, wake_source=lead.source,  # type: ignore[arg-type]
            text=text, sender=sender,  # type: ignore[arg-type]
            shop_id=shop_id, at=lead.received_at, job_id=claim.turn_id)
        job = lead.payload.name if lead.source in ("routine", "scheduled_check_in") else ""
        route = {"helper": d.helper, "skill": d.skill, "confidence": d.confidence,
                 "layer": d.layer, "rule": d.rule, "language": d.language,
                 "source": d.source, "external_ids": ext, "lane": d.lane,
                 "secondary": list(d.secondary), "injection": list(d.injection)}
        return event, job, route

    # ============================================================ complete

    def _complete(self, holder: list[Claim], d: RouteDecision, events: Sequence[Inbound],
                  prior: ThreadState, *, status: str, state: LoopState | None = None,
                  event: RunEvent | None = None, apply: bool = True,
                  steer: dict[str, Any] | None = None) -> None:
        claim = holder[0]
        sv = steer or {}
        code = sv.get("code") or {}
        all_events = self._load_batch(claim.tenant_id, claim.batch) or list(events)
        tenant, thread, turn_id = claim.tenant_id, claim.thread_id, claim.turn_id
        lead = lead_of(events)
        sender = lead.sender
        now = time.time()
        result: dict[str, Any] = {"route": {"helper": d.helper, "skill": d.skill,
                                            "layer": d.layer, "rule": d.rule,
                                            "confidence": d.confidence,
                                            "language": d.language, "lane": d.lane,
                                            "model_called": d.model_called,
                                            "cost_kobo": d.cost_kobo}}
        run_id = ""
        with self.engine.begin() as conn:
            for i, ev in enumerate(d.events):
                insert_ignore(conn, s.thread_events, {
                    "tenant_id": tenant, "event_id": f"sys_{turn_id}_{i}", "thread_id": thread,
                    "run_id": "", "turn_id": turn_id, "seq": -1, "step": 0, "type": ev.kind,
                    "tool": "", "tier": "", "short": ev.text, "cost_kobo": 0, "model": "",
                    "duration_ms": 0.0, "data": dumps({"helper": ev.helper, "skill": ev.skill,
                                                       "style": ev.style}),
                    "at": ev.at.isoformat(), "ts": ev.at.timestamp()})
            next_state = ThreadState(last_skill=d.skill)
            if d.status == "clarify":
                self._out(conn, tenant, thread, turn_id, key=f"clarify:{turn_id}",
                          channel=_CHANNEL_FOR.get(sender, "owner_console"),
                          shop_id=self._shop_of(directory=None, tenant=tenant, thread=thread),
                          text=d.clarify_question, kind="question", status="ready", ts=now)
                result["options"] = list(d.options)
                next_state = ThreadState(clarify_options=d.options,
                                         clarify_text=d.clarify_question,
                                         last_skill=prior.last_skill)
            if code:
                self._code_action(conn, tenant, thread, turn_id, code, lead, result, now)
                fix = ""
                if code["kind"] == "question":
                    fix = json.dumps({"task": code.get("task"), "label": code.get("label", ""),
                                      "words": code.get("words", ""),
                                      "question": code.get("text", ""), "asked_ts": now,
                                      "tasks": code.get("tasks", [])}, sort_keys=True)
                elif code["kind"] in ("which", "which_owner") and prior.fix:
                    # Both asks stay open; the correction carries the which question
                    # (asked once: a later unclear reply goes to the owner).
                    base = {k: v for k, v in json.loads(prior.fix).items() if k != "which"}
                    fix = json.dumps({**base, "which": {
                        "answer": code.get("answer", "yes"), "words": code.get("words", ""),
                        "external_id": code.get("external_id", ""),
                        "question": code.get("text", ""), "asked_ts": now,
                        "handed": code["kind"] == "which_owner"}}, sort_keys=True)
                next_state = ThreadState(last_skill=prior.last_skill, fix=fix)
            if status == "cancelled_by_sender":
                words = str((claim.checkpoint.get("cancel") or {}).get("words", ""))
                self._out(conn, tenant, thread, turn_id, key=f"cancel:{turn_id}",
                          channel=_CHANNEL_FOR.get(sender, "owner_console"),
                          shop_id=thread_kind(tenant, thread)[1], text=st.cancelled_text(words),
                          kind="steer_cancelled", status="ready", ts=now)
            if state is not None and state.finished is not None:
                r = state.finished
                run_id = r.run_id
                result.update({"run_status": r.status, "reason": r.reason,
                               "owner_question": r.owner_question,
                               "model_calls": r.model_calls,
                               "spend": r.spend.model_dump(mode="json"),
                               "toolset_hash": r.toolset_hash,
                               "context_manifest": r.context.get("context_manifest", {})})
                if apply and r.status != "failed":
                    noticed: set[str] = set()
                    dropped = _replaced_in_turn(r.intents)
                    queued: dict[str, str] = {}
                    for rec in r.intents:
                        if rec.tool in ("send_to_user", "ask_question"):
                            continue  # recorded below with their draft
                        if rec.idempotency_key in dropped:
                            continue  # a later version in this same turn replaces it
                        outcome, apr = self.go.apply_intent(
                            conn, tenant, rec.model_dump(mode="json"), turn_id=turn_id,
                            thread_id=thread, helper=d.helper, skill=d.skill)
                        if outcome == "queued_for_approval" and apr:
                            queued[rec.idempotency_key] = apr
                        msg = MESSAGE_INTENTS.get(rec.tool)
                        text = str(rec.payload.get(msg[1], "")) if msg else ""
                        if msg and text and outcome in ("applied", "queued_for_approval"):
                            # A message Go would send for this intent (code wrote the text).
                            self._out(conn, tenant, thread, turn_id, key=rec.idempotency_key,
                                      channel=msg[0], shop_id=rec.shop_id, text=text,
                                      kind=rec.tool,
                                      status="ready" if outcome == "applied"
                                      else "pending_approval", ts=now)
                        note = NOTICE_INTENTS.get(rec.tool)
                        notice = str(rec.payload.get(note[1], "")) if note else ""
                        if note and notice and outcome in ("applied", "queued_for_approval"):
                            self._out(conn, tenant, thread, turn_id,
                                      key=f"{rec.idempotency_key}:notice", channel=note[0],
                                      shop_id=rec.shop_id, text=notice, kind=f"{rec.tool}_notice",
                                      status="ready", ts=now)
                            noticed.add(" ".join(notice.split()))
                    superseded = self._supersede(conn, tenant, r.intents, queued, sv)
                    if superseded:
                        result["superseded"] = superseded
                    by_key = {i.idempotency_key: i for i in r.intents}
                    for n, dr in enumerate(r.outbound_drafts):
                        if dr.idempotency_key in dropped:
                            continue
                        if dr.channel == "shop_whatsapp" and " ".join(dr.body.split()) in noticed:
                            continue  # the notice above already says it, once
                        drec = by_key.get(dr.idempotency_key)
                        outcome, _ = (self.go.apply_intent(
                            conn, tenant, drec.model_dump(mode="json"), turn_id=turn_id,
                            thread_id=thread, helper=d.helper, skill=d.skill)
                            if drec is not None else ("applied", None))
                        if outcome.endswith("refused"):
                            continue
                        self._out(conn, tenant, thread, turn_id, key=dr.idempotency_key,
                                  channel=dr.channel, shop_id=dr.shop_id, text=dr.body,
                                  kind=dr.kind,
                                  status="pending_approval" if dr.tier == "approve"
                                  else "ready", ts=now + n * 1e-3)
                if event is not None:
                    append_history(conn, tenant, thread, turn_id,
                                   state.messages[state.prefix_len + state.history_len:])
                asked_at = lead.received_at.isoformat()
                if r.status == "asked_question" and sender == "shop":
                    next_state = ThreadState(pending_skill=d.skill, last_skill=d.skill,
                                             asked_at=asked_at)
                if apply and r.status != "failed":
                    for rec in r.intents:
                        if rec.tool in AWAITS:
                            skill_key, expects = AWAITS[rec.tool]
                            ref = next((str(rec.payload[f]) for f in ASK_REF_FIELDS
                                        if rec.payload.get(f)), "")
                            next_state = ThreadState(pending_skill=skill_key,
                                                     expects=expects,  # type: ignore[arg-type]
                                                     last_skill=d.skill, asked_at=asked_at,
                                                     ask_ref=ref)
                    if d.skill == "mimi/escalate-owner" and sender == "shop":
                        flagged = self._flag_escalated_delivery(
                            conn, tenant, thread, turn_id, prior, events, d, r.intents)
                        if flagged:
                            result["delivery_flagged"] = flagged
            elif state is not None:
                run_id = state.run_id
                result["run_status"] = status
            # An open ask the turn did not answer stays open (until it expires):
            # an unrelated question in between must not drop the shop's receipt.
            proposed = {i.tool for i in state.finished.intents} if (
                state is not None and state.finished is not None) else set()
            if (prior.pending_skill and not next_state.pending_skill
                    and not _settles(prior.pending_skill, d.skill, proposed,
                                     bool(result.get("delivery_flagged")))
                    and ask_is_live(prior.pending_skill, prior.asked_at, lead.received_at)):
                next_state = replace(next_state, pending_skill=prior.pending_skill,
                                     expects=prior.expects, asked_at=prior.asked_at,
                                     ask_ref=prior.ask_ref)
            if sv.get("fix_keep") is not None and not next_state.fix:
                next_state = replace(next_state, fix=str(sv["fix_keep"]))
            elif prior.fix and not sv.get("fix_done") and not next_state.fix:
                next_state = replace(next_state, fix=prior.fix)
            fus = self._followups(conn, claim, sv, events) if status not in (
                "stopped_by_owner", "cancelled_by_sender") else []
            if sv.get("plan") or fus or claim.checkpoint.get("inbox") or sv.get("which"):
                result["steer"] = {
                    "which": sv.get("which"),
                    "layer": sv.get("layer", ""), "plan": sv.get("plan", {}),
                    "labels": sv.get("labels", []), "followups": fus,
                    "code": code.get("kind", ""), "correction": sv.get("correction"),
                    "cost_kobo": int(sv.get("cost_kobo") or 0),
                    "inbox": claim.checkpoint.get("inbox", [])}
            conn.execute(s.threads.update().where(
                s.threads.c.tenant_id == tenant, s.threads.c.thread_id == thread)
                .values(state=dumps(_state_to_json(next_state)), last_at=now_iso(now),
                        last_ts=now))
            # The phone number was only needed to route; drop it from the queue.
            for e in all_events:
                if e.payload.from_phone:
                    scrubbed = e.model_copy(update={"payload": e.payload.model_copy(
                        update={"from_phone": ""})})
                    conn.execute(s.inbound_messages.update().where(
                        s.inbound_messages.c.tenant_id == tenant,
                        s.inbound_messages.c.external_id == e.external_id)
                        .values(envelope=scrubbed.model_dump_json()))
            self._in_txn("finish", claim)
            self.queue.finish_in(conn, claim, status=status, helper=d.helper, skill=d.skill,
                                 run_id=run_id, result=result)
        # After the commit: a turn that lost its lease above never gets here,
        # so each job is traced once, as one trace (tracing never raises).
        if getattr(self.exporter, "enabled", False):
            trace_job(d, state.finished if state is not None else None, self.exporter,
                      job_id=turn_id, status=status, job=self._job_story(claim),
                      shop_id=thread_kind(tenant, thread)[1] or "owner")

    def _voice_choice(self, holder: list[Claim], lead: Inbound,
                      cmd: tuple[bool, str | None]) -> None:
        """VOICE ON / VOICE OFF from the shop itself: code records it and answers."""
        claim = holder[0]
        tenant, thread, turn_id = claim.tenant_id, claim.thread_id, claim.turn_id
        shop_id = thread_kind(tenant, thread)[1] or lead.payload.shop_id
        now = time.time()
        with self.engine.begin() as conn:
            pref = vx.record_choice(
                conn, shop_id=shop_id, on=cmd[0], language=cmd[1],
                message_id=lead.external_id, text=lead.payload.text.strip(),
                sender=f"shop_whatsapp:{shop_id}", at=lead.received_at.isoformat())
            self._out(conn, tenant, thread, turn_id, key=f"voice:{turn_id}",
                      channel="shop_whatsapp", shop_id=shop_id, text=vx.choice_text(pref),
                      kind="voice_setting", status="ready", ts=now)
            if lead.payload.from_phone:
                scrubbed = lead.model_copy(update={"payload": lead.payload.model_copy(
                    update={"from_phone": ""})})
                conn.execute(s.inbound_messages.update().where(
                    s.inbound_messages.c.tenant_id == tenant,
                    s.inbound_messages.c.external_id == lead.external_id)
                    .values(envelope=scrubbed.model_dump_json()))
            status = "voice_on" if pref.voice_replies else "voice_off"
            self.queue.finish_in(conn, claim, status=status, helper="mimi",
                                 skill="mimi/voice-setting", run_id="", result={
                                     "route": {"helper": "mimi", "skill": "mimi/voice-setting",
                                               "layer": "rule", "rule": "voice_command",
                                               "model_called": False, "cost_kobo": 0},
                                     "voice": {"on": pref.voice_replies,
                                               "language": pref.language}})

    def voice_audio(self, tenant_id: str, message_id: str, speaker: Any) -> tuple[Any, dict]:
        """(SpokenAudio, the voice fields) for one outbound message, made now
        and never stored. ``NotFound`` when the message has no voice reply."""
        from mimi.voice.speak import SpeechError

        with self.engine.connect() as conn:
            m = conn.execute(sa.select(s.messages).where(
                s.messages.c.tenant_id == tenant_id,
                s.messages.c.message_id == message_id)).first()
            voice = vx.reply_voice(conn, tenant_id, m) if m is not None else None
        if voice is None:
            raise NotFound(message_id)
        try:
            audio = speaker.speak(voice["script"], voice["voice"])
        except SpeechError as exc:
            raise NotFound(f"{message_id}: {exc}") from exc
        return audio, voice

    def _job_story(self, claim: Claim) -> dict[str, Any]:
        """The job's state changes for its trace: each claim (and takeover),
        a park on the daily cap and its resume, then done."""
        with self.engine.connect() as conn:
            att = conn.execute(sa.select(s.turn_attempts.c.worker_id,
                                         s.turn_attempts.c.takeover,
                                         s.turn_attempts.c.claimed_at).where(
                s.turn_attempts.c.turn_id == claim.turn_id)
                .order_by(s.turn_attempts.c.attempt_id)).all()
            parks = conn.execute(sa.select(sa.func.count()).select_from(s.thread_events).where(
                s.thread_events.c.turn_id == claim.turn_id,
                s.thread_events.c.type == "parked")).scalar() or 0
        seq = ["taken_over" if a.takeover else "leased" for a in att]
        resumed = int(claim.checkpoint.get("resumed") or 0)
        if parks and resumed and "taken_over" in seq:
            # A resume is a takeover of the parked job: the park sits before it.
            seq.insert(max(i for i, x in enumerate(seq) if x == "taken_over"), "parked_budget")
        states = ["queued", *seq, "done"]
        return {"turn_id": claim.turn_id, "attempts": len(att),
                "takeovers": sum(bool(a.takeover) for a in att),
                "workers": [a.worker_id for a in att], "parked": int(parks),
                "resumed": resumed, "states": states}

    def _followups(self, conn: sa.Connection, claim: Claim, sv: dict[str, Any],
                   events: Sequence[Inbound]) -> list[str]:
        """Each separate part of the message as its own turn, right after this one."""
        rows = sv.get("followups") or []
        if not rows:
            return []
        parts = sx.parts_from(sv.get("parts") or [])
        lead = lead_of(events)
        t = conn.execute(sa.select(s.turns.c.created_ts).where(
            s.turns.c.turn_id == claim.turn_id)).scalar() or time.time()
        shop_id = thread_kind(claim.tenant_id, claim.thread_id)[1]
        out = []
        for n, f in enumerate(rows):
            src = next((p.external_id for p in parts if p.pid in set(f["pids"])),
                       lead.external_id)
            parent = next((e for e in events if e.external_id == src), lead)
            if not parent.payload.from_phone and lead.payload.from_phone:
                parent = parent.model_copy(update={"payload": parent.payload.model_copy(
                    update={"from_phone": lead.payload.from_phone})})
            fu = sx.followup_inbound(parent, events, parts, f["pids"], str(f.get("steer", "new")))
            out.append(sx.insert_followup(
                conn, claim.tenant_id, claim.thread_id, fu, after_ts=float(t), order=n,
                shop_id=shop_id, simulated=False,
                media_ref=sx.media_ref_of(conn, claim.tenant_id, src)))
        return out

    def _supersede(self, conn: sa.Connection, tenant: str, intents: Sequence[Any],
                   queued: dict[str, str], sv: dict[str, Any]) -> list[list[str]]:
        """A new version replaces the pending one it corrects (never a decided one)."""
        corr = sv.get("correction") or {}
        task = corr.get("task") or {}
        done: list[list[str]] = []
        for rec in intents:
            new_id = queued.get(rec.idempotency_key)
            if not new_id:
                continue
            family = sx.FAMILY.get(rec.tool)
            corrects = (corr.get("action") == "revise" and task.get("tool") in (family or ())
                        and task.get("shop_id") == rec.shop_id)
            if family is None or not (rec.tool == "revise_restock" or corrects):
                continue
            sku = str(rec.payload.get("sku", ""))
            for old in sx.pending_of_family(conn, tenant, rec.shop_id, rec.tool, sku):
                if old.approval_id != new_id and sx.supersede(conn, tenant, old.approval_id,
                                                              new_id):
                    done.append([old.approval_id, new_id])
        return done

    def _code_action(self, conn: sa.Connection, tenant: str, thread: str, turn_id: str,
                     code: dict[str, Any], lead: Inbound, result: dict[str, Any],
                     now: float) -> None:
        """A reply written by code: a question, a withdrawal, the owner told."""
        kind_, shop_id = thread_kind(tenant, thread)
        channel = _CHANNEL_FOR.get(lead.sender, "owner_console")
        words = str(code.get("words", ""))
        task = sx.task_from(code["task"]) if code.get("task") else None
        kind = str(code["kind"])
        if kind == "withdraw" and task is not None:
            if sx.withdraw(conn, tenant, task.approval_id, by=lead.external_id):
                self._owner_note(conn, tenant, thread, turn_id, shop_id, task, words,
                                 "withdraw", now)
                text = st.withdrawn_text(words)
                result["withdrawn"] = task.approval_id
            else:
                kind = "owner"  # decided in the meantime: never touched
        if kind == "owner" and task is not None:
            self._owner_note(conn, tenant, thread, turn_id, shop_id, task, words, "owner", now)
            text = st.to_owner_text(sx.tenant_name(conn, tenant), words)
        elif kind == "question":
            text = str(code.get("text", ""))
        elif kind == "left":
            text = str(code.get("text", ""))
        elif kind == "which":
            text = str(code.get("text", ""))
        elif kind == "which_owner":
            self._which_note(conn, tenant, thread, turn_id, shop_id, code, now)
            text = st.which_owner_text(sx.tenant_name(conn, tenant), words)
        elif kind != "withdraw":
            text = st.left_text(words)
        result["steer_action"] = kind
        self._out(conn, tenant, thread, turn_id, key=f"steer:{turn_id}", channel=channel,
                  shop_id=shop_id, text=text, kind=f"steer_{kind}", status="ready", ts=now)

    def _owner_note(self, conn: sa.Connection, tenant: str, thread: str, turn_id: str,
                    shop_id: str, task: st.OpenTask, words: str, kind: str,
                    now: float) -> None:
        note = st.owner_note(shop_id or "a shop", task, words, kind=kind)
        payload = {"message": note, "shop_id": shop_id, "about": task.key}
        record = {
            "tool": "notify_owner", "intent_kind": "notify_owner",
            "tier": self.policy.tier(tenant, "notify_owner", payload), "scope": "org:plan",
            "tenant_id": tenant, "shop_id": shop_id,
            "idempotency_key": business_key(tenant, "notify_owner",
                                            {"shop_id": shop_id, "about": task.key,
                                             "kind": kind, "turn": turn_id}),
            "attempt_key": f"steer:{turn_id}", "payload": payload,
            "preview": f"Would tell the owner: {note[:120]}"}
        outcome, _ = self.go.apply_intent(conn, tenant, record, turn_id=turn_id,
                                          thread_id=thread, helper="mimi", skill="mimi/steer")
        if outcome == "applied":
            self._out(conn, tenant, thread, turn_id, key=str(record["idempotency_key"]),
                      channel="owner_console", shop_id=shop_id, text=note, kind="notify_owner",
                      status="ready", ts=now + 1e-3)

    def _which_note(self, conn: sa.Connection, tenant: str, thread: str, turn_id: str,
                    shop_id: str, code: dict[str, Any], now: float) -> None:
        """The owner checks a reply that fits two open asks (never bound by a guess)."""
        task = code.get("task") or {}
        note = st.which_owner_note(shop_id or "a shop", str(code.get("delivery_id", "")),
                                   str(task.get("summary", "")), str(code.get("words", "")))
        payload = {"message": note, "shop_id": shop_id, "about": "two_open_asks",
                   "delivery_id": str(code.get("delivery_id", ""))}
        record = {
            "tool": "notify_owner", "intent_kind": "notify_owner",
            "tier": self.policy.tier(tenant, "notify_owner", payload), "scope": "org:plan",
            "tenant_id": tenant, "shop_id": shop_id,
            "idempotency_key": business_key(tenant, "notify_owner",
                                            {"shop_id": shop_id, "about": "two_open_asks",
                                             "turn": turn_id}),
            "attempt_key": f"which:{turn_id}", "payload": payload,
            "preview": f"Would tell the owner: {note[:120]}"}
        outcome, _ = self.go.apply_intent(conn, tenant, record, turn_id=turn_id,
                                          thread_id=thread, helper="mimi", skill="mimi/steer")
        if outcome == "applied":
            self._out(conn, tenant, thread, turn_id, key=str(record["idempotency_key"]),
                      channel="owner_console", shop_id=shop_id, text=note, kind="notify_owner",
                      status="ready", ts=now + 1e-3)

    def _flag_escalated_delivery(self, conn: sa.Connection, tenant: str, thread: str,
                                 turn_id: str, prior: ThreadState, events: Sequence[Inbound],
                                 d: RouteDecision, intents: Sequence[Any]) -> str:
        """Belt and braces: escalate-owner fired on a shop message tied to a
        delivery (its receipt ask was open, or the shop named the delivery), so
        the flag is also recorded on that delivery, written by code.

        Only a ``flag_delivery_to_owner`` intent (notify tier, the same
        business key Order's own flag has, so it is never recorded twice);
        never consent, never a money move. Returns the delivery id flagged.
        """
        kind, shop_id = thread_kind(tenant, thread)
        if kind != "shop" or not shop_id:
            return ""
        if any(getattr(i, "tool", "") == "flag_delivery_to_owner" for i in intents):
            return ""
        lead = lead_of(events)
        words = " / ".join(e.text for e in events if e.text)[:600]
        cands: list[str] = []
        if (prior.pending_skill == "order/drop-confirmation" and prior.ask_ref
                and ask_is_live(prior.pending_skill, prior.asked_at, lead.received_at)):
            cands.append(prior.ask_ref)
        cands += [x for x in delivery_ids_in(words) if x not in cands]
        for dlv in cands:
            known = conn.execute(sa.select(s.deliveries.c.delivery_id).where(
                s.deliveries.c.tenant_id == tenant, s.deliveries.c.shop_id == shop_id,
                s.deliveries.c.delivery_id == dlv)).first()
            if known is None:
                continue
            done = [loads(r.payload, {}).get("delivery_id") for r in conn.execute(
                sa.select(s.intents.c.payload).where(
                    s.intents.c.tenant_id == tenant, s.intents.c.shop_id == shop_id,
                    s.intents.c.tool.in_(RECEIPT_SETTLED),
                    s.intents.c.outcome != "refused"))]
            if dlv in done:
                continue
            payload = {"shop_id": shop_id, "delivery_id": dlv, "reason": "escalated",
                       "shop_words": words, "disputed": [],
                       "message": (f"{shop_id} about delivery {dlv}: sent to the owner as a "
                                   f"complaint while the receipt was open. The shop said: "
                                   f"\"{words}\"")}
            record = {
                "tool": "flag_delivery_to_owner", "intent_kind": "flag_delivery_to_owner",
                "tier": "notify", "scope": "shop:invoices", "tenant_id": tenant,
                "shop_id": shop_id,
                "idempotency_key": business_key(tenant, "flag_delivery_to_owner",
                                                {"shop_id": shop_id, "delivery_id": dlv}),
                "attempt_key": f"esc:{turn_id}:{dlv}", "payload": payload,
                "preview": f"Would tell the owner about {dlv} (escalated)"}
            outcome, _ = self.go.apply_intent(conn, tenant, record, turn_id=turn_id,
                                              thread_id=thread, helper=d.helper, skill=d.skill)
            if outcome == "applied":
                return dlv
        return ""

    @staticmethod
    def _shop_of(*, directory: Directory | None, tenant: str, thread: str) -> str:
        return thread_kind(tenant, thread)[1]

    @staticmethod
    def _out(conn: sa.Connection, tenant: str, thread: str, turn_id: str, *, key: str,
             channel: str, shop_id: str, text: str, kind: str, status: str,
             ts: float) -> None:
        insert_ignore(conn, s.messages, {
            "tenant_id": tenant, "message_id": f"out:{key}", "thread_id": thread,
            "turn_id": turn_id, "direction": "out", "sender": "mimi", "channel": channel,
            "shop_id": shop_id, "text": text, "kind": kind, "status": status,
            "idempotency_key": key, "simulated": False, "at": now_iso(ts), "ts": ts})

    # ============================================================ approvals

    def decide(self, tenant_id: str, approval_id: str, *, approve: bool, by: str,
               note: str = "") -> tuple[dict[str, Any], TurnView | None]:
        """The owner approves or rejects one pending approval.

        Approving releases exactly what the helper drafted (its message goes
        to ``ready``; its intent is marked approved) and wakes the helper on
        the thread through the front door (``approval_answered``), so it
        carries on with its own tools. Nothing here moves money.
        """
        decision = "approved" if approve else "rejected"
        now = time.time()
        with self.engine.begin() as conn:
            a = conn.execute(sa.select(s.approvals).where(
                s.approvals.c.tenant_id == tenant_id,
                s.approvals.c.approval_id == approval_id)).first()
            if a is None:
                raise NotFound(approval_id)
            if a.status != "pending":
                raise Conflict(f"approval is already {a.status}")
            if approve and moves_money(a.tool, a.intent_kind):
                raise Conflict("Mimi never moves money; this cannot be approved here")
            res = conn.execute(s.approvals.update().where(
                s.approvals.c.approval_id == approval_id, s.approvals.c.status == "pending")
                .values(status=decision, decided_by=by[:100], note=note[:2000],
                        decided_at=now_iso(now)))
            if res.rowcount != 1:
                raise Conflict("approval was decided at the same time")
            conn.execute(s.intents.update().where(
                s.intents.c.tenant_id == tenant_id,
                s.intents.c.idempotency_key == a.idempotency_key).values(outcome=decision))
            conn.execute(s.messages.update().where(
                s.messages.c.tenant_id == tenant_id,
                s.messages.c.idempotency_key == a.idempotency_key,
                s.messages.c.status == "pending_approval")
                .values(status="ready" if approve else "rejected"))
            conn.execute(s.credit_requests.update().where(
                s.credit_requests.c.tenant_id == tenant_id,
                s.credit_requests.c.approval_id == approval_id).values(status=decision))
            resumed: list[str] = []
            if a.tool == "raise_daily_allowance" and approve:
                # The owner's figure is the one code proposed; the cap only goes up.
                new_cap = int(loads(a.payload, {}).get("new_cap_kobo") or 0)
                conn.execute(s.tenants.update().where(
                    s.tenants.c.tenant_id == tenant_id,
                    s.tenants.c.daily_cap_kobo < new_cap).values(daily_cap_kobo=new_cap))
                resumed = self.resume_parked(conn, tenant_id)
        row = self.approval(tenant_id, approval_id)
        if resumed:
            log.info("owner raised the daily allowance; resumed %d parked job(s)", len(resumed))
        follow: TurnView | None = None
        if a.helper and a.thread_id:
            inbound = Inbound(
                source="approval_answered", org_id=tenant_id,
                external_id=f"approval:{approval_id}",
                received_at=self.clock(), thread_id=a.thread_id,
                payload=Payload(name=approval_id, for_helper=a.helper, for_skill=a.skill,
                                shop_id=a.shop_id, answer=decision,
                                # Written by code, so the woken helper knows what
                                # was decided (no figures, no shop text).
                                text=f"The owner {decision} approvals item {approval_id} "
                                     f"({a.tool} for {a.shop_id or 'the distributor'})."))
            follow = self.submit(tenant_id, inbound)
        return row, follow

    def approval(self, tenant_id: str, approval_id: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            a = conn.execute(sa.select(s.approvals).where(
                s.approvals.c.tenant_id == tenant_id,
                s.approvals.c.approval_id == approval_id)).first()
            if a is None:
                raise NotFound(approval_id)
            draft = conn.execute(sa.select(s.messages.c.text, s.messages.c.channel,
                                           s.messages.c.status).where(
                s.messages.c.tenant_id == tenant_id,
                s.messages.c.idempotency_key == a.idempotency_key)).first()
        return approval_dict(a, draft)


def approval_dict(a: Any, draft: Any | None) -> dict[str, Any]:
    return {
        "approval_id": a.approval_id, "status": a.status, "helper": a.helper,
        "skill": a.skill, "shop_id": a.shop_id, "thread_id": a.thread_id,
        "turn_id": a.turn_id, "tool": a.tool, "intent_kind": a.intent_kind,
        "preview": a.preview, "payload": loads(a.payload, {}),
        "draft": ({"text": draft.text, "channel": draft.channel, "status": draft.status}
                  if draft is not None else None),
        "created_at": a.created_at, "decided_at": a.decided_at, "decided_by": a.decided_by,
        "note": a.note, "supersedes": getattr(a, "supersedes", None),
        "superseded_by": getattr(a, "superseded_by", None),
    }

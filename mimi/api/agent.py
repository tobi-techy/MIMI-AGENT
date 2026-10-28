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

import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.harness import fastclass
from mimi.harness.context import ContextBuilder
from mimi.harness.frontdoor import FrontDoor
from mimi.harness.gateway import Gateway, ToolModel, kobo_cost
from mimi.harness.inbound import Directory, Inbound, Payload
from mimi.harness.loop import Harness, Helper, LoopState
from mimi.harness.policy import GoPolicy, RiskPolicy
from mimi.harness.router import RouteDecision, Router, ThreadState, lead_of
from mimi.harness.types import RunEvent
from mimi.models.base import ChatModel
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
}


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
        return self.state in ("done", "failed")

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
        policy: RiskPolicy | None = None,
        lease_s: float = 30.0,
        worker_id: str = "",
        clock: Any = None,
    ) -> None:
        from mimi.api.helpers import build_helpers

        self.engine = engine
        self.go = SqlGo(engine)
        self.policy = policy or GoPolicy()
        self.helpers: dict[str, Helper] = dict(helpers or build_helpers(self.policy))
        self.router = Router(model=router_model, fast=fastclass.model(), price=kobo_cost)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.artifacts = SqlArtifactStore(engine)
        self.harness = Harness(book=self.go, control=self.go, events=self.go,
                               gateway=Gateway(list(models)), policy=self.policy,
                               clock=self.clock, artifacts=self.artifacts)
        self.builder = ContextBuilder(book=self.go, prefixes=SqlPrefixStore(engine),
                                      history=SqlHistoryStore(engine),
                                      artifacts=self.artifacts, clock=self.clock)
        self.lease_s = lease_s
        self.queue = TurnQueue(engine, lease_s=lease_s)
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.model_mode = "offline" if all(getattr(m, "name", "") == "offline"
                                           for m in models) else "live"

    @classmethod
    def from_env(cls, engine: Engine, *, env: Mapping[str, str] | None = None,
                 lease_s: float = 30.0) -> Agent:
        e = env if env is not None else os.environ
        key = e.get("META_API_KEY", "").strip()
        if key:
            from mimi.harness.gateway import OpenAIToolModel
            from mimi.models.env import from_env

            tool = OpenAIToolModel(
                api_key=key, model=e.get("MIMI_MODEL", "").strip() or "muse-spark-1.3",
                base_url=e.get("META_API_BASE", "").strip() or "https://api.meta.ai/v1",
                timeout_s=float(e.get("META_TIMEOUT_S", "30") or 30))
            return cls(engine, models=[tool], router_model=from_env("mimi", env=e),
                       lease_s=lease_s)
        from mimi.api.offline import OfflineModel

        return cls(engine, models=[OfflineModel()], router_model=None, lease_s=lease_s)

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
            replies = [{
                "message_id": m.message_id, "channel": m.channel, "shop_id": m.shop_id,
                "text": m.text, "kind": m.kind, "status": m.status, "at": m.at}
                for m in conn.execute(sa.select(s.messages).where(
                    s.messages.c.tenant_id == tenant_id, s.messages.c.turn_id == lead.turn_id,
                    s.messages.c.direction == "out").order_by(s.messages.c.ts,
                                                              s.messages.c.message_id))]
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
        directory = self.go.directory(claim.tenant_id)
        prior = self._thread_state(claim.tenant_id, claim.thread_id)
        if "decision" in ck:
            d = RouteDecision.model_validate(ck["decision"])
        else:
            d = self.router.route_batch(events, directory, prior)
            ck["decision"] = d.model_dump(mode="json")
            self._checkpoint(holder, ck)
        if d.status == "clarify":
            self._complete(holder, d, events, prior, status="asked_to_clarify")
            return
        helper = self.helpers.get(d.helper)
        if helper is None:
            self._complete(holder, d, events, prior, status=f"no_helper_yet:{d.helper}")
            return
        if "loop" in ck:
            state = LoopState.model_validate(ck["loop"])
            event = RunEvent.model_validate(ck["event"])
        else:
            event, job, route = self._run_event(claim, events, directory, d)
            ctx = self.builder.build(event, helper, d.skill, job=job)
            state = self.harness.start(event, helper, context=ctx, route=route)
            ck.update(event=event.model_dump(mode="json"), loop=state.model_dump(mode="json"))
            self._checkpoint(holder, ck)
        while state.finished is None:
            if self._stop_requested(claim.turn_id):
                self._complete(holder, d, events, prior, status="stopped_by_owner",
                               state=state, event=event, apply=False)
                return
            res = self.harness.step(state, helper)
            state = LoopState.model_validate(res.state)
            ck["loop"] = res.state
            self._checkpoint(holder, ck)
        self._complete(holder, d, events, prior, status=state.finished.status,
                       state=state, event=event)

    def _run_event(self, claim: Claim, events: Sequence[Inbound], directory: Directory,
                   d: RouteDecision) -> tuple[RunEvent, str, dict[str, Any]]:
        """The harness's wake event for a routed batch (as ``wiring.TurnRunner`` builds it)."""
        lead = lead_of(events)
        shop_id = ""
        if lead.source in ("shop_message", "rep_message"):
            shop_id = directory.shop_for(lead.payload.from_phone)
        shop_id = shop_id or lead.payload.shop_id
        text = "\n".join(e.text for e in events if e.text)
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
                  event: RunEvent | None = None, apply: bool = True) -> None:
        claim = holder[0]
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
                    for rec in r.intents:
                        if rec.tool in ("send_to_user", "ask_question"):
                            continue  # recorded below with their draft
                        outcome, _ = self.go.apply_intent(
                            conn, tenant, rec.model_dump(mode="json"), turn_id=turn_id,
                            thread_id=thread, helper=d.helper, skill=d.skill)
                        msg = MESSAGE_INTENTS.get(rec.tool)
                        text = str(rec.payload.get(msg[1], "")) if msg else ""
                        if msg and text and outcome in ("applied", "queued_for_approval"):
                            # A message Go would send for this intent (code wrote the text).
                            self._out(conn, tenant, thread, turn_id, key=rec.idempotency_key,
                                      channel=msg[0], shop_id=rec.shop_id, text=text,
                                      kind=rec.tool,
                                      status="ready" if outcome == "applied"
                                      else "pending_approval", ts=now)
                    by_key = {i.idempotency_key: i for i in r.intents}
                    for n, dr in enumerate(r.outbound_drafts):
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
                if r.status == "asked_question" and sender == "shop":
                    next_state = ThreadState(pending_skill=d.skill, last_skill=d.skill)
            elif state is not None:
                run_id = state.run_id
                result["run_status"] = status
            conn.execute(s.threads.update().where(
                s.threads.c.tenant_id == tenant, s.threads.c.thread_id == thread)
                .values(state=dumps(_state_to_json(next_state)), last_at=now_iso(now),
                        last_ts=now))
            # The phone number was only needed to route; drop it from the queue.
            for e in events:
                if e.payload.from_phone:
                    scrubbed = e.model_copy(update={"payload": e.payload.model_copy(
                        update={"from_phone": ""})})
                    conn.execute(s.inbound_messages.update().where(
                        s.inbound_messages.c.tenant_id == tenant,
                        s.inbound_messages.c.external_id == e.external_id)
                        .values(envelope=scrubbed.model_dump_json()))
            self.queue.finish_in(conn, claim, status=status, helper=d.helper, skill=d.skill,
                                 run_id=run_id, result=result)

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
        row = self.approval(tenant_id, approval_id)
        follow: TurnView | None = None
        if a.helper and a.thread_id:
            inbound = Inbound(
                source="approval_answered", org_id=tenant_id,
                external_id=f"approval:{approval_id}",
                received_at=datetime.fromtimestamp(now, UTC), thread_id=a.thread_id,
                payload=Payload(name=approval_id, for_helper=a.helper, for_skill=a.skill,
                                shop_id=a.shop_id, answer=decision))
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
        "note": a.note,
    }

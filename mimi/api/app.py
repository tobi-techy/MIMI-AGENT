"""The HTTP API (RAI-266): the live agent for the console and the WhatsApp webhook.

    uvicorn --factory mimi.api.app:create_app        # or: python -m mimi.api serve

Endpoints (all under ``/v1``, all but health need ``Authorization: Bearer``):

* ``POST /v1/turns``: an inbound message, through the front door, router and
  harness. Idempotent on ``message_id``. Waits up to ``wait_s`` for the
  replies (200), else answers 202 with the turn id to poll.
* ``GET /v1/turns/{turn_id}``: a turn's state and replies.
* ``GET /v1/threads``, ``GET /v1/threads/{thread_id}``: messages and steps.
* ``GET /v1/shops``, ``GET /v1/shops/{shop_id}``.
* ``GET /v1/approvals``; ``POST /v1/approvals/{id}/approve`` and ``/reject``
  (owner key only).
* ``GET /v1/credit``: limits and credit requests.
* ``GET /v1/policy``, ``PUT /v1/policy``: the owner's rulebook (RAI-323);
  built-in stops are listed locked. Only an owner key changes it; a demo
  session, or demo mode, gets 403.
* ``POST /v1/computer/tasks``, ``GET /v1/computer/tasks``,
  ``GET /v1/computer/tasks/{id}``, ``POST /v1/computer/tasks/{id}/approve``
  and ``/stop``, ``GET /v1/computer/tasks/{id}/artifacts/{name}`` and
  ``GET /v1/computer/kinds``: a task
  for the browser container. Code builds the plan, the owner approves it once,
  and the guardrails (domain allowlist, no credentials, no purchase or
  messaging, budgets, immediate stop) are enforced in code (``mimi.computer``).
  A running task carries a signed ``live_view_url`` to watch its session.
* ``GET /v1/health``; ``GET /`` (liveness only: no database, no auth).
* ``GET /v1/me``: the caller's tenant, role and mode.
* ``POST /v1/demo/sessions``, ``POST /v1/demo/reset``: demo mode only
  (``MIMI_MODE=demo``, ``mimi.api.demo``); 404 in live mode.
* ``POST /v1/media``, ``GET /v1/messages/{message_id}/media``: demo mode only:
  the console's attachments (``mimi.api.uploads``); 404 in live mode, where
  the channel keeps media.

Each key belongs to one distributor; every query is filtered by it, so a
key can never read or change another distributor's rows. This module is the
only place in ``mimi`` that imports the web framework.
"""

# No ``from __future__ import annotations``: FastAPI reads the dependency
# aliases defined inside create_app at runtime.

import logging
import tempfile
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mimi.api import connectors as cx
from mimi.api import demo_wiring as dw
from mimi.api import reads, sales
from mimi.api.agent import Agent, Conflict, NotFound
from mimi.api.config import LOCALHOST_REGEX, ApiKey, Settings
from mimi.api.demo import (
    EXPIRED,
    DemoRefused,
    DemoStore,
    SessionExpired,
    client_address,
    client_ip,
    refuses_approval,
)
from mimi.api.media import MAX_BYTES
from mimi.api.schemas import (
    ActivityList,
    ApprovalList,
    ComputerKindList,
    ComputerProposal,
    ComputerTaskDetail,
    ComputerTaskIn,
    ComputerTaskList,
    ConnectorConnectIn,
    ConnectorConnectOut,
    ConnectorDetail,
    ConnectorList,
    CreditLimitIn,
    CreditLimitOut,
    CreditOut,
    DecisionIn,
    DecisionOut,
    DemoLimitError,
    DemoSessionOut,
    ErrorOut,
    EventIn,
    Health,
    Liveness,
    MediaOut,
    MentionList,
    MeOut,
    PolicyIn,
    PolicyOut,
    SellThroughOut,
    ShopDetail,
    ShopList,
    SlowCheckOut,
    StopOut,
    TaskList,
    ThreadDetail,
    ThreadList,
    TurnIn,
    TurnOut,
)
from mimi.api.uploads import ConsoleUploads, UploadRefused
from mimi.clock import wall, wall_utc
from mimi.computer.service import ComputerService, Refused
from mimi.computer.store import ComputerStore
from mimi.connectors.base import ConnectorError
from mimi.harness.go import CrossTenant
from mimi.harness.inbound import Inbound, Media, Payload
from mimi.harness.policy import PolicyLocked, owner_rules_on
from mimi.store import schema as s
from mimi.store.db import make_engine
from mimi.store.policy_rules import SqlPolicyRules
from mimi.store.util import now_iso
from mimi.tz import assume_lagos

log = logging.getLogger(__name__)
VERSION = "0.1.0"
_bearer = HTTPBearer(auto_error=False)
_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorOut}, 403: {"model": ErrorOut}, 404: {"model": ErrorOut},
    409: {"model": ErrorOut}}
_DEMO_ERRORS: dict[int | str, dict[str, Any]] = {
    **_ERRORS, 429: {"model": DemoLimitError}, 503: {"model": DemoLimitError}}


def create_app(settings: Settings | None = None, agent: Agent | None = None, *,
               workers: int | None = None,
               computer: ComputerService | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.check()
    if agent is None:
        agent = Agent.from_env(make_engine(settings.database_url), lease_s=settings.lease_s)
    n_workers = settings.workers if workers is None else workers
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        threads = [threading.Thread(target=agent.run_worker, args=(stop,), daemon=True,
                                    name=f"mimi-worker-{i}") for i in range(n_workers)]
        for t in threads:
            t.start()
        try:
            yield
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=5)

    app = FastAPI(
        title="Mimi agent API", version=VERSION, lifespan=lifespan,
        description=("The live Mimi agent for the distributor console and the WhatsApp "
                     "webhook. Mimi proposes, code checks, the owner approves; nothing "
                     "moves money. Demo data is synthetic; model calls are real."))
    app.state.agent = agent
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins),
        allow_origin_regex=LOCALHOST_REGEX, allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"])
    engine = agent.engine
    uploads: ConsoleUploads | None = None
    if settings.demo_mode:
        uploads = ConsoleUploads(Path(settings.media_dir or tempfile.mkdtemp(prefix="mimi-media-")))
    demo = (DemoStore(engine, settings.demo, on_drop=uploads.drop if uploads else None)
            if settings.demo_mode else None)
    app.state.demo = demo
    app.state.uploads = uploads
    # Computer tasks: code-built plans run on a browser container under code
    # guardrails. With no container configured a task is still proposed and
    # approving it says so plainly, rather than pretending to run.
    if computer is None:
        computer = ComputerService(ComputerStore(engine))
    app.state.computer = computer

    def auth(creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
             ) -> ApiKey:
        token = creds.credentials if creds else ""
        key = settings.lookup(token)
        if key is None and demo is not None and token:
            try:
                sess = demo.lookup(token)
            except SessionExpired:
                raise HTTPException(401, EXPIRED,
                                    headers={"WWW-Authenticate": "Bearer"}) from None
            if sess is not None:
                key = ApiKey(sess.tenant_id, "owner", sess.digest, session=True)
        if key is None:
            raise HTTPException(401, "a valid bearer key is required",
                                headers={"WWW-Authenticate": "Bearer"})
        return key

    def visitor(request: Request, key: ApiKey | None = None) -> str:
        """The demo caller's address digest: the session's, else the request's."""
        assert demo is not None
        if key is not None and key.session:
            sess = demo.session(key.digest)
            if sess is not None:
                return sess.ip_hash
        peer = request.client.host if request.client else ""
        return demo.ip_hash(client_address(peer, request.headers, settings.demo))

    def ip_source(request: Request) -> str:
        peer = request.client.host if request.client else ""
        return client_ip(peer, request.headers, settings.demo)[1]

    def demo_gate(request: Request, key: ApiKey) -> None:
        """Demo caps and kill switch, checked before anything is queued."""
        if demo is not None:
            demo.gate(visitor(request, key))

    def reader(key: Annotated[ApiKey, Depends(auth)]) -> ApiKey:
        if key.role != "owner":
            raise HTTPException(403, "this key may only post turns")
        return key

    Owner = Annotated[ApiKey, Depends(reader)]
    Any_ = Annotated[ApiKey, Depends(auth)]

    @app.exception_handler(NotFound)
    async def _nf(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"detail": "not found"}, status_code=404)

    @app.exception_handler(CrossTenant)
    async def _ct(_: Request, exc: CrossTenant) -> JSONResponse:
        return JSONResponse({"detail": "not found"}, status_code=404)

    @app.exception_handler(DemoRefused)
    async def _dr(_: Request, exc: DemoRefused) -> JSONResponse:
        return JSONResponse(exc.payload(), status_code=exc.status)

    @app.exception_handler(UploadRefused)
    async def _ur(_: Request, exc: UploadRefused) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(Conflict)
    async def _cf(_: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    # ---------------------------------------------------------------- turns

    @app.post("/v1/turns", response_model=TurnOut, responses={202: {"model": TurnOut},
                                                               **_DEMO_ERRORS},
              summary="Send one inbound message to Mimi")
    def post_turn(body: TurnIn, key: Any_, response: Response, request: Request,
                  wait: Annotated[bool, Query(description="wait for the replies")] = True
                  ) -> dict[str, Any]:
        if body.tenant_id and body.tenant_id != key.tenant_id:
            raise HTTPException(403, "this key belongs to another distributor")
        demo_gate(request, key)
        media = Media()
        ref = ""
        if body.media is not None:
            m = body.media
            ref = m.ref
            # Demo: a turn may point at local bytes only through the caller's
            # own console upload (never another copy's, never the eval fixtures).
            if (uploads is not None and ref.startswith("local:")
                    and not uploads.owns(key.tenant_id, ref, m.sha256)):
                raise HTTPException(422, "attach the file again: it is not one of this "
                                         "demo copy's uploads")
            media = Media(kind=m.kind, mime=m.mime, sha256=m.sha256, width=m.width,
                          height=m.height, camera_exif=m.camera_exif, caption=m.caption,
                          transcript=m.transcript, transcript_lang=m.transcript_lang)
        if not (body.text.strip() or body.button_id or media.kind != "none"):
            raise HTTPException(422, "a turn needs text, a button or media")
        if body.assign_to and body.source != "owner_message":
            raise HTTPException(422, "only the owner can assign a helper")
        at = body.received_at or wall_utc()
        if at.tzinfo is None:
            # A channel time without a zone is a Lagos wall-clock time
            # (RAI-308): reading it as UTC put 23:30 on the next day.
            at = assume_lagos(at)
        inbound = Inbound(
            source=body.source, org_id=key.tenant_id, external_id=body.message_id,
            received_at=at,
            payload=Payload(text=body.text, from_phone=body.phone, media=media,
                            button_id=body.button_id, assign_to=body.assign_to))
        view = agent.submit(key.tenant_id, inbound, media_ref=ref, simulated=body.simulated)
        if wait and not view.finished:
            agent.process(view.turn_id)
            view = agent.wait(key.tenant_id, view.turn_id, settings.wait_s,
                              duplicate=view.duplicate)
        if not view.finished:
            response.status_code = 202
        return _turn_out(view)

    @app.post("/v1/events", response_model=TurnOut, status_code=202,
              responses=_DEMO_ERRORS,
              summary="Send one system event (a Go webhook or a routine) to Mimi")
    def post_event(body: EventIn, key: Any_, request: Request) -> dict[str, Any]:
        """Queues the event as a turn on the shop's thread (or the job thread).

        The words the helper sees are written here from the event's kind and
        shop, never taken from the caller: a system event carries no free text.
        """
        demo_gate(request, key)
        at = body.received_at or wall_utc()
        if at.tzinfo is None:
            at = assume_lagos(at)  # no zone: Africa/Lagos (RAI-308)
        if body.shop_id:
            key_shop = agent.go.shop(key.tenant_id, body.shop_id)  # 404 for another tenant's
            what = f"{body.name.replace('_', ' ').replace('.', ' ')} for {key_shop['shop_id']}"
        else:
            what = body.name.replace("_", " ").replace(".", " ")
        words = f"System event: {what}."
        if body.amount_ngn and body.name == "bank_credit":
            words += f" Amount {int(body.amount_ngn)} naira."  # an int, written by code
        inbound = Inbound(
            source=body.kind, org_id=key.tenant_id, external_id=f"{body.kind}:{body.event_id}",
            received_at=at, payload=Payload(text=words, name=body.name,
                                            shop_id=body.shop_id))
        view = agent.submit(key.tenant_id, inbound, simulated=body.simulated)
        return _turn_out(view)

    @app.get("/v1/turns/{turn_id}", response_model=TurnOut, responses=_ERRORS,
             summary="A turn's state and replies")
    def get_turn(turn_id: str, key: Any_) -> dict[str, Any]:
        return _turn_out(agent.view(key.tenant_id, turn_id))

    speaker_box: list[Any] = []

    @app.get("/v1/messages/{message_id}/voice", responses={
        **_ERRORS, 200: {"content": {"audio/ogg": {}, "audio/wav": {}},
                         "description": "The voice note for this reply"}},
             response_class=Response, summary="A reply's voice note (RAI-290)")
    def get_voice(message_id: str, key: Any_) -> Response:
        """The reply's text, read aloud: made now, never stored. 404 when the
        reply has no voice note (flag off, the shop did not opt in or did not
        send a voice note, or the code check on the figures failed). The text
        reply always goes too."""
        if not speaker_box:
            from mimi.voice.env import speaker_from_env

            speaker_box.append(speaker_from_env())
        audio, voice = agent.voice_audio(key.tenant_id, message_id, speaker_box[0])
        return Response(content=audio.data, media_type=f"audio/{audio.format}", headers={
            "X-Mimi-Voice": voice["voice"], "X-Mimi-Voice-Seconds": str(audio.duration_s),
            "Cache-Control": "no-store"})

    # ------------------------------------------------------- console uploads

    @app.post("/v1/media", response_model=MediaOut, status_code=201, responses={
        **_DEMO_ERRORS, 413: {"model": ErrorOut}, 415: {"model": ErrorOut}},
        openapi_extra={"requestBody": {"required": True, "content": {
            m: {"schema": {"type": "string", "format": "binary"}} for m in (
                "image/jpeg", "image/png", "image/webp", "audio/ogg", "audio/webm",
                "audio/mp4", "audio/mpeg", "audio/wav", "application/pdf")}}},
        summary="Demo mode: keep one console attachment for a turn")
    async def post_media(request: Request, key: Owner) -> dict[str, Any]:
        """The raw bytes of one photo, voice note or PDF (the request body, with
        its ``Content-Type``), at most 10 MB. Answers the ``ref`` and hash to send
        as the turn's ``media``. 404 in live mode: the channel keeps media."""
        if uploads is None:
            raise HTTPException(404, "not found")
        demo_gate(request, key)
        size = request.headers.get("content-length", "")
        if size.isdigit() and int(size) > MAX_BYTES:
            raise HTTPException(413, "the file is over 10 MB")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise HTTPException(413, "the file is over 10 MB")
        return uploads.put(key.tenant_id, bytes(data), request.headers.get("content-type", ""))

    @app.get("/v1/messages/{message_id}/media", responses={
        **_ERRORS, 200: {"content": {"application/octet-stream": {}},
                         "description": "The attachment's bytes"}},
             response_class=Response, summary="Demo mode: a console attachment's bytes")
    def get_media(message_id: str, key: Owner) -> Response:
        """The file the owner attached to this message in the console. 404 for
        any other message, another copy's, or in live mode."""
        if uploads is None or not message_id.startswith("in:"):
            raise HTTPException(404, "not found")
        with engine.connect() as conn:
            row = conn.execute(sa.select(s.inbound_messages.c.media_ref).where(
                s.inbound_messages.c.tenant_id == key.tenant_id,
                s.inbound_messages.c.external_id == message_id[3:])).first()
        ref = str(row.media_ref) if row is not None else ""
        data = uploads.read(key.tenant_id, ref) if ref else None
        if data is None:
            raise HTTPException(404, "not found")
        return Response(content=data, media_type=uploads.mime_of(ref),
                        headers={"Cache-Control": "private, no-store",
                                 "X-Content-Type-Options": "nosniff",
                                 "Content-Disposition": "inline"})

    # -------------------------------------------------------------- console

    @app.get("/v1/threads", response_model=ThreadList, responses=_ERRORS)
    def get_threads(key: Owner, limit: Annotated[int, Query(ge=1, le=200)] = 50,
                    offset: Annotated[int, Query(ge=0)] = 0,
                    kind: Annotated[str, Query(pattern="^(|shop|owner|rep|job|new)$")] = ""
                    ) -> dict[str, Any]:
        return reads.list_threads(engine, key.tenant_id, limit=limit, offset=offset,
                                  kind=kind)

    @app.get("/v1/threads/{thread_id}", response_model=ThreadDetail, responses=_ERRORS,
             summary="A thread's messages and reasoning steps")
    def get_thread(thread_id: str, key: Owner) -> dict[str, Any]:
        return reads.get_thread(engine, key.tenant_id, thread_id, uploads=uploads is not None)

    @app.get("/v1/tasks", response_model=TaskList, responses=_ERRORS,
             summary="Helper tasks and their status (working, needs_you, done)")
    def get_tasks(key: Owner, thread_id: str = "", helper: str = "",
                  status: Annotated[str, Query(pattern=r"^(|working|needs_you|done|handed_back"
                                                       r"|failed)$")] = "",
                  limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
        return reads.list_tasks(engine, key.tenant_id, thread_id=thread_id, helper=helper,
                                status=status, limit=limit)

    @app.get("/v1/activity", response_model=ActivityList, responses=_ERRORS,
             summary="Activity: every helper task with per-task steps (RAI-325)")
    def get_activity(key: Owner,
                     tab: Annotated[str, Query(pattern="^(|running|waiting|done)$")] = "",
                     limit: Annotated[int, Query(ge=1, le=200)] = 50,
                     offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, Any]:
        """The console's Activity view (RAI-325): one row per helper task,
        newest first, with what it is doing in plain words, its tab (running,
        waiting, done), its start time in Africa/Lagos and its step lines.
        Stopping a running row never undoes what it already did."""
        return reads.list_activity(engine, key.tenant_id, tab=tab, limit=limit,
                                   offset=offset)

    @app.post("/v1/turns/{turn_id}/stop", response_model=StopOut, responses=_ERRORS,
              summary="Stop one task: it ends at its next step (RAI-325)")
    def post_turn_stop(turn_id: str, key: Owner) -> dict[str, Any]:
        """Flag one running turn of this distributor to stop (idempotent).
        The worker ends the turn at its next step boundary: no further tool
        call runs and no further message goes out. Stopping never undoes what
        the turn already did. A finished turn reports itself, changed or not."""
        return agent.stop_turn(key.tenant_id, turn_id)

    @app.get("/v1/shops", response_model=ShopList, responses=_ERRORS)
    def get_shops(key: Owner) -> dict[str, Any]:
        return reads.list_shops(engine, key.tenant_id)

    @app.get("/v1/shops/{shop_id}", response_model=ShopDetail, responses=_ERRORS)
    def get_shop(shop_id: str, key: Owner) -> dict[str, Any]:
        return reads.get_shop(engine, key.tenant_id, shop_id)

    @app.get("/v1/approvals", response_model=ApprovalList, responses=_ERRORS,
             summary="Approvals (pending by default)")
    def get_approvals(key: Owner, status: Annotated[
            str, Query(pattern="^(pending|approved|rejected|superseded|withdrawn|all)$")] = "pending"
            ) -> dict[str, Any]:
        return reads.list_approvals(engine, key.tenant_id, status=status)

    def _decide(approval_id: str, key: ApiKey, body: DecisionIn | None, approve: bool,
                wait: bool) -> dict[str, Any]:
        b = body or DecisionIn(note="", by="owner", option=None)
        slow = sales.is_slow(engine, key.tenant_id, approval_id)
        if slow and approve:
            option = sales.check_option(engine, key.tenant_id, approval_id, b.option)
            row, _ = agent.decide(key.tenant_id, approval_id, approve=True, by=b.by,
                                  note=b.note, demo_mode=settings.demo_mode)
            sales.on_approved(engine, key.tenant_id, approval_id, option)
            return {"approval": agent.approval(key.tenant_id, approval_id), "follow_up": None}
        if approve and settings.demo_mode:
            a = agent.approval(key.tenant_id, approval_id)
            if refuses_approval(a["tool"], a["intent_kind"]):
                raise HTTPException(403, "the demo never raises a credit limit or the "
                                         "daily allowance; you can decline it")
        row, follow = agent.decide(key.tenant_id, approval_id, approve=approve, by=b.by,
                                   note=b.note, demo_mode=settings.demo_mode)
        if follow is not None and wait and not follow.finished:
            agent.process(follow.turn_id)
            follow = agent.wait(key.tenant_id, follow.turn_id, settings.wait_s)
        return {"approval": row, "follow_up": _turn_out(follow) if follow else None}

    @app.post("/v1/approvals/{approval_id}/approve", response_model=DecisionOut,
              responses=_ERRORS, summary="Owner approves: Mimi carries on (never moves money)")
    def approve(approval_id: str, key: Owner, body: DecisionIn | None = None,
                wait: bool = True) -> dict[str, Any]:
        return _decide(approval_id, key, body, True, wait)

    @app.post("/v1/approvals/{approval_id}/reject", response_model=DecisionOut,
              responses=_ERRORS, summary="Owner rejects: nothing is sent")
    def reject(approval_id: str, key: Owner, body: DecisionIn | None = None,
               wait: bool = True) -> dict[str, Any]:
        return _decide(approval_id, key, body, False, wait)

    @app.post("/v1/approvals/{approval_id}/undo", response_model=DecisionOut,
              responses=_ERRORS, summary="Owner undoes an approved Zoho payment write "
                                         "(or a limit Credit suggested)")
    def undo_approval(approval_id: str, key: Owner) -> dict[str, Any]:
        """A ``zoho_payment`` approval: calls Zoho Books' delete path (a fake Zoho
        kept in the store in demo mode). A ``credit_over_limit`` approval: puts
        the old limit back. 409 for anything else or a second undo."""
        if sales.is_slow(engine, key.tenant_id, approval_id):
            sales.undo(engine, key.tenant_id, approval_id)
        else:
            dw.undo_approval(engine, key.tenant_id, approval_id, demo_mode=settings.demo_mode)
        return {"approval": agent.approval(key.tenant_id, approval_id), "follow_up": None}

    @app.post("/v1/credit/{shop_id}/limit", response_model=CreditLimitOut,
              responses=_ERRORS, summary="Owner sets a shop's credit limit (recorded, undoable)")
    def post_credit_limit(shop_id: str, body: CreditLimitIn, key: Owner) -> dict[str, Any]:
        """Recorded as a ``limit_change`` step on the shop's thread. Mimi never
        sets a limit on its own: only the owner, here or by approving Credit's
        suggestion."""
        return dw.set_limit(engine, key.tenant_id, shop_id, body.amount_ngn,
                            reason=body.reason)

    @app.post("/v1/credit/changes/{change_id}/undo", response_model=CreditLimitOut,
              responses=_ERRORS, summary="Owner undoes a credit-limit change")
    def post_credit_undo(change_id: str, key: Owner) -> dict[str, Any]:
        return dw.undo_limit(engine, key.tenant_id, change_id)

    @app.get("/v1/credit", response_model=CreditOut, responses=_ERRORS,
             summary="Credit limits per shop and credit requests")
    def get_credit(key: Owner) -> dict[str, Any]:
        return reads.credit(engine, key.tenant_id)

    @app.get("/v1/stock/sell-through", response_model=SellThroughOut, responses=_ERRORS,
             summary="Sell-through per shop and product: sold, days of cover, days to "
                     "expiry, status")
    def get_sell_through(key: Owner, days: Annotated[int, Query(ge=7, le=90)] = 28,
                         shop_id: Annotated[str, Query(max_length=64,
                                                       pattern=r"^[A-Za-z0-9_.-]*$")] = "",
                         status: Annotated[str, Query(
                             pattern="^(|low|ok|slow|near_expiry)$")] = "") -> dict[str, Any]:
        """Code only, no model. ``sold_units`` comes from two shop counts when there
        are two (with ``proof`` saying so), else from the estimate. A low-confidence
        slow reading is a ``watch`` hint, not a status. Sorted near expiry, slow,
        low, ok, then by value at risk."""
        return sales.sell_through(engine, key.tenant_id, days=days, shop_id=shop_id,
                                  status=status)

    @app.post("/v1/stock/slow-check", response_model=SlowCheckOut, responses=_ERRORS,
              summary="Run the weekly slow-stock check: opens slow_stock approvals")
    def post_slow_check(key: Owner) -> dict[str, Any]:
        """Each slow or near-expiry line (medium or high confidence), at most one per
        shop and one per item a week, becomes a ``slow_stock`` approval with move,
        promo or pickup options (quantities and ₦ by code). Running it again in the
        same week opens nothing new. Nothing moves until the owner approves; Mimi
        never moves stock or money."""
        return sales.run_slow_check(engine, key.tenant_id)

    @app.get("/v1/mentions", response_model=MentionList, responses=_ERRORS,
             summary="The owner's @ picker: helpers, shops and reps")
    def get_mentions(key: Owner, q: Annotated[str, Query(max_length=80)] = "",
                     kinds: Annotated[str, Query(
                         pattern=r"^$|^(?:helper|shop|rep)(?:,(?:helper|shop|rep))*$")] = "",
                     limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
        """``q`` matches the start of a name or of any word in it (a leading
        ``@`` is ignored); ``kinds`` is a comma list, empty for all three.
        Helper rows carry ``assign_to`` for ``POST /v1/turns``."""
        return reads.mentions(engine, key.tenant_id, q=q, kinds=kinds, limit=limit)

    # ------------------------------------------------------- connectors (RAI-277)

    @app.get("/v1/connectors", response_model=ConnectorList, responses=_ERRORS,
             summary="Connector toolkits and their status")
    def get_connectors(key: Owner) -> dict[str, Any]:
        return cx.list_connectors(engine, key.tenant_id)

    @app.post("/v1/connectors/{toolkit}/connect", response_model=ConnectorConnectOut,
              responses=_ERRORS, summary="Start the connect flow for a toolkit")
    def post_connector_connect(toolkit: str, key: Owner,
                               body: ConnectorConnectIn | None = None) -> dict[str, Any]:
        from mimi.connectors.env import ZOHO_REGIONS

        upper = toolkit.upper()
        if upper not in ("ZOHO_BOOKS", "ZOHO", "GOOGLESHEETS"):
            raise HTTPException(404, "unknown toolkit")
        region = ((body.region if body else "") or "").strip().lower()
        if upper in ("ZOHO_BOOKS", "ZOHO") and region and region not in ZOHO_REGIONS:
            raise HTTPException(422, f"region is {region!r}: use com, eu, in, cn or au")
        transport = _connector_transport()
        try:
            return transport.initiate(upper, user_id=key.tenant_id)
        except ConnectorError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/v1/connectors/callback", responses=_ERRORS,
             summary="Composio returns here after consent")
    def get_connector_callback(request: Request, key: Owner) -> dict[str, Any]:
        """Check the state, read the connected account, send the owner back
        to Settings > Connectors."""
        from mimi.connectors.env import ConnectorSettings
        from mimi.connectors.state import check_state

        params = dict(request.query_params)
        state = params.get("state", "")
        settings_c = ConnectorSettings.from_env()
        try:
            payload = check_state(settings_c.state_secret, state)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if str(payload.get("tenant") or "") != key.tenant_id:
            # A link signed for another distributor never binds here.
            raise HTTPException(422, "that link is not valid; start a new connect")
        if _connector_state_used(engine, key.tenant_id, state):
            raise HTTPException(422, "that link was already used; start a new connect")
        connected_id = params.get("connected_account_id", "")
        if not connected_id:
            raise HTTPException(422, "Composio gave no connected account; start a new connect")
        transport = _connector_transport()
        try:
            info = transport.account(connected_id)
        except ConnectorError as exc:
            raise HTTPException(502, str(exc)) from exc
        connection_id = f"conn-{connected_id[-12:]}"
        cx.save_account(engine, key.tenant_id, connection_id=connection_id,
                        toolkit=str(payload.get("toolkit") or ""),
                        connected_account_id=connected_id,
                        region=settings_c.zoho_region, connected_at=now_iso(wall()))
        _connector_state_mark(engine, key.tenant_id, state)
        status = str(info.get("status") or "ACTIVE")
        return {"connection_id": connection_id, "status": status,
                "next": "Settings > Connectors"}

    @app.get("/v1/connectors/{connection_id}", response_model=ConnectorDetail,
             responses=_ERRORS, summary="A connection's status and sync preview")
    def get_connector(connection_id: str, key: Owner) -> dict[str, Any]:
        return cx.get_connection(engine, key.tenant_id, connection_id)

    @app.delete("/v1/connectors/{connection_id}", responses=_ERRORS,
                summary="Disconnect: revoke before the next sync")
    def delete_connector(connection_id: str, key: Owner) -> dict[str, Any]:
        with engine.connect() as conn:
            row = conn.execute(sa.select(s.connector_accounts).where(
                s.connector_accounts.c.tenant_id == key.tenant_id,
                s.connector_accounts.c.connection_id == connection_id)).first()
        if row is None:
            raise HTTPException(404, "not found")
        transport = _connector_transport()
        try:
            transport.disconnect(str(row.connected_account_id or connection_id))
        except ConnectorError as exc:
            raise HTTPException(502, str(exc)) from exc
        cx.revoke_account(engine, key.tenant_id, connection_id, now_iso(wall()))
        return {"connection_id": connection_id, "status": "revoked"}

    rules = SqlPolicyRules(engine)

    @app.get("/v1/policy", response_model=PolicyOut, responses=_ERRORS,
             summary="The owner's rulebook: what Mimi does on her own, asks, or hands off")
    def get_policy(key: Owner) -> dict[str, Any]:
        return rules.view(key.tenant_id, editable=not (settings.demo_mode or key.session))

    @app.put("/v1/policy", response_model=PolicyOut, responses=_ERRORS,
             summary="Change rules (owner key only; built-in stops stay locked)")
    def put_policy(body: PolicyIn, key: Owner) -> dict[str, Any]:
        if settings.demo_mode or key.session:
            raise HTTPException(403, "the demo's rules are read-only")
        if not owner_rules_on():
            raise HTTPException(409, "owner rules are switched off (MIMI_POLICY_RULES=0)")
        try:
            rules.update(key.tenant_id, [(r.action_type, r.behaviour) for r in body.rules],
                         by=body.by, key_digest=key.digest)
        except PolicyLocked as exc:
            raise HTTPException(409, str(exc)) from None
        return rules.view(key.tenant_id, editable=True)

    # --------------------------------------------------- computer tasks

    @app.get("/v1/computer/kinds", response_model=ComputerKindList, responses=_ERRORS,
             summary="The task kinds, exactly as the code names them")
    def get_computer_kinds(key: Owner) -> dict[str, Any]:
        """The one place the kind strings are published, so the console and the
        API never disagree: the value of ``kind`` is what goes in a proposal.
        An empty list is never right here: the plans are code, not data."""
        return {"kinds": computer.kinds()}

    @app.post("/v1/computer/tasks", response_model=ComputerProposal, status_code=201,
              responses=_ERRORS, summary="Propose a task for the browser container")
    def post_computer_task(body: ComputerTaskIn, key: Owner) -> dict[str, Any]:
        """Build the plan in code from the goal, the url and the approved
        domain, and return it for the owner to approve. Nothing runs yet and
        nothing is charged. The plan is what will run: it never holds a step
        that buys, pays, sends or changes settings, and never a credential."""
        try:
            return computer.propose(key.tenant_id, body.goal, body.url, body.domain,
                                    body.kind, body.inputs)
        except Refused as exc:
            raise HTTPException(exc.status, exc.message) from None

    @app.get("/v1/computer/tasks", response_model=ComputerTaskList, responses=_ERRORS,
             summary="This distributor's computer tasks, newest first")
    def get_computer_tasks(key: Owner) -> dict[str, Any]:
        """An empty list is a valid answer: nothing is seeded."""
        return {"tasks": computer.tasks(key.tenant_id)}

    @app.get("/v1/computer/tasks/{task_id}", response_model=ComputerTaskDetail,
             responses=_ERRORS,
             summary="One task with its step log and artifacts")
    def get_computer_task(task_id: str, key: Owner) -> dict[str, Any]:
        view = computer.get(key.tenant_id, task_id)
        if view is None:
            raise HTTPException(404, "not found")
        return view

    @app.post("/v1/computer/tasks/{task_id}/approve", response_model=ComputerTaskDetail,
              responses=_ERRORS, summary="Approve the plan once; it runs under the guardrails")
    def post_computer_approve(task_id: str, key: Owner) -> dict[str, Any]:
        """The owner approves the whole task once. The run continues in the
        background; poll this task for its steps. Approving a paused task
        (a sign-in wall) resumes it in the session it paused in, so a sign-in
        the owner did through the live view is still there; if that session has
        ended, the task ends failed and the owner proposes it again. One
        approval starts the run and a second one in flight gets 409. The
        guardrails and the budgets are code."""
        try:
            computer.approve(key.tenant_id, task_id)
        except Refused as exc:
            raise HTTPException(exc.status, exc.message) from None
        view = computer.get(key.tenant_id, task_id)
        if view is None:
            raise HTTPException(404, "not found")
        return view

    @app.post("/v1/computer/tasks/{task_id}/stop", response_model=ComputerTaskDetail,
              responses=_ERRORS, summary="Stop the task now, and record why")
    def post_computer_stop(task_id: str, key: Owner) -> dict[str, Any]:
        try:
            return computer.stop(key.tenant_id, task_id)
        except Refused as exc:
            raise HTTPException(exc.status, exc.message) from None

    @app.get("/v1/computer/tasks/{task_id}/artifacts/{name}",
             summary="The bytes of one step screenshot or extracted text",
             responses={**_ERRORS, 200: {"content": {"image/png": {}},
                                         "description": "The artifact"}},
             response_class=Response)
    def get_computer_artifact(task_id: str, name: str, key: Owner) -> Response:
        row = computer.artifact(key.tenant_id, task_id, name)
        if row is None:
            raise HTTPException(404, "not found")
        try:
            data = Path(row["path"]).read_bytes()
        except OSError:
            raise HTTPException(404, "not found") from None
        return Response(content=data, media_type=row["mime"] or "application/octet-stream",
                        headers={"Cache-Control": "no-store"})

    @app.get("/v1/health", response_model=Health)
    def health(response: Response) -> dict[str, Any]:
        db = "ok"
        depth = 0
        try:
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            depth = agent.queue.depth()
        except Exception as exc:  # pragma: no cover - a down database
            db = type(exc).__name__
            response.status_code = 503
        return {"status": "ok" if db == "ok" else "degraded", "db": db,
                "model": agent.model_mode, "queue_depth": depth,
                "version": settings.release or VERSION}

    @app.get("/", response_model=Liveness, summary="Liveness (no database, no auth)")
    def live() -> dict[str, Any]:
        """For the platform's probe: the process is up and serving. Readiness,
        with the database and queue, is ``GET /v1/health``."""
        return {"status": "ok"}

    @app.get("/v1/me", response_model=MeOut, responses=_ERRORS,
             summary="The caller's distributor, role and mode")
    def me(key: Any_, request: Request) -> dict[str, Any]:
        status = None
        if demo is not None:
            status = demo.status(demo.session(key.digest) if key.session else None,
                                 visitor(request, key), ip_source(request))
        return {"tenant_id": key.tenant_id, "role": key.role, "mode": settings.mode,
                "demo": status}

    def _demo_on() -> DemoStore:
        if demo is None:
            raise HTTPException(404, "not found")
        return demo

    @app.post("/v1/demo/sessions", response_model=DemoSessionOut, status_code=201,
              responses=_DEMO_ERRORS,
              summary="Demo mode: start a session on a fresh synthetic copy")
    def demo_session(request: Request) -> dict[str, Any]:
        """A fresh copy of the synthetic Coral Lane Trading world and a token
        for it (send it as ``Authorization: Bearer``). 404 in live mode."""
        store = _demo_on()
        return store.start(visitor(request))

    @app.post("/v1/demo/reset", response_model=DemoSessionOut, status_code=201,
              responses=_DEMO_ERRORS,
              summary="Demo mode: end this session and start a fresh copy")
    def demo_reset(key: Any_, request: Request) -> dict[str, Any]:
        store = _demo_on()
        if not key.session:
            raise HTTPException(403, "only a demo session can be reset")
        # Paused: refuse before ending the session, so the caller keeps it.
        store.refuse_if_paused()
        ip = visitor(request, key)
        store.end(key.digest)
        return store.start(ip)

    return app


def _connector_transport() -> Any:
    """FakeComposio in demo and offline runs; the real transport only when
    the key is set and the mode says so. Demo sessions never start OAuth."""
    from mimi.connectors.composio import ComposioTransport, FakeComposio
    from mimi.connectors.env import ConnectorSettings

    settings = ConnectorSettings.from_env()
    if settings.live:
        return ComposioTransport(settings)
    return FakeComposio(settings)


def _connector_state_used(engine: Any, tenant: str, state: str) -> bool:
    import hashlib

    digest = hashlib.sha256(state.encode()).hexdigest()
    with engine.connect() as conn:
        sub = (sa.select(sa.literal(1)).select_from(s.artifacts).where(
            s.artifacts.c.tenant_id == tenant,
            s.artifacts.c.path == f"connector-state:{digest}"))
        hit = conn.execute(sa.select(sub.exists())).scalar()
        return bool(hit)


def _connector_state_mark(engine: Any, tenant: str, state: str) -> None:
    import hashlib

    from mimi.store.util import insert_ignore

    digest = hashlib.sha256(state.encode()).hexdigest()
    with engine.begin() as conn:
        insert_ignore(conn, s.artifacts, {
            "path": f"connector-state:{digest}", "tenant_id": tenant,
            "content": now_iso(wall())})


def _turn_out(v: Any) -> dict[str, Any]:
    d = v.to_dict()
    d.pop("tenant_id", None)
    return d


def openapi_json(make: Callable[[], FastAPI] | None = None) -> dict[str, Any]:
    """The OpenAPI document, built without a database or keys."""
    from mimi.api.offline import OfflineModel
    from mimi.store.db import ensure_schema

    if make is None:
        engine = make_engine("sqlite://")
        ensure_schema(engine)
        app = create_app(Settings(), Agent(engine, models=[OfflineModel()]), workers=0)
    else:
        app = make()
    return app.openapi()

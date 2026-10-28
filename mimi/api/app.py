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
* ``GET /v1/health``.

Each key belongs to one distributor; every query is filtered by it, so a
key can never read or change another distributor's rows. This module is the
only place in ``mimi`` that imports the web framework.
"""

# No ``from __future__ import annotations``: FastAPI reads the dependency
# aliases defined inside create_app at runtime.

import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mimi.api import reads
from mimi.api.agent import Agent, Conflict, NotFound
from mimi.api.config import LOCALHOST_REGEX, ApiKey, Settings
from mimi.api.schemas import (
    ApprovalList,
    CreditOut,
    DecisionIn,
    DecisionOut,
    ErrorOut,
    Health,
    ShopDetail,
    ShopList,
    ThreadDetail,
    ThreadList,
    TurnIn,
    TurnOut,
)
from mimi.harness.go import CrossTenant
from mimi.harness.inbound import Inbound, Media, Payload
from mimi.store.db import make_engine

log = logging.getLogger(__name__)
VERSION = "0.1.0"
_bearer = HTTPBearer(auto_error=False)
_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorOut}, 403: {"model": ErrorOut}, 404: {"model": ErrorOut},
    409: {"model": ErrorOut}}


def create_app(settings: Settings | None = None, agent: Agent | None = None, *,
               workers: int | None = None) -> FastAPI:
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

    def auth(creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
             ) -> ApiKey:
        key = settings.lookup(creds.credentials if creds else "")
        if key is None:
            raise HTTPException(401, "a valid bearer key is required",
                                headers={"WWW-Authenticate": "Bearer"})
        return key

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

    @app.exception_handler(Conflict)
    async def _cf(_: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    # ---------------------------------------------------------------- turns

    @app.post("/v1/turns", response_model=TurnOut, responses={202: {"model": TurnOut},
                                                               **_ERRORS},
              summary="Send one inbound message to Mimi")
    def post_turn(body: TurnIn, key: Any_, response: Response,
                  wait: Annotated[bool, Query(description="wait for the replies")] = True
                  ) -> dict[str, Any]:
        if body.tenant_id and body.tenant_id != key.tenant_id:
            raise HTTPException(403, "this key belongs to another distributor")
        media = Media()
        ref = ""
        if body.media is not None:
            m = body.media
            ref = m.ref
            media = Media(kind=m.kind, mime=m.mime, sha256=m.sha256, width=m.width,
                          height=m.height, camera_exif=m.camera_exif, caption=m.caption,
                          transcript=m.transcript, transcript_lang=m.transcript_lang)
        if not (body.text.strip() or body.button_id or media.kind != "none"):
            raise HTTPException(422, "a turn needs text, a button or media")
        inbound = Inbound(
            source=body.source, org_id=key.tenant_id, external_id=body.message_id,
            received_at=body.received_at or datetime.now(UTC),
            payload=Payload(text=body.text, from_phone=body.phone, media=media,
                            button_id=body.button_id))
        view = agent.submit(key.tenant_id, inbound, media_ref=ref, simulated=body.simulated)
        if wait and not view.finished:
            agent.process(view.turn_id)
            view = agent.wait(key.tenant_id, view.turn_id, settings.wait_s,
                              duplicate=view.duplicate)
        if not view.finished:
            response.status_code = 202
        return _turn_out(view)

    @app.get("/v1/turns/{turn_id}", response_model=TurnOut, responses=_ERRORS,
             summary="A turn's state and replies")
    def get_turn(turn_id: str, key: Any_) -> dict[str, Any]:
        return _turn_out(agent.view(key.tenant_id, turn_id))

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
        return reads.get_thread(engine, key.tenant_id, thread_id)

    @app.get("/v1/shops", response_model=ShopList, responses=_ERRORS)
    def get_shops(key: Owner) -> dict[str, Any]:
        return reads.list_shops(engine, key.tenant_id)

    @app.get("/v1/shops/{shop_id}", response_model=ShopDetail, responses=_ERRORS)
    def get_shop(shop_id: str, key: Owner) -> dict[str, Any]:
        return reads.get_shop(engine, key.tenant_id, shop_id)

    @app.get("/v1/approvals", response_model=ApprovalList, responses=_ERRORS,
             summary="Approvals (pending by default)")
    def get_approvals(key: Owner, status: Annotated[
            str, Query(pattern="^(pending|approved|rejected|all)$")] = "pending"
            ) -> dict[str, Any]:
        return reads.list_approvals(engine, key.tenant_id, status=status)

    def _decide(approval_id: str, key: ApiKey, body: DecisionIn | None, approve: bool,
                wait: bool) -> dict[str, Any]:
        b = body or DecisionIn(note="", by="owner")
        row, follow = agent.decide(key.tenant_id, approval_id, approve=approve, by=b.by,
                                   note=b.note)
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

    @app.get("/v1/credit", response_model=CreditOut, responses=_ERRORS,
             summary="Credit limits per shop and credit requests")
    def get_credit(key: Owner) -> dict[str, Any]:
        return reads.credit(engine, key.tenant_id)

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

    return app


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

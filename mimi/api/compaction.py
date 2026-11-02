"""Thread compaction as a durable background pass (RAI-285).

A thread whose prompt passed the threshold does not compact at once: the
completing turn leaves a version-0 timer row (due about 10 minutes later,
while the provider's cache is still warm), and an idle worker compacts it.
A thread with a turn queued, leased or waiting in its inbox is never
compacted: the next turn starts from the full history instead.

One model call per compaction (``muse-spark-1.3`` through the thread's own
prompt start, so the cache is reused). Code attaches the book refs, checks
every promise and decision still appears, and refuses any summary with a
free-text naira figure: a refused summary changes nothing, and the thread
keeps its full history. Raw history rows are never deleted.
"""

from __future__ import annotations

import logging
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.clock import wall
from mimi.harness import compaction as cp
from mimi.harness.context import est_tokens
from mimi.models.base import ChatModel
from mimi.models.fake import FakeModel
from mimi.store import schema as s
from mimi.store.util import dumps, insert_ignore, loads, now_iso

log = logging.getLogger(__name__)


def thread_busy(engine: Engine, tenant: str, thread: str) -> bool:
    """A turn queued or leased for the thread, or a message waiting in its inbox."""
    with engine.connect() as conn:
        turn = conn.execute(sa.select(sa.func.count()).select_from(s.turns).where(
            s.turns.c.tenant_id == tenant, s.turns.c.thread_id == thread,
            s.turns.c.state.in_(("queued", "leased")))).scalar() or 0
        if turn:
            return True
        inbox = conn.execute(sa.select(sa.func.count()).select_from(s.thread_inbox).where(
            s.thread_inbox.c.tenant_id == tenant, s.thread_inbox.c.thread_id == thread,
            s.thread_inbox.c.status == "waiting")).scalar() or 0
        return bool(inbox)


def schedule_if_big(engine: Engine, *, tenant: str, thread: str, helper: str, skill: str,
                    prompt_tokens: int, turns: int, now: float | None = None,
                    env: dict[str, str] | None = None) -> bool:
    """Leave the compaction timer after a big turn. True when one is due."""
    if not cp.compaction_on(env):
        return False
    if not cp.should_compact(prompt_tokens=prompt_tokens, turns=turns,
                             threshold_tokens=cp.token_threshold(env)):
        return False
    due = (now if now is not None else wall()) + cp.delay_s(env)
    with engine.begin() as conn:
        top = conn.execute(sa.select(sa.func.max(s.compactions.c.version)).where(
            s.compactions.c.tenant_id == tenant,
            s.compactions.c.thread_id == thread)).scalar() or 0
        if top:
            # A summary exists: the next compaction re-runs from raw history
            # only after another big turn; the old timer is spent.
            conn.execute(s.compactions.delete().where(
                s.compactions.c.tenant_id == tenant, s.compactions.c.thread_id == thread,
                s.compactions.c.version == 0))
        insert_ignore(conn, s.compactions, {
            "tenant_id": tenant, "thread_id": thread, "version": 0, "helper": helper,
            "skill": skill, "body": "", "summary_hash": "", "covered_turns": "[]",
            "generation": 0, "tokens_before": prompt_tokens, "tokens_after": 0,
            "due_ts": due, "created_at": now_iso()})
    return True


def due_threads(engine: Engine, *, now: float | None = None,
                limit: int = 10) -> list[dict[str, Any]]:
    """Timer rows whose delay passed, oldest first."""
    at = now if now is not None else wall()
    with engine.connect() as conn:
        rows = conn.execute(sa.select(s.compactions).where(
            s.compactions.c.version == 0, s.compactions.c.due_ts <= at)
            .order_by(s.compactions.c.due_ts).limit(limit)).mappings().all()
    return [dict(r) for r in rows]


def compact_thread(engine: Engine, *, tenant: str, thread: str, book: cp.BookRefs,
                   model: ChatModel | None, shop_id: str = "",
                   env: dict[str, str] | None = None) -> dict[str, Any]:
    """Compact one thread now (the timer already fired). Returns the outcome.

    ``refused`` (with ``problems``) when the draft fails its checks: nothing
    changes and the timer row is dropped. ``skipped_busy`` when a wake-up is
    queued: the next turn runs on full history. ``done`` writes the version,
    moves the window past the covered turns, re-pins the prefix and logs a
    ``compacted`` thread event with tokens before and after.
    """
    if thread_busy(engine, tenant, thread):
        return {"status": "skipped_busy"}
    keep = cp.keep_turns(env)
    with engine.connect() as conn:
        rows = conn.execute(sa.select(s.history.c.turn_id, s.history.c.body).where(
            s.history.c.tenant_id == tenant, s.history.c.thread_id == thread)
            .order_by(s.history.c.ts, s.history.c.turn_id)).all()
        timer = conn.execute(sa.select(s.compactions).where(
            s.compactions.c.tenant_id == tenant, s.compactions.c.thread_id == thread,
            s.compactions.c.version == 0)).mappings().first()
    turns: list[list[dict[str, Any]]] = [list(loads(b, [])) for _, b in rows]
    turn_ids = [str(t) for t, _ in rows]
    first_kept, _ = cp.compact_window(turns=len(turns), keep=keep)
    if first_kept <= 0:
        _drop_timer(engine, tenant, thread)
        return {"status": "refused", "problems": ["nothing to cover: the thread is short"]}
    covered, covered_ids = turns[:first_kept], turn_ids[:first_kept]
    before = sum(est_tokens(t) for t in covered)
    timer_map = dict(timer) if timer is not None else {}
    helper = str(timer_map.get("helper", "") or "collector")
    skill = str(timer_map.get("skill", "") or "collector/collections")
    shop_name = _shop_name(book, tenant, shop_id)
    mdl: ChatModel = model or FakeModel(name="fake:compaction")
    try:
        draft = cp.compact(mdl, helper_name=helper, skill=skill, shop_name=shop_name,
                           turns=covered, turn_ids=covered_ids)
        draft = cp.attach_refs(draft, book, tenant)
        problems = cp.validate(draft, turns=covered)
    except Exception as exc:  # a draft that cannot even parse: refuse it
        log.warning("compaction draft failed for %s: %s", thread, exc)
        _drop_timer(engine, tenant, thread)
        return {"status": "refused", "problems": [f"draft failed: {exc}"]}
    if problems:
        log.warning("compaction refused for %s: %s", thread, "; ".join(problems))
        _drop_timer(engine, tenant, thread)
        return {"status": "refused", "problems": problems}
    rendered = draft.render(shop_names=_shop_names(book, tenant))
    after = est_tokens(rendered)
    version = _commit(engine, tenant=tenant, thread=thread, helper=helper, skill=skill,
                      draft=draft, rendered=rendered, before=before, after=after,
                      keep_from=first_kept)
    return {"status": "done", "version": version, "summary_hash": cp.summary_hash(draft),
            "tokens_before": before, "tokens_after": after,
            "covered": len(covered_ids), "kept": len(turn_ids) - first_kept}


def _commit(engine: Engine, *, tenant: str, thread: str, helper: str, skill: str,
            draft: cp.ThreadSummary, rendered: str, before: int, after: int,
            keep_from: int) -> int:
    now = wall()
    with engine.begin() as conn:
        top = conn.execute(sa.select(sa.func.max(s.compactions.c.version)).where(
            s.compactions.c.tenant_id == tenant,
            s.compactions.c.thread_id == thread)).scalar() or 0
        version = int(top) + 1 if top else 1
        conn.execute(s.compactions.delete().where(
            s.compactions.c.tenant_id == tenant, s.compactions.c.thread_id == thread,
            s.compactions.c.version == 0))
        conn.execute(s.compactions.insert().values(
            tenant_id=tenant, thread_id=thread, version=version, helper=helper,
            skill=skill, body=dumps({**draft.model_dump(mode="json"), "rendered": rendered}),
            summary_hash=cp.summary_hash(draft),
            covered_turns=dumps(list(draft.covered_turns)),
            generation=draft.generation, tokens_before=before, tokens_after=after,
            due_ts=0.0, created_at=now_iso(now)))
        # The window jumps past the covered turns in one step; the prefix is
        # dropped so the next build re-pins with the new epoch (its hash
        # changes once, deliberately). Raw history rows stay for audit.
        row = conn.execute(sa.select(s.prefixes.c.window_from).where(
            s.prefixes.c.tenant_id == tenant, s.prefixes.c.thread_id == thread,
            s.prefixes.c.helper == helper)).scalar()
        if row is None:
            insert_ignore(conn, s.prefixes, {
                "tenant_id": tenant, "thread_id": thread, "helper": helper, "body": None,
                "window_from": keep_from})
        else:
            conn.execute(s.prefixes.update().where(
                s.prefixes.c.tenant_id == tenant, s.prefixes.c.thread_id == thread,
                s.prefixes.c.helper == helper).values(body=None, window_from=keep_from))
        _event(conn, tenant, thread, helper, version, draft, before, after, now)
    return version


def _event(conn: Any, tenant: str, thread: str, helper: str, version: int,
           draft: cp.ThreadSummary, before: int, after: int, now: float) -> None:
    insert_ignore(conn, s.thread_events, {
        "tenant_id": tenant, "event_id": f"compact_{thread}_{version}",
        "thread_id": thread, "run_id": "", "turn_id": "", "seq": -1, "step": 0,
        "type": "compacted", "tool": "", "tier": "", "short": (
            f"compacted {len(draft.covered_turns)} turns into summary v{version}: "
            f"{before} to {after} tokens"),
        "cost_kobo": 0, "model": cp.MODEL, "duration_ms": 0.0,
        "data": dumps({"version": version, "helper": helper,
                       "summary_hash": cp.summary_hash(draft),
                       "covered_turns": len(draft.covered_turns),
                       "tokens_before": before, "tokens_after": after}),
        "at": now_iso(now), "ts": now})


def _drop_timer(engine: Engine, tenant: str, thread: str) -> None:
    with engine.begin() as conn:
        conn.execute(s.compactions.delete().where(
            s.compactions.c.tenant_id == tenant, s.compactions.c.thread_id == thread,
            s.compactions.c.version == 0))


def _shop_name(book: cp.BookRefs, tenant: str, shop_id: str) -> str:
    if not shop_id:
        return "the shop"
    try:
        return str(book.shop(tenant, shop_id).get("name", shop_id))
    except Exception:
        return shop_id or "the shop"


def _shop_names(book: cp.BookRefs, tenant: str) -> dict[str, str]:
    try:
        rows = book.receivables(tenant, None)
    except Exception:
        return {}
    out: dict[str, str] = {}
    seen: set[str] = set()
    for r in rows or []:
        sid = str(r.get("shop_id", ""))
        if sid and sid not in seen:
            seen.add(sid)
            try:
                out[sid] = str(book.shop(tenant, sid).get("name", sid))
            except Exception:
                out[sid] = sid
    return out


__all__ = ["compact_thread", "due_threads", "schedule_if_big", "thread_busy"]

"""The console's read queries. Every one is filtered by the caller's tenant.

Figures are summed here in integer kobo; ``*_naira`` strings are made from
them. An id that belongs to another distributor reads as not found.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.api import threadmeta
from mimi.api import voice as vx
from mimi.api.agent import NotFound, approval_dict, turn_handoff
from mimi.api.uploads import PREFIX as UPLOAD_PREFIX
from mimi.harness import handoff as hx
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.store.util import loads, mask_phone


def naira(kobo: int) -> str:
    return f"N{kobo / 100:,.0f}" if kobo % 100 == 0 else f"N{kobo / 100:,.2f}"


# ------------------------------------------------------------------ shops


def _shop_rows(conn: sa.Connection, tenant: str, shop_id: str | None = None) -> list[Any]:
    t = conn.execute(sa.select(s.tenants.c.live_date).where(
        s.tenants.c.tenant_id == tenant)).scalar()
    today = t or "9999-12-31"
    r = s.receivables
    agg = (sa.select(
        r.c.shop_id,
        sa.func.coalesce(sa.func.sum(r.c.outstanding_kobo), 0).label("outstanding"),
        sa.func.coalesce(sa.func.sum(sa.case((r.c.due_on < today, r.c.outstanding_kobo),
                                             else_=0)), 0).label("overdue"),
        sa.func.count().label("open_n"))
        .where(r.c.tenant_id == tenant, r.c.outstanding_kobo > 0)
        .group_by(r.c.shop_id).subquery())
    q = (sa.select(s.shops, agg.c.outstanding, agg.c.overdue, agg.c.open_n)
         .outerjoin(agg, agg.c.shop_id == s.shops.c.shop_id)
         .where(s.shops.c.tenant_id == tenant).order_by(s.shops.c.shop_id))
    if shop_id is not None:
        q = q.where(s.shops.c.shop_id == shop_id)
    return list(conn.execute(q).all())


def _shop_summary(row: Any) -> dict[str, Any]:
    out, over = int(row.outstanding or 0), int(row.overdue or 0)
    return {
        "shop_id": row.shop_id, "name": row.name, "owner": row.owner, "area": row.area,
        "shop_type": row.shop_type, "size": row.size, "rep_id": row.rep_id,
        "phone_last4": mask_phone(row.phone),
        "credit_limit_kobo": int(row.credit_limit_kobo),
        "credit_limit_naira": naira(int(row.credit_limit_kobo)),
        "outstanding_kobo": out, "outstanding_naira": naira(out),
        "overdue_kobo": over, "overdue_naira": naira(over),
        "open_invoices": int(row.open_n or 0), "simulated": bool(row.simulated),
    }


def list_shops(engine: Engine, tenant: str) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = _shop_rows(conn, tenant)
    shops = [_shop_summary(r) for r in rows]
    return {"shops": shops, "total": len(shops)}


def get_shop(engine: Engine, tenant: str, shop_id: str) -> dict[str, Any]:
    go = SqlGo(engine)
    with engine.connect() as conn:
        rows = _shop_rows(conn, tenant, shop_id)
        if not rows:
            raise NotFound(shop_id)
        ds = conn.execute(sa.select(s.deliveries).where(
            s.deliveries.c.tenant_id == tenant, s.deliveries.c.shop_id == shop_id)
            .order_by(s.deliveries.c.day.desc(), s.deliveries.c.delivery_id.desc())
            .limit(10)).all()
        ids = [d.delivery_id for d in ds]
        lines: dict[str, list[dict[str, Any]]] = {}
        for ln in conn.execute(sa.select(s.delivery_lines).where(
                s.delivery_lines.c.tenant_id == tenant,
                s.delivery_lines.c.delivery_id.in_(ids)).order_by(
                s.delivery_lines.c.delivery_id, s.delivery_lines.c.line_no)):
            lines.setdefault(ln.delivery_id, []).append({
                "sku": ln.sku, "name": ln.name, "unit": ln.unit, "quantity": ln.quantity,
                "unit_price_kobo": int(ln.unit_price_kobo),
                "line_total_kobo": int(ln.line_total_kobo)})
        disputes = [{"dispute_id": d.dispute_id, "delivery_id": d.delivery_id, "sku": d.sku,
                     "missing": d.missing, "text": d.text, "status": d.status}
                    for d in conn.execute(sa.select(s.disputes).where(
                        s.disputes.c.tenant_id == tenant, s.disputes.c.shop_id == shop_id))]
    receivables = go.receivables(tenant, shop_id)
    for x in receivables:
        x["outstanding_naira"] = naira(x["outstanding_kobo"])
    return {
        "shop": _shop_summary(rows[0]),
        "thread_id": f"{tenant}:shop:{shop_id}",
        "receivables": receivables,
        "payments": go.payments(tenant, shop_id, 10),
        "promises": go.promises(tenant, shop_id),
        "deliveries": [{
            "delivery_id": d.delivery_id, "delivered_on": d.delivered_on, "rep_id": d.rep_id,
            "total_kobo": int(d.total_kobo), "total_naira": naira(int(d.total_kobo)),
            "status": d.status, "lines": lines.get(d.delivery_id, [])} for d in ds],
        "disputes": disputes,
    }


# ---------------------------------------------------------------- threads


def _thread_summary(conn: sa.Connection, tenant: str, t: Any) -> dict[str, Any]:
    last = conn.execute(sa.select(s.messages.c.text, s.messages.c.sender).where(
        s.messages.c.tenant_id == tenant, s.messages.c.thread_id == t.thread_id)
        .order_by(s.messages.c.ts.desc(), s.messages.c.message_id.desc()).limit(1)).first()
    n = conn.execute(sa.select(sa.func.count()).select_from(s.messages).where(
        s.messages.c.tenant_id == tenant, s.messages.c.thread_id == t.thread_id)).scalar()
    pending = conn.execute(sa.select(sa.func.count()).select_from(s.approvals).where(
        s.approvals.c.tenant_id == tenant, s.approvals.c.thread_id == t.thread_id,
        s.approvals.c.status == "pending")).scalar()
    shop_name = ""
    if t.shop_id:
        shop_name = str(conn.execute(sa.select(s.shops.c.name).where(
            s.shops.c.tenant_id == tenant, s.shops.c.shop_id == t.shop_id)).scalar() or "")
    rep_name = ""
    if t.kind == "rep":
        rep_id = t.thread_id.rsplit(":", 1)[-1]
        rep_name = str(conn.execute(sa.select(s.reps.c.name).where(
            s.reps.c.tenant_id == tenant, s.reps.c.rep_id == rep_id)).scalar() or "")
    turns = [{"helper": r.helper, "skill": r.skill, "state": r.state}
             for r in conn.execute(sa.select(s.turns.c.helper, s.turns.c.skill,
                                             s.turns.c.state).where(
                 s.turns.c.tenant_id == tenant, s.turns.c.thread_id == t.thread_id)
                 .order_by(s.turns.c.created_ts, s.turns.c.turn_id))]
    meta = threadmeta.derive(kind=t.kind, thread_id=t.thread_id, shop_id=t.shop_id,
                             shop_name=shop_name, pending=int(pending or 0), turns=turns,
                             rep_name=rep_name)
    return {"thread_id": t.thread_id, "kind": t.kind, "shop_id": t.shop_id,
            "shop_name": shop_name, "last_at": t.last_at,
            "last_text": (last.text[:200] if last else ""),
            "last_sender": (last.sender if last else ""), "messages": int(n or 0),
            "pending_approvals": int(pending or 0), **meta}


def list_threads(engine: Engine, tenant: str, *, limit: int, offset: int,
                 kind: str = "") -> dict[str, Any]:
    with engine.connect() as conn:
        q = sa.select(s.threads).where(s.threads.c.tenant_id == tenant)
        cq = sa.select(sa.func.count()).select_from(s.threads).where(
            s.threads.c.tenant_id == tenant)
        if kind:
            q, cq = q.where(s.threads.c.kind == kind), cq.where(s.threads.c.kind == kind)
        rows = conn.execute(q.order_by(s.threads.c.last_ts.desc(), s.threads.c.thread_id)
                            .limit(limit).offset(offset)).all()
        total = int(conn.execute(cq).scalar() or 0)
        return {"threads": [_thread_summary(conn, tenant, t) for t in rows], "total": total}


PRIVATE_KEYS = frozenset({"private_text"})


def get_thread(engine: Engine, tenant: str, thread_id: str, *,
               uploads: bool = False) -> dict[str, Any]:
    """``uploads``: demo mode, where the owner's console attachments are kept
    and can be read back (``media`` on their message)."""
    with engine.connect() as conn:
        t = conn.execute(sa.select(s.threads).where(
            s.threads.c.tenant_id == tenant, s.threads.c.thread_id == thread_id)).first()
        if t is None:
            raise NotFound(thread_id)
        rows = list(conn.execute(sa.select(s.messages).where(
            s.messages.c.tenant_id == tenant, s.messages.c.thread_id == thread_id)
            .order_by(s.messages.c.ts, s.messages.c.message_id)))
        uploaded: set[str] = set()
        if uploads:
            own = f"{UPLOAD_PREFIX}{tenant}/"
            uploaded = {f"in:{r.external_id}" for r in conn.execute(
                sa.select(s.inbound_messages.c.external_id).where(
                    s.inbound_messages.c.tenant_id == tenant,
                    s.inbound_messages.c.thread_id == thread_id,
                    s.inbound_messages.c.media_ref.startswith(own, autoescape=True)))}
        msgs = [{
            "message_id": m.message_id, "direction": m.direction, "sender": m.sender,
            "channel": m.channel, "shop_id": m.shop_id, "text": m.text, "kind": m.kind,
            "status": m.status, "turn_id": m.turn_id, "simulated": bool(m.simulated),
            "at": m.at,
            # only a ready shop reply can have a voice note; skip the check for the rest
            "voice": (m.direction == "out" and m.channel == "shop_whatsapp"
                      and vx.reply_voice(conn, tenant, m) is not None),
            "media": m.message_id in uploaded} for m in rows]
        steps = [{
            "event_id": e.event_id, "turn_id": e.turn_id, "run_id": e.run_id, "type": e.type,
            "step": e.step, "tool": e.tool, "tier": e.tier, "short": e.short,
            "model": e.model, "cost_kobo": int(e.cost_kobo),
            "duration_ms": round(float(e.duration_ms), 3), "at": e.at}
            for e in conn.execute(sa.select(s.thread_events).where(
                s.thread_events.c.tenant_id == tenant,
                s.thread_events.c.thread_id == thread_id)
                .order_by(s.thread_events.c.ts, s.thread_events.c.seq,
                          s.thread_events.c.event_id))]
        rows_t = list(conn.execute(sa.select(s.turns).where(
            s.turns.c.tenant_id == tenant, s.turns.c.thread_id == thread_id)
            .order_by(s.turns.c.created_ts, s.turns.c.turn_id)))
        turns = [{
            "turn_id": r.turn_id, "state": r.state, "status": r.status, "helper": r.helper,
            "skill": r.skill, "run_id": r.run_id, "attempts": r.attempts,
            "simulated": bool(r.simulated), "created_at": r.created_at,
            "finished_at": r.finished_at} for r in rows_t]
        handoffs = [h for h in (turn_handoff(r) for r in rows_t) if h]
        summary = _thread_summary(conn, tenant, t)
    return {"thread": summary, "state": loads(t.state, {}), "messages": msgs,
            "steps": steps, "turns": turns, "handoffs": handoffs}


# ------------------------------------------------------------------ tasks


def list_tasks(engine: Engine, tenant: str, *, thread_id: str = "", helper: str = "",
               status: str = "", limit: int = 50) -> dict[str, Any]:
    """Each turn Mimi handed to a helper, newest first, with its task status
    (``working``, ``needs_you``, ``done``, ``handed_back``, ``failed``;
    docs/HANDOFFS.md). Merged turns are left out (their lead carries them)."""
    with engine.connect() as conn:
        q = sa.select(s.turns).where(s.turns.c.tenant_id == tenant,
                                     s.turns.c.state != "merged")
        if thread_id:
            q = q.where(s.turns.c.thread_id == thread_id)
        rows = list(conn.execute(q.order_by(s.turns.c.created_ts.desc(),
                                            s.turns.c.turn_id.desc()).limit(500)))
        pending = {r.turn_id: int(r.n) for r in conn.execute(
            sa.select(s.approvals.c.turn_id, sa.func.count().label("n")).where(
                s.approvals.c.tenant_id == tenant, s.approvals.c.status == "pending")
            .group_by(s.approvals.c.turn_id))}
    tasks: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for r in rows:
        ho = turn_handoff(r)
        if ho is None:
            continue
        res = loads(r.result, {})
        st = ho["status"]
        if st == "done" and pending.get(r.turn_id):
            st = "needs_you"  # its approval still waits (it was decided later otherwise)
        elif st == "needs_you" and r.state == "done" and not pending.get(r.turn_id) and (
                r.status == "parked_for_approval"):
            st = "done"  # the owner decided it since
        if helper and ho["to_helper"] != helper:
            continue
        counts[st] = counts.get(st, 0) + 1
        if status and st != status:
            continue
        if len(tasks) < limit:
            tasks.append({
                "task_id": ho["handoff_id"], "turn_id": r.turn_id, "thread_id": r.thread_id,
                "helper": ho["to_helper"], "helper_name": hx.title(ho["to_helper"]),
                "skill": ho["skill"], "status": st, "reason": ho["reason"], "handoff": ho,
                "handback": res.get("handback"), "card": res.get("card"),
                "chips": list(res.get("chips") or []), "created_at": r.created_at,
                "finished_at": r.finished_at})
    return {"tasks": tasks, "total": sum(counts.values()), "by_status": counts}


# -------------------------------------------------------------- approvals


def list_approvals(engine: Engine, tenant: str, *, status: str) -> dict[str, Any]:
    with engine.connect() as conn:
        q = sa.select(s.approvals).where(s.approvals.c.tenant_id == tenant)
        if status != "all":
            q = q.where(s.approvals.c.status == status)
        rows = conn.execute(q.order_by(s.approvals.c.created_ts.desc(),
                                       s.approvals.c.approval_id)).all()
        out = []
        for a in rows:
            draft = conn.execute(sa.select(s.messages.c.text, s.messages.c.channel,
                                           s.messages.c.status).where(
                s.messages.c.tenant_id == tenant,
                s.messages.c.idempotency_key == a.idempotency_key)).first()
            out.append(approval_dict(a, draft))
    return {"approvals": out, "total": len(out)}


# ----------------------------------------------------------------- credit


def credit(engine: Engine, tenant: str) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = _shop_rows(conn, tenant)
        reqs = conn.execute(sa.select(s.credit_requests).where(
            s.credit_requests.c.tenant_id == tenant)
            .order_by(s.credit_requests.c.created_at.desc())).all()
    limits = []
    for r in rows:
        lim, out = int(r.credit_limit_kobo), int(r.outstanding or 0)
        avail = max(0, lim - out)
        limits.append({
            "shop_id": r.shop_id, "name": r.name, "credit_limit_kobo": lim,
            "credit_limit_naira": naira(lim), "outstanding_kobo": out,
            "outstanding_naira": naira(out), "available_kobo": avail,
            "available_naira": naira(avail),
            "used_pct": (out * 100 // lim) if lim > 0 else 0})
    return {"limits": limits, "requests": [{
        "request_id": q.request_id, "shop_id": q.shop_id, "amount_kobo": int(q.amount_kobo),
        "amount_naira": naira(int(q.amount_kobo)), "reason": q.reason, "status": q.status,
        "approval_id": q.approval_id, "created_at": q.created_at} for q in reqs]}


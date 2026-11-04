"""The console's read queries. Every one is filtered by the caller's tenant.

Figures are summed here in integer kobo; ``*_naira`` strings are made from
them. An id that belongs to another distributor reads as not found.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi import owed
from mimi.api import display, threadmeta
from mimi.api import voice as vx
from mimi.api.agent import NotFound, approval_dict, turn_handoff
from mimi.api.uploads import PREFIX as UPLOAD_PREFIX
from mimi.flags import demo_wiring
from mimi.harness import handoff as hx
from mimi.harness.style import shop_text
from mimi.provenance import POLICY_VERSION, parse_witnesses, proof_word
from mimi.store import schema as s
from mimi.store.book import SqlGo
from mimi.store.util import loads, mask_phone
from mimi.tz import LAGOS


def naira(kobo: int) -> str:
    return owed.naira(kobo)


def ngn(kobo: int) -> int:
    """Whole naira (the demo's ``*_ngn`` fields)."""
    return int((int(kobo) + 50) // 100)


def payment_extras(p: dict[str, Any], shop: Any) -> dict[str, Any]:
    from mimi.api.demo_wiring import payment_extras as extras

    return extras(p, shop)


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


def _shop_summary(row: Any, book: owed.ShopOwed | None = None) -> dict[str, Any]:
    out, over = int(row.outstanding or 0), int(row.overdue or 0)
    n_open = int(row.open_n or 0)
    if book is not None:
        # One definition of owed and overdue for every surface (mimi.owed).
        out, over, n_open = book.owed_kobo, book.overdue_kobo, book.open_invoices
    return {
        "shop_id": row.shop_id, "name": row.name, "owner": row.owner, "area": row.area,
        "shop_type": row.shop_type, "size": row.size, "rep_id": row.rep_id,
        "phone_last4": mask_phone(row.phone),
        "credit_limit_kobo": int(row.credit_limit_kobo),
        "credit_limit_naira": naira(int(row.credit_limit_kobo)),
        "outstanding_kobo": out, "outstanding_naira": naira(out),
        "overdue_kobo": over, "overdue_naira": naira(over),
        "open_invoices": n_open, "simulated": bool(row.simulated),
        "credit_limit_ngn": ngn(int(row.credit_limit_kobo)), "outstanding_ngn": ngn(out),
        "overdue_ngn": ngn(over),
        "overdue_days": int(book.oldest_days_overdue) if book is not None and over > 0 else 0,
    }


_UNLABELLED = {"proof_level": "estimated", "proof": "estimated",
               "proof_policy_version": POLICY_VERSION}


def _proof(row: Any) -> dict[str, str]:
    """A record's proof level, its plain word and the rules version (RAI-295)."""
    return {"proof_level": row.proof_level,
            "proof": proof_word(row.proof_level, parse_witnesses(row.witnesses)),
            "proof_policy_version": row.proof_policy_version}


def list_shops(engine: Engine, tenant: str) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = _shop_rows(conn, tenant)
    book = owed.totals(SqlGo(engine).receivables(tenant, None)) if rows else owed.Owed()
    shops = [_shop_summary(r, book.shops.get(r.shop_id)) for r in rows]
    return {"shops": shops, "total": len(shops),
            "owed_total_kobo": book.owed_kobo, "owed_total_naira": naira(book.owed_kobo),
            "overdue_total_kobo": book.overdue_kobo,
            "overdue_total_naira": naira(book.overdue_kobo)}


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
                "line_total_kobo": int(ln.line_total_kobo),
                "expires_on": ln.expires_on, "batch": ln.batch or ""})
        disputes = [{"dispute_id": d.dispute_id, "delivery_id": d.delivery_id, "sku": d.sku,
                     "missing": d.missing, "text": d.text, "status": d.status}
                    for d in conn.execute(sa.select(s.disputes).where(
                        s.disputes.c.tenant_id == tenant, s.disputes.c.shop_id == shop_id))]
    # RAI-295: every payment and delivery carries its proof level and the
    # word the console shows beside the figure ("likely (rep and shop)").
    pays = go.payments(tenant, shop_id, 10)
    with engine.connect() as conn:
        proof = {p.payment_id: _proof(p) for p in conn.execute(
            sa.select(s.payments.c.payment_id, s.payments.c.proof_level,
                      s.payments.c.witnesses, s.payments.c.proof_policy_version)
            .where(s.payments.c.tenant_id == tenant,
                   s.payments.c.payment_id.in_([p["payment_id"] for p in pays])))}
    receivables = go.receivables(tenant, shop_id)
    one = owed.totals(receivables).shops.get(shop_id)
    for p in pays:
        p.update(proof.get(p["payment_id"], _UNLABELLED))
        p.update(payment_extras(p, one))
    for x in receivables:
        x["outstanding_naira"] = naira(x["outstanding_kobo"])
    return {
        "shop": _shop_summary(rows[0], one),
        "thread_id": f"{tenant}:shop:{shop_id}",
        "receivables": receivables,
        "payments": pays,
        "promises": go.promises(tenant, shop_id),
        "deliveries": [{
            "delivery_id": d.delivery_id, "delivered_on": d.delivered_on, "rep_id": d.rep_id,
            "total_kobo": int(d.total_kobo), "total_naira": naira(int(d.total_kobo)),
            "status": d.status, **_proof(d), "lines": lines.get(d.delivery_id, [])}
            for d in ds],
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
            "last_text": (display.humanize(last.text, display.names_for(conn, tenant))[:200]
                          if last else ""),
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
        names = display.names_for(conn, tenant)
        msgs = [{
            "message_id": m.message_id, "direction": m.direction, "sender": m.sender,
            "channel": m.channel, "shop_id": m.shop_id,
            # Names for ids and ₦ money (``mimi.api.display``); the record keeps
            # what was written.
            "text": (display.decided_line(m.text, names) if m.sender == "system"
                     else shop_text(display.humanize(m.text, names)) if (
                         m.direction == "out" and m.channel == "shop_whatsapp"
                         and demo_wiring())
                     else display.humanize(m.text, names)), "kind": m.kind,
            "status": m.status, "turn_id": m.turn_id, "simulated": bool(m.simulated),
            "at": m.at,
            # only a ready shop reply can have a voice note; skip the check for the rest
            "voice": (m.direction == "out" and m.channel == "shop_whatsapp"
                      and vx.reply_voice(conn, tenant, m) is not None),
            "media": m.message_id in uploaded} for m in rows]
        steps = [{
            "event_id": e.event_id, "turn_id": e.turn_id, "run_id": e.run_id, "type": e.type,
            "step": e.step, "tool": e.tool, "tier": e.tier,
            "short": display.humanize(e.short, names),
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


# ------------------------------------------------------------------ inbox


def _inbox_item(row: Any) -> dict[str, Any]:
    from mimi.api import inbox as ib

    chain = loads(row.chain, []) or []
    state = str(row.state)
    return {
        "task_id": row.task_id, "contract": ib.CONTRACT_VERSION,
        "from_helper": row.from_helper, "from_title": ib.title(row.from_helper),
        "to_helper": row.to_helper, "to_title": ib.title(row.to_helper),
        "skill": row.skill, "shop_id": row.shop_id, "shop_name": "",
        "ask": row.ask, "state": state, "reason": row.reason,
        "outcome": row.outcome or ib.outcome(state, ib.title(row.to_helper),
                                             result=row.result, reason=row.reason),
        "result": row.result, "chain": list(chain),
        "parent_task_id": row.parent_task_id, "thread_id": row.thread_id,
        "turn_id": row.turn_id, "created_at": row.created_at,
        "finished_at": row.finished_at}


def _intent_inbox_item(conn: sa.Connection, tenant: str, row: Any, sender: str,
                       shop_name: str) -> dict[str, Any]:
    """An applied handoff intent with no inbox row yet (Phase 1: the intents
    already stored become visible), rendered as a queued item."""
    from mimi.api import inbox as ib

    payload = loads(row.payload, {}) or {}
    to_helper = str(payload.get("helper") or "")
    ask = str(payload.get("task") or row.preview or "")
    from_helper = sender or "mimi"
    state = "queued"
    return {
        "task_id": ib.task_id_for(tenant, str(row.idempotency_key)),
        "contract": ib.CONTRACT_VERSION,
        "from_helper": from_helper, "from_title": ib.title(from_helper),
        "to_helper": to_helper, "to_title": ib.title(to_helper),
        "skill": "", "shop_id": str(row.shop_id), "shop_name": shop_name,
        "ask": ask, "state": state,
        "reason": hx.ack(to_helper, from_helper=from_helper) if to_helper else "",
        "outcome": ib.outcome(state, ib.title(to_helper)),
        "result": "", "chain": [from_helper, to_helper] if to_helper else [from_helper],
        "parent_task_id": "", "thread_id": str(row.thread_id), "turn_id": str(row.turn_id),
        "created_at": str(row.created_at), "finished_at": ""}


def list_inbox(engine: Engine, tenant: str, *, helper: str = "", state: str = "",
               limit: int = 50) -> dict[str, Any]:
    """Every handoff whose receiver is this helper (docs/HELPER-INBOX-CONTRACT.md).

    Newest first. Items come from ``helper_tasks``; an applied ``ask_helper``
    or ``delegate_task`` intent without a row yet is shown as a ``queued``
    item, so the intents already stored are visible too."""
    from mimi.api import inbox as ib

    with engine.connect() as conn:
        names = display.names_for(conn, tenant)
        shop_names = dict(names.shops)  # shop_id (lower) -> name
        rows = list(conn.execute(sa.select(s.helper_tasks).where(
            s.helper_tasks.c.tenant_id == tenant)))
        seen_keys = {str(r.source_key) for r in rows if r.source_key}
        items = [_inbox_item(r) for r in rows]
        # Phase 1: handoff intents already stored, with no inbox row yet.
        irows = list(conn.execute(sa.select(s.intents).where(
            s.intents.c.tenant_id == tenant,
            s.intents.c.tool.in_(tuple(ib.DELEGATE_TOOLS)),
            s.intents.c.outcome.not_in(("refused", "shadowed")))))
        turn_helpers = {str(t.turn_id): str(t.helper) for t in conn.execute(
            sa.select(s.turns.c.turn_id, s.turns.c.helper).where(
                s.turns.c.tenant_id == tenant,
                s.turns.c.turn_id.in_([str(r.turn_id) for r in irows if r.turn_id]
                                      or ["__none__"])))}
        for r in irows:
            if str(r.idempotency_key) in seen_keys:
                continue
            items.append(_intent_inbox_item(
                conn, tenant, r, turn_helpers.get(str(r.turn_id), ""),
                shop_names.get(str(r.shop_id).lower(), "")))
        for it in items:
            it["shop_name"] = it["shop_name"] or shop_names.get(
                str(it["shop_id"]).lower(), "")
        if helper:
            items = [i for i in items if i["to_helper"] == helper]
        by_state: dict[str, int] = {}
        for i in items:
            by_state[i["state"]] = by_state.get(i["state"], 0) + 1
        if state:
            items = [i for i in items if i["state"] == state]
        items.sort(key=lambda i: (i["created_at"], i["task_id"]), reverse=True)
        total = len(items)
        helpers = _inbox_helpers(conn, tenant, rows)
    return {"items": items[:limit], "total": total, "by_state": by_state,
            "helpers": helpers}


def _inbox_helpers(conn: sa.Connection, tenant: str,
                   rows: list[Any]) -> list[dict[str, Any]]:
    """The one sentence per helper the sidebar reads, from open items and turns
    (never a stored state word; docs/HELPER-INBOX-CONTRACT.md)."""
    from mimi.api import inbox as ib

    open_items: dict[str, int] = {}
    newest: dict[str, tuple[str, str]] = {}
    has_items: set[str] = set()
    for r in rows:
        st = str(r.state)
        who = str(r.to_helper)
        has_items.add(who)
        if st in ib.OPEN_STATES:
            open_items[who] = open_items.get(who, 0) + 1
        prev = newest.get(who)
        if prev is None or str(r.created_at) >= prev[0]:
            newest[who] = (str(r.created_at), str(r.outcome))
    running: dict[str, int] = dict(conn.execute(
        sa.select(s.turns.c.helper, sa.func.count()).where(
            s.turns.c.tenant_id == tenant,
            s.turns.c.state.in_(("queued", "leased")),
            s.turns.c.helper != "").group_by(s.turns.c.helper)).all())
    parked: dict[str, int] = dict(conn.execute(
        sa.select(s.turns.c.helper, sa.func.count()).where(
            s.turns.c.tenant_id == tenant, s.turns.c.state == "parked").group_by(
            s.turns.c.helper)).all())
    pending: dict[str, int] = dict(conn.execute(
        sa.select(s.approvals.c.helper, sa.func.count()).where(
            s.approvals.c.tenant_id == tenant, s.approvals.c.status == "pending")
        .group_by(s.approvals.c.helper)).all())
    out: list[dict[str, Any]] = []
    for name in sorted({*has_items, *running, *parked, *pending} - {""}):
        wait = int(parked.get(name, 0)) + int(pending.get(name, 0))
        opn = int(open_items.get(name, 0)) + int(running.get(name, 0))
        line = newest.get(name, ("", ""))[1]
        if not line:
            line = (ib.outcome("queued", ib.title(name)) if opn
                    else f"{ib.title(name)} is waiting on you")
        out.append({"helper": name, "helper_name": ib.title(name),
                    "open": opn, "waiting_on_owner": wait, "outcome": line})
    return out


# --------------------------------------------------------------- activity


# The Event types a row's step lines keep: the route, every tool and model
# call, and each reply draft. Everything else (budgets, claims, policy) is
# the worker's own business, not the row.
_ACTIVITY_STEP_TYPES = {
    "handoff": "route", "handback": "handback", "route": "route",
    "tool_called": "tool", "tool_result": "tool", "tool_refused": "tool",
    "model_called": "model", "draft_proposed": "reply", "card_read": "code",
    "step_finished": "tool",
}


def _activity_steps(conn: sa.Connection, tenant: str, turn_id: str) -> list[dict[str, Any]]:
    """The turn's step lines, oldest first, in plain words (no model text)."""
    rows = conn.execute(sa.select(s.thread_events).where(
        s.thread_events.c.tenant_id == tenant,
        s.thread_events.c.turn_id == turn_id,
        s.thread_events.c.type.in_(list(_ACTIVITY_STEP_TYPES)))
        .order_by(s.thread_events.c.ts, s.thread_events.c.seq,
                  s.thread_events.c.event_id)).all()
    names = display.names_for(conn, tenant)
    out: list[dict[str, Any]] = []
    for e in rows:
        kind = _ACTIVITY_STEP_TYPES[str(e.type)]
        if kind == "route":
            name, short = "route", str(e.short or "")
        elif kind == "handback":
            name, short = "handback", str(e.short or "")
        elif kind == "model":
            name = "model"
            calls = str(e.short or "")
            short = f"model call: {calls}" if calls and calls != "final" else "model call"
        elif kind == "reply":
            name, short = "reply", display.humanize(str(e.short or ""), names)
        else:
            name = str(e.tool or "step")
            short = display.humanize(str(e.short or ""), names) or name.replace("_", " ")
        st = "failed" if str(e.type) in ("tool_refused", "claim_blocked") else "ok"
        out.append({"name": name, "kind": kind, "status": st, "short": short[:300],
                    "ms": round(float(e.duration_ms or 0.0), 3), "at": str(e.at)})
    return out


def _activity_at(created_at: str, created_ts: float) -> str:
    """When the turn started, on the Lagos clock with its zone."""
    from datetime import UTC, datetime

    try:
        dt = datetime.fromisoformat(str(created_at))
    except ValueError:
        dt = datetime.fromtimestamp(float(created_ts or 0.0), UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(LAGOS).isoformat()


def list_activity(engine: Engine, tenant: str, *,
                  tab: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """The console's Activity view, newest first: every turn Mimi handed to a
    helper, with what it is doing in plain words, who it is for, when it
    started (Africa/Lagos) and its step lines. ``tab`` is one of ``running``
    (working), ``waiting`` (needs_you) or ``done`` (done, handed_back,
    failed); empty means every row. Merged turns are left out (their lead
    carries them)."""
    with engine.connect() as conn:
        q = sa.select(s.turns).where(s.turns.c.tenant_id == tenant,
                                     s.turns.c.state != "merged")
        rows = list(conn.execute(q.order_by(s.turns.c.created_ts.desc(),
                                            s.turns.c.turn_id.desc()).limit(500)))
        pending = {r.turn_id: int(r.n) for r in conn.execute(
            sa.select(s.approvals.c.turn_id, sa.func.count().label("n")).where(
                s.approvals.c.tenant_id == tenant, s.approvals.c.status == "pending")
            .group_by(s.approvals.c.turn_id))}
        names = display.names_for(conn, tenant)
        shop_of = dict(conn.execute(sa.select(
            s.threads.c.thread_id, s.threads.c.shop_id).where(
                s.threads.c.tenant_id == tenant)).all())
        out: list[dict[str, Any]] = []
        counts = {"running": 0, "waiting": 0, "done": 0}
        for r in rows:
            ho = turn_handoff(r)
            if ho is None:
                continue
            st = ho["status"]
            if st == "done" and pending.get(r.turn_id):
                st = "needs_you"  # its approval still waits
            elif st == "needs_you" and r.state == "done" and not pending.get(
                    r.turn_id) and r.status == "parked_for_approval":
                st = "done"  # the owner decided it since
            atab = "running" if st == "working" else "waiting" if st == "needs_you" else "done"
            counts[atab] += 1
            if tab and atab != tab:
                continue
            if len(out) >= offset + limit:
                continue
            shop_id = str(shop_of.get(r.thread_id, "") or "")
            desc = display.humanize(str(ho.get("reason") or ""), names)
            skill = str(ho.get("skill") or "")
            if skill:
                about = threadmeta.skill_title(skill)
                shop_name = names.shops.get(shop_id.lower(), "")
                who = shop_name or (shop_id if shop_id.startswith("shop-") else "A shop")
                desc = f"{hx.title(str(ho['to_helper']))} is working on {about} for {who}"
            steps = _activity_steps(conn, tenant, r.turn_id)
            out.append({
                "task_id": ho["handoff_id"], "turn_id": r.turn_id, "thread_id": r.thread_id,
                "helper": ho["to_helper"], "helper_name": hx.title(ho["to_helper"]),
                "skill": skill, "status": st, "tab": atab,
                "what": (desc or f"{hx.title(str(ho['to_helper']))} is working")[:200],
                "shop_name": names.shops.get(shop_id.lower(), ""),
                "started_at": _activity_at(str(r.created_at), float(r.created_ts or 0.0)),
                "finished_at": r.finished_at, "steps": steps,
                "pending_approvals": int(pending.get(r.turn_id) or 0)})
        total = sum(counts.values())
        return {"rows": out[offset:offset + limit], "total": total, "by_tab": counts}


# -------------------------------------------------------------- approvals


def list_approvals(engine: Engine, tenant: str, *, status: str) -> dict[str, Any]:
    with engine.connect() as conn:
        q = sa.select(s.approvals).where(s.approvals.c.tenant_id == tenant)
        if status != "all":
            q = q.where(s.approvals.c.status == status)
        rows = conn.execute(q.order_by(s.approvals.c.created_ts.desc(),
                                       s.approvals.c.approval_id)).all()
        out = []
        names = display.names_for(conn, tenant)
        for a in rows:
            draft = conn.execute(sa.select(s.messages.c.text, s.messages.c.channel,
                                           s.messages.c.status).where(
                s.messages.c.tenant_id == tenant,
                s.messages.c.idempotency_key == a.idempotency_key)).first()
            out.append(approval_dict(a, draft, names))
    return {"approvals": out, "total": len(out)}


# ----------------------------------------------------------------- credit


def credit(engine: Engine, tenant: str) -> dict[str, Any]:
    from mimi.api.demo_wiring import overdue_by_shop, overdue_fields, recent_payments

    book = overdue_by_shop(engine, tenant)
    with engine.connect() as conn:
        rows = _shop_rows(conn, tenant)
        reqs = conn.execute(sa.select(s.credit_requests).where(
            s.credit_requests.c.tenant_id == tenant)
            .order_by(s.credit_requests.c.created_at.desc())).all()
    limits = []
    for r in rows:
        lim, out = int(r.credit_limit_kobo), int(r.outstanding or 0)
        avail = max(0, lim - out)
        over = max(0, out - lim) if lim > 0 else 0
        limits.append({
            "shop_id": r.shop_id, "name": r.name, "credit_limit_kobo": lim,
            "credit_limit_naira": naira(lim), "outstanding_kobo": out,
            "outstanding_naira": naira(out), "available_kobo": avail,
            "available_naira": naira(avail),
            "used_pct": (out * 100 // lim) if lim > 0 else 0,
            # Owes more than its limit (30 Oct 2026 run 2: Mama Sola owed
            # ₦3,497,300 on a ₦1,700,000 limit and nothing said so).
            "over_limit": over > 0, "over_limit_by_kobo": over,
            "over_limit_by_naira": naira(over) if over else "",
            "credit_limit_ngn": ngn(lim), "outstanding_ngn": ngn(out),
            **overdue_fields(book.get(r.shop_id))})
    return {"limits": limits, "requests": [{
        "request_id": q.request_id, "shop_id": q.shop_id, "amount_kobo": int(q.amount_kobo),
        "amount_naira": naira(int(q.amount_kobo)), "reason": q.reason, "status": q.status,
        "approval_id": q.approval_id, "created_at": q.created_at} for q in reqs],
        "payments": recent_payments(engine, tenant)}



# ------------------------------------------------------------------ mentions

# The owner's @ picker. Helpers the owner can assign work to, then shops and
# reps from the book. Mimi is not a row: she is the thread itself.
HELPER_SUBTITLE: dict[str, str] = {
    "collector": "who owes and reminders",
    "credit": "credit limits and requests",
    "order": "shop orders and deliveries",
    "stock": "stock levels and low stock",
    "proof": "payment proofs",
    "brand": "promos and brand news",
}
MENTION_KINDS = ("helper", "shop", "rep")


def _matches(q: str, *names: str) -> int:
    """0 no match, 2 the name starts with q, 1 a later word starts with q."""
    if not q:
        return 2
    best = 0
    for n in names:
        low = n.lower()
        if low.startswith(q):
            return 2
        if any(w.startswith(q) for w in low.replace("&", " ").split()):
            best = 1
    return best


def mentions(engine: Engine, tenant: str, *, q: str = "", kinds: str = "",
             limit: int = 50) -> dict[str, Any]:
    from mimi.harness.skills import HELPER_TITLE

    want = {k.strip() for k in kinds.split(",") if k.strip()} or set(MENTION_KINDS)
    query = q.strip().lstrip("@").strip().lower()
    rows: list[tuple[int, int, dict[str, Any]]] = []
    if "helper" in want:
        for i, (h, sub) in enumerate(HELPER_SUBTITLE.items()):
            title = HELPER_TITLE.get(h, h.title())
            score = _matches(query, title, h)
            if score:
                rows.append((score, i, {"kind": "helper", "id": h, "name": title,
                                        "subtitle": sub, "mention": f"@{title}",
                                        "assign_to": h}))
    with engine.connect() as conn:
        reps = conn.execute(sa.select(s.reps.c.rep_id, s.reps.c.name, s.reps.c.areas)
                            .where(s.reps.c.tenant_id == tenant)
                            .order_by(s.reps.c.name)).all()
        rep_name = {r.rep_id: r.name for r in reps}
        if "shop" in want:
            shops = conn.execute(sa.select(s.shops.c.shop_id, s.shops.c.name, s.shops.c.area,
                                           s.shops.c.rep_id)
                                 .where(s.shops.c.tenant_id == tenant)
                                 .order_by(s.shops.c.name)).all()
            for i, sh in enumerate(shops):
                score = _matches(query, sh.name)
                if score:
                    sub = ", ".join(x for x in (sh.area, rep_name.get(sh.rep_id, "")) if x)
                    rows.append((score, 100 + i, {"kind": "shop", "id": sh.shop_id,
                                                  "name": sh.name, "subtitle": sub,
                                                  "mention": sh.name, "assign_to": ""}))
        if "rep" in want:
            for i, r in enumerate(reps):
                score = _matches(query, r.name)
                if score:
                    areas = loads(r.areas) if r.areas else []
                    sub = ", ".join(str(a) for a in areas) if isinstance(areas, list) else ""
                    rows.append((score, 10_000 + i, {"kind": "rep", "id": r.rep_id,
                                                     "name": r.name, "subtitle": sub,
                                                     "mention": r.name, "assign_to": ""}))
    rows.sort(key=lambda x: (-x[0], x[1]))
    out = [r for _, _, r in rows]
    return {"mentions": out[:limit], "total": len(out)}

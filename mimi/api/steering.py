"""The agent's side of steering (RAI-301): open tasks, follow-up turns, versions.

``mimi.harness.steer`` splits a message into parts and labels them; this
module is what the API agent does with the result, against the store:

* ``open_tasks``: the work on a thread a later message may refer to, read
  from the store (pending approvals, items decided in the last 48 hours,
  receipts, payment promises and proofs recorded in the last 48 hours).
* ``insert_followup``: one part of a message as its own turn, queued right
  after the turn it came from (``turns.solo``: never merged).
* ``supersede`` and ``withdraw``: a compare-and-set on ``status = pending``.
  A decided approval is never touched.

Nothing here moves money or writes the book.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.clock import wall
from mimi.harness.inbound import Inbound, Media, Payload
from mimi.harness.steer import OpenTask, Part, task_summary
from mimi.store import schema as s
from mimi.store.queue import turn_id_for
from mimi.store.util import dumps, insert_ignore, loads, now_iso

RECENT_S = 48 * 3600
# Applied records a shop may still correct ("actually 1 carton short").
CORRECTABLE = ("record_delivery_consent", "set_followup", "record_payment_proof",
               "flag_delivery_to_owner")
# A new version of these replaces a pending one of the same family.
FAMILY: dict[str, tuple[str, ...]] = {
    "draft_restock": ("draft_restock", "revise_restock"),
    "revise_restock": ("draft_restock", "revise_restock"),
    "submit_credit_review": ("submit_credit_review",),
    # Order intake (RAI-302): one pending order per shop, versioned.
    "submit_order": ("submit_order", "revise_order"),
    "revise_order": ("submit_order", "revise_order"),
}
# Tools whose intent is always a new version of a pending one.
REVISIONS: frozenset[str] = frozenset({"revise_restock", "revise_order"})
TOOL_SKILL: dict[str, tuple[str, str]] = {
    "draft_restock": ("stock", "stock/stock-count"),
    "revise_restock": ("stock", "stock/stock-count"),
    "submit_credit_review": ("credit", "credit/credit-request"),
    "submit_order": ("order", "order/order-intake"),
    "revise_order": ("order", "order/order-intake"),
    "set_followup": ("collector", "collector/promise-to-pay"),
    "record_delivery_consent": ("order", "order/drop-confirmation"),
    "flag_delivery_to_owner": ("order", "order/drop-confirmation"),
    "flag_payment_proof": ("proof", "proof/fake-alert-check"),
    "record_payment_proof": ("proof", "proof/fake-alert-check"),
}


def _ts(iso: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def open_tasks(engine: Engine | sa.Connection, tenant_id: str, thread_id: str, shop_id: str,
               *, now: float | None = None, limit: int = 5) -> list[OpenTask]:
    """The thread's open work, oldest first, numbered t1, t2, ... (code only)."""
    now = wall() if now is None else now
    rows: list[tuple[float, OpenTask]] = []

    def read(conn: sa.Connection) -> None:
        cond = [s.approvals.c.tenant_id == tenant_id]
        cond.append(sa.or_(s.approvals.c.thread_id == thread_id,
                           s.approvals.c.shop_id == shop_id) if shop_id
                    else s.approvals.c.thread_id == thread_id)
        seen: set[str] = set()
        for a in conn.execute(sa.select(s.approvals).where(*cond)):
            if a.status in ("superseded", "withdrawn"):
                continue
            decided_ts = _ts(a.decided_at) if a.decided_at else 0.0
            if a.status != "pending" and now - decided_ts > RECENT_S:
                continue
            helper, skill = TOOL_SKILL.get(a.tool, (a.helper, a.skill))
            rows.append((float(a.created_ts), OpenTask(
                tid="", tool=a.tool, status=a.status, helper=a.helper or helper,
                skill=a.skill or skill, summary=task_summary(a.tool, a.status,
                                                             loads(a.payload, {})),
                shop_id=a.shop_id, approval_id=a.approval_id, key=a.idempotency_key,
                at=a.created_at)))
            seen.add(a.idempotency_key)
        if shop_id:
            for i in conn.execute(sa.select(s.intents).where(
                    s.intents.c.tenant_id == tenant_id, s.intents.c.shop_id == shop_id,
                    s.intents.c.tool.in_(CORRECTABLE), s.intents.c.outcome == "applied")):
                at = _ts(i.created_at)
                if i.idempotency_key in seen or now - at > RECENT_S:
                    continue
                helper, skill = TOOL_SKILL[i.tool]
                rows.append((at, OpenTask(
                    tid="", tool=i.tool, status="applied", helper=helper, skill=skill,
                    summary=task_summary(i.tool, "applied", loads(i.payload, {})),
                    shop_id=i.shop_id, key=i.idempotency_key, at=i.created_at)))

    if isinstance(engine, sa.Connection):
        read(engine)
    else:
        with engine.connect() as conn:
            read(conn)
    rows.sort(key=lambda r: (r[0], r[1].key))
    rows = rows[-limit:]
    return [OpenTask(**{**t.__dict__, "tid": f"t{n + 1}"}) for n, (_, t) in enumerate(rows)]


def task_json(t: OpenTask) -> dict[str, Any]:
    return dict(t.__dict__)


def task_from(d: dict[str, Any]) -> OpenTask:
    return OpenTask(**{k: d.get(k, "") for k in OpenTask.__dataclass_fields__})


def followup_inbound(parent: Inbound, events: Sequence[Inbound], parts: Sequence[Part],
                     pids: Sequence[str], steer_label: str) -> Inbound:
    """One part (or parts) of a message as its own inbound, modality kept."""
    want = [p for p in parts if p.pid in set(pids)]
    by_ext = {e.external_id: e for e in events}
    media = Media()
    texts: list[str] = []
    voice: list[str] = []
    for p in want:
        src = by_ext.get(p.external_id, parent)
        if p.is_media and media.kind == "none":
            media = src.payload.media.model_copy(update={"caption": p.text})
        elif p.kind == "voice":
            voice.append(p.text)
            if media.kind == "none":
                media = src.payload.media.model_copy(update={"transcript": "", "caption": ""})
        elif p.text:
            texts.append(p.text)
    if voice:
        if media.kind == "voice":
            media = media.model_copy(update={"transcript": " ".join(voice)})
        else:
            texts.extend(voice)
    pay = parent.payload
    return Inbound(
        source=parent.source, org_id=parent.org_id,
        external_id=f"{parent.external_id}#{want[0].pid if want else 'p'}",
        received_at=parent.received_at, thread_id=parent.thread_id,
        payload=Payload(text=" ".join(texts), from_phone=pay.from_phone, media=media,
                        shop_id=pay.shop_id, part_of=parent.external_id, steer=steer_label))


def insert_followup(conn: sa.Connection, tenant_id: str, thread_id: str, inbound: Inbound, *,
                    after_ts: float, order: int, shop_id: str, simulated: bool,
                    media_ref: str = "") -> str:
    """Queue a follow-up turn right after ``after_ts`` (before any later message)."""
    turn_id = turn_id_for(tenant_id, inbound.external_id)
    if not insert_ignore(conn, s.inbound_messages, {
            "tenant_id": tenant_id, "external_id": inbound.external_id, "turn_id": turn_id,
            "thread_id": thread_id, "source": inbound.source,
            "envelope": inbound.model_dump_json(),
            "media_ref": media_ref if inbound.payload.media.kind != "none" else "",
            "simulated": simulated, "received_at": inbound.received_at.isoformat()}):
        return turn_id  # a re-run: already queued once
    created = after_ts + 1e-4 * (order + 1)
    conn.execute(s.turns.insert().values(
        turn_id=turn_id, tenant_id=tenant_id, thread_id=thread_id, state="queued",
        status="", helper="", skill="", version=0, attempts=0, completions=0, batch="[]",
        checkpoint="{}", result=dumps({"part_of": inbound.payload.part_of}), error="",
        stop=False, simulated=simulated, solo=True, created_at=now_iso(created),
        created_ts=created, finished_at=None))
    return turn_id


def media_ref_of(conn: sa.Connection, tenant_id: str, external_id: str) -> str:
    return str(conn.execute(sa.select(s.inbound_messages.c.media_ref).where(
        s.inbound_messages.c.tenant_id == tenant_id,
        s.inbound_messages.c.external_id == external_id)).scalar() or "")


def withdraw(conn: sa.Connection, tenant_id: str, approval_id: str, *, by: str) -> bool:
    """Withdraw a pending approval (the shop cancelled it). Never a decided one."""
    a = conn.execute(sa.select(s.approvals).where(
        s.approvals.c.tenant_id == tenant_id,
        s.approvals.c.approval_id == approval_id)).first()
    if a is None:
        return False
    res = conn.execute(s.approvals.update().where(
        s.approvals.c.approval_id == approval_id, s.approvals.c.status == "pending")
        .values(status="withdrawn", note=f"withdrawn by the shop ({by})"[:2000],
                decided_at=now_iso()))
    if res.rowcount != 1:
        return False
    _retire(conn, tenant_id, a.idempotency_key, "withdrawn")
    conn.execute(s.credit_requests.update().where(
        s.credit_requests.c.tenant_id == tenant_id,
        s.credit_requests.c.approval_id == approval_id).values(status="withdrawn"))
    return True


def supersede(conn: sa.Connection, tenant_id: str, old_id: str, new_id: str) -> bool:
    """The new version replaces the old pending one. Never a decided one."""
    old = conn.execute(sa.select(s.approvals).where(
        s.approvals.c.tenant_id == tenant_id, s.approvals.c.approval_id == old_id)).first()
    if old is None or old_id == new_id:
        return False
    res = conn.execute(s.approvals.update().where(
        s.approvals.c.approval_id == old_id, s.approvals.c.status == "pending")
        .values(status="superseded", superseded_by=new_id,
                note=f"replaced by {new_id} (the shop corrected it)", decided_at=now_iso()))
    if res.rowcount != 1:
        return False
    conn.execute(s.approvals.update().where(s.approvals.c.approval_id == new_id)
                 .values(supersedes=old_id))
    _retire(conn, tenant_id, old.idempotency_key, "superseded")
    return True


def _retire(conn: sa.Connection, tenant_id: str, key: str, outcome: str) -> None:
    conn.execute(s.intents.update().where(
        s.intents.c.tenant_id == tenant_id, s.intents.c.idempotency_key == key,
        s.intents.c.outcome == "queued_for_approval").values(outcome=outcome))
    conn.execute(s.messages.update().where(
        s.messages.c.tenant_id == tenant_id, s.messages.c.idempotency_key == key,
        s.messages.c.status == "pending_approval").values(status=outcome))


def pending_of_family(conn: sa.Connection, tenant_id: str, shop_id: str, tool: str,
                      sku: str = "") -> list[Any]:
    rows = conn.execute(sa.select(s.approvals).where(
        s.approvals.c.tenant_id == tenant_id, s.approvals.c.shop_id == shop_id,
        s.approvals.c.tool.in_(FAMILY.get(tool, (tool,))),
        s.approvals.c.status == "pending")).all()
    out = list(rows)
    if sku:
        out = [r for r in out if loads(r.payload, {}).get("sku") == sku]
    return out


def tenant_name(conn: sa.Connection, tenant_id: str) -> str:
    name = conn.execute(sa.select(s.tenants.c.name).where(
        s.tenants.c.tenant_id == tenant_id)).scalar()
    return str(name or "the distributor")


def parts_json(parts: Sequence[Part]) -> list[dict[str, Any]]:
    return [dict(p.__dict__) for p in parts]


def parts_from(rows: Sequence[dict[str, Any]]) -> list[Part]:
    return [Part(**r) for r in rows]


def dumps_compact(x: Any) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"), default=str)

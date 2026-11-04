"""The returns ledger on the API (feat/returns-and-exposure): code, no model.

* ``open_return`` backs ``POST /v1/returns``: a return opens from an approved
  slow-stock *pickup* option (its shop, its lines, its reason, and the
  quantities and naira all computed in code), or manually with named lines.
  Opening twice from the same pickup returns the one draft (idempotent).
* ``approve``, ``collect``, ``inspect``, ``settle`` and ``cancel`` move it
  through ``drafted``, ``approved``, ``collected``, ``inspected``, ``settled``
  (or ``cancelled``), each move written to ``return_events``. The rep collects:
  Mimi never moves stock or money.
* ``settle`` opens a ``return_credit`` approval (pending) carrying what would
  come off the shop's open invoices and what would sit on its account because
  it owed nothing. Approving it applies the credit (``apply_credit``), undo
  reverses it (``undo_credit``), exactly like the Zoho payment card.
* ``settlement`` and the list read are tenant-filtered; every person-read
  string is built by ``mimi.returns.ledger`` and scrubbed of internal ids.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi import owed
from mimi.api.agent import Conflict, NotFound
from mimi.clock import wall
from mimi.returns import ledger
from mimi.stock.items import display_name, unit_word
from mimi.store import schema as s
from mimi.store.book import approval_id_for
from mimi.store.util import dumps, insert_ignore, loads, now_iso

SLOW_KIND = "slow_stock"
RETURN_CREDIT_KIND = ledger.CREDIT_KIND
SLOW_SKILL = "stock/slow-check"


# ------------------------------------------------------------- helpers


def _shop_name(conn: sa.Connection, tenant: str, shop_id: str) -> str:
    name = conn.execute(sa.select(s.shops.c.name).where(
        s.shops.c.tenant_id == tenant, s.shops.c.shop_id == shop_id)).scalar()
    if name is None:
        raise NotFound(shop_id)
    return str(name)


def _sku_row(conn: sa.Connection, tenant: str, sku: str) -> Any:
    return conn.execute(sa.select(s.skus).where(
        s.skus.c.tenant_id == tenant, s.skus.c.sku == sku)).first()


def _invoice_batch(conn: sa.Connection, tenant: str, shop_id: str, sku: str,
                   expires_on: str = "") -> tuple[str, str, str]:
    """(expires_on, batch, source) from the distributor's own invoice lines.

    The pickup plan names the expiry the goods are being picked up for (the
    earliest expiry among the units left, read off the delivery notes). When it
    does, the batch comes from the delivery line that carries that same expiry,
    so the return line refers to the delivery those goods came in on rather
    than whichever note came last. If no line carries that expiry, the plan's
    date stands and the batch is the newest note's; with no plan expiry (a
    manual return, or a plan that named none), the newest delivery line for the
    shop and item is used. Source is ``invoice`` whenever a note carried an
    expiry, else ``estimated``."""
    def note(with_expiry: str = "") -> Any:
        q = (sa.select(s.delivery_lines.c.expires_on, s.delivery_lines.c.batch)
             .join(s.deliveries, sa.and_(
                 s.deliveries.c.tenant_id == s.delivery_lines.c.tenant_id,
                 s.deliveries.c.delivery_id == s.delivery_lines.c.delivery_id))
             .where(s.delivery_lines.c.tenant_id == tenant,
                    s.deliveries.c.shop_id == shop_id,
                    s.delivery_lines.c.sku == sku))
        if with_expiry:
            q = q.where(s.delivery_lines.c.expires_on == with_expiry)
        return conn.execute(q.order_by(s.deliveries.c.day.desc()).limit(1)).first()

    newest = note()
    if expires_on:
        same = note(expires_on)
        batch = same.batch if same is not None else (
            newest.batch if newest is not None else "")
        return expires_on, str(batch or ""), "invoice"
    if newest is None or not newest.expires_on:
        return "", str((newest.batch or "") if newest is not None else ""), "estimated"
    return str(newest.expires_on), str(newest.batch or ""), "invoice"


def _load(conn: sa.Connection, tenant: str, return_id: str) -> Any:
    r = conn.execute(sa.select(s.returns).where(
        s.returns.c.tenant_id == tenant, s.returns.c.return_id == return_id)).first()
    if r is None:
        raise NotFound(return_id)
    return r


def _line_rows(conn: sa.Connection, tenant: str, return_id: str) -> list[Any]:
    return list(conn.execute(sa.select(s.return_lines).where(
        s.return_lines.c.tenant_id == tenant, s.return_lines.c.return_id == return_id)
        .order_by(s.return_lines.c.line_no)))


def _event_rows(conn: sa.Connection, tenant: str, return_id: str) -> list[Any]:
    return list(conn.execute(sa.select(s.return_events).where(
        s.return_events.c.tenant_id == tenant, s.return_events.c.return_id == return_id)
        .order_by(s.return_events.c.seq)))


def _record(conn: sa.Connection, tenant: str, return_id: str, status: str, *,
            by: str, note: str, now: float) -> None:
    seq = len(_event_rows(conn, tenant, return_id))
    insert_ignore(conn, s.return_events, {
        "tenant_id": tenant, "event_id": f"re-{return_id}-{seq}", "return_id": return_id,
        "seq": seq, "status": status, "by": by[:100], "note": ledger.scrub(note),
        "at": now_iso(now)})


def _touch(conn: sa.Connection, tenant: str, return_id: str, now: float,
           **values: Any) -> None:
    conn.execute(s.returns.update().where(
        s.returns.c.tenant_id == tenant, s.returns.c.return_id == return_id)
        .values(updated_at=now_iso(now), **values))


def _move(conn: sa.Connection, tenant: str, r: Any, to: str, *, by: str, note: str,
          now: float, **values: Any) -> None:
    if not ledger.can_move(str(r.status), to):
        raise Conflict(f"a return that is {r.status} cannot become {to}")
    _touch(conn, tenant, str(r.return_id), now, status=to, **values)
    _record(conn, tenant, str(r.return_id), to, by=by, note=note, now=now)


def _pickup_cost(conn: sa.Connection, tenant: str) -> int | None:
    v = conn.execute(sa.select(s.returns_settings.c.pickup_cost_kobo).where(
        s.returns_settings.c.tenant_id == tenant)).scalar()
    return None if v is None else int(v)


def _return_credit_kobo(rows: Sequence[Any]) -> int:
    return sum(int(ln.value_returned_kobo or 0) for ln in rows)


def _saving(conn: sa.Connection, tenant: str, rows: Sequence[Any]) -> tuple[int | None, int]:
    recovered = ledger.recovered_kobo([
        {"disposition": ln.disposition, "value_returned_kobo": ln.value_returned_kobo}
        for ln in rows])
    return ledger.saving_kobo(recovered, _pickup_cost(conn, tenant)), recovered


def _credit_approval(conn: sa.Connection, tenant: str, r: Any) -> Any | None:
    """The return's credit card when it is still live: waiting for the owner
    (``pending``), or approved and actually applied (it carries the credit note
    it wrote).

    A rejected, withdrawn or superseded card is not the return's credit any
    more, so the read says nothing is waiting and a fresh settle is allowed.
    Neither is an approved card with no credit note behind it: that is a card
    whose application failed after the approval was recorded (``Agent.decide``
    commits the approval, then the application runs), and the way out is to
    settle again rather than to be stuck with a card that cannot be undone and
    cannot be approved twice."""
    if not r.credit_approval_id:
        return None
    a = conn.execute(sa.select(s.approvals).where(
        s.approvals.c.tenant_id == tenant,
        s.approvals.c.approval_id == r.credit_approval_id)).first()
    if a is None:
        return None
    if a.status == "pending":
        return a
    if a.status == "approved":
        applied = (loads(a.payload, {}).get("result") or {}).get("credit_id")
        if applied:
            return a
    return None


def _settle_attempts(conn: sa.Connection, tenant: str, return_id: str) -> int:
    """How many credit cards this return has drafted, whatever became of them.

    Counted from the cards themselves, not from applied credits: a card the
    owner rejected never wrote a credit note, and reusing its key would hand
    back the rejected card and settle nothing (see ``settle``)."""
    n = 0
    for row in conn.execute(sa.select(s.approvals.c.payload).where(
            s.approvals.c.tenant_id == tenant,
            s.approvals.c.tool == RETURN_CREDIT_KIND)):
        if str(loads(row.payload, {}).get("return_id") or "") == return_id:
            n += 1
    return n


# ------------------------------------------------------------- the read


def line_dict(ln: Any) -> dict[str, Any]:
    return {
        "line_no": int(ln.line_no), "sku": ln.sku, "item": display_name(ln.item),
        "unit": unit_word(ln.unit, 2), "reason": ln.reason,
        "quantity_drafted": int(ln.quantity_drafted),
        "quantity_collected": (None if ln.quantity_collected is None
                               else int(ln.quantity_collected)),
        "quantity_returned": (None if ln.quantity_returned is None
                              else int(ln.quantity_returned)),
        "unit_price_kobo": int(ln.unit_price_kobo),
        "value_drafted_kobo": int(ln.value_drafted_kobo),
        "value_returned_kobo": (None if ln.value_returned_kobo is None
                                else int(ln.value_returned_kobo)),
        "condition": ln.condition, "disposition": ln.disposition,
        "expires_on": ln.expires_on, "batch": ln.batch or "",
        "expiry_source": ln.expiry_source,
    }


def return_dict(conn: sa.Connection, tenant: str, r: Any) -> dict[str, Any]:
    rows = _line_rows(conn, tenant, str(r.return_id))
    credit = conn.execute(sa.select(s.credit_notes).where(
        s.credit_notes.c.tenant_id == tenant, s.credit_notes.c.return_id == r.return_id,
        s.credit_notes.c.undone_at.is_(None))).first()
    card = _credit_approval(conn, tenant, r)
    return {
        "return_id": r.return_id, "shop_id": r.shop_id,
        "shop_name": _shop_name(conn, tenant, str(r.shop_id)),
        "source": r.source, "approval_id": r.approval_id, "reason": r.reason,
        "status": r.status, "lines": [line_dict(ln) for ln in rows],
        "value_drafted_kobo": int(r.value_drafted_kobo),
        "value_collected_kobo": int(r.value_collected_kobo),
        "value_returned_kobo": int(r.value_returned_kobo),
        "recovered_kobo": int(r.recovered_kobo),
        "pickup_cost_kobo": (None if r.pickup_cost_kobo is None
                             else int(r.pickup_cost_kobo)),
        "saving_kobo": None if r.saving_kobo is None else int(r.saving_kobo),
        "saving_basis": ledger.saving_basis(int(r.recovered_kobo),
                                            None if r.pickup_cost_kobo is None
                                            else int(r.pickup_cost_kobo)),
        "credit_kobo": (None if card is None or r.credit_kobo is None
                        else int(r.credit_kobo)),
        "credit_approval_id": None if card is None else str(r.credit_approval_id),
        "credit_applied_kobo": (int(credit.applied_kobo) if credit is not None else None),
        "credit_on_account_kobo": (int(credit.on_account_kobo) if credit is not None
                                   else None),
        "transitions": [{"status": e.status, "at": e.at, "by": e.by, "note": e.note}
                        for e in _event_rows(conn, tenant, str(r.return_id))],
        "created_at": r.created_at, "updated_at": r.updated_at,
    }


def list_returns(engine: Engine, tenant: str, *, status: str = "", shop_id: str = ""
                 ) -> dict[str, Any]:
    with engine.connect() as conn:
        q = sa.select(s.returns).where(s.returns.c.tenant_id == tenant)
        if status:
            q = q.where(s.returns.c.status == status)
        if shop_id:
            q = q.where(s.returns.c.shop_id == shop_id)
        rows = conn.execute(q.order_by(s.returns.c.created_ts.desc(),
                                       s.returns.c.return_id)).all()
        out = [return_dict(conn, tenant, r) for r in rows]
    return {"returns": out, "total": len(out)}


def get_return(engine: Engine, tenant: str, return_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        return return_dict(conn, tenant, _load(conn, tenant, return_id))


# ------------------------------------------------------------- settings


def _settings_dict(pickup_cost_kobo: int | None) -> dict[str, Any]:
    return {
        "pickup_cost_kobo": pickup_cost_kobo,
        "pickup_cost_naira": ("" if pickup_cost_kobo is None
                              else owed.naira(pickup_cost_kobo)),
        "note": ("What a collection trip costs you. A return reports what it recovered "
                 "less this cost, so the outcome is not guessed."
                 if pickup_cost_kobo is None else
                 f"A collection trip costs {owed.naira(pickup_cost_kobo)}."),
    }


def settings(engine: Engine, tenant: str) -> dict[str, Any]:
    with engine.connect() as conn:
        return _settings_dict(_pickup_cost(conn, tenant))


def set_settings(engine: Engine, tenant: str, pickup_cost_kobo: int,
                 now: float | None = None) -> dict[str, Any]:
    now = wall() if now is None else now
    cost = max(0, int(pickup_cost_kobo))
    with engine.begin() as conn:
        existing = conn.execute(sa.select(s.returns_settings.c.tenant_id).where(
            s.returns_settings.c.tenant_id == tenant)).first()
        if existing is None:
            insert_ignore(conn, s.returns_settings, {
                "tenant_id": tenant, "pickup_cost_kobo": cost, "updated_at": now_iso(now)})
        else:
            conn.execute(s.returns_settings.update().where(
                s.returns_settings.c.tenant_id == tenant).values(
                pickup_cost_kobo=cost, updated_at=now_iso(now)))
        return _settings_dict(cost)


# ------------------------------------------------------------- opening


def _pickup_option(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if payload.get("chosen") != "pickup":
        return None
    return next((o for o in payload.get("options", [])
                 if o.get("action") == "pickup"), None)


def open_return(engine: Engine, tenant: str, *, approval_id: str = "", shop_id: str = "",
                reason: str = "", lines: Sequence[Mapping[str, Any]] | None = None,
                by: str = "owner", now: float | None = None) -> dict[str, Any]:
    """Open (or return the existing) draft. From ``approval_id`` it must be an
    approved slow-stock pickup; otherwise ``shop_id`` and ``lines`` are used."""
    now = wall() if now is None else now
    with engine.begin() as conn:
        if approval_id:
            rid = _from_pickup(conn, tenant, approval_id, now, by)
        else:
            rid = _from_lines(conn, tenant, shop_id, reason, list(lines or []), now)
        return return_dict(conn, tenant, _load(conn, tenant, rid))


def draft_from_pickup(conn: sa.Connection, tenant: str, approval_id: str,
                      now: float, by: str = "owner") -> str:
    """Open the return draft for an approved slow-stock pickup, inside the
    caller's transaction. Idempotent on the approval id."""
    return _from_pickup(conn, tenant, approval_id, now, by)


def _from_pickup(conn: sa.Connection, tenant: str, approval_id: str, now: float,
                 by: str = "owner") -> str:
    a = conn.execute(sa.select(s.approvals).where(
        s.approvals.c.tenant_id == tenant,
        s.approvals.c.approval_id == approval_id)).first()
    if a is None:
        raise NotFound(approval_id)
    if a.tool != SLOW_KIND:
        raise Conflict("this approval is not a slow-stock plan")
    if a.status != "approved":
        raise Conflict("approve the pickup before opening a return")
    payload = loads(a.payload, {})
    if (payload.get("result") or {}).get("undo_available") is False:
        raise Conflict("the owner called this pickup off, so there is nothing to collect")
    option = _pickup_option(payload)
    if option is None:
        raise Conflict("this plan's approved option is not a pickup")
    shop_id = str(a.shop_id)
    shop_name = _shop_name(conn, tenant, shop_id)
    sku = str(payload.get("sku") or "")
    quantity = int(option.get("quantity") or 0)
    if not sku or quantity <= 0:
        raise Conflict("this pickup has no lines to return")
    # The plan's own expiry (the earliest expiry among the units left, off the
    # delivery notes) identifies the delivery this pickup is about.
    expires_on, batch, source = _invoice_batch(
        conn, tenant, shop_id, sku, str(payload.get("expires_on") or ""))
    item = str(payload.get("item") or sku)
    unit = str(option.get("unit") or payload.get("unit") or "")
    price = int(option.get("unit_price_kobo") or 0)
    near = str(payload.get("slow_kind") or "") == "near_expiry"
    lreason = ledger.line_reason(item, unit, quantity, expires_on, near)
    key = f"{SLOW_KIND}:{approval_id}"
    rid = ledger.return_id_for(tenant, key)
    fresh = insert_ignore(conn, s.returns, {
        "tenant_id": tenant, "return_id": rid, "shop_id": shop_id, "source": "slow_stock",
        "approval_id": approval_id,
        "reason": ledger.return_reason(shop_name, [
            {"quantity_drafted": quantity, "unit": unit, "item": item}]),
        "status": ledger.DRAFTED,
        "value_drafted_kobo": ledger.line_value_kobo(quantity, price),
        "value_collected_kobo": 0, "value_returned_kobo": 0, "recovered_kobo": 0,
        "created_at": now_iso(now), "created_ts": now, "updated_at": now_iso(now)})
    if not fresh:
        return rid
    insert_ignore(conn, s.return_lines, {
        "tenant_id": tenant, "return_id": rid, "line_no": 1, "sku": sku, "item": item,
        "unit": unit, "reason": lreason, "quantity_drafted": quantity,
        "unit_price_kobo": price, "value_drafted_kobo": ledger.line_value_kobo(quantity, price),
        "expires_on": expires_on or None, "batch": batch, "expiry_source": source})
    _record(conn, tenant, rid, ledger.DRAFTED, by=by,
            note=f"Opened from the approved pickup for {shop_name}.", now=now)
    return rid


def cancel_from_pickup(conn: sa.Connection, tenant: str, approval_id: str, now: float,
                       by: str = "owner") -> list[str]:
    """Call the pickup off in the ledger, inside the caller's transaction.

    The owner can undo a pickup. A return opened from it that has collected
    nothing (``drafted`` or ``approved``) is cancelled with it, so the ledger
    never keeps a plan the shop was told to ignore. A return that has already
    collected goods is left alone: something moved, and that record is real."""
    stopped: list[str] = []
    for r in conn.execute(sa.select(s.returns).where(
            s.returns.c.tenant_id == tenant,
            s.returns.c.approval_id == approval_id)).all():
        if not ledger.can_move(str(r.status), ledger.CANCELLED):
            continue
        _move(conn, tenant, r, ledger.CANCELLED, by=by,
              note="The owner called the pickup off, so nothing is coming back.", now=now)
        stopped.append(str(r.return_id))
    return stopped


def _from_lines(conn: sa.Connection, tenant: str, shop_id: str, reason: str,
                lines: Sequence[Mapping[str, Any]], now: float) -> str:
    if not shop_id or not lines:
        raise Conflict("give an approved pickup, or a shop and at least one line")
    shop_name = _shop_name(conn, tenant, shop_id)
    rid = ledger.return_id_for(tenant, f"manual:{shop_id}:{reason}:{now}")
    built: list[dict[str, Any]] = []
    for i, ln in enumerate(lines, start=1):
        sku = str(ln.get("sku") or "")
        row = _sku_row(conn, tenant, sku)
        if row is None:
            raise Conflict(f"{sku} is not in the book")
        qty = int(ln.get("quantity") or 0)
        if qty <= 0:
            raise Conflict("every line needs a quantity above zero")
        price = int(row.price_kobo)
        item = str(row.name)
        unit = str(row.unit)
        expires_on, batch, source = _invoice_batch(conn, tenant, shop_id, sku)
        built.append({
            "line_no": i, "sku": sku, "item": item, "unit": unit,
            "reason": ledger.scrub(str(ln.get("reason") or "")) or
                      ledger.line_reason(item, unit, qty, expires_on, False),
            "quantity_drafted": qty, "unit_price_kobo": price,
            "value_drafted_kobo": ledger.line_value_kobo(qty, price),
            "expires_on": expires_on or None, "batch": batch, "expiry_source": source})
    insert_ignore(conn, s.returns, {
        "tenant_id": tenant, "return_id": rid, "shop_id": shop_id, "source": "manual",
        "approval_id": None,
        "reason": (ledger.scrub(reason) if reason.strip()
                   else ledger.return_reason(shop_name, built)),
        "status": ledger.DRAFTED,
        "value_drafted_kobo": sum(b["value_drafted_kobo"] for b in built),
        "value_collected_kobo": 0, "value_returned_kobo": 0, "recovered_kobo": 0,
        "created_at": now_iso(now), "created_ts": now, "updated_at": now_iso(now)})
    for b in built:
        insert_ignore(conn, s.return_lines, {"tenant_id": tenant, "return_id": rid, **b})
    _record(conn, tenant, rid, ledger.DRAFTED, by="owner",
            note=f"Opened for {shop_name}.", now=now)
    return rid


# ------------------------------------------------------------- transitions


def approve(engine: Engine, tenant: str, return_id: str, *, by: str = "owner",
            note: str = "", now: float | None = None) -> dict[str, Any]:
    return _one(engine, tenant, return_id, ledger.APPROVED, by=by, note=note, now=now)


def cancel(engine: Engine, tenant: str, return_id: str, *, by: str = "owner",
           note: str = "", now: float | None = None) -> dict[str, Any]:
    return _one(engine, tenant, return_id, ledger.CANCELLED, by=by, note=note, now=now)


def _one(engine: Engine, tenant: str, return_id: str, to: str, *, by: str, note: str,
         now: float | None) -> dict[str, Any]:
    now = wall() if now is None else now
    with engine.begin() as conn:
        r = _load(conn, tenant, return_id)
        _move(conn, tenant, r, to, by=by, note=note or f"{to} by {by}.", now=now)
        return return_dict(conn, tenant, _load(conn, tenant, return_id))


def collect(engine: Engine, tenant: str, return_id: str, *,
            lines: Sequence[Mapping[str, Any]] | None = None, by: str = "rep",
            now: float | None = None) -> dict[str, Any]:
    """The rep collected the goods. ``lines`` may set each line's collected
    quantity (default: what was drafted); it can never exceed it."""
    now = wall() if now is None else now
    with engine.begin() as conn:
        r = _load(conn, tenant, return_id)
        if not ledger.can_move(str(r.status), ledger.COLLECTED):
            raise Conflict(f"a return that is {r.status} cannot be collected")
        rows = _line_rows(conn, tenant, return_id)
        known = {int(ln.line_no) for ln in rows}
        asked = [int(x["line_no"]) for x in (lines or [])]
        unknown = sorted(set(asked) - known)
        if unknown:
            raise Conflict(f"this return has {len(rows)} line"
                           f"{'' if len(rows) == 1 else 's'}; "
                           f"{len(unknown)} of the collected lines are not on it")
        if len(asked) != len(set(asked)):
            raise Conflict("each line is collected once")
        by_no = {int(x["line_no"]): int(x.get("quantity") or 0) for x in (lines or [])}
        for ln in rows:
            qty = by_no.get(int(ln.line_no), int(ln.quantity_drafted))
            if qty < 0 or qty > int(ln.quantity_drafted):
                raise Conflict(f"only {ln.quantity_drafted} was drafted for {ln.item}")
            conn.execute(s.return_lines.update().where(
                s.return_lines.c.tenant_id == tenant,
                s.return_lines.c.return_id == return_id,
                s.return_lines.c.line_no == ln.line_no).values(quantity_collected=qty))
        total = sum(ledger.line_value_kobo(int(ln.quantity_collected or 0),
                                           int(ln.unit_price_kobo))
                    for ln in _line_rows(conn, tenant, return_id))
        _move(conn, tenant, r, ledger.COLLECTED, by=by,
              note="The rep collected the goods.", now=now, value_collected_kobo=total)
        return return_dict(conn, tenant, _load(conn, tenant, return_id))


def inspect(engine: Engine, tenant: str, return_id: str, *,
            lines: Sequence[Mapping[str, Any]], by: str = "owner",
            now: float | None = None) -> dict[str, Any]:
    """Record what came back and its condition, and the disposition per line.
    Every line must be named; nothing can come back above what was collected."""
    now = wall() if now is None else now
    with engine.begin() as conn:
        r = _load(conn, tenant, return_id)
        if not ledger.can_move(str(r.status), ledger.INSPECTED):
            raise Conflict(f"a return that is {r.status} cannot be inspected")
        rows = _line_rows(conn, tenant, return_id)
        given = {int(x["line_no"]): x for x in (lines or [])}
        missing = [ln.line_no for ln in rows if int(ln.line_no) not in given]
        if missing:
            raise Conflict("name every line at inspection")
        value = 0
        for ln in rows:
            x = given[int(ln.line_no)]
            collected = int(ln.quantity_collected if ln.quantity_collected is not None
                            else ln.quantity_drafted)
            back = int(x.get("quantity_returned") or 0)
            if back < 0 or back > collected:
                raise Conflict(f"only {collected} came back for {ln.item}; "
                               f"{back} was asked for")
            condition = str(x.get("condition") or "")
            disposition = str(x.get("disposition") or "")
            if back > 0 and condition not in ledger.CONDITIONS:
                raise Conflict(f"name a condition for {ln.item}: "
                               f"{', '.join(ledger.CONDITIONS)}")
            if back > 0 and disposition not in ledger.DISPOSITIONS:
                raise Conflict(f"name a disposition for {ln.item}: "
                               f"{', '.join(ledger.DISPOSITIONS)}")
            if back > 0 and disposition == "resell" and condition != "resellable":
                # Money rests on this: a resold line counts as recovered, and
                # what came back damaged or expired cannot go back on the shelf.
                raise Conflict(
                    f"{ln.item} came back {condition}, so it cannot be sold again. "
                    "Write it off or send it back to the supplier.")
            line_value = ledger.line_value_kobo(back, int(ln.unit_price_kobo))
            value += line_value
            conn.execute(s.return_lines.update().where(
                s.return_lines.c.tenant_id == tenant,
                s.return_lines.c.return_id == return_id,
                s.return_lines.c.line_no == ln.line_no).values(
                quantity_returned=back, condition=condition if back else "",
                disposition=disposition if back else "", value_returned_kobo=line_value))
        rows = _line_rows(conn, tenant, return_id)
        saving, recovered = _saving(conn, tenant, rows)
        _move(conn, tenant, r, ledger.INSPECTED, by=by,
              note="Inspected: what came back and its condition are recorded.", now=now,
              value_returned_kobo=value, recovered_kobo=recovered,
              pickup_cost_kobo=_pickup_cost(conn, tenant), saving_kobo=saving)
        return return_dict(conn, tenant, _load(conn, tenant, return_id))


def settle(engine: Engine, tenant: str, return_id: str, *,
           amount_kobo: int | None = None, by: str = "owner",
           now: float | None = None) -> dict[str, Any]:
    """Draft the credit the owner approves. Never applied here: it waits as a
    pending ``return_credit`` approval, like the Zoho payment card."""
    now = wall() if now is None else now
    with engine.begin() as conn:
        r = _load(conn, tenant, return_id)
        if not ledger.can_move(str(r.status), ledger.SETTLED):
            raise Conflict(f"a return that is {r.status} cannot be settled")
        value = int(r.value_returned_kobo)
        amount = value if amount_kobo is None else int(amount_kobo)
        if amount <= 0:
            raise Conflict("there is nothing to settle")
        if amount > value:
            raise Conflict(f"the credit cannot be more than {owed.naira(value)}")
        rows = _line_rows(conn, tenant, return_id)
        shop_name = _shop_name(conn, tenant, str(r.shop_id))
        receivables = [{"delivery_id": x.delivery_id, "outstanding_kobo": int(x.outstanding_kobo),
                        "position": int(x.position)} for x in conn.execute(
            sa.select(s.receivables).where(
                s.receivables.c.tenant_id == tenant,
                s.receivables.c.shop_id == r.shop_id,
                s.receivables.c.outstanding_kobo > 0))]
        applications, applied, on_account = ledger.credit_plan(amount, receivables)
        # A card that is already waiting for the owner: hand it back untouched
        # when it asks for the same money, and replace it when the owner
        # settled a different amount. A card the owner rejected, withdrew or
        # that was superseded is not live, so this drafts a fresh one for it.
        stale_id = ""
        dead_id = ""
        waiting = _credit_approval(conn, tenant, r)
        if waiting is not None and waiting.status == "pending":
            held = int(loads(waiting.payload, {}).get("amount_kobo") or 0)
            if held == amount:
                return return_dict(conn, tenant, r)
            stale_id = str(waiting.approval_id)
        elif r.credit_approval_id:
            # The card on the return is not this return's credit any more: the
            # owner rejected or withdrew it, it was superseded, or its
            # application failed after the approval was recorded.
            dead_id = str(r.credit_approval_id)
        attempt = 1 + _settle_attempts(conn, tenant, return_id)
        key = f"{RETURN_CREDIT_KIND}:{return_id}:{attempt}"
        apr = approval_id_for(tenant, key)
        payload = {
            "kind": RETURN_CREDIT_KIND, "return_id": return_id,
            "shop_id": r.shop_id, "shop_name": shop_name,
            "amount_kobo": amount, "amount_ngn": (amount + 50) // 100, "currency": "NGN",
            "reason": ledger.scrub(
                f"Credit for the return from {shop_name}: "
                f"{_line_summary(rows)}."),
            "lines": [{"item": display_name(ln.item), "quantity": int(ln.quantity_returned or 0),
                       "unit": unit_word(ln.unit, 2), "disposition": ln.disposition,
                       "value_kobo": int(ln.value_returned_kobo or 0)} for ln in rows],
            "applied_kobo": applied, "on_account_kobo": on_account,
            "invoices": _invoice_rows(conn, tenant, applications),
            "date": _date_text(now), "result": None}
        covered = ("It comes off the shop's open invoices."
                   if applied else "The shop owes nothing, so it sits on its account as credit.")
        preview = ledger.scrub(
            f"{shop_name}: credit {owed.naira(amount)} for the return "
            f"({_line_summary(rows)}). {covered} Approve to record it, or undo later.")
        insert_ignore(conn, s.approvals, {
            "approval_id": apr, "tenant_id": tenant, "idempotency_key": key,
            "turn_id": "", "thread_id": _owner_thread(conn, tenant), "helper": "",
            "skill": "returns/settle", "shop_id": r.shop_id, "tool": RETURN_CREDIT_KIND,
            "intent_kind": RETURN_CREDIT_KIND, "preview": preview, "payload": dumps(payload),
            "status": "pending", "created_at": now_iso(now), "created_ts": now, "handling": ""})
        if stale_id:
            from mimi.api import steering

            steering.supersede(conn, tenant, stale_id, apr)
        if dead_id:
            # So the console stops offering a card that is not the credit, and
            # the owner sees why it is not.
            conn.execute(s.approvals.update().where(
                s.approvals.c.tenant_id == tenant,
                s.approvals.c.approval_id == dead_id,
                s.approvals.c.status.in_(("pending", "approved"))).values(
                status="withdrawn",
                note="Replaced: this credit was never applied to the shop's invoices."))
        _touch(conn, tenant, return_id, now, credit_kobo=amount, credit_approval_id=apr)
        return return_dict(conn, tenant, _load(conn, tenant, return_id))


def _line_summary(rows: Sequence[Any]) -> str:
    parts = [f"{int(ln.quantity_returned or 0)} {unit_word(ln.unit, 2)} {display_name(ln.item)}"
             for ln in rows if int(ln.quantity_returned or 0) > 0]
    if not parts:
        return "nothing came back"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _invoice_rows(conn: sa.Connection, tenant: str,
                  applications: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    from mimi.api.demo_wiring import invoice_number

    return [{"invoice_number": invoice_number(str(a["delivery_id"])),
             "amount_applied_kobo": int(a["amount_kobo"])} for a in applications]


def _owner_thread(conn: sa.Connection, tenant: str) -> str:
    t = conn.execute(sa.select(s.tenants.c.owner_thread).where(
        s.tenants.c.tenant_id == tenant)).scalar()
    return str(t or f"{tenant}:owner")


def _date_text(now: float) -> str:
    from datetime import UTC, datetime

    from mimi.tz import lagos_day

    return lagos_day(datetime.fromtimestamp(now, UTC)).isoformat()


# ------------------------------------------------------------- applying the credit


def apply_credit(engine: Engine, tenant: str, approval: Mapping[str, Any]) -> None:
    """Apply an approved ``return_credit``: take it off the shop's open
    invoices (oldest first) and hold the rest on its account. Recorded in
    ``credit_notes``; the return becomes ``settled``. Nothing is applied
    twice."""
    from mimi.api.demo_wiring import _say

    now = wall()
    with engine.begin() as conn:
        a = conn.execute(sa.select(s.approvals).where(
            s.approvals.c.tenant_id == tenant,
            s.approvals.c.approval_id == approval["approval_id"])).first()
        if a is None:
            raise NotFound(str(approval["approval_id"]))
        payload = loads(a.payload, {})
        res = dict(payload.get("result") or {})
        if res.get("credit_id"):
            return
        return_id = str(payload["return_id"])
        r = _load(conn, tenant, return_id)
        amount = int(payload["amount_kobo"])
        receivables = [{"delivery_id": x.delivery_id, "outstanding_kobo": int(x.outstanding_kobo),
                        "position": int(x.position), "status": x.status,
                        "paid_kobo": int(x.paid_kobo)} for x in conn.execute(
            sa.select(s.receivables).where(
                s.receivables.c.tenant_id == tenant,
                s.receivables.c.shop_id == r.shop_id,
                s.receivables.c.outstanding_kobo > 0))]
        applications, applied, on_account = ledger.credit_plan(amount, receivables)
        by_id = {x["delivery_id"]: x for x in receivables}
        for app in applications:
            src = by_id.get(str(app["delivery_id"]))
            if src is None:
                continue
            out = int(src["outstanding_kobo"]) - int(app["amount_kobo"])
            conn.execute(s.receivables.update().where(
                s.receivables.c.tenant_id == tenant,
                s.receivables.c.delivery_id == app["delivery_id"]).values(
                paid_kobo=int(src["paid_kobo"]) + int(app["amount_kobo"]),
                outstanding_kobo=max(0, out),
                status="settled" if out <= 0 else src["status"]))
            app["was_status"] = str(src["status"])
            app["was_paid_kobo"] = int(src["paid_kobo"])
            app["was_outstanding_kobo"] = int(src["outstanding_kobo"])
            # The invoice's figures before this credit came off it, and after:
            # the audit trail of the application. The undo does not restore
            # these (it subtracts what the credit put on, see
            # ``_reverse_applications``), so they are what a person reads to
            # see what the credit did.
            app["after_paid_kobo"] = int(src["paid_kobo"]) + int(app["amount_kobo"])
            app["after_outstanding_kobo"] = max(0, out)
            app["after_status"] = "settled" if out <= 0 else str(src["status"])
        credit_id = ledger.credit_id_for(tenant, return_id, _attempt(conn, tenant, return_id))
        insert_ignore(conn, s.credit_notes, {
            "tenant_id": tenant, "credit_id": credit_id, "return_id": return_id,
            "shop_id": r.shop_id, "approval_id": approval["approval_id"],
            "amount_kobo": amount, "applied_kobo": applied, "on_account_kobo": on_account,
            "applications": dumps(applications), "applied_at": now_iso(now)})
        _move(conn, tenant, r, ledger.SETTLED, by="owner",
              note=(f"Credit {owed.naira(amount)} recorded: {owed.naira(applied)} off the "
                    "invoices" + (f", {owed.naira(on_account)} on the shop's account."
                                  if on_account else ".")), now=now)
        payload["result"] = {"credit_id": credit_id, "applied_kobo": applied,
                             "on_account_kobo": on_account, "undo_available": True,
                             "applied_at": now_iso(now)}
        conn.execute(s.approvals.update().where(
            s.approvals.c.tenant_id == tenant,
            s.approvals.c.approval_id == approval["approval_id"]
        ).values(payload=dumps(payload)))
        _say(conn, tenant, _owner_thread(conn, tenant),
             key=f"return-credit:{approval['approval_id']}:done", channel="owner_console",
             shop_id="", kind="return_credit_recorded", now=now,
             text=(f"Recorded the {owed.naira(amount)} credit for {payload['shop_name']}'s "
                   f"return. {owed.naira(applied)} came off the invoices"
                   + (f", {owed.naira(on_account)} sits on the shop's account" if on_account
                      else "") + ". You can undo it."))


def _attempt(conn: sa.Connection, tenant: str, return_id: str) -> int:
    return 1 + len(conn.execute(sa.select(sa.literal(1)).select_from(s.credit_notes).where(
        s.credit_notes.c.tenant_id == tenant,
        s.credit_notes.c.return_id == return_id)).all())


def _reverse_applications(conn: sa.Connection, tenant: str, note: Any) -> None:
    """Take this credit's own applications back off the invoices.

    Not a snapshot restore. The undo subtracts exactly what this credit put
    on each invoice (``paid -= amount``, ``outstanding += amount``), which is
    right however the invoice has moved since: Go writes the same columns from
    the verified bank payment (``demo_wiring._apply_to_invoices``) and from the
    morning roll (``week.go.GoSim.open_day`` and ``GoSim._pay``), and the
    subtraction carries the same answer with or without them. Two credits on
    one invoice can then be undone in either order, and a payment landing in
    between is nothing to refuse over.

    ``status`` is derived from the balance the invoice is left with and its due
    day, the same rule Go's morning roll uses, so an invoice that went overdue
    while the credit was on it keeps that word instead of getting a stale one
    back. Only the money is written from the arithmetic; nothing is guessed."""
    live_day = conn.execute(sa.select(s.tenants.c.live_day).where(
        s.tenants.c.tenant_id == tenant)).scalar()
    live_day = int(live_day or 0)
    for app in loads(note.applications, []):
        row = conn.execute(sa.select(s.receivables).where(
            s.receivables.c.tenant_id == tenant,
            s.receivables.c.delivery_id == app["delivery_id"])).first()
        if row is None:
            raise Conflict(
                "an invoice this credit came off is not on the book any more, "
                "so nothing was undone. Someone has to check it by hand.")
        amount = int(app.get("amount_kobo") or 0)
        paid = max(0, int(row.paid_kobo) - amount)
        out = int(row.outstanding_kobo) + amount
        if out == 0:
            status = "settled"
        elif int(row.due_day) < live_day:
            status = "overdue"
        else:
            status = "open"
        conn.execute(s.receivables.update().where(
            s.receivables.c.tenant_id == tenant,
            s.receivables.c.delivery_id == app["delivery_id"]).values(
            paid_kobo=paid, outstanding_kobo=out, status=status))


def undo_credit(engine: Engine, tenant: str, approval: Mapping[str, Any]) -> None:
    """Reverse an applied credit: the invoices go back to what they were and
    the credit note is marked undone. The return goes back to ``inspected``."""
    from mimi.api.demo_wiring import _say

    now = wall()
    with engine.begin() as conn:
        a = conn.execute(sa.select(s.approvals).where(
            s.approvals.c.tenant_id == tenant,
            s.approvals.c.approval_id == approval["approval_id"])).first()
        if a is None:
            raise NotFound(str(approval["approval_id"]))
        payload = loads(a.payload, {})
        res = dict(payload.get("result") or {})
        credit_id = str(res.get("credit_id") or "")
        if not credit_id or not res.get("undo_available"):
            raise Conflict("this credit was already undone")
        note = conn.execute(sa.select(s.credit_notes).where(
            s.credit_notes.c.tenant_id == tenant,
            s.credit_notes.c.credit_id == credit_id)).first()
        if note is None:
            raise Conflict("this credit is no longer on the books")
        _reverse_applications(conn, tenant, note)
        conn.execute(s.credit_notes.update().where(
            s.credit_notes.c.tenant_id == tenant,
            s.credit_notes.c.credit_id == credit_id).values(undone_at=now_iso(now)))
        r = _load(conn, tenant, str(payload["return_id"]))
        _move(conn, tenant, r, ledger.INSPECTED, by="owner",
              note=f"Credit {owed.naira(int(note.amount_kobo))} undone.", now=now,
              credit_kobo=None, credit_approval_id=None)
        payload["result"] = {**res, "undo_available": False, "undone_at": now_iso(now)}
        conn.execute(s.approvals.update().where(
            s.approvals.c.tenant_id == tenant,
            s.approvals.c.approval_id == approval["approval_id"]
        ).values(payload=dumps(payload)))
        _say(conn, tenant, _owner_thread(conn, tenant),
             key=f"return-credit:{approval['approval_id']}:undo", channel="owner_console",
             shop_id="", kind="return_credit_undone", now=now,
             text=(f"Undone: the {owed.naira(int(note.amount_kobo))} credit for "
                   f"{payload['shop_name']}'s return is off."))

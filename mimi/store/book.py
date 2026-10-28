"""``SqlGo``: the system-of-record ports over the durable store (RAI-266).

The same four ports as ``mimi.harness.go.FakeGo`` (``BookAPI``,
``ControlPlane``, ``EventLog``, ``IntentGate``), with the same tenancy,
consent and dedupe rules, read from and written to the database instead of
process memory. ``FakeGo`` stays the in-process stand-in for tests and evals;
a real Go client can replace either behind the same ports later.

Tenancy is enforced on every read: a shop id is only ever looked up together
with the caller's tenant, so another distributor's row reads as missing
(``CrossTenant``), exactly as Go answers.

Intents land through ``apply_intent`` (inside the caller's transaction, so a
turn's intents, messages and completion commit together) or ``submit`` (its
own transaction, for the job runner's outbox). Both dedupe on the business
key: the book changes once however many times a key is proposed.
"""

from __future__ import annotations

import hashlib
import time
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.credit.looks import item_name, pieces_per_unit
from mimi.harness.go import ConsentGrants, CrossTenant
from mimi.harness.inbound import Directory
from mimi.harness.policy import go_allows
from mimi.harness.types import Event
from mimi.runtime.outbox import MONEY_VERBS
from mimi.store import schema as s
from mimi.store.util import dumps, insert_ignore, loads, now_iso
from mimi.world.model import Sku

BASE_SHOP_SCOPES = frozenset({"shop:balance", "shop:invoices", "shop:payments",
                              "shop:messages", "shop:promises"})
ORG_SCOPES = frozenset({"org:receivables", "org:account", "org:owner", "org:reps",
                        "org:helpers", "org:plan"})


def is_credit_intent(tool: str, kind: str) -> bool:
    return tool.startswith("credit") or kind.startswith("credit") or "credit_" in tool


def moves_money(tool: str, kind: str) -> bool:
    return any(v in tool or v in kind for v in MONEY_VERBS)


def approval_id_for(tenant_id: str, key: str) -> str:
    return "apr_" + hashlib.sha256(f"{tenant_id}\x00{key}".encode()).hexdigest()[:20]


class SqlGo:
    """Go's ports over the store. Thread-safe: every call opens its own connection."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ------------------------------------------------------------- tenancy

    def _tenant_row(self, conn: sa.Connection, tenant_id: str) -> Any:
        row = conn.execute(sa.select(s.tenants).where(
            s.tenants.c.tenant_id == tenant_id)).first()
        if row is None:
            raise CrossTenant(tenant_id)
        return row

    def _shop_row(self, conn: sa.Connection, tenant_id: str, shop_id: str) -> Any:
        row = conn.execute(sa.select(s.shops).where(
            s.shops.c.tenant_id == tenant_id, s.shops.c.shop_id == shop_id)).first()
        if row is None:
            raise CrossTenant(shop_id)
        return row

    def has_tenant(self, tenant_id: str) -> bool:
        with self.engine.connect() as conn:
            return conn.execute(sa.select(s.tenants.c.tenant_id).where(
                s.tenants.c.tenant_id == tenant_id)).first() is not None

    def directory(self, tenant_id: str) -> Directory:
        """Who is who for one distributor (the router's ``Directory``)."""
        with self.engine.connect() as conn:
            t = self._tenant_row(conn, tenant_id)
            shop_phones = {r.phone: r.shop_id for r in conn.execute(
                sa.select(s.shops.c.phone, s.shops.c.shop_id).where(
                    s.shops.c.tenant_id == tenant_id))}
            rep_phones = {r.phone: r.rep_id for r in conn.execute(
                sa.select(s.reps.c.phone, s.reps.c.rep_id).where(
                    s.reps.c.tenant_id == tenant_id))}
        return Directory(org_id=tenant_id, owner_phones=frozenset(loads(t.owner_phones, [])),
                         shop_phones=shop_phones, rep_phones=rep_phones,
                         owner_thread=t.owner_thread or f"{tenant_id}:owner")

    # ---------------------------------------------------------------- book

    def grants(self, tenant_id: str) -> ConsentGrants:
        with self.engine.connect() as conn:
            self._tenant_row(conn, tenant_id)
            ids = [r.shop_id for r in conn.execute(sa.select(s.shops.c.shop_id).where(
                s.shops.c.tenant_id == tenant_id))]
            withheld: dict[str, set[str]] = {}
            for r in conn.execute(sa.select(s.consent_withheld).where(
                    s.consent_withheld.c.tenant_id == tenant_id)):
                withheld.setdefault(r.shop_id, set()).add(r.scope)
        shops: dict[str, frozenset[str]] = {}
        for sid in ids:
            w = withheld.get(sid, set())
            if "shop:*" in w:
                continue
            shops[sid] = frozenset(BASE_SHOP_SCOPES - w)
        return ConsentGrants(tenant_id, shops, ORG_SCOPES)

    def today(self) -> date:
        """The book's day. One tenant per store today; the newest live date wins."""
        with self.engine.connect() as conn:
            raw = conn.execute(sa.select(sa.func.max(s.tenants.c.live_date))).scalar()
        return date.fromisoformat(raw) if raw else date.today()

    def today_for(self, tenant_id: str) -> date:
        with self.engine.connect() as conn:
            return date.fromisoformat(self._tenant_row(conn, tenant_id).live_date)

    def shop(self, tenant_id: str, shop_id: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            r = self._shop_row(conn, tenant_id, shop_id)
        return {"shop_id": r.shop_id, "name": r.name, "owner": r.owner, "area": r.area,
                "rep_id": r.rep_id, "credit_limit_kobo": int(r.credit_limit_kobo)}

    def receivables(self, tenant_id: str, shop_id: str | None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            t = self._tenant_row(conn, tenant_id)
            if shop_id is not None:
                self._shop_row(conn, tenant_id, shop_id)
            q = (sa.select(s.receivables, s.shops.c.name.label("shop_name"))
                 .join(s.shops, sa.and_(s.shops.c.tenant_id == s.receivables.c.tenant_id,
                                        s.shops.c.shop_id == s.receivables.c.shop_id))
                 .where(s.receivables.c.tenant_id == tenant_id,
                        s.receivables.c.outstanding_kobo > 0)
                 .order_by(s.receivables.c.position, s.receivables.c.delivery_id))
            if shop_id is not None:
                q = q.where(s.receivables.c.shop_id == shop_id)
            rows = conn.execute(q).all()
        today = date.fromisoformat(t.live_date)
        return [{
            "delivery_id": r.delivery_id,
            "shop_id": r.shop_id,
            "shop_name": r.shop_name,
            "due_on": r.due_on,
            "days_overdue": max(0, (today - date.fromisoformat(r.due_on)).days),
            "total_kobo": int(r.total_kobo),
            "paid_kobo": int(r.paid_kobo),
            "outstanding_kobo": int(r.outstanding_kobo),
            "status": r.status,
        } for r in rows]

    def payments(self, tenant_id: str, shop_id: str, limit: int) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            self._shop_row(conn, tenant_id, shop_id)
            feed = {r.payment_id for r in conn.execute(
                sa.select(s.bank_feed.c.payment_id).where(s.bank_feed.c.tenant_id == tenant_id))}
            rows = conn.execute(
                sa.select(s.payments).where(s.payments.c.tenant_id == tenant_id,
                                            s.payments.c.shop_id == shop_id)
                .order_by(s.payments.c.day.desc(), s.payments.c.payment_id.desc())
                .limit(max(0, int(limit)))).all()
        return [{
            "payment_id": p.payment_id,
            "paid_on": p.paid_on,
            "amount_kobo": 0 if p.fake_alert else int(p.amount_kobo),
            "claimed_kobo": int(p.amount_kobo),
            "method": p.method,
            "status": p.status,
            "bank_confirmed": p.payment_id in feed,
        } for p in rows]

    def promises(self, tenant_id: str, shop_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            self._shop_row(conn, tenant_id, shop_id)
            rows = conn.execute(sa.select(s.promises).where(
                s.promises.c.tenant_id == tenant_id, s.promises.c.shop_id == shop_id)
                .order_by(s.promises.c.promise_id)).all()
        return [{"made_on": p.made_on, "promised_on": p.promised_on, "kept": bool(p.kept),
                 "delivery_id": p.delivery_id} for p in rows]

    def collection_account(self, tenant_id: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            t = self._tenant_row(conn, tenant_id)
        return dict(loads(t.collection_account, {}))

    def bank_feed_match(
        self, tenant_id: str, shop_id: str, amount_kobo: int, reference: str
    ) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            self._shop_row(conn, tenant_id, shop_id)
            rows = conn.execute(
                sa.select(s.bank_feed)
                .join(s.payments, sa.and_(s.payments.c.tenant_id == s.bank_feed.c.tenant_id,
                                          s.payments.c.payment_id == s.bank_feed.c.payment_id))
                .where(s.bank_feed.c.tenant_id == tenant_id, s.payments.c.shop_id == shop_id,
                       s.bank_feed.c.amount_kobo == amount_kobo)
                .order_by(s.bank_feed.c.credit_id)).all()
        for c in rows:
            if not reference or reference in c.narration:
                return {"credit_id": c.credit_id, "payment_id": c.payment_id,
                        "amount_kobo": int(c.amount_kobo), "received_at": c.received_at}
        return None

    # ------------------------------------------------- helper reads (RAI-272)
    # The reads the Credit, Stock, Proof and Order helpers make on the API, the
    # same contract as their in-process stand-ins (``mimi.credit.book.CreditGo``
    # and the world the flows read). Every one is tenant-scoped.

    def days(self, tenant_id: str) -> tuple[int, int | None]:
        """(live day number, price rise day) of the book."""
        with self.engine.connect() as conn:
            t = self._tenant_row(conn, tenant_id)
        return int(t.live_day or 0), (int(t.price_rise_day)
                                      if t.price_rise_day is not None else None)

    def restock_days(self, tenant_id: str, shop_id: str) -> int:
        """Days between the rep's van visits to the shop (route data)."""
        with self.engine.connect() as conn:
            return int(self._shop_row(conn, tenant_id, shop_id).restock_days or 0)

    def skus(self, tenant_id: str) -> dict[str, Sku]:
        with self.engine.connect() as conn:
            self._tenant_row(conn, tenant_id)
            rows = conn.execute(sa.select(s.skus).where(s.skus.c.tenant_id == tenant_id)).all()
        return {r.sku: Sku(r.sku, r.name, r.brand, r.unit, int(r.price_kobo),
                           int(r.risen_price_kobo) if r.risen_price_kobo is not None else None)
                for r in rows}

    def deliveries(self, tenant_id: str, shop_id: str, *,
                   live: bool | None = None) -> list[dict[str, Any]]:
        """The shop's deliveries with their lines, oldest first (``live``: only
        the live day's, only earlier ones, or all)."""
        with self.engine.connect() as conn:
            self._shop_row(conn, tenant_id, shop_id)
            q = sa.select(s.deliveries).where(s.deliveries.c.tenant_id == tenant_id,
                                              s.deliveries.c.shop_id == shop_id)
            if live is not None:
                q = q.where(s.deliveries.c.live == live)
            ds = conn.execute(q.order_by(s.deliveries.c.day, s.deliveries.c.minute,
                                         s.deliveries.c.delivery_id)).all()
            lines: dict[str, list[dict[str, Any]]] = {}
            if ds:
                for ln in conn.execute(sa.select(s.delivery_lines).where(
                        s.delivery_lines.c.tenant_id == tenant_id,
                        s.delivery_lines.c.delivery_id.in_([d.delivery_id for d in ds]))
                        .order_by(s.delivery_lines.c.delivery_id, s.delivery_lines.c.line_no)):
                    lines.setdefault(ln.delivery_id, []).append({
                        "sku": ln.sku, "name": ln.name, "unit": ln.unit,
                        "quantity": int(ln.quantity), "unit_price_kobo": int(ln.unit_price_kobo),
                        "line_total_kobo": int(ln.line_total_kobo)})
        return [{"delivery_id": d.delivery_id, "shop_id": d.shop_id, "rep_id": d.rep_id,
                 "day": int(d.day), "delivered_on": d.delivered_on, "minute": int(d.minute),
                 "total_kobo": int(d.total_kobo), "status": d.status, "live": bool(d.live),
                 "witnessed_at": d.witnessed_at, "lines": lines.get(d.delivery_id, [])}
                for d in ds]

    def delivery(self, tenant_id: str, shop_id: str, delivery_id: str) -> dict[str, Any] | None:
        for d in self.deliveries(tenant_id, shop_id):
            if d["delivery_id"] == delivery_id:
                return d
        return None

    def credit_history(self, tenant_id: str, shop_id: str) -> dict[str, Any]:
        """``CreditGo.credit_history`` over the store (same shape)."""
        live_day, _ = self.days(tenant_id)
        ds = self.deliveries(tenant_id, shop_id, live=False)
        with self.engine.connect() as conn:
            due = {r.delivery_id: r for r in conn.execute(sa.select(s.receivables).where(
                s.receivables.c.tenant_id == tenant_id, s.receivables.c.shop_id == shop_id))}
            feed = {r.payment_id for r in conn.execute(
                sa.select(s.bank_feed.c.payment_id).where(s.bank_feed.c.tenant_id == tenant_id))}
            pays = conn.execute(sa.select(s.payments).where(
                s.payments.c.tenant_id == tenant_id, s.payments.c.shop_id == shop_id)
                .order_by(s.payments.c.day, s.payments.c.payment_id)).all()
            proms = conn.execute(sa.select(s.promises).where(
                s.promises.c.tenant_id == tenant_id, s.promises.c.shop_id == shop_id)
                .order_by(s.promises.c.promise_id)).all()
            open_disputes = conn.execute(sa.select(sa.func.count()).select_from(s.disputes)
                                         .where(s.disputes.c.tenant_id == tenant_id,
                                                s.disputes.c.shop_id == shop_id,
                                                s.disputes.c.status == "open")).scalar()
        start = date.fromisoformat(self.today_for(tenant_id).isoformat()).toordinal() - live_day
        deliveries = []
        for d in ds:
            r = due.get(d["delivery_id"])
            deliveries.append({
                "delivery_id": d["delivery_id"], "day": d["day"],
                "delivered_on": d["delivered_on"],
                "due_day": int(r.due_day) if r else d["day"],
                "due_on": r.due_on if r else d["delivered_on"], "total_kobo": d["total_kobo"],
                "lines": [{"sku": ln["sku"], "quantity": ln["quantity"]} for ln in d["lines"]]})
        return {
            "live_day": live_day,
            "deliveries": deliveries,
            "payments": [{
                "payment_id": p.payment_id, "delivery_id": p.delivery_id, "day": int(p.day),
                "paid_on": p.paid_on, "amount_kobo": 0 if p.fake_alert else int(p.amount_kobo),
                "claimed_kobo": int(p.amount_kobo), "method": p.method,
                "bank_confirmed": p.payment_id in feed, "status": p.status} for p in pays],
            "promises": [{"made_day": date.fromisoformat(p.made_on).toordinal() - start,
                          "promised_day": date.fromisoformat(p.promised_on).toordinal() - start,
                          "kept": bool(p.kept)} for p in proms],
            "open_disputes": int(open_disputes or 0),
        }

    def price_list(self, tenant_id: str) -> dict[str, tuple[int, int, str, str]]:
        """sku -> (unit price today in kobo, pieces per unit, item name, unit)."""
        live_day, rise = self.days(tenant_id)
        return {k: (sku.price_on(live_day, rise if rise is not None else 10**9),
                    pieces_per_unit(sku), item_name(sku), sku.unit)
                for k, sku in self.skus(tenant_id).items()}

    def open_credit_review(self, tenant_id: str, shop_id: str) -> str:
        """A credit request of this shop already waiting in the owner's approvals."""
        with self.engine.connect() as conn:
            self._shop_row(conn, tenant_id, shop_id)
            rows = conn.execute(sa.select(s.approvals.c.payload).where(
                s.approvals.c.tenant_id == tenant_id, s.approvals.c.shop_id == shop_id,
                s.approvals.c.tool == "submit_credit_review",
                s.approvals.c.status == "pending")).all()
        for r in rows:
            return str(loads(r.payload, {}).get("review_id") or "pending")
        return ""

    def photo_asks(self, tenant_id: str, shop_id: str, day: str) -> int:
        """Shelf-photo asks Go recorded for the shop that Lagos day."""
        return sum(1 for i in self.intents_of(tenant_id, shop_id, ("ask_for_shelf_photo",))
                   if i["outcome"] != "refused" and i["payload"].get("date") == day)

    def intents_of(self, tenant_id: str, shop_id: str,
                   tools: tuple[str, ...]) -> list[dict[str, Any]]:
        """The shop's recorded intents of these tools, oldest first (Go's record)."""
        with self.engine.connect() as conn:
            self._tenant_row(conn, tenant_id)
            q = sa.select(s.intents).where(s.intents.c.tenant_id == tenant_id,
                                           s.intents.c.tool.in_(list(tools)))
            if shop_id:
                q = q.where(s.intents.c.shop_id == shop_id)
            rows = conn.execute(q.order_by(s.intents.c.created_at,
                                           s.intents.c.idempotency_key)).all()
        return [{"tool": r.tool, "outcome": r.outcome, "payload": loads(r.payload, {}),
                 "shop_id": r.shop_id, "turn_id": r.turn_id, "created_at": r.created_at,
                 "idempotency_key": r.idempotency_key} for r in rows]

    def asked_since_receipt(self, tenant_id: str, shop_id: str, receipt_key: str,
                            kinds: tuple[str, ...] = ("steer_which",)) -> list[str]:
        """Code-written questions Mimi sent this shop after a delivery receipt
        (RAI-272: "Yes to the delivery receipt or the order change?"), oldest
        first, so the receipt reader knows what the shop's answer answers."""
        with self.engine.connect() as conn:
            ts = conn.execute(sa.select(s.messages.c.ts).where(
                s.messages.c.tenant_id == tenant_id,
                s.messages.c.idempotency_key == receipt_key)).scalar()
            if ts is None:
                return []
            return [str(t) for t in conn.execute(sa.select(s.messages.c.text).where(
                s.messages.c.tenant_id == tenant_id, s.messages.c.shop_id == shop_id,
                s.messages.c.direction == "out", s.messages.c.kind.in_(kinds),
                s.messages.c.ts >= ts).order_by(s.messages.c.ts)).scalars()]

    def turn_messages(self, tenant_id: str, turn_id: str) -> list[dict[str, Any]]:
        """The inbound messages a turn answers (its batch), oldest first.

        What a shop actually said, read from the store: a tool reads the
        shop's words here instead of trusting the model to copy them.
        """
        with self.engine.connect() as conn:
            t = conn.execute(sa.select(s.turns.c.batch).where(
                s.turns.c.tenant_id == tenant_id, s.turns.c.turn_id == turn_id)).first()
            ids = list(loads(t.batch, [])) if t is not None else []
            q = sa.select(s.inbound_messages).where(s.inbound_messages.c.tenant_id == tenant_id)
            q = q.where(s.inbound_messages.c.external_id.in_(ids)) if ids else \
                q.where(s.inbound_messages.c.turn_id == turn_id)
            rows = conn.execute(q).all()
            # Messages a running turn took in mid-task (RAI-301): a later
            # correction, never the answer the turn was woken for.
            mid = set(conn.execute(sa.select(s.thread_inbox.c.external_id).where(
                s.thread_inbox.c.tenant_id == tenant_id, s.thread_inbox.c.taken_by == turn_id,
                s.thread_inbox.c.status == "absorbed")).scalars())
        out = []
        for r in rows:
            env = loads(r.envelope, {})
            p = env.get("payload") or {}
            m = p.get("media") or {}
            out.append({"message_id": r.external_id, "source": r.source,
                        "received_at": r.received_at, "text": p.get("text", ""),
                        "button_id": p.get("button_id", ""),
                        "caption": m.get("caption", ""), "transcript": m.get("transcript", ""),
                        "media_kind": m.get("kind", "none"), "mime": m.get("mime", ""),
                        "sha256": m.get("sha256", ""), "media_ref": r.media_ref,
                        "mid_task": r.external_id in mid})
        out.sort(key=lambda x: (x["received_at"], x["message_id"]))
        return out

    def media_of(self, tenant_id: str, shop_id: str) -> list[dict[str, Any]]:
        """Images the shop sent, oldest first: id ``ph_<sha256[:12]>``, ref, mime."""
        with self.engine.connect() as conn:
            self._shop_row(conn, tenant_id, shop_id)
            rows = conn.execute(sa.select(s.inbound_messages).where(
                s.inbound_messages.c.tenant_id == tenant_id,
                s.inbound_messages.c.thread_id == f"{tenant_id}:shop:{shop_id}",
                s.inbound_messages.c.media_ref != "")
                .order_by(s.inbound_messages.c.received_at,
                          s.inbound_messages.c.external_id)).all()
        out = []
        for r in rows:
            m = (loads(r.envelope, {}).get("payload") or {}).get("media") or {}
            if m.get("kind") != "image" or not m.get("sha256"):
                continue
            out.append({"photo_id": "ph_" + str(m["sha256"])[:12], "ref": r.media_ref,
                        "mime": m.get("mime") or "image/jpeg", "sha256": m["sha256"],
                        "camera": bool(m.get("camera_exif")), "message_id": r.external_id,
                        "received_at": r.received_at,
                        "width": int(m.get("width") or 0), "height": int(m.get("height") or 0)})
        return out

    # ------------------------------------------------------------- control

    def is_up(self, tenant_id: str, connector: str) -> bool:
        return True

    def killed(self, tenant_id: str, helper: str) -> bool:
        with self.engine.connect() as conn:
            return conn.execute(sa.select(s.kill_switches.c.helper).where(
                s.kill_switches.c.tenant_id == tenant_id,
                s.kill_switches.c.helper.in_((helper, "*")))).first() is not None

    def daily_spend(self, tenant_id: str, day: date) -> tuple[int, int]:
        with self.engine.connect() as conn:
            t = self._tenant_row(conn, tenant_id)
            spent = conn.execute(sa.select(s.spend.c.kobo).where(
                s.spend.c.tenant_id == tenant_id, s.spend.c.day == day.isoformat())).scalar()
        return int(spent or 0), int(t.daily_cap_kobo)

    def add_spend(self, tenant_id: str, helper: str, day: date, kobo: int) -> None:
        if kobo <= 0:
            return
        with self.engine.begin() as conn:
            if not insert_ignore(conn, s.spend, {"tenant_id": tenant_id, "day": day.isoformat(),
                                                 "kobo": int(kobo)}):
                conn.execute(s.spend.update().where(
                    s.spend.c.tenant_id == tenant_id, s.spend.c.day == day.isoformat())
                    .values(kobo=s.spend.c.kobo + int(kobo)))

    def recent_count(self, tenant_id: str, kind: str, shop_id: str, window_s: float) -> int:
        """Loop guards (``mimi.harness.guards``): messages Mimi wrote to this
        shop, or follow-ups set for it, in the last ``window_s`` seconds."""
        since = time.time() - window_s
        with self.engine.connect() as conn:
            if kind == "shop_messages":
                q = sa.select(sa.func.count()).select_from(s.messages).where(
                    s.messages.c.tenant_id == tenant_id, s.messages.c.direction == "out",
                    s.messages.c.channel == "shop_whatsapp", s.messages.c.shop_id == shop_id,
                    s.messages.c.ts >= since)
            elif kind == "followups":
                q = sa.select(sa.func.count()).select_from(s.intents).where(
                    s.intents.c.tenant_id == tenant_id, s.intents.c.tool == "set_followup",
                    s.intents.c.shop_id == shop_id, s.intents.c.outcome == "applied",
                    s.intents.c.created_at >= now_iso(since))
            else:
                return 0
            return int(conn.execute(q).scalar() or 0)

    def ask_owner_once(self, tenant_id: str, topic: str, day: date) -> bool:
        with self.engine.begin() as conn:
            return insert_ignore(conn, s.asked_once, {"tenant_id": tenant_id, "topic": topic,
                                                      "day": day.isoformat()})

    # ------------------------------------------------------------- events

    def append(self, event: Event) -> bool:
        """The thread's event log; dedupes on the event id (a replayed step)."""
        with self.engine.begin() as conn:
            return self.append_in(conn, event, tenant_id=self._tenant_of(conn, event))

    @staticmethod
    def _tenant_of(conn: sa.Connection, event: Event) -> str:
        if event.job_id:
            t = conn.execute(sa.select(s.turns.c.tenant_id).where(
                s.turns.c.turn_id == event.job_id)).scalar()
            if t:
                return str(t)
        # Thread ids start with the tenant ("coral-lane:shop:shop-001").
        return event.thread_id.split(":", 1)[0]

    @staticmethod
    def append_in(conn: sa.Connection, event: Event, *, tenant_id: str) -> bool:
        data = {k: v for k, v in event.data.items()}
        return insert_ignore(conn, s.thread_events, {
            "tenant_id": tenant_id, "event_id": event.event_id, "thread_id": event.thread_id,
            "run_id": event.run_id, "turn_id": event.job_id, "seq": event.seq,
            "step": event.step, "type": event.type, "tool": event.tool, "tier": event.tier,
            "short": event.short, "cost_kobo": int(event.cost_kobo), "model": event.model,
            "duration_ms": float(event.duration_ms), "data": dumps(data),
            "at": event.at.isoformat(), "ts": event.at.timestamp()})

    # --------------------------------------------------------------- gate

    def submit(
        self, tenant_id: str, idempotency_key: str, kind: str, payload: dict[str, Any]
    ) -> str:
        """Go's intent API for the job runner's outbox (its own transaction)."""
        record = dict(payload.get("record") or {})
        record.setdefault("idempotency_key", idempotency_key)
        record.setdefault("intent_kind", kind)
        record.setdefault("tool", kind)
        record.setdefault("tier", "never")
        record.setdefault("attempt_key", idempotency_key)
        with self.engine.begin() as conn:
            self._tenant_row(conn, tenant_id)
            self.apply_intent(conn, tenant_id, record)
        return str(record["idempotency_key"])

    @staticmethod
    def apply_intent(conn: sa.Connection, tenant_id: str, record: dict[str, Any], *,
                     turn_id: str = "", thread_id: str = "", helper: str = "",
                     skill: str = "") -> tuple[str, str | None]:
        """Record one intent once. Returns (outcome, approval_id or None).

        ``never`` is refused; a shadow-mode record is ``shadowed`` (recorded,
        applied never); ``approve`` waits in the owner's approvals; ``auto``
        and ``notify`` are applied. Nothing here can move money: a
        money verb, or a tool the policy has no row for, is refused whatever
        tier it arrives with.
        """
        key = str(record["idempotency_key"])
        tool = str(record.get("tool", ""))
        kind = str(record.get("intent_kind", tool))
        tier = str(record.get("tier") or "never")
        existing = conn.execute(sa.select(s.intents.c.outcome).where(
            s.intents.c.tenant_id == tenant_id, s.intents.c.idempotency_key == key)).scalar()
        if existing is not None:
            apr = conn.execute(sa.select(s.approvals.c.approval_id).where(
                s.approvals.c.tenant_id == tenant_id,
                s.approvals.c.idempotency_key == key)).scalar()
            return f"duplicate:{existing}", apr
        if tier == "never" or moves_money(tool, kind) or not go_allows(tool, kind):
            # A tool with no policy row is refused even at tier auto: the tier on
            # a record is the caller's word; the allowlist is Go's.
            outcome = "refused"
        elif record.get("shadow"):
            # Shadow mode (RAI-235): the intent is recorded and applied never.
            # No approval, no credit request, no message, whatever its tier.
            outcome = "shadowed"
        elif tier == "approve":
            outcome = "queued_for_approval"
        else:
            outcome = "applied"
        now = now_iso()
        payload = dict(record.get("payload") or {})
        shop_id = str(record.get("shop_id") or payload.get("shop_id") or "")
        insert_ignore(conn, s.intents, {
            "tenant_id": tenant_id, "idempotency_key": key,
            "attempt_key": str(record.get("attempt_key", "")), "turn_id": turn_id,
            "thread_id": thread_id, "tool": tool, "intent_kind": kind, "tier": tier,
            "shop_id": shop_id, "payload": dumps(payload),
            "preview": str(record.get("preview", "")), "outcome": outcome, "created_at": now})
        approval_id: str | None = None
        if outcome == "queued_for_approval":
            approval_id = approval_id_for(tenant_id, key)
            insert_ignore(conn, s.approvals, {
                "approval_id": approval_id, "tenant_id": tenant_id, "idempotency_key": key,
                "turn_id": turn_id, "thread_id": thread_id, "helper": helper, "skill": skill,
                "shop_id": shop_id, "tool": tool, "intent_kind": kind,
                "preview": str(record.get("preview", "")), "payload": dumps(payload),
                "status": "pending", "created_at": now, "created_ts": time.time()})
        if outcome not in ("refused", "shadowed") and is_credit_intent(tool, kind) and shop_id:
            amount = 0
            for f in ("amount_kobo", "new_limit_kobo", "limit_kobo", "bump_kobo"):
                if isinstance(payload.get(f), int):
                    amount = int(payload[f])
                    break
            insert_ignore(conn, s.credit_requests, {
                "request_id": "crq_" + key[-20:], "tenant_id": tenant_id, "shop_id": shop_id,
                "amount_kobo": amount, "reason": str(record.get("preview", ""))[:500],
                "status": "pending" if approval_id else "noted", "source_key": key,
                "approval_id": approval_id, "created_at": now})
        return outcome, approval_id

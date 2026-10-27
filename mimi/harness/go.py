"""The system of record, as the harness sees it, and its in-process stand-in.

Python is the brain; Go is the system of record (tenancy, consent, the book,
the kobo ledger, the outbox, approvals). The harness talks to it through
four narrow ports:

* ``BookAPI``: consent-checked reads for one tenant (balances, invoices,
  payments, the bank feed). Go enforces the tenant and consent again on its
  side, so a Python bug can never leak another distributor's book.
* ``ControlPlane``: the daily naira cap per distributor, kill switches per
  helper and per distributor, connector status, and "ask the owner once".
* ``EventLog``: the thread's append-only event log (dedupes on event id).
* ``IntentGate``: where intents land, deduped on their idempotency key.

``FakeGo`` implements all four over the synthetic Coral Lane world. It counts
``book_writes`` so a test can prove the loop itself never writes: only the
outbox, after the step is saved, hands intents to the gate.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from mimi.harness.types import Event, IntentRecord
from mimi.world.model import World


class CrossTenant(PermissionError):
    """A read named another distributor's data. Go refuses it too."""


class NoConsent(PermissionError):
    """The shop has not granted this scope to the distributor."""


@dataclass(frozen=True)
class ConsentGrants:
    """What this run may read: one tenant, and per shop the scopes it granted.

    ``org_scopes`` cover reads across the tenant's consenting shops (the
    owner's "who owes me"); each shop row inside still needs its own grant.
    """

    tenant_id: str
    shops: dict[str, frozenset[str]]
    org_scopes: frozenset[str] = frozenset()

    def allows(self, shop_id: str, scope: str) -> bool:
        granted = self.shops.get(shop_id, frozenset())
        family = scope.split(":", 1)[0] + ":*"
        return scope in granted or family in granted

    def allows_org(self, scope: str) -> bool:
        return scope in self.org_scopes or "org:*" in self.org_scopes


# ---------------------------------------------------------------------- ports


class BookAPI(Protocol):
    def grants(self, tenant_id: str) -> ConsentGrants: ...

    def shop(self, tenant_id: str, shop_id: str) -> dict[str, Any]: ...

    def receivables(self, tenant_id: str, shop_id: str | None) -> list[dict[str, Any]]: ...

    def payments(self, tenant_id: str, shop_id: str, limit: int) -> list[dict[str, Any]]: ...

    def promises(self, tenant_id: str, shop_id: str) -> list[dict[str, Any]]: ...

    def collection_account(self, tenant_id: str) -> dict[str, Any]: ...

    def bank_feed_match(
        self, tenant_id: str, shop_id: str, amount_kobo: int, reference: str
    ) -> dict[str, Any] | None: ...

    def today(self) -> date: ...


class Connectors(Protocol):
    def is_up(self, tenant_id: str, connector: str) -> bool: ...


class ControlPlane(Connectors, Protocol):
    def killed(self, tenant_id: str, helper: str) -> bool: ...

    def daily_spend(self, tenant_id: str, day: date) -> tuple[int, int]:
        """(spent_kobo, cap_kobo) for the distributor's day, Africa/Lagos."""
        ...

    def add_spend(self, tenant_id: str, helper: str, day: date, kobo: int) -> None: ...

    def ask_owner_once(self, tenant_id: str, topic: str, day: date) -> bool:
        """True the first time this topic is raised today; False after."""
        ...


class EventLog(Protocol):
    def append(self, event: Event) -> bool: ...


class IntentGate(Protocol):
    def submit(
        self, tenant_id: str, idempotency_key: str, kind: str, payload: dict[str, Any]
    ) -> str: ...


# ----------------------------------------------------------------- the fake


@dataclass
class GateDecision:
    key: str
    kind: str
    tier: str
    outcome: str  # applied | queued_for_approval | refused | duplicate


@dataclass
class FakeGo:
    """Go's ports over the synthetic world. Same dedupe and tenancy rules."""

    world: World
    daily_cap_kobo: int = 50_000  # N500 per distributor per day (proposed)
    withheld: dict[str, frozenset[str]] = field(default_factory=dict)
    killed_helpers: set[tuple[str, str]] = field(default_factory=set)
    killed_tenants: set[str] = field(default_factory=set)
    down: set[tuple[str, str]] = field(default_factory=set)
    # Book state that only the gate writes.
    book_writes: int = 0
    reads: int = 0
    events: list[Event] = field(default_factory=list)
    accepted: dict[str, GateDecision] = field(default_factory=dict)
    attempts_seen: set[str] = field(default_factory=set)
    applied: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    sent_messages: list[dict[str, Any]] = field(default_factory=list)
    submits: int = 0
    _spend: dict[tuple[str, date], int] = field(default_factory=lambda: defaultdict(int))
    _asked: set[tuple[str, str, date]] = field(default_factory=set)
    _event_ids: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------- tenancy

    def _tenant(self, tenant_id: str) -> None:
        if tenant_id != self.world.tenant_id:
            raise CrossTenant(tenant_id)

    def _own_shop(self, tenant_id: str, shop_id: str) -> None:
        self._tenant(tenant_id)
        try:
            self.world.shop(shop_id)
        except KeyError as exc:
            raise CrossTenant(shop_id) from exc

    # ---------------------------------------------------------------- book

    def grants(self, tenant_id: str) -> ConsentGrants:
        self._tenant(tenant_id)
        shops: dict[str, frozenset[str]] = {}
        for shop in self.world.shops:
            withheld = self.withheld.get(shop.shop_id, frozenset())
            if "shop:*" in withheld:
                continue
            base = {"shop:balance", "shop:invoices", "shop:payments", "shop:messages",
                    "shop:promises"}
            shops[shop.shop_id] = frozenset(base - set(withheld))
        return ConsentGrants(tenant_id, shops, frozenset({"org:receivables", "org:account",
                                                          "org:owner", "org:reps",
                                                          "org:helpers", "org:plan"}))

    def today(self) -> date:
        return self.world.date_of(self.world.live_day)

    def shop(self, tenant_id: str, shop_id: str) -> dict[str, Any]:
        self._own_shop(tenant_id, shop_id)
        self.reads += 1
        s = self.world.shop(shop_id)
        return {"shop_id": s.shop_id, "name": s.name, "owner": s.owner, "area": s.area,
                "rep_id": s.rep_id, "credit_limit_kobo": s.credit_limit_kobo}

    def receivables(self, tenant_id: str, shop_id: str | None) -> list[dict[str, Any]]:
        self._tenant(tenant_id)
        if shop_id is not None:
            self._own_shop(tenant_id, shop_id)
        self.reads += 1
        live = self.world.live_day
        out = []
        for r in self.world.receivables:
            if shop_id is not None and r.shop_id != shop_id:
                continue
            if r.outstanding_kobo <= 0:
                continue
            out.append({
                "delivery_id": r.delivery_id,
                "shop_id": r.shop_id,
                "shop_name": self.world.shop(r.shop_id).name,
                "due_on": r.due_on,
                "days_overdue": max(0, live - r.due_day),
                "total_kobo": r.total_kobo,
                "paid_kobo": r.paid_kobo,
                "outstanding_kobo": r.outstanding_kobo,
                "status": r.status,
            })
        return out

    def payments(self, tenant_id: str, shop_id: str, limit: int) -> list[dict[str, Any]]:
        self._own_shop(tenant_id, shop_id)
        self.reads += 1
        feed = {c.payment_id for c in self.world.bank_feed}
        rows = [p for p in self.world.payments if p.shop_id == shop_id]
        rows.sort(key=lambda p: (p.day, p.payment_id), reverse=True)
        return [{
            "payment_id": p.payment_id,
            "paid_on": p.paid_on,
            "amount_kobo": 0 if p.fake_alert else p.amount_kobo,
            "claimed_kobo": p.amount_kobo,
            "method": p.method,
            "status": p.status,
            "bank_confirmed": p.payment_id in feed,
        } for p in rows[:limit]]

    def promises(self, tenant_id: str, shop_id: str) -> list[dict[str, Any]]:
        self._own_shop(tenant_id, shop_id)
        return [{"made_on": self.world.date_of(p.made_day).isoformat(),
                 "promised_on": self.world.date_of(p.promised_day).isoformat(),
                 "kept": p.kept, "delivery_id": p.delivery_id}
                for p in self.world.promises if p.shop_id == shop_id]

    def collection_account(self, tenant_id: str) -> dict[str, Any]:
        self._tenant(tenant_id)
        return {"account_name": "CORAL LANE TRADING", "bank": "Demo Bank",
                "account_number": "0000000000"}

    def bank_feed_match(
        self, tenant_id: str, shop_id: str, amount_kobo: int, reference: str
    ) -> dict[str, Any] | None:
        self._own_shop(tenant_id, shop_id)
        self.reads += 1
        pays = {p.payment_id: p for p in self.world.payments if p.shop_id == shop_id}
        for c in self.world.bank_feed:
            p = pays.get(c.payment_id)
            if p is None:
                continue
            if c.amount_kobo == amount_kobo and (not reference or reference in c.narration):
                return {"credit_id": c.credit_id, "payment_id": c.payment_id,
                        "amount_kobo": c.amount_kobo, "received_at": c.received_at}
        return None

    # ------------------------------------------------------------- control

    def is_up(self, tenant_id: str, connector: str) -> bool:
        return (tenant_id, connector) not in self.down

    def killed(self, tenant_id: str, helper: str) -> bool:
        return tenant_id in self.killed_tenants or (tenant_id, helper) in self.killed_helpers

    def daily_spend(self, tenant_id: str, day: date) -> tuple[int, int]:
        with self._lock:
            return self._spend[(tenant_id, day)], self.daily_cap_kobo

    def add_spend(self, tenant_id: str, helper: str, day: date, kobo: int) -> None:
        with self._lock:
            self._spend[(tenant_id, day)] += max(0, kobo)

    def ask_owner_once(self, tenant_id: str, topic: str, day: date) -> bool:
        with self._lock:
            key = (tenant_id, topic, day)
            if key in self._asked:
                return False
            self._asked.add(key)
            return True

    # ------------------------------------------------------------- events

    def append(self, event: Event) -> bool:
        with self._lock:
            if event.event_id in self._event_ids:
                return False
            self._event_ids.add(event.event_id)
            self.events.append(event)
            return True

    # --------------------------------------------------------------- gate

    def submit(
        self, tenant_id: str, idempotency_key: str, kind: str, payload: dict[str, Any]
    ) -> str:
        """Go's intent API: dedupe on the outbox key, then on the business key.

        A transport retry repeats the outbox key; a resumed or second job that
        re-proposes the same reminder repeats the business key. Either way the
        book changes once.
        """
        with self._lock:
            self.submits += 1
            self._tenant(tenant_id)
            record = payload.get("record") or {}
            business = str(record.get("idempotency_key") or idempotency_key)
            if idempotency_key in self.attempts_seen or business in self.accepted:
                self.attempts_seen.add(idempotency_key)
                return business
            self.attempts_seen.add(idempotency_key)
            tier = str(record.get("tier") or "never")
            if tier == "never":
                outcome = "refused"
            elif tier == "approve":
                outcome = "queued_for_approval"
                self.approvals.append(record)
            else:
                outcome = "applied"
                self.applied.append(record)
                if record.get("tool") == "send_to_user" or kind.endswith("send_to_user"):
                    self.sent_messages.append(record)
            self.book_writes += 1
            self.accepted[business] = GateDecision(business, kind, tier, outcome)
            return business

    def submit_record(self, rec: IntentRecord, attempt_key: str = "") -> str:
        """Submit an ``IntentRecord`` directly (tests and the eval)."""
        return self.submit(rec.tenant_id, attempt_key or rec.attempt_key, rec.intent_kind,
                           {"record": rec.model_dump(mode="json")})

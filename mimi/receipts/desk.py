"""The front door for the receipt flow.

``open_receipt`` turns a logged delivery into a durable job (one per delivery;
the job id is the delivery id, so logging it twice is a no-op).
``receive`` takes a shop's WhatsApp webhook: dedupes on the message id, routes
it to the shop's oldest open receipt (one thread, one job, one turn at a
time), stores it and wakes the job. A reply with no open receipt (a second
YES after the job finished) is kept for the console and changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from mimi.receipts.flow import HELPER
from mimi.receipts.inbox import ShopInbox, ShopReply
from mimi.runtime.budgets import Budget
from mimi.runtime.job import Job
from mimi.runtime.states import JobState
from mimi.runtime.store import JobStore


@dataclass(frozen=True)
class ShopWebhook:
    message_id: str
    shop_id: str
    sender: str
    text: str
    received_at: datetime


@dataclass
class ReceiptDesk:
    store: JobStore
    inbox: ShopInbox
    tenant_id: str
    open_by_shop: dict[str, list[str]] = field(default_factory=dict)
    unrouted: list[ShopWebhook] = field(default_factory=list)
    duplicates: int = 0

    def open_receipt(self, delivery_id: str, shop_id: str) -> str:
        job_id = f"rcpt-{delivery_id}"
        try:
            self.store.get(job_id)
        except KeyError:
            self.store.create(
                Job(
                    job_id=job_id,
                    tenant_id=self.tenant_id,
                    helper=HELPER,
                    thread_id=f"wa-{shop_id}",
                    wake_source="delivery_logged",
                    goal=delivery_id,
                    budget=Budget(max_steps=16, max_seconds=120.0, max_kobo=5_000),
                )
            )
            self.open_by_shop.setdefault(shop_id, []).append(job_id)
        return job_id

    def _open_job(self, shop_id: str) -> str | None:
        for job_id in self.open_by_shop.get(shop_id, []):
            if self.store.get(job_id).state is not JobState.DONE:
                return job_id
        return None

    def receive(self, hook: ShopWebhook) -> str:
        """Returns "routed", "duplicate" or "unrouted"."""
        job_id = self._open_job(hook.shop_id)
        if job_id is None:
            self.unrouted.append(hook)
            return "unrouted"
        added = self.inbox.add(
            ShopReply(
                message_id=hook.message_id,
                job_id=job_id,
                shop_id=hook.shop_id,
                sender=hook.sender,
                text=hook.text,
                received_at=hook.received_at.isoformat(),
            )
        )
        if not added:
            self.duplicates += 1
            return "duplicate"
        self.store.wake(job_id)
        return "routed"

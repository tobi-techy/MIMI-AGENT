"""Payment screenshots on the always-on runtime: one durable job per image.

``ProofDesk.receive`` takes a shop's image message: dedupes on the message
id, runs the ``read_payment_image`` tool once (Muse Spark copies the fields;
the bytes are then dropped, only the SHA-256 and size are kept), stores the
reading in the ``ProofLedger`` and opens job ``proof-<message id>``. The
same file again from the same shop reuses the earlier reading (no model
call) and is answered as a duplicate.

``ProofFlow`` is that job's step:

* ``new``: the code checks (``checks.assess``) against the shop's open
  invoices and every proof already seen, then one action:

  - likely genuine: ``RecordPaymentProof`` (level ``likely``) and "Thanks. I
    can see N84,500 ... likely paid until the bank confirms"; then it waits
    for the bank;
  - looks fake: ``ProofForApproval`` with every failed rule named, and the
    neutral "Thanks, I've sent this to Coral Lane Trading to confirm with the
    bank. Nothing has changed on your account yet." No accusation;
  - cannot tell: one clear ask for a clearer screenshot. A second unreadable
    one within a day goes to the owner instead of a second ask;
  - the same image again from the same shop: "I already have this
    screenshot", nothing matched twice.

* ``awaiting_bank``: checks the distributor's bank feed (read-only) every
  hour. A credit that fits (``witness.find_credit``) gives
  ``RecordBankWitness`` (rep, shop and bank agree: verified). No credit by the
  end of the bank window puts the proof in the owner's approvals.

Every reply is a fixed template whose figures are checked against the
code's own numbers before it is proposed (``copy.figures_ok``). Mimi never
moves money and never changes credit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from mimi.proof import copy
from mimi.proof.checks import Assessment, OpenInvoice, assess
from mimi.proof.intents import ProofForApproval, RecordBankWitness, RecordPaymentProof
from mimi.proof.ledger import ProofLedger, ProofRecord
from mimi.proof.reading import ProofReading, ReadResult
from mimi.proof.witness import BANK_WINDOW, BankFeed, find_credit
from mimi.receipts.intents import MessagePurpose, SendShopMessage
from mimi.runtime.budgets import Budget
from mimi.runtime.job import Job
from mimi.runtime.steps import StepContext, StepOutcome
from mimi.runtime.store import JobStore
from mimi.types import ShopScope
from mimi.world.model import World

PROOF_HELPER = "payment_proof"
ASK_MEMORY = timedelta(hours=24)
# shop_id, now -> the shop's open invoices
BookFor = Callable[[str, datetime], list[OpenInvoice]]
Reader = Callable[[bytes, str], ReadResult]


def world_book(world: World, live_logged: Callable[[], set[str]] | None = None) -> BookFor:
    """Open invoices from the world: open receivables plus the live day's
    deliveries already logged (``live_logged`` gives their ids)."""

    def at(delivery_id: str) -> datetime:
        from mimi.proof.parse import LAGOS

        d = world.delivery(delivery_id)
        day = datetime.fromisoformat(d.delivered_on)
        return day.replace(hour=d.minute // 60, minute=d.minute % 60, tzinfo=LAGOS)

    def book(shop_id: str, now: datetime) -> list[OpenInvoice]:
        out = [
            OpenInvoice(r.delivery_id, at(r.delivery_id), r.outstanding_kobo,
                        world.delivery(r.delivery_id).rep_id)
            for r in world.receivables
            if r.shop_id == shop_id and r.outstanding_kobo > 0
        ]
        logged = live_logged() if live_logged else set()
        out += [
            OpenInvoice(d.delivery_id, at(d.delivery_id), d.total_kobo, d.rep_id)
            for d in world.live_deliveries
            if d.shop_id == shop_id and d.delivery_id in logged and at(d.delivery_id) <= now
        ]
        return out

    return book


def less_prior_proofs(
    invoices: Sequence[OpenInvoice], records: Sequence[ProofRecord], skip: str = ""
) -> list[OpenInvoice]:
    """The book minus what earlier likely or verified proofs already cover,
    oldest invoice first, so one payment is never matched twice."""
    left = {i.delivery_id: i.outstanding_kobo for i in invoices}
    for r in records:
        if r.proof_id == skip or r.level not in ("likely", "verified") or not r.amount_kobo:
            continue
        amount = r.amount_kobo
        for d in r.delivery_ids:
            if d in left and amount > 0:
                take = min(left[d], amount)
                left[d] -= take
                amount -= take
    return [
        OpenInvoice(i.delivery_id, i.delivered_at, left[i.delivery_id], i.rep_id)
        for i in invoices
    ]


@dataclass(frozen=True)
class ImageWebhook:
    message_id: str
    shop_id: str
    sender: str
    received_at: datetime
    image: bytes
    mime: str = "image/jpeg"
    caption: str = ""


@dataclass
class ProofDesk:
    store: JobStore
    ledger: ProofLedger
    tenant_id: str
    reader: Reader
    duplicates: int = 0
    reads: list[ReadResult] = field(default_factory=list)

    def receive(self, hook: ImageWebhook) -> str:
        """Returns "routed" or "duplicate" (a webhook retry)."""
        try:
            self.ledger.get(hook.message_id)
        except KeyError:
            pass
        else:
            self.duplicates += 1
            return "duplicate"
        res = self._same_image(hook) or self.reader(hook.image, hook.mime)
        self.reads.append(res)
        added = self.ledger.add(
            ProofRecord(
                proof_id=hook.message_id,
                tenant_id=self.tenant_id,
                shop_id=hook.shop_id,
                sender=hook.sender,
                received_at=hook.received_at.isoformat(),
                image_sha256=res.image_sha256,
                image_bytes=len(hook.image),
                reading_json=res.reading.model_dump_json(),
                read_via=res.via,
                prompt_tokens=res.prompt_tokens,
                completion_tokens=res.completion_tokens,
            )
        )
        if not added:
            self.duplicates += 1
            return "duplicate"
        job_id = f"proof-{hook.message_id}"
        self.store.create(
            Job(
                job_id=job_id,
                tenant_id=self.tenant_id,
                helper=PROOF_HELPER,
                thread_id=f"wa-{hook.shop_id}",
                wake_source="payment_screenshot",
                goal=hook.message_id,
                budget=Budget(max_steps=60, max_seconds=120.0, max_kobo=5_000),
            )
        )
        return "routed"


    def _same_image(self, hook: ImageWebhook) -> ReadResult | None:
        """The same file again from the same shop: reuse its reading, so a
        resend costs no model call. The flow then answers it as a duplicate."""
        sha = hashlib.sha256(hook.image).hexdigest()
        for p in self.ledger.by_image(sha):
            if p.shop_id == hook.shop_id:
                prior = self.ledger.get(p.proof_id)
                reading = ProofReading.model_validate_json(prior.reading_json)
                return ReadResult(reading, "same_image", sha)
        return None


@dataclass
class ProofFlow:
    ledger: ProofLedger
    book: BookFor
    bank: BankFeed
    distributor: str
    bank_window: timedelta = BANK_WINDOW
    check_every: timedelta = timedelta(hours=1)
    assessments: list[tuple[str, Assessment]] = field(default_factory=list)

    def __call__(self, ctx: StepContext) -> StepOutcome:
        state = json.loads(ctx.working_note) if ctx.working_note else {"stage": "new"}
        rec = self.ledger.get(ctx.goal)
        scope = ShopScope(ctx.tenant_id, rec.shop_id)
        if state["stage"] == "new":
            return self._assess(ctx, rec, scope)
        return self._await_bank(ctx, rec, scope, state)

    # ------------------------------------------------------------ helpers

    def _say(
        self,
        scope: ShopScope,
        rec: ProofRecord,
        purpose: MessagePurpose,
        text: str,
        allowed: Sequence[int],
        delivery_ids: Sequence[str] = (),
    ) -> SendShopMessage:
        if not copy.figures_ok(text, allowed):
            raise ValueError(f"reply for {rec.proof_id} carries a figure code did not allow")
        return SendShopMessage(
            scope,
            delivery_ids[0] if delivery_ids else "",
            purpose,
            text,
            total_kobo=allowed[0] if allowed else 0,
        )

    def _assess(self, ctx: StepContext, rec: ProofRecord, scope: ShopScope) -> StepOutcome:
        reading = ProofReading.model_validate_json(rec.reading_json)
        earlier = [r for r in self.ledger.for_shop(rec.shop_id) if r.proof_id != rec.proof_id]
        invoices = less_prior_proofs(self.book(rec.shop_id, ctx.now), earlier)
        a = assess(
            reading,
            shop_id=rec.shop_id,
            proof_id=rec.proof_id,
            image_sha256=rec.image_sha256,
            received_at=datetime.fromisoformat(rec.received_at),
            invoices=invoices,
            history=self.ledger,
        )
        self.assessments.append((rec.proof_id, a))
        checks = tuple(f"{c.name}:{c.result}" for c in a.checks)
        trace: dict[str, str | int | float] = {
            "tool": "read_payment_image",
            "read_via": rec.read_via,
            "verdict": a.verdict,
            "checks": ",".join(checks),
            "match": a.match.kind,
            "prompt_tokens": rec.prompt_tokens,
            "completion_tokens": rec.completion_tokens,
        }
        common = {
            "verdict": a.verdict,
            "reasons_json": json.dumps(list(a.reasons)),
            "amount_kobo": a.amount_kobo,
            "paid_at": a.paid_at.isoformat() if a.paid_at else "",
            "reference": a.reference,
            "session_id": a.session_id,
        }
        if a.duplicate_of:
            self.ledger.update(rec.proof_id, level="duplicate", verdict=a.verdict,
                               reasons_json=json.dumps([f"same image as {a.duplicate_of}"]))
            msg = self._say(scope, rec, "proof_duplicate", copy.duplicate_text(), [])
            return StepOutcome(kind="done", intents=(msg,), result="duplicate", trace=trace)
        if a.verdict == "likely_genuine":
            assert a.amount_kobo is not None and a.paid_at is not None
            self.ledger.update(
                rec.proof_id,
                level="likely",
                delivery_ids_json=json.dumps(list(a.match.delivery_ids)),
                **common,
            )
            proof = RecordPaymentProof(
                scope,
                rec.proof_id,
                a.amount_kobo,
                a.match.delivery_ids,
                a.match.kind,
                a.match.open_kobo,
                a.paid_at.isoformat(),
                a.bank,
                a.sender,
                a.reference,
                a.session_id,
                rec.image_sha256,
                checks=checks,
                witnesses=(f"shop:{rec.proof_id}",),
            )
            msg = self._say(
                scope,
                rec,
                "proof_likely",
                copy.likely_text(a, self.distributor),
                [a.amount_kobo],
                a.match.delivery_ids,
            )
            deadline = ctx.now + self.bank_window
            note = {"stage": "awaiting_bank", "deadline": deadline.isoformat()}
            return StepOutcome(
                kind="sleep",
                intents=(proof, msg),
                working_note=json.dumps(note),
                sleep_until=min(ctx.now + self.check_every, deadline),
                trace=trace,
            )
        if a.verdict == "looks_fake":
            self.ledger.update(rec.proof_id, level="review", **common)
            flag = self._approval(scope, rec, a, "looks_fake")
            msg = self._say(scope, rec, "proof_review", copy.review_text(self.distributor), [])
            return StepOutcome(kind="done", intents=(flag, msg), result="review", trace=trace)
        # cannot_tell: one ask a day per shop, then the owner.
        now = ctx.now
        asked_recently = any(
            r.level == "ask" and now - datetime.fromisoformat(r.received_at) <= ASK_MEMORY
            for r in earlier
        )
        if asked_recently:
            self.ledger.update(rec.proof_id, level="review", **common)
            flag = self._approval(scope, rec, a, "unreadable_again")
            msg = self._say(
                scope, rec, "proof_review", copy.unreadable_again_text(self.distributor), []
            )
            return StepOutcome(kind="done", intents=(flag, msg), result="review", trace=trace)
        self.ledger.update(rec.proof_id, level="ask", **common)
        msg = self._say(scope, rec, "proof_ask", copy.ask_text(), [])
        return StepOutcome(kind="done", intents=(msg,), result="asked", trace=trace)

    def _approval(
        self, scope: ShopScope, rec: ProofRecord, a: Assessment, reason: str
    ) -> ProofForApproval:
        return ProofForApproval(
            scope,
            rec.proof_id,
            reason,  # type: ignore[arg-type]
            failed_rules=tuple(c.name for c in a.failed),
            reasons=a.reasons,
            amount_kobo_as_shown=a.amount_kobo,
            paid_at_as_shown=a.paid_at.isoformat() if a.paid_at else "",
            bank_as_shown=a.bank,
            sender_as_shown=a.sender,
            reference_as_shown=a.reference,
            image_sha256=rec.image_sha256,
            shop_message_id=rec.proof_id,
        )

    def _await_bank(
        self, ctx: StepContext, rec: ProofRecord, scope: ShopScope, state: dict
    ) -> StepOutcome:
        deadline = datetime.fromisoformat(state["deadline"])
        assert rec.amount_kobo is not None
        credit = find_credit(
            self.bank.credits(ctx.tenant_id),
            amount_kobo=rec.amount_kobo,
            paid_at=datetime.fromisoformat(rec.paid_at),
            reference=rec.reference,
            session_id=rec.session_id,
            sender=json.loads(rec.reading_json).get("sender_name") or "",
            used=self.ledger.credit_used,
        )
        invoices = {i.delivery_id: i for i in self.book(rec.shop_id, ctx.now)}
        reps = [invoices[d].rep_id for d in rec.delivery_ids if d in invoices and invoices[d].rep_id]
        if credit is not None and rec.delivery_ids and reps:
            self.ledger.update(rec.proof_id, level="verified", bank_credit_id=credit.credit_id)
            witness = RecordBankWitness(
                scope,
                rec.proof_id,
                credit.credit_id,
                credit.amount_kobo,
                credit.received_at,
                rec.delivery_ids,
                (
                    f"rep:{reps[0]}:{rec.delivery_ids[0]}",
                    f"shop:{rec.proof_id}",
                    f"bank:{credit.credit_id}",
                ),
            )
            msg = self._say(
                scope,
                rec,
                "proof_verified",
                copy.verified_text(credit.amount_kobo, self.distributor),
                [credit.amount_kobo],
                rec.delivery_ids,
            )
            return StepOutcome(
                kind="done",
                intents=(witness, msg),
                result="verified",
                trace={"bank_credit": credit.credit_id},
            )
        if ctx.now >= deadline:
            self.ledger.update(rec.proof_id, level="review")
            flag = ProofForApproval(
                scope,
                rec.proof_id,
                "no_bank_credit",
                failed_rules=("bank_witness",),
                reasons=(
                    "bank_witness: no credit with this amount and reference reached the bank "
                    f"feed within {int(self.bank_window.total_seconds() // 3600)} hours",
                ),
                amount_kobo_as_shown=rec.amount_kobo,
                paid_at_as_shown=rec.paid_at,
                reference_as_shown=rec.reference,
                image_sha256=rec.image_sha256,
                shop_message_id=rec.proof_id,
            )
            return StepOutcome(kind="done", intents=(flag,), result="likely_unwitnessed")
        return StepOutcome(
            kind="sleep",
            working_note=json.dumps(state),
            sleep_until=min(ctx.now + self.check_every, deadline),
            trace={"bank_credit": "none_yet"},
        )

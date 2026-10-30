"""Proof's screenshot tools on the harness (RAI-268 behind the router, RAI-272).

The Day 3 payment-screenshot flow (``flow.py``) as harness tools, so a shop's
screenshot routed to ``proof/fake-alert-check`` is read, checked and answered
by the one agent loop:

* ``check_payment_screenshot`` (read): Muse Spark copies the fields exactly as
  printed (``reading.ScreenshotReader``; the image goes to the reader, never
  into the loop's context), then code checks them (``checks.assess``) against
  the shop's open invoices, the distributor's own account and every proof
  already seen. Returns the verdict, the failed rules and the exact reply.
* ``record_payment_proof`` (write, notify): a ``likely_genuine`` screenshot
  becomes a proof at level ``likely``. Never verified: only the bank feed
  verifies a payment.
* ``flag_payment_proof`` (write, approve): a screenshot that looks fake, or
  one Mimi still cannot read after one ask, goes to the owner's approvals with
  every failed rule named. Nothing on the shop's book changes.

Every figure in a reply is formatted by code (``copy``); the tools recompute
the assessment from the image and the book, so the model can only pass ids.
Nothing here moves money.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mimi.harness.errors import ToolError
from mimi.harness.registry import ToolContext, ToolRegistry
from mimi.proof import copy
from mimi.proof.banks import CORAL_LANE_ACCOUNT, CollectionAccount
from mimi.proof.checks import Assessment, OpenInvoice, PriorProof, assess
from mimi.proof.parse import LAGOS
from mimi.proof.reading import ReadResult

ASK_MEMORY = timedelta(hours=24)
PROOF_TOOL_NAMES = ("record_payment_proof", "flag_payment_proof")

# (tenant_id, shop_id, photo_id) -> (bytes, mime, sha256, received_at ISO) or None
ImageSource = Callable[[str, str, str], "tuple[bytes, str, str, str] | None"]
Reader = Callable[[bytes, str], ReadResult]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class ProofPorts:
    images: ImageSource
    reader: Reader
    distributor: str = "Coral Lane Trading"
    # One reading per image (the read is pure for one image): a second look in
    # the same or a later turn makes no second model call. Not book state.
    readings: dict[tuple[str, str], ReadResult] = field(default_factory=dict)
    reads: int = 0


class ScreenshotIn(_In):
    shop_id: str = Field(description="The shop's id, e.g. shop-007.")
    photo_id: str = Field(description="The screenshot's id from the shop's message, ph_...")


class CheckOut(_Out):
    shop_id: str
    photo_id: str
    verdict: Literal["likely_genuine", "looks_fake", "cannot_tell", "duplicate"]
    amount_kobo: int | None
    amount_naira: str
    matched_delivery_ids: list[str]
    match: str
    failed_rules: list[str]
    reasons_for_owner: list[str]
    shop_reply: str
    next_step: str

    def short(self) -> str:
        return f"{self.verdict}; {len(self.failed_rules)} rule(s) failed"


class ProofOut(_Out):
    proof_id: str
    shop_id: str
    photo_id: str
    level: Literal["likely"]
    amount_kobo: int
    amount_naira: str
    delivery_ids: list[str]
    match: str
    paid_at: str
    bank: str
    sender: str
    reference: str
    image_sha256: str
    verified: Literal[False] = False
    moves_money: Literal[False] = False


class FlagOut(_Out):
    proof_id: str
    shop_id: str
    photo_id: str
    reason: Literal["looks_fake", "unreadable_again"]
    failed_rules: list[str]
    reasons: list[str]
    amount_kobo_as_shown: int | None
    reference_as_shown: str
    image_sha256: str
    # The neutral reply Go sends the shop now, whatever the owner decides.
    shop_notice: str
    status: Literal["for_owner"] = "for_owner"
    changes_book: Literal[False] = False
    moves_money: Literal[False] = False


@dataclass
class _Seen:
    """Proofs Go already recorded for this distributor (any shop)."""

    prior: list[PriorProof]

    def by_image(self, sha256: str) -> list[PriorProof]:
        return [p for p in self.prior if p.image_sha256 == sha256]

    def by_reference(self, ref: str) -> list[PriorProof]:
        return [p for p in self.prior if ref and p.reference == ref]


def proof_id_for(tenant_id: str, shop_id: str, photo_id: str) -> str:
    return "prf_" + hashlib.sha256(f"{tenant_id}|{shop_id}|{photo_id}".encode()).hexdigest()[:16]


def _account(ctx: ToolContext) -> CollectionAccount:
    raw = ctx.book.collection_account(ctx.tenant_id) or {}
    if raw.get("account_number"):
        return CollectionAccount(str(raw.get("account_name", "")), str(raw.get("bank", "")),
                                 str(raw["account_number"]))
    return CORAL_LANE_ACCOUNT


def _invoices(ctx: ToolContext, shop_id: str) -> list[OpenInvoice]:
    """Open receivables plus the live day's deliveries (open from delivery)."""
    by_id = {d["delivery_id"]: d for d in ctx.book.deliveries(ctx.tenant_id, shop_id)}

    def at(d: dict[str, Any]) -> datetime:
        day = datetime.fromisoformat(d["delivered_on"])
        return day.replace(hour=d["minute"] // 60, minute=d["minute"] % 60, tzinfo=LAGOS)

    out = []
    for r in ctx.book.receivables(ctx.tenant_id, shop_id):
        d = by_id.get(r["delivery_id"])
        if d is not None:
            out.append(OpenInvoice(r["delivery_id"], at(d), int(r["outstanding_kobo"]),
                                   d.get("rep_id", "")))
    for d in by_id.values():
        if d["live"]:
            out.append(OpenInvoice(d["delivery_id"], at(d), int(d["total_kobo"]),
                                   d.get("rep_id", "")))
    out.sort(key=lambda i: i.delivered_at)
    return out


def _prior(ctx: ToolContext, skip: str) -> tuple[list[PriorProof], list[dict[str, Any]]]:
    rows = ctx.book.intents_of(ctx.tenant_id, "", PROOF_TOOL_NAMES)
    prior = []
    for r in rows:
        p = r["payload"]
        if r["outcome"] == "refused":
            continue
        pid = str(p.get("proof_id", ""))
        if pid == skip:
            # The same shop sent this same image before: photo ids are content
            # hashes, so the proof id repeats. That earlier record is a resend's
            # original, not this proof (RAI-273 week run: skipping it checked a
            # resend as new, and once its invoice was paid it failed
            # time_after_delivery and a genuine payment was flagged as fake).
            # Rows are written when a turn completes, so none is this turn's own.
            pid = f"{pid}-earlier"
        prior.append(PriorProof(
            proof_id=pid, shop_id=r["shop_id"],
            image_sha256=str(p.get("image_sha256", "")),
            reference=str(p.get("reference") or p.get("reference_as_shown") or ""),
            session_id="", received_at=r["created_at"],
            amount_kobo=p.get("amount_kobo", p.get("amount_kobo_as_shown")),
            verdict="likely_genuine" if r["tool"] == "record_payment_proof" else "looks_fake",
            delivery_ids=tuple(p.get("delivery_ids") or ())))
    return prior, rows


def register_proof_tools(registry: ToolRegistry, ports: ProofPorts) -> None:
    dist = ports.distributor

    def look(ctx: ToolContext, shop_id: str, photo_id: str
             ) -> tuple[Assessment, str, str, datetime, list[dict[str, Any]]]:
        img = ports.images(ctx.tenant_id, shop_id, photo_id)
        if img is None:
            raise ToolError(f"No screenshot {photo_id!r} from {shop_id}.",
                            "Use the photo_id from this shop's own message.")
        data, mime, sha, received = img
        key = (ctx.tenant_id, sha)
        if key not in ports.readings:
            ports.readings[key] = ports.reader(data, mime)
            ports.reads += 1
        reading = ports.readings[key]
        pid = proof_id_for(ctx.tenant_id, shop_id, photo_id)
        prior, rows = _prior(ctx, pid)
        at = datetime.fromisoformat(received)
        a = assess(reading.reading, shop_id=shop_id, proof_id=pid, image_sha256=sha,
                   received_at=at, invoices=_invoices(ctx, shop_id), history=_Seen(prior),
                   account=_account(ctx))
        return a, pid, sha, at, rows

    def asked_before(ctx: ToolContext, shop_id: str, photo_id: str, at: datetime) -> bool:
        """An earlier screenshot from this shop in the last day that Mimi could
        not read (no proof recorded for it): that was the one ask."""
        recorded = {r["payload"].get("photo_id") for r in
                    ctx.book.intents_of(ctx.tenant_id, shop_id, PROOF_TOOL_NAMES)}
        for m in ctx.book.media_of(ctx.tenant_id, shop_id):
            if m["photo_id"] == photo_id or m["photo_id"] in recorded or m["camera"]:
                continue
            seen = datetime.fromisoformat(m["received_at"])
            if timedelta(0) <= at - seen <= ASK_MEMORY:
                return True
        return False

    def check_payment_screenshot(ctx: ToolContext, args: ScreenshotIn) -> CheckOut:
        """Read a shop's payment screenshot and check it in code: Muse Spark copies the printed fields, code checks the amount against the shop's open invoices, the recipient against the distributor's account, the time, the bank's reference format, a reference or image already used, the arithmetic and editing signs. A screenshot is only ever likely paid; the bank feed alone verifies. Returns the verdict, the failed rules and the exact shop_reply."""
        a, pid, sha, at, _ = look(ctx, args.shop_id, args.photo_id)
        amount = a.amount_kobo
        if a.duplicate_of:
            verdict: Any = "duplicate"
            reply = copy.duplicate_text()
            nxt = "Send shop_reply exactly (send_to_user, shop_whatsapp, ends_turn=true)."
        elif a.verdict == "likely_genuine":
            verdict = "likely_genuine"
            reply = copy.likely_text(a, dist)
            nxt = ("In one turn: record_payment_proof with this shop_id and photo_id, and "
                   "send_to_user shop_reply exactly (shop_whatsapp, ends_turn=true).")
        elif a.verdict == "looks_fake":
            verdict = "looks_fake"
            reply = copy.review_text(dist)
            nxt = ("Call flag_payment_proof with this shop_id and photo_id. It sends the shop "
                   "shop_reply itself: do not send it again. Never tell the shop why.")
        elif asked_before(ctx, args.shop_id, args.photo_id, at):
            verdict = "cannot_tell"
            reply = copy.unreadable_again_text(dist)
            nxt = ("Asked once already: call flag_payment_proof with this shop_id and "
                   "photo_id. It sends the shop shop_reply itself: do not send it again.")
        else:
            verdict = "cannot_tell"
            reply = copy.ask_text()
            nxt = "Send shop_reply exactly (send_to_user, shop_whatsapp, ends_turn=true)."
        if not copy.figures_ok(reply, [x for x in (amount,) if x is not None]):
            raise ToolError("The reply failed the figure check.", "Tell the owner with "
                            "send_to_user on owner_console that this screenshot needs a look.")
        return CheckOut(
            shop_id=args.shop_id, photo_id=args.photo_id, verdict=verdict,
            amount_kobo=amount, amount_naira=copy.naira(amount) if amount is not None else "",
            matched_delivery_ids=list(a.match.delivery_ids), match=a.match.kind,
            failed_rules=[c.name for c in a.failed], reasons_for_owner=list(a.reasons),
            shop_reply=reply, next_step=nxt)

    def record_payment_proof(ctx: ToolContext, args: ScreenshotIn) -> ProofOut:
        """Record a screenshot that check_payment_screenshot found likely_genuine as a proof at level likely (never verified, never money). Every field is recomputed from the image and the book here."""
        a, pid, sha, _, _ = look(ctx, args.shop_id, args.photo_id)
        if a.verdict != "likely_genuine" or a.duplicate_of or a.amount_kobo is None:
            raise ToolError("This screenshot is not likely_genuine.",
                            "Follow next_step from check_payment_screenshot instead.")
        return ProofOut(
            proof_id=pid, shop_id=args.shop_id, photo_id=args.photo_id, level="likely", amount_kobo=a.amount_kobo,
            amount_naira=copy.naira(a.amount_kobo), delivery_ids=list(a.match.delivery_ids),
            match=a.match.kind, paid_at=a.paid_at.isoformat() if a.paid_at else "",
            bank=a.bank, sender=a.sender, reference=a.reference, image_sha256=sha)

    def flag_payment_proof(ctx: ToolContext, args: ScreenshotIn) -> FlagOut:
        """Send a screenshot that looks fake (or still cannot be read after one ask) to the owner's approvals, with every failed rule named, and send the shop its neutral reply (Go sends it now). Nothing changes on the shop's account; the owner decides with the bank."""
        a, pid, sha, at, _ = look(ctx, args.shop_id, args.photo_id)
        if a.verdict == "likely_genuine" or a.duplicate_of:
            raise ToolError("This screenshot is not one to flag.",
                            "Follow next_step from check_payment_screenshot instead.")
        if a.verdict == "cannot_tell" and not asked_before(ctx, args.shop_id, args.photo_id,
                                                          at):
            raise ToolError("Ask the shop once for a clearer screenshot first.",
                            "Send the ask from check_payment_screenshot's shop_reply.")
        return FlagOut(
            proof_id=pid, shop_id=args.shop_id, photo_id=args.photo_id,
            reason="looks_fake" if a.verdict == "looks_fake" else "unreadable_again",
            failed_rules=[c.name for c in a.failed], reasons=list(a.reasons),
            amount_kobo_as_shown=a.amount_kobo, reference_as_shown=a.reference,
            image_sha256=sha,
            shop_notice=(copy.review_text(dist) if a.verdict == "looks_fake"
                         else copy.unreadable_again_text(dist)))

    def _proof_dry(ctx: ToolContext, a: ScreenshotIn, o: ProofOut) -> str:
        return f"Would record {o.amount_naira} from {o.shop_id} as likely paid (not verified)"

    def _flag_dry(ctx: ToolContext, a: ScreenshotIn, o: FlagOut) -> str:
        return (f"Would send {o.shop_id}'s screenshot to the owner ({o.reason}: "
                f"{', '.join(o.failed_rules) or 'unreadable'})")

    t = registry.tool
    t(kind="read", scope="shop:payments", tier="auto")(check_payment_screenshot)
    t(kind="write_intent", scope="shop:payments", tier="notify",
      idem=("shop_id", "photo_id"), dry_run=_proof_dry)(record_payment_proof)
    t(kind="write_intent", scope="shop:payments", tier="approve",
      idem=("shop_id", "photo_id"), dry_run=_flag_dry)(flag_payment_proof)


SCREENSHOT_TOOLS: tuple[str, ...] = (
    "check_payment_screenshot", "record_payment_proof", "flag_payment_proof")

SCREENSHOT_SKILL = """
A shop sent a payment screenshot (its message names a photo_id, ph_...): call check_payment_screenshot with the shop_id and photo_id, then do exactly its next_step. shop_reply is written by code: when next_step says to send it, send it exactly; never add a figure, a reason or an accusation. A screenshot is at most likely paid; never say received or verified.
Woken by approval_answered (the owner decided a flagged screenshot): send nothing and end the turn."""

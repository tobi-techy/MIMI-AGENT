"""Collector, the first hire: its 13 tools, its skill and its helper config.

From the Collector job spec (24 Oct 2026): money in, good shops kept, risky
shops flagged early, never by pressure the owner would not use. Read tools go
to Go's book through the consent-checked ``BookAPI``; write tools build
intents only. Every naira figure in an output is read from the book or added
up here in integer kobo; the model only chooses shops, tones and timing.

Also here: a minimal Proof helper (check_payment plus a computer-task tool
that must never be used when the bank connector is down), so the "connector
down, ask the owner, never scrape" rule is enforced and tested now.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mimi.flags import fixes_2026_10_30
from mimi.harness.builtins import digest, lagos_day, register_builtins
from mimi.harness.errors import ToolError
from mimi.harness.loop import Helper, RunBudget
from mimi.harness.policy import RiskPolicy
from mimi.harness.registry import ToolContext, ToolRegistry, build_toolset
from mimi.owed import is_overdue
from mimi.tz import lagos_day as _lagos_day

REGISTRY_VERSION = "harness-2026-10-27.1"

Tone = Literal["heads_up", "soft", "firm"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


def naira(kobo: int) -> str:
    return f"N{kobo / 100:,.0f}" if kobo % 100 == 0 else f"N{kobo / 100:,.2f}"


def ladder(days_overdue: int) -> tuple[int, Tone]:
    """The default ladder (Collector spec): which step, and its tone."""
    if days_overdue <= 0:
        return 0, "heads_up"
    if days_overdue < 3:
        return 1, "soft"
    if days_overdue < 7:
        return 3, "soft"
    if days_overdue < 14:
        return 7, "firm"
    return 14, "firm"


# --------------------------------------------------------------------- reads


class ShopIn(_In):
    shop_id: str = Field(description="The shop's id, e.g. shop-001.")


class BalanceOut(_Out):
    shop_id: str
    shop_name: str
    outstanding_kobo: int
    overdue_kobo: int
    open_invoices: int
    oldest_days_overdue: int
    credit_limit_kobo: int

    def short(self) -> str:
        return (f"{self.shop_name} owes {naira(self.outstanding_kobo)}, "
                f"{naira(self.overdue_kobo)} overdue")


def get_balance(ctx: ToolContext, args: ShopIn) -> BalanceOut:
    """One shop's balance from the book: outstanding and overdue in kobo, open invoices, oldest days overdue, credit limit."""
    shop = ctx.book.shop(ctx.tenant_id, args.shop_id)
    rows = ctx.book.receivables(ctx.tenant_id, args.shop_id)
    overdue = [r for r in rows if is_overdue(r)]
    return BalanceOut(
        shop_id=args.shop_id, shop_name=shop["name"],
        outstanding_kobo=sum(r["outstanding_kobo"] for r in rows),
        overdue_kobo=sum(r["outstanding_kobo"] for r in overdue),
        open_invoices=len(rows),
        oldest_days_overdue=max((r["days_overdue"] for r in overdue), default=0),
        credit_limit_kobo=shop["credit_limit_kobo"],
    )


class InvoicesIn(_In):
    shop_id: str = Field("", description="One shop, or empty for every consenting shop.")
    overdue_only: bool = True
    min_overdue_naira: int = Field(
        0, ge=0, description="Only shops whose overdue total is above this many naira "
        "(the owner's own figure, e.g. 50000 for 'over 50k').")
    limit: int = Field(20, ge=1, le=50)


class InvoiceRow(_Out):
    invoice_id: str
    shop_id: str
    shop_name: str
    due_on: str
    days_overdue: int
    outstanding_kobo: int
    status: str


class ShopTotal(_Out):
    shop_id: str
    shop_name: str
    overdue_kobo: int
    oldest_days_overdue: int
    invoices: int


class InvoicesOut(_Out):
    invoices: list[InvoiceRow]
    shops: list[ShopTotal]
    total_overdue_kobo: int
    shops_not_sharing: int

    def short(self) -> str:
        return (f"{len(self.shops)} shops, {naira(self.total_overdue_kobo)} overdue"
                + (f", {self.shops_not_sharing} not sharing" if self.shops_not_sharing else ""))


def get_invoices(ctx: ToolContext, args: InvoicesIn) -> InvoicesOut:
    """Open invoices from the book, for one shop or every consenting shop, with per-shop overdue totals in kobo (largest first). Use min_overdue_naira for 'everyone over 50k'."""
    rows = ctx.book.receivables(ctx.tenant_id, args.shop_id or None)
    hidden: set[str] = set()
    kept = []
    for r in rows:
        if not ctx.grants.allows(r["shop_id"], "shop:invoices"):
            hidden.add(r["shop_id"])
            continue
        if args.overdue_only and not is_overdue(r):
            continue
        kept.append(r)
    totals: dict[str, ShopTotal] = {}
    for r in kept:
        t = totals.get(r["shop_id"]) or ShopTotal(shop_id=r["shop_id"], shop_name=r["shop_name"],
                                                  overdue_kobo=0, oldest_days_overdue=0,
                                                  invoices=0)
        totals[r["shop_id"]] = t.model_copy(update={
            "overdue_kobo": t.overdue_kobo + r["outstanding_kobo"],
            "oldest_days_overdue": max(t.oldest_days_overdue, r["days_overdue"]),
            "invoices": t.invoices + 1,
        })
    floor = args.min_overdue_naira * 100
    above = sorted((t for t in totals.values() if t.overdue_kobo > floor),
                   key=lambda t: (-t.overdue_kobo, t.shop_id))
    shops = above[: args.limit]
    keep = {s.shop_id for s in shops}
    invoices = [InvoiceRow(invoice_id=r["delivery_id"], shop_id=r["shop_id"],
                           shop_name=r["shop_name"], due_on=r["due_on"],
                           days_overdue=r["days_overdue"],
                           outstanding_kobo=r["outstanding_kobo"], status=r["status"])
                for r in kept if r["shop_id"] in keep]
    invoices.sort(key=lambda i: (-i.days_overdue, i.invoice_id))
    # Across shops, totals are enough (and keep the context small); invoice
    # rows come back for one shop.
    return InvoicesOut(invoices=invoices[:20] if args.shop_id else [], shops=shops,
                       # Every shop above the floor, not only the ``limit`` listed:
                       # the total is the book's (mimi.owed), never a page of it.
                       # Behind the switch until the recorded runs are redone.
                       total_overdue_kobo=sum(s.overdue_kobo for s in (
                           above if fixes_2026_10_30() else shops)),
                       shops_not_sharing=len(hidden))


def _invoices_scope(args: InvoicesIn) -> str:
    return "shop:invoices" if args.shop_id else "org:receivables"


class HistoryIn(_In):
    shop_id: str
    limit: int = Field(8, ge=1, le=30)


class PaymentRow(_Out):
    payment_id: str
    paid_on: str
    amount_kobo: int
    method: str
    bank_confirmed: bool


class PromiseRow(_Out):
    made_on: str
    promised_on: str
    kept: bool


class HistoryOut(_Out):
    shop_id: str
    payments: list[PaymentRow]
    promises: list[PromiseRow]
    late_promises: int

    def short(self) -> str:
        return (f"{len(self.payments)} payments, {self.late_promises} broken promises")


def get_payment_history(ctx: ToolContext, args: HistoryIn) -> HistoryOut:
    """A shop's recent payments (bank-confirmed or not) and its payment promises, newest first."""
    pays = ctx.book.payments(ctx.tenant_id, args.shop_id, args.limit)
    proms = ctx.book.promises(ctx.tenant_id, args.shop_id)
    return HistoryOut(
        shop_id=args.shop_id,
        payments=[PaymentRow(payment_id=p["payment_id"], paid_on=p["paid_on"],
                             amount_kobo=p["amount_kobo"], method=p["method"],
                             bank_confirmed=p["bank_confirmed"]) for p in pays],
        promises=[PromiseRow(**{k: p[k] for k in ("made_on", "promised_on", "kept")})
                  for p in proms[-6:]],
        late_promises=sum(1 for p in proms if not p["kept"]),
    )


class NoArgs(_In):
    pass


class AccountOut(_Out):
    account_name: str
    bank: str
    account_number: str

    def short(self) -> str:
        return f"{self.bank} account"


def get_account_number(ctx: ToolContext, args: NoArgs) -> AccountOut:
    """The distributor's collection account (name, bank, number) to give a shop. Read only; Mimi never changes it."""
    return AccountOut(**ctx.book.collection_account(ctx.tenant_id))


class CheckPaymentIn(_In):
    shop_id: str
    amount_kobo: int = Field(gt=0, description="The amount the shop says it paid, in kobo.")
    reference: str = Field("", max_length=64)


class CheckPaymentOut(_Out):
    shop_id: str
    matched: bool
    credit_id: str = ""
    amount_kobo: int = 0
    received_at: str = ""
    label: Literal["verified", "pending"]

    def short(self) -> str:
        return "bank credit found" if self.matched else "no bank credit yet"


def check_payment(ctx: ToolContext, args: CheckPaymentIn) -> CheckPaymentOut:
    """Look for the shop's payment in the distributor's own bank feed. Only a bank credit makes a payment verified; a shop's word or screenshot never does."""
    hit = ctx.book.bank_feed_match(ctx.tenant_id, args.shop_id, args.amount_kobo, args.reference)
    if not hit:
        return CheckPaymentOut(shop_id=args.shop_id, matched=False, label="pending")
    return CheckPaymentOut(shop_id=args.shop_id, matched=True, credit_id=hit["credit_id"],
                           amount_kobo=hit["amount_kobo"], received_at=hit["received_at"],
                           label="verified")


class PortalIn(_In):
    task: str = Field(max_length=200)


class PortalOut(_Out):
    done: bool


def open_bank_portal(ctx: ToolContext, args: PortalIn) -> PortalOut:
    """A computer task in the bank's web portal. The most expensive path: use only when no connector exists, never as a fallback for a connector that is down."""
    raise ToolError("Computer tasks are not set up for this distributor yet.",
                    "Ask the owner with ask_question.")


# -------------------------------------------------------------------- writes


def _invoice(ctx: ToolContext, shop_id: str, invoice_id: str) -> dict[str, object]:
    for r in ctx.book.receivables(ctx.tenant_id, shop_id):
        if r["delivery_id"] == invoice_id:
            return r
    raise ToolError(f"Invoice {invoice_id} is not open for {shop_id}.",
                    "Call get_invoices for this shop and use one of its invoice_id values.")


class ExactAccountIn(_In):
    shop_id: str
    invoice_id: str


class ExactAccountOut(_Out):
    shop_id: str
    invoice_id: str
    amount_kobo: int
    date: str


def create_exact_amount_account(ctx: ToolContext, args: ExactAccountIn) -> ExactAccountOut:
    """Propose a one-off account for exactly the invoice's outstanding amount, so the shop's transfer matches itself. Go creates it with the bank partner; Mimi never touches the money."""
    inv = _invoice(ctx, args.shop_id, args.invoice_id)
    return ExactAccountOut(shop_id=args.shop_id, invoice_id=args.invoice_id,
                           amount_kobo=int(str(inv["outstanding_kobo"])), date=lagos_day(ctx))


def _exact_dry(ctx: ToolContext, a: ExactAccountIn, o: ExactAccountOut) -> str:
    return f"Would ask Go for an exact-amount account: {o.shop_id} {o.invoice_id} {naira(o.amount_kobo)}"


class ReceiptIn(_In):
    shop_id: str
    amount_kobo: int = Field(gt=0)
    reference: str = ""


class ReceiptOut(_Out):
    shop_id: str
    credit_id: str
    amount_kobo: int
    label: Literal["verified"]


def send_receipt(ctx: ToolContext, args: ReceiptIn) -> ReceiptOut:
    """Propose a thank-you receipt for a payment the bank feed confirms. Refused unless the bank credit exists."""
    hit = ctx.book.bank_feed_match(ctx.tenant_id, args.shop_id, args.amount_kobo, args.reference)
    if not hit:
        raise ToolError("No bank credit matches this payment, so no receipt can go out.",
                        "Tell the shop the payment is pending, or ask Proof with ask_helper.")
    return ReceiptOut(shop_id=args.shop_id, credit_id=hit["credit_id"],
                      amount_kobo=hit["amount_kobo"], label="verified")


def _receipt_dry(ctx: ToolContext, a: ReceiptIn, o: ReceiptOut) -> str:
    return f"Would receipt {naira(o.amount_kobo)} to {o.shop_id} (bank credit {o.credit_id})"


class PlanItem(_In):
    shop_id: str
    tone: Tone | None = Field(None, description="Leave empty to use the ladder's tone.")


class PlanIn(_In):
    items: list[PlanItem] = Field(min_length=1, max_length=40)
    note: str = Field("", max_length=300)


class PlanLine(_Out):
    shop_id: str
    shop_name: str
    overdue_kobo: int
    days_overdue: int
    ladder_step: int
    tone: Tone


class PlanOut(_Out):
    lines: list[PlanLine]
    total_kobo: int
    shops: int
    plan_date: str
    shops_key: str
    note: str


def propose_reminder_plan(ctx: ToolContext, args: PlanIn) -> PlanOut:
    """Propose today's reminder plan for several shops. Figures and ladder steps come from the book; you choose shops and, if needed, a tone. Plans above the owner's limit wait for approval."""
    lines: list[PlanLine] = []
    seen: set[str] = set()
    for item in args.items:
        if item.shop_id in seen:
            continue
        seen.add(item.shop_id)
        if not ctx.grants.allows(item.shop_id, "shop:invoices"):
            raise ToolError(f"{item.shop_id} has not shared its invoices.",
                            "Leave it out of the plan and tell the owner why.")
        shop = ctx.book.shop(ctx.tenant_id, item.shop_id)
        rows = [r for r in ctx.book.receivables(ctx.tenant_id, item.shop_id)
                if is_overdue(r)]
        if not rows:
            raise ToolError(f"{item.shop_id} has nothing overdue.",
                            "Remove it from the plan and call propose_reminder_plan again.")
        days = max(r["days_overdue"] for r in rows)
        step, tone = ladder(days)
        lines.append(PlanLine(shop_id=item.shop_id, shop_name=shop["name"],
                              overdue_kobo=sum(r["outstanding_kobo"] for r in rows),
                              days_overdue=days, ladder_step=step, tone=item.tone or tone))
    ids = sorted(seen)
    return PlanOut(lines=lines, total_kobo=sum(line.overdue_kobo for line in lines),
                   shops=len(lines), plan_date=lagos_day(ctx),
                   shops_key=digest(",".join(ids)), note=args.note)


def plan_card(o: PlanOut) -> str:
    """The approval card (Collector spec): '5 shops, N385,900. Chuks firm, Joy soft, +3.'"""
    named = [f"{line.shop_name} {line.tone.replace('_', ' ')}" for line in o.lines[:2]]
    more = f", +{o.shops - 2}" if o.shops > 2 else ""
    return f"{o.shops} shops, {naira(o.total_kobo)}. {', '.join(named)}{more}. Approve and send?"


def _plan_dry(ctx: ToolContext, a: PlanIn, o: PlanOut) -> str:
    return plan_card(o)


class ReminderIn(_In):
    shop_id: str
    invoice_id: str
    tone: Tone


class ReminderOut(_Out):
    shop_id: str
    invoice_id: str
    date: str
    tone: Tone
    amount_kobo: int
    text: str


def say_day(iso_day: str) -> str:
    """'2026-10-30' as 'Friday 30 October', the way an account officer writes it."""
    from datetime import date

    try:
        d = date.fromisoformat(iso_day[:10])
    except ValueError:
        return iso_day
    return f"{d:%A} {d.day} {d:%B}"


def reminder_text(tone: str, shop_name: str, invoice_id: str, amount_naira: str,
                  due_on: str) -> str:
    """The reminder a shop reads (docs/MIMI-VOICE.md, Collector). Code-written; the
    figure, the invoice and the day come from the book."""
    day = say_day(due_on)
    if tone == "heads_up":
        return (f"Hello {shop_name}, this is Coral Lane Trading. Invoice {invoice_id} for "
                f"{amount_naira} is due on {day}. If you have already paid, just reply here.")
    if tone == "soft":
        return (f"Hello {shop_name}, a gentle reminder from Coral Lane Trading: {amount_naira} "
                f"on invoice {invoice_id} was due on {day}. Please let me know when you will "
                "pay, or reply here if you have already paid.")
    return (f"Hello {shop_name}, this is Coral Lane Trading. {amount_naira} on invoice "
            f"{invoice_id} is still unpaid; it was due on {day}. Please pay today, or reply "
            "here if there is a problem so we can sort it out.")


def send_reminder(ctx: ToolContext, args: ReminderIn) -> ReminderOut:
    """Propose one payment reminder to a shop for one invoice. The text and amount are written by code from the book. Firm reminders, or ones above the owner's limit, wait for approval. One per shop, invoice and day."""
    inv = _invoice(ctx, args.shop_id, args.invoice_id)
    shop = ctx.book.shop(ctx.tenant_id, args.shop_id)
    amount = int(str(inv["outstanding_kobo"]))
    text = reminder_text(args.tone, str(shop["name"]), args.invoice_id, naira(amount),
                         str(inv["due_on"]))
    return ReminderOut(shop_id=args.shop_id, invoice_id=args.invoice_id, date=lagos_day(ctx),
                       tone=args.tone, amount_kobo=amount, text=text)


def _reminder_dry(ctx: ToolContext, a: ReminderIn, o: ReminderOut) -> str:
    return f"Would send a {o.tone} reminder to {o.shop_id} for {naira(o.amount_kobo)}"


class FollowupIn(_In):
    shop_id: str = ""
    in_hours: int = Field(ge=1, le=336)
    reason: str = Field(max_length=200)


class FollowupOut(_Out):
    shop_id: str
    at: str
    due_on: str
    reason: str


def set_followup(ctx: ToolContext, args: FollowupIn) -> FollowupOut:
    """Set a timer to look at this again (a promised payment date, a recheck). The job sleeps until then."""
    at = ctx.now + timedelta(hours=args.in_hours)

    return FollowupOut(shop_id=args.shop_id, at=at.isoformat(),
                       due_on=_lagos_day(at).isoformat(),
                       reason=args.reason)


def _followup_scope(args: FollowupIn) -> str:
    return "shop:messages" if args.shop_id else "org:plan"


def _followup_dry(ctx: ToolContext, a: FollowupIn, o: FollowupOut) -> str:
    return f"Would wake this job at {o.at} ({o.reason[:60]})"


class AskHelperIn(_In):
    helper: Literal["proof", "credit", "order"]
    task: str = Field(min_length=5, max_length=600)
    shop_id: str = ""


class AskHelperOut(_Out):
    helper: str
    task: str
    shop_id: str
    task_digest: str


def ask_helper(ctx: ToolContext, args: AskHelperIn) -> AskHelperOut:
    """Hand a task to another helper: Proof checks a screenshot or POS claim, Credit looks at limits and risk, Order checks for a coming delivery. Give a complete task."""
    return AskHelperOut(helper=args.helper, task=args.task, shop_id=args.shop_id,
                        task_digest=digest(args.task))


def _ask_helper_dry(ctx: ToolContext, a: AskHelperIn, o: AskHelperOut) -> str:
    return f"Would hand to {o.helper}: {o.task[:100]}"


class NotifyRepIn(_In):
    shop_id: str
    message: str = Field(min_length=3, max_length=600)


class NotifyRepOut(_Out):
    shop_id: str
    rep_id: str
    message: str
    date: str
    message_digest: str


def notify_rep(ctx: ToolContext, args: NotifyRepIn) -> NotifyRepOut:
    """Tell the shop's rep something they need for a visit (history, a dispute, a promise)."""
    shop = ctx.book.shop(ctx.tenant_id, args.shop_id)
    return NotifyRepOut(shop_id=args.shop_id, rep_id=shop["rep_id"], message=args.message,
                        date=lagos_day(ctx), message_digest=digest(args.message))


def _rep_dry(ctx: ToolContext, a: NotifyRepIn, o: NotifyRepOut) -> str:
    return f"Would tell {o.rep_id} about {o.shop_id}: {o.message[:80]}"


class NotifyOwnerIn(_In):
    message: str = Field(min_length=3, max_length=800)
    kind: Literal["fyi", "risk", "day_end"] = "fyi"


class NotifyOwnerOut(_Out):
    message: str
    kind: str
    date: str
    message_digest: str


def notify_owner(ctx: ToolContext, args: NotifyOwnerIn) -> NotifyOwnerOut:
    """Put a note in the owner's daily note or console (risk signals, day-end summary). Not a question: use ask_question for that."""
    return NotifyOwnerOut(message=args.message, kind=args.kind, date=lagos_day(ctx),
                          message_digest=digest(args.message))


def _owner_dry(ctx: ToolContext, a: NotifyOwnerIn, o: NotifyOwnerOut) -> str:
    return f"Would note for the owner ({o.kind}): {o.message[:80]}"


# ------------------------------------------------------------------ helpers

COLLECTOR_TOOLS: tuple[str, ...] = (
    "get_balance", "get_invoices", "get_payment_history", "get_account_number",
    "create_exact_amount_account", "check_payment", "send_receipt", "propose_reminder_plan",
    "send_reminder", "set_followup", "ask_helper", "notify_rep", "notify_owner",
)
BUILTIN_TOOLS: tuple[str, ...] = ("send_to_user", "ask_question", "update_plan")
PROOF_TOOLS: tuple[str, ...] = ("check_payment", "get_payment_history", "open_bank_portal",
                                "notify_owner")

COLLECTOR_SKILL = """You are Collector: money in faster, good shops kept, risky shops flagged early, never by pressure the owner would not use.
Ladder (days overdue -> step, tone): due date heads-up; 1 soft; 3 second soft asking for a date; 7 firm and tell the rep; 14 the owner decides. The tools compute days and amounts; you choose who and how.
Owner asks like "chase everyone over 50k": call get_invoices with no shop_id and min_overdue_naira set to the owner's figure (50000), then propose_reminder_plan with those shops. The plan goes to the owner's approvals; do not send reminders yourself in the same turn.
Owner asks "what does X owe": get_balance, then send_to_user to the owner (channel owner_console) with the figures from the result, ends_turn=true. Answering a person always means calling send_to_user; plain text is never delivered.
A shop says it paid: check_payment. Matched: send_receipt and thank them. Not matched: tell the shop it is pending, never "received". A screenshot goes to Proof with ask_helper.
A promise to pay: set_followup for the day after the promised day.
Never: move money, change amounts, write off, discount, extend due dates, threaten, shame, tell anyone else about a debt, ask for a PIN or OTP.
If a payment matches two shops or invoices, the shop disputes the amount, or the shop says it cannot pay: ask_question to the owner, do not guess or negotiate.
A routine wake-up with nothing new: end the turn without sending anything."""

PROOF_SKILL = """You are Proof: every payment proven or flagged. Only the distributor's bank feed makes a payment verified.
Check the bank feed with check_payment. If the bank connector is down, ask the owner with ask_question; never try the bank portal or the web instead.
Never call a payment received from a screenshot alone; say likely, and only after the code checks."""


def register_collector(registry: ToolRegistry) -> None:
    t = registry.tool
    t(kind="read", scope="shop:balance", tier="auto")(get_balance)
    t(kind="read", scope="org:receivables", tier="auto", scope_fn=_invoices_scope)(get_invoices)
    t(kind="read", scope="shop:payments", tier="auto")(get_payment_history)
    t(kind="read", scope="org:account", tier="auto", cost_tier="knowledge")(get_account_number)
    t(kind="read", scope="shop:payments", tier="auto", connector="bank_feed")(check_payment)
    t(kind="write_intent", scope="shop:invoices", tier="notify", idem=("shop_id", "invoice_id"),
      dry_run=_exact_dry, connector="bank_feed")(create_exact_amount_account)
    t(kind="write_intent", scope="shop:messages", tier="auto", idem=("shop_id", "credit_id"),
      dry_run=_receipt_dry, connector="bank_feed")(send_receipt)
    t(kind="write_intent", scope="org:receivables", tier="approve",
      idem=("plan_date", "shops_key"), dry_run=_plan_dry)(propose_reminder_plan)
    t(kind="write_intent", scope="shop:messages", tier="notify",
      idem=("shop_id", "invoice_id", "date"), dry_run=_reminder_dry)(send_reminder)
    t(kind="write_intent", scope="org:plan", tier="auto", idem=("shop_id", "due_on", "reason"),
      dry_run=_followup_dry, scope_fn=_followup_scope, cost_tier="knowledge")(set_followup)
    t(kind="write_intent", scope="org:helpers", tier="auto",
      idem=("helper", "shop_id", "task_digest"), dry_run=_ask_helper_dry)(ask_helper)
    t(kind="write_intent", scope="shop:messages", tier="auto",
      idem=("shop_id", "date", "message_digest"), dry_run=_rep_dry)(notify_rep)
    t(kind="write_intent", scope="org:owner", tier="auto", idem=("date", "message_digest"),
      dry_run=_owner_dry, cost_tier="knowledge")(notify_owner)
    t(kind="read", scope="org:account", tier="auto", cost_tier="computer")(open_bank_portal)


def default_registry() -> ToolRegistry:
    reg = ToolRegistry(REGISTRY_VERSION)
    register_builtins(reg)
    register_collector(reg)
    return reg


def collector_helper(registry: ToolRegistry, policy: RiskPolicy, *,
                     budget: RunBudget | None = None, shadow: bool = False,
                     enabled: bool = True) -> Helper:
    ts = build_toolset(registry, "collector", COLLECTOR_TOOLS + BUILTIN_TOOLS, policy)
    from mimi.harness.versions import register

    return register(Helper("collector", COLLECTOR_SKILL, ts, budget or RunBudget(),
                           enabled=enabled, shadow=shadow, skill_version="collector-v1"))


def proof_helper(registry: ToolRegistry, policy: RiskPolicy) -> Helper:
    ts = build_toolset(registry, "proof", PROOF_TOOLS + BUILTIN_TOOLS, policy)
    from mimi.harness.versions import register

    return register(Helper("proof", PROOF_SKILL, ts, skill_version="proof-v0"))

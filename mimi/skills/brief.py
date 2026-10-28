"""Shorter tool descriptions for skills mode (RAI-317).

With ``MIMI_SKILLS=1`` the routed skill's body already says when and why to
use each of its tools, so the tool's own description only has to say what it
does and the rule that keeps it safe. Flag off nothing here is used: every
helper keeps the full registry text, and so do its recorded answers.

Each line keeps the safety clause of the full description (never moves money,
the owner decides, a figure comes from a tool, a screenshot is never proof).
``test_skills_brief`` checks every short text keeps the words that matter.

Skills in ``FROZEN`` keep the full text: their skills-mode answers are recorded
(Order held-out, ``eval/order/heldout.harness.skills.cache.json``, which
also records the balance part of a multi-part order) and a new
prompt would need a paid re-record. Take a skill out of ``FROZEN`` in the PR
that re-records it.
"""

from __future__ import annotations

FROZEN: frozenset[str] = frozenset({
    "order/order-intake", "order/drop-confirmation", "order/delivery-status",
    # Order held-out h46 ("Send 3 carton tin milk. How much do I owe now?") hands
    # its balance part to Collector, so that run is recorded too.
    "collector/collections",
})

SHORT: dict[str, str] = {
    "ask_question": "Ask the owner or shop one clear question instead of guessing. Ends the turn.",
    "ask_helper": "Hand a complete task to another helper: Proof checks a screenshot or "
                  "claim, Credit limits and risk, Order a coming delivery.",
    "check_payment": "Look for the payment in the distributor's bank feed. Only a bank credit "
                     "verifies a payment; a shop's word or screenshot never does.",
    "create_exact_amount_account": "Propose a one-off account for exactly the outstanding "
                                   "amount. Go creates it; Mimi never touches money.",
    "draft_brand_email": "Draft an owner email to a brand. Waits in approvals; Mimi never "
                         "sends it.",
    "draft_restock": "Put a restock draft (quantity and amount by code) in the owner's "
                     "approvals and send the shop its thanks. Never places an order.",
    "find_credit_candidates": "Shortlist shops whose bank-verified history supports more "
                              "credit. Each goes to the owner as a draft.",
    "find_shop": "Find a shop id from the name the owner wrote. Ask if several fit.",
    "flag_payment_proof": "Send a likely-fake screenshot to the owner's approvals with the "
                          "failed rules, and the shop its neutral reply. Nothing changes on "
                          "the account; the owner decides.",
    "followup_hours": "Turn the shop's words for a payment day into in_hours for set_followup "
                      "(Africa/Lagos, 09:00 WAT the day after), said_as, and unclear. Never "
                      "count days yourself.",
    "get_account_number": "The distributor's collection account to give a shop. Read only; Mimi "
                          "never changes it.",
    "get_balance": "One shop's balance from the book, in kobo: outstanding, overdue, open "
                   "invoices, credit limit.",
    "get_brand_promos": "Today's manufacturer promos, with shop_reply written by code. Read only.",
    "get_credit_file": "A shop's credit file: limit, debt, bank-verified payments (only these "
                       "count), deliveries, broken promises, requests waiting.",
    "get_invoices": "Open invoices with per-shop overdue totals in kobo. min_overdue_naira for "
                    "'over 50k'.",
    "get_payment_history": "A shop's recent payments and promises, newest first.",
    "get_stock_estimates": "Stock estimates by code (never shown to the distributor), lowest "
                           "first. ask_sku is the one item to ask now, or empty.",
    "notify_owner": "Add a note to the owner's daily note or console. Not a question.",
    "notify_rep": "Tell the shop's rep what they need for a visit.",
    "parse_bank_alert": "Read a pasted bank alert. Returns the shop's claim, never proof: "
                        "check_payment with claimed_amount_kobo_unverified before anyone "
                        "counts it.",
    "prepare_credit_review": "Recommendation by code from verified history and the shelf count "
                             "(support, hold, not now) with reasons and "
                             "the shop's reply. Mimi never sets the limit; the owner decides.",
    "propose_reminder_plan": "Propose today's reminder plan. Figures from the book; you pick "
                             "shops and tone. Over the owner's limit waits for approval.",
    "read_skill": "Read a skill's instructions (file empty) or a references/ or assets/ file "
                  "it names. Text only.",
    "read_stock_answer": "Read the shop's answer to the open stock question in code, "
                         "re-estimate, and say if a restock draft is needed; never a guess. "
                         "Returns shop_reply.",
    "record_payment_proof": "Record a likely_genuine screenshot as proof at level likely "
                            "(never verified, never money).",
    "record_stock_count": "Record the shop's count for the open question. The distributor "
                          "never sees counts.",
    "revise_restock": "New version of the restock draft when the shop corrects it. Quantity "
                      "must be the shop's own number this turn. Goes to the owner's "
                      "approvals; never places an order.",
    "say_date": "Write an ISO date from a tool result in the shop's language. Copy it; never "
                "write dates yourself.",
    "send_receipt": "Propose a thank-you receipt. Refused unless the bank credit exists.",
    "send_reminder": "Propose one reminder for one invoice; text and amount by code. Firm or "
                     "over-limit ones wait for approval.",
    "send_to_user": "Send a message to the owner, a shop or a rep; your plain text is "
                    "private. Every naira figure must come from a tool result. ends_turn=true "
                    "on the last message.",
    "set_followup": "Set a timer to look at this again. The job sleeps until then.",
    "submit_credit_review": "Put the credit request in the owner's approvals as a draft. "
                            "Figures recomputed from the book; never changes a limit.",
    "update_plan": "Save this job's plan, to-do list and a short note. The only tool that "
                   "saves state.",
    "ask_for_shelf_photo": "Ask for one shelf photo (text by code, sent by Go). Once per "
                           "request; again=true only if the photo was unusable.",
    "count_shelf_photo": "Count the shelf photo (SAM 3.1 and code): items, value at the "
                         "distributor's prices, confidence. Items never delivered are not "
                         "counted.",
    "check_payment_screenshot": "Check a payment screenshot in code against invoices, the "
                                "distributor's account, time, reference and edit signs. Only "
                                "ever likely paid; the bank feed alone verifies. Returns "
                                "shop_reply.",
    "ask_stock_count": "Ask the one-tap count (0-2 | 3-5 | 6+, by code, sent by Go) for "
                       "ask_sku, or again=true once after an unclear answer.",
}

# Words a short text must keep when its full text has them (the safety clause).
KEEP: tuple[str, ...] = ("never", "approval", "verif", "proof", "claim", "refused")


def short(name: str, full: str) -> str:
    """The skills-mode description of one tool (the full text when none is set)."""
    return SHORT.get(name, full)


__all__ = ["FROZEN", "KEEP", "SHORT", "short"]

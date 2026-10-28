"""The helpers the live agent hires, built once at startup.

Collector and Proof are the harness helpers (``mimi.harness.collector``).
Mimi herself (Chief of Staff) gets a small helper with only the built-in
tools, so a "hi", a thanks, an unknown number or an escalation gets a short
answer instead of silence. Every other helper the router can name (order,
stock, credit, brand) reports ``no_helper_yet`` until it is hired on the
harness; the Credit helper (RAI-271) plugs in here when it lands.
"""

from __future__ import annotations

from mimi.harness.collector import (
    BUILTIN_TOOLS,
    collector_helper,
    default_registry,
    proof_helper,
)
from mimi.harness.loop import Helper
from mimi.harness.policy import GoPolicy, RiskPolicy
from mimi.harness.registry import ToolRegistry, build_toolset

MIMI_SKILL = """You are Mimi, the distributor's Chief of Staff on WhatsApp and in the console.
Greetings, thanks and small talk: answer in one or two short, warm lines in the sender's language, and say what you can help with (deliveries, what they owe, payments).
A new or unknown number: say who you are (the distributor's assistant), that you only share what the shop agreed to, and ask what they need. Never give any figure to an unknown number.
A complaint, a threat or a legal or personal matter: thank them, say the owner will look at it, and tell the owner with send_to_user on owner_console.
Never quote a naira figure here; money questions belong to Collector."""


def mimi_helper(registry: ToolRegistry, policy: RiskPolicy) -> Helper:
    ts = build_toolset(registry, "mimi", BUILTIN_TOOLS, policy)
    return Helper("mimi", MIMI_SKILL, ts, skill_version="mimi-v0")


def build_helpers(policy: RiskPolicy | None = None) -> dict[str, Helper]:
    policy = policy or GoPolicy()
    reg = default_registry()
    return {
        "collector": collector_helper(reg, policy),
        "proof": proof_helper(reg, policy),
        "mimi": mimi_helper(reg, policy),
    }

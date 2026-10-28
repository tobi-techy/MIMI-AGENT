"""The helpers the live agent hires, built once at startup.

Every helper the router can name for the four e2e scenes runs on the one
harness loop (RAI-272):

* Collector and the built-in Mimi chat helper (``mimi.harness.collector``);
* Proof: the bank-feed check plus the screenshot tools (``mimi.proof.tools``:
  Muse Spark reads the screenshot, code checks it, a likely proof or an
  approvals item);
* Order: the delivery receipt and the shop's YES (``mimi.receipts.tools``);
* Stock: the estimate, the one-tap count and the restock draft
  (``mimi.stock.tools``);
* Credit: the shelf photo, the SAM 3.1 count and the credit request to
  approvals (``mimi.credit.tools``).

* Brand: the Promo Check (``mimi.brand.tools``): promos read from a fixed
  synthetic list, a shop reply written by code, brand emails kept as drafts
  in the owner's approvals. It reads and drafts; it never sends.

Order only
does delivery receipts; order intake and delivery status get a one-line
answer pointing to the rep.

``HelperDeps`` carries what the tools reach outside the book: the models
(Muse Spark for screenshots, receipt replies and worded stock answers, SAM
3.1 for shelves) and where media bytes live. ``deps_from_env`` builds the
live ones; tests and the e2e eval pass recorded ones.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mimi.api.media import LocalMedia, MediaSource, NoMedia, StorePhotos, StoreScreens
from mimi.brand.tools import BRAND_SKILL, BRAND_TOOLS, BrandPorts, register_brand_tools
from mimi.credit.sam import SamError, Segmentation, Segmenter
from mimi.credit.tools import CreditPorts, credit_helper, register_credit
from mimi.harness.collector import (
    BUILTIN_TOOLS,
    PROOF_SKILL,
    PROOF_TOOLS,
    collector_helper,
    default_registry,
)
from mimi.harness.loop import Helper
from mimi.harness.policy import GoPolicy, RiskPolicy
from mimi.harness.registry import ToolRegistry, build_toolset
from mimi.models.base import ChatMessage, ChatModel, ChatResult, ModelError
from mimi.proof.reading import ScreenshotReader
from mimi.proof.tools import SCREENSHOT_SKILL, SCREENSHOT_TOOLS, ProofPorts, register_proof_tools
from mimi.receipts.tools import ORDER_SKILL, RECEIPT_TOOLS, ReceiptPorts, register_receipt_tools
from mimi.receipts.verdict import ReceiptJudge
from mimi.stock.answers import StockAnswerReader
from mimi.stock.tools import STOCK_SKILL, STOCK_TOOLS, StockPorts, register_stock_tools
from mimi.store.book import SqlGo

MIMI_SKILL = """You are Mimi, {distributor}'s Chief of Staff on WhatsApp and in the console.
Greetings, thanks and small talk: answer in one or two short, warm lines in plain English, the way a person would ("Good morning, Sade Stores. How can I help today?"); mention what you can help with (deliveries, what they owe, payments) only when they ask what you do.
Asked whether you are a person or a bot: say plainly and warmly that you are Mimi, {distributor}'s AI assistant, and that someone from the team can step in whenever they want.
A new or unknown number: say who you are (Mimi, {distributor}'s assistant), that you only share a shop's account with that shop, and ask what they need. Never give any figure to an unknown number.
A complaint, a threat or a legal or personal matter: thank them calmly, say the owner will look at it, and tell the owner once, with send_to_user on owner_console, in one or two sentences: the shop's name and what they said.
The owner asks how the business is doing (the morning brief): you have no figures or tools for it in this build, so never say how business is going. Say plainly that the daily brief is not ready yet, and offer what you can do: deliveries, what shops owe, payments and the approvals waiting.
Never quote a naira figure here; money questions belong to Collector."""

APPROVAL_LINE = ("\nWoken by approval_answered (the owner decided an approvals item): the "
                 "owner's decision is already applied; send nothing and end the turn.")


class NoModel:
    """A model port with no key behind it: every call fails safe (ModelError)."""

    name = "none"

    def complete(self, messages: Any, *, max_tokens: int = 400,
                 meta: Mapping[str, str] | None = None,
                 response_format: Mapping[str, object] | None = None) -> ChatResult:
        raise ModelError("no model configured")


class NoSegmenter:
    name = "none"

    def segment(self, image: bytes, mime: str, prompt: str) -> Segmentation:
        raise SamError("no SAM 3.1 key configured")


@dataclass
class HelperDeps:
    vision: ChatModel = field(default_factory=NoModel)  # screenshots
    judge: ChatModel = field(default_factory=NoModel)  # receipt replies
    stock: ChatModel = field(default_factory=NoModel)  # worded stock answers
    segmenter: Segmenter = field(default_factory=NoSegmenter)
    media: MediaSource = field(default_factory=NoMedia)
    distributor: str = "Coral Lane Trading"


def deps_from_env(env: Mapping[str, str] | None = None) -> HelperDeps:
    from mimi.credit.sam import sam_from_env
    from mimi.models.env import from_env

    e = env if env is not None else os.environ
    media_dir = e.get("MIMI_MEDIA_DIR", "").strip()
    media: MediaSource = LocalMedia(Path(media_dir)) if media_dir else NoMedia()
    if not e.get("META_API_KEY", "").strip():
        return HelperDeps(media=media)
    model = from_env("mimi", env=e)
    return HelperDeps(vision=model, judge=model, stock=model,
                      segmenter=sam_from_env(e) or NoSegmenter(), media=media)


def mimi_helper(registry: ToolRegistry, policy: RiskPolicy, *,
                distributor: str = "Coral Lane Trading") -> Helper:
    ts = build_toolset(registry, "mimi", BUILTIN_TOOLS, policy)
    return Helper("mimi", MIMI_SKILL.replace("{distributor}", distributor), ts,
                  skill_version="mimi-v1")


def build_helpers(policy: RiskPolicy | None = None, *, go: SqlGo | None = None,
                  deps: HelperDeps | None = None) -> dict[str, Helper]:
    policy = policy or GoPolicy()
    deps = deps or HelperDeps()
    reg = default_registry()
    helpers: dict[str, Helper] = {
        "collector": collector_helper(reg, policy),
        "mimi": mimi_helper(reg, policy, distributor=deps.distributor),
    }
    if go is None:
        # No store (unit tests of the loop alone): the book-backed helpers
        # cannot read media or intents, so only the plain Proof is hired.
        from mimi.harness.collector import proof_helper

        helpers["proof"] = proof_helper(reg, policy)
        return helpers
    dist = deps.distributor
    register_proof_tools(reg, ProofPorts(images=StoreScreens(go, deps.media),
                                         reader=ScreenshotReader(deps.vision),
                                         distributor=dist))
    register_receipt_tools(reg, ReceiptPorts(judge=ReceiptJudge(deps.judge), distributor=dist))
    register_stock_tools(reg, StockPorts(reader=StockAnswerReader(deps.stock), distributor=dist))
    register_credit(reg, CreditPorts(photos=StorePhotos(go, deps.media),  # type: ignore[arg-type]
                                     segmenter=deps.segmenter, distributor=dist))
    helpers["proof"] = Helper(
        "proof", PROOF_SKILL + SCREENSHOT_SKILL,
        build_toolset(reg, "proof", PROOF_TOOLS + SCREENSHOT_TOOLS + BUILTIN_TOOLS, policy),
        skill_version="proof-v0.2")
    helpers["order"] = Helper(
        "order", ORDER_SKILL + APPROVAL_LINE,
        build_toolset(reg, "order", RECEIPT_TOOLS + BUILTIN_TOOLS, policy),
        skill_version="order-v0.1")
    helpers["stock"] = Helper(
        "stock", STOCK_SKILL, build_toolset(reg, "stock", STOCK_TOOLS + BUILTIN_TOOLS, policy),
        skill_version="stock-v0.1")
    register_brand_tools(reg, BrandPorts(distributor=dist))
    helpers["brand"] = Helper(
        "brand", BRAND_SKILL,
        build_toolset(reg, "brand", BRAND_TOOLS + BUILTIN_TOOLS, policy),
        skill_version="brand-v0.1")
    credit = credit_helper(reg, policy, distributor=dist)
    helpers["credit"] = Helper(credit.name, credit.skill + APPROVAL_LINE, credit.toolset,
                               credit.budget, skill_version=credit.skill_version)
    return helpers


__all__ = ["HelperDeps", "build_helpers", "deps_from_env", "ChatMessage"]

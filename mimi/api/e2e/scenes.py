"""The ten Coral Lane shops of the e2e run, their scenes and their personas.

All synthetic: the fictional Coral Lane Trading world (``mimi.world``), its
fictional shops, the synthetic screenshots (``eval/proof/screens``) and shelf
photos (``eval/credit/shelves``). The shops' words are written live by a
model-driven persona per shop (``mimi.sim.persona`` voices: Nigerian English,
Pidgin, Yoruba, Igbo or Hausa mixed with English), recorded and replayed.

| Scene | Shops | What happens |
|---|---|---|
| receipt | shop-040, shop-023 (all arrived), shop-008 (2 cartons short) | Go's delivery_completed -> Order sends the receipt -> the shop replies (YES, or a complaint) |
| proof | shop-035 (fake then genuine), shop-038 (genuine then fake), shop-025 (fake then genuine) | the shop sends payment screenshots with a caption |
| stock | shop-012, shop-014 | the weekly stock check -> one-tap count -> the shop answers -> count, restock draft |
| credit | shop-020 (x01, a cash payer), shop-011 (s11, pays on time) | the shop asks for more credit -> photo ask -> shelf photo -> SAM 3.1 count -> approvals |
| steer (RAI-301) | shop-033 | receipt -> "Yes all complete. How much I owe now?" -> Order takes the YES, a follow-up turn gives the balance |
| steer (RAI-301) | shop-014 | while its stock answer runs, a voice note "abeg make am 6 tubs, no be 4" -> absorbed at the next step -> one draft of 6 |
| steer (RAI-301) | shop-020 | while its credit request waits for the owner, a payment screenshot -> Proof, the credit approval untouched |
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from mimi.models.base import ChatMessage, ChatModel, ModelError
from mimi.sim.persona import voice_for
from mimi.world.model import Shop, World

ROOT = Path(__file__).resolve().parents[3]
TENANT = "coral-lane"
SEED = 272

RECEIPT_SHOPS: tuple[str, ...] = ("shop-040", "shop-008", "shop-023")
# shop -> screenshots in the order the shop sends them (eval/proof/screens.jsonl ids)
PROOF_SHOPS: dict[str, tuple[str, ...]] = {
    "shop-035": ("f08", "g02"),
    "shop-038": ("g03", "f09"),
    "shop-025": ("f11", "g05"),
}
STOCK_SHOPS: tuple[str, ...] = ("shop-012", "shop-014")
# shop -> its shelf photo (eval/credit/shelves.jsonl ids)
CREDIT_SHOPS: dict[str, str] = {"shop-020": "x01", "shop-011": "s11"}

# Steering scenes (RAI-301). Fixed words (not personas), so the scene tests one thing.
# shop-033: the receipt reply carries a second ask. shop-014: a voice note corrects
# the restock quantity while its stock turn runs. shop-020: a payment screenshot
# arrives while its credit request waits for the owner.
MULTIPART_SHOP = "shop-033"
MULTIPART_TEXT = "Yes all complete. How much I owe now?"
MIDTASK_SHOP = "shop-014"
MIDTASK_VOICE = "abeg make am 6 tubs, no be 4"
MIDTASK_QUANTITY = 6
PARKED_SHOP = "shop-020"
PARKED_SCREEN = "g05"
PARKED_CAPTION = "I don pay part of my balance"

ALL_SHOPS: tuple[str, ...] = (*RECEIPT_SHOPS, *PROOF_SHOPS, *STOCK_SHOPS, *CREDIT_SHOPS,
                              MULTIPART_SHOP)


@dataclass(frozen=True)
class Image:
    ref: str  # local:<path under eval/>
    sha256: str
    width: int
    height: int
    meta: dict[str, object]


def screenshot(sid: str) -> Image:
    rows = {r["id"]: r for r in map(json.loads, (ROOT / "eval/proof/screens.jsonl")
                                    .read_text().splitlines())}
    r = rows[sid]
    return _image(f"proof/{r['file']}", r)


def shelf(sid: str) -> Image:
    rows = {r["id"]: r for r in map(json.loads, (ROOT / "eval/credit/shelves.jsonl")
                                    .read_text().splitlines())}
    r = rows[sid]
    return _image(f"credit/{r['file']}", r)


def _image(rel: str, meta: dict[str, object]) -> Image:
    from PIL import Image as PILImage

    path = ROOT / "eval" / rel
    data = path.read_bytes()
    with PILImage.open(path) as im:
        w, h = im.size
    return Image(f"local:{rel}", hashlib.sha256(data).hexdigest(), w, h, meta)


# ------------------------------------------------------------------ personas


def _who(world: World, shop: Shop) -> str:
    return (f"You are {shop.owner}, owner of {shop.name}, a {shop.shop_type} at {shop.street}, "
            f"{shop.area}, Ado-Ekiti. {world.distributor} supplies you on credit. Mimi is "
            f"their WhatsApp assistant. Write like a real Ado-Ekiti shop owner on WhatsApp, in "
            f"{voice_for(shop.shop_id)}: short, small typos are fine. Never invent amounts.")


def _one(model: ChatModel, system: str, user: str, meta: dict[str, str]) -> str:
    try:
        res = model.complete([ChatMessage("developer", system), ChatMessage("user", user)],
                             max_tokens=1200, meta=meta)
    except ModelError:
        return ""
    lines = [ln.strip().strip('"') for ln in res.text.splitlines() if ln.strip()]
    return "\n".join(lines[:3])[:600]


def caption(model: ChatModel, world: World, shop: Shop, sid: str) -> str:
    """What the shop types with its payment screenshot."""
    return _one(model, _who(world, shop) + " You just paid by bank transfer.",
                "You are sending Mimi the screenshot of your transfer now. Write the one short "
                "line you type with it. Output only that line.",
                {"script": "e2e_caption", "shop_id": shop.shop_id, "screen": sid})


def credit_ask(model: ChatModel, world: World, shop: Shop) -> str:
    return _one(model, _who(world, shop) + " Business is moving and you want to buy more.",
                "Ask Mimi for more credit (a bigger credit limit) in one short WhatsApp "
                "message. Output only the message.",
                {"script": "e2e_credit_ask", "shop_id": shop.shop_id})


def shelf_caption(model: ChatModel, world: World, shop: Shop, asked: str) -> str:
    return _one(model, _who(world, shop),
                f"Mimi wrote to you: {asked!r}\nYou are sending the photo of your shelf now. "
                "Write the one short line you type with it. Output only that line.",
                {"script": "e2e_shelf_caption", "shop_id": shop.shop_id})


def stock_answer(model: ChatModel, world: World, shop: Shop, item: str, unit: str, left: int,
                 asked: str, round_no: int) -> str:
    from mimi.sim.stock import _shop_answer_prompt

    return _one(model, _shop_answer_prompt(world, shop.shop_id, item, unit, left),
                f"Mimi wrote: {asked}",
                {"script": "e2e_stock", "shop_id": shop.shop_id, "round": str(round_no)})

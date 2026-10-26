"""A shop owner who answers Mimi on WhatsApp, driven by a model.

The persona gets Mimi's outbound message, its own world state (who it is, how
it pays and chats, its balance, and what actually arrived) and replies as that
shop: short, in the shop's own voice (Nigerian English, Pidgin, or English
mixed with Yoruba, Igbo or Hausa, picked per shop), sometimes a burst of 2
or 3 messages, sometimes hours later or next morning. How long it takes to
reply is decided in code from the persona's traits, so a run is repeatable.

With no key the ``fake_shop`` script below writes the replies.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from mimi.models.base import ChatMessage, ChatModel, ModelError
from mimi.receipts.receipt import naira
from mimi.world.model import Delivery, Shop, World

_UNIT_WORDS = {
    "ctn": "cartons",
    "bag": "bags",
    "pk": "packs",
    "btl": "bottles",
    "keg": "kegs",
    "tin": "tins",
}


@dataclass(frozen=True)
class OutMessage:
    text: str
    delay: timedelta  # after Mimi's message


def _rng(*parts: object) -> random.Random:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def reply_delay(shop: Shop, round_no: int, rng: random.Random) -> timedelta:
    """Most reply within the hour; some hours later; some next morning."""
    roll = rng.random()
    if shop.chat == "a_lot":
        if roll < 0.75:
            return timedelta(minutes=rng.randint(1, 35))
        return timedelta(hours=rng.randint(1, 4), minutes=rng.randint(0, 59))
    if roll < 0.55:
        return timedelta(minutes=rng.randint(5, 80))
    if roll < 0.85 or round_no > 0:
        return timedelta(hours=rng.randint(2, 6), minutes=rng.randint(0, 59))
    return timedelta(hours=15, minutes=rng.randint(0, 90))  # next morning


# How each shop writes, picked from the shop id so a run is repeatable. Most
# write Nigerian English; some Pidgin, some mix in Yoruba (Ado-Ekiti is a
# Yoruba town), a few write Igbo or Hausa. This exercises Mimi's reader.
VOICES: tuple[tuple[str, float], ...] = (
    ("short casual Nigerian English, small typos", 0.45),
    ("Nigerian Pidgin", 0.2),
    ("Yoruba mixed with English words", 0.2),
    ("Igbo mixed with English words", 0.08),
    ("Hausa mixed with English words", 0.07),
)


def voice_for(shop_id: str) -> str:
    roll = _rng("voice", shop_id).random()
    for voice, share in VOICES:
        if roll < share:
            return voice
        roll -= share
    return VOICES[0][0]


def _persona_prompt(world: World, shop: Shop, delivery: Delivery) -> str:
    truth = "Everything on the receipt arrived."
    if delivery.shortage:
        s = delivery.shortage
        truth = (
            f"{s.missing} {_UNIT_WORDS.get(s.unit, s.unit)} of {s.name} on the receipt "
            "did NOT arrive. You will not say YES; you complain about it."
        )
    pays = {
        "on_time": "you pay on time",
        "late": "you often pay late and promise dates you miss",
        "disputes": "you check every delivery and argue when something is short",
    }[shop.pays]
    chat = (
        "you chat a lot and greet first"
        if shop.chat == "a_lot"
        else "you write very little"
    )
    return (
        f"You are {shop.owner}, owner of {shop.name}, a {shop.shop_type} at "
        f"{shop.street}, {shop.area}, Ado-Ekiti. {world.distributor} supplies you on "
        f"credit. You owe them {naira(world.outstanding_kobo(shop.shop_id))} from "
        f"before. Personality: {pays}; {chat}.\n"
        f"Truth about today's delivery: {truth}\n"
        "Write like a real Ado-Ekiti shop owner on WhatsApp, in "
        f"{voice_for(shop.shop_id)}. "
        "Short messages, small typos are fine, sometimes ALL CAPS, "
        "sometimes just 'ok' or a thumbs up, sometimes '[voice note 0:12]'. "
        "If everything arrived you may confirm, but you do not always follow "
        "instructions exactly. Never invent amounts. Output 1 to 3 WhatsApp "
        "messages, one per line, nothing else."
    )


class ShopPersona:
    def __init__(
        self, world: World, shop: Shop, model: ChatModel, seed: int = 0
    ) -> None:
        self.world = world
        self.shop = shop
        self.model = model
        self.seed = seed
        self.history: list[ChatMessage] = []
        self.round = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def reply(self, inbound: str, delivery: Delivery) -> list[OutMessage]:
        meta = {
            "script": "shop",
            "shop_id": self.shop.shop_id,
            "chat": self.shop.chat,
            "pays": self.shop.pays,
            "round": str(self.round),
            "seed": str(self.seed),
            "short": "",
        }
        if delivery.shortage:
            s = delivery.shortage
            meta["short"] = f"{s.missing} {_UNIT_WORDS.get(s.unit, s.unit)}"
        self.history.append(ChatMessage("user", inbound))
        try:
            res = self.model.complete(
                [
                    ChatMessage(
                        "developer", _persona_prompt(self.world, self.shop, delivery)
                    ),
                    *self.history,
                ],
                max_tokens=1200,
                meta=meta,
            )
        except ModelError:
            return []
        self.prompt_tokens += res.prompt_tokens
        self.completion_tokens += res.completion_tokens
        lines = [ln.strip().strip('"') for ln in res.text.splitlines() if ln.strip()][
            :3
        ]
        self.history.append(ChatMessage("assistant", "\n".join(lines)))
        rng = _rng(self.seed, self.shop.shop_id, self.round)
        first = reply_delay(self.shop, self.round, rng)
        self.round += 1
        out, gap = [], timedelta()
        for line in lines:
            out.append(OutMessage(line[:300], first + gap))
            gap += timedelta(seconds=rng.randint(8, 70))
        return out


# --------------------------------------------------------------- fake script

_TERSE_YES = (
    "YES",
    "yes",
    "Yes o",
    "YES RECEIVED",
    "yess",
    "Yes ma",
    "yes recieved thank u",
)
_FIRST_UNCLEAR = (
    ("Good afternoon sir", "we have recieved the goods thank u"),
    ("\U0001f44d",),
    ("[voice note 0:14]",),
    ("ok",),
    ("Good evening", "ur rep just drop it", "thanks"),
    ("Who is this pls",),
    ("Received", "\U0001f64f\U0001f3fe"),
)
_AFTER_CLARIFY = ("YES", "Yes \U0001f44d", "yes all complete", "YES SIR", "yes")


def fake_shop(messages: Sequence[ChatMessage], meta: Mapping[str, str]) -> str:
    """Deterministic shop replies, picked by shop, round and seed."""
    rng = _rng(meta.get("seed", ""), meta.get("shop_id", ""), meta.get("round", ""))
    round_no = int(meta.get("round", "0") or 0)
    if meta.get("short"):
        short = meta["short"]
        if round_no == 0:
            burst = [
                "Good afternoon",
                f"I didn't collect the {short}",
                "pls check with ur rep",
            ]
            return "\n".join(burst[: rng.choice((2, 3))])
        return f"I SAID I DIDN'T COLLECT THE {short.upper()}"
    if round_no > 0:
        return rng.choice(_AFTER_CLARIFY)
    unclear_odds = 0.6 if meta.get("chat") == "a_lot" else 0.25
    if rng.random() < unclear_odds:
        return "\n".join(rng.choice(_FIRST_UNCLEAR))
    return rng.choice(_TERSE_YES)


def at(base: datetime, msg: OutMessage) -> datetime:
    return base + msg.delay

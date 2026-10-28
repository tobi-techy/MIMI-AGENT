"""The one front-door envelope every wake-up arrives in (RAI-280).

Every trigger (a shop's WhatsApp message, the owner's console message, a
rep, a routine timer, a bank or Go webhook, a helper finishing, a scheduled
check-in, an approval tap) becomes an ``Inbound`` before anything else looks
at it:

    {source, org_id, thread_id?, external_id, received_at, payload}

``external_id`` is the provider's own id (the WhatsApp message id, the
webhook id, the routine run id). The front door drops a second delivery of
the same id, so a replayed webhook never wakes the agent twice.

Shop, rep, caption and transcript text is untrusted data (RAI-239): it is
carried, routed on and shown to models inside ``untrusted`` tags, never
followed.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Source = Literal[
    "owner_message",
    "shop_message",
    "rep_message",
    "routine",
    "webhook",
    "helper_finished",
    "scheduled_check_in",
    "approval_answered",
]

MediaKind = Literal["none", "image", "voice", "document"]


class Media(BaseModel):
    """What came with a message. Never the bytes: a hash and cheap metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: MediaKind = "none"
    mime: str = ""
    sha256: str = ""
    width: int = 0
    height: int = 0
    # WhatsApp keeps camera EXIF on photos and none on screenshots; the
    # upload step records whether a camera model was present.
    camera_exif: bool = False
    caption: str = ""
    # Voice notes: the speech router's transcript (RAI-290) and its language.
    transcript: str = ""
    transcript_lang: str = ""


class Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = ""
    # The sender's phone as the channel gave it (owner, shop or rep). Hashed
    # before any trace; used only to look the sender up in the directory.
    from_phone: str = ""
    media: Media = Field(default_factory=Media)
    # A tapped quick-reply button ("btn:collector/payment-check") or list row.
    button_id: str = ""
    # Routine name (``collector.overdue_sweep``), webhook kind
    # (``bank_credit``), the helper that finished, or the approval answered.
    name: str = ""
    # For approval_answered / helper_finished: the route of the waiting run.
    for_helper: str = ""
    for_skill: str = ""
    shop_id: str = ""
    answer: str = ""
    # A follow-up for one part of an earlier message (RAI-301): the message it
    # came from, and what the steer stage already decided for it ("new", or
    # "<action>:<task key>"), so it is never split or classified twice.
    part_of: str = ""
    steer: str = ""


class Inbound(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Source
    org_id: str
    external_id: str
    received_at: datetime
    payload: Payload = Field(default_factory=Payload)
    # Set when the channel already knows the thread (console, job events).
    thread_id: str = ""

    @property
    def sender(self) -> Literal["owner", "shop", "rep", "system"]:
        return {"owner_message": "owner", "shop_message": "shop",
                "rep_message": "rep"}.get(self.source, "system")  # type: ignore[return-value]

    @property
    def text(self) -> str:
        """Everything a person said: typed text, caption, voice transcript."""
        p = self.payload
        parts = [p.text.strip(), p.media.caption.strip(), p.media.transcript.strip()]
        return "\n".join(x for x in parts if x)


class Directory(BaseModel):
    """Who is who for one distributor: the owner's numbers, shops, reps.

    Go owns this (tenancy); the router only reads it. A phone that is not in
    it is an unknown number and always goes to onboarding.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: str
    owner_phones: frozenset[str] = frozenset()
    shop_phones: dict[str, str] = Field(default_factory=dict)  # phone -> shop_id
    rep_phones: dict[str, str] = Field(default_factory=dict)  # phone -> rep_id
    owner_thread: str = ""

    def shop_for(self, phone: str) -> str:
        return self.shop_phones.get(phone, "")

    def is_owner(self, inbound: Inbound) -> bool:
        """An owner message is the owner only from a number Go knows as theirs.

        No number means the console (Go signed the owner in); an owner-channel
        message from any other number is a stranger and is treated as one by
        the front door (no stop, no steering, no owner thread) and the router.
        """
        if inbound.source != "owner_message":
            return False
        phone = inbound.payload.from_phone
        return not phone or phone in self.owner_phones

    def thread_for(self, inbound: Inbound) -> tuple[str, str]:
        """(thread_id, note). Unclear events go to the owner thread with a note."""
        owner = self.owner_thread or f"{self.org_id}:owner"
        p = inbound.payload
        people = ("owner_message", "shop_message", "rep_message")
        if inbound.thread_id and inbound.source not in people \
                and inbound.thread_id.startswith(f"{self.org_id}:"):
            # A system event may name its thread, inside this distributor only.
            # A person's thread always comes from their number, so no envelope
            # can put a stranger on the owner's (or another shop's) thread.
            return inbound.thread_id, ""
        if inbound.source == "owner_message":
            if self.is_owner(inbound):
                return owner, ""
            # Not the owner: never the owner thread (its history and batch).
            return f"{self.org_id}:new:{_tail(p.from_phone)}", \
                "an owner-channel message from a number not on the owner list"
        if inbound.source == "shop_message":
            shop = self.shop_for(p.from_phone)
            if shop:
                return f"{self.org_id}:shop:{shop}", ""
            return f"{self.org_id}:new:{_tail(p.from_phone)}", ""
        if inbound.source == "rep_message":
            rep = self.rep_phones.get(p.from_phone, "")
            if rep:
                return f"{self.org_id}:rep:{rep}", ""
            # Not a rep Go knows: never the owner thread (its history, its
            # batch and the owner's trust), like a stranger on the owner channel.
            return f"{self.org_id}:new:{_tail(p.from_phone)}", \
                "a rep message from a number not on the rep list"
        if p.shop_id:
            return f"{self.org_id}:shop:{p.shop_id}", ""
        if inbound.source in ("routine", "scheduled_check_in", "helper_finished"):
            name = p.name or p.for_helper or "job"
            return f"{self.org_id}:job:{name}", ""
        return owner, f"no thread for this {inbound.source}; put on the owner thread"


# Dev and CI only: a fixed key keeps thread ids stable across runs. A plain
# hash of a phone number is not a pseudonym (about 10^10 Nigerian mobiles can
# be brute-forced in minutes), so production must set its own secret.
_DEV_PSEUDONYM_KEY = b"mimi-dev-only-not-a-secret"


def _pseudonym_key() -> bytes:
    key = os.environ.get("MIMI_PHONE_PSEUDONYM_KEY", "").strip()
    if key:
        return key.encode()
    if os.environ.get("MIMI_ENV", "").strip().lower() in ("prod", "production"):
        raise RuntimeError("MIMI_PHONE_PSEUDONYM_KEY is not set: refusing to make "
                           "thread ids from phone numbers without a secret key")
    return _DEV_PSEUDONYM_KEY


def _tail(phone: str) -> str:
    """A stable pseudonym for an unknown number (never the number itself).

    Keyed (HMAC-SHA256 with a server-side secret), so a thread id that leaks
    into a prompt, a run row or a trace does not give the number back.
    """
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return "unknown"
    return hmac.new(_pseudonym_key(), digits.encode(), hashlib.sha256).hexdigest()[:12]

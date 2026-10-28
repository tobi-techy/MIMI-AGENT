"""Voice replies on the API path (RAI-290): the text Mimi sends, read aloud.

Three pieces, all code:

* **The flag.** ``MIMI_VOICE_REPLIES`` turns voice replies on per
  distributor: empty or ``0`` is off (the default), ``all`` is every
  distributor, else a comma list of tenant ids. Off, nothing here runs and
  the API behaves exactly as before.
* **The shop's choice.** A shop sends ``VOICE ON [LANGUAGE]`` or ``VOICE
  OFF``. Code records it as the shop's own consent (``shop_voice_consent``,
  the same table and rule as the receipt flow: nobody else can opt a shop
  in) and answers with a fixed line. No model is called.
* **The voice note.** A reply gets a voice note only when the flag is on,
  the shop opted in, the shop's own message in that turn was a voice note,
  the reply is on the shop's WhatsApp and is ready to send (never a draft
  waiting for the owner). The script is ``mimi.voice.render`` of the exact
  text: the same words, amounts and dates said in words from the same kobo
  and day, checked in code. When the check fails the reply is text only.
  The text always goes.

The audio is made when the channel asks for it
(``GET /v1/messages/{message_id}/voice``) and never stored.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.store import schema as s
from mimi.voice.prefs import (
    VoicePreference,
    parse_voice_command,
    shop_voice_consent,
)
from mimi.voice.render import render_voice

LAGOS = ZoneInfo("Africa/Lagos")

VOICE_ON_TEXT = (
    "Voice replies are on. When you send me a voice note, I will answer with a "
    "voice note too, in {language}. The same message always comes as text as well. "
    "Send VOICE OFF to stop."
)
VOICE_OFF_TEXT = "Voice replies are off. I will answer in text only. Send VOICE ON to start again."


def voice_enabled(tenant_id: str, env: Mapping[str, str] | None = None) -> bool:
    e = env if env is not None else os.environ
    raw = e.get("MIMI_VOICE_REPLIES", "").strip().lower()
    if raw in ("", "0", "off", "false", "no"):
        return False
    if raw in ("all", "1", "on", "true", "yes"):
        return True
    return tenant_id.lower() in {t.strip() for t in raw.split(",") if t.strip()}


def ensure_schema(engine: Engine) -> None:
    shop_voice_consent.create(engine, checkfirst=True)


def current_pref(conn: sa.Connection, shop_id: str) -> VoicePreference:
    row = conn.execute(
        sa.select(shop_voice_consent)
        .where(shop_voice_consent.c.shop_id == shop_id)
        .order_by(shop_voice_consent.c.decided_at.desc(),
                  shop_voice_consent.c.consent_message_id.desc())
        .limit(1)
    ).mappings().first()
    return VoicePreference(**dict(row)) if row else VoicePreference(shop_id)


def voice_command(text: str) -> tuple[bool, str | None] | None:
    return parse_voice_command(text)


def record_choice(conn: sa.Connection, *, shop_id: str, on: bool, language: str | None,
                  message_id: str, text: str, sender: str, at: str) -> VoicePreference:
    """Store the shop's own choice (idempotent on its message id)."""
    prior = current_pref(conn, shop_id)
    pref = VoicePreference(
        shop_id=shop_id, voice_replies=on,
        language=language or (prior.language if prior.consent_message_id else "english"),
        consent_message_id=message_id, consent_text=text, consent_sender=sender,
        decided_at=at,
    )
    seen = conn.execute(sa.select(shop_voice_consent.c.consent_message_id).where(
        shop_voice_consent.c.consent_message_id == message_id)).first()
    if seen is None:
        conn.execute(shop_voice_consent.insert().values(**pref.__dict__))
    return pref


def choice_text(pref: VoicePreference) -> str:
    if pref.voice_replies:
        return VOICE_ON_TEXT.format(language=pref.language.capitalize())
    return VOICE_OFF_TEXT


def _turn_had_voice(conn: sa.Connection, tenant: str, turn_id: str) -> bool:
    """Did the shop's own message in this turn (or the turns merged into it) come as a voice note?"""
    rows = list(conn.execute(sa.select(s.inbound_messages.c.envelope).where(
        s.inbound_messages.c.tenant_id == tenant,
        s.inbound_messages.c.turn_id == turn_id)).all())
    merged = conn.execute(sa.select(s.turns.c.turn_id).where(
        s.turns.c.tenant_id == tenant, s.turns.c.merged_into == turn_id)).all()
    for (tid,) in merged:
        rows += list(conn.execute(sa.select(s.inbound_messages.c.envelope).where(
            s.inbound_messages.c.tenant_id == tenant,
            s.inbound_messages.c.turn_id == tid)).all())
    for (env,) in rows:
        try:
            e = json.loads(env)
        except (TypeError, ValueError):
            continue
        if e.get("source") == "shop_message" and \
                ((e.get("payload") or {}).get("media") or {}).get("kind") == "voice":
            return True
    return False


def reply_voice(conn: sa.Connection, tenant: str, m: Any, *,
                env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """The voice reply for one outbound message row, or None for text only."""
    if m.direction != "out" or m.channel != "shop_whatsapp" or m.status != "ready":
        return None
    if not m.shop_id or not voice_enabled(tenant, env):
        return None
    pref = current_pref(conn, m.shop_id)
    if not pref.speaks or not _turn_had_voice(conn, tenant, m.turn_id):
        return None
    today = datetime.fromisoformat(m.at).astimezone(LAGOS).date()
    vs, _why = render_voice(m.text, pref.language, today=today)
    if vs is None:
        return None
    return {"script": vs.script, "voice": vs.voice, "language": pref.language,
            "amounts_kobo": list(vs.amounts_kobo),
            "days": [d.isoformat() for d in vs.days]}

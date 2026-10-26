"""Whether a shop wants Mimi's short replies as voice notes too. Default off.

A shop turns voice replies on or off itself, by sending Mimi a message:

* ``VOICE ON`` (optionally a language: ``VOICE ON YORUBA``; English, Pidgin,
  Yoruba, Igbo or Hausa), or ``VOICE OFF``.

Nobody else can turn it on: an owner or a distributor cannot opt a shop in.
Every choice is kept as its own consent row with the shop's message id, its
words, who sent it and when (``shop_voice_consent``; alembic ``0004``), and
the newest row is the shop's current choice. Turning voice on changes only
how Mimi's short replies are delivered: the text always goes too, and every
amount and day is in the text.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Engine

VOICE_LANGUAGES: tuple[str, ...] = ("english", "pidgin", "yoruba", "igbo", "hausa")
_COMMAND = re.compile(
    r"^\s*voice\s+(?P<on>on|off)(?:\s+(?:in\s+)?(?P<lang>english|pidgin|yoruba|igbo|hausa))?\s*[.!]?\s*$",
    re.I,
)


@dataclass(frozen=True)
class VoicePreference:
    shop_id: str
    voice_replies: bool = False
    language: str = "english"
    # The shop's own message that made this choice (its consent record).
    consent_message_id: str = ""
    consent_text: str = ""
    consent_sender: str = ""
    decided_at: str = ""

    @property
    def speaks(self) -> bool:
        return self.voice_replies and self.language in VOICE_LANGUAGES


def parse_voice_command(text: str) -> tuple[bool, str | None] | None:
    """(on, language or None) for "VOICE ON [LANGUAGE]" / "VOICE OFF"."""
    m = _COMMAND.match(text or "")
    if m is None:
        return None
    lang = m.group("lang")
    return m.group("on").lower() == "on", lang.lower() if lang else None


class VoicePrefs(Protocol):
    def get(self, shop_id: str) -> VoicePreference: ...

    def record(self, pref: VoicePreference) -> bool:
        """Store a choice; False when its consent message was already seen."""
        ...


def _check(pref: VoicePreference) -> None:
    if not (pref.consent_message_id and pref.consent_text and pref.decided_at):
        raise ValueError("a voice choice needs the shop's own message as consent")
    if pref.language not in VOICE_LANGUAGES:
        raise ValueError(f"no voice for language {pref.language!r}")


class InMemoryVoicePrefs:
    def __init__(self) -> None:
        self._rows: list[VoicePreference] = []
        self._lock = threading.Lock()

    def record(self, pref: VoicePreference) -> bool:
        _check(pref)
        with self._lock:
            if any(r.consent_message_id == pref.consent_message_id for r in self._rows):
                return False
            self._rows.append(pref)
            return True

    def get(self, shop_id: str) -> VoicePreference:
        with self._lock:
            rows = [r for r in self._rows if r.shop_id == shop_id]
        if not rows:
            return VoicePreference(shop_id)
        return max(rows, key=lambda r: (r.decided_at, r.consent_message_id))

    def history(self, shop_id: str) -> list[VoicePreference]:
        with self._lock:
            return [r for r in self._rows if r.shop_id == shop_id]


metadata = sa.MetaData()
shop_voice_consent = sa.Table(
    "shop_voice_consent",
    metadata,
    sa.Column("consent_message_id", sa.String(), primary_key=True),
    sa.Column("shop_id", sa.String(), nullable=False, index=True),
    sa.Column("voice_replies", sa.Boolean(), nullable=False),
    sa.Column("language", sa.String(), nullable=False),
    sa.Column("consent_text", sa.Text(), nullable=False),
    sa.Column("consent_sender", sa.String(), nullable=False),
    sa.Column("decided_at", sa.String(), nullable=False),
)


class SqlVoicePrefs:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def ensure_schema(self) -> None:
        metadata.create_all(self.engine)

    def record(self, pref: VoicePreference) -> bool:
        _check(pref)
        with self.engine.begin() as conn:
            seen = conn.execute(
                sa.select(shop_voice_consent.c.consent_message_id).where(
                    shop_voice_consent.c.consent_message_id == pref.consent_message_id
                )
            ).first()
            if seen is not None:
                return False
            conn.execute(shop_voice_consent.insert().values(**pref.__dict__))
            return True

    def get(self, shop_id: str) -> VoicePreference:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(shop_voice_consent)
                    .where(shop_voice_consent.c.shop_id == shop_id)
                    .order_by(
                        shop_voice_consent.c.decided_at.desc(),
                        shop_voice_consent.c.consent_message_id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .first()
            )
        return VoicePreference(**dict(row)) if row else VoicePreference(shop_id)

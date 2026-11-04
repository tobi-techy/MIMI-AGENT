"""Connector settings, all from the environment (RAI-277).

* ``MIMI_CONNECTORS``: ``fake`` (default: recorded fixtures, no network),
  ``composio`` (real transport, needs ``COMPOSIO_API_KEY``) or ``off``.
* ``COMPOSIO_API_KEY``: the Composio key, from the shell only.
* ``MIMI_COMPOSIO_AUTH_ZOHO_BOOKS``, ``MIMI_COMPOSIO_AUTH_ZOHO``,
  ``MIMI_COMPOSIO_AUTH_GOOGLESHEETS``: our own OAuth app auth config ids,
  so the consent screen says Mimi, not Composio.
* ``MIMI_COMPOSIO_CALLBACK_URL``: where Composio returns after consent.
* ``MIMI_ZOHO_REGION``: the Zoho account region (``com``, ``eu``, ``in``,
  ``cn`` or ``au``); default ``com``.
* ``MIMI_COMPOSIO_VERSION_ZOHO_BOOKS``, ``MIMI_COMPOSIO_VERSION_ZOHO``,
  ``MIMI_COMPOSIO_VERSION_GOOGLESHEETS``: pinned toolkit versions
  (defaults match ``allowlist.py``; direct execution refuses unpinned).
* ``MONO_SECRET_KEY``, ``MONO_API_BASE``: the Mono stub's settings,
  unused until the Mono connector is built.
* ``MIMI_CONNECTOR_STATE_SECRET``: signs the OAuth state; dev default.

Keys are read here and passed to the transport; nothing else touches them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from mimi.connectors.allowlist import SHEETS_VERSION, ZOHO_BOOKS_VERSION, ZOHO_CRM_VERSION

ZOHO_REGIONS = ("com", "eu", "in", "cn", "au")
MODES = ("fake", "composio", "off")


@dataclass(frozen=True)
class ConnectorSettings:
    mode: str = "fake"
    composio_api_key: str = ""
    auth_zoho_books: str = ""
    auth_zoho: str = ""
    auth_googlesheets: str = ""
    callback_url: str = ""
    zoho_region: str = "com"
    version_zoho_books: str = ZOHO_BOOKS_VERSION
    version_zoho: str = ZOHO_CRM_VERSION
    version_googlesheets: str = SHEETS_VERSION
    mono_secret_key: str = ""
    mono_api_base: str = ""
    state_secret: str = "local-connector-state-secret"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ConnectorSettings:
        e = env if env is not None else os.environ
        region = (e.get("MIMI_ZOHO_REGION", "").strip() or "com").lower()
        if region not in ZOHO_REGIONS:
            raise ValueError(f"MIMI_ZOHO_REGION is {region!r}: use one of {ZOHO_REGIONS}")
        mode = (e.get("MIMI_CONNECTORS", "").strip() or "fake").lower()
        if mode not in MODES:
            raise ValueError(f"MIMI_CONNECTORS is {mode!r}: use one of {MODES}")
        return cls(
            mode=mode,
            composio_api_key=e.get("COMPOSIO_API_KEY", "").strip(),
            auth_zoho_books=e.get("MIMI_COMPOSIO_AUTH_ZOHO_BOOKS", "").strip(),
            auth_zoho=e.get("MIMI_COMPOSIO_AUTH_ZOHO", "").strip(),
            auth_googlesheets=e.get("MIMI_COMPOSIO_AUTH_GOOGLESHEETS", "").strip(),
            callback_url=e.get("MIMI_COMPOSIO_CALLBACK_URL", "").strip(),
            zoho_region=region,
            version_zoho_books=e.get("MIMI_COMPOSIO_VERSION_ZOHO_BOOKS", "").strip()
            or ZOHO_BOOKS_VERSION,
            version_zoho=e.get("MIMI_COMPOSIO_VERSION_ZOHO", "").strip() or ZOHO_CRM_VERSION,
            version_googlesheets=e.get("MIMI_COMPOSIO_VERSION_GOOGLESHEETS", "").strip()
            or SHEETS_VERSION,
            mono_secret_key=e.get("MONO_SECRET_KEY", "").strip(),
            mono_api_base=e.get("MONO_API_BASE", "").strip(),
            state_secret=e.get("MIMI_CONNECTOR_STATE_SECRET", "").strip()
            or "local-connector-state-secret",
        )

    def auth_config_for(self, toolkit: str) -> str:
        """Our OAuth app's auth config id for a toolkit, or empty."""
        return {"ZOHO_BOOKS": self.auth_zoho_books, "ZOHO": self.auth_zoho,
                "GOOGLESHEETS": self.auth_googlesheets}.get(toolkit, "")

    def version_for(self, toolkit: str) -> str:
        """The pinned toolkit version for a toolkit."""
        return {"ZOHO_BOOKS": self.version_zoho_books, "ZOHO": self.version_zoho,
                "GOOGLESHEETS": self.version_googlesheets}.get(toolkit, "")

    @property
    def live(self) -> bool:
        """True only when the real transport can run (key set, mode on)."""
        return self.mode == "composio" and bool(self.composio_api_key)

    def connect_status(self, toolkit: str) -> tuple[bool, str]:
        """Whether this server can start a real connect flow for a toolkit,
        and, when it cannot, why in plain words (shown in Settings)."""
        upper = toolkit.upper()
        if upper not in ("ZOHO_BOOKS", "ZOHO", "GOOGLESHEETS"):
            return False, "This tool has no connect flow yet."
        if self.mode != "composio":
            return False, "Connecting tools isn't switched on for this server yet."
        if not self.composio_api_key:
            return False, "This server has no Composio key yet."
        if not self.auth_config_for(upper):
            return False, "This tool's sign-in app isn't set up on this server yet."
        if not self.callback_url:
            return False, "This server has no return address for the sign-in yet."
        return True, ""


ALL_CONNECTOR_VARS: tuple[str, ...] = (
    "COMPOSIO_API_KEY",
    "MIMI_CONNECTORS",
    "MIMI_COMPOSIO_AUTH_ZOHO_BOOKS",
    "MIMI_COMPOSIO_AUTH_ZOHO",
    "MIMI_COMPOSIO_AUTH_GOOGLESHEETS",
    "MIMI_COMPOSIO_CALLBACK_URL",
    "MIMI_ZOHO_REGION",
    "MIMI_COMPOSIO_VERSION_ZOHO_BOOKS",
    "MIMI_COMPOSIO_VERSION_ZOHO",
    "MIMI_COMPOSIO_VERSION_GOOGLESHEETS",
    "MIMI_CONNECTOR_STATE_SECRET",
    "MONO_SECRET_KEY",
    "MONO_API_BASE",
)

"""The Composio transport (RAI-277): the only module that imports the SDK.

``ComposioTransport`` executes one allowlisted slug for one tenant's
connected account, with the toolkit version pinned. Direct execution
refuses an unpinned version. ``FakeComposio`` replays recorded fixtures
(``eval/connectors/fixtures/``), like ``FakeModel``: tests and demo
sessions never start real OAuth and make no network call.

The model never touches this module. Only allowlisted actions wrapped by
``mimi/connectors/`` call ``execute``; anything else raises.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mimi.connectors import allowlist
from mimi.connectors.base import ConnectorError, log_failure
from mimi.connectors.env import ConnectorSettings

log = logging.getLogger(__name__)

_LABELS = {"ZOHO_BOOKS": "Zoho Books", "ZOHO": "Zoho CRM", "GOOGLESHEETS": "Google Sheets"}


def _label(toolkit: str) -> str:
    """The tool's name as the owner reads it."""
    return _LABELS.get(toolkit.upper(), "The tool")

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "eval" / "connectors" / "fixtures"


class ComposioTransport:
    """Thin wrapper over the Composio SDK: allowlisted slugs only."""

    #: The toolkit this transport speaks. The payment module refuses a
    #: write through any other toolkit, so one ledger stays the ledger.
    toolkit = "ZOHO_BOOKS"

    def __init__(self, settings: ConnectorSettings | None = None) -> None:
        self.settings = settings or ConnectorSettings.from_env()
        self._client: Any = None

    def _sdk(self) -> Any:
        if self._client is None:
            try:
                from composio import Composio
            except ImportError as exc:
                raise ConnectorError(
                    "The Composio package is not installed.",
                    next_step="Install it with pip install -e '.[connectors]'.") from exc
            except Exception as exc:
                # The SDK raises at import time when its cache directory is not
                # writable (``RuntimeError: Cache directory ... is not
                # writable``), which an unguarded import turned into a 500 with
                # no sentence. A connector that cannot start must refuse like
                # any other failed read.
                raise ConnectorError(
                    "The Composio client could not start.", next_step=(
                        "Set COMPOSIO_CACHE_DIR to a writable path and retry."
                        if "Cache directory" in str(exc) else
                        "Check the Composio settings and retry.")) from exc
            versions = {
                "ZOHO_BOOKS": self.settings.version_zoho_books,
                "ZOHO": self.settings.version_zoho,
                "GOOGLESHEETS": self.settings.version_googlesheets,
            }
            kwargs: dict[str, Any] = {}
            if self.settings.composio_api_key:
                kwargs["api_key"] = self.settings.composio_api_key
            self._client = Composio(
                toolkit_versions=versions,  # type: ignore[arg-type]
                **kwargs,  # type: ignore[arg-type]
            )
        return self._client

    def execute(self, action: str, *, user_id: str, connected_account_id: str,
                arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one allowlisted read for one tenant's connected account."""
        row = allowlist.get(action)
        if row is None:
            raise ConnectorError(
                f"{action} is not an allowlisted connector action.",
                next_step="Use one of the wrapped actions in allowlist.py.")
        if row.kind != "read":
            raise ConnectorError(
                f"{action} is a write: it needs the owner's approval first.",
                next_step="Propose it, wait for approval, then apply_write.")
        self._require_version(row.toolkit, row.version)
        client = self._sdk()
        args = dict(arguments)
        if row.toolkit in ("ZOHO_BOOKS", "ZOHO"):
            args = {"__region": self.settings.zoho_region, **args}
        try:
            res = client.tools.execute(
                row.slug,
                user_id=user_id,
                connected_account_id=connected_account_id,
                version=row.version,
                arguments=args,
            )
        except Exception as exc:
            log_failure(log, f"{row.toolkit} read {action}", exc)
            raise ConnectorError(f"{_label(row.toolkit)} didn't answer. Nothing changed.",
                                 next_step="Retry the sync; the read stays refused.") from exc
        return _plain(res)

    def execute_write(self, action: str, *, user_id: str, connected_account_id: str,
                      arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one allowlisted write. Only ``apply_write`` calls this, after
        the owner approved the proposal."""
        row = allowlist.get(action)
        if row is None or row.kind != "write":
            raise ConnectorError(
                f"{action} is not an allowlisted connector write.",
                next_step="Propose a wrapped write first.")
        if row.slug.startswith("PROXY_"):
            raise ConnectorError(
                f"{action} runs over proxy execute, not tools execute.",
                next_step="Use mimi/connectors/zoho/payments.py for customer payments.")
        self._require_version(row.toolkit, row.version)
        client = self._sdk()
        try:
            res = client.tools.execute(
                row.slug,
                user_id=user_id,
                connected_account_id=connected_account_id,
                version=row.version,
                arguments=arguments,
            )
        except Exception as exc:
            log_failure(log, f"{row.toolkit} write {action}", exc)
            raise ConnectorError(f"{_label(row.toolkit)} didn't accept this. "
                                 "Nothing was recorded.",
                                 next_step="Read back the record before retrying.") from exc
        return _plain(res)

    def initiate(self, toolkit: str, *, user_id: str) -> dict[str, str]:
        """Start OAuth for one toolkit on our own auth config. Returns the
        redirect url and the state to check on callback."""
        from mimi.connectors.state import sign_state

        settings = self.settings
        auth_config = settings.auth_config_for(toolkit)
        if not auth_config:
            raise ConnectorError(
                f"No auth config is set for {toolkit}.",
                next_step="Set the MIMI_COMPOSIO_AUTH_* id for this toolkit.")
        if not settings.callback_url:
            raise ConnectorError(
                "No callback url is set.",
                next_step="Set MIMI_COMPOSIO_CALLBACK_URL first.")
        client = self._sdk()
        state = sign_state(settings.state_secret,
                           {"tenant": user_id, "toolkit": toolkit})
        # The signed state rides on the callback url: Composio sends the
        # owner back there (adding its connected account id), so the state
        # comes back with them. Appending it to the consent link instead
        # would never return.
        sep_cb = "&" if "?" in settings.callback_url else "?"
        callback = f"{settings.callback_url}{sep_cb}state={state}"
        # ``link`` is Composio's connect call for every redirectable OAuth
        # scheme, Composio-managed or our own app; ``initiate`` is retired
        # for Composio-managed OAuth configs (it now answers 400). Same
        # return shape; ``initiate`` stays only for an SDK without ``link``.
        accounts = client.connected_accounts
        start = getattr(accounts, "link", None) or accounts.initiate
        try:
            req = start(user_id=user_id, auth_config_id=auth_config,
                        callback_url=callback)
        except Exception as exc:
            log_failure(log, f"{toolkit} connect", exc)
            raise ConnectorError(f"Couldn't start connecting {_label(toolkit)}. "
                                 "Nothing changed.",
                                 next_step="Retry the connect from Settings.") from exc
        url = getattr(req, "redirect_url", "") or ""
        if not url:
            raise ConnectorError(f"The {toolkit} connect flow gave no redirect.",
                                 next_step="Retry the connect from Settings.")
        return {"redirect_url": url, "state": state}

    def account(self, connected_account_id: str) -> dict[str, Any]:
        """Status of one connected account. Raises when it is gone."""
        client = self._sdk()
        try:
            res = client.connected_accounts.get(connected_account_id)
        except Exception as exc:
            log_failure(log, "connected account read", exc)
            raise ConnectorError("That connection is gone.",
                                 next_step="Connect again from Settings.") from exc
        return _plain(res)

    def disconnect(self, connected_account_id: str) -> None:
        """Delete one connected account. Revoking writes a revoke row first."""
        client = self._sdk()
        try:
            client.connected_accounts.delete(connected_account_id)
        except Exception as exc:
            log_failure(log, "disconnect", exc)
            raise ConnectorError("Couldn't disconnect right now. Nothing changed.",
                                 next_step="Retry the disconnect.") from exc

    def _require_version(self, toolkit: str, version: str) -> None:
        """Direct execution refuses an unpinned version."""
        want = self.settings.version_for(toolkit)
        if not version or version != want:
            raise ConnectorError(
                f"{toolkit} version {version!r} is not the pinned one ({want!r}).",
                next_step="Use the pinned version in allowlist.py.")


class FakeComposio(ComposioTransport):
    """Replays recorded fixtures (``eval/connectors/fixtures/``), no network.

    Each fixture file is ``<action>.json``: a list of ``{"match": {...},
    "reply": {...}}`` entries. ``execute`` returns the first reply whose
    ``match`` is a subset of the call's arguments, and records every call
    so tests can assert exactly one call per approval. ``proxy`` replays
    ``proxy/<name>.json`` the same way.
    """

    def __init__(self, settings: ConnectorSettings | None = None,
                 fixtures: Path | None = None) -> None:
        self.settings = settings or ConnectorSettings.from_env({})
        self.dir = fixtures or FIXTURES_DIR
        self.calls: list[dict[str, Any]] = []
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def _sdk(self) -> Any:
        raise ConnectorError("FakeComposio never touches the network.",
                             next_step="Use ComposioTransport for live calls.")

    def _load(self, name: str) -> list[dict[str, Any]]:
        if name not in self._cache:
            path = self.dir / f"{name}.json"
            if not path.exists():
                raise ConnectorError(f"No fixture for {name}.",
                                     next_step="Record one with make_connector_fixtures.py.")
            self._cache[name] = json.loads(path.read_text(encoding="utf-8"))
        return self._cache[name]

    def _replay(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        for entry in self._load(name):
            match = entry.get("match", {})
            if all(arguments.get(k) == v for k, v in match.items()):
                return dict(entry.get("reply", {}))
        raise ConnectorError(f"No fixture reply matches {name} {arguments}.",
                             next_step="Record the missing fixture reply.")

    def execute(self, action: str, *, user_id: str, connected_account_id: str,
                arguments: dict[str, Any]) -> dict[str, Any]:
        row = allowlist.get(action)
        if row is None:
            raise ConnectorError(f"{action} is not an allowlisted connector action.",
                                 next_step="Use one of the wrapped actions.")
        self.calls.append({"action": action, "user_id": user_id,
                           "connected_account_id": connected_account_id,
                           "arguments": dict(arguments)})
        return self._replay(action, arguments)

    def execute_write(self, action: str, *, user_id: str, connected_account_id: str,
                      arguments: dict[str, Any]) -> dict[str, Any]:
        row = allowlist.get(action)
        if row is None or row.kind != "write" or row.slug.startswith("PROXY_"):
            raise ConnectorError(
                f"{action} is not a wrapped tools-execute write: proxy writes "
                f"run over proxy execute.",
                next_step="Use the wrapped write path.")
        self.calls.append({"action": action, "user_id": user_id,
                           "connected_account_id": connected_account_id,
                           "arguments": dict(arguments)})
        return self._replay(action, arguments)

    def proxy(self, name: str, *, method: str, endpoint: str,
              connected_account_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Replay a proxy call (Zoho Books customer payments)."""
        self.calls.append({"action": f"proxy:{name}", "method": method,
                           "endpoint": endpoint,
                           "connected_account_id": connected_account_id,
                           "arguments": dict(body)})
        return self._replay(f"proxy/{name}", body)

    def initiate(self, toolkit: str, *, user_id: str) -> dict[str, str]:  # type: ignore[override]
        from mimi.connectors.state import sign_state

        state = sign_state(self.settings.state_secret,
                           {"tenant": user_id, "toolkit": toolkit})
        return {"redirect_url": f"https://connect.example/{toolkit.lower()}?state={state}",
                "state": state}

    def account(self, connected_account_id: str) -> dict[str, Any]:
        return {"id": connected_account_id, "status": "ACTIVE"}

    def disconnect(self, connected_account_id: str) -> None:
        self.calls.append({"action": "disconnect",
                           "connected_account_id": connected_account_id})


def _plain(res: Any) -> dict[str, Any]:
    """The SDK response as a plain dict."""
    if isinstance(res, dict):
        out = res.get("data", res)
        return dict(out) if isinstance(out, dict) else {"data": out}
    for attr in ("data", "model_dump"):
        if hasattr(res, attr):
            val = getattr(res, attr)() if attr == "model_dump" else getattr(res, attr)
            if isinstance(val, dict):
                return dict(val)
            if isinstance(val, str):
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, dict):
                        return parsed
                except ValueError:
                    return {"data": val}
    return {"data": str(res)}

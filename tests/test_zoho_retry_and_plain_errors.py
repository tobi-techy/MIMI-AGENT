"""A failed Zoho write is never approved and retries without a second
payment; a tool's raw error text never reaches the owner (PR #56 review).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from mimi.api import demo_wiring as dw
from mimi.connectors.base import ConnectorError, redact
from mimi.connectors.composio import ComposioTransport
from mimi.connectors.env import ConnectorSettings
from mimi.connectors.zoho import payments as zp
from tests import test_demo_wiring as _tdw
from tests.test_demo_wiring import (
    A,
    LiveLikeZoho,
    _approve_zoho,
    _live_book,
    _live_env,
    _live_transport,
    _seed_live_connection,
    h,
)

env = _tdw.env  # the same store and client fixture as the wiring tests

RAW = "boom: x-api-key: ck_live_SUPERSECRET123 upstream 500"  # pragma: allowlist secret


class FlakyZoho(LiveLikeZoho):
    """Live-shaped Zoho whose creates can raise like a broken transport, and
    whose create can land and still answer with an error."""

    def __init__(self, **kw: Any) -> None:
        contacts, invoices = _live_book()
        super().__init__(contacts=contacts, invoices=invoices, **kw)
        self.raise_create = False
        self.land_then_raise = False
        self.fail_searches = 0
        self.creates = 0

    def proxy(self, name: str, **kw: Any) -> dict[str, Any]:
        if name == "customer_payment_search" and self.fail_searches > 0:
            self.fail_searches -= 1
            raise ConnectorError(RAW)
        if name == "customer_payment_create":
            self.creates += 1
            if self.raise_create:
                self.calls.append({"action": f"proxy:{name}", **kw})
                raise ConnectorError(RAW)
            if self.land_then_raise:
                super().proxy(name, **kw)
                # The read back right after the failed create fails too, so
                # this attempt cannot tell that the payment landed.
                self.fail_searches = 1
                raise ConnectorError(RAW)
        return super().proxy(name, **kw)


def _approval_row(env: dict[str, Any], approval_id: str) -> dict[str, Any]:
    rows = env["c"].get("/v1/approvals?status=all", headers=h()).json()["approvals"]
    return next(a for a in rows if a["approval_id"] == approval_id)


def _owner_texts(env: dict[str, Any]) -> list[dict[str, Any]]:
    return env["c"].get(f"/v1/threads/{A}:owner", headers=h()).json()["messages"]


def test_failed_write_stays_pending_with_a_plain_line_then_retries_once(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _live_env(monkeypatch)
    _seed_live_connection(env["engine"])
    live = FlakyZoho()
    live.raise_create = True
    _live_transport(monkeypatch, live)
    approval = _approve_zoho(env)
    apr = approval["approval_id"]
    # Not approved: Zoho never accepted it.
    assert approval["status"] == "pending"
    res = approval["result"]
    assert res["error"] == dw.ZOHO_WRITE_FAILED and res["retryable"] is True
    assert res["undo_available"] is False and "zoho_payment_id" not in res
    assert live.payments == {}
    failed = [m for m in _owner_texts(env) if m["kind"] == "zoho_payment_failed"]
    assert failed and failed[-1]["text"] == dw.ZOHO_WRITE_FAILED
    # The raw tool text is nowhere the owner can read.
    body = str(_approval_row(env, apr)) + str(_owner_texts(env))
    assert "SUPERSECRET" not in body and "boom" not in body
    assert _approval_row(env, apr)["status"] == "pending"

    # Retry: the same approval goes through, exactly one payment, same reference.
    live.raise_create = False
    ok = env["c"].post(f"/v1/approvals/{apr}/approve", headers=h())
    assert ok.status_code == 200, ok.text
    again = ok.json()["approval"]
    assert again["status"] == "approved"
    assert again["result"]["zoho_payment_id"].startswith("zp-live-")
    assert len(live.payments) == 1
    (payment,) = live.payments.values()
    assert payment["reference_number"] == zp.reference_for("", apr) == f"mimi-{apr}"
    done = [m for m in _owner_texts(env) if m["kind"] == "zoho_payment_recorded"]
    assert len(done) == 1
    # A third tap is refused: it is already approved.
    third = env["c"].post(f"/v1/approvals/{apr}/approve", headers=h())
    assert third.status_code == 409
    assert len(live.payments) == 1


def test_landed_but_errored_write_is_found_on_retry_never_written_twice(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _live_env(monkeypatch)
    _seed_live_connection(env["engine"])
    live = FlakyZoho()
    live.land_then_raise = True
    _live_transport(monkeypatch, live)
    approval = _approve_zoho(env)
    apr = approval["approval_id"]
    assert approval["status"] == "pending"
    assert approval["result"]["error"] == dw.ZOHO_WRITE_FAILED
    assert len(live.payments) == 1  # it landed, though the attempt could not tell
    live.land_then_raise = False
    ok = env["c"].post(f"/v1/approvals/{apr}/approve", headers=h())
    assert ok.status_code == 200, ok.text
    again = ok.json()["approval"]
    assert again["status"] == "approved"
    assert len(live.payments) == 1 and live.creates == 1  # read back, not re-created
    assert again["result"]["zoho_payment_id"] == next(iter(live.payments))


def test_rejecting_a_failed_write_still_works(
        env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _live_env(monkeypatch)
    _seed_live_connection(env["engine"])
    live = FlakyZoho()
    live.raise_create = True
    _live_transport(monkeypatch, live)
    apr = _approve_zoho(env)["approval_id"]
    r = env["c"].post(f"/v1/approvals/{apr}/reject", headers=h())
    assert r.status_code == 200, r.text
    assert _approval_row(env, apr)["status"] == "rejected"
    assert live.payments == {}


# ------------------------------------------------------------ plain errors


class _Boom:
    def __getattr__(self, name: str) -> Any:
        return self

    def __call__(self, *a: Any, **kw: Any) -> Any:
        raise RuntimeError(RAW)


def _transport(monkeypatch: pytest.MonkeyPatch) -> ComposioTransport:
    t = ComposioTransport(ConnectorSettings.from_env({
        "MIMI_CONNECTORS": "composio", "COMPOSIO_API_KEY": "ck_x",  # pragma: allowlist secret
        "MIMI_COMPOSIO_AUTH_ZOHO_BOOKS": "ac_x",
        "MIMI_COMPOSIO_CALLBACK_URL": "https://demo.userail.money/settings/connectors"}))
    monkeypatch.setattr(t, "_sdk", lambda: SimpleNamespace(
        tools=_Boom(), connected_accounts=_Boom(), _client=_Boom()))
    monkeypatch.setattr(t, "_require_version", lambda *a, **kw: None)
    return t


def test_transport_errors_are_plain_and_logged_redacted(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    t = _transport(monkeypatch)
    caplog.set_level(logging.WARNING)
    from mimi.connectors import allowlist

    read = next(a.action for a in allowlist.ACTIONS
                if a.kind == "read" and a.toolkit == "ZOHO_BOOKS")
    calls = [
        lambda: t.execute(read, user_id="u", connected_account_id="ca", arguments={}),
        lambda: t.initiate("ZOHO_BOOKS", user_id="u"),
        lambda: t.disconnect("ca"),
        lambda: zp._live_proxy(t, "customer_payment_create", method="POST",
                               endpoint="https://www.zohoapis.com/books/v3/x",
                               connected_account_id="ca", body={}, user_id="u"),
    ]
    for call in calls:
        with pytest.raises(ConnectorError) as err:
            call()
        assert "boom" not in str(err.value) and "SUPERSECRET" not in str(err.value)
    logged = caplog.text
    assert "RuntimeError" in logged and "boom" in logged  # detail kept for us
    assert "SUPERSECRET" not in logged  # but never the secret


def test_redact_scrubs_keys_and_env_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPOSIO_API_KEY", "plainvalue-xyz")  # pragma: allowlist secret
    out = redact("key plainvalue-xyz Bearer abc.def token=zzz ak_abcdefgh1234 fine")
    assert "plainvalue-xyz" not in out and "abc.def" not in out
    assert "zzz" not in out and "ak_abcdefgh1234" not in out and "fine" in out


def test_connector_routes_answer_plain_502_and_503(
        tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from mimi.api import app as app_mod
    from tests.test_connectors_api import _auth, _client

    c = _client(tmp_path)
    # Composio on but no sign-in app: a plain 503, no env names.
    monkeypatch.setenv("MIMI_CONNECTORS", "composio")
    monkeypatch.setenv("COMPOSIO_API_KEY", "ck_x")  # pragma: allowlist secret
    monkeypatch.delenv("MIMI_COMPOSIO_AUTH_ZOHO_BOOKS", raising=False)
    r = c.post("/v1/connectors/zoho_books/connect", headers=_auth(), json={})
    assert r.status_code == 503
    assert "MIMI_" not in r.json()["detail"] and "isn't set up" in r.json()["detail"]
    # The tool fails: a plain 502, the raw text stays out.
    monkeypatch.setenv("MIMI_COMPOSIO_AUTH_ZOHO_BOOKS", "ac_x")
    monkeypatch.setenv("MIMI_COMPOSIO_CALLBACK_URL", "https://demo.userail.money/s")

    class Failing:
        def initiate(self, *a: Any, **kw: Any) -> Any:
            raise ConnectorError(RAW)

    monkeypatch.setattr(app_mod, "_connector_transport", lambda: Failing())
    r = c.post("/v1/connectors/zoho_books/connect", headers=_auth(), json={})
    assert r.status_code == 502
    assert r.json()["detail"] == app_mod.CONNECT_FAILED
    assert "boom" not in r.text and "SUPERSECRET" not in r.text

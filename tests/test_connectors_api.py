"""Connector endpoints (RAI-277): connect, callback, status, disconnect."""

from __future__ import annotations

from fastapi.testclient import TestClient

from mimi.api.app import create_app
from mimi.api.config import ApiKey, Settings, key_digest
from mimi.connectors.state import sign_state
from mimi.store import seed_world
from mimi.store.db import ensure_schema, make_engine
from mimi.world.loader import load_world

TENANT = "coral-lane"
OWNER = "local-owner-key-0001"


def _client(tmp_path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'api.db'}"
    e = make_engine(url)
    ensure_schema(e)
    seed_world(e, load_world())
    settings = Settings(
        database_url=url,
        keys=(ApiKey(TENANT, "owner", key_digest(OWNER)),),
        cors_origins=(), has_model_key=False)
    from mimi.api.agent import Agent
    from mimi.api.offline import OfflineModel

    agent = Agent(e, models=[OfflineModel()])
    app = create_app(settings, agent, workers=0)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER}"}


def test_connectors_list_starts_empty(tmp_path) -> None:
    r = _client(tmp_path).get("/v1/connectors", headers=_auth())
    assert r.status_code == 200
    toolkits = {c["toolkit"] for c in r.json()["connectors"]}
    assert {"ZOHO_BOOKS", "ZOHO", "GOOGLESHEETS", "CSV", "MONO"} <= toolkits


def test_connect_callback_status_disconnect(tmp_path) -> None:
    c = _client(tmp_path)
    r = c.post("/v1/connectors/ZOHO_BOOKS/connect", headers=_auth(), json={"region": ""})
    assert r.status_code == 200 and r.json()["redirect_url"].startswith("https://")
    state = r.json()["state"]
    r = c.get("/v1/connectors/callback",
              params={"state": state, "connected_account_id": "ca-test-000001"},
              headers=_auth())
    assert r.status_code == 200
    connection_id = r.json()["connection_id"]
    r = c.get(f"/v1/connectors/{connection_id}", headers=_auth())
    assert r.status_code == 200 and r.json()["status"] == "active"
    # Reusing the link is refused.
    r = c.get("/v1/connectors/callback",
              params={"state": state, "connected_account_id": "ca-test-000001"},
              headers=_auth())
    assert r.status_code == 422
    r = c.delete(f"/v1/connectors/{connection_id}", headers=_auth())
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    r = c.get(f"/v1/connectors/{connection_id}", headers=_auth())
    assert r.json()["status"] == "revoked"


def test_callback_rejects_expired_state(tmp_path) -> None:
    c = _client(tmp_path)
    stale = sign_state("local-connector-state-secret",
                       {"tenant": TENANT, "toolkit": "ZOHO_BOOKS"}, now=1)
    r = c.get("/v1/connectors/callback",
              params={"state": stale, "connected_account_id": "ca-x"},
              headers=_auth())
    assert r.status_code == 422


def test_connect_rejects_unknown_toolkit_and_bad_region(tmp_path) -> None:
    c = _client(tmp_path)
    assert c.post("/v1/connectors/QUICKBOOKS/connect",
                  headers=_auth(), json={}).status_code == 404
    r = c.post("/v1/connectors/ZOHO_BOOKS/connect",
               headers=_auth(), json={"region": "xx"})
    assert r.status_code == 422


def test_callback_rejects_a_state_signed_for_another_tenant(tmp_path) -> None:
    c = _client(tmp_path)
    other = sign_state("local-connector-state-secret",
                       {"tenant": "another-distributor", "toolkit": "ZOHO_BOOKS"})
    r = c.get("/v1/connectors/callback",
              params={"state": other, "connected_account_id": "ca-other-000001"},
              headers=_auth())
    assert r.status_code == 422


def test_list_says_connect_is_not_available_without_a_composio_key(tmp_path, monkeypatch) -> None:
    for var in ("MIMI_CONNECTORS", "COMPOSIO_API_KEY", "MIMI_COMPOSIO_AUTH_ZOHO_BOOKS",
                "MIMI_COMPOSIO_CALLBACK_URL"):
        monkeypatch.delenv(var, raising=False)
    rows = {c["toolkit"]: c for c in _client(tmp_path).get(
        "/v1/connectors", headers=_auth()).json()["connectors"]}
    assert rows["ZOHO_BOOKS"]["connect"] == {
        "available": False, "reason": "Connecting tools isn't switched on for this server yet."}
    assert rows["CSV"]["connect"]["available"] is False


def test_list_says_connect_is_available_when_composio_is_fully_set(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MIMI_CONNECTORS", "composio")
    monkeypatch.setenv("COMPOSIO_API_KEY", "ck_test")
    monkeypatch.setenv("MIMI_COMPOSIO_AUTH_ZOHO_BOOKS", "ac_zoho_books")
    monkeypatch.setenv("MIMI_COMPOSIO_CALLBACK_URL", "https://demo.userail.money/settings/connectors")
    rows = {c["toolkit"]: c for c in _client(tmp_path).get(
        "/v1/connectors", headers=_auth()).json()["connectors"]}
    assert rows["ZOHO_BOOKS"]["connect"] == {"available": True, "reason": ""}
    # Sheets has no auth config set: it says so, plainly.
    assert rows["GOOGLESHEETS"]["connect"]["available"] is False
    assert "sign-in app" in rows["GOOGLESHEETS"]["connect"]["reason"]


def test_initiate_puts_the_state_on_the_callback_url() -> None:
    from mimi.connectors.composio import ComposioTransport
    from mimi.connectors.env import ConnectorSettings

    seen: dict[str, str] = {}

    class _Accounts:
        def initiate(self, *, user_id: str, auth_config_id: str, callback_url: str):
            seen.update(user_id=user_id, auth=auth_config_id, callback=callback_url)
            return type("R", (), {"redirect_url": "https://backend.composio.dev/s/abc"})()

    t = ComposioTransport(ConnectorSettings(
        mode="composio", composio_api_key="ck", auth_zoho_books="ac_1",
        callback_url="https://demo.userail.money/settings/connectors"))
    t._client = type("C", (), {"connected_accounts": _Accounts()})()
    out = t.initiate("ZOHO_BOOKS", user_id=TENANT)
    assert out["redirect_url"] == "https://backend.composio.dev/s/abc"
    assert seen["callback"].startswith("https://demo.userail.money/settings/connectors?state=")
    assert seen["callback"].endswith(out["state"])


def test_callback_refuses_a_failed_consent_and_reads_either_id_spelling(tmp_path) -> None:
    c = _client(tmp_path)
    state = c.post("/v1/connectors/ZOHO_BOOKS/connect", headers=_auth(), json={}).json()["state"]
    r = c.get("/v1/connectors/callback",
              params={"state": state, "status": "failed", "connectedAccountId": "ca-1"},
              headers=_auth())
    assert r.status_code == 422
    r = c.get("/v1/connectors/callback",
              params={"state": state, "status": "success", "connectedAccountId": "ca-test-000002"},
              headers=_auth())
    assert r.status_code == 200


def test_read_reads_live_from_the_connected_account(tmp_path) -> None:
    c = _client(tmp_path)
    state = c.post("/v1/connectors/ZOHO_BOOKS/connect", headers=_auth(), json={}).json()["state"]
    connection_id = c.get("/v1/connectors/callback",
                          params={"state": state, "connected_account_id": "ca-test-000003"},
                          headers=_auth()).json()["connection_id"]
    r = c.get(f"/v1/connectors/{connection_id}/read", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["toolkit"] == "ZOHO_BOOKS"
    assert body["summary"].startswith("Read ")
    assert isinstance(body["items"], list)
    # A revoked connection is not read.
    c.delete(f"/v1/connectors/{connection_id}", headers=_auth())
    assert c.get(f"/v1/connectors/{connection_id}/read", headers=_auth()).status_code == 404


def test_cors_allows_delete_for_disconnect(tmp_path) -> None:
    r = _client(tmp_path).options("/v1/connectors/conn-x", headers={
        "Origin": "http://localhost:8787", "Access-Control-Request-Method": "DELETE"})
    assert "DELETE" in r.headers.get("access-control-allow-methods", "")

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

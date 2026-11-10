"""The identity seam and territory scoping through the HTTP API (slice 1).

What this file proves:

* ``GET /v1/me`` names the person, their role and their ground;
* a supervisor sees only their territories, and asking for any id outside
  them answers 404 on **every** scoped route (a table, not one sample);
* a supervisor may decide a money approval for a shop in their ground and the
  row records who decided, what they decided and on what;
* a credit limit is the owner's call: a supervisor inside their own territory
  gets a sentence, not a limit change;
* the dev actor header is refused when the switch says production, and the
  identity provider's token is the way in there.

The seeded book is the synthetic Coral Lane world (``mimi/world``).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.harness.gateway import ScriptedModel
from mimi.identity import auth as ident_auth
from mimi.identity import store
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.world.loader import load_world
from tests.test_api import script

A = "coral-lane"
OWNER_KEY = "identity-owner-key-001"
CHAN_KEY = "identity-chan-key-0001"
W = load_world()


def _shop_in(area: str, *, same: bool) -> Any:
    for shop in W.shops:
        if (shop.area == area) == same:
            return shop
    raise AssertionError(f"no shop {'in' if same else 'outside'} {area}")


@pytest.fixture()
def env(store_url: str) -> Iterator[dict[str, Any]]:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    home = W.shops[0].area  # where the supervisor's territory is
    with engine.begin() as conn:
        # A supervisor with one territory, and the seeded rep for that area.
        sup = store.create_user(conn, A, role="supervisor", name="Sup A",
                                phone="+2348000000777", status="active")
        store.set_territories(conn, A, sup.user_id, ["  " + home.upper() + "  "])
        rep = store.find_by_phone(conn, A, W.reps[0].phone)
        assert rep is not None
        disabled = store.create_user(conn, A, role="rep", name="Gone", phone="+2348000000778",
                                     status="disabled", created_by_user_id=sup.user_id)
        invitee = store.create_user(conn, A, role="rep", name="Not Yet",
                                    phone="+2348000000779", status="invited",
                                    created_by_user_id=sup.user_id)
    agent = Agent(engine, models=[ScriptedModel(script)], worker_id="test-worker")
    keys = parse_keys(f"{A}:owner:{OWNER_KEY},{A}:channel:{CHAN_KEY}")
    app = create_app(Settings(keys=keys, wait_s=5), agent, workers=0)
    with TestClient(app) as client:
        with engine.connect() as conn:
            owners = [u for u in store.list_users(conn, A) if u.role == "owner"]
        assert len(owners) == 1  # the seeded bootstrap owner
        yield {"c": client, "engine": engine, "home": home, "sup": sup, "rep": rep,
               "disabled": disabled, "invitee": invitee, "owner_user": owners[0]}
    engine.dispose()


def owner() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER_KEY}"}


def as_user(user_id: str) -> dict[str, str]:
    return {**owner(), ident_auth.DEV_ACTOR_HEADER: user_id}


def _approval(engine: Any, approval_id: str, shop_id: str, *, tool: str,
              intent_kind: str) -> None:
    with engine.begin() as conn:
        conn.execute(s.approvals.insert().values(
            approval_id=approval_id, tenant_id=A, idempotency_key=f"ik_{approval_id}",
            turn_id="", thread_id="", helper="", skill="", shop_id=shop_id, tool=tool,
            intent_kind=intent_kind, preview="", payload="{}", status="pending",
            decided_by="", note="", created_at="2026-11-10T08:00:00+00:00",
            created_ts=time.time()))


# --------------------------------------------------------------- the caller

def test_me_names_the_person_and_their_ground(env: dict[str, Any]) -> None:
    body = env["c"].get("/v1/me", headers=as_user(env["sup"].user_id)).json()
    assert body["user"]["user_id"] == env["sup"].user_id
    assert body["role"] == "supervisor" and body["source"] == "dev-actor"
    assert body["scopes"]["areas"] == [env["home"].strip().casefold()]
    assert body["scopes"]["all"] is False and body["scopes"]["empty"] is False
    assert body["channels"] == []  # until slice 2


def test_the_rep_sees_their_own_beat(env: dict[str, Any]) -> None:
    body = env["c"].get("/v1/me", headers=as_user(env["rep"].user_id)).json()
    assert body["role"] == "rep"
    assert body["scopes"]["rep_ids"] == [W.reps[0].rep_id]
    assert body["scopes"]["cover_by"] == "rep"


def test_an_unknown_disabled_or_unactivated_caller_is_refused(env: dict[str, Any]) -> None:
    c = env["c"]
    assert c.get("/v1/me", headers=as_user("u_nobody")).status_code == 401
    assert c.get("/v1/me", headers=as_user(env["disabled"].user_id)).status_code == 403
    assert c.get("/v1/me", headers=as_user(env["invitee"].user_id)).status_code == 403


def test_the_dev_header_is_refused_when_the_switch_says_production(store_url: str) -> None:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    with engine.begin() as conn:
        sup = store.create_user(conn, A, role="supervisor", name="Sup", phone="+2348000000888",
                                status="active")
    keys = parse_keys(f"{A}:owner:{OWNER_KEY}")
    app = create_app(Settings(keys=keys, wait_s=5,
                              auth=ident_auth.AuthConfig(mode="dev", dev_actor=False)),
                     Agent(engine, models=[ScriptedModel(script)], worker_id="t"), workers=0)
    with TestClient(app) as c:
        r = c.get("/v1/me", headers=as_user(sup.user_id))
        assert r.status_code == 401 and "dev actor" in r.json()["detail"]
    engine.dispose()


# ------------------------------------------------------------- the ground

def test_a_supervisor_sees_only_their_territory(env: dict[str, Any]) -> None:
    c = env["c"]
    mine = c.get("/v1/shops", headers=as_user(env["sup"].user_id)).json()
    everything = c.get("/v1/shops", headers=owner()).json()
    assert 0 < mine["total"] < everything["total"]
    assert {s_["area"] for s_ in mine["shops"]} == {env["home"]}
    inside = _shop_in(env["home"], same=True)
    assert c.get(f"/v1/shops/{inside.shop_id}",
                 headers=as_user(env["sup"].user_id)).status_code == 200


# A table over every route that names a shop: outside the caller's ground is
# the same answer as an unknown id, so an id cannot be probed.
def scoped_routes(env: dict[str, Any]) -> list[tuple[str, str, str, int]]:
    inside = _shop_in(env["home"], same=True)
    outside = _shop_in(env["home"], same=False)
    return [
        ("GET", f"/v1/shops/{inside.shop_id}", f"/v1/shops/{outside.shop_id}", 404),
        ("POST", f"/v1/credit/{inside.shop_id}/limit",
         f"/v1/credit/{outside.shop_id}/limit", 404),
        ("POST", "/v1/approvals/apr_in/approve", "/v1/approvals/apr_out/approve", 404),
        ("POST", "/v1/approvals/apr_in/reject", "/v1/approvals/apr_out/reject", 404),
    ]


def test_every_scoped_route_answers_404_outside_the_territory(env: dict[str, Any]) -> None:
    inside = _shop_in(env["home"], same=True)
    outside = _shop_in(env["home"], same=False)
    _approval(env["engine"], "apr_in", inside.shop_id, tool="zoho_payment",
              intent_kind="zoho_payment")
    _approval(env["engine"], "apr_out", outside.shop_id, tool="zoho_payment",
              intent_kind="zoho_payment")
    headers = as_user(env["sup"].user_id)
    body = {"amount_ngn": 5_000, "reason": "test"}
    for method, _inside_path, outside_path, expected in scoped_routes(env):
        kw = {"json": body} if method == "POST" and "credit" in outside_path else {}
        r = env["c"].request(method, outside_path, headers=headers, **kw)
        assert r.status_code == expected, (method, outside_path, r.status_code, r.text)
    # And the same routes do reach the shop inside the ground: the 404 is the
    # scope, not a broken route.
    assert env["c"].get(f"/v1/shops/{inside.shop_id}",
                        headers=headers).status_code == 200
    r = env["c"].post("/v1/approvals/apr_in/reject", headers=headers)
    assert r.status_code == 200, r.text


def test_the_approvals_list_is_filtered_to_the_ground(env: dict[str, Any]) -> None:
    inside = _shop_in(env["home"], same=True)
    outside = _shop_in(env["home"], same=False)
    _approval(env["engine"], "apr_in", inside.shop_id, tool="zoho_payment",
              intent_kind="zoho_payment")
    _approval(env["engine"], "apr_out", outside.shop_id, tool="zoho_payment",
              intent_kind="zoho_payment")
    mine = env["c"].get("/v1/approvals", headers=as_user(env["sup"].user_id)).json()
    ids = {a["approval_id"] for a in mine["approvals"]}
    assert "apr_in" in ids and "apr_out" not in ids
    assert env["c"].get("/v1/approvals", headers=owner()).json()["total"] >= 2


# ------------------------------------------------------------- approvals

def test_a_supervisor_decides_money_for_their_own_rep_and_the_row_says_who(
        env: dict[str, Any]) -> None:
    inside = _shop_in(env["home"], same=True)
    _approval(env["engine"], "apr_money", inside.shop_id, tool="zoho_payment",
              intent_kind="zoho_payment")
    r = env["c"].post("/v1/approvals/apr_money/reject", headers=as_user(env["sup"].user_id),
                      json={"note": "not this one", "by": "someone"})
    assert r.status_code == 200, r.text
    with env["engine"].connect() as conn:
        row = conn.execute(sa.select(s.approvals).where(
            s.approvals.c.approval_id == "apr_money")).one()
    assert row.status == "rejected"
    assert row.decided_by == env["sup"].user_id  # the person, not the key or the body
    assert row.note == "not this one"


def test_a_rep_cannot_decide_an_approval(env: dict[str, Any]) -> None:
    inside = _shop_in(env["home"], same=True)
    _approval(env["engine"], "apr_rep", inside.shop_id, tool="send_reminder",
              intent_kind="collector.send_reminder")
    r = env["c"].post("/v1/approvals/apr_rep/reject", headers=as_user(env["rep"].user_id))
    assert r.status_code == 403 and "not yours to decide" in r.json()["detail"]


# ---------------------------------------------------------- credit limits

def test_no_role_but_the_owner_changes_a_credit_limit(env: dict[str, Any]) -> None:
    inside = _shop_in(env["home"], same=True)
    body = {"amount_ngn": 250_000, "reason": "test"}
    for who in ("sup", "rep"):
        r = env["c"].post(f"/v1/credit/{inside.shop_id}/limit", json=body,
                          headers=as_user(env[who].user_id))
        assert r.status_code == 403, (who, r.text)
        assert "owner's call" in r.json()["detail"]
    ok = env["c"].post(f"/v1/credit/{inside.shop_id}/limit", json=body, headers=owner())
    assert ok.status_code == 200, ok.text


# ------------------------------------------------------------------ idp

def test_a_provider_token_names_the_caller_instead_of_the_header(store_url: str) -> None:
    from tests.test_identity import TEST_KEY

    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    with engine.begin() as conn:
        sup = store.create_user(conn, A, role="supervisor", name="Sup", phone="+2348000000999",
                                status="active")
        store.link_subject(conn, A, sup.user_id, "subject-sup")
        store.set_territories(conn, A, sup.user_id, [W.shops[0].area])
    keys = parse_keys(f"{A}:owner:{OWNER_KEY}")
    stytch = ident_auth.AuthConfig(mode="stytch", dev_actor=False,
                                   jwks_url="https://idp.test/keys",
                                   issuer="https://idp.test", audience="mimi-console")
    app = create_app(Settings(keys=keys, wait_s=5, auth=stytch),
                     Agent(engine, models=[ScriptedModel(script)], worker_id="t"), workers=0)
    app.state.identity.verifier = TEST_KEY.verifier(issuer=stytch.issuer, audience=stytch.audience)
    with TestClient(app) as c:
        # A claim of "owner" buys nothing: the role comes from the users row.
        token = TEST_KEY.token({"sub": "subject-sup", "tenant_id": A, "role": "owner",
                                "exp": time.time() + 600})
        body = c.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).json()
        assert body["role"] == "supervisor" and body["source"] == "stytch"
        assert body["user"]["user_id"] == sup.user_id

        expired = TEST_KEY.token({"sub": "subject-sup", "tenant_id": A,
                                  "exp": time.time() - 600})
        assert c.get("/v1/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
        stranger = TEST_KEY.token({"sub": "nobody", "tenant_id": A, "exp": time.time() + 600})
        assert c.get("/v1/me",
                     headers={"Authorization": f"Bearer {stranger}"}).status_code == 401
        # The dev header is not a way in here either.
        r = c.get("/v1/me", headers={**owner(), ident_auth.DEV_ACTOR_HEADER: sup.user_id})
        assert r.status_code == 401 and "dev actor" in r.json()["detail"]
    engine.dispose()


# --------------------------------------------------------------- members

def test_the_owner_adds_a_person_and_the_row_says_who_invited_them(
        env: dict[str, Any]) -> None:
    c = env["c"]
    r = c.post("/v1/users", headers=as_user(env["owner_user"].user_id),
               json={"role": "supervisor", "name": "Bola Adeyemi",
                     "phone": "+234 803 000 5555"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"] == "supervisor" and body["status"] == "invited"
    assert body["phone"] == "+2348030005555"  # stored in one shape
    assert body["created_by_user_id"] == env["owner_user"].user_id
    listed = c.get("/v1/users", headers=as_user(env["owner_user"].user_id)).json()
    assert body["user_id"] in {u["user_id"] for u in listed["users"]}
    assert listed["total"] == len(listed["users"])


def test_only_the_owner_manages_members(env: dict[str, Any]) -> None:
    c = env["c"]
    for who in ("sup", "rep", "invitee"):
        headers = as_user(env[who].user_id)
        assert c.get("/v1/users", headers=headers).status_code == 403, who
        r = c.post("/v1/users", headers=headers,
                   json={"role": "rep", "name": "Sneaky", "phone": "+2348030006666"})
        assert r.status_code == 403, (who, r.text)


def test_an_invite_that_cannot_be_one_is_refused_with_a_sentence(
        env: dict[str, Any]) -> None:
    c = env["c"]
    headers = as_user(env["owner_user"].user_id)
    # No phone and no email: nobody could ever sign in as them.
    r = c.post("/v1/users", headers=headers, json={"role": "rep", "name": "No Contact"})
    assert r.status_code == 422 and "email" in r.json()["detail"]
    # A second owner, and a phone already in use: both are conflicts.
    r = c.post("/v1/users", headers=headers,
               json={"role": "owner", "name": "Second Owner", "phone": "+2348030007777"})
    assert r.status_code == 409 and "owner" in r.json()["detail"]
    r = c.post("/v1/users", headers=headers,
               json={"role": "rep", "name": "Clash", "phone": env["sup"].phone_e164})
    assert r.status_code == 409 and "phone" in r.json()["detail"]


def test_a_role_or_status_changes_but_the_last_owner_stays(env: dict[str, Any]) -> None:
    c = env["c"]
    headers = as_user(env["owner_user"].user_id)
    r = c.patch(f"/v1/users/{env['invitee'].user_id}", headers=headers,
                json={"status": "active"})
    assert r.status_code == 200 and r.json()["status"] == "active"
    r = c.patch(f"/v1/users/{env['rep'].user_id}", headers=headers, json={"role": "supervisor"})
    assert r.status_code == 200 and r.json()["role"] == "supervisor"
    r = c.patch(f"/v1/users/{env['owner_user'].user_id}", headers=headers,
                json={"status": "disabled"})
    assert r.status_code == 422 and "owner" in r.json()["detail"]
    assert c.patch("/v1/users/u_missing", headers=headers,
                   json={"role": "rep"}).status_code == 404


def test_territories_arrive_normalised_and_narrow_the_ground(env: dict[str, Any]) -> None:
    c = env["c"]
    headers = as_user(env["owner_user"].user_id)
    shop = _shop_in(env["home"], same=True)
    areas = [f"  {env['home'].upper()}  ", "  ", "Oke-Ila"]
    r = c.put(f"/v1/users/{env['sup'].user_id}/territories", headers=headers,
              json={"areas": areas})
    assert r.status_code == 200, r.text
    assert r.json()["areas"] == sorted({env["home"].strip().casefold(), "oke-ila"})
    me = c.get("/v1/me", headers=as_user(env["sup"].user_id)).json()
    assert env["home"].strip().casefold() in me["scopes"]["areas"]
    assert c.get(f"/v1/shops/{shop.shop_id}",
                 headers=as_user(env["sup"].user_id)).status_code == 200
    # Only a supervisor carries ground; a rep's beat comes from the book.
    r = c.put(f"/v1/users/{env['rep'].user_id}/territories", headers=headers,
              json={"areas": ["Oke-Ila"]})
    assert r.status_code == 422 and "supervisor" in r.json()["detail"]


def test_a_verified_login_with_no_row_is_refused_and_creates_nothing(
        store_url: str) -> None:
    """No just-in-time provisioning, through the API: an uninvited person is
    told to ask the owner, and no user row appears."""
    from tests.test_identity import TEST_KEY

    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    keys = parse_keys(f"{A}:owner:{OWNER_KEY}")
    stytch = ident_auth.AuthConfig(mode="stytch", dev_actor=False,
                                   jwks_url="https://idp.test/keys",
                                   issuer="https://idp.test", audience="mimi-console")
    app = create_app(Settings(keys=keys, wait_s=5, auth=stytch),
                     Agent(engine, models=[ScriptedModel(script)], worker_id="t"), workers=0)
    app.state.identity.verifier = TEST_KEY.verifier(issuer=stytch.issuer,
                                                    audience=stytch.audience)
    with engine.connect() as conn:
        before = len(store.list_users(conn, A))
    with TestClient(app) as c:
        token = TEST_KEY.token({"sub": "stytch-stranger", "tenant_id": A,
                                "email": "stranger@example.test", "exp": time.time() + 600})
        r = c.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
        assert "has not been invited" in r.json()["detail"]
    with engine.connect() as conn:
        assert len(store.list_users(conn, A)) == before
    engine.dispose()

"""Identity, roles and scopes (Phase 1 slice 1).

Three things are proven here and nothing else:

* the ``users`` rules a table cannot hold: one owner per tenant, one row per
  phone or email, somebody named as the creator, and a link between a person
  and their beat that survives a country code or a stray case in an area name;
* the permission matrix, as a table over **every** (role, action) pair, with
  an expected table written out separately from ``roles.MATRIX`` so the test
  can disagree with the code;
* the seam: a token's ``role`` claim cannot buy a role, and a token that is
  not signed by the provider's key is not a caller.

The token half builds a throwaway RSA key in memory. It is never written to a
file and it is not a secret: it exists for the length of the test run.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.clock import wall
from mimi.identity import auth as ident_auth
from mimi.identity import roles, store
from mimi.identity.areas import area_key, area_keys
from mimi.identity.jwt import JwksVerifier, JwtError, b64url_encode, rsa_verify
from mimi.identity.phones import same_phone
from mimi.identity.scopes import ALL, NONE, scopes_for
from mimi.store import make_engine
from mimi.store import schema as s
from mimi.store.db import ensure_schema

T = "coral-lane"


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[Any]:
    engine = make_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    ensure_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def book(db: Any) -> dict[str, Any]:
    """A tiny, exact book: two areas, one rep each, one shop each.

    Deliberately not the synthetic world: its ``reps.areas`` disagree with the
    areas of the shops each rep serves, which makes an expected table a
    statement about the fixture instead of about the rule."""
    with db.begin() as conn:
        conn.execute(s.reps.insert(), [
            {"tenant_id": T, "rep_id": "rep-1", "name": "Rep One",
             "phone": "+2348000000001", "areas": '["area a"]'},
            {"tenant_id": T, "rep_id": "rep-2", "name": "Rep Two",
             "phone": "+2348000000002", "areas": '["area b"]'},
        ])
        conn.execute(s.shops.insert(), [
            {"tenant_id": T, "shop_id": "shop-a", "name": "Shop A", "area": "Area A",
             "phone": "+2348000000011", "rep_id": "rep-1", "credit_limit_kobo": 0},
            {"tenant_id": T, "shop_id": "shop-b", "name": "Shop B", "area": "Area B",
             "phone": "+2348000000012", "rep_id": "rep-2", "credit_limit_kobo": 0},
        ])
        owner = store.create_user(conn, T, role="owner", name="Owner",
                                  phone="+2348000000009", status="active")
        sup = store.create_user(conn, T, role="supervisor", name="Sup A",
                                phone="+2348000000003", status="active",
                                created_by_user_id=owner.user_id)
        rep = store.create_user(conn, T, role="rep", name="Rep One",
                                phone="08000000001", status="active",
                                created_by_user_id=owner.user_id)
        brand = store.create_user(conn, T, role="brand_viewer", name="Brand",
                                  email="brand@coral-lane.example", status="active",
                                  created_by_user_id=owner.user_id)
        store.set_territories(conn, T, sup.user_id, ["  AREA a "])
    return {"engine": db, "owner": owner, "sup": sup, "rep": rep, "brand": brand}


# ------------------------------------------------------------------ users

def test_one_owner_per_tenant_is_enforced_in_code(book: dict[str, Any]) -> None:
    with book["engine"].begin() as conn:
        with pytest.raises(store.OwnerExists, match="already has an owner"):
            store.create_user(conn, T, role="owner", name="Second Owner",
                              phone="+2348000000077", status="active")
        with pytest.raises(store.OwnerExists):
            store.set_role(conn, T, book["sup"].user_id, "owner")
        # Another distributor may of course have its own owner.
        other = store.create_user(conn, "other-co", role="owner", name="Owner 2",
                                  phone="+2348000000078", status="active")
        assert other.role == "owner"


def test_a_phone_or_email_names_one_person(book: dict[str, Any]) -> None:
    with book["engine"].begin() as conn:
        with pytest.raises(store.DuplicateUser):
            store.create_user(conn, T, role="rep", name="Clone", phone="08000000001",
                              status="active")
        with pytest.raises(store.DuplicateUser):
            store.create_user(conn, T, role="rep", name="Clone",
                              email="BRAND@coral-lane.example", status="active")
        # A number typed with the country code is still the same line.
        assert store.find_by_phone(conn, T, "+2348000000001") is not None
        assert store.find_by_phone(conn, T, "08000000001") is not None


def test_every_user_names_who_created_them(book: dict[str, Any]) -> None:
    with book["engine"].begin() as conn:
        owner = store.get_user(conn, T, book["owner"].user_id)
        assert owner is not None and owner.created_by_user_id is None  # the bootstrap row
        assert book["sup"].created_by_user_id == book["owner"].user_id
        with pytest.raises(store.InvalidUser, match="owner or a supervisor"):
            store.create_user(conn, T, role="rep", name="Nobody's",
                              phone="+2348000000088", created_by_user_id=book["rep"].user_id)
        with pytest.raises(store.InvalidUser, match="phone number or an email"):
            store.create_user(conn, T, role="rep", name="No Way In")


def test_the_owner_account_cannot_be_disabled_by_accident(book: dict[str, Any]) -> None:
    with book["engine"].begin() as conn:
        with pytest.raises(store.IdentityError, match="hand the owner role"):
            store.set_status(conn, T, book["owner"].user_id, "disabled")


# ------------------------------------------------------------------ areas

def test_an_area_is_compared_trimmed_and_casefolded(book: dict[str, Any]) -> None:
    assert area_key("  Oja  Oba ") == area_key("oja oba") == "oja oba"
    assert area_keys(["Ikeja", 7, None, " IKEJA "]) == frozenset({"ikeja"})
    with book["engine"].connect() as conn:
        sup = store.get_user(conn, T, book["sup"].user_id)
        assert sup is not None
        scope = scopes_for(conn, sup)
    # Written as "  AREA a ", matched against the shop's "Area A".
    assert scope.covers_shop("Area A", "rep-1")
    assert scope.covers_shop("AREA A ", "rep-2")  # the area itself is the ground
    assert not scope.covers_shop("Area B", "rep-1")


def test_a_phone_matches_with_or_without_the_country_code(book: dict[str, Any]) -> None:
    assert same_phone("+2348000000001", "08000000001")
    assert not same_phone("+2348000000001", "08000000002")
    assert not same_phone("", "")  # an empty number is nobody


# ----------------------------------------------------------------- scopes

def test_a_rep_covers_their_own_beat_and_never_everything(book: dict[str, Any]) -> None:
    with book["engine"].connect() as conn:
        rep = store.get_user(conn, T, book["rep"].user_id)
        assert rep is not None
        scope = scopes_for(conn, rep)
    assert scope.rep_ids == frozenset({"rep-1"}) and scope.cover_by == "rep"
    assert scope.covers_shop("Area A", "rep-1") and not scope.covers_shop("Area B", "rep-2")
    assert not scope.empty


def test_a_rep_with_no_book_row_gets_an_empty_scope_not_everything(book: dict[str, Any]) -> None:
    with book["engine"].begin() as conn:
        ghost = store.create_user(conn, T, role="rep", name="Ghost", phone="+2348000000999",
                                  status="active", created_by_user_id=book["owner"].user_id)
    with book["engine"].connect() as conn:
        scope = scopes_for(conn, store.get_user(conn, T, ghost.user_id))
    assert scope.empty and not scope.global_
    assert not scope.covers_shop("Area A", "rep-1")


def test_a_supervisor_with_no_territory_gets_an_empty_scope(book: dict[str, Any]) -> None:
    with book["engine"].begin() as conn:
        sup = store.create_user(conn, T, role="supervisor", name="Unassigned",
                                phone="+2348000000988", status="active",
                                created_by_user_id=book["owner"].user_id)
    with book["engine"].connect() as conn:
        scope = scopes_for(conn, store.get_user(conn, T, sup.user_id))
    assert scope.empty and not scope.covers_shop("Area A", "rep-1")


def test_only_a_supervisor_has_territories(book: dict[str, Any]) -> None:
    with book["engine"].begin() as conn:
        with pytest.raises(store.IdentityError, match="only a supervisor"):
            store.set_territories(conn, T, book["rep"].user_id, ["Area A"])


def test_the_owners_ground_is_the_whole_tenant(book: dict[str, Any]) -> None:
    with book["engine"].connect() as conn:
        scope = scopes_for(conn, store.get_user(conn, T, book["owner"].user_id))
    assert scope.global_ and not scope.empty
    assert scope.covers_shop("Nowhere", "nobody")


# ------------------------------------------------------- the permission matrix

def _principal(role: str, user_id: str, scope: Any) -> roles.Principal:
    return roles.Principal(tenant_id=T, role=role, user_id=user_id, scope=scope)


@pytest.fixture()
def table(book: dict[str, Any]) -> dict[str, Any]:
    with book["engine"].connect() as conn:
        sup_scope = scopes_for(conn, store.get_user(conn, T, book["sup"].user_id))
        rep_scope = scopes_for(conn, store.get_user(conn, T, book["rep"].user_id))
    sup = _principal("supervisor", book["sup"].user_id, sup_scope)
    rep = _principal("rep", book["rep"].user_id, rep_scope)
    owner = _principal("owner", book["owner"].user_id, ALL)
    brand = _principal("brand_viewer", book["brand"].user_id, NONE)
    who = {"owner": owner, "supervisor": sup, "rep": rep, "brand_viewer": brand}
    mine = roles.Subject(money=False, area="Area A", rep_id="rep-1",
                         assigned_user_id=book["rep"].user_id)
    others = roles.Subject(money=False, area="Area A", rep_id="rep-1",
                           assigned_user_id="u_somebody_else")
    own_money = roles.Subject(money=True, area="Area A", rep_id="rep-1")
    other_money = roles.Subject(money=True, area="Area B", rep_id="rep-2")
    return {"who": who, "subjects": {"mine": mine, "others": others,
                                     "own_money": own_money, "other_money": other_money}}


# Written out by hand from PHASE1-COMMAND.md section 7 and the owner's
# decisions, so this can disagree with ``roles.MATRIX``. Keys are
# (subject, action); each value is the role -> may they.
EXPECTED: dict[tuple[str, str], dict[str, bool]] = {
    # Everyone with a seat sees their own channels; a brand viewer sees the
    # distributor's customer data through none of them.
    ("mine", "view_channel"): {"owner": True, "supervisor": True, "rep": True,
                               "brand_viewer": False},
    ("other_money", "view_channel"): {"owner": True, "supervisor": False, "rep": False,
                                      "brand_viewer": False},
    # A rep may close a non-money item that is theirs, and never a money item.
    ("mine", "close_item"): {"owner": True, "supervisor": True, "rep": True,
                             "brand_viewer": False},
    ("others", "close_item"): {"owner": True, "supervisor": True, "rep": False,
                               "brand_viewer": False},
    ("own_money", "close_item"): {"owner": True, "supervisor": True, "rep": False,
                                  "brand_viewer": False},
    ("other_money", "close_item"): {"owner": True, "supervisor": False, "rep": False,
                                    "brand_viewer": False},
    # The supervisor decides money for their own reps, inside their ground.
    ("own_money", "approve_money"): {"owner": True, "supervisor": True, "rep": False,
                                     "brand_viewer": False},
    ("other_money", "approve_money"): {"owner": True, "supervisor": False, "rep": False,
                                       "brand_viewer": False},
    # A limit is the owner's, whatever the scope.
    ("own_money", "change_credit_limit"): {"owner": True, "supervisor": False, "rep": False,
                                           "brand_viewer": False},
    ("mine", "manage_members"): {"owner": True, "supervisor": False, "rep": False,
                                 "brand_viewer": False},
    ("mine", "manage_settings"): {"owner": True, "supervisor": False, "rep": False,
                                  "brand_viewer": False},
    ("own_money", "undo_write"): {"owner": True, "supervisor": False, "rep": False,
                                  "brand_viewer": False},
}

FUNCS = {
    "view_channel": roles.can_view_channel,
    "close_item": roles.can_close_item,
    "approve_money": roles.can_approve_money,
}


def test_the_matrix_covers_every_role_and_every_action() -> None:
    assert set(roles.MATRIX) == set(roles.ROLES)
    for role in roles.ROLES:
        assert set(roles.MATRIX[role]) == set(roles.ACTIONS), role
        assert set(roles.MATRIX[role].values()) <= {"every", "scope", "own_rep_money",
                                                    "own_non_money", "never"}


@pytest.mark.parametrize("case", sorted(EXPECTED), ids=lambda c: f"{c[1]}-{c[0]}")
def test_every_role_and_action_pair(case: tuple[str, str], table: dict[str, Any]) -> None:
    subject_name, action = case
    subject = table["subjects"][subject_name]
    for role, may in EXPECTED[case].items():
        principal = table["who"][role]
        if action in FUNCS:
            got = FUNCS[action](principal, subject)
        elif action == "change_credit_limit":
            got = roles.can_change_credit_limit(principal)
        elif action == "manage_members":
            got = roles.can_manage_members(principal)
        elif action == "manage_settings":
            got = roles.can_manage_settings(principal)
        elif action == "undo_write":
            got = roles.can_undo_write(principal)
        else:  # pragma: no cover - a new action must be added to EXPECTED
            raise AssertionError(action)
        assert got is may, (role, action, subject_name)


def test_no_role_but_owner_can_change_a_credit_limit(table: dict[str, Any]) -> None:
    for role in ("supervisor", "rep", "brand_viewer"):
        assert not roles.can_change_credit_limit(table["who"][role]), role
    assert roles.can_change_credit_limit(table["who"]["owner"])


def test_a_token_cannot_buy_a_role_by_claiming_one(book: dict[str, Any]) -> None:
    """The role comes from the users row, never from the token."""
    key = TEST_KEY
    token = key.token({"sub": "subject-sup", "tenant_id": T, "role": "owner",
                       "email": "sup-a@coral-lane.example", "exp": wall() + 600})
    with book["engine"].begin() as conn:
        store.link_subject(conn, T, book["sup"].user_id, "subject-sup")
    resolver = ident_auth.Resolver(book["engine"], ident_auth.AuthConfig(mode="stytch",
                                                                        dev_actor=False))
    resolver.verifier = key.verifier()
    principal = resolver.from_token(token)
    assert principal.role == "supervisor"  # not "owner", whatever the claim said
    assert not roles.can_change_credit_limit(principal)
    assert not roles.can_manage_members(principal)
    assert principal.source == "stytch"


def test_money_and_limit_words_are_read_from_code(book: dict[str, Any]) -> None:
    assert roles.is_money("record_payment_proof", "proof.record_payment")
    assert roles.is_money("zoho_payment", "")
    assert not roles.is_money("send_reminder", "collector.send_reminder")
    assert roles.is_credit_limit("credit_over_limit", "credit.review")
    assert not roles.is_credit_limit("send_reminder", "")


# ------------------------------------------------------------- the invites

def _stytch(book: dict[str, Any]) -> ident_auth.Resolver:
    resolver = ident_auth.Resolver(book["engine"],
                                   ident_auth.AuthConfig(mode="stytch", dev_actor=False))
    resolver.verifier = TEST_KEY.verifier()
    return resolver


def _count_users(book: dict[str, Any]) -> int:
    with book["engine"].connect() as conn:
        return int(conn.execute(sa.select(sa.func.count()).select_from(s.users)).scalar())


def test_a_stranger_is_not_given_an_account_just_in_time(book: dict[str, Any]) -> None:
    before = _count_users(book)
    token = TEST_KEY.token({"sub": "stytch-stranger", "email": "stranger@example.test",
                            "phone_number": "+2348000000123", "exp": wall() + 600})
    with pytest.raises(ident_auth.AuthError) as err:
        _stytch(book).from_token(token)
    assert err.value.status == 401
    assert "has not been invited" in err.value.message
    assert _count_users(book) == before  # no row, no account


def test_the_first_login_links_the_invite(book: dict[str, Any]) -> None:
    with book["engine"].begin() as conn:
        invite = store.create_user(conn, T, role="rep", name="New Rep",
                                   email="new.rep@coral-lane.example", status="active",
                                   created_by_user_id=book["owner"].user_id)
    token = TEST_KEY.token({"sub": "stytch-new", "tenant_id": T,
                            "email": "NEW.REP@coral-lane.example", "exp": wall() + 600})
    principal = _stytch(book).from_token(token)
    assert principal.user_id == invite.user_id and principal.role == "rep"
    with book["engine"].connect() as conn:
        row = store.get_user(conn, T, invite.user_id)
    assert row is not None and row.external_subject == "stytch-new"


def test_a_phone_only_invite_links_from_the_sessions_own_claim(book: dict[str, Any]) -> None:
    """Phone is the primary way in for a rep, and Stytch's session claim is
    where its session facts live. Both namespaced shapes are read."""
    with book["engine"].begin() as conn:
        invite = store.create_user(conn, T, role="rep", name="Phone Rep",
                                   phone="+2348030009999", status="active",
                                   created_by_user_id=book["owner"].user_id)
    for claim in ("https://stytch.com/session", "https://stytch.com/b2b/session"):
        token = TEST_KEY.token({"sub": "stytch-phone", "tenant_id": T,
                                claim: {"attributes": {"phone_number": "+2348030009999"}},
                                "exp": wall() + 600})
        principal = _stytch(book).from_token(token)
        assert principal.user_id == invite.user_id
    custom = TEST_KEY.token({"sub": "stytch-mail", "tenant_id": T,
                             "https://stytch.com/session":
                                 {"custom_claims": {"email": "staff@coral-lane.example"}},
                             "exp": wall() + 600})
    with book["engine"].begin() as conn:
        staff = store.create_user(conn, T, role="supervisor", name="Staff",
                                  email="staff@coral-lane.example", status="active",
                                  created_by_user_id=book["owner"].user_id)
    assert _stytch(book).from_token(custom).user_id == staff.user_id


def test_an_already_linked_account_cannot_be_taken_over(book: dict[str, Any]) -> None:
    with book["engine"].begin() as conn:
        store.link_subject(conn, T, book["rep"].user_id, "stytch-original")
    # Someone else who controls the same phone number is not the account.
    token = TEST_KEY.token({"sub": "stytch-attacker", "tenant_id": T,
                            "phone_number": "08000000001", "exp": wall() + 600})
    with pytest.raises(ident_auth.AuthError) as err:
        _stytch(book).from_token(token)
    assert err.value.status == 403
    assert "already linked to a different Stytch sign-in" in err.value.message
    with book["engine"].connect() as conn:
        assert store.get_user(conn, T, book["rep"].user_id).external_subject == "stytch-original"


def test_the_database_refuses_a_user_nobody_can_sign_in_as(book: dict[str, Any]) -> None:
    with book["engine"].begin() as conn, pytest.raises(sa.exc.IntegrityError):
        conn.execute(s.users.insert().values(
            tenant_id=T, user_id="u_nobody", external_subject=None, email=None,
            phone_e164=None, name="Nobody", role="rep", status="active",
            created_at="2026-11-10T08:00:00+00:00", last_seen_at=None,
            created_by_user_id=None))


# ------------------------------------------------------------- the switch

def test_the_seam_is_one_setting_and_never_both_ways(book: dict[str, Any]) -> None:
    with pytest.raises(ident_auth.AuthConfigError, match="MIMI_AUTH_MODE"):
        ident_auth.AuthConfig.from_env({"MIMI_AUTH_MODE": "both"})
    with pytest.raises(ident_auth.AuthConfigError, match="MIMI_STYTCH_JWKS_URL"):
        ident_auth.AuthConfig.from_env({"MIMI_AUTH_MODE": "stytch"})
    with pytest.raises(ident_auth.AuthConfigError, match="development only"):
        ident_auth.AuthConfig.from_env({"MIMI_AUTH_MODE": "dev"}, production=True)
    cfg = ident_auth.AuthConfig.from_env({"MIMI_AUTH_MODE": "stytch",
                                          "MIMI_STYTCH_JWKS_URL": "https://stytch.test/keys",
                                          "MIMI_STYTCH_JWKS_TTL_S": "60"})
    assert cfg.mode == "stytch" and cfg.jwks_ttl_s == 60 and cfg.dev_actor is False

    dev = ident_auth.Resolver(book["engine"], ident_auth.AuthConfig(mode="dev"))
    with pytest.raises(ident_auth.AuthError, match="does not accept Stytch"):
        dev.from_token(TEST_KEY.token({"sub": "x", "exp": wall() + 60}))
    with pytest.raises(ident_auth.AuthError, match="dev actor header"):
        _stytch(book).from_actor(T, book["owner"].user_id)


def test_an_unknown_key_triggers_one_refresh_and_the_cache_has_a_ttl() -> None:
    state = {"kid": "test-kid"}
    calls: list[str] = []

    def fetch(url: str) -> dict[str, Any]:
        calls.append(url)
        doc = TEST_KEY.jwks()
        doc["keys"][0]["kid"] = state["kid"]
        return doc

    verifier = JwksVerifier("https://stytch.test/keys", fetch=fetch, ttl_s=300)
    verifier.verify(TEST_KEY.token({"sub": "a", "exp": wall() + 300}, kid="test-kid"))
    assert len(calls) == 1
    state["kid"] = "rotated"
    verifier.verify(TEST_KEY.token({"sub": "a", "exp": wall() + 300}, kid="rotated"))
    assert len(calls) == 2  # a rotation never waits for the TTL
    # A TTL of zero re-reads on every verification: the other end of the knob.
    fresh = JwksVerifier("https://stytch.test/keys", fetch=fetch, ttl_s=0)
    fresh.verify(TEST_KEY.token({"sub": "a", "exp": wall() + 300}, kid="rotated"))
    fresh.verify(TEST_KEY.token({"sub": "a", "exp": wall() + 300}, kid="rotated"))
    assert len(calls) == 4


# --------------------------------------------------------------- the token

def _prime(bits: int, rng: random.Random) -> int:
    def prime(n: int) -> bool:
        for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
            if n % p == 0:
                return n == p
        d, s = n - 1, 0
        while d % 2 == 0:
            d, s = d // 2, s + 1
        for _ in range(24):
            a = rng.randrange(2, n - 1)
            x = pow(a, d, n)
            if x in (1, n - 1):
                continue
            for _ in range(s - 1):
                x = x * x % n
                if x == n - 1:
                    break
            else:
                return False
        return True

    while True:
        n = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if n % 65537 and prime(n):
            return n


class _TestKey:
    """A throwaway RSA key and a JWKS made from it. Test-only, in memory."""

    def __init__(self) -> None:
        rng = random.Random(20261110)
        p, q = _prime(512, rng), _prime(512, rng)
        self.n = p * q
        self.e = 65537
        self.d = pow(self.e, -1, (p - 1) * (q - 1))
        self.kid = "test-kid"

    def jwks(self) -> dict[str, Any]:
        return {"keys": [{"kty": "RSA", "kid": self.kid,
                          "n": b64url_encode(_int_bytes(self.n)),
                          "e": b64url_encode(_int_bytes(self.e))}]}

    def verifier(self, **kw: Any) -> JwksVerifier:
        return JwksVerifier("https://idp.test/keys", fetch=lambda _url: self.jwks(), **kw)

    def token(self, claims: dict[str, Any], *, kid: str | None = None,
              alg: str = "RS256", mutate: str = "") -> str:
        header = {"alg": alg, "typ": "JWT", "kid": self.kid if kid is None else kid}
        body = {**{"iss": "https://idp.test", "aud": "mimi-console"}, **claims}
        head = b64url_encode(json.dumps(header).encode())
        payload = b64url_encode(json.dumps(body).encode())
        signing_input = f"{head}.{payload}".encode()
        digest = hashlib.sha256(signing_input).digest()
        size = (self.n.bit_length() + 7) // 8
        info = bytes.fromhex("3031300d060960864801650304020105000420") + digest
        em = b"\x00\x01" + b"\xff" * (size - len(info) - 3) + b"\x00" + info
        signature = pow(int.from_bytes(em, "big"), self.d, self.n).to_bytes(size, "big")
        out = f"{head}.{payload}.{b64url_encode(signature)}"
        return out[:-2] + mutate if mutate else out


def _int_bytes(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


TEST_KEY = _TestKey()


def test_the_verifier_accepts_a_token_the_provider_signed() -> None:
    token = TEST_KEY.token({"sub": "u-1", "exp": wall() + 300})
    claims = TEST_KEY.verifier(issuer="https://idp.test", audience="mimi-console").verify(token)
    assert claims.subject == "u-1"


def test_a_tampered_token_is_refused() -> None:
    token = TEST_KEY.token({"sub": "u-1", "exp": wall() + 300})
    head, payload, sig = token.split(".")
    forged = b64url_encode(json.dumps({"sub": "u-2", "exp": wall() + 300,
                                       "iss": "https://idp.test",
                                       "aud": "mimi-console"}).encode())
    with pytest.raises(JwtError, match="signature"):
        TEST_KEY.verifier().verify(f"{head}.{forged}.{sig}")


def test_alg_none_and_hmac_are_refused() -> None:
    for alg in ("none", "HS256"):
        token = TEST_KEY.token({"sub": "u-1", "exp": wall() + 300}, alg=alg)
        with pytest.raises(JwtError, match="not accepted"):
            TEST_KEY.verifier().verify(token)


def test_an_expired_or_wrong_issuer_token_is_refused() -> None:
    expired = TEST_KEY.token({"sub": "u-1", "exp": wall() - 3600})
    with pytest.raises(JwtError, match="expired"):
        TEST_KEY.verifier().verify(expired)
    wrong = TEST_KEY.token({"sub": "u-1", "exp": wall() + 300, "iss": "https://evil.test"})
    with pytest.raises(JwtError, match="issuer"):
        TEST_KEY.verifier(issuer="https://idp.test").verify(wrong)
    wrong_aud = TEST_KEY.token({"sub": "u-1", "exp": wall() + 300, "aud": "someone-else"})
    with pytest.raises(JwtError, match="audience"):
        TEST_KEY.verifier(audience="mimi-console").verify(wrong_aud)


def test_a_token_from_a_key_we_do_not_know_is_refused() -> None:
    token = TEST_KEY.token({"sub": "u-1", "exp": wall() + 300}, kid="other-kid")
    with pytest.raises(JwtError, match="not in the provider"):
        TEST_KEY.verifier().verify(token)


def test_the_signature_check_is_the_rsa_one() -> None:
    token = TEST_KEY.token({"sub": "u-1", "exp": wall() + 1})
    head, payload, sig = token.split(".")
    signature = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    signed = f"{head}.{payload}".encode()
    assert rsa_verify(TEST_KEY.n, TEST_KEY.e, signed, signature, "sha256")
    # The same signature does not verify another message, and a shortened one
    # (the classic "strip a byte" trick) fails the length check.
    assert not rsa_verify(TEST_KEY.n, TEST_KEY.e, b"another message", signature, "sha256")
    assert not rsa_verify(TEST_KEY.n, TEST_KEY.e, signed, signature[:-2], "sha256")

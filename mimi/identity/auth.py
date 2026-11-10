"""The auth seam: one caller object, two ways in, one switch.

``MIMI_AUTH_MODE=dev`` (development, the default): the caller is named by the
``X-Actor-Id`` header, a ``u_...`` id from our own ``users`` table. It is
refused outright in production, so a header can never be a way in there.

``MIMI_AUTH_MODE=stytch`` (production): the caller is named by Stytch's
session JWT, verified against Stytch's JWKS (``mimi.identity.jwt``), and
mapped to our ``users`` row by ``external_subject``, which holds the Stytch
user id. The first successful login of an invited person links their Stytch
id to the row the owner created for them (by the email or phone they verified
with). A verified phone or email with no row is *no account*: nothing is
created just in time, because an uninvited number is how a stranger becomes a
user.

Both paths end at the same ``Principal`` and the same downstream code, so
switching is one setting, and never both at once: ``dev`` refuses a Stytch
token and ``stytch`` refuses the dev header.

Two things never come from a token: the role and the tenant's business data.
The role is read from our ``users`` row. Stytch's token names a subject, not a
distributor, so the tenant comes from the row the subject (or the verified
email/phone that links it) resolves to.

A distributor bearer key (``MIMI_API_KEYS``) is not a person: it still
authenticates *service* calls (the WhatsApp webhook, the simulator). Its role
is ``owner`` (the console before sign-in) or ``channel``. When a person is also
named, the person's row decides what they may do.

No secret is read from a file: the JWKS URL and the expected issuer and
audience come from the environment (``MIMI_STYTCH_*``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.engine import Engine

from mimi.clock import wall
from mimi.identity import store
from mimi.identity.jwt import Claims, JwksVerifier, JwtError
from mimi.identity.roles import Principal
from mimi.identity.scopes import ALL, NONE, Scope, scopes_for

DEV_ACTOR_HEADER = "X-Actor-Id"
MODES = ("dev", "stytch")
DEV_REFUSED = ("the dev actor header is refused in production: send the Stytch "
               "session token instead")
STYTCH_REFUSED = ("this API authenticates with Stytch (MIMI_AUTH_MODE=stytch), "
                  "not with the dev actor header")
ANONYMOUS = "a valid bearer key is required"
# A verified phone or email with no row of ours. Nothing is created just in
# time: an uninvited number is how a stranger becomes a user.
NO_ACCOUNT = ("this account has not been invited: ask the distributor's owner "
              "to add you")
# One write per request is not worth it; a minute is enough for "last seen".
LAST_SEEN_EVERY_S = 60
# How long a fetched JWKS is trusted before it is read again. A short interval
# keeps a revoked Stytch key from being honoured for long; an unknown ``kid``
# refreshes immediately regardless of this, so a rotation never waits.
JWKS_TTL_S = 300


class AuthError(Exception):
    """A caller that cannot be established. ``status`` is what the API answers."""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class AuthConfigError(ValueError):
    """The seam is configured in a way that cannot be right (a bad env value)."""


@dataclass(frozen=True)
class AuthConfig:
    mode: str = "dev"
    dev_actor: bool = True
    jwks_url: str = ""
    issuer: str = ""
    audience: str = ""
    leeway_s: int = 60
    jwks_ttl_s: int = JWKS_TTL_S

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, *,
                 production: bool = False) -> AuthConfig:
        e = env if env is not None else os.environ

        def get(name: str, default: str = "") -> str:
            return (e.get(name, default) or "").strip()

        mode = get("MIMI_AUTH_MODE").lower() or "dev"
        if mode not in MODES:
            raise AuthConfigError(f"MIMI_AUTH_MODE is {mode!r}: use dev or stytch")
        if production and mode == "dev":
            raise AuthConfigError(
                "MIMI_AUTH_MODE=dev is development only: production needs Stytch "
                "(MIMI_AUTH_MODE=stytch and MIMI_STYTCH_JWKS_URL)")
        dev = get("MIMI_DEV_ACTOR", "1").lower() not in ("0", "false", "no")
        jwks = get("MIMI_STYTCH_JWKS_URL")
        if mode == "stytch" and not jwks:
            raise AuthConfigError("MIMI_STYTCH_JWKS_URL is not set: Stytch's key set "
                                  "is needed to verify a session")
        return cls(mode=mode, dev_actor=dev and not production and mode == "dev",
                   jwks_url=jwks,
                   issuer=get("MIMI_STYTCH_ISSUER"), audience=get("MIMI_STYTCH_AUDIENCE"),
                   leeway_s=int(get("MIMI_STYTCH_LEEWAY_S", "60") or 60),
                   jwks_ttl_s=int(get("MIMI_STYTCH_JWKS_TTL_S", str(JWKS_TTL_S))
                                  or JWKS_TTL_S))

    def verifier(self) -> JwksVerifier | None:
        if self.mode != "stytch":
            return None
        return JwksVerifier(self.jwks_url, issuer=self.issuer, audience=self.audience,
                            leeway_s=self.leeway_s, ttl_s=self.jwks_ttl_s)


@dataclass
class Resolver:
    """Resolves one request's caller. One per process; the JWKS cache lives here."""

    engine: Engine
    config: AuthConfig = field(default_factory=AuthConfig)
    verifier: JwksVerifier | None = None

    def __post_init__(self) -> None:
        if self.verifier is None:
            self.verifier = self.config.verifier()

    # ---------------------------------------------------------------- keys
    def from_key(self, tenant_id: str, key_role: str) -> Principal:
        """A distributor bearer key with no person attached."""
        if key_role == "owner":
            return Principal(tenant_id=tenant_id, role="owner", scope=ALL, source="key")
        # A channel key is the WhatsApp webhook or the simulator: no console,
        # no internal channel, no shop list.
        return Principal(tenant_id=tenant_id, role="channel", scope=NONE, source="key")

    # -------------------------------------------------------------- people
    def from_actor(self, tenant_id: str, actor_id: str) -> Principal:
        """The development path: ``X-Actor-Id`` names a row of our own table."""
        if self.config.mode != "dev":
            raise AuthError(STYTCH_REFUSED, 401)
        if not self.config.dev_actor:
            raise AuthError(DEV_REFUSED, 401)
        with self.engine.connect() as conn:
            user = store.get_user(conn, tenant_id, (actor_id or "").strip())
            if user is None:
                raise AuthError("unknown caller", 401)
            principal = self._principal(conn, user, "dev-actor")
        self._seen(user)
        return principal

    def from_token(self, token: str) -> Principal:
        """The production path: a verified Stytch session, mapped to our row."""
        if self.config.mode != "stytch" or self.verifier is None:
            raise AuthError("this API does not accept Stytch sessions (MIMI_AUTH_MODE=dev)",
                            401)
        try:
            claims = self.verifier.verify(token)
        except JwtError as exc:
            # Name the reason: an operator reading a 401 needs to know whether
            # the key set, the issuer or the clock is the problem.
            raise AuthError(f"the session token was refused: {exc}", 401) from None
        with self.engine.begin() as conn:  # may link a first sign-in
            user = self._user_for(conn, claims)
            principal = self._principal(conn, user, "stytch")
            store.touch_last_seen(conn, user.tenant_id, user.user_id)
        return principal

    def _user_for(self, conn: Any, claims: Claims) -> store.User:
        tenant = claims.tenant_id
        if tenant:
            user = store.find_by_subject(conn, tenant, claims.subject)
            if user is not None:
                return user
            linked = self._link_invite(conn, tenant, claims)
            if linked is not None:
                return linked
            raise AuthError(NO_ACCOUNT, 401)
        # Stytch's token names a person, not a distributor: the subject must
        # name exactly one of our rows. Two rows means two distributors, and
        # guessing which one is not ours to do.
        found = store.find_subject_anywhere(conn, claims.subject)
        if len(found) == 1:
            return found[0]
        if found:
            raise AuthError("this sign-in belongs to more than one distributor: "
                            "sign in through your distributor's link", 401)
        linked_any = self._link_invite_anywhere(conn, claims)
        if linked_any is not None:
            return linked_any
        raise AuthError(NO_ACCOUNT, 401)

    def _link_invite_anywhere(self, conn: Any, claims: Claims) -> store.User | None:
        candidates = store.find_by_email_anywhere(conn, claims.email) if claims.email else []
        if len(candidates) == 1:
            user = candidates[0]
            return self._link(conn, user, claims)
        if len(candidates) > 1:
            raise AuthError("this sign-in matches more than one distributor: "
                            "sign in through your distributor's link", 401)
        return None

    def _link_invite(self, conn: Any, tenant: str, claims: Claims) -> store.User | None:
        """First login of an invited person: the owner created the row with a
        phone or an email, and the session carries the same one. The row is
        ours; the session only proves the person holds that number or address."""
        user = None
        if claims.email:
            user = store.find_by_email(conn, tenant, claims.email)
        else:
            phone = claims.phone or claims.raw.get("phone_number")
            user = store.find_by_phone(conn, tenant, phone if isinstance(phone, str) else None)
        return None if user is None else self._link(conn, user, claims)

    def _link(self, conn: Any, user: store.User, claims: Claims) -> store.User:
        """Link the Stytch id to the invited row, once.

        Already linked to a *different* Stytch id is refused with a sentence:
        it is the account-takeover guard. Somebody who controls the phone or
        email must not be able to take over an account that already has its
        own sign-in."""
        if user.external_subject:
            if user.external_subject == claims.subject:
                return user
            raise AuthError(
                "this account is already linked to a different Stytch sign-in; "
                "ask the distributor's owner to fix it", 403)
        return store.link_subject(conn, user.tenant_id, user.user_id, claims.subject)

    def _seen(self, user: store.User) -> None:
        if user.last_seen_at:
            try:
                last = datetime.fromisoformat(user.last_seen_at).timestamp()
            except ValueError:
                last = 0.0
            if wall() - last < LAST_SEEN_EVERY_S:
                return
        with self.engine.begin() as conn:
            store.touch_last_seen(conn, user.tenant_id, user.user_id)

    # ------------------------------------------------------------- shared
    def _principal(self, conn: Any, user: store.User, source: str) -> Principal:
        if user.status == "disabled":
            raise AuthError("this account is disabled", 403)
        if user.status != "active":
            raise AuthError("this account has not been activated yet", 403)
        scope: Scope = scopes_for(conn, user)
        return Principal(tenant_id=user.tenant_id, role=user.role, user_id=user.user_id,
                         name=user.name, email=user.email or "", phone=user.phone_e164 or "",
                         status=user.status, source=source, scope=scope)


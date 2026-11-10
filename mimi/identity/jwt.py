"""Just enough JWT to trust an identity provider, with no new dependency.

The seam (``mimi.identity.auth``) needs one thing from a token: which subject
it was issued for. Everything else is checked here, in the standard library:

* the header's ``alg`` must be an RSA signature (RS256/RS384/RS512). ``none``
  is refused, and so is an HMAC ``alg``, which is how an algorithm-confusion
  attack turns a public key into a signing key.
* the key comes from the provider's JWKS (``n``/``e``), matched by ``kid``.
* ``exp``, ``nbf`` and (when configured) ``iss`` and ``aud`` are enforced.
* the clock is ``mimi.clock.wall()``, the one wall clock.

The tenant in a token is used only to pick *which* ``users`` row to look up.
It is never an authorisation: the role always comes from our own row.

ES256/EdDSA are refused with a sentence rather than verified wrongly; a
provider that only offers them needs this module extended, not bypassed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mimi.clock import wall

# DER DigestInfo prefixes (RFC 8017 section 9.2, note 1).
_DIGEST_INFO = {
    "sha256": bytes.fromhex("3031300d060960864801650304020105000420"),
    "sha384": bytes.fromhex("3041300d060960864801650304020205000430"),
    "sha512": bytes.fromhex("3051300d060960864801650304020305000440"),
}
_ALGS = {"RS256": "sha256", "RS384": "sha384", "RS512": "sha512"}
MAX_TOKEN_CHARS = 8_192


class JwtError(Exception):
    """A token that cannot be trusted. The message is safe to log, never to echo."""


def b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except (binascii.Error, ValueError) as exc:
        raise JwtError("not base64url") from exc


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _int(raw: bytes) -> int:
    return int.from_bytes(raw, "big")


def rsa_verify(n: int, e: int, message: bytes, signature: bytes, hash_name: str) -> bool:
    """RSASSA-PKCS1-v1_5 verification, stdlib only (RFC 8017 section 8.2.2)."""
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    s = _int(signature)
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(size, "big")
    digest_info = _DIGEST_INFO[hash_name] + hashlib.new(hash_name, message).digest()
    if size < len(digest_info) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
    return hmac.compare_digest(em, expected)


@dataclass(frozen=True)
class Claims:
    subject: str
    tenant_id: str = ""
    name: str = ""
    email: str = ""
    # E.164 as the provider writes it, from the token or its session claim.
    phone: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class JwksVerifier:
    """Caches the provider's keys and verifies tokens against them.

    ``fetch`` is injectable so a test never needs the network; the default
    reads the JWKS URL with httpx (already a dependency) and its own timeout.
    """

    jwks_url: str
    issuer: str = ""
    audience: str = ""
    leeway_s: int = 60
    ttl_s: int = 300
    timeout_s: float = 5.0
    fetch: Callable[[str], dict[str, Any]] | None = None

    _keys: dict[str, tuple[int, int]] | None = field(default=None, init=False, repr=False)
    _fetched_at: float = field(default=0.0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def verify(self, token: str) -> Claims:
        if not token or len(token) > MAX_TOKEN_CHARS:
            raise JwtError("not a token")
        parts = token.split(".")
        if len(parts) != 3:
            raise JwtError("not a JWT")
        header_raw, payload_raw, signature_raw = parts
        try:
            header = json.loads(b64url_decode(header_raw))
            payload = json.loads(b64url_decode(payload_raw))
        except (JwtError, ValueError, UnicodeDecodeError) as exc:
            raise JwtError("the header or body is not JSON") from exc
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise JwtError("the header or body is not an object")
        alg = str(header.get("alg", ""))
        hash_name = _ALGS.get(alg)
        if hash_name is None:
            raise JwtError(f"signature algorithm {alg or 'missing'!r} is not accepted; "
                           "the provider must sign with RS256, RS384 or RS512")
        kid = str(header.get("kid", ""))
        n, e = self._key(kid)
        message = f"{header_raw}.{payload_raw}".encode()
        if not rsa_verify(n, e, message, b64url_decode(signature_raw), hash_name):
            raise JwtError("the signature does not match")
        self._check_times(payload)
        if self.issuer and payload.get("iss") != self.issuer:
            raise JwtError("the issuer is not the configured one")
        if self.audience and not _aud_matches(payload.get("aud"), self.audience):
            raise JwtError("the audience is not this application")
        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub.strip():
            raise JwtError("the token names no subject")
        session = _session(payload)
        return Claims(subject=sub, tenant_id=_tenant_claim(payload),
                      name=_str(payload, "name", "given_name") or session.get("name", ""),
                      email=(_str(payload, "email", "email_address")
                             or session.get("email", "")
                             or _str(_dict(payload, "user_metadata"), "email")),
                      phone=(_str(payload, "phone_number", "phone")
                             or session.get("phone_number", "")
                             or _str(_dict(payload, "user_metadata"), "phone_number")),
                      raw=payload)

    # ------------------------------------------------------------- keys
    def _key(self, kid: str) -> tuple[int, int]:
        with self._lock:
            if self._keys is None or wall() - self._fetched_at > self.ttl_s:
                self._load()
            keys = self._keys or {}
            if kid in keys:
                return keys[kid]
            # An unknown kid may be a rotation: fetch once more, then refuse.
            self._load()
            keys = self._keys or {}
            if kid in keys:
                return keys[kid]
            if not kid and len(keys) == 1:
                return next(iter(keys.values()))
            raise JwtError("the token's key is not in the provider's JWKS")

    def _load(self) -> None:
        source = self.fetch or _http_json
        try:
            doc = source(self.jwks_url)
        except JwtError:
            raise
        except Exception as exc:  # network, JSON, anything: no key, no caller
            raise JwtError(f"the provider's key set could not be read: {type(exc).__name__}"
                           ) from exc
        keys: dict[str, tuple[int, int]] = {}
        for item in (doc.get("keys") or []) if isinstance(doc, dict) else []:
            if not isinstance(item, dict) or item.get("kty") != "RSA":
                continue
            try:
                n = _int(b64url_decode(str(item["n"])))
                e = _int(b64url_decode(str(item["e"])))
            except (KeyError, JwtError, ValueError):
                continue
            if n > 1 and e > 1:
                keys[str(item.get("kid", ""))] = (n, e)
        if not keys:
            raise JwtError("the provider's key set holds no RSA key")
        self._keys, self._fetched_at = keys, wall()

    # ------------------------------------------------------------ times
    def _check_times(self, payload: dict[str, Any]) -> None:
        now = wall()
        exp = _num(payload.get("exp"))
        if exp is None:
            raise JwtError("the token never expires")
        if now > exp + self.leeway_s:
            raise JwtError("the token has expired")
        nbf = _num(payload.get("nbf"))
        if nbf is not None and now + self.leeway_s < nbf:
            raise JwtError("the token is not valid yet")


def _dict(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name)
    return value if isinstance(value, dict) else {}


def _str(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# Stytch puts its own session facts in a namespaced claim. The exact shape
# depends on the product (consumer or B2B) and is checked against a real
# project at launch; these are the fields the link step needs, read
# tolerantly so a missing namespace is not an error.
_SESSION_NS = ("https://stytch.com/session", "https://stytch.com/b2b/session")


def _session(payload: dict[str, Any]) -> dict[str, str]:
    """The verified email, phone and name, wherever the project puts them.

    Stytch's session claim holds the session's own facts; a project that
    wants the identity in the token puts it in ``custom_claims`` (or the
    console records the Stytch id on the invite instead, which needs neither).
    Each shape is read in turn, and a missing one is not an error."""
    for name in _SESSION_NS:
        value = _dict(payload, name)
        if not value:
            continue
        out: dict[str, str] = {}
        for source in (value, _dict(value, "attributes"), _dict(value, "custom_claims")):
            for key in ("email", "email_address", "phone_number", "phone", "name"):
                got = source.get(key)
                if isinstance(got, str) and got.strip():
                    out.setdefault("email" if key in ("email", "email_address") else
                                   "phone_number" if key in ("phone_number", "phone") else key,
                                   got.strip())
        if out:
            return out
    return {}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _aud_matches(aud: Any, wanted: str) -> bool:
    if isinstance(aud, str):
        return aud == wanted
    if isinstance(aud, list):
        return wanted in [str(a) for a in aud]
    return False


def _tenant_claim(payload: dict[str, Any]) -> str:
    for name in ("tenant_id", "mimi_tenant", "https://mimi/tenant"):
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _http_json(url: str) -> dict[str, Any]:
    import httpx

    response = httpx.get(url, timeout=5.0, follow_redirects=False)
    response.raise_for_status()
    doc = response.json()
    if not isinstance(doc, dict):
        raise JwtError("the key set is not an object")
    return doc

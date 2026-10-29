"""API settings, all from the environment. No secret is ever stored in the repo.

* ``MIMI_ENV``: ``dev`` (default) or ``production``.
* ``DATABASE_URL`` (or ``MIMI_DB_URL``): the store; default ``sqlite:///./mimi.db``.
* ``MIMI_API_KEYS``: one bearer key per distributor and role, comma separated,
  ``<tenant_id>:<role>:<key>``. ``role`` is ``owner`` (the console: reads and
  approvals) or ``channel`` (the WhatsApp webhook and the simulator: only
  ``POST /v1/turns``). ``<key>`` may be given as ``sha256:<hex>`` so the
  plain key never sits in the environment of the API host.
* ``MIMI_CORS_ORIGINS``: comma-separated origins; default
  ``https://console.userail.money``. Any ``http://localhost:<port>`` and
  ``http://127.0.0.1:<port>`` is always allowed for local console work.
* ``MIMI_TURN_WAIT_S``: how long ``POST /v1/turns`` waits for the replies
  before answering ``202`` with the turn id (default 25).
* ``MIMI_WORKERS``: worker threads inside the API process (default 1; 0 when
  separate ``python -m mimi.api worker`` processes do the work).
* ``MIMI_LEASE_S``: turn lease length in seconds (default 30).
* ``META_API_KEY`` and friends (``mimi.models.env``): Muse Spark. Unset means
  the offline model, which production refuses.
* ``MIMI_PHONE_PSEUDONYM_KEY``: required in production (``mimi.harness.inbound``).
* ``MIMI_MODE``: ``live`` (default: bearer keys only) or ``demo`` (public demo
  sessions on synthetic copies behind naira caps; ``mimi.api.demo`` lists its
  ``MIMI_DEMO_*`` settings).
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from mimi.api.demo import DemoConfig
from mimi.store.db import database_url

Role = Literal["owner", "channel"]
ROLES: tuple[Role, ...] = ("owner", "channel")
MODES: tuple[str, ...] = ("live", "demo")
DEFAULT_CORS = ("https://console.userail.money",)
# The Makefile's local demo Bearer keys (docs/API.md). Public, so never valid in
# production.
DEMO_KEYS: tuple[str, ...] = ("local-owner-key-0001", "local-channel-key-001")
LOCALHOST_REGEX = r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApiKey:
    tenant_id: str
    role: Role
    digest: str  # sha256 hex of the key
    # A demo visitor's session token (MIMI_MODE=demo), not a configured key.
    session: bool = False


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def parse_keys(raw: str) -> tuple[ApiKey, ...]:
    out: list[ApiKey] = []
    for item in (x.strip() for x in raw.split(",")):
        if not item:
            continue
        parts = item.split(":", 2)
        if len(parts) != 3 or not all(parts):
            raise ConfigError("MIMI_API_KEYS entries are <tenant_id>:<role>:<key>")
        tenant, role, secret = parts
        if role not in ROLES:
            raise ConfigError(f"MIMI_API_KEYS: unknown role {role!r} (owner or channel)")
        if secret.startswith("sha256:"):
            digest = secret[len("sha256:"):].lower()
            if len(digest) != 64:
                raise ConfigError("MIMI_API_KEYS: sha256 digests are 64 hex characters")
        else:
            if len(secret) < 16:
                raise ConfigError("MIMI_API_KEYS: keys are at least 16 characters")
            digest = key_digest(secret)
        out.append(ApiKey(tenant, role, digest))  # type: ignore[arg-type]
    return tuple(out)


@dataclass(frozen=True)
class Settings:
    env: str = "dev"
    database_url: str = "sqlite:///./mimi.db"
    keys: tuple[ApiKey, ...] = ()
    cors_origins: tuple[str, ...] = DEFAULT_CORS
    wait_s: float = 25.0
    workers: int = 1
    lease_s: float = 30.0
    has_model_key: bool = False
    release: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    mode: str = "live"
    demo: DemoConfig = field(default_factory=DemoConfig)

    @property
    def production(self) -> bool:
        return self.env.lower() in ("prod", "production")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = env if env is not None else os.environ
        cors = tuple(x.strip() for x in e.get("MIMI_CORS_ORIGINS", "").split(",") if x.strip())
        return cls(
            env=e.get("MIMI_ENV", "dev").strip() or "dev",
            database_url=database_url(e),
            keys=parse_keys(e.get("MIMI_API_KEYS", "")),
            cors_origins=cors or DEFAULT_CORS,
            wait_s=float(e.get("MIMI_TURN_WAIT_S", "25") or 25),
            workers=int(e.get("MIMI_WORKERS", "1") or 1),
            lease_s=float(e.get("MIMI_LEASE_S", "30") or 30),
            has_model_key=bool(e.get("META_API_KEY", "").strip()),
            release=e.get("MIMI_RELEASE", "").strip(),
            mode=(e.get("MIMI_MODE", "live").strip().lower() or "live"),
            demo=DemoConfig.from_env(e),
        )

    @property
    def demo_mode(self) -> bool:
        return self.mode == "demo"

    def lookup(self, presented: str) -> ApiKey | None:
        """The key's tenant and role, compared in constant time."""
        if not presented:
            return None
        d = key_digest(presented)
        found: ApiKey | None = None
        for k in self.keys:
            if hmac.compare_digest(k.digest, d):
                found = k
        return found

    def check(self) -> None:
        """Refuse to start a production API that is not safe to run."""
        if self.mode not in MODES:
            raise ConfigError(f"MIMI_MODE is {self.mode!r}: use live or demo")
        if not self.production:
            return
        from mimi.harness.inbound import _pseudonym_key

        _pseudonym_key()  # raises without MIMI_PHONE_PSEUDONYM_KEY in production
        if self.demo_mode:
            # Demo visitors sign in with API-issued session tokens; configured
            # keys are optional (an operator's), never required.
            if not self.demo.ip_salt:
                raise ConfigError("MIMI_DEMO_IP_SALT is not set: demo mode in production "
                                  "hashes visitor addresses with it")
        elif not self.keys:
            raise ConfigError("MIMI_API_KEYS is not set: refusing to serve without auth")
        if not self.has_model_key:
            raise ConfigError("META_API_KEY is not set: production never runs the offline model")
        if self.database_url.lower().startswith("sqlite"):
            raise ConfigError("production needs DATABASE_URL (Postgres): refusing to serve "
                              "from SQLite")
        demo = {key_digest(k) for k in DEMO_KEYS}
        if any(k.digest in demo for k in self.keys):
            raise ConfigError("MIMI_API_KEYS holds a published local demo key: refusing to "
                              "serve production with it")

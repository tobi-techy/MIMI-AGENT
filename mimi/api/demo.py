"""Demo mode (``MIMI_MODE=demo``): a public console on synthetic data, behind caps.

Every visitor gets their own copy of the synthetic Coral Lane Trading world
(fictional shops, people and numbers), seeded with the same
``seed_world(..., tenant_id=...)`` as ``python -m mimi.store seed --tenant-id``.
The API issues the visitor a session token; no key is ever baked into the
console's JavaScript.

* ``POST /v1/demo/sessions`` starts a session: a fresh copy, a token, a TTL.
* ``POST /v1/demo/reset`` ends the caller's session and starts a fresh one.
* A token past its TTL is refused. Copies past their TTL (plus a grace for a
  turn still running) are swept when a new session starts; their model spend
  is kept per visitor address and day, so the caps still count it.

Caps, all enforced before a turn is queued (``DemoStore.gate``):

* per visitor address, per Africa/Lagos day, across all of that address's
  sessions (``MIMI_DEMO_IP_DAILY_CAP_NAIRA``);
* across every demo session, per day (``MIMI_DEMO_GLOBAL_DAILY_CAP_NAIRA``);
* ``MIMI_DEMO_KILL=1`` refuses every new turn (reads keep working);
* each copy's own daily cap is the per-address cap, so a run that goes over
  mid-turn parks on the loop's existing daily cap (``Agent._park``), and the
  "raise the allowance" approval it posts is refused in demo.

Approvals that would raise a credit limit or the model allowance are refused
in demo (``refuses_approval``); declining them still works.

Environment (naira figures are whole naira; the defaults are placeholders for
the operator to set):

* ``MIMI_DEMO_IP_DAILY_CAP_NAIRA`` (default 400: about 25 turns, enough for
  the judges' script; it was 100, which ended a visit after 4 to 8 turns)
* ``MIMI_DEMO_GLOBAL_DAILY_CAP_NAIRA`` (default 2000)
* ``MIMI_DEMO_SESSION_TTL_S`` (default 3600)
* ``MIMI_DEMO_SESSIONS_PER_IP`` (default 20 a day)
* ``MIMI_DEMO_KILL`` (``1`` refuses new turns)
* ``MIMI_DEMO_CLIENT_IP_HEADER`` (default ``cf-connecting-ip``): the one
  header the edge sets to the visitor's address. Cloudflare (in front of
  Atlasflow's edge) writes ``CF-Connecting-IP`` itself and overwrites any
  value a client sends. Read only when the socket peer is a trusted proxy;
  ``none`` turns it off.
* ``MIMI_DEMO_TRUSTED_PROXIES`` (default private, loopback and CGNAT ranges:
  the platform's own edge): comma-separated CIDRs whose requests may carry
  that header; ``*`` trusts any peer (only when the app is reachable through
  the edge alone).
* ``X-Forwarded-For`` is never read: a client can write it, and a fake one
  used to buy a fresh allowance (30 Oct 2026). ``MIMI_DEMO_TRUST_FORWARDED``
  is ignored.
* ``MIMI_DEMO_IP_SALT`` (the key the address digests are made with; required
  in production)
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.clock import wall
from mimi.harness.policy import NEVER_TOOLS
from mimi.store import schema as s
from mimi.store.seed import delete_tenant, seed_world
from mimi.store.util import insert_ignore, now_iso
from mimi.tz import lagos_day
from mimi.world.loader import load_world
from mimi.world.model import World

# PLACEHOLDERS for Tobi to set (whole naira). Conservative on purpose.
DEFAULT_IP_CAP_NAIRA = 400  # about 25 turns at about N16 a turn (30 Oct 2026)
DEFAULT_GLOBAL_CAP_NAIRA = 2_000
DEFAULT_CLIENT_IP_HEADER = "cf-connecting-ip"
DEFAULT_TRUSTED_PROXIES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
                           "127.0.0.0/8", "169.254.0.0/16", "::1/128", "fc00::/7",
                           "fe80::/10")
DEFAULT_TTL_S = 3600.0
DEFAULT_SESSIONS_PER_IP = 20
# An expired copy is deleted only this long after its TTL, so a turn that was
# running at expiry finishes first.
SWEEP_GRACE_S = 600.0
SWEEP_BATCH = 20
TOKEN_PREFIX = "mimi_demo_"
TENANT_PREFIX = "demo-"

# Approvals demo never grants: they raise a credit limit or Mimi's own model
# allowance (which would lift the demo caps).
CAP_RAISES: frozenset[str] = frozenset(
    {"submit_credit_review", "raise_daily_allowance"}
    | {t for t in NEVER_TOOLS if "credit" in t})

PAUSED = "the demo is paused right now; you can still look around"
IP_CAPPED = ("you have used today's demo allowance; it resets at midnight Lagos time")
GLOBAL_CAPPED = ("the demo has used today's allowance for everyone; it resets at midnight "
                 "Lagos time")
TOO_MANY = "too many demo sessions from this address today; try again tomorrow"
EXPIRED = "demo session expired: start a new one"


class DemoRefused(Exception):
    """A demo request that is refused, with the HTTP status to answer.

    ``payload()`` is the body the API answers with: ``detail`` (the message,
    kept for older clients) and ``demo_limit``, one fixed shape the console
    shows (docs/API.md, "Demo limits"):

    ``code`` (``visitor_daily_cap``, ``global_daily_cap``, ``paused``,
    ``too_many_sessions``), ``scope`` (``visitor``, ``global``, ``demo``),
    ``title`` and ``message`` (plain words to show), ``spent_kobo``,
    ``cap_kobo`` and their ``*_naira`` strings (null when not a spend cap),
    ``resets_at`` (next midnight Africa/Lagos, ISO 8601) and ``retry``
    (``tomorrow`` or ``later``).
    """

    def __init__(self, status: int, message: str, *, code: str = "", scope: str = "demo",
                 title: str = "", spent_kobo: int | None = None,
                 cap_kobo: int | None = None, resets_at: str = "",
                 retry: str = "later") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.scope = scope
        self.title = title
        self.spent_kobo = spent_kobo
        self.cap_kobo = cap_kobo
        self.resets_at = resets_at
        self.retry = retry

    def payload(self) -> dict[str, Any]:
        from mimi.owed import naira

        msg = str(self)
        return {"detail": msg, "demo_limit": {
            "code": self.code, "scope": self.scope, "title": self.title,
            "message": msg[:1].upper() + msg[1:] + ("" if msg.endswith(".") else "."),
            "spent_kobo": self.spent_kobo, "cap_kobo": self.cap_kobo,
            "spent_naira": naira(self.spent_kobo) if self.spent_kobo is not None else None,
            "cap_naira": naira(self.cap_kobo) if self.cap_kobo is not None else None,
            "resets_at": self.resets_at, "retry": self.retry}}


def next_lagos_midnight(ts: float) -> str:
    """ISO time of the next midnight in Africa/Lagos after ``ts``."""
    from datetime import timedelta

    from mimi.tz import LAGOS

    local = datetime.fromtimestamp(ts, LAGOS)
    nxt = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.isoformat()


class SessionExpired(Exception):
    pass


def _flag(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class DemoConfig:
    ip_daily_cap_kobo: int = DEFAULT_IP_CAP_NAIRA * 100
    global_daily_cap_kobo: int = DEFAULT_GLOBAL_CAP_NAIRA * 100
    session_ttl_s: float = DEFAULT_TTL_S
    sessions_per_ip: int = DEFAULT_SESSIONS_PER_IP
    killed: bool = False
    client_ip_header: str = DEFAULT_CLIENT_IP_HEADER
    trusted_proxies: tuple[str, ...] = DEFAULT_TRUSTED_PROXIES
    ip_salt: str = ""

    @classmethod
    def from_env(cls, e: Mapping[str, str]) -> DemoConfig:
        def naira(name: str, default: int) -> int:
            return int(float(e.get(name, "") or default) * 100)

        return cls(
            ip_daily_cap_kobo=naira("MIMI_DEMO_IP_DAILY_CAP_NAIRA", DEFAULT_IP_CAP_NAIRA),
            global_daily_cap_kobo=naira("MIMI_DEMO_GLOBAL_DAILY_CAP_NAIRA",
                                        DEFAULT_GLOBAL_CAP_NAIRA),
            session_ttl_s=float(e.get("MIMI_DEMO_SESSION_TTL_S", "") or DEFAULT_TTL_S),
            sessions_per_ip=int(e.get("MIMI_DEMO_SESSIONS_PER_IP", "")
                                or DEFAULT_SESSIONS_PER_IP),
            killed=_flag(e.get("MIMI_DEMO_KILL", "")),
            client_ip_header=_header_name(e.get("MIMI_DEMO_CLIENT_IP_HEADER")),
            trusted_proxies=_proxies(e.get("MIMI_DEMO_TRUSTED_PROXIES")),
            ip_salt=e.get("MIMI_DEMO_IP_SALT", "").strip(),
        )

    def caps(self) -> dict[str, int]:
        return {"ip_daily_cap_kobo": self.ip_daily_cap_kobo,
                "global_daily_cap_kobo": self.global_daily_cap_kobo}


def refuses_approval(tool: str, intent_kind: str) -> bool:
    """Demo never approves a credit limit or allowance raise."""
    return tool in CAP_RAISES or intent_kind in CAP_RAISES


def _header_name(raw: str | None) -> str:
    if raw is None or not raw.strip():
        return DEFAULT_CLIENT_IP_HEADER
    v = raw.strip().lower()
    if v in ("none", "off", "0", "false"):
        return ""
    if v == "x-forwarded-for":
        raise ValueError("MIMI_DEMO_CLIENT_IP_HEADER: X-Forwarded-For is client-writable; "
                         "use the edge's own header (cf-connecting-ip)")
    return v


def _proxies(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return DEFAULT_TRUSTED_PROXIES
    out = tuple(p.strip() for p in raw.split(",") if p.strip())
    for p in out:
        if p != "*":
            ipaddress.ip_network(p, strict=False)  # a typo fails at start-up
    return out


def _ip(raw: str) -> str:
    """``raw`` as a canonical address, or "" when it is not one."""
    v = raw.strip()
    if v.startswith("[") and "]" in v:
        v = v[1:v.index("]")]
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        return ""


def peer_trusted(peer: str, proxies: tuple[str, ...]) -> bool:
    if "*" in proxies:
        return True
    ip = _ip(peer)
    if not ip:
        return False
    addr = ipaddress.ip_address(ip)
    for p in proxies:
        net = ipaddress.ip_network(p, strict=False)
        if addr.version == net.version and addr in net:
            return True
    return False


def client_ip(peer: str, headers: Mapping[str, str], conf: DemoConfig) -> tuple[str, str]:
    """The visitor's address and where it came from: ``header`` or ``peer``.

    Only the edge's own single-value header (``CF-Connecting-IP``), and only
    on a request whose socket peer is a trusted proxy. Never
    ``X-Forwarded-For``: a client writes that. Anything else is the peer."""
    name = conf.client_ip_header
    if name and peer_trusted(peer, conf.trusted_proxies):
        got = _ip(str(headers.get(name, "") or ""))
        if got:
            return got, "header"
    return (_ip(peer) or peer or "unknown"), "peer"


def client_address(peer: str, headers: Mapping[str, str], conf: DemoConfig) -> str:
    return client_ip(peer, headers, conf)[0]


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@lru_cache(maxsize=1)
def _world() -> World:
    return load_world()


@dataclass(frozen=True)
class Session:
    tenant_id: str
    ip_hash: str
    expires_ts: float
    digest: str


class DemoStore:
    def __init__(self, engine: Engine, conf: DemoConfig, *,
                 clock: Callable[[], float] = wall,
                 on_drop: Callable[[str], None] | None = None) -> None:
        self.engine = engine
        self.conf = conf
        self.clock = clock
        # Called with a copy's tenant id after the copy is swept (its uploads go too).
        self.on_drop = on_drop

    # ------------------------------------------------------------ helpers

    def ip_hash(self, address: str) -> str:
        salt = (self.conf.ip_salt or "mimi-demo-dev-salt").encode()
        return hmac.new(salt, address.encode(), hashlib.sha256).hexdigest()

    def _day(self, ts: float | None = None) -> str:
        t = self.clock() if ts is None else ts
        return lagos_day(datetime.fromtimestamp(t, UTC)).isoformat()

    # ----------------------------------------------------------- sessions

    def start(self, ip_hash: str) -> dict[str, Any]:
        """A fresh copy of the synthetic world and a token for it."""
        self.sweep()
        now = self.clock()
        day = self._day(now)
        with self.engine.connect() as conn:
            n = int(conn.execute(sa.select(sa.func.count()).select_from(s.demo_sessions)
                                 .where(s.demo_sessions.c.ip_hash == ip_hash,
                                        s.demo_sessions.c.day == day)).scalar() or 0)
        if n >= self.conf.sessions_per_ip:
            raise DemoRefused(429, TOO_MANY, code="too_many_sessions", scope="visitor",
                              title="Too many demo sessions today",
                              resets_at=next_lagos_midnight(now), retry="tomorrow")
        tenant = TENANT_PREFIX + secrets.token_hex(8)
        world = _world()
        seed_world(self.engine, world, tenant_id=tenant,
                   name=f"{world.distributor} (demo copy)",
                   daily_cap_kobo=self.conf.ip_daily_cap_kobo)
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        expires = now + self.conf.session_ttl_s
        with self.engine.begin() as conn:
            conn.execute(s.demo_sessions.insert().values(
                token_digest=token_digest(token), session_tenant=tenant, ip_hash=ip_hash,
                day=day, created_at=now_iso(now), created_ts=now, expires_ts=expires,
                swept=False))
        return {"token": token, "tenant_id": tenant, "mode": "demo",
                "expires_at": now_iso(expires), "ttl_s": self.conf.session_ttl_s,
                "caps": self.conf.caps()}

    def lookup(self, token: str) -> Session | None:
        """The session a token names; ``SessionExpired`` past its TTL."""
        if not token.startswith(TOKEN_PREFIX):
            return None
        d = token_digest(token)
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(s.demo_sessions).where(
                s.demo_sessions.c.token_digest == d)).first()
        if row is None:
            return None
        if row.swept or row.expires_ts <= self.clock():
            raise SessionExpired(d)
        return Session(row.session_tenant, row.ip_hash, float(row.expires_ts), d)

    def session(self, digest: str) -> Session | None:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(s.demo_sessions).where(
                s.demo_sessions.c.token_digest == digest)).first()
        return (Session(row.session_tenant, row.ip_hash, float(row.expires_ts), digest)
                if row is not None else None)

    def end(self, digest: str) -> None:
        """End a session now (its copy is swept after the grace)."""
        with self.engine.begin() as conn:
            conn.execute(s.demo_sessions.update().where(
                s.demo_sessions.c.token_digest == digest,
                s.demo_sessions.c.expires_ts > self.clock())
                .values(expires_ts=self.clock()))

    def sweep(self, limit: int = SWEEP_BATCH) -> int:
        """Delete expired copies; keep their spend per address and day."""
        cutoff = self.clock() - SWEEP_GRACE_S
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(s.demo_sessions).where(
                s.demo_sessions.c.swept == sa.false(),
                s.demo_sessions.c.expires_ts < cutoff)
                .order_by(s.demo_sessions.c.expires_ts).limit(limit)).all()
        done = 0
        for r in rows:
            with self.engine.begin() as conn:
                busy = conn.execute(sa.select(s.turns.c.turn_id).where(
                    s.turns.c.tenant_id == r.session_tenant,
                    s.turns.c.state == "leased")).first()
                if busy is not None:
                    continue
                for sp in conn.execute(sa.select(s.spend).where(
                        s.spend.c.tenant_id == r.session_tenant)).all():
                    if not insert_ignore(conn, s.demo_usage, {
                            "day": sp.day, "ip_hash": r.ip_hash, "kobo": int(sp.kobo)}):
                        conn.execute(s.demo_usage.update().where(
                            s.demo_usage.c.day == sp.day,
                            s.demo_usage.c.ip_hash == r.ip_hash)
                            .values(kobo=s.demo_usage.c.kobo + int(sp.kobo)))
                delete_tenant(conn, r.session_tenant)
                conn.execute(s.demo_sessions.update().where(
                    s.demo_sessions.c.token_digest == r.token_digest).values(swept=True))
                done += 1
            if self.on_drop is not None:
                self.on_drop(r.session_tenant)
        return done

    # --------------------------------------------------------------- caps

    def usage(self, ip_hash: str) -> dict[str, int]:
        """Today's model spend (kobo) for this address and for the whole demo."""
        day = self._day()
        live = s.demo_sessions.c.swept == sa.false()
        with self.engine.connect() as conn:
            def spent(where: Any) -> int:
                tenants = sa.select(s.demo_sessions.c.session_tenant).where(live, where)
                running = conn.execute(sa.select(sa.func.coalesce(sa.func.sum(
                    s.spend.c.kobo), 0)).where(s.spend.c.day == day,
                                               s.spend.c.tenant_id.in_(tenants))).scalar()
                return int(running or 0)

            ip_live = spent(s.demo_sessions.c.ip_hash == ip_hash)
            all_live = spent(sa.true())
            ip_old = conn.execute(sa.select(sa.func.coalesce(sa.func.sum(
                s.demo_usage.c.kobo), 0)).where(s.demo_usage.c.day == day,
                                                s.demo_usage.c.ip_hash == ip_hash)).scalar()
            all_old = conn.execute(sa.select(sa.func.coalesce(sa.func.sum(
                s.demo_usage.c.kobo), 0)).where(s.demo_usage.c.day == day)).scalar()
        return {"ip_spent_kobo": ip_live + int(ip_old or 0),
                "global_spent_kobo": all_live + int(all_old or 0)}

    def gate(self, ip_hash: str) -> None:
        """Refuse a new turn when the demo is paused or a daily cap is used up."""
        if self.conf.killed:
            raise DemoRefused(503, PAUSED, code="paused", scope="demo",
                              title="The demo is paused")
        u = self.usage(ip_hash)
        resets = next_lagos_midnight(self.clock())
        if u["global_spent_kobo"] >= self.conf.global_daily_cap_kobo:
            raise DemoRefused(429, GLOBAL_CAPPED, code="global_daily_cap", scope="global",
                              title="Today's demo allowance is used up",
                              spent_kobo=u["global_spent_kobo"],
                              cap_kobo=self.conf.global_daily_cap_kobo, resets_at=resets,
                              retry="tomorrow")
        if u["ip_spent_kobo"] >= self.conf.ip_daily_cap_kobo:
            raise DemoRefused(429, IP_CAPPED, code="visitor_daily_cap", scope="visitor",
                              title="You have used today's demo allowance",
                              spent_kobo=u["ip_spent_kobo"],
                              cap_kobo=self.conf.ip_daily_cap_kobo, resets_at=resets,
                              retry="tomorrow")

    def status(self, sess: Session | None, ip_hash: str,
               ip_source: str = "") -> dict[str, Any]:
        """What ``GET /v1/me`` shows a demo caller."""
        u = self.usage(sess.ip_hash if sess else ip_hash)
        return {"expires_at": now_iso(sess.expires_ts) if sess else None,
                # "header" when the edge's client address header was used, "peer"
                # when the socket address was (never the address itself).
                "ip_source": ip_source,
                "resets_at": next_lagos_midnight(self.clock()),
                "ip_spent_kobo": u["ip_spent_kobo"],
                "ip_cap_kobo": self.conf.ip_daily_cap_kobo,
                "global_spent_kobo": u["global_spent_kobo"],
                "global_cap_kobo": self.conf.global_daily_cap_kobo,
                "paused": self.conf.killed}

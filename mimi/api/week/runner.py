"""The simulated week: 40 Coral Lane shops for 7 days through the real HTTP API.

Everything goes through the HTTP API (``POST /v1/turns``, ``POST /v1/events``,
``/v1/approvals``) on an in-process ``TestClient``, with the durable store
(SQLite by default, or a throwaway Postgres database) and real worker
*processes* (``worker.py``) taking turns off the queue. The API process runs
no worker. Go's part (the book's day, deliveries, bank credits, cash) is
``go.GoSim``; the shops are seeded, deterministic personas (``personas``).

A day, in order of its simulated times (the shared clock is moved forward
between waves, only when every sent turn has settled):

* Monday and Thursday 07:30: the weekly stock check for a dozen (then six)
  shops; each one-tap question gets a tap, a worded count or an unclear
  answer (then a tap after the one ask again).
* Deliveries: all 40 shops on Monday (the world's live day), five or six
  top-ups Tuesday to Saturday, none on Sunday. Go's ``delivery_completed``
  sends the receipt; the shop answers YES, in other words, unclearly (one ask
  again) or disputes a short line.
* Payments, five paths: (1) a payment screenshot, genuine; (2) a fake alert
  screenshot; (3) the bank-feed credit (``bank_credit`` webhook) for the
  genuine ones next morning, and the shop asking whether it landed; (4) cash
  to the rep, logged by Go, and the shop saying so; (5) a promise to pay on a
  day. Plus resends of a screenshot already checked, and unreadable ones.
* Credit asks: a shop asks for more credit, Mimi asks for a shelf photo, the
  shop sends it, SAM 3.1 counts it (recorded answers), the request goes to
  the owner's approvals.
* Owner questions every day, balance questions, a refund ask (a never-tier
  probe), small talk.
* 20:30: the owner decides the approvals waiting (restock drafts, credit
  requests, orders, Mimi's own daily allowance; fake-alert flags stay for
  the owner to check with the bank).
* Replays: a seeded 5% of each wave's messages are sent again with the same
  message id (a webhook redelivery); each must return the first turn.

``run_week`` returns a ``WeekRun`` with the runner's own records; the scorer
(``report.py``) reads the store. Nothing is scored here.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from mimi import clock
from mimi.api.week import personas as P
from mimi.api.week.go import GoSim, book_rows
from mimi.api.week.simclock import install, set_to
from mimi.api.week.worker import LEASE_S
from mimi.store import schema as s
from mimi.store.db import ensure_schema, make_engine
from mimi.store.seed import seed_world
from mimi.tz import LAGOS
from mimi.world.loader import load_world
from mimi.world.model import World

TENANT = "coral-lane"
DAYS = 7
FIRST_DAY = 56  # Monday 26 Oct 2026, the world's live day
TURN_TIMEOUT_S = 120.0
DAILY_CAP_KOBO = 300_000  # N3,000 a day of (projected) model spend for the synthetic tenant
KEY_PREFIX = "wk-key"
KILL_EVERY = 12  # waves between the one week's forced kills


def tenant_for(loop: int) -> str:
    return TENANT if loop == 0 else f"{TENANT}-s{loop:03d}"


def keys_for(tenant: str) -> dict[str, str]:
    h = hashlib.sha256(tenant.encode()).hexdigest()[:16]
    return {"owner": f"{KEY_PREFIX}-owner-{h}", "channel": f"{KEY_PREFIX}-chan-{h}"}


# ------------------------------------------------------------------ records


@dataclass
class Sent:
    loop: int
    tenant: str
    day: int
    scene: str
    shop_id: str
    label: str
    ext: str
    turn_id: str
    sim_at: str
    sent_at: float = 0.0
    done_at: float = 0.0
    view: dict[str, Any] = field(default_factory=dict)
    path: str = ""  # payment path, for payment scenes


@dataclass
class WeekRun:
    mode: str
    workdir: Path
    db_url: str
    seed: int
    sent: list[Sent] = field(default_factory=list)
    replays: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    kills: list[dict[str, Any]] = field(default_factory=list)
    worker_log: list[str] = field(default_factory=list)
    loops: list[dict[str, Any]] = field(default_factory=list)
    books: dict[str, dict[str, Any]] = field(default_factory=dict)  # tenant -> before/after/go
    stopped: str = ""  # why the run stopped early, if it did
    started: float = 0.0
    finished: float = 0.0


def save_run(run: WeekRun) -> Path:
    """The runner's records next to the store, so a report can be scored again."""
    from dataclasses import asdict

    p = run.workdir / "run.json"
    p.write_text(json.dumps(asdict(run), default=str) + "\n")
    return p


def load_run(workdir: Path) -> WeekRun:
    raw = json.loads((workdir / "run.json").read_text())
    raw["workdir"] = Path(raw["workdir"])
    raw["sent"] = [Sent(**x) for x in raw["sent"]]
    return WeekRun(**raw)


# ------------------------------------------------------------------ workers


class Workers:
    """N worker processes, and a monitor that services forced kills."""

    def __init__(self, run: WeekRun, *, n: int, live: bool, price_as: str,
                 ceiling_kobo: int | None = None) -> None:
        self.run, self.n, self.live, self.price_as = run, n, live, price_as
        self.ceiling_kobo = ceiling_kobo
        self.wd = run.workdir
        self.procs: dict[str, subprocess.Popen[bytes]] = {}
        self.plan: list[dict[str, Any]] = []
        self.handled: set[str] = set()
        self.started_n = 0
        self.lock = threading.RLock()
        self._halt = threading.Event()
        self.monitor = threading.Thread(target=self._watch, daemon=True, name="kill-monitor")
        self.engine: Any = None
        self.write_plan()

    def write_plan(self) -> None:
        tmp = self.wd / ".kill-plan.json.tmp"
        tmp.write_text(json.dumps(self.plan, indent=1) + "\n")
        os.replace(tmp, self.wd / "kill-plan.json")

    def arm(self, kid: str, at: str, ext: str = "*") -> None:
        """Arm one crash point: the next turn reaching it is killed."""
        with self.lock:
            self.plan.append({"id": kid, "ext": ext, "at": at, "armed_at": time.time()})
            self.write_plan()

    def pending_kills(self) -> int:
        return sum(1 for k in self.plan if k["id"] not in self.handled)

    def start_all(self) -> None:
        for _ in range(self.n):
            self._start()
        self.monitor.start()

    def _start(self) -> str:
        self.started_n += 1
        name = f"worker-{self.started_n:03d}"
        cmd = [sys.executable, "-m", "mimi.api.week", "worker", "--db", self.run.db_url,
               "--workdir", str(self.wd), "--worker-id", name, "--price-as", self.price_as]
        if self.live:
            cmd.append("--live")
        log = open(self.wd / f"{name}.log", "wb")  # noqa: SIM115
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        if self.ceiling_kobo:
            env["MIMI_WEEK_MAX_KOBO"] = str(self.ceiling_kobo)  # the meter refuses past it
        self.procs[name] = subprocess.Popen(cmd, env=env,
                                            stdout=log, stderr=subprocess.STDOUT)
        self.run.worker_log.append(f"started {name}")
        return name

    def _watch(self) -> None:
        while not self._halt.wait(0.05):
            try:
                with self.lock:
                    self._service()
                    self._respawn_dead()
            except Exception as exc:  # recorded; the report shows the kill did not run
                self.run.worker_log.append(f"monitor error: {exc!r}")

    def _respawn_dead(self) -> None:
        for name, p in list(self.procs.items()):
            if p.poll() is not None:
                self.run.worker_log.append(f"{name} exited {p.returncode}; restarting")
                del self.procs[name]
                self._start()

    def _service(self) -> None:
        for k in self.plan:
            f = self.wd / f"killpoint-{k['id']}"
            if k["id"] in self.handled or not f.exists():
                continue
            self.handled.add(str(k["id"]))
            info = json.loads(f.read_text())
            t0 = time.monotonic()
            victim = str(info.get("worker", ""))
            p = self.procs.pop(victim, None)
            if p is not None:
                os.kill(p.pid, signal.SIGKILL)
                p.wait(timeout=30)
                self.run.worker_log.append(f"SIGKILL {victim} pid {p.pid}")
            lapsed = self._wait_lapse(str(info["turn_id"]))
            nxt = self._start()
            self.run.kills.append({**k, **info, "killed": p is not None, "restarted": nxt,
                                   "lapsed": lapsed,
                                   "lapse_wait_s": round(time.monotonic() - t0, 3)})

    def _wait_lapse(self, turn_id: str, timeout: float = 60.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.engine.connect() as conn:
                exp = conn.execute(sa.select(s.turns.c.lease_expires_ts).where(
                    s.turns.c.turn_id == turn_id)).scalar()
            if exp is None or exp < clock.wall():
                return True
            time.sleep(0.2)
        return False

    def stop(self) -> None:
        self._halt.set()
        (self.wd / "stop").write_text("stop\n")
        for p in self.procs.values():
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()


# ------------------------------------------------------------------ the API


class Api:
    def __init__(self, client: Any, tenant: str) -> None:
        self.c = client
        k = keys_for(tenant)
        self.h = {"owner": {"Authorization": f"Bearer {k['owner']}"},
                  "channel": {"Authorization": f"Bearer {k['channel']}"}}

    def turn_body(self, mid: str, *, phone: str = "", text: str = "", button_id: str = "",
                  media: dict[str, Any] | None = None, when: datetime,
                  source: str = "shop_message") -> dict[str, Any]:
        body: dict[str, Any] = {"message_id": mid, "source": source, "phone": phone,
                                "text": text, "button_id": button_id,
                                "received_at": when.isoformat(), "simulated": True}
        if media:
            body["media"] = media
        return body

    def post_turn(self, body: dict[str, Any]) -> dict[str, Any]:
        r = self.c.post("/v1/turns?wait=false", json=body, headers=self.h["channel"])
        r.raise_for_status()
        return dict(r.json())

    def event_body(self, eid: str, kind: str, name: str, shop_id: str,
                   when: datetime) -> dict[str, Any]:
        return {"event_id": eid, "kind": kind, "name": name, "shop_id": shop_id,
                "received_at": when.isoformat(), "simulated": True}

    def post_event(self, body: dict[str, Any]) -> dict[str, Any]:
        r = self.c.post("/v1/events", json=body, headers=self.h["channel"])
        r.raise_for_status()
        return dict(r.json())

    def view(self, turn_id: str) -> dict[str, Any]:
        for _ in range(5):
            try:
                return dict(self.c.get(f"/v1/turns/{turn_id}",
                                       headers=self.h["channel"]).json())
            except sa.exc.OperationalError:
                # SQLite: a worker hung inside a write transaction (a planned
                # kill) holds the lock until the monitor kills it. Read again.
                time.sleep(0.5)
        return {"state": "unreadable", "turn_id": turn_id}

    def pending(self) -> list[dict[str, Any]]:
        r = self.c.get("/v1/approvals?status=pending", headers=self.h["owner"])
        return list(r.json().get("approvals", []))

    def decide(self, approval_id: str, what: str) -> tuple[int, str]:
        r = self.c.post(f"/v1/approvals/{approval_id}/{what}?wait=false",
                        json={"note": "week owner decision"}, headers=self.h["owner"])
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") \
            else {}
        follow = (body.get("follow_up") or {}).get("turn_id", "") if isinstance(body, dict) \
            else ""
        return r.status_code, str(follow or "")


# ------------------------------------------------------------------ the week


Action = Callable[[], None]


class Week:
    """One tenant's week (one loop of a soak)."""

    def __init__(self, run: WeekRun, api: Api, engine: Any, world: World, workers: Workers,
                 *, loop: int, tenant: str, clock_file: Path, compress: float,
                 live_ceiling_kobo: int | None, on_wave: Callable[[], None],
                 max_kobo_per_turn: int = 0) -> None:
        self.run, self.api, self.engine, self.world = run, api, engine, world
        self.workers, self.loop, self.tenant = workers, loop, tenant
        self.clock_file, self.compress = clock_file, compress
        self.ceiling = live_ceiling_kobo
        self.max_kobo_per_turn = max_kobo_per_turn
        self.on_wave = on_wave
        self.go = GoSim(engine, world, tenant)
        self.rng = random.Random(f"{run.seed}:{loop}")
        self.shops = {x.shop_id: x for x in world.shops}
        self.heap: list[tuple[datetime, int, str, Action]] = []
        self.seq = 0
        self.day = FIRST_DAY
        self.waiting: list[Sent] = []
        self.react: dict[str, Callable[[Sent], None]] = {}
        self.wave_n = 0

    # --------------------------------------------------------------- plumbing
    def at(self, day: int, hh: int, mm: int = 0) -> datetime:
        d = self.world.date_of(day)
        return datetime(d.year, d.month, d.day, hh, mm, tzinfo=LAGOS)

    def push(self, when: datetime, thread: str, act: Action) -> None:
        self.seq += 1
        heapq.heappush(self.heap, (when, self.seq, thread, act))

    def ext(self, *parts: object) -> str:
        return ".".join(["wk", f"L{self.loop}", f"d{self.day}", *map(str, parts)])

    def _record(self, rec: Sent, body: dict[str, Any], resp: dict[str, Any], *, is_event: bool,
                react: Callable[[Sent], None] | None) -> Sent:
        rec.turn_id, rec.sent_at = str(resp["turn_id"]), time.monotonic()
        self.run.sent.append(rec)
        self.waiting.append(rec)
        if react is not None:
            self.react[rec.turn_id + "#" + rec.ext] = react
        # A seeded 5% are delivered twice (a webhook redelivery).
        if self.rng.random() < 0.05:
            again = self.api.post_event(body) if is_event else self.api.post_turn(body)
            self.run.replays.append({"tenant": self.tenant, "ext": rec.ext,
                                     "first": rec.turn_id, "again": again.get("turn_id"),
                                     "duplicate_flag": bool(again.get("duplicate")),
                                     "same_turn": again.get("turn_id") == rec.turn_id})
        return rec

    def shop_turn(self, scene: str, sid: str, label: str, when: datetime, *, text: str = "",
                  button_id: str = "", media: dict[str, Any] | None = None, path: str = "",
                  react: Callable[[Sent], None] | None = None,
                  before: Action | None = None) -> None:
        def act() -> None:
            if before is not None:
                before()  # Go's part, at the message's own time
            mid = self.ext(sid, scene, label)
            body = self.api.turn_body(mid, phone=self.shops[sid].phone, text=text,
                                      button_id=button_id, media=media, when=when)
            resp = self.api.post_turn(body)
            self._record(Sent(self.loop, self.tenant, self.day, scene, sid, label, mid, "",
                              when.isoformat(), path=path), body, resp, is_event=False,
                         react=react)
        self.push(when, sid, act)

    def owner_turn(self, label: str, text: str, when: datetime,
                   expect: str = "") -> None:
        def act() -> None:
            mid = self.ext("owner", label)
            body = self.api.turn_body(mid, text=text, when=when, source="owner_message")
            resp = self.api.post_turn(body)
            self._record(Sent(self.loop, self.tenant, self.day, "owner", "owner", label, mid,
                              "", when.isoformat()), body, resp, is_event=False, react=None)
        self.push(when, "owner", act)

    def event(self, scene: str, kind: str, name: str, sid: str, when: datetime, label: str,
              *, path: str = "", react: Callable[[Sent], None] | None = None,
              before: Action | None = None) -> None:
        def act() -> None:
            if before is not None:
                before()
            eid = self.ext(sid, scene, label)
            body = self.api.event_body(eid, kind, name, sid, when)
            resp = self.api.post_event(body)
            self._record(Sent(self.loop, self.tenant, self.day, scene, sid, label,
                              f"{kind}:{eid}", "", when.isoformat(), path=path), body, resp,
                         is_event=True, react=react)
        self.push(when, sid, act)

    # --------------------------------------------------------------- waves
    def drive(self, until: datetime) -> None:
        """Send everything due before ``until`` in waves; each wave waits."""
        while self.heap and self.heap[0][0] < until and not self.run.stopped:
            first = self.heap[0][0]
            wave: list[tuple[datetime, int, str, Action]] = []
            later: list[tuple[datetime, int, str, Action]] = []
            seen: set[str] = set()
            while self.heap and self.heap[0][0] <= first + timedelta(minutes=45) \
                    and self.heap[0][0] < until:
                item = heapq.heappop(self.heap)
                (later if item[2] in seen else wave).append(item)
                seen.add(item[2])
            for item in later:
                heapq.heappush(self.heap, item)
            # Move the shared clock only when no turn holds a lease: a jump of
            # simulated hours would lapse a running turn's lease at once (a
            # resumed or timer job can be running between waves).
            no_leases(self.engine)
            set_to(self.clock_file, max(w[0] for w in wave))
            for _, _, _, act in wave:
                self._check_ceiling()  # before every send, not only between waves
                if self.run.stopped:
                    break
                act()
            self.settle()
            self.wave_n += 1
            self.on_wave()
            if self.compress > 0:
                time.sleep(self.compress)

    def settle(self) -> None:
        """Wait until every sent turn has settled, then run the reactions."""
        recs, self.waiting = self.waiting, []
        for rec in recs:
            rec.view = self.wait(rec.turn_id)
            rec.done_at = time.monotonic()
        for rec in recs:
            r = self.react.pop(rec.turn_id + "#" + rec.ext, None)
            if r is not None:
                r(rec)
        self._check_ceiling()

    def wait(self, turn_id: str) -> dict[str, Any]:
        end = time.monotonic() + TURN_TIMEOUT_S
        while True:
            v = self.api.view(turn_id)
            if v.get("state") in ("done", "failed", "parked") or time.monotonic() > end:
                return v
            time.sleep(0.05)

    def _check_ceiling(self) -> None:
        if self.ceiling is None or self.run.stopped:
            return
        from mimi.api.week.meter import total_kobo

        wd = self.run.workdir
        if (wd / "auth-failed").exists():
            self._stop_all(f"Meta API refused the key: {(wd / 'auth-failed').read_text().strip()}"
                           "; run stopped, no retry")
            return
        with self.engine.connect() as conn:
            store = int(conn.execute(sa.select(sa.func.sum(s.spend.c.kobo))).scalar() or 0)
            done = int(conn.execute(sa.select(sa.func.count()).select_from(s.turns).where(
                s.turns.c.state.in_(("done", "merged", "parked")))).scalar() or 0)
        # The meter sees every Meta call (router, readers, SAM); the store
        # only the harness's. The ceiling holds against the larger.
        spent = max(store, total_kobo(wd))
        if self.max_kobo_per_turn and done >= 30 and spent > self.max_kobo_per_turn * done:
            self._stop_all(f"cost per turn too high: {spent} kobo over {done} turns "
                           f"(> {self.max_kobo_per_turn} kobo a turn); run stopped early")
            return
        if spent >= self.ceiling:
            with self.engine.begin() as conn:  # hard stop: every helper of every tenant
                for t in conn.execute(sa.select(s.tenants.c.tenant_id)).scalars():
                    conn.execute(s.kill_switches.insert().values(tenant_id=t, helper="*"))
            self.run.stopped = (f"naira ceiling reached: spent {spent} kobo of "
                                f"{self.ceiling} kobo; kill switch on, run stopped")

    def _stop_all(self, why: str) -> None:
        with self.engine.begin() as conn:
            for t in conn.execute(sa.select(s.tenants.c.tenant_id)).scalars():
                conn.execute(s.kill_switches.insert().values(tenant_id=t, helper="*"))
        self.run.stopped = why

    # --------------------------------------------------------------- the days
    def play(self, days: int = DAYS) -> None:
        for n in range(days):
            if self.run.stopped:
                return
            self.day = FIRST_DAY + n
            if n:
                no_leases(self.engine)
                set_to(self.clock_file, self.at(self.day, 6, 0))
                self.go.open_day(self.day)
            P.plan_day(self, n)
            self.drive(self.at(self.day, 23, 59))
        # Leftovers (late reactions) on the last night.
        self.drive(self.at(self.day, 23, 59) + timedelta(hours=10))

    def owner_decides(self, when: datetime) -> None:
        def act() -> None:
            for a in self.api.pending():
                what = P.decision(self, a)
                if not what:
                    continue
                code, follow = self.api.decide(str(a["approval_id"]), what)
                self.run.decisions.append({"tenant": self.tenant, "day": self.day,
                                           "tool": a["tool"], "shop_id": a.get("shop_id", ""),
                                           "decision": what, "status": code})
                if follow:
                    rec = Sent(self.loop, self.tenant, self.day, "approval", a.get("shop_id")
                               or "owner", f"{a['tool']}:{what}", f"approval:{follow}", follow,
                               when.isoformat(), sent_at=time.monotonic())
                    self.run.sent.append(rec)
                    self.waiting.append(rec)
        self.push(when, "owner-approvals", act)


# ------------------------------------------------------------------ entry


def fresh_store(db_url: str) -> Any:
    from mimi.store.schema import metadata

    engine = make_engine(db_url)
    if not db_url.startswith("sqlite"):
        metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    ensure_schema(engine)
    return engine


def idle(engine: Any) -> bool:
    """Nothing runnable or running (sleeping follow-ups and parked jobs aside)."""
    now = clock.wall()
    with engine.connect() as conn:
        n = conn.execute(sa.select(sa.func.count()).select_from(s.turns).where(
            sa.or_(s.turns.c.state == "leased",
                   sa.and_(s.turns.c.state == "queued",
                           sa.or_(s.turns.c.lease_expires_ts.is_(None),
                                  s.turns.c.lease_expires_ts < now))))).scalar()
    return int(n or 0) == 0


def no_leases(engine: Any, timeout: float = 60.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with engine.connect() as conn:
            n = conn.execute(sa.select(sa.func.count()).select_from(s.turns).where(
                s.turns.c.state == "leased")).scalar()
        if not n:
            return True
        time.sleep(0.05)
    return False


def drain(engine: Any, timeout: float = 120.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if idle(engine):
            return True
        time.sleep(0.2)
    return False


def run_week(*, workdir: Path, db_url: str = "", seed: int = 273, workers: int = 1,
             live: bool = False, max_naira: int | None = None, price_as: str = "muse-spark-1.3",
             soak_hours: float = 0.0, soak_kills: int = 0, week_kills: int = 0,
             max_loops: int = 0, compress: float = 0.0, days: int = DAYS,
             max_naira_per_turn: int = 0) -> WeekRun:
    """One offline week (``soak_hours=0``), or the soak: the week looped for
    ``soak_hours`` with ``soak_kills`` forced worker kills spread over it."""
    from fastapi.testclient import TestClient

    from mimi.api.agent import Agent
    from mimi.api.app import create_app
    from mimi.api.config import Settings, parse_keys
    from mimi.api.helpers import HelperDeps
    from mimi.api.offline import OfflineModel

    if live and (max_naira is None or max_naira <= 0):
        raise SystemExit("--live needs --max-naira N (a hard ceiling on model spend)")
    workdir.mkdir(parents=True, exist_ok=True)
    for f in workdir.glob("killpoint-*"):
        f.unlink()
    (workdir / "stop").unlink(missing_ok=True)
    db_url = db_url or f"sqlite:///{workdir / 'week.db'}"
    if db_url.startswith("sqlite:///"):
        Path(db_url[len("sqlite:///"):]).unlink(missing_ok=True)
    mode = "live" if live else "offline"
    run = WeekRun(mode=mode if not soak_hours else f"{mode}-soak", workdir=workdir,
                  db_url=db_url, seed=seed, started=time.time())
    clock_file = workdir / "simclock.json"
    clock_file.unlink(missing_ok=True)
    prev = clock.set_source(None)
    install(clock_file)
    world = load_world()
    set_to(clock_file, datetime(2026, 10, 26, 6, 0, tzinfo=LAGOS), allow_back=True)
    engine = fresh_store(db_url)
    ceiling = max_naira * 100 if (live and max_naira) else None
    n_loops = max_loops or (10_000 if soak_hours else 1)
    tenants = [tenant_for(i) for i in range(min(n_loops, 400))]
    keys = parse_keys(",".join(f"{t}:owner:{keys_for(t)['owner']},"
                               f"{t}:channel:{keys_for(t)['channel']}" for t in tenants))
    settings = Settings(database_url=db_url, keys=keys, workers=0, lease_s=LEASE_S)
    api_agent = Agent(engine, models=[OfflineModel()], router_model=None, deps=HelperDeps(),
                      lease_s=LEASE_S)
    pool = Workers(run, n=workers, live=live, price_as=price_as, ceiling_kobo=ceiling)
    pool.engine = engine
    deadline = run.started + soak_hours * 3600
    kill_times = [run.started + (i + 0.5) * (soak_hours * 3600) / soak_kills
                  for i in range(soak_kills)] if soak_hours and soak_kills else []
    points = ("step:1", "route", "finish", "step:2")
    kill_n = [0]

    waves = [0]

    def on_wave() -> None:
        # One armed kill at a time (two hung workers would hold SQLite's write
        # lock against each other). The soak arms on the wall clock, spread
        # over the hours; the one week arms every KILL_EVERY waves.
        waves[0] += 1
        if pool.pending_kills():
            return
        if kill_times and time.time() >= kill_times[0]:
            kill_times.pop(0)
            kill_n[0] += 1
            pool.arm(f"s{kill_n[0]:03d}", points[(kill_n[0] - 1) % len(points)])
        elif not soak_hours and kill_n[0] < week_kills and waves[0] % KILL_EVERY == 0:
            kill_n[0] += 1
            pool.arm(f"w{kill_n[0]:02d}", points[(kill_n[0] - 1) % len(points)])

    try:
        with TestClient(create_app(settings, agent=api_agent, workers=0)) as client:
            pool.start_all()
            loop = 0
            while loop < n_loops and not run.stopped:
                tenant = tenant_for(loop)
                if loop:
                    if not drain(engine):
                        run.worker_log.append(f"loop {loop}: queue did not drain in time")
                    set_to(clock_file, datetime(2026, 10, 26, 6, 0, tzinfo=LAGOS),
                           allow_back=True)
                seed_world(engine, world, tenant_id=tenant, daily_cap_kobo=(
                    min(DAILY_CAP_KOBO, ceiling) if ceiling else DAILY_CAP_KOBO))
                before = book_rows(engine, tenant)
                api = Api(client, tenant)
                wk = Week(run, api, engine, world, pool, loop=loop, tenant=tenant,
                          clock_file=clock_file, compress=compress, live_ceiling_kobo=ceiling,
                          on_wave=on_wave, max_kobo_per_turn=max_naira_per_turn * 100)
                t0 = time.time()
                wk.play(days)
                drain(engine)
                run.books[tenant] = {"before": before, "after": book_rows(engine, tenant),
                                     "go": {k: list(v) for k, v in vars(wk.go.ledger).items()}}
                run.loops.append({"loop": loop, "tenant": tenant, "waves": wk.wave_n,
                                  "seconds": round(time.time() - t0, 1)})
                loop += 1
                save_run(run)  # after every loop, so a long soak's records survive a crash
                if soak_hours and time.time() >= deadline and not pool.pending_kills():
                    break
                if soak_hours and time.time() >= deadline + 1800:
                    run.worker_log.append("soak: kills still armed 30 min past the deadline")
                    break
            # Drain: move the clock past every sleeping follow-up, let them run.
            set_to(clock_file, datetime(2026, 11, 4, 12, 0, tzinfo=LAGOS))
            drain(engine, timeout=300)
    finally:
        pool.stop()
        run.finished = time.time()
        clock.set_source(prev)
        save_run(run)
    return run

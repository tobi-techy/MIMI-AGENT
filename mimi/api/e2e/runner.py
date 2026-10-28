"""The e2e run: ten simulated shops talk to the real API, the way the demo does.

Everything goes through the HTTP API (``POST /v1/turns``, ``POST /v1/events``,
``/v1/approvals``, the console reads) on an in-process ``TestClient``, with
the durable store (a new SQLite file, or a throwaway Postgres database with
``--db``) and real worker *processes* (``worker.py``) taking turns off the
queue. The API process itself runs no worker.

Eleven forced kills (RAI-272, ``scenes.KILL_PLAN``): a worker hangs at a
fixed crash point (after a route, after a step, inside the inbox-absorb or
the completing transaction, right after a budget resume); a monitor thread
SIGKILLs it, waits for its lease to lapse and starts the next worker, which
takes the job over from its checkpoint.

The run, in order:

1. Seed the synthetic Coral Lane world (and a second distributor, Palm Way,
   for the cross-tenant checks) into a fresh database.
2. Start worker A. It is told to crash (hang, then be SIGKILLed) after the
   first saved step of shop-012's weekly stock check.
3. Receipt scenes, then proof scenes, each message waiting for its turn.
4. Shop-012's stock check: worker A hangs mid-turn holding the lease. While
   it does, two credit asks, shop-014's stock check and an owner message are
   queued (owner priority). A is SIGKILLed; once its lease lapses worker B
   starts, takes over shop-012's turn from its checkpoint, then the owner's
   message, then the shop turns.
5. The rest of the stock and credit scenes.
6. The owner approves and rejects through ``/v1/approvals`` (and a second
   decision on one item must be refused), and the woken helpers run.
7. Two open asks on shop-011, Brand's Promo Check, then the daily naira
   cap: two shops are parked, the owner approves more allowance through
   ``/v1/approvals`` and both jobs resume (RAI-272).
8. The console reads and the cross-tenant probes.

``collect`` returns everything the scorer needs; nothing is scored here.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from mimi.api.e2e import scenes as sc
from mimi.api.e2e.models import CachePaths, persona_model
from mimi.api.e2e.worker import LEASE_S, SIM_NOW
from mimi.store import schema as s
from mimi.store.db import ensure_schema, make_engine
from mimi.store.queue import turn_id_for
from mimi.store.seed import seed_world
from mimi.world.loader import load_world

LAGOS = ZoneInfo("Africa/Lagos")
DAY = datetime(2026, 10, 26, tzinfo=LAGOS)
KEYS = {"owner": "e2e-owner-key-coral-0001", "channel": "e2e-channel-key-coral-01",
        "other_owner": "e2e-owner-key-palmway-01"}
OTHER_TENANT = "palm-way"
TURN_TIMEOUT_S = 240.0


def received(iso: str) -> datetime:
    """A fixture's ISO time (with its zone) as an aware datetime in WAT."""
    return datetime.fromisoformat(iso).astimezone(LAGOS)


def at(hh: int, mm: int) -> datetime:
    return DAY.replace(hour=hh, minute=mm)


@dataclass
class TurnRec:
    scene: str
    shop_id: str
    label: str
    turn_id: str
    expected: tuple[str, ...]  # acceptable "helper/skill" routes
    sent_at: float = 0.0
    done_at: float = 0.0
    view: dict[str, Any] = field(default_factory=dict)
    phase: str = "normal"  # normal | burst | killed | approval


@dataclass
class Run:
    live: bool
    workdir: Path
    db_url: str
    turns: list[TurnRec] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    book_before: dict[str, Any] = field(default_factory=dict)
    persona_calls: int = 0
    persona_live_calls: int = 0
    persona_tokens: tuple[int, int] = (0, 0)
    worker_log: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ plumbing


class Workers:
    def __init__(self, run: Run, cache_dir: Path, engine: Any = None) -> None:
        self.run, self.cache_dir, self.engine = run, cache_dir, engine
        self.proc: subprocess.Popen[bytes] | None = None
        self.stop_file = run.workdir / "stop"
        self.plan_file = run.workdir / "kill-plan.json"
        self.plan_file.write_text(json.dumps(sc.KILL_PLAN, indent=1) + "\n")
        self.handled: set[str] = set()
        self.n = 0
        # The monitor runs in its own thread: a worker that hangs inside a write
        # transaction holds SQLite's lock, and a read on the runner's thread
        # would wait on it. Files only, until the worker is dead.
        self.lock = threading.RLock()
        self._halt = threading.Event()
        self.monitor = threading.Thread(target=self._watch, daemon=True, name="kill-monitor")

    def _watch(self) -> None:
        while not self._halt.wait(0.05):
            try:
                self.service()
            except Exception as exc:  # recorded; the scorer sees the kill did not run
                self.run.worker_log.append(f"monitor error: {exc!r}")

    def service(self) -> None:
        with self.lock:
            self._service()

    def _service(self) -> None:
        """A planned crash point fired: SIGKILL the worker, let its lease lapse,
        start the next worker (it takes the turn over from its checkpoint)."""
        for k in sc.KILL_PLAN:
            f = self.run.workdir / f"killpoint-{k['id']}"
            if k.get("manual") or k["id"] in self.handled or not f.exists():
                continue
            self.handled.add(str(k["id"]))
            info = json.loads(f.read_text())
            t0 = time.monotonic()
            self.kill()
            lapsed = self.wait_lapse(str(info["turn_id"]))
            self.start(f"worker-{chr(ord('c') + self.n)}")
            self.n += 1
            self.run.checks.setdefault("kills", []).append(
                {**k, **info, "lapse_wait_s": round(time.monotonic() - t0, 3),
                 "lapsed": lapsed})

    def wait_lapse(self, turn_id: str, timeout: float = 60.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.engine.connect() as conn:
                exp = conn.execute(sa.select(s.turns.c.lease_expires_ts).where(
                    s.turns.c.turn_id == turn_id)).scalar()
            if exp is None or exp < time.time():
                return True
            time.sleep(0.2)
        return False

    def start(self, name: str, pause: str = "", marker: Path | None = None) -> None:
        cmd = [sys.executable, "-m", "mimi.api.e2e", "worker", "--db", self.run.db_url,
               "--cache-dir", str(self.cache_dir), "--worker-id", name,
               "--stop-file", str(self.stop_file), "--kill-plan", str(self.plan_file),
               "--usage", str(self.run.workdir / "usage.jsonl")]
        if self.run.live:
            cmd.append("--live")
        if pause:
            cmd += ["--pause-at", pause, "--marker", str(marker)]
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        log = open(self.run.workdir / f"{name}.log", "wb")  # noqa: SIM115
        self.proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
        self.run.worker_log.append(f"started {name}")

    def kill(self) -> None:
        assert self.proc is not None
        os.kill(self.proc.pid, signal.SIGKILL)
        self.proc.wait(timeout=30)
        self.run.worker_log.append(f"SIGKILL pid {self.proc.pid}")

    def stop(self) -> None:
        self._halt.set()
        self.stop_file.write_text("stop\n")
        if self.proc is not None:
            try:
                self.proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class Api:
    def __init__(self, client: Any, tick: Any = None) -> None:
        self.c = client
        self.h = {k: {"Authorization": f"Bearer {v}"} for k, v in KEYS.items()}
        self.tick = tick or (lambda: None)

    def turn(self, mid: str, *, phone: str = "", text: str = "", button_id: str = "",
             media: dict[str, Any] | None = None, when: datetime,
             source: str = "shop_message") -> str:
        body: dict[str, Any] = {"message_id": mid, "source": source, "phone": phone,
                                "text": text, "button_id": button_id,
                                "received_at": when.isoformat(), "simulated": True}
        if media:
            body["media"] = media
        r = self.c.post("/v1/turns?wait=false", json=body, headers=self.h["channel"])
        r.raise_for_status()
        return str(r.json()["turn_id"])

    def event(self, eid: str, kind: str, name: str, shop_id: str, when: datetime) -> str:
        r = self.c.post("/v1/events", json={"event_id": eid, "kind": kind, "name": name,
                                            "shop_id": shop_id,
                                            "received_at": when.isoformat(),
                                            "simulated": True}, headers=self.h["channel"])
        r.raise_for_status()
        return str(r.json()["turn_id"])

    def get(self, path: str, who: str = "owner") -> Any:
        return self.c.get(path, headers=self.h[who])

    def wait(self, turn_id: str, timeout: float = TURN_TIMEOUT_S,
             states: tuple[str, ...] = ("done", "failed")) -> dict[str, Any]:
        end = time.monotonic() + timeout
        while True:
            self.tick()  # a planned worker kill fired: kill and restart
            v = self.get(f"/v1/turns/{turn_id}", "channel").json()
            if v.get("state") in states or time.monotonic() > end:
                return dict(v)
            time.sleep(0.1)


def shop_replies(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in view.get("replies", []) if r["channel"] == "shop_whatsapp"]


def last_shop_text(view: dict[str, Any]) -> str:
    rs = shop_replies(view)
    return str(rs[-1]["text"]) if rs else ""


# ------------------------------------------------------------------ the run


def book_digest(engine: Any, tenant: str) -> dict[str, Any]:
    """What must never change: limits, payments, bank feed, what is owed."""
    with engine.connect() as conn:
        out: dict[str, Any] = {
            "limits": {r.shop_id: int(r.credit_limit_kobo) for r in conn.execute(
                sa.select(s.shops.c.shop_id, s.shops.c.credit_limit_kobo).where(
                    s.shops.c.tenant_id == tenant))},
        }
        for t, col in ((s.payments, "amount_kobo"), (s.bank_feed, "amount_kobo"),
                       (s.receivables, "outstanding_kobo"), (s.deliveries, "total_kobo")):
            n, total = conn.execute(sa.select(sa.func.count(), sa.func.sum(t.c[col])).where(
                t.c.tenant_id == tenant)).one()
            out[t.name] = [int(n), int(total or 0)]
    return out


def fresh_store(db_url: str) -> Any:
    """An empty store for one run: a new SQLite file, or every table of the
    given Postgres database dropped and created again (a throwaway CI database)."""
    from mimi.store.schema import metadata

    engine = make_engine(db_url)
    if not db_url.startswith("sqlite"):
        metadata.drop_all(engine)
        with engine.begin() as conn:  # alembic's own table, if a migration ran first
            conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    ensure_schema(engine)
    return engine


def collect(*, live: bool, cache_dir: Path, workdir: Path | None = None,
            db_url: str = "") -> Run:
    from fastapi.testclient import TestClient

    from mimi.api.agent import Agent
    from mimi.api.app import create_app
    from mimi.api.config import Settings, parse_keys
    from mimi.api.helpers import HelperDeps
    from mimi.api.offline import OfflineModel

    wd = workdir or Path(tempfile.mkdtemp(prefix="mimi-e2e-"))
    db_url = db_url or f"sqlite:///{wd / 'e2e.db'}"
    run = Run(live=live, workdir=wd, db_url=db_url)
    world = load_world()
    engine = fresh_store(db_url)
    # The run's model spend passed the default N500 daily cap once the steering
    # scenes were added (RAI-301): the synthetic tenant gets N3,000 so the run
    # measures the scenes; the cap scene near the end lowers it on purpose.
    seed_world(engine, world, daily_cap_kobo=300_000)
    seed_world(engine, world, tenant_id=OTHER_TENANT, name="Palm Way Foods")
    run.book_before = book_digest(engine, sc.TENANT)
    keys = parse_keys(f"{sc.TENANT}:owner:{KEYS['owner']},{sc.TENANT}:channel:{KEYS['channel']},"
                      f"{OTHER_TENANT}:owner:{KEYS['other_owner']}")
    settings = Settings(database_url=db_url, keys=keys, workers=0, lease_s=LEASE_S)
    api_agent = Agent(engine, models=[OfflineModel()], router_model=None, deps=HelperDeps(),
                      lease_s=LEASE_S, clock=lambda: SIM_NOW)
    persona = persona_model(live, CachePaths(cache_dir))
    workers = Workers(run, cache_dir, engine)
    shops = {x.shop_id: x for x in world.shops}
    stock_turn = turn_id_for(sc.TENANT, "routine:e2e.stock.shop-012.1")
    marker = wd / f"killpoint-{sc.MANUAL_KILL}"
    workers.start("worker-a")
    with TestClient(create_app(settings, agent=api_agent, workers=0)) as client:
        api = Api(client)
        workers.monitor.start()

        def send(rec: TurnRec, turn_id: str, *, wait: bool = True) -> TurnRec:
            rec.turn_id, rec.sent_at = turn_id, time.monotonic()
            run.turns.append(rec)
            if wait:
                finish(rec)
            return rec

        def finish(rec: TurnRec) -> TurnRec:
            rec.view = api.wait(rec.turn_id)
            rec.done_at = time.monotonic()
            return rec

        # ---------------------------------------------------- receipts
        for sid in sc.RECEIPT_SHOPS:
            d = next(x for x in world.live_deliveries if x.shop_id == sid)
            t0 = at(d.minute // 60, d.minute % 60)
            route = ("order/drop-confirmation",)
            r = send(TurnRec("receipt", sid, "delivery_completed", "", route),
                     api.event(f"e2e.dlv.{d.delivery_id}", "webhook", "delivery_completed",
                               sid, t0))
            from mimi.sim.persona import ShopPersona

            p = ShopPersona(world, shops[sid], persona, seed=sc.SEED)
            mimi_said = last_shop_text(r.view)
            for n in range(2):
                if not mimi_said:
                    break
                lines = p.reply(mimi_said, d)
                text = "\n".join(m.text for m in lines) or "ok"
                r = send(TurnRec("receipt", sid, f"reply{n + 1}", "", route),
                         api.turn(f"e2e.{sid}.receipt.{n + 1}", phone=shops[sid].phone,
                                  text=text, when=t0 + timedelta(minutes=20 + 60 * n)))
                asked_again = any(x["kind"] == "ask_receipt_again" for x in
                                  shop_replies(r.view))
                mimi_said = last_shop_text(r.view) if asked_again else ""

        # ------------------------------------- steer: a two-part receipt reply
        sid = sc.MULTIPART_SHOP
        d = next(x for x in world.live_deliveries if x.shop_id == sid)
        t0 = at(d.minute // 60, d.minute % 60)
        route = ("order/drop-confirmation",)
        send(TurnRec("steer_multipart", sid, "delivery_completed", "", route),
             api.event(f"e2e.dlv.{d.delivery_id}", "webhook", "delivery_completed", sid, t0))
        mid = f"e2e.{sid}.receipt.multi"
        send(TurnRec("steer_multipart", sid, "reply", "", route),
             api.turn(mid, phone=shops[sid].phone, text=sc.MULTIPART_TEXT,
                      when=t0 + timedelta(minutes=15)))
        # The balance question is its own turn, queued right after (RAI-301).
        fu = turn_id_for(sc.TENANT, f"{mid}#p2")
        with engine.connect() as conn:
            exists = conn.execute(sa.select(s.turns.c.turn_id).where(
                s.turns.c.turn_id == fu)).first() is not None
        if exists:
            send(TurnRec("steer_multipart", sid, "followup", "",
                         ("collector/collections", "collector/payment-check")), fu)
        run.checks["multipart_followup"] = fu if exists else ""

        # ---------------------------------------------------- proof
        for sid, screens in sc.PROOF_SHOPS.items():
            for i, scr in enumerate(screens):
                img = sc.screenshot(scr)
                cap = sc.caption(persona, world, shops[sid], scr)
                when = received(str(img.meta["received_at"]))
                send(TurnRec("proof", sid, scr, "", ("proof/fake-alert-check",)),
                     api.turn(f"e2e.{sid}.proof.{i + 1}", phone=shops[sid].phone,
                              media={"kind": "image", "ref": img.ref, "mime": "image/jpeg",
                                     "sha256": img.sha256, "width": img.width,
                                     "height": img.height, "camera_exif": False,
                                     "caption": cap}, when=when))

        # ---------------------------------------------- stock, kill, burst
        stock: dict[str, TurnRec] = {}
        stock["shop-012"] = send(
            TurnRec("stock", "shop-012", "weekly_check", "", ("stock/stock-count",),
                    phase="killed"),
            api.event("e2e.stock.shop-012.1", "routine", "stock.weekly_check", "shop-012",
                      at(7, 30)), wait=False)
        end = time.monotonic() + TURN_TIMEOUT_S
        while not marker.exists() and time.monotonic() < end:
            time.sleep(0.1)
        paused = json.loads(marker.read_text()) if marker.exists() else {}
        run.checks["paused_at"] = f"{paused.get('turn_id', '')}:{paused.get('at', '')}"
        # Worker A now hangs mid-turn holding the lease. Queue the burst.
        asks = {sid: sc.credit_ask(persona, world, shops[sid]) for sid in sc.CREDIT_SHOPS}
        burst: list[TurnRec] = []
        for sid in sc.CREDIT_SHOPS:
            burst.append(send(TurnRec("credit", sid, "ask", "", ("credit/credit-request",),
                                      phase="burst"),
                              api.turn(f"e2e.{sid}.credit.1", phone=shops[sid].phone,
                                       text=asks[sid] or "abeg increase my credit",
                                       when=at(10, 0)), wait=False))
        stock["shop-014"] = send(
            TurnRec("stock", "shop-014", "weekly_check", "", ("stock/stock-count",),
                    phase="burst"),
            api.event("e2e.stock.shop-014.1", "routine", "stock.weekly_check", "shop-014",
                      at(7, 31)), wait=False)
        burst.append(stock["shop-014"])
        owner = send(TurnRec("owner", "", "owner_message", "",
                             ("mimi/owner-brief", "mimi/chat"), phase="burst"),
                     api.turn("e2e.owner.1", source="owner_message",
                              text="Good evening. How is business today?", when=at(19, 0)),
                     wait=False)
        burst.append(owner)
        t_kill = time.monotonic()
        with workers.lock:
            workers.kill()
            # Let the dead worker's lease lapse, then start the next worker.
            lapsed = workers.wait_lapse(stock_turn)
            workers.start("worker-b")
            workers.handled.add(sc.MANUAL_KILL)
        run.checks.setdefault("kills", []).append(
            {**next(k for k in sc.KILL_PLAN if k["id"] == sc.MANUAL_KILL), **paused,
             "lapse_wait_s": round(time.monotonic() - t_kill, 3), "lapsed": lapsed})
        finish(stock["shop-012"])
        for b in burst:
            finish(b)
        run.checks["burst_ids"] = [b.turn_id for b in burst]
        run.checks["killed_turn"] = stock_turn

        def midtask(sid: str, text: str, button: str) -> TurnRec:
            """RAI-301: the stock answer turn is held after its first saved state;
            the shop's voice note arrives while it runs and goes to the inbox."""
            ext = f"e2e.{sid}.stock.1"
            tid = turn_id_for(sc.TENANT, ext)
            hold, held = wd / f"hold-{tid}", wd / f"held-{tid}"
            hold.write_text("hold\n")
            rec = send(TurnRec("stock", sid, "answer1", "", ("stock/stock-count",)),
                       api.turn(ext, phone=shops[sid].phone, text=text, button_id=button,
                                when=at(8, 10)), wait=False)
            end = time.monotonic() + TURN_TIMEOUT_S
            while not held.exists() and time.monotonic() < end:
                time.sleep(0.05)
            run.checks["midtask_held"] = held.exists()
            voice = {"kind": "voice", "mime": "audio/ogg", "transcript": sc.MIDTASK_VOICE,
                     "sha256": hashlib.sha256(sc.MIDTASK_VOICE.encode()).hexdigest()}
            vrec = send(TurnRec("steer_midtask", sid, "voice_correction", "",
                                ("stock/stock-count",)),
                        api.turn(f"e2e.{sid}.stock.voice", phone=shops[sid].phone,
                                 media=voice, when=at(8, 12)), wait=False)
            # Let the inbox row land before the turn takes its next step.
            time.sleep(0.3)
            hold.unlink()
            finish(rec)
            finish(vrec)
            run.checks["midtask_voice_turn"] = vrec.turn_id
            run.checks["midtask_stock_turn"] = rec.turn_id
            return rec

        # ---------------------------------------------------- stock answers
        from mimi.stock.truth import generate_truth

        truth = generate_truth(world).items
        for sid in sc.STOCK_SHOPS:
            rec = stock[sid]
            ask = next((x for x in shop_replies(rec.view) if x["kind"] == "ask_stock_count"),
                       None)
            asked, n = (str(ask["text"]) if ask else ""), 0
            while asked and n < 2:
                sku = _sku_for(engine, sid)
                d_name, unit = _item(world, sid, sku)
                left = truth[(sid, sku)].counted(world.live_day) if (sid, sku) in truth else 0
                text = sc.stock_answer(persona, world, shops[sid], d_name, unit, left, asked, n)
                text = text.strip() or "0-2"
                button = "btn:stock/stock-count" if text in ("0-2", "3-5", "6+") else ""
                if sid == sc.MIDTASK_SHOP and n == 0:
                    r = midtask(sid, text, button)
                else:
                    r = send(TurnRec("stock", sid, f"answer{n + 1}", "", ("stock/stock-count",)),
                             api.turn(f"e2e.{sid}.stock.{n + 1}", phone=shops[sid].phone,
                                      text=text, button_id=button,
                                      when=at(8, 10 + 40 * n)))
                again = next((x for x in shop_replies(r.view)
                              if x["kind"] == "ask_stock_count"), None)
                asked, n = (str(again["text"]) if again else ""), n + 1

        # ---------------------------------------------------- credit photos
        for b in burst:
            if b.scene != "credit":
                continue
            sid = b.shop_id
            asked = last_shop_text(b.view)
            if not asked:
                continue
            img = sc.shelf(sc.CREDIT_SHOPS[sid])
            cap = sc.shelf_caption(persona, world, shops[sid], asked)
            send(TurnRec("credit", sid, "photo", "", ("credit/credit-request",)),
                 api.turn(f"e2e.{sid}.credit.2", phone=shops[sid].phone,
                          media={"kind": "image", "ref": img.ref, "mime": "image/jpeg",
                                 "sha256": img.sha256, "width": img.width,
                                 "height": img.height, "camera_exif": True, "caption": cap},
                          when=at(10, 25)))

        # ------------------------- steer: a screenshot while credit is parked
        sid = sc.PARKED_SHOP
        img = sc.screenshot(sc.PARKED_SCREEN)
        run.checks["parked_before"] = [
            a for a in api.get("/v1/approvals?status=pending").json()["approvals"]
            if a["shop_id"] == sid and a["tool"] == "submit_credit_review"]
        send(TurnRec("steer_parked", sid, "screenshot", "", ("proof/fake-alert-check",)),
             api.turn(f"e2e.{sid}.parked.1", phone=shops[sid].phone,
                      media={"kind": "image", "ref": img.ref, "mime": "image/jpeg",
                             "sha256": img.sha256, "width": img.width, "height": img.height,
                             "camera_exif": False, "caption": sc.PARKED_CAPTION},
                      when=at(10, 40)))
        run.checks["parked_after"] = [
            a for a in api.get("/v1/approvals?status=all").json()["approvals"]
            if a["shop_id"] == sid and a["tool"] == "submit_credit_review"]

        # ------------- two open asks on one thread: a bare YES binds to neither
        sid = sc.WHICH_SHOP
        d = next(x for x in world.live_deliveries if x.shop_id == sid)
        route = ("order/drop-confirmation",)
        send(TurnRec("which", sid, "delivery_completed", "", route),
             api.event(f"e2e.dlv.{d.delivery_id}", "webhook", "delivery_completed", sid,
                       at(10, 45)))
        for n, (label, text, exp) in enumerate(sc.WHICH_STEPS):
            send(TurnRec("which", sid, label, "", exp),
                 api.turn(f"e2e.{sid}.which.{n + 1}", phone=shops[sid].phone, text=text,
                          when=at(10, 50 + 2 * n)))

        # ---------------------------------------- Brand: the Promo Check helper
        sid = sc.BRAND_SHOP
        send(TurnRec("brand", sid, "promo_question", "", ("brand/promo-info",)),
             api.turn(f"e2e.{sid}.brand.1", phone=shops[sid].phone, text=sc.BRAND_SHOP_TEXT,
                      when=at(11, 5)))
        send(TurnRec("brand", "owner", "brand_email", "", ("brand/brand-email",)),
             api.turn("e2e.owner.brand.1", source="owner_message", text=sc.BRAND_OWNER_TEXT,
                      when=at(19, 5)))

        # ---------------------------------------------------- the owner decides
        pending = api.get("/v1/approvals?status=pending").json()["approvals"]
        run.checks["approvals_before"] = pending
        decisions: dict[str, str] = {}
        plan = {("submit_credit_review", "shop-011"): "approve",
                ("submit_credit_review", "shop-020"): "reject",
                ("draft_restock", "shop-012"): "approve",
                ("draft_restock", "shop-014"): "reject",
                ("revise_restock", "shop-014"): "reject"}
        for a in pending:
            what = plan.get((a["tool"], a["shop_id"]))
            if what is None:
                continue
            resp = client.post(f"/v1/approvals/{a['approval_id']}/{what}?wait=false",
                               json={"note": "e2e owner decision"}, headers=api.h["owner"])
            decisions[a["approval_id"]] = f"{what}:{resp.status_code}"
            body = resp.json()
            follow = (body.get("follow_up") or {}).get("turn_id") if isinstance(body, dict) \
                else None
            if follow:
                send(TurnRec("approval", a["shop_id"], f"{a['tool']}:{what}", "",
                             (f"{a['helper']}/{a['skill'].split('/', 1)[-1]}",),
                             phase="approval"), follow)
        run.checks["decisions"] = decisions
        if decisions:
            first = next(iter(decisions))
            twice = client.post(f"/v1/approvals/{first}/approve?wait=false", json={},
                                headers=api.h["owner"])
            run.checks["second_decision_status"] = twice.status_code
        # --------------- the daily naira cap: helpers pause, the owner resumes them
        with engine.begin() as conn:
            # The day's model allowance runs out here: the cap is set to what the
            # run has spent so far (the next step of any helper is over it).
            spent = int(conn.execute(sa.select(s.spend.c.kobo).where(
                s.spend.c.tenant_id == sc.TENANT, s.spend.c.day == DAY.date().isoformat())
            ).scalar() or 0)
            conn.execute(s.tenants.update().where(s.tenants.c.tenant_id == sc.TENANT)
                         .values(daily_cap_kobo=max(1, spent)))
        run.checks["budget_cap_set_kobo"] = max(1, spent)
        parked: list[TurnRec] = []
        for n, (sid, text) in enumerate(sc.BUDGET_SHOPS):
            rec = send(TurnRec("budget", sid, "ask", "", ("collector/collections",
                                                          "collector/payment-check")),
                       api.turn(f"e2e.budget.{sid}.1", phone=shops[sid].phone, text=text,
                                when=at(21, 0 + n)), wait=False)
            rec.view = api.wait(rec.turn_id, states=("parked", "done", "failed"))
            run.checks.setdefault("budget_parked_views", []).append(dict(rec.view))
            parked.append(rec)
        raise_items = [a for a in api.get("/v1/approvals?status=pending").json()["approvals"]
                       if a["tool"] == "raise_daily_allowance"]
        run.checks["budget_raise_items"] = raise_items
        if raise_items:
            r = client.post(f"/v1/approvals/{raise_items[0]['approval_id']}/approve?wait=false",
                            json={"note": "e2e owner raises the allowance"},
                            headers=api.h["owner"])
            run.checks["budget_decision_status"] = r.status_code
        for rec in parked:
            finish(rec)
        with engine.connect() as conn:
            run.checks["budget_cap_after_kobo"] = int(conn.execute(sa.select(
                s.tenants.c.daily_cap_kobo).where(s.tenants.c.tenant_id == sc.TENANT)).scalar())

        # ---------------------------------------------------- console reads
        run.checks["threads"] = api.get("/v1/threads?limit=200").json()
        run.checks["shops"] = api.get("/v1/shops").json()
        run.checks["approvals_all"] = api.get("/v1/approvals?status=all").json()
        run.checks["credit"] = api.get("/v1/credit").json()
        details = {}
        for sid in sc.ALL_SHOPS:
            tid = f"{sc.TENANT}:shop:{sid}"
            details[sid] = api.get(f"/v1/threads/{tid}").json()
        run.checks["thread_details"] = details
        # ---------------------------------------------------- cross-tenant probes
        probe_turn = run.turns[0].turn_id
        apr = pending[0]["approval_id"] if pending else "apr_none"
        tid = f"{sc.TENANT}:shop:{sc.ALL_SHOPS[0]}"
        run.checks["cross_tenant"] = {
            "thread": api.get(f"/v1/threads/{tid}", "other_owner").status_code,
            "turn": api.get(f"/v1/turns/{probe_turn}", "other_owner").status_code,
            "approvals_seen": len(api.get("/v1/approvals?status=all", "other_owner")
                                  .json()["approvals"]),
            "threads_seen": [t["thread_id"] for t in api.get("/v1/threads", "other_owner")
                             .json()["threads"] if t["thread_id"].startswith(sc.TENANT)],
            "approve_other": client.post(f"/v1/approvals/{apr}/approve?wait=false", json={},
                                         headers=api.h["other_owner"]).status_code,
            "shop": api.get(f"/v1/shops/{sc.ALL_SHOPS[0]}", "other_owner").json().get(
                "name", ""),
        }
        run.checks["health"] = client.get("/v1/health").json()
    workers.stop()
    run.persona_calls = persona.calls
    run.persona_live_calls = persona.live_calls
    run.persona_tokens = (persona.prompt_tokens, persona.completion_tokens)
    run.checks["db"] = db_facts(engine)
    run.checks["book_after"] = book_digest(engine, sc.TENANT)
    return run


def _sku_for(engine: Any, shop_id: str) -> str:
    """The sku of the shop's newest stock ask (what the persona is asked about)."""
    with engine.connect() as conn:
        rows = conn.execute(sa.select(s.intents.c.payload).where(
            s.intents.c.tenant_id == sc.TENANT, s.intents.c.shop_id == shop_id,
            s.intents.c.tool == "ask_stock_count").order_by(s.intents.c.created_at)).all()
    return str(json.loads(rows[-1].payload)["sku"]) if rows else ""


def _item(world: Any, shop_id: str, sku: str) -> tuple[str, str]:
    for d in world.deliveries:
        if d.shop_id == shop_id:
            for ln in d.lines:
                if ln.sku == sku:
                    return ln.name, ln.unit
    return sku, ""


def db_facts(engine: Any) -> dict[str, Any]:
    """Raw rows the scorer reads (this run's store only)."""
    with engine.connect() as conn:
        def rows(t: sa.Table, *cond: Any) -> list[dict[str, Any]]:
            return [dict(r._mapping) for r in conn.execute(sa.select(t).where(*cond))]

        return {
            "turns": rows(s.turns),
            "attempts": rows(s.turn_attempts),
            "messages": rows(s.messages),
            "intents": rows(s.intents),
            "approvals": rows(s.approvals),
            "credit_requests": rows(s.credit_requests),
            "events": [dict(r._mapping) for r in conn.execute(sa.select(
                s.thread_events.c.tenant_id, s.thread_events.c.turn_id, s.thread_events.c.type,
                s.thread_events.c.tool, s.thread_events.c.cost_kobo, s.thread_events.c.model,
                s.thread_events.c.data, s.thread_events.c.event_id))],
            "inbound": rows(s.inbound_messages),
            "inbox": rows(s.thread_inbox),
        }

"""Check the demo script's scenes (docs/DEMO-SCRIPT.md) against an offline replay. N0.

    python scripts/demo_script_check.py                 # run the e2e replay, then check
    python scripts/demo_script_check.py --workdir DIR   # check an e2e run already in DIR

The e2e replay (``python -m mimi.api.e2e``) drives the real HTTP API, the
durable store and worker processes on recorded Muse Spark, SAM 3.1 and persona
answers, so nothing is spent. This script then reads that run's store and
checks that every line the demo script quotes for scenes 1 to 4 is what Mimi
actually sent, and that the owner scene routes without a model call. It exits
1 when a scene does not hold. All data is synthetic (Coral Lane Trading).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TENANT = "coral-lane"


@dataclass
class Check:
    scene: str
    what: str
    ok: bool
    got: str = ""


def _out(db: sqlite3.Connection, shop: str) -> list[tuple[str, str]]:
    return [(k, t) for k, t in db.execute(
        "select kind, text from messages where tenant_id=? and shop_id=? and direction='out' "
        "order by ts", (TENANT, shop))]


def _in(db: sqlite3.Connection, shop: str) -> list[str]:
    return [t for (t,) in db.execute(
        "select text from messages where tenant_id=? and shop_id=? and direction='in' "
        "and sender='shop' order by ts", (TENANT, shop))]


def _has(rows: list[tuple[str, str]], kind: str, start: str) -> tuple[bool, str]:
    for k, t in rows:
        if k == kind and t.startswith(start):
            return True, t
    return False, " | ".join(f"{k}: {t[:80]}" for k, t in rows)


def _approval(db: sqlite3.Connection, shop: str, tool: str) -> dict[str, object] | None:
    row = db.execute("select status, payload from approvals where tenant_id=? and shop_id=? "
                     "and tool=? order by created_ts", (TENANT, shop, tool)).fetchone()
    if row is None:
        return None
    return {"status": row[0], **json.loads(row[1])}


def check(db: sqlite3.Connection) -> list[Check]:
    c: list[Check] = []

    # Scene 1: receipts. shop-040 confirms, shop-008 is short and goes to the owner.
    rows = _out(db, "shop-040")
    ok, got = _has(rows, "send_delivery_receipt",
                   "Coral Lane Trading delivery receipt for Kunle Kiosk")
    c.append(Check("1", "shop-040 gets the receipt (dlv-56-9039, N64,250)",
                   ok and "Total: N64,250" in got, got[:120]))
    c.append(Check("1", "shop-040 replies 'ok / gbogbo goods ti de'",
                   _in(db, "shop-040") == ["ok\ngbogbo goods ti de"], str(_in(db, "shop-040"))))
    ok, got = _has(rows, "reply", "Thank you. Delivery dlv-56-9039 is confirmed.")
    c.append(Check("1", "shop-040 confirmed", ok, got[:120]))
    rows = _out(db, "shop-008")
    ok, got = _has(rows, "flag_delivery_to_owner", "shop-008 about delivery dlv-56-9007: dispute")
    c.append(Check("1", "shop-008's short delivery flagged to the owner", ok, got[:120]))
    ok, got = _has(rows, "reply", "Thanks, I've told Coral Lane Trading about this delivery.")
    c.append(Check("1", "shop-008 told, nothing changed", ok, got[:120]))
    consents = dict(db.execute(
        "select shop_id, count(*) from messages where tenant_id=? and kind='reply' and "
        "text like 'Thank you. Delivery % is confirmed.' group by shop_id", (TENANT,)).fetchall())
    c.append(Check("1", "no confirm for shop-008", "shop-008" not in consents, str(consents)))

    # Scene 2: shop-035's fake (f08, impossible date) is flagged, the genuine one (g02) is likely.
    a = _approval(db, "shop-035", "flag_payment_proof")
    c.append(Check("2", "f08 flagged looks_fake on time_real, money not moved",
                   a is not None and a.get("failed_rules") == ["time_real"]
                   and a.get("moves_money") is False, str(a and a.get("reasons"))))
    rows = _out(db, "shop-035")
    ok, got = _has(rows, "flag_payment_proof_notice",
                   "Thanks, I've sent this to Coral Lane Trading to confirm with the bank.")
    c.append(Check("2", "shop-035 gets a neutral notice (no blame)", ok, got[:120]))
    ok, got = _has(rows, "reply", "Thanks. I can see N34,950 from Chidi Fagbemi on your screenshot.")
    c.append(Check("2", "g02 read as likely paid until the bank confirms",
                   ok and "likely paid until the bank confirms" in got, got[:160]))

    # Scene 3: shop-014 taps 3-5, then a voice note "make am 6 tubs" -> one draft of 6, one reply.
    rows = _out(db, "shop-014")
    ok, got = _has(rows, "ask_stock_count", "Quick stock check: how many tubs of Mamaland Custard")
    c.append(Check("3", "one-tap question 0-2 | 3-5 | 6+", ok and "0-2 | 3-5 | 6+" in got,
                   got[:120]))
    a = _approval(db, "shop-014", "revise_restock")
    c.append(Check("3", "restock draft v2 of 6 tubs, nothing ordered",
                   a is not None and a.get("quantity") == 6 and a.get("places_order") is False,
                   str(a and {k: a.get(k) for k in ("quantity", "version", "status")})))
    shop_replies = [t for k, t in rows if k != "ask_stock_count"]
    c.append(Check("3", "one reply: 'Okay. I've changed it to 6 tubs.'",
                   shop_replies == ["Okay. I've changed it to 6 tubs. Coral Lane Trading will "
                                    "confirm before delivery."], str(shop_replies)))

    # Scene 4: shop-011 asks for credit, sends shelf photo s11, SAM counts, support to approvals.
    rows = _out(db, "shop-011")
    ok, got = _has(rows, "ask_for_shelf_photo", "I can ask Coral Lane Trading for you.")
    c.append(Check("4", "Mimi asks for one shelf photo", ok, got[:120]))
    ok, got = _has(rows, "reply", "I counted about 11 Sunrise Instant Noodles 70g packets")
    c.append(Check("4", "SAM 3.1 count read back, owner decides", ok and "They decide the limit."
                   in got, got[:160]))
    a = _approval(db, "shop-011", "submit_credit_review")
    c.append(Check("4", "credit review 'support' from 13/13 verified payments, limit unchanged",
                   a is not None and a.get("recommendation") == "support"
                   and a.get("changes_limit") is False and a.get("moves_money") is False,
                   str(a and a.get("note_for_owner"))))
    return c


def owner_route() -> list[Check]:
    """Scene 5 on the demo-mode API with the offline model: the owner's question
    routes to Collector by rule (no model call, N0). Its answer needs Muse Spark."""
    from fastapi.testclient import TestClient

    from mimi.api.app import create_app
    from mimi.api.config import Settings
    from mimi.store import ensure_schema, make_engine
    from mimi.store.seed import seed_world
    from mimi.world.loader import load_world

    tmp = Path(tempfile.mkdtemp(prefix="mimi-demo-owner-"))
    url = f"sqlite:///{tmp / 'demo.db'}"
    engine = make_engine(url)
    ensure_schema(engine)
    seed_world(engine, load_world())
    env = {"MIMI_MODE": "demo", "DATABASE_URL": url, "META_API_KEY": "",
           "MIMI_DEMO_IP_SALT": "check-salt", "MIMI_WORKERS": "1"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        with TestClient(create_app(Settings.from_env())) as c:
            tok = c.post("/v1/demo/sessions").json()["token"]
            r = c.post("/v1/turns", headers={"Authorization": f"Bearer {tok}"},
                       json={"message_id": "demo-owner-1", "source": "owner_message",
                             "text": "Who owes me the most?"}).json()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    route = r.get("route", {})
    return [Check("5", "owner question routes to collector/chase by rule, no model call",
                  route.get("skill") == "collector/chase" and route.get("model_called") is False,
                  json.dumps(route))]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scripts/demo_script_check.py")
    ap.add_argument("--workdir", default="", help="an e2e workdir with e2e.db (skips the run)")
    args = ap.parse_args(argv)
    wd = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="mimi-demo-"))
    if not args.workdir:
        env = {**os.environ, "META_API_KEY": "", "SPITCH_API_KEY": ""}
        r = subprocess.run([sys.executable, "-m", "mimi.api.e2e", "--no-write", "--workdir",
                            str(wd)], cwd=ROOT, env=env, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout[-2000:], r.stderr[-2000:])
            print("e2e replay failed")
            return 1
    db = sqlite3.connect(wd / "e2e.db")
    results = check(db) + owner_route()
    for x in results:
        print(f"{'PASS' if x.ok else 'FAIL'}  scene {x.scene}: {x.what}"
              + ("" if x.ok else f"\n      got: {x.got}"))
    bad = [x for x in results if not x.ok]
    print(f"{len(results) - len(bad)}/{len(results)} checks hold (workdir {wd})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

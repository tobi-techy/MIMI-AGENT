"""The simulated week (RAI-273): its pieces offline, and one short run end to end.

The full week and the soak run in their own CI job (``python -m mimi.api.week``);
here: the shared clock, Go's stand-in, the playbook's gate probes, the
binding fallback and phrase rule the week run found, and a one-day run with a
forced worker kill through the real API and worker processes.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import sqlalchemy as sa

from mimi.api.week.go import GoSim
from mimi.api.week.playbook import Playbook, naira
from mimi.api.week.simclock import FileClock, set_to
from mimi.harness.binding import AnswerBinder
from mimi.harness.lexicon import score as lex_score
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.tz import LAGOS
from mimi.world.loader import load_world

W = load_world()


# ------------------------------------------------------------------ the clock


def test_the_shared_clock_reads_the_sim_time_and_only_moves_forward(tmp_path: Path) -> None:
    f = tmp_path / "simclock.json"
    mon = datetime(2026, 10, 26, 7, 30, tzinfo=LAGOS)
    set_to(f, mon, allow_back=True)
    fc = FileClock(f)
    assert abs(fc() - mon.timestamp()) < 2
    set_to(f, mon - timedelta(hours=5))  # forward only: ignored
    assert fc() >= mon.timestamp() - 1
    set_to(f, mon + timedelta(days=1))
    assert abs(fc() - (mon + timedelta(days=1)).timestamp()) < 2
    set_to(f, mon, allow_back=True)  # a new soak loop, after the queue drained
    assert abs(fc() - mon.timestamp()) < 2


# ------------------------------------------------------------------ Go's stand-in


def test_go_rolls_the_day_and_writes_only_what_it_logs(tmp_path: Path) -> None:
    import random

    e = make_engine(f"sqlite:///{tmp_path / 'go.db'}")
    ensure_schema(e)
    seed_world(e, W)
    go = GoSim(e, W, W.tenant_id)
    go.open_day(57)
    with e.connect() as conn:
        live = conn.execute(sa.select(sa.func.count()).select_from(s.deliveries).where(
            s.deliveries.c.live.is_(True))).scalar()
        recs = conn.execute(sa.select(sa.func.count()).select_from(s.receivables).where(
            s.receivables.c.delivery_id.like("dlv-56-%"))).scalar()
        day = conn.execute(sa.select(s.tenants.c.live_date)).scalar()
    assert live == 0 and recs == 40 and day == "2026-10-27"
    d = go.add_delivery("shop-007", 57, 600, random.Random(1))
    assert d["total_kobo"] == sum(ln["quantity"] * ln["unit_price_kobo"] for ln in d["lines"])
    pid = go.bank_credit("shop-007", 1_000_000, 57, "2026-10-27T09:00:00", "NIP/X/Y/REF")
    assert go.ledger.deliveries == [d["delivery_id"]] and go.ledger.payments == [pid]
    assert len(go.ledger.credits) == 1


# ------------------------------------------------------------------ the playbook


def _tool_msg(name: str, data: dict[str, object], step: int = 0) -> dict[str, object]:
    return {"role": "tool", "tool_call_id": f"call_{step}_0_{name}",
            "content": json.dumps({"ok": True, "data": data})}


def _tools(*names: str) -> list[dict[str, object]]:
    return [{"type": "function", "function": {"name": n}} for n in names]


SHOP_MSG = ('Now: Tuesday 27 October 2026 15:00 WAT (Africa/Lagos)\nWake source: shop_message\n'
            '<shop_message shop_id="shop-007" untrusted="true">\n{t}\n</shop_message>')


def test_the_playbook_probes_the_claims_gate_with_an_invented_figure() -> None:
    pb = Playbook(probe_share=1.0, price_as="")
    tools = _tools("get_balance", "send_to_user")
    msgs: list[dict[str, object]] = [{"role": "user", "content": SHOP_MSG.format(
        t="How much I owe una now?")}]
    first = pb.chat(msgs, tools, max_tokens=100)
    assert first.tool_calls[0].name == "get_balance"
    msgs += [{"role": "assistant", "content": ""},
             _tool_msg("get_balance", {"shop_id": "shop-007", "outstanding_kobo": 1_234_500})]
    probe = pb.chat(msgs, tools, max_tokens=100)
    body = json.loads(probe.tool_calls[0].arguments)["body"]
    assert naira(1_234_600) in body  # one naira off: the gate must hold it
    msgs += [{"role": "assistant", "content": ""},
             {"role": "user", "content": "[Mimi harness] held back. It was not sent."}]
    fixed = pb.chat(msgs, tools, max_tokens=100)
    assert naira(1_234_500) in json.loads(fixed.tool_calls[0].arguments)["body"]


def test_the_playbook_tries_a_never_tier_move_on_a_refund_ask() -> None:
    pb = Playbook(price_as="")
    msgs = [{"role": "user", "content": SHOP_MSG.format(t="Abeg refund the 5k I overpaid")}]
    turn = pb.chat(msgs, _tools("get_balance", "get_payment_history", "send_to_user"),
                   max_tokens=100)
    assert turn.tool_calls[0].name == "refund_payment"


def test_projected_cost_is_priced_and_offline_is_free() -> None:
    msgs = [{"role": "user", "content": SHOP_MSG.format(t="Good morning")}]
    assert Playbook().chat(msgs, _tools("send_to_user"), max_tokens=100).cost_kobo > 0
    assert Playbook(price_as="").chat(msgs, _tools("send_to_user"),
                                      max_tokens=100).cost_kobo == 0


# ------------------------------------------------------------------ what the week found


def test_an_acknowledgement_stays_on_the_open_ask_when_the_binder_has_no_answer() -> None:
    b = AnswerBinder(model=None)
    for text in ("ok noted", "ok", "👍", "thanks"):
        assert b.classify("order/drop-confirmation", text).binds, text
    assert not b.classify("order/drop-confirmation", "Good morning").binds
    assert not b.classify("order/drop-confirmation", "how much do I owe you now").binds


def test_which_shops_owe_me_is_a_chase_question() -> None:
    assert lex_score("Which shops owe me the most?").top == "collector/chase"


# ------------------------------------------------------------------ one day, end to end


def test_one_simulated_day_through_the_api_with_a_worker_kill(tmp_path: Path) -> None:
    from mimi.api.week.report import score
    from mimi.api.week.runner import run_week

    t0 = time.time()
    run = run_week(workdir=tmp_path / "wk", days=1, week_kills=1)
    r = score(run)
    assert r["turns"]["total"] > 100 and r["turns"]["complete_rate"] >= 0.98
    assert r["kills"]["fired"] == 1 and not r["kills"]["bad"]
    assert not r["invented_figures"] and not r["duplicate_sends"] and not r["lost_jobs"]
    assert not r["budget_breaches"] and not r["never_tier_actions"]
    assert r["claims_gate_hits"] >= 0 and r["cost_kobo"]["spent"] == 0
    assert r["passed"], r
    assert time.time() - t0 < 120

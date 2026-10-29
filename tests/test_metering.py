"""The one metering seam (``mimi.models.metering``): every model call is charged.

* cached input is priced at Meta's cached rate, from one price table;
* only ``metering.book`` writes the spend table;
* every model the agent reaches is behind the seam, and a client called around
  it inside a turn is caught (strict: the turn fails);
* the router's, steer's and readers' calls reach the spend table (the daily
  cap) and the run budget, not only the harness loop's.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from mimi.harness.inbound import Inbound, Payload
from mimi.models import metering, prices
from mimi.models.base import ChatResult
from mimi.models.fake import FakeModel
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.world.loader import load_world

ROOT = Path(__file__).resolve().parents[1] / "mimi"
W = load_world()
NOW = datetime(2026, 10, 26, 11, 0, tzinfo=UTC)  # 12:00 WAT
DAY = "2026-10-26"


# ------------------------------------------------------------------ prices


def test_cached_input_is_priced_at_the_cached_rate() -> None:
    p = prices.TOKEN_PRICES["muse-spark-1.3"]
    assert (p.input, p.cached_input, p.output) == (1.25, 0.15, 4.25)
    # 3,510 in (2,545 cached), 114 out: 965 x 1.25 + 2,545 x 0.15 + 114 x 4.25
    # = 2,073.25 micro-USD = N3.32, rounded up to 332 kobo.
    assert prices.kobo_cost("muse-spark-1.3", 3510, 114, 2545) == 332
    assert prices.kobo_cost("muse-spark-1.3", 3510, 114, 2545, cached_rate=False) == 780
    assert prices.kobo_cost("muse-spark-1.3", 3510, 114) == 780  # nothing cached: unchanged
    assert prices.kobo_cost("fake", 1000, 1000, 10) == 0
    # A cached count above the prompt never makes a call cheaper than all-cached.
    assert prices.kobo_cost("muse-spark-1.3", 100, 0, 10_000) == prices.kobo_cost(
        "muse-spark-1.3", 100, 0, 100)


def test_the_full_rate_flag_restores_the_old_accounting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_CACHED_RATE", "0")
    assert prices.kobo_cost("muse-spark-1.3", 3510, 114, 2545) == 780


def test_rates_live_in_one_place() -> None:
    """No other module carries its own per-token or per-image price."""
    from mimi.credit import sam
    from mimi.harness import gateway
    from mimi.observe import turns

    assert gateway.kobo_cost is prices.kobo_cost
    assert sam.SAM_USD_PER_IMAGE == prices.SAM_USD_PER_IMAGE
    assert turns.TOKEN_PRICES["muse-spark-1.3"] == (1.25, 4.25)
    assert sam.sam_cost_kobo(2) == prices.sam_kobo(2) == 800


# ------------------------------------------------------------------ the seam


def _calls(path: Path, attr: str) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == attr]


def test_only_the_metering_seam_writes_the_spend_table() -> None:
    callers = {str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py") if _calls(p, "add_spend")}
    assert callers == {"models/metering.py"}


def test_every_model_client_guards_its_entry() -> None:
    """A new client that forgets ``metering.guard`` fails here."""
    entries = {"models/meta.py": "complete", "models/cache.py": "complete",
               "models/fake.py": "complete", "harness/gateway.py": "chat",
               "api/offline.py": "chat", "api/helpers.py": "complete",
               "credit/sam.py": "segment", "voice/spitch.py": "transcribe",
               "voice/meta.py": "transcribe", "voice/cache.py": "transcribe",
               "voice/fake.py": "transcribe"}
    for rel, fn in entries.items():
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)
                    and "Protocol" not in ast.unparse(n).split(":", 1)[0]]:
            for f in cls.body:
                if isinstance(f, ast.FunctionDef) and f.name == fn:
                    body = ast.unparse(f)
                    assert ("metering.guard(" in body or "metering.metered(" in body), (
                        rel, cls.name, fn)


class Priced:
    """A chat model that answers like Muse Spark and reports real token use."""

    def __init__(self, text: str, prompt: int = 700, completion: int = 300,
                 name: str = "muse-spark-1.3") -> None:
        self.name, self.text, self.prompt, self.completion = name, text, prompt, completion
        self.calls = 0

    def complete(self, messages: Sequence[Any], *, max_tokens: int = 400,
                 meta: Mapping[str, str] | None = None,
                 response_format: Mapping[str, object] | None = None) -> ChatResult:
        metering.guard(self.name)
        self.calls += 1
        return ChatResult(self.text, self.name, self.prompt, self.completion)


@pytest.fixture()
def engine() -> Any:
    e = make_engine("sqlite://")
    ensure_schema(e)
    seed_world(e, W, daily_cap_kobo=1_000_000)
    return e


def _agent(engine: Any, router_model: Any = None) -> Any:
    from mimi.api.agent import Agent
    from mimi.api.offline import OfflineModel

    return Agent(engine, models=[OfflineModel()], router_model=router_model, lease_s=30.0,
                 clock=lambda: NOW)


def _say(a: Any, ext: str, text: str, shop: str = "shop-012") -> str:
    v = a.submit("coral-lane", Inbound(source="shop_message", org_id="coral-lane",
                                       external_id=ext, received_at=NOW,
                                       payload=Payload(text=text,
                                                       from_phone=W.shop(shop).phone)))
    return str(v.turn_id)


def _spent(engine: Any) -> int:
    with engine.connect() as conn:
        return int(conn.execute(sa.select(sa.func.coalesce(sa.func.sum(s.spend.c.kobo), 0))
                                .where(s.spend.c.tenant_id == "coral-lane",
                                       s.spend.c.day == DAY)).scalar() or 0)


def _turn(engine: Any, turn_id: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(sa.select(s.turns).where(s.turns.c.turn_id == turn_id)).first()


def _walk_models(agent: Any) -> dict[str, Any]:
    out = {"router": agent.router.model, "binding": agent.router.binder.model,
           "steer": agent.steerer.model, "vision": agent.deps.vision,
           "judge": agent.deps.judge, "stock": agent.deps.stock,
           "segmenter": agent.deps.segmenter, "list_reader": agent.list_reader.model}
    return out


def test_every_model_the_agent_reaches_is_metered(engine: Any) -> None:
    a = _agent(engine, router_model=FakeModel())
    for where, m in _walk_models(a).items():
        assert metering.is_metered(m), where
    roles = {k: v.role for k, v in _walk_models(a).items()}
    assert roles["router"] == "router" and roles["steer"] == "steer"
    assert roles["binding"] == "binding" and roles["vision"] == "vision"
    # The tools' readers are built from the same metered deps.
    reader = a.helpers["proof"].toolset
    assert reader is not None


def test_a_client_called_around_the_seam_fails_the_turn(engine: Any,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_METER_STRICT", "1")
    raw = Priced('{"route": "collector/balance", "confidence": 0.97, "second": "",'
                 ' "language": "en"}')
    a = _agent(engine, router_model=FakeModel())
    a.router.model = raw  # someone wires a client in without the seam
    before = len(metering.violations)
    tid = _say(a, "m.bypass", "the thing we talked about last week, any update on that side")
    for _ in range(4):
        if a.process() is None:
            break
    assert raw.calls == 0  # the guard refused the call before it was made
    assert len(metering.violations) > before
    assert "outside the metering seam" in metering.violations[-1]
    row = _turn(engine, tid)
    assert row.completions == 0 and row.state != "done"


def test_the_router_models_call_reaches_the_spend_table_and_the_run(engine: Any) -> None:
    model = Priced('{"route": "collector/balance", "confidence": 0.97, "second": "",'
                   ' "language": "en"}')
    a = _agent(engine, router_model=model)
    tid = _say(a, "m.router", "the thing we talked about last week, any update on that side")
    a.process()
    if model.calls == 0:
        pytest.skip("the router decided by rule; no model call to charge")
    row = _turn(engine, tid)
    res = json.loads(row.result or "{}")
    per_call = prices.kobo_cost("muse-spark-1.3", 700, 300)
    metered = res["metered"]
    side = sum(r["kobo"] for k, r in metered.items() if k != "harness")
    assert side == per_call * model.calls and side > 0
    spend = res.get("spend") or {}
    assert _spent(engine) == side + int(spend.get("kobo", 0))  # the daily cap counts it
    if spend.get("steps"):
        assert spend["side_kobo"] == side  # and the run budget does


def test_the_old_accounting_books_only_the_loop(engine: Any,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMI_METER_ALL", "0")
    model = Priced('{"route": "collector/balance", "confidence": 0.97, "second": "",'
                   ' "language": "en"}')
    a = _agent(engine, router_model=model)
    tid = _say(a, "m.old", "the thing we talked about last week, any update on that side")
    a.process()
    if model.calls == 0:
        pytest.skip("the router decided by rule")
    res = json.loads(_turn(engine, tid).result or "{}")
    assert res["metered"]["router"]["kobo"] > 0  # still priced for the reports
    assert _spent(engine) == int((res.get("spend") or {}).get("kobo", 0))


def test_side_spend_counts_toward_the_run_budget() -> None:
    """A run whose turn already spent its budget on readers stops before the loop."""
    from mimi.harness.collector import collector_helper, default_registry
    from mimi.harness.gateway import Gateway, ScriptedModel, say
    from mimi.harness.go import FakeGo
    from mimi.harness.loop import Harness
    from mimi.harness.policy import GoPolicy
    from mimi.harness.types import RunEvent

    go = FakeGo(W)
    helper = collector_helper(default_registry(), GoPolicy())
    model = ScriptedModel([say("done")])
    h = Harness(book=go, control=go, events=go, gateway=Gateway([model]), policy=GoPolicy(),
                clock=lambda: NOW)
    ev = RunEvent(event_id="e1", tenant_id=W.tenant_id, thread_id="t1", job_id="j1",
                  wake_source="shop_message", text="How much I owe?", sender="shop",
                  shop_id="shop-012", at=NOW)
    with metering.scope(control=go, tenant_id=W.tenant_id, day=NOW.date(), strict=True) as sc:
        state = h.start(ev, helper)
        metering.charge("vision", "muse-spark-1.3", kobo=helper.budget.max_kobo)
        res = h.step(state, helper)
    assert res.state["finished"]["status"] == "stopped_budget"
    assert res.state["spend"]["side_kobo"] == helper.budget.max_kobo
    assert model.calls == 0
    assert go.daily_spend(W.tenant_id, NOW.date())[0] == helper.budget.max_kobo
    assert sc.tally()["vision"]["calls"] == 1

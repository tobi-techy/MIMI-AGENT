"""Eval honesty (RAI-307, RAI-310, RAI-311, RAI-312).

Each test was written first and failed on main (02023d4):

* RAI-307: a toolset of ten near-limit tools (39,000 chars) built fine, and
  only the Collector registry's tools were size-checked in CI.
* RAI-310: a slice rate (by kind, split, language, tag) left out the cases
  that could not run, so ``balance`` read ``8/8`` with two cases missing.
* RAI-311: ``--only`` runs exited 0 without checking a pass mark.
* RAI-312: the router's ``p95_latency_ms<800`` passed on a replay, where the
  number is local code time plus model time read from the cache.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from mimi.harness.registry import (
    MAX_TOOL_CHARS,
    MAX_TOOLSET_CHARS,
    TOOLSET_BUDGETS,
    RegistryError,
    ToolRegistry,
    build_toolset,
)
from mimi.world import load_world

W = load_world()


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_API_KEY", "")
    monkeypatch.setenv("SPITCH_API_KEY", "")


# ------------------------------------------------------------- RAI-307


class _AutoPolicy:
    def base_tier(self, name: str) -> str:
        return "auto"


def _near_limit_registry(n: int) -> ToolRegistry:
    reg = ToolRegistry("t")
    for i in range(n):
        class In(BaseModel):
            model_config = ConfigDict(extra="forbid")

            shop_id: str

        class Out(BaseModel):
            ok: bool

        def fn(ctx: Any, args: In) -> Out:
            return Out(ok=True)

        fn.__name__ = fn.__qualname__ = f"tool_{i}"
        fn.__doc__ = "x" * (MAX_TOOL_CHARS - 300)
        fn.__annotations__ = {"ctx": Any, "args": In, "return": Out}
        reg.tool(kind="read", scope="shop:balance", tier="auto")(fn)
    return reg


def test_poc_a_toolset_of_many_near_limit_tools_is_refused() -> None:
    reg = _near_limit_registry(10)
    assert all(s.size() <= MAX_TOOL_CHARS for s in reg.specs())
    with pytest.raises(RegistryError, match="toolset"):
        build_toolset(reg, "x", [f"tool_{i}" for i in range(10)], _AutoPolicy())  # type: ignore[arg-type]


def _all_helpers() -> dict[str, Any]:
    from mimi.api.helpers import build_helpers
    from mimi.harness.collector import default_registry, proof_helper
    from mimi.harness.policy import GoPolicy
    from mimi.store import SqlGo, ensure_schema, make_engine, seed_world

    e = make_engine("sqlite://")
    ensure_schema(e)
    seed_world(e, W)
    hs: dict[str, Any] = dict(build_helpers(go=SqlGo(e)))
    hs["proof (no store)"] = proof_helper(default_registry(), GoPolicy())
    return hs


def test_every_helper_toolset_and_tool_is_within_its_budget() -> None:
    sizes = {}
    for key, h in _all_helpers().items():
        for spec in h.toolset.specs:
            assert spec.size() <= MAX_TOOL_CHARS, (key, spec.name, spec.size())
        size = len(json.dumps(h.toolset.schemas(), separators=(",", ":")))
        budget = TOOLSET_BUDGETS.get(key.split(" ")[0])
        assert budget is not None, f"{key} has no toolset budget in TOOLSET_BUDGETS"
        assert size <= budget <= MAX_TOOLSET_CHARS, (key, size, budget)
        sizes[key] = (size, budget)
    print("\n".join(f"{k} toolset {s:,} chars (budget {b:,})" for k, (s, b) in sizes.items()))


def _worst_slices() -> dict[str, int]:
    from mimi.harness.context import SLICES, ContextBuilder, Manifest
    from mimi.harness.go import FakeGo
    from mimi.harness.types import RunEvent

    go = FakeGo(W)
    b = ContextBuilder(book=go)
    from mimi.harness.context_eval import _at
    from mimi.harness.scenarios import WAT

    at = _at(W, WAT)
    worst: dict[str, int] = {}
    for skill in SLICES:
        for shop in W.shops:
            ev = RunEvent(event_id="e", tenant_id=W.tenant_id, thread_id="t",
                          wake_source="shop_message", text="hi", sender="shop",
                          shop_id=shop.shop_id, at=at)
            man = Manifest(tenant_id=W.tenant_id, thread_id="t", helper="h", skill=skill,
                           budget_tokens=1)
            data, _ = b._slice(ev, skill, shop.shop_id, man)
            n = len(json.dumps(data, ensure_ascii=False, sort_keys=True))
            worst[skill] = max(worst.get(skill, 0), n)
    return worst


def test_every_book_slice_is_within_its_budget() -> None:
    from mimi.harness.context import SLICE_BUDGETS, SLICES

    worst = _worst_slices()
    assert set(SLICE_BUDGETS) == set(SLICES)
    for skill, n in worst.items():
        assert n <= SLICE_BUDGETS[skill], (skill, n, SLICE_BUDGETS[skill])


def test_a_slice_that_grows_past_its_budget_fails() -> None:
    from mimi.harness import context

    grown = dict(context.SLICE_ROWS, open_invoices=60, payments=60, promises=60)
    old = context.SLICE_ROWS
    context.SLICE_ROWS = grown
    try:
        worst = _worst_slices()
    finally:
        context.SLICE_ROWS = old
    over = [s for s, n in worst.items() if n > context.SLICE_BUDGETS[s]]
    assert over, worst


# ------------------------------------------------------------- RAI-310


def test_poc_router_slice_rates_count_cache_misses() -> None:
    from mimi.harness.router import Router
    from mimi.harness.router_eval import load_cases, run, summarise

    cases = [c for c in load_cases() if c.get("lang") == "pcm"][:4]
    rows = run(W, cases, Router(model=None))
    rows[0] = (rows[0][0], None, "no recorded answer; run --live")
    s = summarise(rows)
    assert s["cache_misses"] == 1
    assert s["by_language"]["pcm"].endswith("(1 miss)"), s["by_language"]
    assert s["by_language"]["pcm"].split(" ")[0].endswith("/4"), s["by_language"]


def test_poc_harness_by_kind_counts_cache_misses(tmp_path: Path) -> None:
    from mimi.harness.eval import run_scenarios
    from mimi.harness.gateway import RecordedToolModel
    from mimi.harness.scenarios import build

    scs = [s for s in build(W) if s.kind == "balance"][:2]
    rec = RecordedToolModel(tmp_path / "empty.cache.json", None, name="muse-spark-1.3")
    res = run_scenarios(W, scs, lambda sc: rec).summary()
    assert res["cache_misses"] == 2
    assert res["by_kind"] == {"balance": "0/2 (2 misses)"}


def test_slice_rate_reads_as_before_when_nothing_missed() -> None:
    from mimi.evalmarks import slice_rate

    assert slice_rate(10, 10) == "10/10"
    assert slice_rate(9, 10, 1) == "9/10 (1 miss)"
    assert slice_rate(7, 10, 3) == "7/10 (3 misses)"


def test_order_eval_slices_count_cases_cut_by_the_spend_cap() -> None:
    from mimi.api.order_eval import _by_kind

    rows = [{"kind": ["typed"], "correct": True}]
    cut = [{"id": "h02", "kind": ["typed"]}]
    assert _by_kind(rows, cut) == {"typed": "1/2 (1 miss)"}


# ------------------------------------------------------------- RAI-311


def test_poc_order_eval_only_with_no_case_fails() -> None:
    from mimi.api import order_eval

    assert order_eval.main(["--no-write", "--only", "nosuch"]) == 1


def test_poc_order_eval_only_fails_on_a_failed_case(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimi.api import order_eval

    assert order_eval.main(["--no-write", "--only", "h01"]) == 0
    real = order_eval.score_case

    def failing(c: dict[str, Any], res: dict[str, Any]) -> dict[str, Any]:
        r = real(c, res)
        return {**r, "correct": False, "misses": ["forced"]}

    monkeypatch.setattr(order_eval, "score_case", failing)
    assert order_eval.main(["--no-write", "--only", "h01"]) == 1


def test_poc_router_eval_only_without_the_cascade_checks_nothing() -> None:
    from mimi.harness import router_eval

    assert router_eval.main(["--no-write", "--only", "rules_lexical"]) == 1


def test_poc_context_eval_only_with_no_case_fails() -> None:
    from mimi.harness import context_eval

    assert context_eval.main(["--no-write", "--only", "nosuch"]) == 1


def test_context_eval_only_checks_the_picked_case() -> None:
    from mimi.harness import context_eval

    assert context_eval.main(["--no-write", "--only", "chase-01"]) == 0


def test_poc_harness_record_only_with_no_case_fails() -> None:
    from mimi.harness import eval as harness_eval

    assert harness_eval.main(["--record-only", "--only", "nosuch"]) == 1


@pytest.mark.parametrize("mod", ["mimi.proof.eval", "mimi.stock.eval"])
def test_poc_flow_evals_only_with_no_case_fail(mod: str) -> None:
    import importlib

    m = importlib.import_module(mod)
    assert m.main(["--only", "nosuch"]) == 1


def test_poc_steer_eval_only_with_no_case_fails() -> None:
    from mimi.api import steer_eval

    assert steer_eval.main(["--no-write", "--only", "nosuch"]) == 1


def test_exit_code_needs_a_checked_mark() -> None:
    from mimi.evalmarks import NOT_MEASURED, exit_code

    assert exit_code({}) == 1
    assert exit_code({"p95": NOT_MEASURED}) == 1
    assert exit_code({"a": True, "p95": NOT_MEASURED}) == 0
    assert exit_code({"a": True, "b": False}) == 1
    assert exit_code({"a": True}, picked=0) == 1


# ------------------------------------------------------------- RAI-312


def test_poc_replayed_router_latency_never_passes_the_p95_mark() -> None:
    from mimi.evalmarks import NOT_MEASURED, passed
    from mimi.harness.router_eval import pass_marks

    c = {"accuracy": 0.99, "money_misroutes": 0, "clarify_rate": 0.04,
         "rules_cases_that_called_the_model": 0, "unknown_numbers_not_onboarding": 0,
         "injections_that_raised_risk": 0, "latency_ms": {"p95": 18.9}, "cache_misses": 0}
    replay = pass_marks(c, latency_measured=False)
    assert replay["p95_latency_ms<800"] == NOT_MEASURED
    assert passed(replay)  # decided by the measured marks only
    live = pass_marks(c, latency_measured=True)
    assert live["p95_latency_ms<800"] is True
    slow = pass_marks({**c, "latency_ms": {"p95": 900.0}}, latency_measured=True)
    assert slow["p95_latency_ms<800"] is False and not passed(slow)

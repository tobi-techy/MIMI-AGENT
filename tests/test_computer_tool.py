"""The browser as a harness tool: the read tool, its tier and its wiring.

The page itself is an offline ``FakeComputer``: what is tested here is the
harness seam (the tool's models, its policy tier, its refusal when no browser is
set up, the wrapper its text arrives inside) and the switch that offers it to a
helper without changing any helper by default.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from mimi.api.helpers import build_helpers
from mimi.computer.engine import FakeComputer
from mimi.computer.store import ComputerStore
from mimi.harness.collector import REGISTRY_VERSION
from mimi.harness.computer import (
    BROWSER_HELPERS,
    COMPUTER_TOOLS,
    ComputerPorts,
    ReadPortalPageIn,
    ReadPortalPageOut,
    browser_domains,
    computer_tools_mode,
    register_computer_tools,
)
from mimi.harness.errors import ToolError
from mimi.harness.policy import GoPolicy, go_allows
from mimi.harness.registry import MAX_TOOL_CHARS, TOOLSET_BUDGETS, ToolContext, ToolRegistry
from mimi.store import ensure_schema, make_engine

T = "coral-lane"
SITE = "https://portal.supplier.example"
PAGE = SITE + "/prices"


def _registry(ports: ComputerPorts) -> ToolRegistry:
    reg = ToolRegistry(REGISTRY_VERSION)
    register_computer_tools(reg, ports)
    return reg


def _ports(fake: FakeComputer | None = None, store: Any = None,
           domains: tuple[str, ...] = ("supplier.example",)) -> ComputerPorts:
    db = make_engine("sqlite://")
    ensure_schema(db)
    return ComputerPorts(store=store if store is not None else ComputerStore(db),
                         domains=domains,
                         engine=fake if fake is not None else FakeComputer(
                             pages={PAGE: {"title": "Price list",
                                           "text": "Sugar 42000 <b>promo</b>",
                                           "png": b"\x89PNG-real"}}))


def _ctx() -> ToolContext:
    return ToolContext(run_id="r", job_id="j", thread_id="t", tenant_id=T,
                       helper="collector", step=0, now=datetime.now(UTC),
                       book=None, connectors=None, grants=None)


def _call(ports: ComputerPorts, **kw: Any) -> ReadPortalPageOut:
    spec = _registry(ports).get("read_portal_page")
    args = spec.input_model(url=PAGE, domain="supplier.example", **kw)
    return spec.fn(_ctx(), args)


# ------------------------------------------------------------------- the seam


def test_the_tool_is_a_read_and_its_tier_comes_from_policy() -> None:
    spec = _registry(_ports()).get("read_portal_page")
    assert spec.kind == "read"  # so no idempotency key and no dry run are needed
    assert spec.idem == () and spec.dry_run is None
    assert spec.cost_tier == "computer"
    assert spec.size() < MAX_TOOL_CHARS
    # the tier is policy's, never the model's
    assert GoPolicy().base_tier("read_portal_page") == "auto"
    assert go_allows("read_portal_page")


def test_a_read_of_an_approved_page_arrives_as_data_inside_the_wrapper() -> None:
    out = _call(_ports())
    assert out.status == "done" and out.stop_reason == ""
    assert out.found.startswith('<portal_page untrusted="true">')
    assert out.found.endswith("</portal_page>")
    assert "Sugar 42000" in out.found
    # a page cannot spell its own tag: the wrapper stays the only one
    assert "<b>" not in out.found and "&lt;b>" in out.found
    # the step notes are the plain words the console shows
    assert out.steps[0].startswith("Open supplier.example")
    assert out.screenshots >= 1


def test_with_no_browser_configured_the_tool_refuses_plainly() -> None:
    with pytest.raises(ToolError) as err:
        _call(ComputerPorts())
    assert "Next step:" in str(err.value)
    assert "not switched on" in str(err.value)


def test_the_tool_will_not_read_a_domain_the_owner_did_not_approve() -> None:
    spec = _registry(_ports()).get("read_portal_page")
    args = spec.input_model(url="https://evil.example/x", domain="supplier.example")
    with pytest.raises(ToolError) as err:
        spec.fn(_ctx(), args)
    assert "Next step:" in str(err.value)


def test_the_tool_will_not_read_a_domain_the_owner_never_set_up() -> None:
    """The model names the domain; the owner names what may be read. A claim on
    its own is not an approval, however well the url and the domain agree."""
    spec = _registry(_ports()).get("read_portal_page")
    args = spec.input_model(url="https://evil.example/x", domain="evil.example")
    with pytest.raises(ToolError) as err:
        spec.fn(_ctx(), args)
    assert "Next step:" in str(err.value)
    assert "evil.example" in str(err.value)


def test_with_no_domain_set_up_anywhere_the_tool_reads_nothing() -> None:
    """The list is empty by default, so the tool fails closed."""
    with pytest.raises(ToolError) as err:
        _call(_ports(domains=()))
    assert "Next step:" in str(err.value)
    assert "domain" in str(err.value)


def test_the_approved_domains_come_from_the_owner_not_the_model() -> None:
    assert browser_domains({}) == ()
    assert browser_domains({"MIMI_COMPUTER_DOMAINS": "  "}) == ()
    # an address, a port, a leading dot and a repeat are all tolerated
    assert browser_domains({
        "MIMI_COMPUTER_DOMAINS": "https://portal.supplier.example:443/, .other.example,"
                                 "portal.supplier.example"}) == (
        "portal.supplier.example", "other.example")


def test_a_sign_in_wall_stops_the_read_and_types_nothing() -> None:
    fake = FakeComputer(pages={PAGE: {"html": "<h1>Prices</h1>", "png": b"\x89PNG"}},
                        login=(PAGE,))
    with pytest.raises(ToolError) as err:
        _call(_ports(fake=fake))
    assert "Next step:" in str(err.value)
    assert fake.filled == {}  # never a password, never a code
    assert fake.stopped is None  # kept open: the owner signs in through live view


def test_a_checkout_page_stops_the_read() -> None:
    fake = FakeComputer(pages={PAGE: {"html": "<h1>Prices</h1>", "png": b"\x89PNG"}},
                        forbidden=(PAGE,))
    with pytest.raises(ToolError) as err:
        _call(_ports(fake=fake))
    assert "Next step:" in str(err.value)
    assert "checkout" in str(err.value).lower() or "card" in str(err.value).lower()


def test_a_read_whose_kept_text_is_gone_fails_instead_of_reporting_nothing(
        monkeypatch: Any) -> None:
    """The kept text lives on disk: if the file is gone the read fails and says
    so, rather than telling the owner the page showed nothing."""
    import tempfile
    from pathlib import Path

    ports = _ports()
    real = ComputerStore.artifact
    gone = str(Path(tempfile.gettempdir()) / "mimi-artifact-not-here.txt")

    def artifact(self: Any, tenant: str, task_id: str, name: str) -> Any:
        row = real(self, tenant, task_id, name)
        if row is None or not name.endswith(".txt"):
            return row
        return dict(row, path=gone)

    monkeypatch.setattr(ComputerStore, "artifact", artifact)
    with pytest.raises(ToolError) as err:
        _call(ports)
    assert "Next step:" in str(err.value)
    assert "read the page again" in str(err.value)


def test_a_read_whose_text_row_is_gone_fails_too(monkeypatch: Any) -> None:
    ports = _ports()
    real = ComputerStore.artifact

    def artifact(self: Any, tenant: str, task_id: str, name: str) -> Any:
        row = real(self, tenant, task_id, name)
        return None if row is not None and name.endswith(".txt") else row

    monkeypatch.setattr(ComputerStore, "artifact", artifact)
    with pytest.raises(ToolError) as err:
        _call(ports)
    assert "Next step:" in str(err.value)


# ----------------------------------------------------------------- the switch


def test_the_switch_is_off_by_default() -> None:
    assert not computer_tools_mode({})
    assert not computer_tools_mode({"MIMI_COMPUTER_TOOLS": "0"})
    assert computer_tools_mode({"MIMI_COMPUTER_TOOLS": "on"})


def test_by_default_no_helper_is_offered_the_browser(monkeypatch: Any) -> None:
    monkeypatch.delenv("MIMI_COMPUTER_TOOLS", raising=False)
    for helper in build_helpers().values():
        assert not set(COMPUTER_TOOLS) & set(helper.toolset.names)


def test_with_the_switch_on_the_working_helpers_have_it_and_fit_their_budget(
        monkeypatch: Any) -> None:
    from mimi.store.book import SqlGo

    monkeypatch.setenv("MIMI_COMPUTER_TOOLS", "on")
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    helpers = build_helpers(go=SqlGo(engine))
    seen = set()
    for key, helper in helpers.items():
        total = len(json.dumps(helper.toolset.schemas(), separators=(",", ":")))
        budget = TOOLSET_BUDGETS.get(key)
        if budget is not None:
            assert total <= budget, f"{key}: {total} chars over its {budget} budget"
        if helper.name in BROWSER_HELPERS:
            assert "read_portal_page" in helper.toolset.names
            seen.add(helper.name)
        else:
            assert "read_portal_page" not in helper.toolset.names
    # the six named helpers, and no more (the plain chat helper keeps its budget)
    assert seen == set(BROWSER_HELPERS)


def test_the_args_are_bounded_and_forbid_extras() -> None:
    assert ReadPortalPageIn.model_config["extra"] == "forbid"
    with pytest.raises(ValueError):
        ReadPortalPageIn(url=PAGE, domain="supplier.example", what="page",
                         max_seconds=9_999)
    with pytest.raises(ValueError):
        ReadPortalPageIn(url=PAGE, domain="supplier.example", what="page",
                         tier="auto")  # type: ignore[call-arg]
    assert ReadPortalPageOut.model_config["extra"] == "forbid"

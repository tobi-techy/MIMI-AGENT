"""The SLO page and the alert rules are one artifact in two files.

A number in ``docs/SLO.md`` that no series carries is a promise nobody checks;
an alert that names a series the code does not emit never fires. These tests
hold the page, the rules and ``mimi.observe.metrics`` together, and keep the
placeholders visibly provisional until the load run replaces them.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mimi.api.agent import Agent
from mimi.harness.inbound import Inbound, Payload
from mimi.observe import metrics
from mimi.observe import spend as budget
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.world.loader import load_world

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "SLO.md").read_text()
RULES = (ROOT / "ops" / "prometheus" / "mimi.rules.yml").read_text()
SERIES = re.compile(r"\bmimi_[a-z0-9_]+")
W = load_world()
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def page() -> str:
    """One rendered page from a store with a finished turn and a queued one."""
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    seed_world(engine, W, daily_cap_kobo=50_000)
    from mimi.api.offline import OfflineModel

    agent = Agent(engine, models=[OfflineModel()], router_model=None,
                  worker_id="w-slo", clock=lambda: NOW)
    v = agent.submit("coral-lane", Inbound(source="shop_message", org_id="coral-lane",
                                           external_id="slo.1", received_at=NOW,
                                           payload=Payload(text="How much I owe now?",
                                                           from_phone=W.shop("shop-033").phone)))
    agent.process(str(v.turn_id))
    agent.submit("coral-lane", Inbound(source="shop_message", org_id="coral-lane",
                                       external_id="slo.2", received_at=NOW,
                                       payload=Payload(text="How much I owe now?",
                                                       from_phone=W.shop("shop-033").phone)))
    return metrics.render_prometheus(metrics.snapshot(engine))


def emitted(page: str) -> set[str]:
    return {line.split("{")[0].split(" ")[0] for line in page.splitlines()
            if line and not line.startswith("#")}


def sections() -> dict[str, str]:
    """``{"SLO-1": "...body..."}`` for each ``### SLO-n`` heading."""
    out: dict[str, str] = {}
    for block in DOC.split("### ")[1:]:
        title, _, body = block.partition("\n")
        m = re.match(r"(SLO-\d+\w?)", title)
        if m:
            out[m.group(1)] = body
    return out


def anchor(heading: str) -> str:
    """GitHub's anchor for a heading, near enough for a runbook link."""
    text = heading.strip().lower()
    text = re.sub(r"[^a-z0-9 \-]", "", text)
    return text.replace(" ", "-")


def anchors() -> set[str]:
    return {anchor(line.lstrip("# ")) for line in DOC.splitlines() if line.startswith("#")}


def alerts() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for block in RULES.split("- alert: ")[1:]:
        name, _, body = block.partition("\n")
        row = {"name": name.strip()}
        for key in ("severity", "summary", "runbook", "slo", "for"):
            m = re.search(rf"^\s*{key}:\s*(.+)$", body, re.MULTILINE)
            row[key] = m.group(1).strip().strip('"') if m else ""
        out.append(row)
    return out


# ---------------------------------------------------------------- the page


FIELD = r"\*\*{name}:\*\* ((?:[^\n]+)(?:\n  [^\n]+)*)"


def field(body: str, name: str) -> str | None:
    """A bold field and its indented continuation lines, as one string."""
    m = re.search(FIELD.format(name=name), body)
    return " ".join(m.group(1).split()) if m else None


def test_the_page_states_five_objectives_each_with_a_number_and_an_owner() -> None:
    found = sections()
    assert set(found) == {"SLO-1", "SLO-2", "SLO-3", "SLO-4", "SLO-5"}
    for name, body in found.items():
        metric = field(body, "Metric")
        threshold = field(body, "Threshold")
        action = field(body, "First action")
        owner = field(body, "Owner")
        assert metric, f"{name} has no metric"
        assert threshold and re.search(r"\d", threshold), f"{name} has no number"
        assert action and len(action) > 60, f"{name} has no first action"
        assert owner and len(owner) > 3, f"{name} has no owner"
        assert SERIES.search(metric), f"{name}'s metric is not a series"


def test_every_metric_the_page_names_is_on_the_page_we_render(page: str) -> None:
    named = set(SERIES.findall(DOC))
    assert named, "the page names no metric"
    missing = sorted(named - emitted(page))
    assert missing == [], f"docs/SLO.md names series nothing emits: {missing}"


def test_provisional_numbers_are_marked_and_listed() -> None:
    body, _, listed = DOC.partition("## Provisional numbers")
    assert listed, "the provisional list is missing"
    marked: set[str] = set()
    current = ""
    for line in body.splitlines():
        m = re.match(r"### (SLO-\d+\w?)", line)
        if m:
            current = m.group(1)
        if "[PROVISIONAL" in line:
            if current:
                marked.add(current)
            for in_table in re.findall(r"\|\s*(SLO-\d+\w?)\s*\|", line):
                marked.add(in_table)
    assert len(marked) >= 3, "load-dependent numbers must be marked provisional"
    for name in marked:
        assert name in listed, f"{name} is marked provisional but not listed"


def test_the_doc_does_not_quote_a_number_it_did_not_measure() -> None:
    """The measured numbers come from this bench and from COST.md."""
    assert "N16.42" in DOC and "COST.md" in DOC
    assert "27.6 turns/s" in DOC
    assert "33 ms" in DOC and "45 ms" in DOC
    # The cap the doc quotes is the one the code computes: N250 a shop, floored
    # at one shop, then said in turns at the measured average. If either the
    # multiplier or the average moves, the doc has to move with it.
    assert budget.SHOP_ALLOWANCE_KOBO == 25_000 and "N250" in DOC
    assert budget.DEFAULT_DAILY_CAP_KOBO == 25_000  # the one-shop floor
    for shops, cap, turns in ((1, "N250", 15), (8, "N2,000", 121),
                              (40, "N10,000", 609)):
        assert budget.shop_allowance(shops) == 25_000 * shops
        assert budget.turns_for(budget.shop_allowance(shops)) == turns
        assert cap in DOC and str(turns) in DOC
    # One active shop is the floor even when no shop has any activity.
    assert budget.shop_allowance(0) == budget.shop_allowance(1)


# --------------------------------------------------------------- the rules


def test_every_alert_names_a_series_we_emit(page: str) -> None:
    used = set(SERIES.findall(RULES))
    missing = sorted(used - emitted(page) - {"mimi"})  # "mimi" is prose in a path
    assert missing == [], f"mimi.rules.yml names series nothing emits: {missing}"


def test_every_alert_can_be_acted_on() -> None:
    found = alerts()
    assert len(found) >= 8
    for row in found:
        assert row["severity"] in {"page", "warning", "ticket"}, row["name"]
        assert row["summary"] and row["for"], row["name"]
        assert row["runbook"].startswith("docs/SLO.md#"), row["name"]


def test_every_runbook_link_lands_on_a_heading() -> None:
    known = anchors()
    for row in alerts():
        assert row["runbook"].split("#", 1)[1] in known, row["name"]


def test_every_objective_has_an_alert(page: str) -> None:
    named = {row["slo"] for row in alerts() if row["slo"]}
    assert {"SLO-1", "SLO-2", "SLO-3", "SLO-4", "SLO-5"} <= named


def test_the_rules_file_is_prometheus_yaml() -> None:
    assert RULES.startswith("#")
    assert "groups:" in RULES and "rules:" in RULES
    # A rule with no expr never fires; a rule with no annotations cannot be read.
    for block in RULES.split("- alert: ")[1:]:
        assert "expr:" in block and "annotations:" in block


def test_a_dying_page_is_itself_an_alert() -> None:
    assert "absent(mimi_queue_depth)" in RULES

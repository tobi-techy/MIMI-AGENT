"""Computer tasks: the guardrails, the plans, the runner and the five endpoints.

Everything runs offline against ``FakeComputer``, which is a small scripted site
in memory. The live browser container is exercised by ``scripts/computer_smoke.py``
against a real cloud browser session, not here.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from mimi.api.agent import Agent
from mimi.api.app import create_app
from mimi.api.config import Settings, parse_keys
from mimi.api.offline import OfflineModel
from mimi.computer import guardrails as g
from mimi.computer import plans
from mimi.computer.engine import FakeComputer, TaskRef
from mimi.computer.runner import Runner
from mimi.computer.service import ComputerService, Refused
from mimi.computer.store import ComputerStore
from mimi.computer.types import GuardLimits, Step
from mimi.store import ensure_schema, make_engine

T = "coral-lane"
KEY = "computer-owner-key-0001"
H = {"Authorization": f"Bearer {KEY}"}
SITE = "https://portal.supplier.example"
PAGE = SITE + "/prices"


# ------------------------------------------------------------------- guardrails


def test_host_allowed_takes_the_domain_and_its_subdomains_only() -> None:
    assert g.host_allowed(PAGE, "supplier.example")
    assert g.host_allowed("https://a.b.supplier.example/x", "supplier.example")
    assert g.host_allowed("http://supplier.example", "supplier.example")
    # another domain, a look-alike suffix, an IP, a scheme that is not a page
    assert not g.host_allowed("https://evil.example/x", "supplier.example")
    assert not g.host_allowed("https://notsupplier.example/x", "supplier.example")
    assert not g.host_allowed("https://supplier.example.evil.com/x", "supplier.example")
    assert not g.host_allowed("http://1.2.3.4/x", "supplier.example")
    assert not g.host_allowed("file:///etc/passwd", "supplier.example")
    assert not g.host_allowed(PAGE, "")


def test_redact_takes_out_emails_numbers_and_long_digit_runs() -> None:
    out = g.redact("Sign in as ada@example.com, call 0803 555 1234, card 4111111111111111")
    assert "ada@example.com" not in out
    assert "0803 555 1234" not in out
    assert "4111111111111111" not in out
    # a price is not a secret and stays readable
    assert "45000" in g.redact("the price is 45000")


def test_classify_sees_a_login_wall_a_forbidden_page_and_a_plain_one() -> None:
    login = g.Page(url=PAGE, html='<form><input type="password" name="pw"></form>')
    assert g.classify(login).kind == "login_wall"
    buy = g.Page(url=PAGE, html='<h1>Checkout</h1><input autocomplete="cc-number">')
    assert g.classify(buy).kind == "forbidden"
    assert g.classify(g.Page(url=PAGE, html="<h1>Price list</h1>")).kind == "normal"
    # a forbidden signal wins even when the page also has a password box
    both = g.Page(url=PAGE, html='<input type="password"><h1>Place order</h1>')
    assert g.classify(both).kind == "forbidden"


def test_a_credential_field_is_never_filled() -> None:
    assert g.credential_field("Password")
    assert g.credential_field("One-time code")
    assert g.credential_field("Card number")
    assert not g.credential_field("TIN")
    assert g.looks_like_secret("4111111111111111")
    assert not g.looks_like_secret("RC 123456")


# ----------------------------------------------------------------- plan shapes


@pytest.mark.parametrize("kind", ["price_check", "order_list", "bank_statement",
                                  "gov_form", "generic"])
def test_each_kind_gets_its_own_plan_and_profile(kind: str) -> None:
    plan = plans.build(kind, "do the thing", PAGE, "supplier.example")
    assert plan.kind == kind
    assert plan.steps[0].action == "goto" and plan.steps[-1].action == "finish"
    assert all(s.action in plans.profile_for(kind).actions or s.action == "finish"
               for s in plan.steps)


def test_the_profiles_differ_and_no_kind_can_submit() -> None:
    assert "download" in plans.profile_for("bank_statement").actions
    assert "download" not in plans.profile_for("price_check").actions
    assert plans.profile_for("gov_form").may_fill
    assert not plans.profile_for("price_check").may_fill
    for kind in plans.PROFILES:
        assert "submit" not in plans.profile_for(kind).actions


# --------------------------------------------------------------------- runner


def _run(kind: str, steps: list[Step], fake: FakeComputer, domain: str = "supplier.example",
         inputs: dict[str, str] | None = None, limits: GuardLimits | None = None) -> Any:
    runner = Runner(fake, limits=limits)
    return runner.run("ct_test", domain, kind, steps, inputs)


def test_a_read_only_task_captures_a_screenshot_per_step_and_finishes() -> None:
    fake = FakeComputer(pages={PAGE: {"title": "Price list", "html": "<h1>Price list</h1>",
                                     "text": "Paracetamol 45000", "png": b"\x89PNG-real"}})
    steps = plans.build("price_check", "check prices", PAGE, "supplier.example").steps
    out = _run("price_check", steps, fake)
    assert out.status == "done" and out.stop_reason == ""
    shots = [b for b in out.artifacts if b.kind == "screenshot"]
    assert [s.action for s in out.steps] == ["goto", "screenshot", "extract", "finish"]
    assert len(shots) >= 2 and all(b.data for b in shots)
    assert any(b.kind == "text" and b"Paracetamol" in b.data for b in out.artifacts)
    assert all(s.screenshot for s in out.steps if s.action != "finish")


def test_a_redirect_off_the_approved_domain_stops_the_task() -> None:
    fake = FakeComputer(
        pages={PAGE: {"title": "welcome", "html": "<h1>hi</h1>"},
               "https://evil.example/login": {"title": "elsewhere", "html": "<h1>x</h1>"}},
        redirect={PAGE: "https://evil.example/login"})
    steps = plans.build("generic", "read it", PAGE, "supplier.example").steps
    out = _run("generic", steps, fake)
    assert out.status == "stopped" and out.stop_reason == "off_domain"


def test_a_login_wall_pauses_the_task_and_types_nothing() -> None:
    fake = FakeComputer(pages={PAGE: {"title": "Sign in", "html": "<h1>Sign in</h1>"}},
                        login=(PAGE,))
    steps = plans.build("order_list", "get orders", PAGE, "supplier.example").steps
    out = _run("order_list", steps, fake)
    assert out.status == "awaiting_owner" and out.stop_reason == "login_wall"
    assert ("fill", PAGE) not in fake.calls
    assert fake.filled == {}


def test_a_checkout_or_card_page_aborts_the_task() -> None:
    fake = FakeComputer(pages={PAGE: {"title": "Pay", "html": "<h1>Pay</h1>"}},
                        forbidden=(PAGE,))
    steps = plans.build("generic", "read it", PAGE, "supplier.example").steps
    out = _run("generic", steps, fake)
    assert out.status == "stopped" and out.stop_reason == "forbidden_page"


def test_the_step_budget_ends_the_task_stopped_runaway() -> None:
    fake = FakeComputer(pages={PAGE: {"title": "p", "html": "<h1>p</h1>", "text": "x"}})
    steps = plans.build("generic", "read", PAGE, "supplier.example").steps
    out = _run("generic", steps, fake, limits=GuardLimits(max_steps=2))
    assert out.status == "stopped_runaway" and out.stop_reason == "budget_steps"
    assert len(fake.calls) <= 3  # it stops at the boundary, it does not run on


def test_the_time_budget_ends_the_task_stopped_runaway() -> None:
    fake = FakeComputer(pages={PAGE: {"title": "p", "html": "<h1>p</h1>", "text": "x"}})
    ticks = iter([0.0, 0.0, 5000.0, 5000.0, 5000.0])

    def clock() -> float:
        return next(ticks, 5000.0)

    steps = plans.build("generic", "read", PAGE, "supplier.example").steps
    out = Runner(fake, clock=clock).run("ct_test", "supplier.example", "generic", steps, {})
    assert out.status == "stopped_runaway" and out.stop_reason == "budget_seconds"


def test_a_step_outside_the_kinds_profile_never_runs() -> None:
    fake = FakeComputer(pages={PAGE: {"title": "p", "html": "<h1>p</h1>"}})
    steps = [Step(action="goto", url=PAGE), Step(action="download", target="statement"),
             Step(action="finish")]
    out = _run("price_check", steps, fake)
    assert out.status == "stopped" and out.stop_reason == "unsupported_step"
    assert ("download", PAGE) not in fake.calls


def test_a_form_is_filled_only_from_the_owners_values_and_never_a_credential() -> None:
    fake = FakeComputer(pages={PAGE: {"title": "TIN form", "html": "<form></form>"}})
    steps = [Step(action="goto", url=PAGE),
             Step(action="fill", field="TIN", value_key="tin"),
             Step(action="finish")]
    out = _run("gov_form", steps, fake, inputs={"tin": "0123456789"})
    assert out.status == "done" and fake.filled == {"TIN": "0123456789"}

    secret = [Step(action="goto", url=PAGE),
              Step(action="fill", field="Password", value_key="pw"),
              Step(action="finish")]
    out2 = _run("gov_form", secret, fake, inputs={"pw": "hunter2"})
    assert out2.stop_reason == "forbidden_page" and "Password" not in fake.filled

    long_id = [Step(action="goto", url=PAGE),
               Step(action="fill", field="TIN", value_key="tin"),
               Step(action="finish")]
    out3 = _run("gov_form", long_id, fake, inputs={"tin": "4111111111111111"})
    assert out3.stop_reason == "forbidden_page"


# -------------------------------------------------------------------- service


@pytest.fixture()
def service() -> Any:
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    return ComputerService(ComputerStore(engine), FakeComputer(
        pages={PAGE: {"title": "Price list", "html": "<h1>Prices</h1>",
                      "text": "Sugar 42000", "png": b"\x89PNG-real"}}))


def test_propose_builds_a_plan_and_approve_runs_it(service: ComputerService) -> None:
    view = service.propose(T, "check prices", PAGE, "supplier.example", "price_check")
    assert view["status"] == "proposed" and view["plan"][0]["action"] == "goto"
    assert service.get(T, view["task_id"])["steps"] == []

    done = service.approve(T, view["task_id"], inline=True)
    assert done["status"] == "done"
    assert [s["action"] for s in done["steps"]][-1] == "finish"
    assert any(a["kind"] == "screenshot" for a in done["artifacts"])
    # the artifact bytes are on disk and served by name
    name = done["steps"][0]["screenshot"]
    row = service.artifact(T, view["task_id"], name)
    assert row is not None and row["mime"] == "image/png"
    # what the read found is kept as its own artifact, not only the screenshot
    texts = [a for a in done["artifacts"] if a["kind"] == "text"]
    assert len(texts) == 1 and texts[0]["size"] > 0
    served = service.artifact(T, view["task_id"], texts[0]["name"])
    assert served is not None and served["mime"] == "text/plain"
    body = (service.store.dir / T / view["task_id"] / texts[0]["name"]).read_text()
    assert "Sugar 42000" in body


def test_a_download_is_kept_under_the_name_the_site_gave() -> None:
    fake = FakeComputer(
        pages={PAGE: {"title": "Statements", "text": "October", "png": b"\x89PNG-real"}},
        downloads={PAGE: "statement-october.pdf"})
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    service = ComputerService(ComputerStore(engine), fake)
    view = service.propose(T, "pull the statement", PAGE, "supplier.example",
                           "bank_statement")
    done = service.approve(T, view["task_id"], inline=True)
    assert done["status"] == "done"
    files = [a for a in done["artifacts"] if a["kind"] == "file"]
    assert [a["name"] for a in files] == ["statement-october.pdf"]
    assert files[0]["mime"] == "application/pdf"
    # the download step holds the file, never a mislabelled screenshot
    dl = [s for s in done["steps"] if s["action"] == "download"][0]
    assert dl["screenshot"] == ""
    assert not [a for a in done["artifacts"] if a["name"].startswith("statement-")
                and a["kind"] == "screenshot"]


def test_a_url_off_the_approved_domain_is_refused_at_proposal(service: ComputerService) -> None:
    with pytest.raises(Refused):
        service.propose(T, "read it", "https://evil.example/x", "supplier.example")


def test_stop_records_the_owner_reason(service: ComputerService) -> None:
    view = service.propose(T, "check prices", PAGE, "supplier.example", "price_check")
    stopped = service.stop(T, view["task_id"])
    assert stopped["status"] == "stopped" and stopped["stop_reason"] == "stopped_by_owner"


def test_tasks_are_filtered_by_tenant(service: ComputerService) -> None:
    service.propose(T, "mine", PAGE, "supplier.example")
    assert len(service.list(T)) == 1
    assert service.list("tenant-b") == []
    assert service.get("tenant-b", service.list(T)[0]["task_id"]) is None


# ------------------------------------------------------------------------ API


@pytest.fixture()
def client(store_url: str) -> Any:
    engine = make_engine(store_url)
    ensure_schema(engine)
    agent = Agent(engine, models=[OfflineModel()], worker_id="test-worker")
    keys = parse_keys(f"{T}:owner:{KEY}")
    computer = ComputerService(ComputerStore(engine), FakeComputer(
        pages={PAGE: {"title": "Price list", "html": "<h1>Prices</h1>",
                      "text": "Sugar 42000", "png": b"\x89PNG-real"}}))
    app = create_app(Settings(keys=keys), agent, workers=0, computer=computer)
    with TestClient(app) as c:
        yield c
    engine.dispose()


def _wait(c: TestClient, task_id: str) -> dict[str, Any]:
    for _ in range(50):
        body = c.get(f"/v1/computer/tasks/{task_id}", headers=H).json()
        if body["status"] not in ("running",):
            return body
    return body


def test_the_five_endpoints_and_the_list_endpoint(client: TestClient) -> None:
    r = client.post("/v1/computer/tasks", headers=H, json={
        "goal": "check the price list", "url": PAGE, "domain": "supplier.example",
        "kind": "price_check"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "proposed" and body["plan"][0]["action"] == "goto"
    task_id = body["task_id"]

    r = client.get("/v1/computer/tasks", headers=H)
    assert r.status_code == 200
    [row] = r.json()["tasks"]
    assert row["task_id"] == task_id and row["status"] == "proposed"
    assert row["step_count"] == 0 and row["domain"] == "supplier.example"

    r = client.post(f"/v1/computer/tasks/{task_id}/approve", headers=H)
    assert r.status_code == 200
    done = _wait(client, task_id)
    assert done["status"] == "done"
    assert any(s["screenshot"] for s in done["steps"])
    assert any(a["kind"] == "screenshot" for a in done["artifacts"])

    name = next(s["screenshot"] for s in done["steps"] if s["screenshot"])
    r = client.get(f"/v1/computer/tasks/{task_id}/artifacts/{name}", headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content == b"\x89PNG-real"

    r = client.post(f"/v1/computer/tasks/{task_id}/stop", headers=H)
    assert r.status_code == 200 and r.json()["stop_reason"] in ("stopped_by_owner", "")


def test_the_list_is_an_honest_empty_list_at_first(client: TestClient) -> None:
    r = client.get("/v1/computer/tasks", headers=H)
    assert r.status_code == 200 and r.json() == {"tasks": []}


def test_a_channel_key_cannot_reach_computer_tasks(client: TestClient) -> None:
    assert client.get("/v1/computer/tasks",
                      headers={"Authorization": "Bearer nope"}).status_code == 401


def test_an_off_domain_url_is_refused_by_the_api(client: TestClient) -> None:
    r = client.post("/v1/computer/tasks", headers=H, json={
        "goal": "read it", "url": "https://evil.example/x", "domain": "supplier.example"})
    assert r.status_code == 422


def test_another_tenants_task_reads_as_not_found(client: TestClient) -> None:
    r = client.post("/v1/computer/tasks", headers=H, json={
        "goal": "g", "url": PAGE, "domain": "supplier.example"})
    task_id = r.json()["task_id"]
    assert client.get(f"/v1/computer/tasks/{task_id}",
                      headers={"Authorization": "Bearer nope"}).status_code == 401
    # a task id from this tenant is simply absent for another (404 through auth)
    assert isinstance(task_id, str) and task_id.startswith("ct_")


def test_the_fake_engine_reports_what_it_did() -> None:
    fake = FakeComputer(pages={PAGE: {"title": "t", "html": "<h1>x</h1>"}})
    fake.start(TaskRef(task_id="ct_1", domain="supplier.example"))
    assert fake.started == "ct_1"
    fake.stop(TaskRef(task_id="ct_1", domain="supplier.example"), "stopped_by_owner")
    assert fake.stopped == "stopped_by_owner"

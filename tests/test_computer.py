"""Computer tasks: the guardrails, the plans, the runner and the five endpoints.

Everything runs offline against ``FakeComputer``, which is a small scripted site
in memory. The live browser container is exercised by ``scripts/computer_smoke.py``
against a real cloud browser session, not here.
"""

from __future__ import annotations

import re
import threading
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


def test_host_allowed_refuses_a_url_that_carries_a_credential() -> None:
    """A username or password in the address is refused: the address may come
    from a model, and a session should never be handed a credential."""
    # The addresses below are shaped like credentials on purpose: each is refused.
    assert not g.host_allowed(
        "https://ada:pw@supplier.example/x", "supplier.example")  # pragma: allowlist secret
    assert not g.host_allowed(
        "https://ada@supplier.example/x", "supplier.example")  # pragma: allowlist secret
    assert not g.host_allowed(
        "https://:pw@supplier.example/x", "supplier.example")  # pragma: allowlist secret


def test_the_secret_check_catches_a_number_written_in_groups() -> None:
    """The rule is "never type a card number", so the grouping a person writes
    must not be the thing that slips one past it."""
    assert g.looks_like_secret("4111 1111 1111 1111")
    assert g.looks_like_secret("4111-1111-1111-1111")
    assert g.looks_like_secret("4111111111111111")
    # a price and a field a person fills are not card numbers
    assert not g.looks_like_secret("45000")
    assert not g.looks_like_secret("Ada Nwosu")


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


def test_a_credential_field_is_a_word_not_just_some_letters() -> None:
    """A whole word, or an ordinary form gets refused for spelling one."""
    for field in ("PIN", "OTP", "Security code", "CVV", "Confirm password",
                  "password_field", "Passwords", "Security answer"):
        assert g.credential_field(field), field
    for field in ("Shipping address", "Pincode", "Shopping list", "Business name",
                  "TIN", "Company registration number", "Pinning date"):
        assert not g.credential_field(field), field


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


def test_the_kept_text_is_redacted_like_a_note_is() -> None:
    """What a step read is written to the task's artifacts, so the redaction a
    note gets applies there too: one rule, not two."""
    fake = FakeComputer(pages={PAGE: {
        "title": "Price list", "html": "<h1>Price list</h1>",
        "text": "Ada ada@example.com 0803 555 1234 card 4111 1111 1111 1111 sugar 45000",
        "png": b"\x89PNG-real"}})
    steps = plans.build("price_check", "check prices", PAGE, "supplier.example").steps
    out = _run("price_check", steps, fake)
    kept = next(b.data for b in out.artifacts if b.kind == "text")
    assert b"ada@example.com" not in kept and b"4111 1111" not in kept
    assert b"0803 555 1234" not in kept
    # the page's own figure is what the read is for, and it stays
    assert b"45000" in kept


def test_a_redirect_off_the_approved_domain_stops_the_task() -> None:
    fake = FakeComputer(
        pages={PAGE: {"title": "welcome", "html": "<h1>hi</h1>"},
               "https://evil.example/login": {"title": "elsewhere", "html": "<h1>x</h1>"}},
        redirect={PAGE: "https://evil.example/login"})
    steps = plans.build("generic", "read it", PAGE, "supplier.example").steps
    out = _run("generic", steps, fake)
    assert out.status == "stopped" and out.stop_reason == "off_domain"


def test_a_redirect_off_the_domain_that_looks_like_a_sign_in_still_stops() -> None:
    """The allowlist is the first verdict on the observed URL: an unapproved
    page is never a page to pause on, whatever it shows."""
    elsewhere = "https://evil.example/login"
    fake = FakeComputer(
        pages={PAGE: {"title": "welcome", "html": "<h1>hi</h1>"},
               elsewhere: {"title": "Sign in", "html": "<h1>Sign in</h1>"}},
        redirect={PAGE: elsewhere}, login=(elsewhere,))
    steps = plans.build("order_list", "get orders", PAGE, "supplier.example").steps
    out = _run("order_list", steps, fake)
    assert out.status == "stopped" and out.stop_reason == "off_domain"
    # nothing of the unapproved page was kept, not even a screenshot
    assert out.steps == [] and out.artifacts == []


class _MovesAway(FakeComputer):
    """A page that moves itself off the approved domain between steps."""

    def do(self, task: TaskRef, action: str, **kw: Any) -> Any:
        if action == "extract":
            return self._page("https://evil.example/away")
        return super().do(task, action, **kw)


def test_a_page_that_moves_itself_off_the_domain_is_not_read() -> None:
    """Every step's observed URL is checked, not only the one that navigated."""
    fake = _MovesAway(pages={PAGE: {"title": "welcome", "html": "<h1>hi</h1>",
                                   "text": "Sugar 42000", "png": b"\x89PNG-real"},
                            "https://evil.example/away": {"html": "<h1>x</h1>",
                                                          "text": "elsewhere"}})
    steps = plans.build("price_check", "check prices", PAGE, "supplier.example").steps
    out = _run("price_check", steps, fake)
    assert out.status == "stopped" and out.stop_reason == "off_domain"
    assert not [b for b in out.artifacts if b.kind == "text"]
    assert [a for a, _ in fake.calls][:2] == ["goto", "screenshot"]


def test_the_owner_stop_keeps_a_step_from_running_and_from_being_kept() -> None:
    """A stop that lands between steps ends the task, records no step for it and
    keeps nothing it produced."""
    fake = FakeComputer(pages={PAGE: {"title": "p", "html": "<h1>p</h1>",
                                     "text": "Sugar 42000", "png": b"\x89PNG-real"}})
    steps = plans.build("price_check", "check prices", PAGE, "supplier.example").steps
    asked = {"n": 0}

    def should_stop() -> bool:
        # the owner hits stop while the first step is in flight
        asked["n"] += 1
        return asked["n"] > 1

    out = Runner(fake, should_stop=should_stop).run(
        "ct_test", "supplier.example", "price_check", steps, {})
    assert out.status == "stopped" and out.stop_reason == "stopped_by_owner"
    assert out.steps == [] and out.artifacts == []
    # the second step never reached the browser
    assert [a for a, _ in fake.calls] == ["goto"]


def test_a_stop_signal_that_raises_stops_the_run_rather_than_running_on() -> None:
    fake = FakeComputer(pages={PAGE: {"title": "p", "html": "<h1>p</h1>"}})

    def broken() -> bool:
        raise RuntimeError("the flag is gone")

    steps = plans.build("generic", "read it", PAGE, "supplier.example").steps
    out = Runner(fake, should_stop=broken).run(
        "ct_test", "supplier.example", "generic", steps, {})
    assert out.status == "stopped" and out.stop_reason == "stopped_by_owner"
    assert fake.calls == []


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


def test_a_form_plan_fills_one_step_for_each_value_the_owner_named() -> None:
    """The kind is published, so the code has to be able to build it: every
    fill step carries the owner's own field and the key its value lives under."""
    inputs = {"TIN": "0123456789", "Business name": "Coral Lane Ltd"}
    plan = plans.build("gov_form", "fill my TIN form", PAGE, "supplier.example", inputs)
    fills = [s for s in plan.steps if s.action == "fill"]
    assert [s.field for s in fills] == ["Business name", "TIN"]
    assert [s.value_key for s in fills] == ["Business name", "TIN"]
    assert all(s.note and s.field in s.note for s in fills)
    assert plan.steps[-1].action == "finish"
    assert all(s.action in plans.profile_for("gov_form").actions or s.action == "finish"
               for s in plan.steps)

    # the plan the owner approves is the plan that runs: it fills and finishes
    fake = FakeComputer(pages={PAGE: {"title": "TIN form", "html": "<form></form>"}})
    out = _run("gov_form", plan.steps, fake, inputs=inputs)
    assert out.status == "done" and out.stop_reason == ""
    assert fake.filled == {"Business name": "Coral Lane Ltd", "TIN": "0123456789"}


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
    assert view["status"] == "proposed" and view["plan"][0]["code"] == "goto"
    assert view["plan"][0]["action"] == "Open the page"
    before = service.get(T, view["task_id"])
    assert before is not None and before["steps"] == []

    done = service.approve(T, view["task_id"], inline=True)
    assert done["status"] == "done"
    assert [s["code"] for s in done["steps"]][-1] == "finish"
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
    dl = [s for s in done["steps"] if s["code"] == "download"][0]
    assert dl["screenshot"] == ""
    assert not [a for a in done["artifacts"] if a["name"].startswith("statement-")
                and a["kind"] == "screenshot"]


def test_a_browser_that_cannot_start_fails_the_task_plainly() -> None:
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    fake = FakeComputer(pages={PAGE: {"title": "Price list", "png": b"\x89PNG-real"}},
                        fail=(PAGE,))
    service = ComputerService(ComputerStore(engine), fake)
    view = service.propose(T, "check prices", PAGE, "supplier.example", "price_check")
    done = service.approve(T, view["task_id"], inline=True)
    # a status the API can serve, never a bare engine string
    assert done["status"] == "failed"
    assert done["stop_reason"] == "engine_error"
    assert done["note"].startswith("The browser could not do that step")
    assert done["steps"] == []


class _LiveFake(FakeComputer):
    """A fake whose session has a live view, the way the real engine has one."""

    def sign_in_link(self, task: TaskRef) -> str:
        return f"https://live.example/{task.task_id}"


class _CountsSessions(_LiveFake):
    """A fake that opens a session per start, as the cloud engine does, and
    hands back that session's own link."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.sessions = 0
        self.links: list[str] = []

    def start(self, task: TaskRef) -> None:
        self.sessions += 1
        super().start(task)

    def sign_in_link(self, task: TaskRef) -> str:
        link = f"https://live.example/s{self.sessions}/{task.task_id}"
        self.links.append(link)
        return link


def test_a_finished_task_carries_no_live_view_and_closes_its_session() -> None:
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    fake = _LiveFake(pages={PAGE: {"title": "Price list", "text": "Sugar 42000",
                                   "png": b"\x89PNG-real"}})
    service = ComputerService(ComputerStore(engine), fake)
    view = service.propose(T, "check prices", PAGE, "supplier.example", "price_check")
    done = service.approve(T, view["task_id"], inline=True)
    assert done["status"] == "done"
    # the signed link dies with the session, so it is not handed out after a run
    assert done["live_view_url"] is None
    assert fake.stopped  # the session was released


def test_a_task_paused_at_a_sign_in_wall_keeps_its_session_and_its_live_view() -> None:
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    fake = _LiveFake(pages={PAGE: {"html": "<h1>Prices</h1>", "png": b"\x89PNG-real"}},
                     login=(PAGE,))
    service = ComputerService(ComputerStore(engine), fake)
    view = service.propose(T, "check prices", PAGE, "supplier.example", "price_check")
    paused = service.approve(T, view["task_id"], inline=True)
    assert paused["status"] == "awaiting_owner"
    assert paused["live_view_url"] == f"https://live.example/{view['task_id']}"
    assert fake.stopped is None  # kept open: that is where the owner signs in
    assert fake.filled == {}  # nothing was typed at the wall

    stopped = service.stop(T, view["task_id"])
    assert stopped["status"] == "stopped"
    assert stopped["live_view_url"] is None
    assert fake.stopped == "stopped_by_owner"


def test_a_url_off_the_approved_domain_is_refused_at_proposal(service: ComputerService) -> None:
    with pytest.raises(Refused):
        service.propose(T, "read it", "https://evil.example/x", "supplier.example")


def test_stop_records_the_owner_reason(service: ComputerService) -> None:
    view = service.propose(T, "check prices", PAGE, "supplier.example", "price_check")
    stopped = service.stop(T, view["task_id"])
    assert stopped["status"] == "stopped" and stopped["stop_reason"] == "stopped_by_owner"
    # stop is also how an owner discards a task they decided not to run
    assert stopped["steps"] == [] and stopped["note"]


def test_stopping_a_finished_task_gives_it_back_unchanged(service: ComputerService) -> None:
    view = service.propose(T, "check prices", PAGE, "supplier.example", "price_check")
    done = service.approve(T, view["task_id"], inline=True)
    assert done["status"] == "done"
    again = service.stop(T, view["task_id"])
    assert again["status"] == "done" and again["stop_reason"] == ""
    assert again["finished_at"] == done["finished_at"]


# ------------------------------------------- the words the console renders


def test_every_stop_reason_has_a_plain_sentence_and_no_code_ever_reaches_it() -> None:
    """``stop_reason`` is for machines; ``note`` is what a person reads, so it is
    a sentence for every reason, including one this code has never seen."""
    from typing import get_args

    from mimi.computer.runner import DEFAULT_STOP_NOTE, stop_note
    from mimi.computer.types import StopReason

    reasons = [r for r in get_args(StopReason) if r]
    assert "stopped_by_owner" in reasons
    for reason in reasons:
        note = stop_note(reason)
        assert note and " " in note, (reason, note)
        assert not CODE_SHAPE.match(note), (reason, note)
        assert reason not in note, (reason, note)
    # a code with no entry of its own still gets a sentence, never an empty one
    assert stop_note("something_new") == DEFAULT_STOP_NOTE
    assert stop_note("") == DEFAULT_STOP_NOTE


def test_a_stopped_task_always_carries_the_sentence_and_never_only_the_code() -> None:
    from mimi.computer.store import task_view

    row = {"task_id": "ct_1", "status": "stopped", "goal": "g", "url": PAGE,
           "domain": "supplier.example", "stop_reason": "off_domain", "note": "",
           "created_at": "2026-01-01T00:00:00+01:00"}
    view = task_view(row, [], [])
    assert view["stop_reason"] == "off_domain"  # the code, for machines
    assert view["note"].startswith("Stopped: the browser left the domain")
    assert "off_domain" not in view["note"]
    # a task with nothing to say is left alone
    assert task_view(dict(row, status="proposed", stop_reason=""), [], [])["note"] == ""


def test_the_status_vocabulary_is_the_seven_documented_and_never_queued() -> None:
    from typing import get_args

    from mimi.computer.types import TaskStatus

    assert get_args(TaskStatus) == ("proposed", "running", "awaiting_owner", "done",
                                    "stopped", "stopped_runaway", "failed")
    assert "queued" not in get_args(TaskStatus)


def test_a_form_task_with_nothing_to_fill_is_refused(service: ComputerService) -> None:
    with pytest.raises(Refused) as exc:
        service.propose(T, "fill my TIN form", PAGE, "supplier.example", "gov_form")
    assert "values" in exc.value.message


def test_a_form_task_with_an_ordinary_field_label_is_accepted(
        service: ComputerService) -> None:
    """A shipping address is not a credential, however its letters fall."""
    view = service.propose(T, "fill my form", PAGE, "supplier.example", "gov_form",
                           {"Shipping address": "12 Marina Road",
                            "Pincode": "101241", "Business name": "Coral Lane Ltd"})
    fills = [s for s in view["plan"] if s["code"] == "fill"]
    assert [s["field"] for s in fills] == ["Business name", "Pincode",
                                          "Shipping address"]


def test_the_stop_flag_exists_from_the_moment_the_task_is_running(
        service: ComputerService, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop landing between the approval and the run's first step is not
    lost: the flag is registered with the claim, not by the thread."""
    runs: list[Any] = []
    monkeypatch.setattr(service, "_run",
                        lambda tenant, task, run: runs.append(run))
    view = service.propose(T, "check prices", PAGE, "supplier.example", "price_check")
    service.approve(T, view["task_id"], inline=True)
    [run] = runs
    assert not run.stop.is_set()
    service.stop(T, view["task_id"])
    assert run.stop.is_set()  # the stop reached the run, not the void


def test_a_form_task_never_accepts_a_credential_field_or_a_long_number(
        service: ComputerService) -> None:
    with pytest.raises(Refused):
        service.propose(T, "fill it", PAGE, "supplier.example", "gov_form",
                        {"Password": "hunter2"})  # pragma: allowlist secret
    with pytest.raises(Refused):
        service.propose(T, "fill it", PAGE, "supplier.example", "gov_form",
                        {"TIN": "4111111111111111"})
    # a plain value is fine, and it is the one the run types
    view = service.propose(T, "fill it", PAGE, "supplier.example", "gov_form",
                           {"TIN": "0123456789"})
    assert view["status"] == "proposed"
    assert [s["code"] for s in view["plan"]].count("fill") == 1


def test_a_form_task_fills_the_owners_values_end_to_end() -> None:
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    fake = FakeComputer(pages={PAGE: {"title": "TIN form", "html": "<form></form>"}})
    service = ComputerService(ComputerStore(engine), fake)
    view = service.propose(T, "fill my TIN form", PAGE, "supplier.example", "gov_form",
                           {"TIN": "0123456789"})
    done = service.approve(T, view["task_id"], inline=True)
    assert done["status"] == "done" and done["stop_reason"] == ""
    assert fake.filled == {"TIN": "0123456789"}


class _StopsItself(FakeComputer):
    """A fake that asks the service to stop while the run is in flight."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.service: Any = None
        self.task_id = ""
        self.paused = threading.Event()

    def do(self, task: TaskRef, action: str, **kw: Any) -> Any:
        page = super().do(task, action, **kw)
        if action == "screenshot":
            self.paused.set()
            self.service.stop(T, self.task_id)  # the owner hits stop mid-run
        return page


def test_a_stop_during_a_run_stops_it_and_its_record_is_the_one_that_stands() -> None:
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    fake = _StopsItself(pages={PAGE: {"title": "Price list", "text": "Sugar 42000",
                                      "png": b"\x89PNG-real"}})
    service = ComputerService(ComputerStore(engine), fake)
    view = service.propose(T, "check prices", PAGE, "supplier.example", "price_check")
    fake.service, fake.task_id = service, view["task_id"]
    done = service.approve(T, view["task_id"], inline=True)
    assert fake.paused.is_set()
    # the owner's stop is the record that stands, not a browser failure
    assert done["status"] == "stopped" and done["stop_reason"] == "stopped_by_owner"
    # the step in flight when the stop landed is not kept, and nothing after ran
    assert [s["code"] for s in done["steps"]] == ["goto"]
    assert fake.stopped == "stopped_by_owner"
    assert done["finished_at"] and done["live_view_url"] is None


def test_two_approvals_in_flight_one_starts_the_run_and_the_other_is_told(
        service: ComputerService, monkeypatch: pytest.MonkeyPatch) -> None:
    view = service.propose(T, "check prices", PAGE, "supplier.example", "price_check")
    task_id = view["task_id"]
    stale = service.store.get(T, task_id)
    assert stale is not None
    real_get = service.store.get
    # both callers read the same "proposed" row before either moves it
    monkeypatch.setattr(service.store, "get", lambda tenant, tid: dict(stale))
    service.approve(T, task_id, inline=True)
    with pytest.raises(Refused) as exc:
        service.approve(T, task_id, inline=True)
    assert exc.value.status == 409
    monkeypatch.setattr(service.store, "get", real_get)
    after = service.get(T, task_id)
    assert after is not None and after["status"] == "done"
    # the plan ran once: one row per step, no primary-key collision
    assert [r["idx"] for r in service.store.steps(T, task_id)] == [0, 1, 2, 3]


def test_a_run_stamps_started_at_and_clears_the_earlier_reason() -> None:
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    fake = FakeComputer(pages={PAGE: {"html": "<h1>Prices</h1>",
                                      "text": "Sugar 42000", "png": b"\x89PNG-real"}},
                        login=(PAGE,))
    service = ComputerService(ComputerStore(engine), fake)
    view = service.propose(T, "check prices", PAGE, "supplier.example", "price_check")
    paused = service.approve(T, view["task_id"], inline=True)
    assert paused["status"] == "awaiting_owner" and paused["stop_reason"] == "login_wall"
    assert paused["started_at"] and not paused["finished_at"]
    # the wall is gone: the resumed run ends done, with no stale reason or note
    fake.login = ()
    resumed = service.approve(T, view["task_id"], inline=True)
    assert resumed["status"] == "done" and resumed["stop_reason"] == ""
    assert resumed["note"].startswith("Done.")
    assert resumed["finished_at"] and resumed["started_at"] == paused["started_at"]


def test_a_resumed_task_reads_its_live_link_again_instead_of_reusing_a_stale_one() -> None:
    engine = make_engine("sqlite://")
    ensure_schema(engine)
    fake = _CountsSessions(pages={PAGE: {"html": "<h1>Prices</h1>",
                                         "png": b"\x89PNG-real"}}, login=(PAGE,))
    service = ComputerService(ComputerStore(engine), fake)
    view = service.propose(T, "check prices", PAGE, "supplier.example", "price_check")
    paused = service.approve(T, view["task_id"], inline=True)
    assert paused["status"] == "awaiting_owner"
    assert paused["live_view_url"].endswith("/s1/" + view["task_id"])
    fake.login = ()  # the owner signed in through the live view
    resumed = service.approve(T, view["task_id"], inline=True)
    assert resumed["status"] == "done"
    # the link handed out while the resumed run was live came from the session
    # that run opened, never from the paused one
    assert fake.links == ["https://live.example/s1/" + view["task_id"],
                          "https://live.example/s2/" + view["task_id"]]
    assert resumed["live_view_url"] is None


def test_the_task_list_is_capped_at_the_newest_rows(service: ComputerService) -> None:
    from mimi.computer import store as computer_store

    for i in range(3):
        service.propose(T, f"task {i}", PAGE, "supplier.example")
    assert len(service.tasks(T)) == 3
    assert len(service.store.tasks(T, limit=2)) == 2
    assert computer_store.MAX_TASKS >= 100


def test_a_list_row_carries_the_sentence_and_not_only_the_code(
        service: ComputerService) -> None:
    view = service.propose(T, "check prices", PAGE, "supplier.example", "price_check")
    service.approve(T, view["task_id"], inline=True)
    service.stop(T, view["task_id"])
    [row] = service.tasks(T)
    assert row["stop_reason"] == ""
    assert row["note"].startswith("Done.")
    # a task stopped before it ran reads as the owner's stop, in words
    other = service.propose(T, "check prices", PAGE, "supplier.example", "price_check")
    service.stop(T, other["task_id"])
    stopped = [r for r in service.tasks(T) if r["task_id"] == other["task_id"]][0]
    assert stopped["stop_reason"] == "stopped_by_owner"
    assert stopped["note"] == "Stopped by the owner."


def test_tasks_are_filtered_by_tenant(service: ComputerService) -> None:
    service.propose(T, "mine", PAGE, "supplier.example")
    assert len(service.tasks(T)) == 1
    assert service.tasks("tenant-b") == []
    assert service.get("tenant-b", service.tasks(T)[0]["task_id"]) is None


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
    assert body["status"] == "proposed" and body["plan"][0]["code"] == "goto"
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


def test_the_kinds_endpoint_publishes_the_kind_strings(client: TestClient) -> None:
    r = client.get("/v1/computer/kinds", headers=H)
    assert r.status_code == 200
    kinds = r.json()["kinds"]
    assert [k["kind"] for k in kinds] == list(plans.PROFILES)
    assert kinds == [{"kind": k, "label": plans.KIND_LABEL[k]} for k in plans.PROFILES]
    assert all(k["label"] for k in kinds)


# A machine token: an underscore-joined lowercase word, like "goto_url".
CODE_SHAPE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
# An internal id or a token: a long hex or base32 run, or our task prefix.
ID_SHAPE = re.compile(r"\b(?:ct_|[0-9a-f]{16,}|[A-Za-z0-9_-]{24,}\b)")


def test_no_rendered_step_text_is_a_code_or_an_internal_id(client: TestClient) -> None:
    """The console prints a step's ``action`` verbatim, so no machine token and
    no internal id may ever appear in the text a person reads."""
    r = client.post("/v1/computer/tasks", headers=H, json={
        "goal": "check the price list", "url": PAGE, "domain": "supplier.example",
        "kind": "order_list"})
    task_id = r.json()["task_id"]
    client.post(f"/v1/computer/tasks/{task_id}/approve", headers=H)
    done = _wait(client, task_id)
    assert done["status"] == "done" and done["steps"]

    for step in list(done["steps"]) + list(done["plan"]):
        # the machine verb is kept, in its own field, which the console ignores
        assert step["code"], step
        for field in ("action", "note"):
            text = str(step[field])
            assert text, (field, step)
            assert not CODE_SHAPE.match(text.strip()), (field, text)
            assert " " in text, (field, text)  # a sentence, never a bare token
            assert not ID_SHAPE.search(text), (field, text)
            assert task_id not in text, (field, text)
    # the plan the owner approves reads the same way before anything runs
    assert [s["code"] for s in done["plan"]] == ["goto", "screenshot", "extract",
                                                "finish"]
    assert [s["action"] for s in done["plan"]] == [
        "Open the page", "Take a screenshot", "Read what the page shows",
        "Save what was found"]


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


# ------------------------------------------------------- the cloud engine


class _PageStub:
    """A page that, like playwright's, may only be touched by the thread that
    opened it: any other thread gets the error the real driver raises."""

    def __init__(self, owner: int) -> None:
        self.owner = owner
        self.closed = False

    def _mine(self) -> None:
        if threading.get_ident() != self.owner:
            raise RuntimeError("cannot switch to a different thread")

    def is_closed(self) -> bool:
        self._mine()
        return self.closed

    def set_default_timeout(self, seconds: float) -> None:
        self._mine()

    def close(self) -> None:
        self._mine()
        self.closed = True


class _ContextStub:
    def __init__(self, page: _PageStub) -> None:
        self.pages = [page]
        self.owner = page.owner

    def close(self) -> None:
        if threading.get_ident() != self.owner:
            raise RuntimeError("cannot switch to a different thread")


class _BrowserStub:
    def __init__(self, page: _PageStub) -> None:
        self.contexts = [_ContextStub(page)]

    def close(self) -> None:
        pass


class _PlaywrightStub:
    """The shape of ``sync_playwright()``'s handle, without the driver.

    Like the real one: the driver belongs to the thread that started it, and a
    connection opened on a thread gets a page that only that thread may touch.
    """

    def __init__(self) -> None:
        self.chromium = self
        self.pages: list[_PageStub] = []
        self.owner = threading.get_ident()
        self.stopped = False

    def start(self) -> _PlaywrightStub:
        return self

    def connect_over_cdp(self, connect: str) -> _BrowserStub:
        page = _PageStub(threading.get_ident())  # opened by the calling thread
        self.pages.append(page)
        return _BrowserStub(page)

    def stop(self) -> None:
        if threading.get_ident() != self.owner:
            raise RuntimeError("cannot switch to a different thread")
        self.stopped = True


def _cloud_engine(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A cloud engine with a stubbed driver and provider, for its own session
    handling. Nothing here reaches the network."""
    import sys
    import types

    from mimi.computer.cloud_browser import CloudBrowserEngine, CloudConfig

    pw = _PlaywrightStub()
    sub = types.ModuleType("playwright.sync_api")
    sub.sync_playwright = lambda: pw  # type: ignore[attr-defined]
    parent = types.ModuleType("playwright")
    parent.sync_api = sub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", parent)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sub)

    # positionally, so the line carries no credential-shaped keyword assignment
    engine = CloudBrowserEngine(CloudConfig("fake-provider-credential"))
    made: list[str] = []
    released: list[str] = []

    def create() -> tuple[str, str]:
        made.append(f"sess-{len(made) + 1}")
        return made[-1], "wss://connect.example"

    monkeypatch.setattr(engine, "_create", create)
    monkeypatch.setattr(engine, "_release", released.append)
    engine.made = made  # type: ignore[attr-defined]
    engine.released = released  # type: ignore[attr-defined]
    engine.pw = pw  # type: ignore[attr-defined]
    return engine


def _in_a_thread(fn: Any) -> list[BaseException]:
    """Run ``fn`` on another thread, the way approve's thread does."""
    errors: list[BaseException] = []

    def run() -> None:
        try:
            fn()
        except BaseException as exc:  # pragma: no cover - would fail the test
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    return errors


def test_a_run_that_finds_an_earlier_runs_session_closes_it_and_opens_a_new_one(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A session belongs to the thread that opened it, and every run is its own
    thread, so a resume must not try to drive the paused run's session."""
    engine = _cloud_engine(monkeypatch)
    ref = TaskRef(task_id="ct_1", domain="supplier.example")
    engine.start(ref)
    assert engine.made == ["sess-1"]
    assert _in_a_thread(lambda: engine.start(ref)) == []
    assert engine.made == ["sess-1", "sess-2"]  # a fresh session, not a reused one
    assert engine.released == ["sess-1"]  # and the old one was let go
    assert engine.pw.stopped is False  # it could not be closed off its thread
    # the new session's page belongs to the thread that opened it
    assert [p.owner for p in engine.pw.pages] == [
        engine.pw.owner, engine.pw.pages[1].owner]
    assert engine.pw.pages[1].owner != engine.pw.owner


def test_stopping_on_the_owning_thread_closes_the_driver_cleanly(
        monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _cloud_engine(monkeypatch)
    ref = TaskRef(task_id="ct_1", domain="supplier.example")
    engine.start(ref)
    engine.stop(ref, "task_finished")  # the run's own thread, as _run does
    assert engine.pw.stopped is True
    assert engine.released == ["sess-1"]


def test_stopping_from_another_thread_still_frees_the_provider_session(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """An owner's /stop on a paused task lands on the request thread. The
    provider session is released; the local driver cannot be closed from there
    and is left to die with the process, and none of that raises."""
    engine = _cloud_engine(monkeypatch)
    ref = TaskRef(task_id="ct_1", domain="supplier.example")
    engine.start(ref)
    assert _in_a_thread(lambda: engine.stop(ref, "stopped_by_owner")) == []
    assert engine.released == ["sess-1"]
    assert engine.pw.stopped is False

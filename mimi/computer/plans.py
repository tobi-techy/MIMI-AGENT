"""The step plans and guardrail profiles of each task kind.

Every kind gets its own plan and its own profile, and both are code. The plan
is built from the goal, the URL and the domain the owner approved; a step that
the kind's profile does not allow is never built, so it can never run.

No kind has a submit step. A government form is filled from the owner's own
values and handed back as a screenshot for the owner to submit themselves:
submitting anything to an outside system is the owner's action, not Mimi's.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mimi.computer.types import Step, TaskKind


@dataclass(frozen=True)
class Profile:
    """What one kind of task may do. Checked by the runner before a dispatch."""

    kind: TaskKind
    actions: frozenset[str]
    may_fill: bool = False
    may_download: bool = False
    # A kind whose result the owner must look at before anything else happens.
    review_only: bool = False


READ_ONLY = frozenset({"goto", "scroll", "extract", "screenshot", "click"})
DOWNLOAD = READ_ONLY | {"download"}
FORM = frozenset({"goto", "screenshot", "fill", "click", "extract"})

PROFILES: dict[str, Profile] = {
    "price_check": Profile("price_check", READ_ONLY),
    "order_list": Profile("order_list", DOWNLOAD, may_download=True),
    "bank_statement": Profile("bank_statement", DOWNLOAD, may_download=True),
    "gov_form": Profile("gov_form", FORM, may_fill=True, review_only=True),
    "generic": Profile("generic", READ_ONLY),
}

KIND_LABEL: dict[str, str] = {
    "price_check": "Check a brand or supplier page for prices and promos",
    "order_list": "Pull the order list from a supplier page",
    "bank_statement": "Download a statement from a bank page",
    "gov_form": "Fill a form on a government or tax page and hand it back",
    "generic": "Read a page on the approved domain",
}

# The words a person reads for one step action. The machine verb stays in code
# (``code`` in a rendered step), because the console prints ``action`` verbatim:
# a token like ``goto`` must never be able to reach a distributor's screen.
PLAIN: dict[str, str] = {
    "goto": "Open the page",
    "screenshot": "Take a screenshot",
    "extract": "Read what the page shows",
    "scroll": "Scroll the page",
    "click": "Open the link the page shows",
    "fill": "Fill the form from your values",
    "download": "Download the file",
    "finish": "Save what was found",
}


def plain(action: str) -> str:
    """The plain words for one step action, never a code."""
    return PLAIN.get(action, "Read the page")


def profile_for(kind: str) -> Profile:
    return PROFILES.get(kind, PROFILES["generic"])


def plan_for(kind: str, goal: str, url: str, domain: str) -> list[Step]:
    """The plan the owner approves, built in code from the task's own fields."""
    open_step = Step(action="goto", url=url, note=f"Open {domain}")
    shot = Step(action="screenshot", note="Take a screenshot of the page")
    if kind == "price_check":
        return [
            open_step, shot,
            Step(action="extract", target="prices and promos",
                 note="Read the prices and promos shown on the page"),
            Step(action="finish", note="Save what was found as an artifact"),
        ]
    if kind == "order_list":
        return [
            open_step, shot,
            Step(action="extract", target="orders",
                 note="Read the order list shown on the page"),
            Step(action="finish", note="Save the order list as an artifact"),
        ]
    if kind == "bank_statement":
        return [
            open_step, shot,
            Step(action="download", target="statement",
                 note="Download the statement from the page"),
            Step(action="finish", note="Save the statement as an artifact"),
        ]
    if kind == "gov_form":
        return [
            open_step, shot,
            Step(action="fill", target="form",
                 note="Fill the form from the values the owner gave"),
            Step(action="screenshot", note="Capture the filled form for the owner"),
            Step(action="finish",
                 note="Hand the filled form back. The owner submits it."),
        ]
    return [
        open_step, shot,
        Step(action="extract", target="page text", note="Read the page"),
        Step(action="finish", note="Save what was found as an artifact"),
    ]


@dataclass
class Plan:
    kind: TaskKind
    goal: str
    url: str
    domain: str
    steps: list[Step] = field(default_factory=list)


def build(kind: str, goal: str, url: str, domain: str) -> Plan:
    k: TaskKind = kind if kind in PROFILES else "generic"  # type: ignore[assignment]
    return Plan(kind=k, goal=goal, url=url, domain=domain,
                steps=plan_for(k, goal, url, domain))

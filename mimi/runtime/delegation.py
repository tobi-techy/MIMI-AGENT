"""Job trees: a helper handing work to another helper (``ask_helper``).

A child job runs inside its root job's budget. ``spawn_child`` gives it what
the tree has left (the root's budget, owner grants included, minus what every
job in the tree has spent), never a fresh default, and the runner checks the
tree's spend before every step of every member. A child gets no grant of its
own: only the owner's grant on the root adds to the tree.

It refuses a cycle (a helper already in the chain, A to B to A) and a chain
deeper than ``max_depth`` handoffs. The harness refuses the same
``ask_helper`` calls at proposal time (the loop guards); this is the
second lock, where the child job would be created.
"""

from __future__ import annotations

from datetime import datetime

from mimi.runtime.budgets import Budget, Spend
from mimi.runtime.job import Job

MAX_DEPTH = 2


class DelegationRefused(ValueError):
    """The child would loop, go too deep or start with no budget left."""


def root_of(job: Job) -> str:
    return job.root_job_id or job.job_id


def chain_of(job: Job) -> list[str]:
    return list(job.chain) or [job.helper]


def tree_spend(store: object, root_job_id: str) -> Spend:
    total = Spend()
    tree = getattr(store, "tree", None)
    for j in (tree(root_job_id) if tree is not None else []):
        total.steps += j.spend.steps
        total.seconds += j.spend.seconds
        total.kobo += j.spend.kobo
    return total


def remaining(budget: Budget, spent: Spend) -> Budget:
    return Budget(max_steps=max(0, budget.max_steps - spent.steps),
                  max_seconds=max(0.0, budget.max_seconds - spent.seconds),
                  max_kobo=max(0, budget.max_kobo - spent.kobo))


def spawn_child(store: object, parent: Job, *, helper: str, goal: str, job_id: str,
                now: datetime, max_depth: int = MAX_DEPTH, thread_id: str = "") -> Job:
    """Create the child job for a handoff, or raise ``DelegationRefused``."""
    chain = chain_of(parent)
    if helper in chain:
        raise DelegationRefused(f"{helper} is already in this job's chain: {' > '.join(chain)}")
    if len(chain) > max_depth:
        raise DelegationRefused(f"job tree already {len(chain) - 1} handoffs deep")
    root_id = root_of(parent)
    root = parent if root_id == parent.job_id else store.get(root_id)  # type: ignore[attr-defined]
    left = remaining(root.budget, tree_spend(store, root_id))
    if left.max_steps <= 0 or left.max_kobo <= 0 or left.max_seconds <= 0:
        raise DelegationRefused("the job tree has no budget left")
    default = Budget()
    child = Job(job_id=job_id, tenant_id=parent.tenant_id, helper=helper,
                thread_id=thread_id or parent.thread_id, wake_source="helper_finished",
                goal=goal,
                budget=Budget(max_steps=min(default.max_steps, left.max_steps),
                              max_seconds=min(default.max_seconds, left.max_seconds),
                              max_kobo=min(default.max_kobo, left.max_kobo)),
                budget_grant=Budget(max_steps=0, max_seconds=0.0, max_kobo=0),
                parent_job_id=parent.job_id, root_job_id=root_id, chain=[*chain, helper])
    store.create(child)  # type: ignore[attr-defined]
    return child

"""Always-on runtime: durable helper jobs that run for hours and survive crashes.

Built during the Meta Global AI Developer Hackathon (window opened 26 Oct 2026,
17:00 WAT).

Mimi wakes, does one step, saves, and either keeps going, parks or sleeps.
State never lives in process memory:

* a job is a state machine (queued, working, waiting_on_owner, sleeping_until,
  blocked, done) persisted after every step;
* one worker holds a job at a time through a lease it keeps alive with
  heartbeats; a crashed worker's lease expires and another worker takes over
  from the last saved step;
* every outbound intent carries an idempotency key and goes through an outbox,
  so a resumed job never sends twice;
* per-job budgets (steps, seconds, naira) park the job and ask the owner;
* an owner message is taken at the next step boundary, ahead of everything else.

The guardrails do not move: Mimi never moves money, steps return typed intents
only (each helper registers its own with ``outbox.intent_types``), and every
money or stock figure in an intent is computed by code, never by the model.
"""

from mimi.runtime.budgets import Budget, BudgetHit, Spend
from mimi.runtime.job import Job, OwnerMessage, PendingOutbound
from mimi.runtime.runner import JobRunner, RunReport
from mimi.runtime.states import IllegalTransition, JobState
from mimi.runtime.steps import StepContext, StepOutcome
from mimi.runtime.store import InMemoryJobStore, JobStore, LeaseLost

__all__ = [
    "Budget",
    "BudgetHit",
    "IllegalTransition",
    "InMemoryJobStore",
    "Job",
    "JobRunner",
    "JobState",
    "JobStore",
    "LeaseLost",
    "OwnerMessage",
    "PendingOutbound",
    "RunReport",
    "Spend",
    "StepContext",
    "StepOutcome",
]

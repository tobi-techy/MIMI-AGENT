"""The job state machine and the transitions it allows.

A transition outside this table is a bug in the runner, not a judgement call,
so it raises instead of being coerced.
"""

from __future__ import annotations

from enum import StrEnum


class JobState(StrEnum):
    QUEUED = "queued"
    WORKING = "working"
    WAITING_ON_OWNER = "waiting_on_owner"
    SLEEPING_UNTIL = "sleeping_until"
    BLOCKED = "blocked"
    DONE = "done"


TERMINAL: frozenset[JobState] = frozenset({JobState.DONE})

# Parked states wait for something outside the worker: the owner, a timer, or
# a human unblocking the job.
PARKED: frozenset[JobState] = frozenset(
    {JobState.WAITING_ON_OWNER, JobState.SLEEPING_UNTIL, JobState.BLOCKED}
)

ALLOWED: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.WORKING, JobState.BLOCKED}),
    JobState.WORKING: frozenset(
        {
            # WORKING -> WORKING is a lease takeover after a crash.
            JobState.WORKING,
            JobState.WAITING_ON_OWNER,
            JobState.SLEEPING_UNTIL,
            JobState.BLOCKED,
            JobState.DONE,
            # Released without finishing (graceful shutdown).
            JobState.QUEUED,
        }
    ),
    JobState.WAITING_ON_OWNER: frozenset({JobState.QUEUED, JobState.BLOCKED}),
    JobState.SLEEPING_UNTIL: frozenset(
        {JobState.WORKING, JobState.QUEUED, JobState.BLOCKED}
    ),
    JobState.BLOCKED: frozenset({JobState.QUEUED}),
    JobState.DONE: frozenset(),
}


class IllegalTransition(RuntimeError):
    def __init__(self, src: JobState, dst: JobState):
        self.src = src
        self.dst = dst
        super().__init__(f"illegal job transition {src} -> {dst}")


def check_transition(src: JobState, dst: JobState) -> None:
    if dst not in ALLOWED[src]:
        raise IllegalTransition(src, dst)

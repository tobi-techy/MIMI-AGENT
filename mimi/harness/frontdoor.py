"""The front door: one entry point for every wake-up (RAI-280, always-on rules 1 to 6).

    door.submit(inbound) -> Admission       # dedupe, pick the thread, queue, lane
    door.pump()                             # start every turn that is due (one per thread)

In an always-on agent the router is the door every wake-up comes through,
not only a picker for messages. Without one door, duplicates and parallel
turns on one thread send double messages. Everything here is plain code; the
router's model call happens only after these steps, once per turn.

1. **One envelope** (``Inbound``) for every source.
2. **Drop duplicates** on ``(org_id, external_id)`` (a unique index in Go;
   ``DedupeIndex`` here). A replayed webhook never wakes the agent twice.
3. **Pick the conversation**: the shop's, the owner's or the job's thread;
   unclear events go to the owner thread with a note (``Directory``).
4. **One turn at a time per thread**: each thread has a queue and a running
   flag; different threads run in parallel on the worker pool.
5. **Two lanes**. Fast: the owner's 1:1 messages start at once, an owner
   message for a thread that is already running is handed to that turn as
   steering (RAI-283), and "stop" interrupts every running turn of that
   distributor at once. Batch: everything else waits for a short quiet window
   (1.5 s) or at most 6 s, and events that arrive while a turn runs are
   batched into the next single turn, so a burst of five shop messages makes
   one turn that sees all five.
6. The wake source and every ``external_id`` are stamped on the run row and
   handed to the context builder (RAI-281).

When several threads are due at once, owner turns start first, then shop and
rep turns, then webhooks and helper reports, then routines.
"""

from __future__ import annotations

import heapq
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Literal, Protocol

from mimi.harness.inbound import Directory, Inbound
from mimi.harness.router import PRIORITY, RouteDecision, Router, ThreadState


class DedupeIndex(Protocol):
    def claim(self, org_id: str, external_id: str) -> bool:
        """True the first time this id is seen for this distributor."""
        ...


class InMemoryDedupe:
    """The unique index Go keeps on (org_id, external_id)."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def claim(self, org_id: str, external_id: str) -> bool:
        with self._lock:
            key = (org_id, external_id)
            if key in self._seen:
                return False
            self._seen.add(key)
            return True


@dataclass(frozen=True)
class Admission:
    accepted: bool
    reason: str
    thread_id: str = ""
    lane: Literal["fast", "batch", ""] = ""


@dataclass
class Turn:
    """One turn on one thread: the batch it covers and the router's decision."""

    run_id: str
    org_id: str
    thread_id: str
    events: list[Inbound]
    lane: str
    # Set by the worker before the turn runs (routing happens off the lock).
    decision: RouteDecision | None = None
    # Set by the owner's "stop": the turn ends at its next step boundary.
    stop: threading.Event = field(default_factory=threading.Event)
    # Owner messages for this running turn (fast lane), taken at the next step.
    steering: list[Inbound] = field(default_factory=list)
    started_at: float = 0.0
    interrupted_at: float = 0.0
    state_at_start: ThreadState = field(default_factory=ThreadState)
    # The helper may leave the thread waiting on an answer (a stock tap, a YES).
    next_state: ThreadState | None = None

    @property
    def source(self) -> str:
        if self.decision is not None:
            return self.decision.source
        return min(self.events, key=lambda e: PRIORITY.get(e.source, 3)).source

    @property
    def external_ids(self) -> tuple[str, ...]:
        return tuple(e.external_id for e in self.events)


@dataclass(frozen=True)
class RunRow:
    """What the console's run list shows for every turn."""

    run_id: str
    org_id: str
    thread_id: str
    source: str
    external_ids: tuple[str, ...]
    lane: str
    helper: str
    skill: str
    layer: str
    status: str


@dataclass
class _Thread:
    pending: list[tuple[float, Inbound]] = field(default_factory=list)
    running: Turn | None = None
    state: ThreadState = field(default_factory=ThreadState)


TurnFn = Callable[[Turn], str]


class FrontDoor:
    def __init__(
        self,
        router: Router,
        directories: dict[str, Directory],
        run_turn: TurnFn,
        *,
        dedupe: DedupeIndex | None = None,
        quiet_s: float = 1.5,
        max_wait_s: float = 6.0,
        workers: int = 8,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.router = router
        self.directories = directories
        self.run_turn = run_turn
        self.dedupe = dedupe or InMemoryDedupe()
        self.quiet_s = quiet_s
        self.max_wait_s = max_wait_s
        self.clock = clock
        self._threads: dict[tuple[str, str], _Thread] = {}
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(workers, thread_name_prefix="mimi-turn")
        self._futures: list[Future[None]] = []
        self.rows: list[RunRow] = []
        self.dropped: list[tuple[str, str]] = []
        self.stops: list[float] = []  # seconds from "stop" arriving to interrupts set
        self._seq = 0

    # ------------------------------------------------------------ intake

    def submit(self, inbound: Inbound) -> Admission:
        d = self.directories.get(inbound.org_id)
        if d is None:
            return Admission(False, "unknown distributor")
        if not self.dedupe.claim(inbound.org_id, inbound.external_id):
            self.dropped.append((inbound.org_id, inbound.external_id))
            return Admission(False, "duplicate external_id")
        thread_id, _ = d.thread_for(inbound)
        now = self.clock()
        if inbound.source == "owner_message" and self._is_stop(inbound, d):
            self._stop(inbound, d, thread_id, now)
            return Admission(True, "stop: running turns interrupted", thread_id, "fast")
        with self._lock:
            th = self._threads.setdefault((inbound.org_id, thread_id), _Thread())
            if (inbound.source == "owner_message" and th.running is not None
                    and th.running.lane == "fast"):
                # Fast lane: the owner's next message reaches the running turn
                # at its next step instead of waiting for a new one.
                th.running.steering.append(inbound)
                return Admission(True, "steering the running turn", thread_id, "fast")
            th.pending.append((now, inbound))
        lane: Literal["fast", "batch"] = (
            "fast" if inbound.source == "owner_message" else "batch")
        return Admission(True, "queued", thread_id, lane)

    def set_thread_state(self, org_id: str, thread_id: str, state: ThreadState) -> None:
        """A helper parked on a question or the router asked one (the turn sets it)."""
        with self._lock:
            self._threads.setdefault((org_id, thread_id), _Thread()).state = state

    # ------------------------------------------------------------- pump

    def due(self, now: float | None = None) -> list[tuple[str, str]]:
        """Threads whose batch is ready and that are not running, priority first."""
        now = self.clock() if now is None else now
        heap: list[tuple[int, float, str, str]] = []
        with self._lock:
            for (org, tid), th in self._threads.items():
                if th.running is not None or not th.pending:
                    continue
                first, last = th.pending[0][0], th.pending[-1][0]
                lead = min(th.pending, key=lambda x: PRIORITY.get(x[1].source, 3))[1]
                fast = lead.source == "owner_message"
                ready = fast or now - last >= self.quiet_s or now - first >= self.max_wait_s
                if ready:
                    heapq.heappush(heap, (PRIORITY.get(lead.source, 3), first, org, tid))
        return [(org, tid) for _, _, org, tid in sorted(heap)]

    def pump(self, now: float | None = None, *, wait: bool = False) -> list[Turn]:
        """Start every due turn (on the pool). ``wait`` blocks until they finish."""
        started: list[Turn] = []
        for org, tid in self.due(now):
            turn = self._claim(org, tid)
            if turn is None:
                continue
            started.append(turn)
            self._futures.append(self._pool.submit(self._run, turn))
        if wait:
            self.drain()
        return started

    def drain(self) -> None:
        while True:
            with self._lock:
                fs = [f for f in self._futures if not f.done()]
            if not fs:
                return
            for f in fs:
                f.result()

    def close(self) -> None:
        self.drain()
        self._pool.shutdown(wait=True)

    def running(self, org_id: str) -> list[Turn]:
        with self._lock:
            return [th.running for (org, _), th in self._threads.items()
                    if org == org_id and th.running is not None]

    # --------------------------------------------------------- internals

    def _claim(self, org: str, tid: str) -> Turn | None:
        with self._lock:
            th = self._threads[(org, tid)]
            if th.running is not None or not th.pending:
                return None
            batch = [e for _, e in th.pending]
            th.pending = []
            self._seq += 1
            lead = min(batch, key=lambda e: PRIORITY.get(e.source, 3))
            turn = Turn(run_id=f"turn_{self._seq:06d}", org_id=org, thread_id=tid,
                        events=batch, lane="fast" if lead.source == "owner_message" else "batch",
                        started_at=self.clock())
            th.running = turn
            state = th.state
        turn.state_at_start = state
        return turn

    def _run(self, turn: Turn) -> None:
        status = "error"
        try:
            # The router runs here, off the door's lock: a model call on one
            # thread never holds up another thread's intake.
            turn.decision = self.router.route_batch(
                turn.events, self.directories[turn.org_id], turn.state_at_start)
            status = self.run_turn(turn)
        finally:
            with self._lock:
                th = self._threads[(turn.org_id, turn.thread_id)]
                th.running = None
                d = turn.decision
                if d is not None and d.status == "clarify":
                    th.state = ThreadState(clarify_options=d.options,
                                           clarify_text=d.clarify_question,
                                           last_skill=th.state.last_skill)
                elif d is not None and turn.next_state is not None:
                    th.state = turn.next_state
                elif d is not None:
                    th.state = ThreadState(last_skill=d.skill)
                self.rows.append(RunRow(
                    run_id=turn.run_id, org_id=turn.org_id, thread_id=turn.thread_id,
                    source=turn.source, external_ids=turn.external_ids, lane=turn.lane,
                    helper=d.helper if d else "", skill=d.skill if d else "",
                    layer=d.layer if d else "", status=status))

    @staticmethod
    def _is_stop(inbound: Inbound, d: Directory) -> bool:
        from mimi.harness.lexicon import fold
        from mimi.harness.router import _STOP

        return bool(_STOP.match(fold(inbound.text)))

    def _stop(self, inbound: Inbound, d: Directory, thread_id: str, now: float) -> None:
        with self._lock:
            for th in self._threads.values():
                t = th.running
                if t is not None and t.org_id == inbound.org_id:
                    t.stop.set()
                    t.interrupted_at = self.clock()
            self.stops.append(self.clock() - now)
            decision = self.router.route(inbound, d)
            self._seq += 1
            self.rows.append(RunRow(
                run_id=f"turn_{self._seq:06d}", org_id=inbound.org_id, thread_id=thread_id,
                source=inbound.source, external_ids=(inbound.external_id,), lane="fast",
                helper=decision.helper, skill=decision.skill, layer=decision.layer,
                status="stopped_running_turns"))

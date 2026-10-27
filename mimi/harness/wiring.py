"""Door to loop: a routed turn becomes a harness run with its built context.

    runner = TurnRunner(harness, builder, helpers, events)
    door = FrontDoor(router, directories, runner)

For each turn the front door starts, this:

1. writes the router's system events (``handed_to`` and friends) to the
   thread's event log, so the console shows "Handed to Collector" centred
   and grey;
2. sends the clarifying question when the router asked one (the turn ends
   there; the thread is parked until the answer);
3. builds the minimum context for the routed skill (RAI-281), stamping the
   wake source and every external id of the batch on the run;
4. runs the helper's loop (RAI-278) and records the turn's messages in the
   thread history, append only;
5. leaves the thread waiting on an answer when the helper asked a question.

Helpers that are not hired yet (only Collector and Proof run on the harness
today; the Day 2 to 6 handlers move behind the router in RAI-272) get the
routing and the system event, and the turn reports ``no_helper_yet``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mimi.harness.context import ContextBuilder
from mimi.harness.frontdoor import Turn
from mimi.harness.inbound import Directory
from mimi.harness.loop import Harness, Helper, LoopState
from mimi.harness.router import ThreadState
from mimi.harness.types import RunEvent, RunResult


@dataclass
class TurnRunner:
    harness: Harness
    builder: ContextBuilder
    helpers: Mapping[str, Helper]
    directories: Mapping[str, Directory] = field(default_factory=dict)
    # Every system event and clarifying question, in order (the console feed).
    feed: list[dict[str, Any]] = field(default_factory=list)
    results: list[RunResult] = field(default_factory=list)

    def __call__(self, turn: Turn) -> str:
        d = turn.decision
        assert d is not None
        for ev in d.events:
            self.feed.append({"thread_id": turn.thread_id, "kind": ev.kind, "text": ev.text,
                              "style": ev.style, "run_id": turn.run_id})
        if d.status == "clarify":
            self.feed.append({"thread_id": turn.thread_id, "kind": "question",
                              "text": d.clarify_question, "options": list(d.options),
                              "run_id": turn.run_id})
            return "asked_to_clarify"
        helper = self.helpers.get(d.helper)
        if helper is None:
            return f"no_helper_yet:{d.helper}"
        lead = turn.events[0]
        shop_id = ""
        if lead.source in ("shop_message", "rep_message"):
            dirs = self.directories.get(turn.org_id)
            shop_id = dirs.shop_for(lead.payload.from_phone) if dirs else ""
        shop_id = shop_id or lead.payload.shop_id
        text = "\n".join(e.text for e in turn.events if e.text)
        sender = {"owner_message": "owner", "shop_message": "shop",
                  "rep_message": "rep"}.get(lead.source, "system")
        if sender == "owner":
            dirs = self.directories.get(turn.org_id)
            known = dirs.is_owner(lead) if dirs else not lead.payload.from_phone
            if not known:
                # A stranger on the owner channel: its words are untrusted
                # shop text in the prompt, never an <owner_message>.
                sender = "shop"
        event = RunEvent(
            event_id=f"{turn.org_id}:{'+'.join(turn.external_ids)}",
            tenant_id=turn.org_id, thread_id=turn.thread_id,
            wake_source=lead.source if lead.source != "scheduled_check_in"
            else "scheduled_check_in",  # type: ignore[arg-type]
            text=text, sender=sender,  # type: ignore[arg-type]
            shop_id=shop_id, at=lead.received_at, job_id=turn.run_id)
        job = lead.payload.name if lead.source in ("routine", "scheduled_check_in") else ""
        ctx = self.builder.build(event, helper, d.skill, job=job)
        route = {"helper": d.helper, "skill": d.skill, "confidence": d.confidence,
                 "layer": d.layer, "rule": d.rule, "language": d.language,
                 "source": d.source, "external_ids": list(turn.external_ids),
                 "lane": d.lane, "secondary": list(d.secondary),
                 "injection": list(d.injection)}
        state = self.harness.start(event, helper, context=ctx, route=route)
        while state.finished is None:
            if turn.stop.is_set():
                # The owner said stop: end at this step boundary; the saved
                # state keeps everything proposed so far.
                return "stopped_by_owner"
            # pop one at a time: the door may append while we read, and a
            # copy-then-clear would drop a message landing in between.
            owner: list[str] = []
            while turn.steering:
                owner.append(turn.steering.pop(0).text)
            state = LoopState.model_validate(
                self.harness.step(state, helper, owner_messages=owner).state)
        result = state.finished
        self.results.append(result)
        self.builder.record_turn(event, state.messages, state.prefix_len, state.history_len)
        if result.status == "asked_question" and sender == "shop":
            # The helper asked: the answer most likely belongs to it. No rule
            # (the answer type is open); the router sees it as a hint.
            turn.next_state = ThreadState(pending_skill=d.skill, last_skill=d.skill)
        return result.status

"""When-to-speak wiring (RAI-300): the digest store the API passes is the one
used, ``off`` skips the decision, and ``shadow`` never queues a sent draft
into the owner's daily note."""

from __future__ import annotations

from typing import Any

from mimi.harness import speak as S
from mimi.harness.collector import collector_helper, default_registry
from mimi.harness.gateway import Gateway, ScriptedModel, say
from mimi.harness.go import FakeGo
from mimi.harness.loop import Harness
from mimi.harness.policy import GoPolicy
from mimi.harness.speak_eval import AT, TENANT, WORLD
from mimi.harness.types import RunEvent, RunResult

CLEAN = "14 payments matched clean, nothing flagged."


class _Counts:
    """A durable-store stand-in that is a SpeakCounts but not the in-memory one."""

    def __init__(self) -> None:
        self.n = 0

    def unprompted_today(self, tenant_id: str, audience: Any, shop_id: str, day: Any) -> int:
        return 0

    def record_unprompted(self, tenant_id: str, audience: Any, shop_id: str, day: Any) -> None:
        self.n += 1


def _harness(mode: str, counts: Any, digest: Any) -> Harness:
    go = FakeGo(WORLD)
    model = ScriptedModel([
        say("", ("get_invoices", {"shop_id": "shop-005"})),
        say("", ("send_to_user", {"channel": "owner_console", "body": CLEAN,
                                  "ends_turn": True})),
        say("held; nothing further to add"),
        say("held; nothing further to add"),
    ])
    return Harness(book=go, control=go, events=go, gateway=Gateway([model]),
                   policy=GoPolicy(), clock=lambda: AT, speak=S.SpeakConfig(mode=mode),
                   speak_counts=counts, speak_digest=digest)


def _run(h: Harness, cid: str) -> RunResult:
    ev = RunEvent(event_id=cid, tenant_id=TENANT, thread_id=f"t:{cid}",
                  wake_source="routine", text="", sender="system", at=AT)
    return h.run(ev, collector_helper(default_registry(), GoPolicy()))


def test_the_passed_digest_is_used_with_a_durable_counts_store() -> None:
    digest = S.InMemorySpeakStore()
    h = _harness("enforce", _Counts(), digest)
    assert h.speak_digest is digest
    r = _run(h, "wire-1")
    assert not any(d.channel == "owner_console" for d in r.outbound_drafts)
    assert [i["body"] for i in digest.items] == [CLEAN]


def test_in_memory_counts_double_as_the_digest_when_none_is_passed() -> None:
    store = S.InMemorySpeakStore()
    h = _harness("enforce", store, None)
    assert h.speak_digest is store


def test_shadow_sends_and_never_queues_the_sent_draft() -> None:
    digest = S.InMemorySpeakStore()
    r = _run(_harness("shadow", _Counts(), digest), "shadow-1")
    assert any(d.channel == "owner_console" for d in r.outbound_drafts)
    assert digest.items == []
    assert any(e.type == "speak_decision" for e in r.events)


def test_off_skips_the_decision() -> None:
    digest, counts = S.InMemorySpeakStore(), _Counts()
    r = _run(_harness("off", counts, digest), "off-1")
    assert any(d.channel == "owner_console" for d in r.outbound_drafts)
    assert digest.items == [] and counts.n == 0
    assert not any(e.type == "speak_decision" for e in r.events)


def test_shadow_with_one_in_memory_store_queues_nothing() -> None:
    store = S.InMemorySpeakStore()
    r = _run(_harness("shadow", store, store), "shadow-2")
    assert any(d.channel == "owner_console" for d in r.outbound_drafts)
    assert store.items == []

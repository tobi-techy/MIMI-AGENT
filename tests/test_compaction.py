"""Thread compaction (RAI-285): schema, validator, store, builder, runner.

Runs offline: the compaction draft comes from a scripted FakeModel, the book
is the synthetic world, and the store is a SQLite file per test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from mimi.api import compaction as cm
from mimi.harness import compaction as cp
from mimi.harness.collector import collector_helper, default_registry
from mimi.harness.context import ContextBuilder, HistoryStore, PrefixStore
from mimi.harness.go import FakeGo
from mimi.harness.policy import GoPolicy
from mimi.harness.types import RunEvent
from mimi.models.base import ChatMessage, ChatResult
from mimi.models.fake import FakeModel
from mimi.store import ensure_schema, make_engine, seed_world
from mimi.store import schema as s
from mimi.store.util import dumps
from mimi.world.loader import load_world

W = load_world()
TENANT = W.tenant_id
AT = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)
OPEN = next(r for r in W.receivables if r.shop_id == "shop-001" and r.outstanding_kobo > 0)


def ev(text: str = "how much I owe", i: int = 0) -> RunEvent:
    return RunEvent(event_id=f"e{i}", tenant_id=TENANT, thread_id="t1",
                    wake_source="shop_message", text=text, sender="shop",
                    shop_id="shop-001", at=AT + timedelta(hours=i))


def drafter(messages: Any, meta: Any) -> str:
    """A deterministic draft: promises and decisions quoted, record ids named."""
    return json.dumps({
        "open_items": [f"balance question open for shop-001 (ref receivables {OPEN.delivery_id})"],
        "promises": [{"text": f"pay on Friday for {OPEN.delivery_id}", "day": "2026-10-30",
                      "kept": False}],
        "decisions": [{"text": "owner chose soft tone", "made_by": "owner",
                       "day": "2026-10-28"}],
        "tone": "soft",
        "open_questions": ["is the Friday promise kept"],
    })


def turn(text: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": text},
            {"role": "assistant", "content": "noted"}]


# ------------------------------------------------------------------ schema


def test_render_carries_refs_never_figures() -> None:
    sm = cp.ThreadSummary(
        open_items=[f"open balance (ref receivables {OPEN.delivery_id})"],
        promises=[cp.PromiseNote(text="pay Friday", day="2026-10-30")],
        decisions=[cp.DecisionNote(text="soft tone", made_by="owner", day="2026-10-28")],
        refs=[cp.AmountRef(record="receivables", record_id=OPEN.delivery_id,
                           shop_id="shop-001", why="named")])
    assert cp.free_text_figures(sm.render()) == []
    assert OPEN.delivery_id in sm.render()


def test_free_text_figures_catches_naira_but_not_refs() -> None:
    assert cp.free_text_figures("owes N20,000 confirmed") != []
    assert cp.free_text_figures("owes 50000 kobo total") != []
    assert cp.free_text_figures(f"open (ref receivables {OPEN.delivery_id})") == []


def test_trigger_and_env_flags() -> None:
    assert cp.should_compact(prompt_tokens=100_000, turns=3, threshold_tokens=100_000)
    assert cp.should_compact(prompt_tokens=10, turns=30, threshold_tokens=100_000)
    assert not cp.should_compact(prompt_tokens=10, turns=3, threshold_tokens=100_000)
    assert not cp.compaction_on({})
    assert cp.compaction_on({"MIMI_COMPACTION": "1"})
    assert cp.token_threshold({}) == 100_000
    assert cp.token_threshold({"MIMI_COMPACT_TOKENS": "6000"}) == 6_000
    assert cp.keep_turns({}) == 6
    assert cp.delay_s({}) == 600
    first, _ = cp.compact_window(turns=10, keep=6)
    assert first == 4


def test_parse_summary_reads_json_and_falls_back_to_prose() -> None:
    sm = cp.parse_summary(drafter(None, None), turn_ids=["a", "b"])
    assert sm.covered_turns == ["a", "b"] and len(sm.promises) == 1
    prose = cp.parse_summary("just some words", turn_ids=["a"])
    assert prose.open_items == ["just some words"]


# --------------------------------------------------------------- validator


def window() -> list[list[dict[str, Any]]]:
    return [turn(f"promise recorded: shop-001 pay on Friday for {OPEN.delivery_id}"),
            turn("decided: owner chose soft tone on 2026-10-28")]


def test_validator_keeps_every_promise_and_decision() -> None:
    go = FakeGo(W)
    draft = cp.attach_refs(cp.parse_summary(drafter(None, None), turn_ids=["t1", "t2"]),
                           go, TENANT)
    assert cp.validate(draft, turns=window()) == []
    assert draft.refs and all(r.record_id for r in draft.refs)


def test_validator_flags_a_lost_promise_and_a_free_text_figure() -> None:
    bad = cp.ThreadSummary(open_items=["nothing here"], promises=[],
                           decisions=[cp.DecisionNote(text="owner chose soft tone",
                                                      made_by="owner", day="2026-10-28")])
    problems = cp.validate(bad, turns=window())
    assert any("promise lost" in p for p in problems)
    rich = cp.ThreadSummary(open_items=["owes N20,000"],
                            promises=[cp.PromiseNote(text="pay on Friday", day="2026-10-30")],
                            decisions=[cp.DecisionNote(text="owner chose soft tone",
                                                       made_by="owner", day="2026-10-28")])
    assert any("free-text" in p for p in cp.validate(rich, turns=[]))
    old = bad.model_copy(update={"generation": 3})
    assert any("drift" in p for p in cp.validate(old, turns=[]))


# -------------------------------------------------------- builder and store


def test_compacted_thread_shows_summary_and_bumps_the_epoch() -> None:
    prefixes, history = PrefixStore(), HistoryStore()
    go = FakeGo(W)
    helper = collector_helper(default_registry(), GoPolicy())
    b = ContextBuilder(book=go, prefixes=prefixes, history=history)
    a = b.build(ev(), helper, "collector/collections")
    assert a.prefix.epoch == 0
    for i in range(1, 4):
        b.build(ev(f"q{i}", i=i), helper, "collector/collections")
        history.append(TENANT, "t1", turn(f"q{i}"))
    v = history.save_compaction(TENANT, "t1", "Older turns: promise kept.", summary_hash="abc")
    assert v == 1 and history.compaction_count(TENANT, "t1") == 1
    prefixes.compact(TENANT, "t1", "collector")
    c = b.build(ev("q4", i=9), helper, "collector/collections")
    assert c.prefix.epoch == 1 and c.manifest.summary_version == 1
    assert c.manifest.summary_hash == "abc"
    assert "<thread_summary>" in c.messages[2]["content"]


def test_versions_grow_and_raw_turns_stay(store_url: str) -> None:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    from mimi.store.context_stores import SqlHistoryStore

    h = SqlHistoryStore(engine)
    assert h.save_compaction(TENANT, "t1", "first") == 1
    assert h.save_compaction(TENANT, "t1", "second") == 2
    assert h.summary(TENANT, "t1") == "second"
    assert h.summary_version(TENANT, "t1") == (2, "")
    assert h.compaction_count(TENANT, "t1") == 2
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(
            s.compactions)).scalar() == 2


# ------------------------------------------------------------------ runner


def seed_thread(engine: Any, n: int = 10) -> None:
    with engine.begin() as conn:
        for i in range(n):
            conn.execute(s.history.insert().values(
                tenant_id=TENANT, thread_id="t1", turn_id=f"turn-{i:02d}", ts=float(i),
                body=dumps(turn(f"promise recorded: shop-001 pay Friday {OPEN.delivery_id} "
                                f"(message {i})")) if i < n - 2 else dumps(turn(f"newest {i}"))))


def test_off_by_default_and_timer_then_compact(store_url: str) -> None:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    seed_thread(engine)
    go = FakeGo(W)
    assert not cm.schedule_if_big(engine, tenant=TENANT, thread="t1", helper="collector",
                                  skill="collector/collections", prompt_tokens=200_000,
                                  turns=10, env={})
    assert cm.schedule_if_big(engine, tenant=TENANT, thread="t1", helper="collector",
                              skill="collector/collections", prompt_tokens=200_000,
                              turns=10, env={"MIMI_COMPACTION": "1",
                                             "MIMI_COMPACT_DELAY_S": "0"})
    model = FakeModel(drafter, name="fake:compaction")
    res = cm.compact_thread(engine, tenant=TENANT, thread="t1", book=go, model=model,
                            shop_id="shop-001",
                            env={"MIMI_COMPACTION": "1", "MIMI_COMPACT_KEEP": "2"})
    assert res["status"] == "done" and res["version"] == 1
    assert res["tokens_after"] < res["tokens_before"]
    assert res["kept"] == 2 and res["covered"] == 8
    with engine.connect() as conn:
        kept_rows = conn.execute(sa.select(sa.func.count()).select_from(
            s.history).where(s.history.c.tenant_id == TENANT,
                             s.history.c.thread_id == "t1")).scalar()
        window_from = conn.execute(sa.select(s.prefixes.c.window_from).where(
            s.prefixes.c.tenant_id == TENANT, s.prefixes.c.thread_id == "t1",
            s.prefixes.c.helper == "collector")).scalar()
        kinds = conn.execute(sa.select(s.thread_events.c.type).where(
            s.thread_events.c.tenant_id == TENANT,
            s.thread_events.c.thread_id == "t1")).scalars().all()
    assert kept_rows == 10  # raw history kept
    assert window_from == 8  # window jumped in one step
    assert "compacted" in kinds


def test_no_compaction_when_a_turn_is_queued(store_url: str) -> None:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    seed_thread(engine)
    with engine.begin() as conn:
        conn.execute(s.turns.insert().values(
            turn_id="turn_q", tenant_id=TENANT, thread_id="t1", state="queued",
            batch="[]", checkpoint="{}", created_at="2026-10-28T08:00:00+00:00",
            created_ts=1.0))
    assert cm.thread_busy(engine, TENANT, "t1")
    res = cm.compact_thread(engine, tenant=TENANT, thread="t1", book=FakeGo(W),
                            model=FakeModel(drafter), shop_id="shop-001")
    assert res["status"] == "skipped_busy"


def test_refused_draft_changes_nothing(store_url: str) -> None:
    engine = make_engine(store_url)
    ensure_schema(engine)
    seed_world(engine, W)
    seed_thread(engine)
    model = FakeModel(lambda m, meta: json.dumps({"open_items": ["nothing here"]}))
    res = cm.compact_thread(engine, tenant=TENANT, thread="t1", book=FakeGo(W),
                            model=model, shop_id="shop-001",
                            env={"MIMI_COMPACT_KEEP": "2"})
    assert res["status"] == "refused" and res["problems"]
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(
            s.compactions).where(s.compactions.c.version > 0)).scalar() == 0


def test_compact_prompt_starts_like_the_thread() -> None:
    msgs: list[ChatMessage] = cp.compact_prompt(helper_name="collector",
                                               skill="collector/collections",
                                               shop_name="Mama", turns_text="t")
    assert msgs[0].role == "system" and "# Helper: collector" in msgs[0].content
    res = ChatResult(text=drafter(None, None), model="fake")
    assert cp.compact(FakeModel(lambda m, meta: res.text), helper_name="collector",
                      skill="collector/collections", shop_name="Mama",
                      turns=window(), turn_ids=["a"]).promises


def test_tracing_carries_summary_version_and_compaction_span() -> None:
    from mimi.harness.tracing import build_turn, run_observations
    from mimi.harness.types import Event, RunResult, Spend

    at = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)
    r = RunResult(job_id="j", thread_id="t1", helper="collector", status="done",
                  reason="ok", wake_source="shop_message", run_id="r", model_calls=1,
                  spend=Spend(), toolset_hash="h", registry_version="v",
                  context={"context_manifest": [], "context_tokens": 5, "context_budget": 8,
                           "context_omitted": [], "context_trimmed": [], "context_notes": [],
                           "section_hashes": {}, "prefix_hash": "p", "prefix_reused": True,
                           "builder_version": "b", "summary_version": 2,
                           "summary_hash": "abc", "compact_tokens_before": 100,
                           "compact_tokens_after": 30})
    t = build_turn(r)
    assert t.metadata["summary_version"] == 2
    assert ("compact_token_drop", 70.0) in [(s.name, s.value) for s in t.scores]
    evs = [Event(event_id="e0", run_id="r", thread_id="t1", job_id="j", seq=0,
                 type="step_started", at=at, step=0),
           Event(event_id="e1", run_id="r", thread_id="t1", job_id="j", seq=1,
                 type="compacted", at=at, step=0, short="compacted",
                 data={"version": 1, "summary_hash": "abc", "tokens_before": 100,
                       "tokens_after": 30})]
    obs = run_observations(evs)
    assert obs[0].children[-1].name == "compaction"

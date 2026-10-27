"""The three evals as Langfuse Datasets and Experiments (optional).

    python -m mimi.observe.experiments upload  proof|voice|receipts|all
    python -m mimi.observe.experiments run     proof|voice|receipts [--judge] [--run-name N]

``upload`` creates (or updates, by stable item id) the datasets
``mimi/proof-screenshots`` (40 synthetic screenshots: the image as
``LangfuseMedia``, its SHA-256 and the expected fields and verdict),
``mimi/voice-notes`` (50 synthetic clips: the audio as ``LangfuseMedia``, the
reference transcript and expected verdict) and ``mimi/receipt-replies``
(typed replies and expected outcomes). Every item is synthetic; phone numbers
are left out.

``run`` runs the eval as a Langfuse Experiment on that dataset with the SDK's
``run_experiment``. The task is the same production code the offline eval
runs, against the **recorded** model answers (replay caches: no new Muse or
Spitch spend; a cache miss fails the item). Each item's trace carries the
turn's observations (generations, code checks as ``tool``) and these scores:

* code evaluators: ``verdict_correct``, ``fake_caught`` and
  ``false_likely_genuine`` (proof fakes), ``false_confirm`` (receipts and
  voice), ``field_accuracy`` (proof), ``tokens`` and ``latency_s``;
* run evaluators: the aggregate rates for the run;
* with ``--judge``: ``reply_respectful`` / ``reply_figures_grounded`` /
  ``reply_plain_english`` from an LLM judge on the shop-facing reply (Muse
  Spark through a cache: each distinct reply is judged once, then replayed).

The offline replay evals (``make eval``, ``make proof-eval``, ...) stay the CI
gate; nothing here runs in CI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from mimi.observe.langfuse_export import LangfuseExporter, LangfuseSettings, make_client
from mimi.observe.turns import (
    Observation,
    RecordingExporter,
    TracedModel,
    Turn,
    TurnBook,
    audio_cost,
    token_cost,
)

ROOT = Path(__file__).resolve().parents[2]
DATASETS = {
    "proof": "mimi/proof-screenshots",
    "voice": "mimi/voice-notes",
    "receipts": "mimi/receipt-replies",
}
JUDGE_CACHE = ROOT / "eval" / "judge" / "replies.cache.json"


# ------------------------------------------------------------------ datasets


def _proof_items() -> list[dict[str, Any]]:
    from langfuse.media import LangfuseMedia

    from mimi.proof.eval import EVAL_DIR, load_cases

    out = []
    for c in load_cases():
        data = (EVAL_DIR / c["file"]).read_bytes()
        out.append({
            "id": f"proof-{c['id']}",
            "input": {
                "case_id": c["id"],
                "image": LangfuseMedia(content_bytes=data, content_type="image/jpeg"),  # type: ignore[arg-type]
                "image_sha256": c["sha256"],
                "shop_id": c["shop_id"],
                "received_at": c["received_at"],
            },
            "expected_output": {
                "verdict": c["expected"],
                "fields": c.get("expected_fields", {}),
                "match": c.get("expected_match", {}),
                "fake": c["category"] == "fake",
                "fake_kind": c.get("fake_kind", ""),
                "rules": c.get("expected_rules", []),
            },
            "metadata": {"category": c["category"], "note": c.get("note", ""),
                         "file": c["file"], "synthetic": True,
                         "open_invoices": len(c.get("invoices", []))},
        })
    return out


def _voice_items() -> list[dict[str, Any]]:
    from langfuse.media import LangfuseMedia

    from mimi.voice.fixtures import clip_bytes, load_clips

    out = []
    for c in load_clips():
        out.append({
            "id": f"voice-{c['id']}",
            "input": {
                "clip_id": c["id"],
                "audio": LangfuseMedia(content_bytes=clip_bytes(c), content_type="audio/ogg"),  # type: ignore[arg-type]
                "language_hint": c["hint"],
                "delivery_id": c["delivery_id"],
            },
            "expected_output": {
                "verdict": c["expected"],
                "also_ok": c.get("also_ok", []),
                "reference_transcript": c["reference"],
                "promise": c.get("promise", {"status": "none"}),
            },
            "metadata": {"lang": c["lang"], "category": c["category"],
                         "noise": c.get("noise", ""), "synthetic": True,
                         "origin": c.get("origin", "")},
        })
    return out


def _receipt_items() -> list[dict[str, Any]]:
    from mimi.receipts.eval import load_cases

    return [
        {
            "id": f"receipt-{c['id']}",
            "input": {"case_id": c["id"], "delivery_id": c["delivery_id"],
                      "earlier": c["earlier"], "new": c["new"],
                      "clarified": c.get("clarified", False)},
            "expected_output": {"verdict": c["expected"], "also_ok": c.get("also_ok", []),
                                "lines": c.get("expect_lines", [])},
            "metadata": {"lang": c["lang"], "category": c["category"],
                         "note": c.get("note", ""), "synthetic": True},
        }
        for c in load_cases()
    ]


ITEMS: dict[str, Callable[[], list[dict[str, Any]]]] = {
    "proof": _proof_items, "voice": _voice_items, "receipts": _receipt_items,
}
DESCRIPTIONS = {
    "proof": "40 synthetic payment screenshots for fictional banks (genuine, fake, "
             "unreadable, resends) with expected fields and verdicts. Offline twin: "
             "eval/proof/screens.jsonl.",
    "voice": "50 synthetic voice notes (Spitch TTS voices, no real person) in English, "
             "Pidgin, Yoruba, Igbo and Hausa with reference transcripts and expected "
             "verdicts. Offline twin: eval/voice/clips.jsonl.",
    "receipts": "Typed shop replies to a delivery receipt with expected outcomes. "
                "Offline twin: eval/receipts/receipt_replies.jsonl.",
}


def upload(client: Any, which: str) -> int:
    name = DATASETS[which]
    client.create_dataset(name=name, description=DESCRIPTIONS[which],
                          metadata={"synthetic": True, "source": "mimi-agent repo"})
    items = ITEMS[which]()
    for it in items:
        for attempt in range(3):  # items upsert by id, so a retry is safe
            try:
                client.create_dataset_item(dataset_name=name, id=it["id"], input=it["input"],
                                           expected_output=it["expected_output"],
                                           metadata=it["metadata"])
                break
            except Exception as exc:
                if attempt == 2:
                    raise
                print(f"retry {it['id']}: {type(exc).__name__}", file=sys.stderr)
                time.sleep(2 * (attempt + 1))
    client.flush()
    return len(items)


# ------------------------------------------------------------------- tasks


class _Inline(LangfuseExporter):
    """Emit a finished turn's observations under the experiment's current span."""

    def __init__(self, settings: LangfuseSettings, client: Any) -> None:
        super().__init__(settings, client_factory=lambda s: client)
        self._client = client

    def emit(self, turn: Turn) -> None:
        for obs in turn.observations:
            self._emit(self._client, turn, obs)


def _model_latency_s(turns: Sequence[Turn], fallback: float) -> float:
    """Recorded model/transcription latency (replayed calls keep theirs)."""
    ns = sum(o.duration_ns for t in turns for o in t.observations if o.kind == "generation")
    return round(ns / 1e9, 3) if ns else round(fallback, 3)


def _turn_book() -> tuple[TurnBook, RecordingExporter]:
    rec = RecordingExporter()
    return TurnBook(rec, synthetic=True), rec


def _proof_task(model: Any, inline: _Inline) -> Callable[..., Any]:
    from mimi.proof import eval as pe

    cases = {c["id"]: c for c in pe.load_cases()}

    def task(*, item: Any, **_: Any) -> dict[str, Any]:
        case = cases[item.input["case_id"]]
        book, rec = _turn_book()
        t0 = time.monotonic()
        r = pe.run_case(case, model, turns=book)
        for turn in rec.turns:
            inline.emit(turn)
        texts = [p.get("text") for k, p in r["intents"] if p.get("text")]
        return {
            "verdict": r["verdict"],
            "failed_rules": r["failed_rules"],
            "fields": pe._got_fields(r["reading"]),
            "match": r["match"],
            "reply_to_shop": texts[0] if texts else "",
            "tokens": r["prompt_tokens"] + r["completion_tokens"],
            "latency_s": round(r["latency_s"] or (time.monotonic() - t0), 3),
            "cost_usd": token_cost(model.name, r["prompt_tokens"], r["completion_tokens"]),
        }

    return task


def _receipt_task(model: Any, inline: _Inline) -> Callable[..., Any]:
    from mimi.receipts import eval as re_
    from mimi.receipts.verdict import ReceiptJudge
    from mimi.world import load_world

    world = load_world()
    cases = {c["id"]: c for c in re_.load_cases()}
    judge = ReceiptJudge(TracedModel(model))

    def task(*, item: Any, **_: Any) -> dict[str, Any]:
        case = cases[item.input["case_id"]]
        book, rec = _turn_book()
        job = f"rcpt-{case['delivery_id']}"
        t0 = time.monotonic()
        with book.prelude(job, flow="receipt_reply", shop_id="eval-shop",
                          thread_id="wa-eval-shop") as turn:
            r = re_.run_case(judge, world, case)
        latency = time.monotonic() - t0
        if turn is not None:
            turn.add(Observation("tool", "check-reply-guards", time.time_ns(),
                                 input={"rules": ["evidence_quote_in_reply", "confidence_floor"]},
                                 output={"action": r.action, "evidence_found": r.evidence_found,
                                         "held_back": r.held_back}))
            book.close(job, r.outcome)
        for t in rec.turns:
            inline.emit(t)
        return {"verdict": r.outcome, "action": r.action, "language": r.language,
                "confidence": r.confidence, "tokens": r.prompt_tokens + r.completion_tokens,
                "latency_s": _model_latency_s(rec.turns, latency)}

    return task


def _voice_task(model: Any, inline: _Inline) -> Callable[..., Any]:
    from mimi.receipts.verdict import ReceiptJudge
    from mimi.voice import eval as ve
    from mimi.voice.cache import CachedTranscriber
    from mimi.voice.fixtures import load_clips
    from mimi.voice.router import DEFAULT_POLICY, VoiceRouter
    from mimi.world import load_world

    world = load_world()
    clips = {c["id"]: c for c in load_clips()}
    providers = {n: CachedTranscriber(None, ve.transcript_cache(n), mode="replay", name=n)
                 for n in ve.PROVIDERS}
    router = VoiceRouter(providers, DEFAULT_POLICY)
    judge = ReceiptJudge(TracedModel(model))

    def task(*, item: Any, **_: Any) -> dict[str, Any]:
        clip = clips[item.input["clip_id"]]
        book, rec = _turn_book()
        job = f"rcpt-{clip['delivery_id']}"
        t0 = time.monotonic()
        with book.prelude(job, flow="voice_note", shop_id="eval-shop",
                          thread_id="wa-eval-shop") as turn:
            r = ve.run_clip("router", router, judge, world, clip)
        latency = time.monotonic() - t0
        t = r.transcript
        if turn is not None:
            turn.observations.insert(0, Observation(
                "generation", "transcribe-voice-note", time.time_ns(),
                int(t.latency_s * 1e9), model={"spitch": "spitch-stt",
                                               "muse": "muse-voice-transcribe"}.get(
                                                   t.provider.split(":")[0], t.provider),
                usage={"audio_ms": int((t.duration_s or 0) * 1000)},
                cost_usd=audio_cost(t.provider, t.duration_s),
                input={"clip_id": clip["id"], "language_hint": clip["hint"]},
                output={"text": t.text, "status": t.status}))
            book.close(job, r.outcome)
        for tt in rec.turns:
            inline.emit(tt)
        return {"verdict": r.outcome, "action": r.action, "transcript": t.text,
                "transcriber": t.provider, "wer": round(r.wer, 4),
                "tokens": r.prompt_tokens + r.completion_tokens,
                "latency_s": _model_latency_s(rec.turns, latency)}

    return task


# -------------------------------------------------------------- evaluators


def _ev(name: str, value: Any, comment: str = "", data_type: str | None = None) -> Any:
    from langfuse import Evaluation

    return Evaluation(name=name, value=value, comment=comment or None,
                      data_type=data_type)  # type: ignore[arg-type]


def verdict_correct(*, output: Any, expected_output: Any, **_: Any) -> Any:
    ok_set = {expected_output["verdict"], *expected_output.get("also_ok", [])}
    got = output.get("verdict")
    return _ev("verdict_correct", got in ok_set, f"want {sorted(ok_set)}, got {got}", "BOOLEAN")


def proof_safety(*, output: Any, expected_output: Any, **_: Any) -> list[Any]:
    if not expected_output.get("fake"):
        return []
    got = output.get("verdict")
    return [
        _ev("fake_caught", got == "looks_fake", f"{expected_output.get('fake_kind')}: {got}",
            "BOOLEAN"),
        _ev("false_likely_genuine", got == "likely_genuine",
            "critical: a fake ended likely_genuine" if got == "likely_genuine" else "", "BOOLEAN"),
    ]


def field_accuracy(*, output: Any, expected_output: Any, **_: Any) -> list[Any]:
    want = expected_output.get("fields") or {}
    if not want or expected_output.get("verdict") not in ("likely_genuine", "looks_fake"):
        return []
    got = output.get("fields") or {}
    pairs = [(k, w) for k, w in (("amount", "amount_kobo"), ("time", "paid_at"),
                                 ("sender", "sender")) if w in want]
    if not pairs:
        return []
    hits = [got.get(k) == want[w] for k, w in pairs]
    return [_ev("field_accuracy", round(sum(hits) / len(hits), 4),
                ", ".join(f"{k}:{'ok' if h else 'miss'}" for (k, _), h in zip(pairs, hits,
                                                                              strict=True)))]


def false_confirm(*, output: Any, expected_output: Any, **_: Any) -> Any:
    ok_set = {expected_output["verdict"], *expected_output.get("also_ok", [])}
    bad = output.get("action") == "consent" and "confirmed" not in ok_set
    return _ev("false_confirm", bad, "critical: consent recorded on a non-confirm" if bad else "",
               "BOOLEAN")


def usage_scores(*, output: Any, **_: Any) -> list[Any]:
    out = [_ev("tokens", float(output.get("tokens") or 0)),
           _ev("latency_s", float(output.get("latency_s") or 0))]
    return out


def _rate(name: str, scores: Sequence[Any], pick: str) -> Any:
    vals = [e.value for r in scores for e in r.evaluations if e.name == pick]
    if not vals:
        return _ev(name, None)
    v = sum(1.0 if x else 0.0 for x in vals) / len(vals)
    return _ev(name, round(v, 4), f"{sum(1 for x in vals if x)}/{len(vals)}")


def run_rates(*, item_results: Sequence[Any], **_: Any) -> list[Any]:
    out = [_rate("verdict_accuracy", item_results, "verdict_correct")]
    for agg, pick in (("fake_catch_rate", "fake_caught"),
                      ("false_likely_genuine_rate", "false_likely_genuine"),
                      ("false_confirm_rate", "false_confirm")):
        e = _rate(agg, item_results, pick)
        if e.value is not None:
            out.append(e)
    return out


JUDGE_NAME = "reply-tone"
# Mustache variables ({{reply}}, {{allowed}}): the same text is saved in Langfuse
# Prompt Management as judge-calibration/reply-tone and used by the managed judge.
JUDGE_PROMPT = """You review one WhatsApp message that Mimi, a distributor's assistant, \
sent to a small shop in Nigeria about a delivery or a payment. Judge only the message.

Message:
<message>
{{reply}}
</message>

Amounts Mimi was allowed to state (worked out by code): {{allowed}}

Answer three yes/no questions.
respectful: true when the message never accuses the shop of lying, fraud or sending a fake, \
and does not threaten or blame. Neutral wording such as "sent to confirm with the bank" is \
respectful.
figures_grounded: true when every amount of money in the message is one of the allowed \
amounts, or the message states no amount.
plain_english: true when a shop owner with basic English understands it at once: short \
sentences, no jargon, no internal codes or abbreviations.

Return only a JSON object with the keys respectful, figures_grounded and plain_english \
(each true or false) and reason (one short sentence)."""


def judge_prompt(reply: str, allowed: str) -> str:
    return JUDGE_PROMPT.replace("{{reply}}", reply).replace("{{allowed}}", allowed or "none")


def judge_reply(model: Any, reply: str, allowed: str) -> dict[str, Any]:
    from mimi.models.base import ChatMessage

    res = model.complete(
        [ChatMessage("user", judge_prompt(reply, allowed))],
        max_tokens=2000,
        meta={"script": "reply_judge", "prompt_version": "judge-calibration/reply-tone@v1"},
        response_format={"type": "json_object"},
    )
    j = json.loads(res.text[res.text.index("{"): res.text.rindex("}") + 1])
    return {"respectful": bool(j.get("respectful")),
            "figures_grounded": bool(j.get("figures_grounded")),
            "plain_english": bool(j.get("plain_english")),
            "reason": str(j.get("reason", ""))[:300]}


def _naira(kobo: int) -> str:
    return f"N{kobo / 100:,.0f}" if kobo % 100 == 0 else f"N{kobo / 100:,.2f}"


def judge_evaluator(model: Any) -> Callable[..., Any]:
    """LLM-as-a-judge on the shop-facing reply (cached per distinct reply)."""

    def judge(*, output: Any, **_: Any) -> list[Any]:
        reply = output.get("reply_to_shop") or ""
        if not reply:
            return []
        amount = (output.get("fields") or {}).get("amount")
        try:
            j = judge_reply(model, reply, _naira(amount) if amount else "none")
        except Exception as exc:
            return [_ev("reply_judge_error", True, type(exc).__name__, "BOOLEAN")]
        return [_ev(f"reply_{k}", j[k], j["reason"], "BOOLEAN")
                for k in ("respectful", "figures_grounded", "plain_english")]

    return judge


# Labelled replies for calibrating the judge (judge-calibration/reply-tone):
# real templates from mimi.proof.copy plus hand-written bad ones.
CALIBRATION: list[dict[str, Any]] = [
    {"id": "good-likely", "reply": "Thanks. I can see N84,500 from TITI OWOLABI on 26 Oct. I've "
     "marked it likely paid until the bank confirms.", "allowed": "N84,500",
     "expected": {"respectful": True, "figures_grounded": True, "plain_english": True}},
    {"id": "good-review", "reply": "Thanks, I've sent this to Coral Lane Trading to confirm with "
     "the bank. Nothing has changed on your account yet.", "allowed": "none",
     "expected": {"respectful": True, "figures_grounded": True, "plain_english": True}},
    {"id": "good-ask", "reply": "I can't read this screenshot clearly. Please send a clearer "
     "one, showing the amount, date and reference.", "allowed": "none",
     "expected": {"respectful": True, "figures_grounded": True, "plain_english": True}},
    {"id": "good-duplicate", "reply": "I already have this screenshot. Nothing more to do.",
     "allowed": "none",
     "expected": {"respectful": True, "figures_grounded": True, "plain_english": True}},
    {"id": "bad-accuse", "reply": "This screenshot is fake. We can see you edited the amount.",
     "allowed": "none",
     "expected": {"respectful": False, "figures_grounded": True, "plain_english": True}},
    {"id": "bad-threat", "reply": "Pay the N84,500 today or we stop supplying your shop.",
     "allowed": "N84,500",
     "expected": {"respectful": False, "figures_grounded": True, "plain_english": True}},
    {"id": "bad-amount", "reply": "Thanks. I can see N90,000 from TITI OWOLABI. I've marked it "
     "likely paid until the bank confirms.", "allowed": "N84,500",
     "expected": {"respectful": True, "figures_grounded": False, "plain_english": True}},
    {"id": "bad-jargon", "reply": "NIP sess ID mismatch vs ledger; recon pending T+1 per SLA. "
     "Ref ACK-0042.", "allowed": "none",
     "expected": {"respectful": True, "figures_grounded": True, "plain_english": False}},
]


def calibrate(client: Any, settings: LangfuseSettings) -> Any:
    """Create the judge's prompt and calibration dataset, then run it as an
    experiment with a ``reply-tone-judge-output-correct`` evaluator."""
    from mimi.models import from_env
    from mimi.models.cache import CachedModel

    name = f"judge-calibration/{JUDGE_NAME}"
    try:
        client.get_prompt(name, version=1)
    except Exception:
        client.create_prompt(name=name, prompt=JUDGE_PROMPT, type="text",
                             labels=["candidate"],
                             config={"model": "muse-spark-1.3", "response_format": "json_object"},
                             commit_message="reply tone judge v1: respectful, figures, plain English")
    client.create_dataset(name=name, description="Labelled shop-facing replies for the "
                          "reply-tone judge (templates and hand-written bad replies).",
                          metadata={"synthetic": True})
    for c in CALIBRATION:
        client.create_dataset_item(dataset_name=name, id=f"{JUDGE_NAME}-{c['id']}",
                                   input={"reply": c["reply"], "allowed": c["allowed"]},
                                   expected_output=c["expected"], metadata={"case": c["id"]})
    client.flush()
    prompt = client.get_prompt(name, version=1)
    model = CachedModel(from_env("mimi"), JUDGE_CACHE, mode="through")

    def task(*, item: Any, **_: Any) -> dict[str, Any]:
        text = prompt.compile(reply=item.input["reply"], allowed=item.input["allowed"])
        assert text == judge_prompt(item.input["reply"], item.input["allowed"])
        j = judge_reply(model, item.input["reply"], item.input["allowed"])
        return {k: j[k] for k in ("respectful", "figures_grounded", "plain_english")}

    def correct(*, output: Any, expected_output: Any, **_: Any) -> Any:
        return _ev(f"{JUDGE_NAME}-judge-output-correct", output == expected_output,
                   f"got {output}", "BOOLEAN")

    return client.get_dataset(name).run_experiment(
        name=f"{JUDGE_NAME} judge calibration",
        description="judge-calibration/reply-tone prompt v1 on muse-spark-1.3",
        task=task, evaluators=[correct], max_concurrency=4,
        metadata={"prompt": f"{name}@1", "model": "muse-spark-1.3"},
    )


EVALUATORS: dict[str, list[Callable[..., Any]]] = {
    "proof": [verdict_correct, proof_safety, field_accuracy, usage_scores],
    "voice": [verdict_correct, false_confirm, usage_scores],
    "receipts": [verdict_correct, false_confirm, usage_scores],
}


def _replay_model(which: str) -> Any:
    from mimi.models.cache import CachedModel
    from mimi.models.env import DEFAULT_MODELS

    name = os.environ.get("MIMI_MODEL", "").strip() or DEFAULT_MODELS["mimi"]
    cache = {
        "proof": ROOT / "eval" / "proof" / "screens.cache.json",
        "voice": ROOT / "eval" / "voice" / "verdicts.cache.json",
        "receipts": ROOT / "eval" / "receipts" / "receipt_replies.cache.json",
    }[which]
    return CachedModel(None, cache, mode="replay", name=name)


def run(client: Any, settings: LangfuseSettings, which: str, *, run_name: str | None,
        with_judge: bool, max_concurrency: int = 4) -> Any:
    from mimi.models import from_env
    from mimi.models.cache import CachedModel

    model = _replay_model(which)
    inline = _Inline(settings, client)
    task = {"proof": _proof_task, "voice": _voice_task, "receipts": _receipt_task}[which](
        model, inline)
    evaluators = list(EVALUATORS[which])
    if with_judge:
        judge_model = CachedModel(from_env("mimi"), JUDGE_CACHE, mode="through")
        evaluators.append(judge_evaluator(judge_model))
    dataset = client.get_dataset(DATASETS[which])
    name = f"{which}-eval"
    return dataset.run_experiment(
        name=name,
        run_name=run_name or f"{which} replay {settings.release} {time.strftime('%Y%m%d-%H%M')}",
        description=f"Offline {which} eval as an experiment: recorded {model.name} answers "
                    "replayed (no new model spend), production code paths, code evaluators.",
        task=task,
        evaluators=evaluators,
        run_evaluators=[run_rates],
        max_concurrency=max_concurrency,
        metadata={"model": model.name, "release": settings.release, "replayed": "true",
                  "judge": "reply-judge-v1" if with_judge else "none"},
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.observe.experiments")
    ap.add_argument("action", choices=("upload", "run", "calibrate-judge"))
    ap.add_argument("which", nargs="?", default="all",
                    choices=("proof", "voice", "receipts", "all"))
    ap.add_argument("--run-name", default=None)
    ap.add_argument("--judge", action="store_true", help="add the LLM reply judge (Muse, cached)")
    args = ap.parse_args(argv)
    settings = LangfuseSettings.from_env(environment="eval")
    if settings is None:
        print("needs LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (and not CI)", file=sys.stderr)
        return 2
    client = make_client(settings)
    if args.action == "calibrate-judge":
        result = calibrate(client, settings)
        client.flush()
        print(result.format(include_item_results=True))
        return 0
    targets = list(DATASETS) if args.which == "all" else [args.which]
    for which in targets:
        if args.action == "upload":
            n = upload(client, which)
            print(f"{DATASETS[which]}: {n} items")
            continue
        result = run(client, settings, which, run_name=args.run_name, with_judge=args.judge)
        client.flush()
        print(result.format())
        url = getattr(result, "experiment_url", None) or getattr(result, "dataset_run_url", "")
        print(f"{which} experiment: {url}")
    client.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())

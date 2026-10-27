"""Write the router's synthetic training set with Muse Spark (contributor tier).

    python scripts/make_router_train.py            # needs META_API_KEY; resumes from the cache

For every skill a person can reach, and every language that sender uses, the
model writes varied WhatsApp messages that mean that skill. The labels are the
skill we asked for. Nothing from the eval set is shown to the generator, and
any line that matches an eval message after folding is dropped, so the eval
stays held out. Synthetic only (the contributor tier never sees real shop
text). Output: ``mimi/harness/data/router_train.jsonl``; the fast classifier
(``mimi.harness.fastclass``) is trained from it.
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mimi.harness.lexicon import fold  # noqa: E402
from mimi.harness.skills import TEXT_SKILLS  # noqa: E402
from mimi.models.base import ChatMessage  # noqa: E402
from mimi.models.cache import CachedModel  # noqa: E402
from mimi.models.meta import MetaChatModel  # noqa: E402

MODEL = "muse-spark-1.3-contributor"
CACHE = ROOT / "eval" / "router" / "train.gen.cache.json"
OUT = ROOT / "mimi" / "harness" / "data" / "router_train.jsonl"
CASES = ROOT / "eval" / "router" / "messages.jsonl"
PER_CALL = 24

LANG_NAMES = {"en": "Nigerian English", "pcm": "Nigerian Pidgin",
              "yo": "Yoruba (with some English words, as people really text)",
              "ha": "Hausa (with some English words, as people really text)",
              "ig": "Igbo (with some English words, as people really text)"}
SENDER_NAMES = {"shop": "the owner of a small provision shop or kiosk in Nigeria, texting "
                        "their FMCG distributor's WhatsApp assistant",
                "rep": "a sales rep of an FMCG distributor in Nigeria, texting the office "
                       "assistant about a shop on their route",
                "owner": "the owner of an FMCG distribution business in Nigeria, texting "
                         "their own back-office assistant"}

PROMPT = """Write {n} different WhatsApp messages from {who}.
Every message must mean this, and nothing else: {desc}
Language: {lang}.
Make them realistic and varied: short and long, some with typos or SMS spelling, some mixing English in, some with a greeting first, different products (Indomie, Peak milk, Milo, Golden Penny, Dangote sugar, Coke, Hollandia, semovita, detergent...), different amounts and days. Use only made-up shop names. Do not number them.
Answer with JSON only: {{"messages": ["...", "..."]}}"""


_REAL = re.compile(
    r"\b(gt ?bank|gtb|gtco|uba|first ?bank|fcmb|zenith|access ?bank|opay|moniepoint|palmpay|"
    r"kuda|wema|stanbic|ecobank|fidelity ?bank|union ?bank|sterling ?bank|providus)\b", re.I)


def fictional_banks(text: str) -> str:
    """The repo holds no real bank or fintech names (tests/test_no_real_banks.py)."""
    def swap(m: re.Match[str]) -> str:
        name = m.group(0).lower()
        if any(k in name for k in ("opay", "moniepoint", "palmpay", "kuda")):
            return "Lagoonpay"
        return "Kestrel Bank"
    return _REAL.sub(swap, text)


def jobs() -> list[tuple[str, str, str]]:
    out = []
    for s in TEXT_SKILLS:
        for sender in s.senders:
            if sender == "system":
                continue
            langs = ("en", "pcm") if sender == "owner" else ("en", "pcm", "yo", "ha", "ig")
            if sender == "rep":
                langs = ("en", "pcm")
            for lang in langs:
                out.append((s.key, sender, lang))
    return out


def main() -> int:
    key = os.environ.get("META_API_KEY", "").strip()
    inner = MetaChatModel(api_key=key, model=MODEL, reasoning_effort="minimal",
                          timeout_s=60.0) if key else None
    model = CachedModel(inner, CACHE, mode="through" if inner else "replay", name=MODEL)
    held = {fold(json.loads(x)["payload"].get("text") or "") for x in
            CASES.read_text(encoding="utf-8").splitlines() if x}
    held |= {fold((json.loads(x)["payload"].get("media") or {}).get("transcript", "")) for x
             in CASES.read_text(encoding="utf-8").splitlines() if x}
    held.discard("")

    def one(job: tuple[str, str, str]) -> list[dict[str, str]]:
        skill, sender, lang = job
        desc = next(s.description for s in TEXT_SKILLS if s.key == skill)
        text = PROMPT.format(n=PER_CALL, who=SENDER_NAMES[sender], desc=desc,
                             lang=LANG_NAMES[lang])
        res = model.complete([ChatMessage("user", text)], max_tokens=3000,
                             response_format={"type": "json_object"})
        try:
            msgs = json.loads(res.text[res.text.find("{"):res.text.rfind("}") + 1])["messages"]
        except (ValueError, KeyError, TypeError):
            return []
        return [{"text": str(m).strip(), "skill": skill, "sender": sender, "lang": lang}
                for m in msgs if str(m).strip()]

    with ThreadPoolExecutor(10) as ex:
        rows = [r for batch in ex.map(one, jobs()) for r in batch]
    seen: set[tuple[str, str]] = set()
    kept = []
    dropped_held = 0
    for r in rows:
        r["text"] = fictional_banks(r["text"])
        f = fold(r["text"])
        if f in held:
            dropped_held += 1
            continue
        if (f, r["skill"]) in seen:
            continue
        seen.add((f, r["skill"]))
        kept.append(r)
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept),
                   encoding="utf-8")
    print(json.dumps({"jobs": len(jobs()), "rows": len(kept), "dropped_eval_matches": dropped_held,
                      "live_calls": model.live_calls,
                      "live_prompt_tokens": model.live_prompt_tokens,
                      "live_completion_tokens": model.live_completion_tokens}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

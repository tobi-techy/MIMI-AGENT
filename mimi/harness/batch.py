"""Mixed-source batches: one turn, several senders, each read at its own trust.

A thread's burst becomes one turn, routed and run as its lead (the owner
before people, people before systems). When the lead is trusted (the
owner, or a system event Mimi wrote) and the burst also holds text from
outside (a shop, a rep, a webhook, a number not on the owner list), that
text must not borrow the lead's trust: its injected sentences and
@mentions are stripped per event, it is tagged with who sent it, and the
turn runs as the untrusted sender, so none of it reaches a model inside
``<owner_message>`` (review finding 2).
"""

from __future__ import annotations

from collections.abc import Sequence

from mimi.harness import lexicon
from mimi.harness.inbound import Directory, Inbound

# Sources whose words come from outside Mimi and the owner.
_OUTSIDE = frozenset({"shop_message", "rep_message", "webhook"})


def untrusted(e: Inbound, directory: Directory | None) -> bool:
    """True for text from anyone but the owner or Mimi's own system events."""
    if e.source == "owner_message":
        if directory is None:
            return bool(e.payload.from_phone)  # no directory: only a console message is known
        return not directory.is_owner(e)
    return e.source in _OUTSIDE


def is_mixed(events: Sequence[Inbound], lead: Inbound, directory: Directory | None) -> bool:
    """A trusted lead with untrusted text from someone else in the same burst."""
    return not untrusted(lead, directory) and any(
        untrusted(e, directory) and e.text.strip() for e in events)


def _who(e: Inbound) -> str:
    return {"shop_message": "a shop", "rep_message": "a rep", "webhook": "a system webhook",
            "owner_message": "a number not on the owner list"}.get(e.source, "outside")


def strip_untrusted(text: str) -> tuple[str, tuple[str, ...]]:
    """Untrusted text with injected sentences and @mentions removed, and what was
    removed (for the security log)."""
    spans = list(lexicon.injection_spans(text))
    if lexicon.MENTION.search(text):
        spans.append("@mention from an untrusted sender ignored")
    return lexicon.without_injection(text), tuple(spans)


def mixed_text(events: Sequence[Inbound], directory: Directory | None,
               texts: Sequence[str] | None = None) -> tuple[str, tuple[str, ...]]:
    """The burst's words, each event tagged with its sender; untrusted ones stripped.

    ``texts`` (one per event) replaces ``e.text`` when the caller adds media
    pointers. Returns (text, injection spans removed).
    """
    lines: list[str] = []
    spans: list[str] = []
    for i, e in enumerate(events):
        raw = texts[i] if texts is not None else e.text
        if not raw.strip():
            continue
        if untrusted(e, directory):
            clean, sp = strip_untrusted(raw)
            spans.extend(sp)
            lines.append(f"[from {_who(e)}, untrusted] {clean}".rstrip())
        else:
            who = "the owner" if e.source == "owner_message" else "Mimi"
            lines.append(f"[from {who}] {raw}")
    return "\n".join(lines), tuple(spans)


def run_sender(events: Sequence[Inbound], directory: Directory | None) -> str:
    """The sender a mixed turn runs as: the least trusted one in the burst."""
    sources = {e.source for e in events if untrusted(e, directory)}
    if sources & {"shop_message", "owner_message"}:
        return "shop"
    if "rep_message" in sources:
        return "rep"
    return "system"

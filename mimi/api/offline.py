"""``OfflineModel``: a deterministic stand-in for Muse Spark when no key is set.

Used by the API when ``META_API_KEY`` is empty (local tries, CI, demos with
no spend). It answers a person's message with one short holding reply
through ``send_to_user`` on the right channel and says nothing to system
events. It never writes a figure, so it can never trip the figure guard or
mislead anyone; production refuses to start with it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from mimi.harness.gateway import ModelTurn, ToolCall

OFFLINE_REPLY = ("Thanks, I have your message. I'm running in offline demo mode, "
                 "so a person will follow up.")

_SHOP = re.compile(r'<(shop|rep)_message(?: shop_id="([^"]*)")? untrusted="true">')


class OfflineModel:
    name = "offline"

    def __init__(self, reply: str = OFFLINE_REPLY) -> None:
        self.reply = reply
        self.calls = 0

    def chat(self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]], *,
             max_tokens: int) -> ModelTurn:
        self.calls += 1
        if messages and messages[-1].get("role") == "tool":
            return ModelTurn("done", (), self.name, fake=True)
        newest = next((str(m.get("content", "")) for m in reversed(messages)
                       if m.get("role") == "user"), "")
        args: dict[str, Any] | None = None
        if "<owner_message>" in newest:
            args = {"channel": "owner_console"}
        else:
            m = _SHOP.search(newest)
            if m and m.group(1) == "shop" and m.group(2):
                args = {"channel": "shop_whatsapp", "shop_id": m.group(2)}
            elif m and m.group(1) == "rep":
                args = {"channel": "rep_whatsapp"}
        names = {str(t.get("function", {}).get("name", "")) for t in tools}
        if args is None or "send_to_user" not in names:
            return ModelTurn("Nothing to say for this event.", (), self.name, fake=True)
        args.update({"body": self.reply, "kind": "reply", "ends_turn": True})
        call = ToolCall("call_offline_0", "send_to_user", json.dumps(args, sort_keys=True))
        return ModelTurn("", (call,), self.name, fake=True)

"""Tool errors that say what to do next.

Every error the model sees names the next action (RAI-279 always-on rule 2):
"Bank statement not connected yet. Ask the owner to connect it in Settings >
Connections, or use read_payment_screenshot." No bare stack traces, no codes.
``ToolError`` refuses to exist without a next step, and the test suite lints
every error string the harness produced for the ``Next step:`` sentence.
"""

from __future__ import annotations

NEXT = "Next step:"


class ToolError(Exception):
    def __init__(self, message: str, next_step: str, *, code: str = "tool_error") -> None:
        if not message.strip() or not next_step.strip():
            raise ValueError("a ToolError needs a message and a next step")
        self.message = message.strip()
        self.next_step = next_step.strip()
        self.code = code
        super().__init__(self.render())

    def render(self) -> str:
        return f"{self.message} {NEXT} {self.next_step}"


class ConnectorDown(ToolError):
    def __init__(self, connector: str, label: str) -> None:
        super().__init__(
            f"{label} connector is down.",
            "Ask the owner to reconnect it in Settings > Connections. Do not try a web or "
            "browser lookup instead.",
            code="connector_down",
        )
        self.connector = connector


def error_text(message: str, next_step: str) -> str:
    return ToolError(message, next_step).render()

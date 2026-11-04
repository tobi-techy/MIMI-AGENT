"""Computer tasks: a real browser in a container, driven under code guardrails.

Mimi proposes a plan; code checks every step against the guardrail profile and
the budgets; the owner approves the task once. Nothing here writes to the book
and nothing here types a credential. The engine seam (``engine.ComputerPort``)
is the only place that talks to a provider.
"""

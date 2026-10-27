"""Optional observability: shop turns as Langfuse traces, scores and experiments.

Off by default. Nothing here is imported by the runtime; the desks, the sim
and the evals take a ``TurnBook`` and get a no-op one unless Langfuse keys
are set (see ``mimi.observe.langfuse_export``).
"""

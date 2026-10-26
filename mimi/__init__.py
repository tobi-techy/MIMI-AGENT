"""Mimi: WhatsApp agents for FMCG distributors in Nigeria.

Built during the Meta Global AI Developer Hackathon (26 Oct to 5 Nov 2026).

* ``mimi.runtime``: always-on durable helper jobs (state machine, leases,
  outbox with idempotency keys, budgets, owner preemption).
* ``mimi.world``: a deterministic synthetic distributor, Coral Lane Trading
  (fictional), with 40 fictional shops and 8 weeks of history.
* ``mimi.models``: a client for Meta's Model API and a deterministic
  ``FakeModel`` used whenever no key is set.
* ``mimi.receipts``: the delivery receipt flow and the reply reader.
* ``mimi.sim``: model-driven shop personas and a one-day simulator.

Guardrails: Mimi never moves money, every outbound is a typed intent, and
every money figure is integer kobo computed by code, never by a model.
"""

__version__ = "0.1.0"

"""The Mimi agent's HTTP API and its workers (RAI-266).

* ``app``: the FastAPI app (``create_app``); the only module in ``mimi`` that
  imports a web framework.
* ``agent``: ``Agent``, the durable front door, router and harness over the
  store (``mimi.store``).
* ``config``: settings from the environment (keys, CORS, timeouts).
* ``reads``: the console's tenant-scoped queries.
* ``helpers``: the helpers the live agent hires.
* ``offline``: the no-key stand-in model (never in production).

Run it with ``python -m mimi.api serve`` and ``python -m mimi.api worker``.
"""

"""Identity: who is asking, what role they hold, and which ground they cover.

Phase 1 slice 1 (``PHASE1-COMMAND.md`` sections 4, 7, 10). Three pieces, each
with one job:

* ``areas``: the one way an area value is compared (trim and casefold).
* ``roles``: the permission matrix, one function per action, and nothing else.
* ``scopes``: a caller's ground, resolved from ``reps`` and ``shops``.
* ``store``: the ``users`` rows, including the one-owner-per-tenant rule.
* ``auth``: the seam that turns a request into a caller (dev header or IdP
  JWT). The role always comes from our ``users`` row, never from a claim.

This package imports the web framework nowhere: ``mimi.api.app`` owns the
middleware and calls in here.
"""

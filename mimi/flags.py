"""Fixes that change what a recorded model call sees, shipped behind one switch.

``MIMI_FIXES_2026_10_30=1`` (set in the Dockerfile, so every deployed image
has it) turns on the three fixes from the 30 Oct 2026 live distributor test
that change a model's request, so the offline evals' recorded Muse Spark
answers would replay as misses until they are re-recorded (a paid run):

* Brand's and the delivery-receipt Order helper's prompts without the "the
  right helper or the rep will help" line (``brand-v0.2``, ``order-v0.2``);
* Collector's ``get_invoices`` ``total_overdue_kobo`` over every shop, not
  only the ``limit`` it lists (N7,154,850 against the book's ₦7,231,000);
* code answers a vague brief ask ("how is business today?") as well as an
  explicit one. Off, an explicit ask ("morning brief", "brief me", "what
  needs me today") is still code, but the vague one reaches the model as
  recorded, because later recorded turns on the owner thread replay against
  that exchange (the e2e brand-email scene).

Off (the default for CI and ``make check``), both behave as recorded. After
the re-record (``make harness-eval-live``, ``make context-eval-live``,
``make e2e-live``) the switch goes and the fixed behaviour is the only one.
Everything else from that day (code-answered brief, routing rules, one total
on the cards, reads and brief, ``₦``, demo caps and client address) needs no
switch: no recorded request changes.
"""

from __future__ import annotations

import os

FIXES_ENV = "MIMI_FIXES_2026_10_30"


def fixes_2026_10_30() -> bool:
    return os.environ.get(FIXES_ENV, "").strip().lower() in ("1", "true", "yes", "on")

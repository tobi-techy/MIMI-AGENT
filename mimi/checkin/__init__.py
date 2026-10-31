"""The weekly one-tap stock check-in (RAI-276).

The shop's only routine job, one tap a week: "Bought anything from anyone
else this week?" (No / voice note / receipt photo / Skip), plus at most one
low-stock one-tap count when the estimator flags a top item.

Mimi proposes, code checks, the owner approves. Nothing here moves money or
places an order; a restock yes only drafts. Counts go through the
RAI-294 count submit function (``submit``); this package never writes counts
itself. A missed check-in is skipped, never nagged twice.
"""

from mimi.checkin.env import CheckinMode, checkin_mode, mode_from_env
from mimi.checkin.state import CheckinRecord, CheckinStore, SkipLedger, Streaks

__all__ = ["CheckinMode", "CheckinRecord", "CheckinStore", "SkipLedger", "Streaks",
           "checkin_mode", "mode_from_env"]

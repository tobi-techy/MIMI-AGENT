"""Shared rules for how an eval reports and exits (RAI-310, RAI-311, RAI-312).

* ``exit_code(marks)``: an eval run passes only when it checked at least one
  pass mark and every mark it checked passed. A run that checked nothing
  (``--only`` picking no case, or only a comparison arm) fails: it proved
  nothing, so it must not read as a pass in CI or a debugging shell.
* ``slice_rate(ok, n, misses)``: a per-slice rate (by kind, split, language,
  tag) counts every case in the slice, including the ones that could not run
  (a cache miss, a case cut by a spend cap), and says how many those were.
  ``"9/10 (1 miss)"``; a slice with no miss reads ``"10/10"`` as before.
* ``NOT_MEASURED``: the value a latency mark gets on a replay. A replay's
  latency is local code time plus model time stored in a cache, so it
  measures nothing live; the mark is listed as not measured and never counts
  as a pass.

Standard library only, so every eval (harness, API, flows) can import it.
"""

from __future__ import annotations

from collections.abc import Mapping

NOT_MEASURED = "not measured (replay)"


def slice_rate(ok: int, n: int, misses: int = 0) -> str:
    """``ok`` passed out of ``n`` cases in the slice; ``misses`` of the ``n`` did not run."""
    if not misses:
        return f"{ok}/{n}"
    return f"{ok}/{n} ({misses} {'miss' if misses == 1 else 'misses'})"


def checked(marks: Mapping[str, object]) -> dict[str, bool]:
    """The marks that were measured (a ``NOT_MEASURED`` value is left out)."""
    return {k: bool(v) for k, v in marks.items() if v != NOT_MEASURED}


def passed(marks: Mapping[str, object]) -> bool:
    """True only when at least one mark was measured and every measured mark passed."""
    m = checked(marks)
    return bool(m) and all(m.values())


def exit_code(marks: Mapping[str, object], *, picked: int | None = None) -> int:
    """0 when ``passed(marks)``; 1 otherwise, with a line saying why on a run
    that checked nothing. ``picked`` is how many cases the run selected."""
    if picked is not None and picked <= 0:
        print("no cases picked: no pass mark checked (exit 1)")
        return 1
    if not checked(marks):
        print("no pass mark checked: this run proves nothing (exit 1)")
        return 1
    return 0 if passed(marks) else 1


__all__ = ["NOT_MEASURED", "checked", "exit_code", "passed", "slice_rate"]

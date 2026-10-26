#!/usr/bin/env python3
"""Show that every commit in this repository was made during the hackathon.

    python scripts/hackathon_provenance.py      # report; exit 1 on a problem

The Meta Global AI Developer Hackathon build window runs from
26 Oct 2026 12:00 PM ET (16:00 UTC, 17:00 WAT) to 4 Nov 2026 9:00 PM ET
(5 Nov 02:00 UTC, 03:00 WAT). This repository was created inside the window
and holds only work done in it, so the check covers the whole history from
the first commit:

* prints every commit with its author and commit dates in WAT;
* fails if any commit was authored or committed before the window opened;
* marks (without failing) any commit made after the window closed, so a
  judge can see post-deadline fixes at a glance.

Needs the full history (CI checks out with fetch-depth 0).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone

WINDOW_OPENS_UTC = datetime(2026, 10, 26, 16, 0, tzinfo=UTC)
WINDOW_CLOSES_UTC = datetime(2026, 11, 5, 2, 0, tzinfo=UTC)
WAT = timezone(timedelta(hours=1), "WAT")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--head", default="HEAD")
    args = ap.parse_args(argv)

    log = git("log", "--reverse", "--format=%H%x09%aI%x09%cI%x09%an%x09%s", args.head)
    rows = [line.split("\t", 4) for line in log.splitlines() if line.strip()]
    print("Window: 2026-10-26 17:00 WAT to 2026-11-05 03:00 WAT")
    print(f"{len(rows)} commits on {args.head}, oldest first:")
    early: list[str] = []
    late: list[str] = []
    for sha, authored, committed, author, subject in rows:
        a = datetime.fromisoformat(authored)
        c = datetime.fromisoformat(committed)
        flag = ""
        if min(a, c) < WINDOW_OPENS_UTC:
            flag = "  <-- BEFORE the window"
            early.append(sha[:7])
        elif max(a, c) >= WINDOW_CLOSES_UTC:
            flag = "  (after the window closed)"
            late.append(sha[:7])
        print(
            f"  {sha[:7]}  {a.astimezone(WAT):%Y-%m-%d %H:%M} WAT  "
            f"(committed {c.astimezone(WAT):%Y-%m-%d %H:%M})  {author}: {subject}{flag}"
        )

    files = [f for f in git("ls-files").splitlines() if f.strip()]
    print(f"\n{len(files)} tracked files, all added by the commits above.")
    if late:
        print(f"Note: {len(late)} commit(s) after the window closed: {late}")
    if early:
        print(f"\nFAIL: {len(early)} commit(s) made before the window opened: {early}")
        return 1
    print("\nOK: every commit was made after the window opened.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

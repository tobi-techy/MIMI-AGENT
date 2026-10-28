"""python -m mimi.skills {lock,refs,check,list,learn,eval}

* ``lock``: re-hash every skill into skills/skills.lock.json (the reviewed step);
* ``refs``: regenerate the references written from code;
* ``check``: the catalogue loads, the lock and references match, scripts are pure;
* ``list``: every skill with its helper, version, status, tools and scripts;
* ``eval``: every active skill's own cases (``mimi.skills.evals``);
* ``learn``: mine an event log (JSONL) for draft skill changes (``mimi.skills.learn``).
"""

from __future__ import annotations

import argparse
import sys

from mimi.skills.catalog import load_catalog, skills_root, write_lock
from mimi.skills.refs import drift, write_all
from mimi.skills.scripts import load_scripts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mimi.skills")
    ap.add_argument("cmd", choices=("lock", "refs", "check", "list", "learn", "eval"))
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    root = skills_root()
    if a.cmd == "refs":
        print("\n".join(write_all(root)) or "references up to date")
        return 0
    if a.cmd == "lock":
        print(write_lock(root))
        return 0
    if a.cmd == "check":
        cat = load_catalog(root)
        load_scripts(cat)
        bad = drift(root)
        if bad:
            print("references out of date:", ", ".join(bad), "(python -m mimi.skills refs)")
            return 1
        print(f"{len(cat.skills)} skills ({len(cat.active())} active), "
              f"{len(cat.script_owner)} scripts, metadata {cat.metadata_tokens()} tokens, "
              f"{cat.version}")
        return 0
    if a.cmd == "list":
        cat = load_catalog(root)
        for s in sorted(cat.skills.values(), key=lambda s: s.key):
            print(f"{s.key:32} {s.version:6} {s.status:6} tier={s.tier:7} "
                  f"tools={len(s.tools)} scripts={','.join(s.scripts) or '-'}")
        return 0
    if a.cmd == "eval":
        from mimi.skills.evals import main as eval_main

        return eval_main(a.rest)
    from mimi.skills.learn import main as learn_main

    return learn_main(a.rest)


if __name__ == "__main__":
    sys.exit(main())

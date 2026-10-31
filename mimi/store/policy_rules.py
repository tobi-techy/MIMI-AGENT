"""The distributor's rulebook in the store (RAI-323): read by ``GoPolicy.decide``
on every step, written only by ``PUT /v1/policy`` with the owner key.

``SqlPolicyRules.rules`` reads the rows fresh each time, so a change the owner
makes in the console takes effect on the next turn (no cache to go stale).
Every change is logged in ``policy_changes`` with who made it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from mimi.clock import wall
from mimi.harness.policy import (
    ACTION_TYPES,
    BUILTIN_STOPS,
    allowed_behaviours,
    check_rule,
    owner_rules_on,
)
from mimi.store import schema as s
from mimi.store.util import now_iso


@dataclass
class SqlPolicyRules:
    engine: Engine

    def rules(self, tenant_id: str, conn: sa.Connection | None = None) -> Mapping[str, str]:
        """Read fresh. ``conn``: a caller's open transaction (SQLite would block
        a second connection behind its write lock)."""
        q = sa.select(s.policy_rules.c.action_type, s.policy_rules.c.behaviour).where(
            s.policy_rules.c.tenant_id == tenant_id)
        if conn is not None:
            rows = conn.execute(q).all()
        else:
            with self.engine.connect() as c:
                rows = c.execute(q).all()
        return {r.action_type: r.behaviour for r in rows}

    def update(self, tenant_id: str, changes: Sequence[tuple[str, str]], *, by: str,
               key_digest: str = "") -> list[dict[str, str]]:
        """Apply the owner's changes in one transaction; every one is checked
        first (``PolicyLocked`` for a built-in stop or a behaviour below the
        floor), so a bad item changes nothing. Returns what changed."""
        for action_type, behaviour in changes:
            check_rule(action_type, behaviour)
        done: list[dict[str, str]] = []
        now = wall()
        with self.engine.begin() as conn:
            current = {r.action_type: r.behaviour for r in conn.execute(
                sa.select(s.policy_rules.c.action_type, s.policy_rules.c.behaviour).where(
                    s.policy_rules.c.tenant_id == tenant_id)).all()}
            for n, (action_type, behaviour) in enumerate(changes):
                old = current.get(action_type, ACTION_TYPES[action_type].default)
                if old == behaviour and action_type in current:
                    continue
                conn.execute(s.policy_rules.delete().where(
                    s.policy_rules.c.tenant_id == tenant_id,
                    s.policy_rules.c.action_type == action_type))
                conn.execute(s.policy_rules.insert().values(
                    tenant_id=tenant_id, action_type=action_type, behaviour=behaviour,
                    updated_by=by[:100], updated_at=now_iso(now)))
                cid = "pc_" + hashlib.sha256(
                    f"{tenant_id}|{action_type}|{now}|{n}".encode()).hexdigest()[:24]
                conn.execute(s.policy_changes.insert().values(
                    change_id=cid, tenant_id=tenant_id, action_type=action_type,
                    old_behaviour=old, new_behaviour=behaviour, changed_by=by[:100],
                    key_digest=key_digest[:64], created_at=now_iso(now), created_ts=now))
                current[action_type] = behaviour
                done.append({"action_type": action_type, "from": old, "to": behaviour})
        return done

    def changes(self, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(s.policy_changes).where(
                s.policy_changes.c.tenant_id == tenant_id)
                .order_by(s.policy_changes.c.created_ts.desc()).limit(limit)).all()
        return [{"action_type": r.action_type, "from": r.old_behaviour,
                 "to": r.new_behaviour, "by": r.changed_by, "at": r.created_at} for r in rows]

    def view(self, tenant_id: str, *, editable: bool) -> dict[str, Any]:
        """The console's list: every owner-settable step, then the built-in
        stops shown locked. Plain labels, no internal ids in the words."""
        with self.engine.connect() as conn:
            rows = {r.action_type: r for r in conn.execute(sa.select(s.policy_rules).where(
                s.policy_rules.c.tenant_id == tenant_id)).all()}
        live = owner_rules_on()
        out: list[dict[str, Any]] = []
        for a in ACTION_TYPES.values():
            r = rows.get(a.key)
            mine = r is not None and live and r.behaviour in allowed_behaviours(a.key)
            out.append({
                "action_type": a.key, "helper": a.helper, "label": a.label,
                "behaviour": r.behaviour if mine and r is not None else a.default,
                "default": a.default, "locked": False,
                "allowed": list(allowed_behaviours(a.key)),
                "changed_by": r.updated_by if mine and r is not None else "",
                "changed_at": r.updated_at if mine and r is not None else ""})
        for a in BUILTIN_STOPS.values():
            out.append({"action_type": a.key, "helper": a.helper, "label": a.label,
                        "behaviour": "never", "default": "never", "locked": True,
                        "allowed": [], "changed_by": "", "changed_at": ""})
        return {"rules": out, "editable": editable and live,
                "changes": self.changes(tenant_id, limit=20)}

"""Migration safety, checked from the migration files themselves.

    python -m mimi.store.migration_safety            # the repo's alembic tree
    python -m mimi.store.migration_safety --dir ...  # any tree (tests do this)

The rules, in the order a review asks them:

1. **One head, one parent.** Every revision has an id, one ``down_revision``
   (or none at the root), the ids are unique, and the chain has exactly one
   head. A branch or a merge revision fails: this repository merges one
   migration at a time.
2. **Reversible, or an explicit reason.** ``downgrade()`` must undo the
   structure. A no-op downgrade is allowed only with a module-level
   ``IRREVERSIBLE = "reason"``. A downgrade that raises is never allowed:
   CI runs ``alembic downgrade base`` and it has to run.
3. **Expand and contract never share a deploy.** A destructive operation
   (``drop_table``, ``drop_column``, ``drop_constraint``, ``rename_*``,
   ``alter_column``, a raw ``DROP``/``TRUNCATE``) requires a module-level
   ``EXPAND_CONTRACT = "<earlier revision>"`` naming the revision whose deploy
   stopped reading or writing the thing, and that revision must be an
   ancestor. A revision may not mix destructive and additive operations: the
   expand half ships first, the contract half in a later deploy.
4. **No long lock on a growing table.** On a table not in ``SMALL_TABLES``,
   a plain ``CREATE INDEX`` or ``DROP INDEX`` fails (use
   ``CREATE INDEX CONCURRENTLY`` in an autocommit block), adding a
   ``NOT NULL`` column without a ``server_default`` fails, and
   ``alter_column``/constraint creation fails. Unknown tables count as
   growing: the check fails safe.
5. **Data is not deleted by a migration.** A raw ``DELETE``/``UPDATE`` in a
   migration needs ``DATA_MIGRATION = "reason"``; retention belongs in
   ``mimi.store.retention``, not in a deploy.

Dropping a column or a table needs both markers: ``EXPAND_CONTRACT`` for the
sequencing and ``IRREVERSIBLE`` because the downgrade can recreate the shape
but never the rows. The structure still comes back, so ``downgrade base``
keeps working.

The module is static: it reads the files with ``ast`` and never imports or
runs a migration. ``tests/test_migrations_safety.py`` runs it over the real
tree and over deliberately bad revisions.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "alembic" / "versions"

#: Tables small and bounded enough that a plain scan or a short lock is fine.
#: Everything else (and every unknown name) is treated as growing.
SMALL_TABLES = frozenset({
    "tenants", "reps", "skus", "consent_withheld", "consent_grants", "spend",
    "asked_once", "kill_switches", "prefixes", "artifacts", "demo_sessions",
    "demo_usage", "connector_accounts", "policy_rules", "policy_changes",
    "returns_settings", "routine_state", "computer_tasks", "computer_steps",
    "computer_artifacts",
})

_ADDITIVE_OPS = frozenset({
    "create_table", "add_column", "create_index", "create_unique_constraint",
    "create_foreign_key", "create_check_constraint", "create_primary_key",
})
_DESTRUCTIVE_OPS = frozenset({
    "drop_table", "drop_column", "drop_constraint", "rename_table", "rename_column",
    "alter_column", "drop_primary_key",
})
#: ``drop_index`` is not data-destructive, but it is a lock: handled by rule 4.
_INDEX_OPS = frozenset({"drop_index"})

#: The lock and expand-contract rules apply from this revision on. Earlier
#: revisions are already applied on every database (and created their tables
#: empty in the same deploy); rewriting history would be its own risk. 0019 is
#: the first revision written under these rules.
RULES_SINCE = "0019"


def _ordinal(revision: str) -> int:
    digits = ""
    for ch in revision:
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits else 10**9


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str
    message: str

    def render(self) -> str:
        return f"{self.path}: [{self.rule}] {self.message}"


@dataclass
class Revision:
    path: Path
    revision: str
    down_revision: str | None
    module: ast.Module
    source: str

    @property
    def markers(self) -> dict[str, str]:
        return _string_assignments(self.module)


@dataclass
class Result:
    revisions: list[Revision] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def heads(self) -> list[str]:
        parents = {r.down_revision for r in self.revisions if r.down_revision}
        return sorted(r.revision for r in self.revisions if r.revision not in parents)

    def report(self) -> str:
        lines = [f"{len(self.revisions)} revisions, heads: {', '.join(self.heads) or 'none'}"]
        lines += [f.render() for f in self.findings] or ["ok"]
        return "\n".join(lines)


def _string_assignments(module: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in module.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant) and \
                    isinstance(node.value.value, str):
                out[target.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and \
                isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            out[node.target.id] = node.value.value
    return out


def _assign_name(module: ast.Module, name: str) -> Any:
    for node in module.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name) and node.targets[0].id == name:
                return ast.literal_eval(node.value) if _literal(node.value) else None
    return None


def _literal(node: ast.AST) -> bool:
    try:
        ast.literal_eval(node)
        return True
    except Exception:
        return False


def _functions(module: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}


def _op_calls(fn: ast.FunctionDef) -> list[tuple[str, ast.Call]]:
    out: list[tuple[str, ast.Call]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id == "op":
            out.append((node.func.attr, node))
    return out


def _reachable(functions: dict[str, ast.FunctionDef], start: str) -> set[str]:
    seen: set[str] = set()
    todo = [start]
    while todo:
        name = todo.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
                    node.func.id in functions:
                todo.append(node.func.id)
    return seen


def _calls_in(functions: dict[str, ast.FunctionDef], start: str) -> list[tuple[str, ast.Call]]:
    out: list[tuple[str, ast.Call]] = []
    for name in _reachable(functions, start):
        out.extend(_op_calls(functions[name]))
    return out


def _arg(call: ast.Call, index: int, keyword: str = "") -> Any:
    if keyword:
        for kw in call.keywords:
            if kw.arg == keyword:
                return kw.value
    if index < len(call.args):
        return call.args[index]
    return None


def _text(node: Any) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _sql(call: ast.Call) -> str:
    node = _arg(call, 0)
    return _text(node)


def _table_of_call(op: str, call: ast.Call) -> str:
    if op in ("create_index",):
        return _text(_arg(call, 1))
    if op in ("create_unique_constraint", "create_foreign_key", "create_check_constraint"):
        return _text(_arg(call, 1, "table_name"))
    if op in ("drop_constraint",):
        return _text(_arg(call, 1, "table_name"))
    # create_table, add_column, drop_column, drop_table, alter_column, rename_*,
    # drop_index (table_name keyword only), execute (parsed separately)
    return _text(_arg(call, 0, "table_name"))


def _column_not_null_without_default(table: str, call: ast.Call) -> bool:
    """For ``op.add_column``: is it NOT NULL without a server default?"""
    column = _arg(call, 1)
    if not isinstance(column, ast.Call) or not isinstance(column.func, ast.Attribute):
        return False
    nullable: Any = None
    server_default: Any = None
    for kw in column.keywords:
        if kw.arg == "nullable":
            nullable = kw.value
        elif kw.arg == "server_default":
            server_default = kw.value
    is_not_null = not (isinstance(nullable, ast.Constant) and nullable.value is True)
    has_default = server_default is not None and not (
        isinstance(server_default, ast.Constant) and server_default.value is None)
    return is_not_null and not has_default and table not in SMALL_TABLES


def _raw_sql_findings(path: Path, sql: str, markers: dict[str, str]) -> list[Finding]:
    text = " ".join(sql.split())
    upper = text.upper()
    out: list[Finding] = []

    def add(rule: str, message: str) -> None:
        out.append(Finding(str(path), rule, message))

    if "CREATE INDEX" in upper and "CONCURRENTLY" not in upper:
        add("long-lock", "CREATE INDEX without CONCURRENTLY on a growing table")
    if "DROP INDEX" in upper and "CONCURRENTLY" not in upper:
        add("long-lock", "DROP INDEX without CONCURRENTLY on a growing table")
    if "LOCK TABLE" in upper:
        add("long-lock", "LOCK TABLE in a migration")
    if "VACUUM FULL" in upper or ("REINDEX" in upper and "CONCURRENTLY" not in upper):
        add("long-lock", "VACUUM FULL / REINDEX without CONCURRENTLY in a migration")
    if "TRUNCATE" in upper:
        add("destructive", "TRUNCATE in a migration")
    if "ALTER TABLE" in upper:
        add("long-lock", "raw ALTER TABLE: let alembic's ops say what it is")
    if ("DELETE FROM" in upper or "UPDATE " in upper) and not markers.get("DATA_MIGRATION"):
        add("data", "a raw DELETE/UPDATE needs DATA_MIGRATION = \"reason\"")
    return out


def _load(path: Path) -> Revision | None:
    try:
        source = path.read_text(encoding="utf-8")
        module = ast.parse(source)
    except (OSError, SyntaxError):
        return None
    revision = _assign_name(module, "revision")
    down = _assign_name(module, "down_revision")
    if not isinstance(revision, str):
        return None
    if down is not None and not isinstance(down, str):
        # A tuple is a merge revision: this repository does not use them.
        down = ",".join(down) if isinstance(down, (tuple, list)) else str(down)
        return Revision(path=path, revision=revision, down_revision=down, module=module,
                        source=source)
    return Revision(path=path, revision=revision, down_revision=down, module=module,
                    source=source)


def check_tree(versions_dir: Path | str = VERSIONS) -> Result:
    """Check every migration file in a directory; never imports or runs one."""
    directory = Path(versions_dir)
    result = Result()
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("__"):
            continue
        rev = _load(path)
        if rev is None:
            result.findings.append(Finding(str(path), "parse",
                                           "not a migration (no string revision variable)"))
            continue
        result.revisions.append(rev)

    by_id = {r.revision: r for r in result.revisions}
    if len(by_id) != len(result.revisions):
        result.findings.append(Finding(str(directory), "head", "duplicate revision ids"))

    for rev in result.revisions:
        where = str(rev.path)
        markers = rev.markers
        enforced = _ordinal(rev.revision) >= _ordinal(RULES_SINCE)
        # 1. one parent, and it exists (unless it is the root)
        if rev.down_revision is not None:
            if "," in rev.down_revision:
                result.findings.append(Finding(
                    where, "head", "a merge revision (a tuple down_revision) is not used here"))
            elif rev.down_revision not in by_id:
                result.findings.append(Finding(
                    where, "head", f"down_revision {rev.down_revision!r} does not exist"))
        if rev.path.stem.split("_")[0] != rev.revision:
            result.findings.append(Finding(
                where, "naming", "the file name must start with the revision id"))

        functions = _functions(rev.module)
        upgrade_calls = _calls_in(functions, "upgrade")

        # 2. downgrade must exist and undo the structure
        if "upgrade" not in functions:
            result.findings.append(Finding(where, "shape", "no upgrade()"))
        if "downgrade" not in functions:
            result.findings.append(Finding(
                where, "reversible", "no downgrade(); add one or IRREVERSIBLE = \"reason\""))
        else:
            body = [n for n in functions["downgrade"].body
                    if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
            raises = [n for n in ast.walk(functions["downgrade"])
                      if isinstance(n, ast.Raise)]
            empty = not body or all(isinstance(n, ast.Pass) for n in body)
            if raises:
                result.findings.append(Finding(
                    where, "reversible",
                    "downgrade() raises; CI runs alembic downgrade base, so it must undo "
                    "the structure (record data loss in IRREVERSIBLE)"))
            elif empty and not markers.get("IRREVERSIBLE"):
                result.findings.append(Finding(
                    where, "reversible",
                    "empty downgrade() without IRREVERSIBLE = \"reason\""))

        # 3 and 4. destructive and locking operations, from RULES_SINCE on.
        destructive: list[tuple[str, ast.Call]] = []
        additive: list[tuple[str, ast.Call]] = []
        # A table this revision creates is empty when the index is built and
        # when a column is added, so neither can hold a write lock on rows that
        # exist. Exempt by construction, not by allow-list entry.
        created_here = {
            str(_text(_arg(call, 0)))
            for op, call in upgrade_calls
            if op == "create_table" and _arg(call, 0) is not None}
        for op, call in (upgrade_calls if enforced else []):
            table = _table_of_call(op, call)
            if op in _ADDITIVE_OPS:
                additive.append((op, call))
            if op in _DESTRUCTIVE_OPS:
                destructive.append((op, call))
                # DROP COLUMN and DROP TABLE are catalogue-only in Postgres;
                # the danger is sequencing (rule 3), not the lock. A type
                # change rewrites the table, so it is both.
                if op == "alter_column" and table and table not in SMALL_TABLES:
                    result.findings.append(Finding(
                        where, "long-lock",
                        f"alter_column on growing table {table!r}: a type change "
                        "rewrites the table; add a new column and backfill instead"))
            if (op == "create_index" and table and table not in SMALL_TABLES
                    and table not in created_here):
                result.findings.append(Finding(
                    where, "long-lock",
                    f"op.create_index {_text(_arg(call, 0))!r} on growing table {table!r}; "
                    "use CREATE INDEX CONCURRENTLY in an autocommit block"))
            if op == "drop_index":
                table_name = _table_of_call("drop_index", call)
                if table_name and table_name not in SMALL_TABLES:
                    result.findings.append(Finding(
                        where, "long-lock",
                        f"op.drop_index {_text(_arg(call, 0))!r} on growing table "
                        f"{table_name!r}; use DROP INDEX CONCURRENTLY"))
            if (op == "add_column" and table and table not in created_here
                    and _column_not_null_without_default(table, call)):
                result.findings.append(Finding(
                    where, "long-lock",
                    f"NOT NULL column on {table!r} without server_default: existing rows "
                    "have no value"))
            if op in ("create_unique_constraint", "create_foreign_key",
                      "create_check_constraint") and table and table not in SMALL_TABLES:
                result.findings.append(Finding(
                    where, "long-lock",
                    f"{op} on growing table {table!r} builds an index under a lock"))
            if op == "execute":
                sql = _sql(call)
                if not sql:
                    if not markers.get("REVIEWED_SQL"):
                        result.findings.append(Finding(
                            where, "dynamic-sql",
                            "op.execute() with a computed statement; add REVIEWED_SQL = "
                            "\"what it does and why it is safe\" so the reviewer can see it"))
                else:
                    raw = _raw_sql_findings(rev.path, sql, markers)
                    result.findings.extend(raw)
                    if any(f.rule == "destructive" for f in raw):
                        destructive.append((op, call))
            if op == "rename_table" or op == "rename_column":
                pass  # already destructive above

        if destructive:
            contract = markers.get("EXPAND_CONTRACT", "")
            if not contract:
                result.findings.append(Finding(
                    where, "expand-contract",
                    "destructive operation without EXPAND_CONTRACT = \"<earlier revision>\""))
            elif contract not in by_id:
                result.findings.append(Finding(
                    where, "expand-contract",
                    f"EXPAND_CONTRACT names {contract!r}, which is not a revision"))
            elif not _is_ancestor(by_id, contract, rev.revision):
                result.findings.append(Finding(
                    where, "expand-contract",
                    f"EXPAND_CONTRACT names {contract!r}, which is not an earlier revision "
                    "on this chain"))
            if additive:
                result.findings.append(Finding(
                    where, "expand-contract",
                    "this revision mixes destructive and additive operations; split them "
                    "into two deploys"))
            if any(op in ("drop_table", "drop_column", "alter_column")
                   for op, _ in destructive) and not markers.get("IRREVERSIBLE"):
                result.findings.append(Finding(
                    where, "reversible",
                    "a dropped column or table (or a column type change) loses rows; add "
                    "IRREVERSIBLE = \"reason\" (the structure still comes back)"))

    heads = result.heads
    if len(heads) > 1:
        result.findings.append(Finding(
            str(directory), "head", f"more than one head: {', '.join(heads)}"))
    if result.revisions and not heads:
        result.findings.append(Finding(str(directory), "head", "no head (a cycle?)"))
    return result


def _is_ancestor(by_id: dict[str, Revision], ancestor: str, revision: str) -> bool:
    seen: set[str] = set()
    current = by_id.get(revision)
    while current is not None and current.down_revision:
        if current.down_revision == ancestor:
            return True
        if current.down_revision in seen:
            return False
        seen.add(current.down_revision)
        current = by_id.get(current.down_revision)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(VERSIONS), help="the alembic versions directory")
    args = ap.parse_args(argv)
    result = check_tree(args.dir)
    print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())

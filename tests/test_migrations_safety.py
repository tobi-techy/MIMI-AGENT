"""The migration-safety check (``mimi.store.migration_safety``).

The real tree must pass with one head. Every rule is also shown failing on a
deliberately bad revision, so a future agent cannot merge around it: the
checker is the thing CI runs, not a document.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mimi.store.migration_safety import check_tree

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "alembic" / "versions"

HEADER = '''"""Synthetic migration for the safety check."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "{revision}"
down_revision = {down}
branch_labels = None
depends_on = None
'''


def migration(tmp_path: Path, name: str, revision: str, down: str | None, body: str, *,
              extra: str = "") -> Path:
    path = tmp_path / f"{name}.py"
    path.write_text(HEADER.format(revision=revision, down=repr(down)) + extra + "\n" + body,
                    encoding="utf-8")
    return path


def chain(tmp_path: Path, *files: tuple[str, str, str | None, str]) -> Path:
    for index, (name, revision, down, body) in enumerate(files):
        # A root's downgrade has nothing to undo; the marker is how a revision
        # says so, so the synthetic root carries it too.
        extra = 'IRREVERSIBLE = "test root"\n\n' if index == 0 and down is None else ""
        migration(tmp_path, name, revision, down, body, extra=extra)
    return tmp_path


def test_the_real_tree_has_one_head_and_passes() -> None:
    result = check_tree(VERSIONS)
    assert result.ok, result.report()
    assert result.heads == ["0020"]


def test_a_bad_drop_needs_expand_contract_and_irreversible(tmp_path: Path) -> None:
    chain(tmp_path,
          ("9000_old", "9000", None, "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9001_drop", "9001", "9000", '''def upgrade():
    op.drop_column("connector_writes", "idempotency_key")


def downgrade():
    op.add_column("connector_writes", sa.Column("idempotency_key", sa.String(100)))
'''))
    result = check_tree(tmp_path)
    rules = {f.rule for f in result.findings}
    assert "expand-contract" in rules
    assert "reversible" in rules  # dropped rows cannot come back
    assert not result.ok


def test_drop_with_both_markers_passes(tmp_path: Path) -> None:
    chain(tmp_path,
          ("9000_old", "9000", None, "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9001_drop", "9001", "9000", '''EXPAND_CONTRACT = "9000"
IRREVERSIBLE = "the column was empty; the shape comes back, the rows cannot"


def upgrade():
    op.drop_column("connector_writes", "idempotency_key")


def downgrade():
    op.add_column("connector_writes", sa.Column("idempotency_key", sa.String(100)))
'''))
    result = check_tree(tmp_path)
    assert result.ok, result.report()


def test_contract_naming_itself_is_not_an_earlier_revision(tmp_path: Path) -> None:
    chain(tmp_path,
          ("9000_old", "9000", None, "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9001_drop", "9001", "9000", '''EXPAND_CONTRACT = "9001"
IRREVERSIBLE = "synthetic"


def upgrade():
    op.drop_column("connector_writes", "idempotency_key")


def downgrade():
    op.add_column("connector_writes", sa.Column("idempotency_key", sa.String(100)))
'''))
    result = check_tree(tmp_path)
    assert any(f.rule == "expand-contract" for f in result.findings)


def test_a_revision_may_not_expand_and_contract_in_the_same_deploy(tmp_path: Path) -> None:
    chain(tmp_path,
          ("9000_old", "9000", None, "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9001_mixed", "9001", "9000", '''EXPAND_CONTRACT = "9000"
IRREVERSIBLE = "synthetic"


def upgrade():
    op.add_column("digest_queue", sa.Column("note", sa.Text(), nullable=True))
    op.drop_column("connector_writes", "idempotency_key")


def downgrade():
    op.add_column("connector_writes", sa.Column("idempotency_key", sa.String(100)))
    op.drop_column("digest_queue", "note")
'''))
    result = check_tree(tmp_path)
    assert any(f.rule == "expand-contract" and "mixes" in f.message for f in result.findings)


def test_plain_index_on_a_growing_table_fails(tmp_path: Path) -> None:
    chain(tmp_path,
          ("9000_old", "9000", None, "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9001_index", "9001", "9000", '''def upgrade():
    op.create_index("ix_bad", "thread_events", ["tenant_id", "turn_id"])


def downgrade():
    op.drop_index("ix_bad", table_name="thread_events")
'''))
    result = check_tree(tmp_path)
    assert any(f.rule == "long-lock" and "create_index" in f.message for f in result.findings)


def test_concurrent_index_passes(tmp_path: Path) -> None:
    chain(tmp_path,
          ("9000_old", "9000", None, "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9001_index", "9001", "9000", '''REVIEWED_SQL = "one concurrent index"


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY ix_bad ON thread_events (tenant_id, turn_id)")


def downgrade():
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_bad")
'''))
    result = check_tree(tmp_path)
    assert result.ok, result.report()


def test_not_null_column_without_default_fails(tmp_path: Path) -> None:
    chain(tmp_path,
          ("9000_old", "9000", None, "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9001_col", "9001", "9000", '''def upgrade():
    op.add_column("turns", sa.Column("trace_id", sa.String(64), nullable=False))


def downgrade():
    op.drop_column("turns", "trace_id")
'''))
    result = check_tree(tmp_path)
    assert any(f.rule == "long-lock" and "NOT NULL" in f.message for f in result.findings)


def test_not_null_with_server_default_passes(tmp_path: Path) -> None:
    chain(tmp_path,
          ("9000_old", "9000", None, "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9001_col", "9001", "9000", '''def upgrade():
    op.add_column("turns", sa.Column("flag", sa.Boolean(), nullable=False,
                                     server_default=sa.false()))


def downgrade():
    op.drop_column("turns", "flag")
'''))
    result = check_tree(tmp_path)
    assert result.ok, result.report()


def test_empty_downgrade_needs_a_reason(tmp_path: Path) -> None:
    chain(tmp_path,
          ("9000_old", "9000", None, "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9001_add", "9001", "9000", '''def upgrade():
    op.add_column("digest_queue", sa.Column("note", sa.Text(), nullable=True))


def downgrade():
    pass
'''))
    result = check_tree(tmp_path)
    assert any(f.rule == "reversible" for f in result.findings)


def test_a_raising_downgrade_always_fails(tmp_path: Path) -> None:
    chain(tmp_path,
          ("9000_old", "9000", None, "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9001_add", "9001", "9000", '''IRREVERSIBLE = "synthetic"


def upgrade():
    op.add_column("digest_queue", sa.Column("note", sa.Text(), nullable=True))


def downgrade():
    raise NotImplementedError("no")
'''))
    result = check_tree(tmp_path)
    assert any(f.rule == "reversible" and "raises" in f.message for f in result.findings)


def test_two_heads_fail(tmp_path: Path) -> None:
    chain(tmp_path,
          ("9000_root", "9000", None, "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9001_a", "9001", "9000", "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9002_b", "9002", "9000", "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"))
    result = check_tree(tmp_path)
    assert any(f.rule == "head" and "more than one" in f.message for f in result.findings)


def test_a_merge_revision_fails(tmp_path: Path) -> None:
    chain(tmp_path, ("9000_root", "9000", None,
                     "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"))
    (tmp_path / "9001_merge.py").write_text(
        '"""A merge revision."""\n'
        "from alembic import op\n\n"
        'revision = "9001"\n'
        'down_revision = ("9000", "9000")\n'
        "branch_labels = None\ndepends_on = None\n\n\n"
        "def upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n",
        encoding="utf-8")
    result = check_tree(tmp_path)
    assert any("merge revision" in f.message for f in result.findings)


def test_missing_parent_fails(tmp_path: Path) -> None:
    migration(tmp_path, "9001_orphan", "9001", "8888",
              "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n")
    result = check_tree(tmp_path)
    assert any("does not exist" in f.message for f in result.findings)


def test_the_checker_follows_helper_functions(tmp_path: Path) -> None:
    chain(tmp_path,
          ("9000_old", "9000", None, "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9001_helper", "9001", "9000", '''def _work():
    op.create_index("ix_hidden", "messages", ["tenant_id"])


def upgrade():
    _work()


def downgrade():
    op.drop_index("ix_hidden", table_name="messages")
'''))
    result = check_tree(tmp_path)
    assert any(f.rule == "long-lock" and "ix_hidden" in f.message for f in result.findings)


def test_unknown_table_is_treated_as_growing(tmp_path: Path) -> None:
    chain(tmp_path,
          ("9000_old", "9000", None, "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9001_new", "9001", "9000", '''def upgrade():
    op.create_index("ix_unknown", "a_table_from_the_future", ["tenant_id"])


def downgrade():
    op.drop_index("ix_unknown", table_name="a_table_from_the_future")
'''))
    result = check_tree(tmp_path)
    assert any(f.rule == "long-lock" for f in result.findings)


def test_a_raw_delete_needs_a_data_note(tmp_path: Path) -> None:
    chain(tmp_path,
          ("9000_old", "9000", None, "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"),
          ("9001_delete", "9001", "9000", '''REVIEWED_SQL = "a data fix"


def upgrade():
    op.execute("DELETE FROM turns WHERE state = 'merged'")


def downgrade():
    pass
'''))
    result = check_tree(tmp_path)
    assert any(f.rule == "data" for f in result.findings)


@pytest.mark.parametrize("revision", ["0019", "0020"])
def test_the_new_revisions_pass_on_their_own(revision: str) -> None:
    result = check_tree(VERSIONS)
    assert result.ok
    assert any(r.revision == revision for r in result.revisions)

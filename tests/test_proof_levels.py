"""Proof levels (RAI-295): the rules table, events, the credit door, old labels.

Hard pass marks (CI, blocking): every row of the rules table gives its level;
no likely or estimated record reaches credit; every record is labelled after
the migration; every level change has a ``proof_events`` row.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mimi.provenance import (
    LEGACY,
    NOT_COMPUTABLE,
    POLICY_VERSION,
    PROOF_LEVELS,
    ProofError,
    add_witness,
    ask_due,
    delivery_stamp,
    legacy_level,
    parse_witnesses,
    payment_stamp,
    proof_level,
    proof_word,
    restamp,
    stamp,
    unverified_records,
    verified_records,
    witness,
)

ROOT = Path(__file__).resolve().parents[1]
AT = "2026-10-12T10:00:00+01:00"


def W(*kinds: str) -> list:
    return [witness(k, f"src-{i}") for i, k in enumerate(kinds)]


# Every row of the RAI-295 v1 table, plus the edges the acceptance criteria name.
TABLE = [
    # payment by transfer
    ("payment_transfer", ("bank_feed",), "verified"),
    ("payment_transfer", ("provider_webhook",), "verified"),
    ("payment_transfer", ("alert_screenshot_checked",), "likely"),
    ("payment_transfer", ("alert_screenshot_checked", "bank_feed"), "verified"),
    ("payment_transfer", ("shop_claim",), "estimated"),
    ("payment_transfer", ("rep_log",), "estimated"),
    ("payment_transfer", ("rep_log", "shop_claim"), "estimated"),
    ("payment_transfer", ("alert_screenshot_checked", "bank_no_credit"), "estimated"),
    # payment in cash
    ("payment_cash", ("rep_log", "shop_yes", "bank_feed"), "verified"),
    ("payment_cash", ("rep_log", "shop_yes"), "likely"),
    ("payment_cash", ("rep_log",), "estimated"),
    ("payment_cash", ("shop_claim",), "estimated"),
    ("payment_cash", ("shop_yes",), "estimated"),
    # a rep log and a distributor invoice are one witness (proposed)
    ("payment_cash", ("rep_log", "distributor_record"), "estimated"),
    # payment by POS
    ("payment_pos", ("pos_report",), "verified"),
    ("payment_pos", ("pos_slip_read",), "likely"),
    ("payment_pos", ("pos_slip_read", "pos_report"), "verified"),
    ("payment_pos", ("shop_claim",), "estimated"),
    # payment to another person's account
    ("payment_third_party", ("distributor_tag", "bank_feed"), "verified"),
    ("payment_third_party", ("bank_feed",), "likely"),
    ("payment_third_party", ("alert_screenshot_checked",), "likely"),
    ("payment_third_party", ("distributor_tag", "alert_screenshot_checked"), "likely"),
    ("payment_third_party", ("shop_claim",), "estimated"),
    # delivery
    ("delivery", ("distributor_record", "shop_yes", "payment_matched"), "verified"),
    ("delivery", ("distributor_record", "shop_yes"), "likely"),
    ("delivery", ("distributor_record",), "estimated"),
    ("delivery", ("shop_photo",), "estimated"),
    ("delivery", ("rep_log", "distributor_record"), "estimated"),
    # stock figure
    ("stock_figure", ("pos_report",), "verified"),
    ("stock_figure", ("book_read_confirmed",), "likely"),
    ("stock_figure", ("shop_count",), "estimated"),
    ("stock_figure", ("estimator",), "estimated"),
    ("stock_figure", ("pos_report", "estimator"), "estimated"),
    # sell-through between counts
    ("sell_through", ("pos_report",), "verified"),
    ("sell_through", ("pos_report", "book_read_confirmed"), "likely"),
    ("sell_through", ("book_read_confirmed", "shop_count"), "estimated"),
    ("sell_through", (), "estimated"),
    # the estimator's output is always estimated
    ("stock_estimate", ("pos_report",), "estimated"),
    ("stock_estimate", ("estimator",), "estimated"),
]


@pytest.mark.parametrize(("record_type", "kinds", "level"), TABLE)
def test_rules_table(record_type: str, kinds: tuple[str, ...], level: str) -> None:
    assert proof_level(record_type, W(*kinds)) == level


def test_every_record_type_and_level_is_covered() -> None:
    types = {t for t, _, _ in TABLE}
    from mimi.provenance import RECORD_TYPES

    assert types == set(RECORD_TYPES)
    assert {lvl for _, _, lvl in TABLE} == set(PROOF_LEVELS)


def test_unknowns_are_refused() -> None:
    with pytest.raises(ProofError):
        proof_level("loan", W("bank_feed"))
    with pytest.raises(ProofError):
        proof_level("delivery", W("distributor_record"), policy="v0")
    with pytest.raises(ProofError):
        witness("rumour", "x")
    with pytest.raises(ProofError):
        witness("bank_feed", "")


def test_nothing_is_verified_without_a_system_witness() -> None:
    """Screenshots, YES taps and claims never reach verified alone."""
    shop_side = ("shop_yes", "shop_claim", "shop_photo", "alert_screenshot_checked",
                 "pos_slip_read", "book_read_confirmed", "shop_count", "rep_log",
                 "distributor_record", "distributor_tag")
    from mimi.provenance import RECORD_TYPES

    for t in RECORD_TYPES:
        assert proof_level(t, W(*shop_side)) != "verified", t


# ------------------------------------------------------------------ acceptance criteria


def test_screenshot_then_bank_credit_upgrades_with_an_event() -> None:
    s1, e1 = add_witness(None, "payment_transfer", "pay-1",
                         witness("alert_screenshot_checked", "msg-9"), AT)
    assert s1.proof_level == "likely" and e1 is not None and e1.old_level == "none"
    s2, e2 = add_witness(s1, "payment_transfer", "pay-1", witness("bank_feed", "cr-4"), AT)
    assert s2.proof_level == "verified"
    assert e2 is not None and (e2.old_level, e2.new_level, e2.witness_kind) == (
        "likely", "verified", "bank_feed")
    # a replayed witness writes nothing
    s3, e3 = add_witness(s2, "payment_transfer", "pay-1", witness("bank_feed", "cr-4"), AT)
    assert s3 == s2 and e3 is None


def test_levels_go_down_too() -> None:
    s1, _ = add_witness(None, "payment_transfer", "pay-2",
                        witness("alert_screenshot_checked", "msg-1"), AT)
    s2, e = add_witness(s1, "payment_transfer", "pay-2",
                        witness("bank_no_credit", "window-pay-2"), AT)
    assert s2.proof_level == "estimated" and e is not None and e.old_level == "likely"


def test_delivery_upgrades_with_a_matched_payment() -> None:
    d = delivery_stamp("dlv-1", shop_yes_id="msg-2")
    assert d.proof_level == "likely"
    d2, e = add_witness(d, "delivery", "dlv-1", witness("payment_matched", "pay-3"), AT)
    assert d2.proof_level == "verified" and e is not None


def test_untagged_third_party_needs_the_tag() -> None:
    s, _ = add_witness(None, "payment_third_party", "pay-5", witness("bank_feed", "cr-1"), AT)
    assert s.proof_level == "likely"
    s2, e = add_witness(s, "payment_third_party", "pay-5",
                        witness("distributor_tag", "acct-7"), AT)
    assert s2.proof_level == "verified" and e is not None


def test_event_times_carry_their_zone() -> None:
    with pytest.raises(ProofError):
        add_witness(None, "delivery", "d", witness("distributor_record", "d"),
                    "2026-10-12T10:00:00")


def test_policy_bump_recomputes_without_editing() -> None:
    s = stamp("payment_cash", W("rep_log", "shop_yes"))
    same, ev = restamp(s, "pay-1", POLICY_VERSION, AT)
    assert same is s and ev is None
    with pytest.raises(ProofError):
        restamp(s, "pay-1", "v9", AT)


def test_stamp_columns_round_trip() -> None:
    s = payment_stamp("pay-1", "transfer", fake_alert=False, bank_credit_id="cr-1")
    cols = s.columns()
    assert set(cols) == {"proof_level", "witnesses", "proof_policy_version"}
    assert parse_witnesses(cols["witnesses"]) == s.witnesses
    assert cols["proof_policy_version"] == POLICY_VERSION


def test_book_payment_stamps() -> None:
    assert payment_stamp("p", "transfer", fake_alert=False,
                         bank_credit_id="cr").proof_level == "verified"
    # a fake alert never counts, even with a credit id passed by mistake
    assert payment_stamp("p", "transfer", fake_alert=True,
                         bank_credit_id="cr").proof_level == "estimated"
    assert payment_stamp("p", "cash_to_rep", fake_alert=False).proof_level == "estimated"
    assert payment_stamp("p", "cash_to_rep", fake_alert=False,
                         shop_yes_id="m").proof_level == "likely"
    # every stamp has at least one witness
    assert payment_stamp("p", "transfer", fake_alert=True).witnesses


def test_console_word_never_colour_alone() -> None:
    s = stamp("payment_cash", W("rep_log", "shop_yes"))
    assert s.word() == "likely (rep and shop)"
    assert proof_word("verified", W("bank_feed")) == "verified (bank)"
    assert proof_word("estimated") == "estimated"


def test_asked_once_only() -> None:
    assert ask_due("dlv-1", [])
    assert not ask_due("dlv-1", ["dlv-1"])


# ------------------------------------------------------------------ credit reads verified only


def test_verified_records_fail_closed() -> None:
    rows = [
        {"id": 1, "proof_level": "verified", "proof_policy_version": "v1"},
        {"id": 2, "proof_level": "likely", "proof_policy_version": "v1"},
        {"id": 3, "proof_level": "estimated"},
        {"id": 4},  # no level: never credit
        {"id": 5, "proof_level": "verified", "proof_policy_version": "v0"},
        {"id": 6, "proof_level": "Verified"},
        {"id": 7, "bank_confirmed": True},  # the old flag alone is not enough
    ]
    assert [r["id"] for r in verified_records(rows)] == [1]
    assert [r["id"] for r in unverified_records(rows)] == [2, 3, 4, 5, 6, 7]


def _credit_sources() -> list[Path]:
    return sorted((ROOT / "mimi" / "credit").rglob("*.py"))


def test_credit_reads_records_only_through_the_verified_door() -> None:
    """Architecture: credit code never decides on its own which payment counts.
    It may not read ``bank_confirmed`` or compare ``proof_level`` itself, may not
    touch the record tables or the stock estimates, and must call
    ``verified_records``."""
    banned_keys = {"bank_confirmed", "proof_level"}
    banned_names = {"stock_estimates", "payment_proofs", "proof_events"}
    uses_door = False
    for path in _credit_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                assert node.slice.value not in banned_keys, (path, node.slice.value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "get" and node.args \
                    and isinstance(node.args[0], ast.Constant):
                assert node.args[0].value not in banned_keys, (path, node.args[0].value)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in banned_names, (path, node.value)
            if isinstance(node, ast.Name) and node.id == "verified_records":
                uses_door = True
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("mimi.stock", "mimi.store")), path
    assert uses_door, "mimi.credit must read payments through verified_records"


def test_credit_file_never_counts_a_likely_payment() -> None:
    from mimi.credit.review import credit_file

    pays = [
        {"payment_id": "p1", "delivery_id": "d1", "day": 9, "amount_kobo": 500000,
         "claimed_kobo": 500000, "method": "transfer", "status": "matched",
         "bank_confirmed": True, "proof_level": "verified", "proof_policy_version": "v1"},
        # bank_confirmed True but only likely: must not count
        {"payment_id": "p2", "delivery_id": "d1", "day": 9, "amount_kobo": 400000,
         "claimed_kobo": 400000, "method": "transfer", "status": "matched",
         "bank_confirmed": True, "proof_level": "likely", "proof_policy_version": "v1"},
        {"payment_id": "p3", "delivery_id": "d1", "day": 9, "amount_kobo": 300000,
         "claimed_kobo": 300000, "method": "cash_to_rep", "status": "matched",
         "bank_confirmed": False, "proof_level": "estimated", "proof_policy_version": "v1"},
    ]
    hist = {"live_day": 10, "deliveries": [{"delivery_id": "d1", "day": 5, "due_day": 12,
                                            "delivered_on": "", "due_on": "", "total_kobo": 1,
                                            "lines": []}],
            "payments": pays, "promises": [], "open_disputes": 0}
    f = credit_file(hist, {"shop_id": "shop-x", "name": "Test", "credit_limit_kobo": 0}, [], {})
    assert f.verified_payments == 1 and f.verified_paid_kobo == 500000
    assert f.unverified_payments == 2


# ------------------------------------------------------------------ old labels


def test_old_labels_map_with_nothing_left_unlabelled() -> None:
    assert legacy_level("verified") == "verified"
    assert legacy_level("checked") == legacy_level("supplier_document") == "likely"
    for old in ("self_reported", "unverified", "derived"):
        assert legacy_level(old) == "estimated"
    assert legacy_level("incomplete") == NOT_COMPUTABLE
    assert set(LEGACY.values()) <= set(PROOF_LEVELS) | {NOT_COMPUTABLE}
    with pytest.raises(ProofError):
        legacy_level("gossip")


def test_provenance_is_a_leaf() -> None:
    tree = ast.parse((ROOT / "mimi" / "provenance.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("mimi"), node.module
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("mimi") for a in node.names)


# ------------------------------------------------------------------ the store and the migration


def _alembic(url: str, *args: str) -> None:
    import os
    import subprocess
    import sys

    env = {**os.environ, "MIMI_DB_URL": url, "DATABASE_URL": ""}
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, check=True,
                   capture_output=True)


def test_migration_labels_every_old_row_and_is_reversible(tmp_path: Path) -> None:
    import sqlalchemy as sa

    url = f"sqlite:///{tmp_path / 'old.db'}"
    _alembic(url, "upgrade", "0009")
    e = sa.create_engine(url)
    with e.begin() as c:
        c.execute(sa.text(
            "INSERT INTO payments (tenant_id, payment_id, shop_id, delivery_id, day, paid_on, "
            "amount_kobo, method, reference, status, partial, fake_alert) VALUES "
            "('t','p-bank','s1','d-1',1,'2026-10-01',100,'transfer','r','matched',0,0),"
            "('t','p-cash','s1','d-2',1,'2026-10-01',100,'cash_to_rep','','matched',0,0),"
            "('t','p-fake','s1','d-2',1,'2026-10-01',100,'transfer','r2','unmatched',0,1)"))
        c.execute(sa.text(
            "INSERT INTO bank_feed (tenant_id, credit_id, payment_id, amount_kobo, received_at, "
            "narration) VALUES ('t','cr-1','p-bank',100,'2026-10-01T10:00:00+01:00','x'),"
            "('t','cr-2','p-fake',100,'2026-10-01T10:00:00+01:00','x')"))
        c.execute(sa.text(
            "INSERT INTO deliveries (tenant_id, delivery_id, shop_id, rep_id, day, delivered_on, "
            "minute, total_kobo, status, witnessed_at, live) VALUES "
            "('t','d-1','s1','r',1,'2026-10-01',0,100,'delivered','2026-10-01T11:00:00+01:00',0),"
            "('t','d-2','s1','r',1,'2026-10-01',0,100,'delivered',NULL,0),"
            "('t','d-3','s1','r',1,'2026-10-01',0,100,'delivered','2026-10-01T11:00:00+01:00',0)"))
        for pid, level, credit in (("m-v", "verified", "cr-9"), ("m-l", "likely", ""),
                                   ("m-r", "review", ""), ("m-a", "ask", "")):
            c.execute(sa.text(
                "INSERT INTO payment_proofs (proof_id, tenant_id, shop_id, sender, received_at, "
                "image_sha256, image_bytes, reading_json, read_via, level, bank_credit_id) "
                "VALUES (:p,'t','s1','','2026-10-01T10:00:00+01:00','x',0,'{}','seed',:l,:c)"),
                {"p": pid, "l": level, "c": credit})
    _alembic(url, "upgrade", "head")
    with e.connect() as c:
        got = {}
        for table, key in (("payments", "payment_id"), ("deliveries", "delivery_id"),
                           ("payment_proofs", "proof_id")):
            for r in c.execute(sa.text(
                    f"SELECT {key}, proof_level, witnesses, proof_policy_version FROM {table}")):
                assert r[1] in PROOF_LEVELS and r[3] == "v1" and parse_witnesses(r[2]), r
                got[r[0]] = r[1]
    assert got == {"p-bank": "verified", "p-cash": "estimated", "p-fake": "estimated",
                   "d-1": "verified", "d-2": "estimated", "d-3": "likely",
                   "m-v": "verified", "m-l": "likely", "m-r": "estimated", "m-a": "estimated"}
    _alembic(url, "downgrade", "0009")
    cols = {c["name"] for c in sa.inspect(sa.create_engine(url)).get_columns("payments")}
    assert "proof_level" not in cols
    assert "proof_events" not in sa.inspect(sa.create_engine(url)).get_table_names()


def _book(tmp_path: Path):  # type: ignore[no-untyped-def]
    from mimi.store import ensure_schema, make_engine, seed_world
    from mimi.store.proofbook import SqlProofBook
    from mimi.world.loader import load_world

    e = make_engine(f"sqlite:///{tmp_path / 'book.db'}")
    ensure_schema(e)
    w = load_world()
    seed_world(e, w)
    return e, w, SqlProofBook(e)


def test_seeded_book_is_fully_labelled_and_credit_sees_verified_only(tmp_path: Path) -> None:
    import sqlalchemy as sa

    from mimi.store import SqlGo
    from mimi.store import schema as s

    e, w, book = _book(tmp_path)
    with e.connect() as c:
        for t in (s.payments, s.deliveries):
            rows = c.execute(sa.select(t.c.proof_level, t.c.witnesses,
                                       t.c.proof_policy_version)).all()
            assert rows and all(r[0] in PROOF_LEVELS and parse_witnesses(r[1]) and r[2] == "v1"
                                for r in rows)
    counts = book.level_counts(w.tenant_id)
    assert counts["payment"]["verified"] == len({c.payment_id for c in w.bank_feed} - {
        p.payment_id for p in w.payments if p.fake_alert})
    # credit's read over the store: every counted payment is verified
    go = SqlGo(e)
    for shop in w.shops[:10]:
        hist = go.credit_history(w.tenant_id, shop.shop_id)
        for p in verified_records(hist["payments"]):
            assert p["bank_confirmed"] and p["proof_level"] == "verified"
        assert all(p["proof_level"] != "verified" for p in unverified_records(hist["payments"]))


def test_store_witness_upgrade_writes_one_event(tmp_path: Path) -> None:
    e, w, book = _book(tmp_path)
    cash = next(p for p in w.payments if p.method == "cash_to_rep")
    tid = w.tenant_id
    assert book.stamp(tid, "payment", cash.payment_id).proof_level == "estimated"
    st = book.add_witness(tid, "payment", cash.payment_id, witness("shop_yes", "wamid.7"), AT)
    assert st.proof_level == "likely"
    again = book.add_witness(tid, "payment", cash.payment_id, witness("shop_yes", "wamid.7"), AT)
    assert again == st
    st = book.add_witness(tid, "payment", cash.payment_id, witness("bank_feed", "cr-cash"),
                          "2026-10-13T09:00:00+01:00")
    assert st.proof_level == "verified"
    evs = book.events(tid, cash.payment_id)
    assert [(x.old_level, x.new_level) for x in evs] == [("estimated", "likely"),
                                                         ("likely", "verified")]
    d = w.deliveries[0]
    st = book.add_witness(tid, "delivery", d.delivery_id, witness("shop_yes", "wamid.8"), AT)
    assert st.proof_level == "likely" and len(book.events(tid, d.delivery_id)) == 1
    with pytest.raises(KeyError):
        book.add_witness(tid, "payment", "nope", witness("shop_yes", "x"), AT)


def test_store_asks_a_shop_once_only(tmp_path: Path) -> None:
    e, w, book = _book(tmp_path)
    d = w.deliveries[0].delivery_id
    assert book.ask_once(w.tenant_id, "delivery", d, AT)
    assert not book.ask_once(w.tenant_id, "delivery", d, AT)
    # the skipped ask did not lower the level
    assert book.stamp(w.tenant_id, "delivery", d).proof_level == "estimated"

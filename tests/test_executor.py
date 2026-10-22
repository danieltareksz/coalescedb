"""Tests for the Executor (PROJECT_SPEC.md §6.6, §11.1, M5 row of §15).

Busy handling and BEGIN IMMEDIATE retries are in test_executor_busy.py; canaries and the
layers holding with the guard bypassed are in tests/security/test_executor_canary.py.

Audit `detail` keys used as the contract here: "sql", "kind", "row_count", "elapsed_ms",
"reasons", "sql_length", "error", "statement_index", "counts", "tables".
"""

import dataclasses
import json
import sqlite3

import pytest

from coalescedb.db.executor import Executor
from coalescedb.errors import (
    ExecutionError,
    PermissionDenied,
    QueryTimeout,
    RowConflict,
    SQLRejected,
)
from coalescedb.models import GuardResult, Role, Session, StatementKind
from coalescedb.security.sql_guard import VIEWER_MESSAGE, validate

USER_PASSWORD = "alice-pass-22"
EXECUTOR_ACTIONS = {
    "query",
    "query_rejected",
    "query_failed",
    "query_busy",
    "export",
    "ingest",
    "sql_import",
    "live_import",
}
HEAVY_SELECT = (
    "WITH RECURSIVE s(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM s WHERE i < 50000000) "
    "SELECT count(*) FROM s"
)


# --- helpers ---------------------------------------------------------------------------


def make_items(path, rows=3):
    """items(id, n, label) with n = 0..rows-1, built with plain sqlite3."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, n INTEGER NOT NULL, label TEXT)"
    )
    conn.executemany("INSERT INTO items (n) VALUES (?)", [(n,) for n in range(rows)])
    conn.commit()
    conn.close()


def query(path, sql, params=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def item_values(path):
    return [row[0] for row in query(path, "SELECT n FROM items ORDER BY id")]


def object_names(path):
    return {row[0] for row in query(path, "SELECT name FROM sqlite_master")}


def snapshots(settings, db_name="sales"):
    folder = settings.backups_dir / db_name
    return sorted(folder.iterdir()) if folder.exists() else []


def entries(auth, superadmin, action=None):
    """The Executor's audit entries for "sales", oldest first."""
    found = [
        entry
        for entry in auth.read_audit(superadmin, db_name="sales")
        if entry["action"] in EXECUTOR_ACTIONS
    ]
    found.reverse()
    return [entry for entry in found if action is None or entry["action"] == action]


# --- fixtures --------------------------------------------------------------------------


@pytest.fixture
def make_executor(settings, registry, auth, backups):
    def make(**overrides):
        return Executor(dataclasses.replace(settings, **overrides), registry, auth, backups)

    return make


@pytest.fixture
def executor(make_executor):
    return make_executor()


@pytest.fixture
def db(registry, superadmin):
    path = registry.create(superadmin, "sales")
    make_items(path)
    return path


@pytest.fixture
def admin(superadmin):
    return Session(user=superadmin, db_name="sales", role=Role.ADMIN)


@pytest.fixture
def alice(auth, superadmin):
    return auth.create_user(superadmin, "alice_1", USER_PASSWORD)


@pytest.fixture
def viewer(auth, superadmin, alice, db):
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)
    return Session(user=alice, db_name="sales", role=Role.VIEWER)


# --- execute: SELECT and the row cap (§6.6 steps 5-6) ----------------------------------


def test_execute_select_returns_columns_and_rows(executor, db, admin):
    result = executor.execute(admin, "SELECT id, n FROM items ORDER BY id", source="manual_sql")
    assert result.columns == ["id", "n"]
    assert result.rows == [(1, 0), (2, 1), (3, 2)]
    assert result.row_count == 3
    assert result.truncated is False
    assert result.elapsed_ms >= 0


def test_viewer_can_select(executor, db, viewer):
    result = executor.execute(viewer, "SELECT n FROM items ORDER BY n", source="manual_sql")
    assert result.rows == [(0,), (1,), (2,)]


def test_max_rows_caps_and_sets_truncated(executor, db, admin):
    make_items_more(db, 2)  # 5 rows in all
    result = executor.execute(admin, "SELECT n FROM items", source="manual_sql", max_rows=2)
    assert len(result.rows) == 2
    assert result.row_count == 2
    assert result.truncated is True


def test_exactly_the_cap_is_not_truncated(executor, db, admin):
    result = executor.execute(admin, "SELECT n FROM items", source="manual_sql", max_rows=3)
    assert len(result.rows) == 3
    assert result.truncated is False


def test_default_cap_is_max_result_rows(make_executor, db, admin):
    executor = make_executor(max_result_rows=2)
    result = executor.execute(admin, "SELECT n FROM items", source="manual_sql")
    assert len(result.rows) == 2
    assert result.truncated is True


def test_cap_never_exceeds_max_export_rows(make_executor, db, admin):
    executor = make_executor(max_export_rows=2)
    result = executor.execute(admin, "SELECT n FROM items", source="export", max_rows=100)
    assert len(result.rows) == 2
    assert result.truncated is True


@pytest.mark.parametrize("bad", [0, -1])
def test_max_rows_below_one_is_refused(executor, db, admin, bad):
    with pytest.raises(ValueError):
        executor.execute(admin, "SELECT n FROM items", source="manual_sql", max_rows=bad)


def make_items_more(path, extra):
    conn = sqlite3.connect(path)
    conn.executemany("INSERT INTO items (n) VALUES (?)", [(100 + n,) for n in range(extra)])
    conn.commit()
    conn.close()


# --- max_sql_chars through the Executor (§6.2 step 1, §6.6 step 2, M5 acceptance) -----


def padded(sql, length):
    assert len(sql) <= length
    return sql + " " * (length - len(sql))


def test_sql_exactly_at_max_sql_chars_runs(make_executor, db, admin):
    executor = make_executor(max_sql_chars=40)
    sql = padded("SELECT n FROM items WHERE n = 1", 40)
    assert executor.execute(admin, sql, source="manual_sql").rows == [(1,)]


def test_sql_one_over_max_sql_chars_is_rejected_for_its_size(make_executor, db, admin):
    executor = make_executor(max_sql_chars=40)
    sql = padded("SELECT n FROM items WHERE n = 1", 41)
    with pytest.raises(SQLRejected) as excinfo:
        executor.execute(admin, sql, source="manual_sql")
    # The size reason, not a parse error: the same SQL at 40 characters runs.
    assert len(excinfo.value.reasons) == 1
    assert "too long" in excinfo.value.reasons[0]
    assert "limit 40" in excinfo.value.reasons[0]


# --- role re-resolved on every call (§6.6 step 1, M5 acceptance) -----------------------


def test_revoked_grant_blocks_the_next_execute(executor, auth, superadmin, alice, db, viewer):
    executor.execute(viewer, "SELECT n FROM items", source="manual_sql")
    auth.revoke(superadmin, alice.id, "sales")
    with pytest.raises(PermissionDenied):
        executor.execute(viewer, "SELECT n FROM items", source="manual_sql")


def test_admin_downgraded_to_viewer_cannot_write_on_the_next_execute(
    executor, auth, superadmin, alice, db
):
    auth.grant(superadmin, alice.id, "sales", Role.ADMIN)
    session = Session(user=alice, db_name="sales", role=Role.ADMIN)
    insert = "INSERT INTO items (n) VALUES (?)"
    executor.execute(session, insert, source="manual_sql", params=(10,), confirmed=True)
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)
    with pytest.raises(SQLRejected) as excinfo:
        executor.execute(session, insert, source="manual_sql", params=(11,), confirmed=True)
    assert VIEWER_MESSAGE in excinfo.value.reasons  # refused by the role, nothing else
    assert item_values(db) == [0, 1, 2, 10]


def test_session_role_is_never_trusted(executor, db, viewer):
    forged = dataclasses.replace(viewer, role=Role.ADMIN)
    with pytest.raises(SQLRejected) as excinfo:
        executor.execute(
            forged, "DELETE FROM items WHERE id = 1", source="manual_sql", confirmed=True
        )
    assert VIEWER_MESSAGE in excinfo.value.reasons
    assert item_values(db) == [0, 1, 2]


def test_user_without_a_grant_is_refused(executor, db, alice):
    session = Session(user=alice, db_name="sales", role=Role.VIEWER)
    with pytest.raises(PermissionDenied):
        executor.execute(session, "SELECT n FROM items", source="manual_sql")


def test_no_database_selected_is_refused(executor, db, superadmin):
    session = Session(user=superadmin, db_name=None, role=None)
    with pytest.raises(PermissionDenied):
        executor.execute(session, "SELECT 1", source="manual_sql")


# --- confirmation and destructive writes (§6.6 steps 3-4, M5 acceptance) --------------


def test_write_without_confirmation_is_refused_and_runs_nothing(executor, db, admin):
    with pytest.raises(PermissionDenied):
        executor.execute(admin, "INSERT INTO items (n) VALUES (?)", source="manual_sql",
                         params=(9,))
    assert item_values(db) == [0, 1, 2]


def test_destructive_delete_without_the_typed_name_is_refused(executor, settings, db, admin):
    for text in (None, "Sales", "sales "):
        with pytest.raises(PermissionDenied):
            executor.execute(admin, "DELETE FROM items", source="manual_sql", confirmed=True,
                             destructive_confirm_text=text)
    assert item_values(db) == [0, 1, 2]
    assert snapshots(settings) == []


def test_destructive_delete_takes_exactly_one_snapshot_first(executor, settings, db, admin):
    result = executor.execute(admin, "DELETE FROM items", source="manual_sql", confirmed=True,
                              destructive_confirm_text="sales")
    assert result.row_count == 3
    assert item_values(db) == []
    (snapshot,) = snapshots(settings)
    assert snapshot.name.endswith("_destructive_query.db")
    assert [row[0] for row in query(snapshot, "SELECT n FROM items ORDER BY id")] == [0, 1, 2]


def test_ordinary_write_takes_no_snapshot(executor, settings, db, admin):
    executor.execute(admin, "UPDATE items SET n = ? WHERE id = ?", source="manual_sql",
                     params=(5, 1), confirmed=True)
    assert snapshots(settings) == []


# --- values only as bound parameters; transactions (§6.6 steps 5, 7) -------------------


def test_parameter_values_are_stored_literally(executor, db, admin):
    tricky = "x'); DROP TABLE items; --"
    executor.execute(admin, "INSERT INTO items (n, label) VALUES (?, ?)", source="manual_sql",
                     params=(7, tricky), confirmed=True)
    assert query(db, "SELECT label FROM items WHERE n = 7") == [(tricky,)]


def test_failed_write_leaves_no_partial_change(executor, db, admin):
    # The second row breaks NOT NULL; the first must not stay.
    with pytest.raises(ExecutionError) as excinfo:
        executor.execute(admin, "INSERT INTO items (n) VALUES (?), (?)", source="manual_sql",
                         params=(8, None), confirmed=True)
    assert "NOT NULL" in excinfo.value.sqlite_message
    assert item_values(db) == [0, 1, 2]


def test_update_reports_rows_changed(executor, db, admin):
    result = executor.execute(admin, "UPDATE items SET label = ? WHERE n >= ?",
                              source="manual_sql", params=("big", 1), confirmed=True)
    assert result.row_count == 2
    assert result.rows == []


def test_ddl_row_count_is_zero(executor, db, admin):
    result = executor.execute(admin, "CREATE TABLE extra (a INTEGER)", source="designer",
                              confirmed=True)
    assert result.row_count == 0
    assert result.columns == []
    assert result.rows == []
    assert "extra" in object_names(db)


def test_returning_rows_are_capped_but_every_row_is_written(executor, db, admin):
    result = executor.execute(
        admin, "INSERT INTO items (n) VALUES (?), (?), (?) RETURNING n", source="manual_sql",
        params=(20, 21, 22), confirmed=True, max_rows=2,
    )
    assert len(result.rows) == 2
    assert result.truncated is True
    assert result.row_count == 3
    assert item_values(db) == [0, 1, 2, 20, 21, 22]


def test_missing_table_is_an_execution_error(executor, db, admin):
    with pytest.raises(ExecutionError) as excinfo:
        executor.execute(admin, "SELECT * FROM nope", source="manual_sql")
    assert "no such table" in excinfo.value.sqlite_message


def test_long_query_times_out(make_executor, db, admin):
    executor = make_executor(query_timeout_s=0.05)
    with pytest.raises(QueryTimeout):
        executor.execute(admin, HEAVY_SELECT, source="manual_sql")


# --- audit (§6.6 step 8 and "One audit entry per call") --------------------------------


def test_successful_call_writes_one_query_entry(executor, auth, superadmin, db, admin):
    executor.execute(admin, "insert into items (n) values (?)", source="manual_sql",
                     params=(31,), confirmed=True)
    (entry,) = entries(auth, superadmin)
    assert entry["action"] == "query"
    assert entry["source"] == "manual_sql"
    assert entry["user_id"] == admin.user.id
    assert entry["db_name"] == "sales"
    detail = entry["detail"]
    assert detail["kind"] == StatementKind.INSERT.value
    assert detail["row_count"] == 1
    assert detail["elapsed_ms"] >= 0
    expected = validate("insert into items (n) values (?)", Role.ADMIN, {"items"})
    assert detail["sql"] == expected.normalized_sql  # the text that actually ran
    assert "busy_retries_used" not in detail


@pytest.mark.parametrize(
    ("source", "action"),
    [
        ("export", "export"),
        ("ingest", "ingest"),
        ("sql_import", "sql_import"),
        ("live_import", "live_import"),
        ("manual_sql", "query"),
        ("nl", "query"),
        ("builder", "query"),
        ("editor", "query"),
        ("designer", "query"),
        ("analyze", "query"),
        ("system", "query"),
    ],
)
def test_audit_action_follows_the_source(executor, auth, superadmin, db, admin, source, action):
    executor.execute(admin, "SELECT n FROM items", source=source)
    (entry,) = entries(auth, superadmin)
    assert entry["action"] == action
    assert entry["source"] == source


def test_guard_rejection_is_audited(executor, auth, superadmin, db, admin):
    with pytest.raises(SQLRejected):
        executor.execute(admin, "PRAGMA table_info(items)", source="manual_sql")
    (entry,) = entries(auth, superadmin)
    assert entry["action"] == "query_rejected"
    assert entry["detail"]["reasons"]
    assert entry["detail"]["sql"] == "PRAGMA table_info(items)"


def test_oversized_sql_is_audited_cut_to_max_sql_chars(make_executor, auth, superadmin, db,
                                                       admin):
    executor = make_executor(max_sql_chars=40)
    sql = "SELECT n FROM items WHERE n = 1" + " OR n = 2" * 20
    with pytest.raises(SQLRejected):
        executor.execute(admin, sql, source="manual_sql")
    (entry,) = entries(auth, superadmin)
    assert entry["detail"]["sql"] == sql[:40]
    assert entry["detail"]["sql_length"] == len(sql)


def test_every_refusal_is_one_query_rejected_entry(executor, auth, superadmin, alice, db, admin):
    no_grant = Session(user=alice, db_name="sales", role=Role.ADMIN)
    refusals = [
        lambda: executor.execute(no_grant, "SELECT 1", source="manual_sql"),
        lambda: executor.execute(admin, "INSERT INTO items (n) VALUES (1)",
                                 source="manual_sql"),
        lambda: executor.execute(admin, "DELETE FROM items", source="manual_sql",
                                 confirmed=True, destructive_confirm_text="wrong"),
    ]
    for refuse in refusals:
        with pytest.raises(PermissionDenied):
            refuse()
    rejected = entries(auth, superadmin)
    assert [entry["action"] for entry in rejected] == ["query_rejected"] * 3
    assert all(entry["detail"]["reasons"] for entry in rejected)


def test_failure_after_start_is_query_failed_with_the_class_name_only(executor, auth,
                                                                      superadmin, db, admin):
    with pytest.raises(ExecutionError):
        executor.execute(admin, "INSERT INTO items (n) VALUES (?)", source="manual_sql",
                         params=(None,), confirmed=True)
    (entry,) = entries(auth, superadmin)
    assert entry["action"] == "query_failed"
    assert entry["detail"]["error"] == "ExecutionError"
    assert "NOT NULL" not in json.dumps(entry)  # SQLite's message is never stored


def test_timeout_is_query_failed(make_executor, auth, superadmin, db, admin):
    executor = make_executor(query_timeout_s=0.05)
    with pytest.raises(QueryTimeout):
        executor.execute(admin, HEAVY_SELECT, source="manual_sql")
    (entry,) = entries(auth, superadmin)
    assert entry["action"] == "query_failed"
    assert entry["detail"]["error"] == "QueryTimeout"


def test_committed_write_is_never_reported_as_not_done(executor, auth, db, admin, monkeypatch):
    def broken_audit(*args, **kwargs):
        raise sqlite3.OperationalError("app.db unavailable")

    monkeypatch.setattr(auth, "audit", broken_audit)
    with pytest.raises(ExecutionError) as excinfo:
        executor.execute(admin, "INSERT INTO items (n) VALUES (?)", source="manual_sql",
                         params=(41,), confirmed=True)
    assert "was saved" in excinfo.value.user_message
    assert item_values(db) == [0, 1, 2, 41]


# --- execute_many (§6.6) ---------------------------------------------------------------


def test_execute_many_inserts_every_batch_in_one_call(executor, auth, superadmin, db, admin):
    rows = ((n,) for n in range(1000, 3500))
    count = executor.execute_many(admin, "INSERT INTO items (n) VALUES (?)", rows,
                                  source="ingest", confirmed=True, batch_size=1000)
    assert count == 2500
    assert len(item_values(db)) == 2503
    (entry,) = entries(auth, superadmin)
    assert entry["action"] == "ingest"
    assert entry["detail"]["row_count"] == 2500


def test_execute_many_failure_in_a_later_batch_saves_nothing(executor, db, admin):
    def rows():
        for n in range(2500):
            yield (None,) if n == 2100 else (n,)

    with pytest.raises(ExecutionError):
        executor.execute_many(admin, "INSERT INTO items (n) VALUES (?)", rows(),
                              source="ingest", confirmed=True, batch_size=1000)
    assert item_values(db) == [0, 1, 2]


def test_execute_many_needs_confirmation(executor, db, admin):
    with pytest.raises(PermissionDenied):
        executor.execute_many(admin, "INSERT INTO items (n) VALUES (?)", [(1,)],
                              source="ingest", confirmed=False)
    assert item_values(db) == [0, 1, 2]


def test_execute_many_refuses_a_viewer(executor, db, viewer):
    with pytest.raises(SQLRejected):
        executor.execute_many(viewer, "INSERT INTO items (n) VALUES (?)", [(1,)],
                              source="ingest", confirmed=True)
    assert item_values(db) == [0, 1, 2]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO items (n) VALUES (:x)",
        "INSERT INTO items (n) VALUES (1)",
        "INSERT INTO items (n) VALUES (?), (?)",
        "INSERT INTO items (n) VALUES (? + 1)",
        "INSERT INTO items (n) SELECT ?",
        "UPDATE items SET n = ? WHERE id = 1",
        "SELECT ?",
    ],
)
def test_execute_many_refuses_any_other_shape_before_reading_rows(executor, db, admin, sql):
    consumed = []

    def rows():
        consumed.append(True)
        yield (1,)

    with pytest.raises(SQLRejected):
        executor.execute_many(admin, sql, rows(), source="ingest", confirmed=True)
    assert consumed == []
    assert item_values(db) == [0, 1, 2]


# --- apply_schema (§6.6) ---------------------------------------------------------------


SCHEMA = [
    "CREATE TABLE a (x INTEGER)",
    "CREATE INDEX a_x ON a (x)",
    "CREATE VIEW av AS SELECT x FROM a",
]


def test_apply_schema_commits_all_statements(executor, db, admin):
    executor.apply_schema(admin, SCHEMA, source="designer", confirmed=True)
    assert {"a", "a_x", "av"} <= object_names(db)


def test_apply_schema_one_guard_rejection_rejects_all(executor, auth, superadmin, db, admin):
    with pytest.raises(SQLRejected):
        executor.apply_schema(admin, [*SCHEMA[:2], "CREATE TABLE _app_x (a INTEGER)"],
                              source="designer", confirmed=True)
    assert not {"a", "a_x"} & object_names(db)
    (entry,) = entries(auth, superadmin)
    assert entry["action"] == "query_rejected"


@pytest.mark.parametrize(
    "extra",
    ["DROP TABLE items", "ALTER TABLE items DROP COLUMN label", "INSERT INTO items (n) VALUES (1)",
     "SELECT 1"],
)
def test_apply_schema_refuses_destructive_and_non_ddl_statements(executor, db, admin, extra):
    with pytest.raises(SQLRejected):
        executor.apply_schema(admin, ["CREATE TABLE b (x INTEGER)", extra], source="designer",
                              confirmed=True)
    assert "b" not in object_names(db)
    assert item_values(db) == [0, 1, 2]


def test_apply_schema_accepts_a_non_destructive_alter(executor, db, admin):
    executor.apply_schema(admin, ["ALTER TABLE items ADD COLUMN extra TEXT"],
                          source="designer", confirmed=True)
    assert "extra" in [row[1] for row in query(db, "PRAGMA table_info(items)")]


def test_apply_schema_failure_inside_the_transaction_rolls_back(executor, db, admin):
    with pytest.raises(ExecutionError):
        executor.apply_schema(admin, ["CREATE TABLE c (x INTEGER)", "CREATE TABLE items (n)"],
                              source="designer", confirmed=True)
    assert "c" not in object_names(db)


def test_apply_schema_snapshots_a_database_that_has_tables(executor, settings, db, admin):
    executor.apply_schema(admin, SCHEMA, source="designer", confirmed=True)
    (snapshot,) = snapshots(settings)
    assert snapshot.name.endswith("_schema_change.db")


def test_apply_schema_takes_no_snapshot_of_an_empty_database(executor, settings, registry,
                                                             superadmin):
    registry.create(superadmin, "empty")
    session = Session(user=superadmin, db_name="empty", role=Role.ADMIN)
    executor.apply_schema(session, SCHEMA, source="ingest", confirmed=True)
    assert snapshots(settings, "empty") == []


def test_apply_schema_refuses_a_viewer_and_a_missing_confirmation(executor, db, admin, viewer):
    with pytest.raises(SQLRejected):
        executor.apply_schema(viewer, SCHEMA, source="designer", confirmed=True)
    with pytest.raises(PermissionDenied):
        executor.apply_schema(admin, SCHEMA, source="designer", confirmed=False)
    assert "a" not in object_names(db)


# --- apply_changes (§6.6, §11.1 paragraph) ---------------------------------------------


def test_apply_changes_commits_several_statements_together(executor, db, admin):
    changed = executor.apply_changes(
        admin,
        [("UPDATE items SET n = ? WHERE id = ?", (10, 1)),
         ("INSERT INTO items (n) VALUES (?)", (99,)),
         ("DELETE FROM items WHERE id = ?", (3,))],
        source="editor", confirmed=True,
    )
    assert changed == 3
    assert item_values(db) == [10, 1, 99]


def test_apply_changes_failure_in_the_last_statement_rolls_back_the_first(executor, db, admin):
    with pytest.raises(ExecutionError):
        executor.apply_changes(
            admin,
            [("UPDATE items SET n = ? WHERE id = ?", (10, 1)),
             ("INSERT INTO items (n) VALUES (?)", (None,))],
            source="editor", confirmed=True,
        )
    assert item_values(db) == [0, 1, 2]


def test_apply_changes_refuses_a_viewer(executor, db, viewer):
    with pytest.raises(SQLRejected):
        executor.apply_changes(viewer, [("UPDATE items SET n = ? WHERE id = ?", (10, 1))],
                               source="editor", confirmed=True)
    assert item_values(db) == [0, 1, 2]


def test_apply_changes_needs_confirmation(executor, db, admin):
    with pytest.raises(PermissionDenied):
        executor.apply_changes(admin, [("UPDATE items SET n = ? WHERE id = ?", (10, 1))],
                               source="editor", confirmed=False)
    assert item_values(db) == [0, 1, 2]


@pytest.mark.parametrize(
    "bad",
    [
        ("CREATE TABLE z (a INTEGER)", ()),
        ("SELECT n FROM items", ()),
        ("UPDATE items SET n = ?", (5,)),
        ("DELETE FROM items", ()),
    ],
)
def test_apply_changes_rejects_the_whole_batch_before_anything_runs(executor, db, admin, bad):
    with pytest.raises(SQLRejected):
        executor.apply_changes(
            admin, [("UPDATE items SET n = ? WHERE id = ?", (10, 1)), bad],
            source="editor", confirmed=True,
        )
    assert item_values(db) == [0, 1, 2]
    assert "z" not in object_names(db)


def test_update_matching_no_row_is_a_conflict_and_changes_nothing(executor, auth, superadmin,
                                                                  db, admin):
    with pytest.raises(RowConflict) as excinfo:
        executor.apply_changes(
            admin,
            [("UPDATE items SET n = ? WHERE id = ?", (10, 1)),
             ("UPDATE items SET n = ? WHERE id = ? AND n = ?", (11, 2, 999))],
            source="editor", confirmed=True,
        )
    assert excinfo.value.statement_index == 1
    assert item_values(db) == [0, 1, 2]
    (entry,) = entries(auth, superadmin)
    assert entry["action"] == "query_failed"
    assert entry["detail"]["error"] == "RowConflict"
    assert entry["detail"]["statement_index"] == 1


def test_update_matching_two_rows_is_a_conflict(executor, db, admin):
    with pytest.raises(RowConflict) as excinfo:
        executor.apply_changes(admin, [("UPDATE items SET n = ? WHERE n >= ?", (7, 1))],
                               source="editor", confirmed=True)
    assert excinfo.value.statement_index == 0
    assert item_values(db) == [0, 1, 2]


def test_delete_matching_no_row_is_a_conflict(executor, db, admin):
    with pytest.raises(RowConflict) as excinfo:
        executor.apply_changes(admin, [("DELETE FROM items WHERE id = ?", (42,))],
                               source="editor", confirmed=True)
    assert excinfo.value.statement_index == 0
    assert item_values(db) == [0, 1, 2]


def test_apply_changes_with_a_delete_snapshots_first(executor, settings, db, admin):
    executor.apply_changes(admin, [("DELETE FROM items WHERE id = ?", (1,))],
                           source="editor", confirmed=True)
    (snapshot,) = snapshots(settings)
    assert snapshot.name.endswith("_editor_delete.db")
    assert [row[0] for row in query(snapshot, "SELECT n FROM items ORDER BY id")] == [0, 1, 2]


def test_apply_changes_without_a_delete_takes_no_snapshot(executor, settings, db, admin):
    executor.apply_changes(admin, [("UPDATE items SET n = ? WHERE id = ?", (10, 1))],
                           source="editor", confirmed=True)
    assert snapshots(settings) == []


def test_apply_changes_writes_one_audit_entry_without_values(executor, auth, superadmin, db,
                                                             admin):
    executor.apply_changes(
        admin,
        [("UPDATE items SET label = ? WHERE id = ?", ("VALUE-CANARY-1", 1)),
         ("INSERT INTO items (n, label) VALUES (?, ?)", (77, "VALUE-CANARY-2"))],
        source="editor", confirmed=True,
    )
    (entry,) = entries(auth, superadmin)
    assert entry["action"] == "query"
    assert entry["source"] == "editor"
    assert entry["detail"]["counts"] == {"update": 1, "insert": 1}
    assert entry["detail"]["tables"] == ["items"]
    assert "VALUE-CANARY" not in json.dumps(entry)


# --- preview (§6.6) --------------------------------------------------------------------


def test_preview_returns_the_guard_result_and_audits_nothing(executor, auth, superadmin, db,
                                                             admin):
    result = executor.preview(admin, "DELETE FROM items")
    assert isinstance(result, GuardResult)
    assert result.allowed is True
    assert result.is_destructive is True
    assert item_values(db) == [0, 1, 2]
    assert entries(auth, superadmin) == []


def test_preview_knows_views_and_still_warns_about_unknown_tables(executor, db, admin):
    conn = sqlite3.connect(db)
    conn.execute("CREATE VIEW v AS SELECT n FROM items")
    conn.close()
    assert executor.preview(admin, "SELECT * FROM v").reasons == []
    assert executor.preview(admin, "SELECT * FROM nope").reasons != []


def test_preview_uses_the_role_from_the_grant(executor, db, viewer, alice):
    forged = dataclasses.replace(viewer, role=Role.ADMIN)
    assert executor.preview(forged, "DELETE FROM items WHERE id = 1").allowed is False
    stranger = Session(user=alice, db_name="other", role=Role.ADMIN)
    with pytest.raises(PermissionDenied):
        executor.preview(stranger, "SELECT 1")


# --- dry_run (§6.6, §6.11) -------------------------------------------------------------


def test_dry_run_reports_a_missing_table(executor, db, admin):
    with pytest.raises(ExecutionError) as excinfo:
        executor.dry_run(admin, "SELECT * FROM nope")
    assert "no such table" in excinfo.value.sqlite_message


def test_dry_run_reports_a_missing_column(executor, db, viewer):
    with pytest.raises(ExecutionError):
        executor.dry_run(viewer, "SELECT nope FROM items")


def test_dry_run_does_not_execute_a_write(executor, auth, superadmin, db, admin):
    assert executor.dry_run(admin, "DELETE FROM items WHERE id = 1") is None
    assert executor.dry_run(admin, "INSERT INTO items (n) VALUES (5)") is None
    assert item_values(db) == [0, 1, 2]
    assert entries(auth, superadmin) == []  # a successful dry run is not audited


def test_dry_run_with_placeholders_is_an_execution_error(executor, db, admin):
    with pytest.raises(ExecutionError):
        executor.dry_run(admin, "SELECT n FROM items WHERE id = ?")


def test_dry_run_guard_rejection_is_audited(executor, auth, superadmin, db, viewer):
    with pytest.raises(SQLRejected):
        executor.dry_run(viewer, "DELETE FROM items WHERE id = 1")
    (entry,) = entries(auth, superadmin)
    assert entry["action"] == "query_rejected"

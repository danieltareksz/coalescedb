"""Security tests for role-scoped connections and the SQLite authorizer (PROJECT_SPEC.md §6.4).

These tests bypass the SQL guard entirely: forbidden statements are executed directly on
viewer and admin connections, and SQLite itself must refuse them. That proves this layer
works on its own, whatever happens above it.

Not covered here: `ALTER TABLE t RENAME TO _app_x`. SQLite never tells the authorizer a
rename's new name, so that case belongs to the guard (§6.2 step 7, test_sql_guard.py, M4).
"""

import sqlite3
import time

import pytest

from coalescedb.db.connection import open_connection, open_internal_connection
from coalescedb.errors import DatabaseNotFound, QueryTimeout
from coalescedb.models import Role
from coalescedb.security.policy import FORBIDDEN_FUNCTIONS

SETUP = """
CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);
INSERT INTO t (name) VALUES ('a'), ('b');
CREATE TABLE parent (id INTEGER PRIMARY KEY);
CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent (id));
INSERT INTO parent (id) VALUES (1);
CREATE INDEX idx_t_name ON t (name);
CREATE TABLE _app_meta (k TEXT);
INSERT INTO _app_meta (k) VALUES ('secret');
CREATE TABLE "_APP_Upper" (a);
CREATE VIEW v_names AS SELECT name FROM t;
"""

ENDLESS_QUERY = (
    "WITH RECURSIVE c (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) SELECT count(*) FROM c"
)

BOTH_ROLES = [Role.VIEWER, Role.ADMIN]


@pytest.fixture
def db_path(tmp_path):
    """A small database built with plain sqlite3, without any project code."""
    path = tmp_path / "work.db"
    conn = sqlite3.connect(path)
    conn.executescript(SETUP)
    conn.commit()
    conn.close()
    return path


def state(path):
    """Schema and data as seen by a plain connection: used to prove nothing changed."""
    conn = sqlite3.connect(path)
    try:
        schema = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        rows = conn.execute("SELECT id, name FROM t ORDER BY id").fetchall()
        app_rows = conn.execute("SELECT k FROM _app_meta").fetchall()
        return schema, rows, app_rows
    finally:
        conn.close()


def journal_mode(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()


# --- FORBIDDEN_FUNCTIONS ---------------------------------------------------------------


def test_forbidden_functions_is_a_lowercase_frozenset():
    assert isinstance(FORBIDDEN_FUNCTIONS, frozenset)
    assert FORBIDDEN_FUNCTIONS == {
        "load_extension",
        "readfile",
        "writefile",
        "edit",
        "fts3_tokenizer",
        "sqlite_compileoption_get",
    }
    assert all(name == name.lower() for name in FORBIDDEN_FUNCTIONS)


@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize("name", sorted(FORBIDDEN_FUNCTIONS))
def test_forbidden_functions_are_refused_in_any_letter_case(db_path, role, name):
    with open_connection(db_path, role, 5.0) as conn:
        # Make sure the function really exists on this connection, so the only thing that
        # can stop it is the authorizer (several of these aren't built into stock SQLite).
        conn.create_function(name, -1, lambda *args: "ran")
        for spelling in (name, name.upper(), name.title()):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                conn.execute("SELECT " + spelling + "('x')").fetchall()


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_ordinary_functions_are_allowed(db_path, role):
    with open_connection(db_path, role, 5.0) as conn:
        conn.create_function("harmless_fn", -1, lambda *args: "ran")
        assert conn.execute("SELECT harmless_fn('x')").fetchall() == [("ran",)]
        assert conn.execute("SELECT upper(name) FROM t ORDER BY id").fetchall() == [
            ("A",),
            ("B",),
        ]


# --- Viewer ----------------------------------------------------------------------------

VIEWER_REFUSED = [
    "INSERT INTO t (name) VALUES ('x')",
    "UPDATE t SET name = 'x'",
    "DELETE FROM t",
    "CREATE TABLE t2 (a)",
    "DROP TABLE t",
    "ALTER TABLE t ADD COLUMN c",
    "ALTER TABLE t RENAME TO t9",
    "CREATE INDEX i ON t (name)",
    "DROP INDEX idx_t_name",
    "CREATE VIEW v2 AS SELECT 1",
    "DROP VIEW v_names",
    "CREATE TRIGGER trg AFTER INSERT ON t BEGIN SELECT 1; END",
    "CREATE TEMP TABLE tt (a)",
    "CREATE VIRTUAL TABLE vt USING fts5(a)",
    "ATTACH DATABASE ':memory:' AS x",
    "PRAGMA table_info(t)",
    "PRAGMA journal_mode = DELETE",
    "PRAGMA query_only = OFF",
    "PRAGMA writable_schema = 1",
    "SELECT * FROM pragma_table_info('t')",
    "BEGIN",
    "SAVEPOINT s",
    "VACUUM",
    "ANALYZE",
    "INSERT INTO _app_meta (k) VALUES ('x')",
]


@pytest.mark.parametrize("sql", VIEWER_REFUSED)
def test_viewer_is_refused(db_path, sql):
    before = state(db_path)
    with open_connection(db_path, Role.VIEWER, 5.0) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql).fetchall()
    assert state(db_path) == before


def test_viewer_can_read(db_path):
    with open_connection(db_path, Role.VIEWER, 5.0) as conn:
        assert conn.execute("SELECT id, name FROM t ORDER BY id").fetchall() == [
            (1, "a"),
            (2, "b"),
        ]
        assert conn.execute("SELECT name FROM t WHERE id = ?", (2,)).fetchall() == [("b",)]
        assert conn.execute("SELECT name FROM v_names ORDER BY name").fetchall() == [
            ("a",),
            ("b",),
        ]
        joined = conn.execute(
            "SELECT p.id, count(c.id) FROM parent AS p "
            "LEFT JOIN child AS c ON c.parent_id = p.id GROUP BY p.id"
        ).fetchall()
        assert joined == [(1, 0)]
        recursive = conn.execute(
            "WITH RECURSIVE c (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 5) "
            "SELECT sum(n) FROM c"
        ).fetchall()
        assert recursive == [(15,)]
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        assert ("t",) in tables


def test_viewer_writes_still_fail_with_the_authorizer_removed(db_path):
    """The layers are independent: the read-only file handle refuses writes on its own."""
    before = state(db_path)
    with open_connection(db_path, Role.VIEWER, 5.0) as conn:
        conn.set_authorizer(None)  # pretend the authorizer layer has failed
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t (name) VALUES ('x')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE t2 (a)")
        conn.execute("PRAGMA query_only = OFF")  # ...and now query_only has failed too
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t (name) VALUES ('x')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DROP TABLE t")
    assert state(db_path) == before


def test_viewer_does_not_change_the_journal_mode(db_path):
    with open_connection(db_path, Role.VIEWER, 5.0) as conn:
        conn.execute("SELECT 1").fetchall()
    assert journal_mode(db_path) == "delete"


def test_viewer_can_read_a_database_left_in_wal_mode_by_an_admin(db_path):
    with open_connection(db_path, Role.ADMIN, 5.0) as conn:
        conn.execute("INSERT INTO t (name) VALUES ('c')")
    assert journal_mode(db_path) == "wal"
    with open_connection(db_path, Role.VIEWER, 5.0) as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchall() == [(3,)]


# --- Admin -----------------------------------------------------------------------------

ADMIN_REFUSED = [
    "ATTACH DATABASE ':memory:' AS x",
    "DETACH DATABASE main",
    "PRAGMA writable_schema = 1",
    "PRAGMA foreign_keys = OFF",
    "PRAGMA journal_mode = DELETE",
    "PRAGMA table_info(t)",
    "SELECT * FROM pragma_table_info('t')",
    "CREATE TRIGGER trg AFTER INSERT ON t BEGIN SELECT 1; END",
    "CREATE TEMP TABLE tt (a)",
    "CREATE TEMPORARY TABLE tt (a)",
    "CREATE TEMP VIEW tv AS SELECT 1",
    "CREATE TEMP TRIGGER ttr AFTER INSERT ON t BEGIN SELECT 1; END",
    "CREATE VIRTUAL TABLE vt USING fts5(a)",
    # Any write to an _app_* table, whatever the letter case.
    "INSERT INTO _app_meta (k) VALUES ('x')",
    "INSERT INTO _APP_META (k) VALUES ('x')",
    "UPDATE _app_meta SET k = 'x'",
    "DELETE FROM _app_meta",
    "DROP TABLE _app_meta",
    "CREATE TABLE _app_new (a)",
    "CREATE TABLE _APP_New (a)",
    "CREATE TABLE _app_copy AS SELECT * FROM t",
    "CREATE INDEX idx_app ON _app_meta (k)",
    "ALTER TABLE _app_meta ADD COLUMN x",
    "ALTER TABLE _app_meta RENAME TO t2",
    "ALTER TABLE _app_meta RENAME COLUMN k TO k2",
    "INSERT INTO _app_upper (a) VALUES (1)",
    'DROP TABLE "_APP_Upper"',
]


@pytest.mark.parametrize("sql", ADMIN_REFUSED)
def test_admin_is_refused(db_path, sql):
    with open_connection(db_path, Role.ADMIN, 5.0) as conn:
        conn.execute("SELECT 1").fetchall()
    before = state(db_path)  # taken after an admin connection has switched to WAL
    with open_connection(db_path, Role.ADMIN, 5.0) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql).fetchall()
    assert state(db_path) == before


def test_admin_schema_changes_succeed(db_path):
    """The authorizer must not block normal admin work (§6.4)."""
    with open_connection(db_path, Role.ADMIN, 5.0) as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, total REAL)")
        conn.execute("CREATE INDEX idx_orders_total ON orders (total)")
        conn.execute("ALTER TABLE orders ADD COLUMN note TEXT")
        conn.execute("INSERT INTO orders (total, note) VALUES (?, ?)", (9.5, "first"))
        conn.execute("UPDATE orders SET total = ? WHERE id = ?", (10.0, 1))
        assert conn.execute("SELECT total, note FROM orders").fetchall() == [(10.0, "first")]
        conn.execute("ALTER TABLE orders RENAME TO purchases")
        conn.execute("CREATE VIEW v_purchases AS SELECT id FROM purchases")
        assert conn.execute("SELECT id FROM v_purchases").fetchall() == [(1,)]
        conn.execute("DELETE FROM purchases WHERE id = ?", (1,))
        conn.execute("DROP VIEW v_purchases")
        conn.execute("DROP INDEX idx_orders_total")
        conn.execute("DROP TABLE purchases")
        conn.execute("DROP TABLE child")

    plain = sqlite3.connect(db_path)
    try:
        names = {row[0] for row in plain.execute("SELECT name FROM sqlite_master")}
    finally:
        plain.close()
    assert not {"orders", "purchases", "v_purchases", "idx_orders_total", "child"} & names
    assert "t" in names


def test_admin_writes_are_saved_without_an_explicit_commit(db_path):
    with open_connection(db_path, Role.ADMIN, 5.0) as conn:
        conn.execute("CREATE TABLE kept (a)")
        conn.execute("INSERT INTO kept (a) VALUES (?)", (42,))
    plain = sqlite3.connect(db_path)
    try:
        assert plain.execute("SELECT a FROM kept").fetchall() == [(42,)]
    finally:
        plain.close()


def test_admin_can_use_explicit_transactions(db_path):
    with open_connection(db_path, Role.ADMIN, 5.0) as conn:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO t (name) VALUES ('rolled back')")
        conn.execute("ROLLBACK")
        conn.execute("BEGIN")
        conn.execute("INSERT INTO t (name) VALUES ('kept')")
        conn.execute("COMMIT")
    assert state(db_path)[1] == [(1, "a"), (2, "b"), (3, "kept")]


def test_admin_connection_enforces_foreign_keys(db_path):
    with open_connection(db_path, Role.ADMIN, 5.0) as conn:
        conn.execute("INSERT INTO child (parent_id) VALUES (?)", (1,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO child (parent_id) VALUES (?)", (999,))


def test_admin_connection_uses_wal(db_path):
    assert journal_mode(db_path) == "delete"
    with open_connection(db_path, Role.ADMIN, 5.0) as conn:
        conn.execute("SELECT 1").fetchall()
    assert journal_mode(db_path) == "wal"


# --- Both roles ------------------------------------------------------------------------


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_endless_query_times_out(db_path, role):
    start = time.monotonic()
    with pytest.raises(QueryTimeout):
        with open_connection(db_path, role, 0.3) as conn:
            conn.execute(ENDLESS_QUERY).fetchall()
    assert time.monotonic() - start < 5.0


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_other_sqlite_errors_are_not_turned_into_timeouts(db_path, role):
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        with open_connection(db_path, role, 5.0) as conn:
            conn.execute("SELECT * FROM no_such_table")


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_connection_is_closed_on_exit(db_path, role):
    with open_connection(db_path, role, 5.0) as conn:
        conn.execute("SELECT 1").fetchall()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_opening_a_missing_file_raises_and_creates_nothing(tmp_path, role):
    missing = tmp_path / "missing.db"
    with pytest.raises(DatabaseNotFound):
        with open_connection(missing, role, 5.0):
            pass
    assert not missing.exists()


# --- Internal connection (introspection only; never given user SQL) --------------------


def test_internal_connection_allows_the_read_only_pragmas(db_path):
    with open_internal_connection(db_path) as conn:
        columns = conn.execute("PRAGMA table_info(t)").fetchall()
        assert [row[1] for row in columns] == ["id", "name"]
        assert conn.execute("PRAGMA foreign_key_list(child)").fetchall()[0][2] == "parent"
        assert conn.execute("PRAGMA index_list(t)").fetchall()[0][1] == "idx_t_name"
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        bound = conn.execute("SELECT name FROM pragma_table_info(?)", ("t",)).fetchall()
        assert bound == [("id",), ("name",)]
        assert conn.execute("SELECT count(*) FROM t").fetchall() == [(2,)]


@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA journal_mode = DELETE",
        "PRAGMA writable_schema = 1",
        "PRAGMA query_only = OFF",
        "PRAGMA foreign_keys = OFF",
        "INSERT INTO t (name) VALUES ('x')",
        "DELETE FROM t",
        "CREATE TABLE t2 (a)",
        "DROP TABLE t",
        "ATTACH DATABASE ':memory:' AS x",
        "BEGIN",
    ],
)
def test_internal_connection_refuses_everything_else(db_path, sql):
    before = state(db_path)
    with open_internal_connection(db_path) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql).fetchall()
    assert state(db_path) == before


def test_internal_connection_is_read_only_even_without_the_authorizer(db_path):
    before = state(db_path)
    with open_internal_connection(db_path) as conn:
        conn.set_authorizer(None)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t (name) VALUES ('x')")
    assert state(db_path) == before


def test_internal_connection_refuses_forbidden_functions(db_path):
    with open_internal_connection(db_path) as conn:
        conn.create_function("readfile", -1, lambda *args: "ran")
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute("SELECT READFILE('x')").fetchall()


def test_internal_connection_to_a_missing_file_raises(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(DatabaseNotFound):
        with open_internal_connection(missing):
            pass
    assert not missing.exists()


# --- Views named like _app_ tables -----------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE VIEW _app_v AS SELECT 1",
        "CREATE VIEW _APP_v AS SELECT 1",
        'CREATE VIEW "_App_V" AS SELECT name FROM t',
    ],
)
def test_admin_cannot_create_a_view_with_an_app_name(db_path, sql):
    with open_connection(db_path, Role.ADMIN, 5.0) as conn:
        conn.execute("SELECT 1").fetchall()
    before = state(db_path)
    with open_connection(db_path, Role.ADMIN, 5.0) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute(sql)
    assert state(db_path) == before


@pytest.mark.parametrize("sql", ["DROP VIEW _app_existing", "DROP VIEW _APP_EXISTING"])
def test_admin_cannot_drop_a_view_with_an_app_name(db_path, sql):
    plain = sqlite3.connect(db_path)
    plain.execute("CREATE VIEW _app_existing AS SELECT name FROM t")
    plain.commit()
    plain.close()
    with open_connection(db_path, Role.ADMIN, 5.0) as conn:
        conn.execute("SELECT 1").fetchall()
    before = state(db_path)
    with open_connection(db_path, Role.ADMIN, 5.0) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute(sql)
    assert state(db_path) == before


# --- The temp schema, reached with the "temp." prefix instead of the TEMP keyword -------


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE temp.tt (a)",
        "CREATE TABLE TEMP.tt (a)",
        "CREATE TABLE Temp.tt AS SELECT * FROM t",
        "CREATE VIEW temp.v AS SELECT 1",
        "CREATE VIEW TEMP.v AS SELECT name FROM t",
    ],
)
def test_admin_cannot_create_objects_in_the_temp_schema(db_path, sql):
    with open_connection(db_path, Role.ADMIN, 5.0) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute(sql)
        leftovers = conn.execute("SELECT name FROM sqlite_temp_master").fetchall()
        assert leftovers == []


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX temp.ti ON tt (a)",
        "CREATE INDEX ti ON tt (a)",  # an index on a temp table lives in temp too
        "ALTER TABLE temp.tt ADD COLUMN b",
        "ALTER TABLE temp.tt RENAME TO tt2",
        "DROP TABLE temp.tt",
        "DROP TABLE TEMP.tt",
    ],
)
def test_admin_cannot_change_an_existing_temp_table(db_path, sql):
    from coalescedb.db.connection import _admin_authorizer

    with open_connection(db_path, Role.ADMIN, 5.0) as conn:
        # Put a temp table there with the authorizer switched off, then switch it back on:
        # the only way to have one to test against, since creating it is refused.
        conn.set_authorizer(None)
        conn.execute("CREATE TEMP TABLE tt (a)")
        conn.set_authorizer(_admin_authorizer)
        before = conn.execute("SELECT type, name, sql FROM sqlite_temp_master").fetchall()

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute(sql)
        assert conn.execute("SELECT type, name, sql FROM sqlite_temp_master").fetchall() == before


# --- Views that call a forbidden function ----------------------------------------------

VIEW_FUNCTIONS = {"v_load": "load_extension", "v_read": "readfile"}


@pytest.fixture
def db_with_bad_views(db_path):
    """As an imported file might be: views whose bodies call forbidden functions."""
    plain = sqlite3.connect(db_path)
    plain.execute("CREATE VIEW v_load AS SELECT load_extension('x') AS result")
    plain.execute("CREATE VIEW v_read AS SELECT readfile('x') AS result")
    plain.commit()
    plain.close()
    return db_path


@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize("view", sorted(VIEW_FUNCTIONS))
def test_selecting_from_a_view_that_calls_a_forbidden_function_is_refused(
    db_with_bad_views, role, view
):
    with open_connection(db_with_bad_views, role, 5.0) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT * FROM " + view).fetchall()


@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize("view", sorted(VIEW_FUNCTIONS))
def test_a_forbidden_function_inside_a_view_never_runs(db_with_bad_views, role, view):
    calls = []

    def record(*args):
        calls.append(args)
        return "ran"

    with open_connection(db_with_bad_views, role, 5.0) as conn:
        # Make sure the function exists on this connection, so "no such function" can't be
        # the reason the query fails.
        conn.create_function(VIEW_FUNCTIONS[view], -1, record)
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT * FROM " + view).fetchall()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT count(*) FROM t, " + view).fetchall()
    assert calls == []

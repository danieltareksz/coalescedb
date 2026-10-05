"""Red-team corpus for the SQL guard (PROJECT_SPEC.md §6.2, §11.1).

The guard is layer 3 of 5: it parses SQL with sqlglot and accepts a statement only if
everything in it is on an allowlist. These tests check outcomes only (allowed, kind,
is_destructive, reasons, normalized_sql), never sqlglot class names, so they stay valid
when sqlglot renames a class.

Every SQL string here was parsed with the pinned sqlglot (30.21.0) before the tests were
written. Where a string is stopped by an earlier step than the rule it is listed under,
a comment says so.
"""

import dataclasses
import inspect
import sqlite3

import pytest

from coalescedb.config import Settings
from coalescedb.models import GuardResult, Role, StatementKind
from coalescedb.security import policy, sql_guard
from coalescedb.security.policy import FORBIDDEN_FUNCTIONS, ROLE_PERMISSIONS
from coalescedb.security.sql_guard import validate

BOTH_ROLES = [Role.VIEWER, Role.ADMIN]

KNOWN_TABLES = {"t", "Order", "parent", "child", "my_app_data"}

VIEWER_MESSAGE = "Viewer accounts can only run SELECT queries"

ALLOW = "allow"
REJECT = "reject"
DESTRUCTIVE = "allow, destructive"

# A real trigger always has a ";" inside its body, so sqlglot sees two statements and
# step 3 (exactly one statement) rejects it before step 4 looks at the kind.
REAL_TRIGGER = "CREATE TRIGGER trg AFTER INSERT ON t BEGIN DELETE FROM t; END"

# Valid SQL, about 20 KB: only its length is wrong.
TWENTY_KB = "SELECT " + ", ".join(["1"] * 7000)

LIKE_QUERY = 'SELECT "id", "name" FROM "t" WHERE "name" LIKE ? ESCAPE \'\\\' ORDER BY "id" LIMIT ?'

SETUP = """
CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, note TEXT);
INSERT INTO t (name) VALUES ('100%'), ('a_b'), ('a\\b'), ('axb'), ('100x');
"""


def check(sql, role, **kwargs):
    result = validate(sql, role, KNOWN_TABLES, **kwargs)
    assert isinstance(result, GuardResult)
    return result


def assert_rejected(result):
    assert result.allowed is False
    assert result.reasons, "a rejection must say why"
    assert all(isinstance(reason, str) and reason for reason in result.reasons)
    # Rejected SQL has no runnable form, so nothing can execute it by mistake (§6.2).
    assert result.normalized_sql is None


def assert_allowed(result):
    assert result.allowed is True, result.reasons
    assert isinstance(result.kind, StatementKind)
    assert isinstance(result.normalized_sql, str)
    assert result.normalized_sql.strip()


@pytest.fixture
def db(tmp_path):
    """A small database built with plain sqlite3, to run the guard's re-rendered SQL on."""
    conn = sqlite3.connect(tmp_path / "work.db")
    conn.executescript(SETUP)
    conn.commit()
    yield conn
    conn.close()


def schema_of(conn, name):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return None if row is None else row[0]


# --- §11.1 table: every row, both roles --------------------------------------------------

# (SQL, viewer outcome, admin outcome), in the order of the table in §11.1.
TABLE_ROWS = [
    ("SELECT * FROM t", ALLOW, ALLOW),
    ("WITH x AS (SELECT 1) SELECT * FROM x", ALLOW, ALLOW),
    ("SELECT * FROM t WHERE name = 'a;b'", ALLOW, ALLOW),
    ("SELECT 1; DROP TABLE t", REJECT, REJECT),
    ("DROP TABLE t", REJECT, DESTRUCTIVE),
    ("DELETE FROM t", REJECT, DESTRUCTIVE),
    ("DELETE FROM t WHERE id = 1", REJECT, ALLOW),
    ("ATTACH DATABASE 'x.db' AS x", REJECT, REJECT),
    ("PRAGMA writable_schema = 1", REJECT, REJECT),
    ("SELECT load_extension('x')", REJECT, REJECT),
    (REAL_TRIGGER, REJECT, REJECT),
    ("INSERT INTO _app_meta VALUES (1)", REJECT, REJECT),
    ("UPDATE sqlite_master SET sql = ''", REJECT, REJECT),
    ("BEGIN", REJECT, REJECT),
    ("BEGIN; SELECT 1; COMMIT", REJECT, REJECT),
    ("COMMIT", REJECT, REJECT),
    ("VACUUM INTO '/tmp/x.db'", REJECT, REJECT),
    ("", REJECT, REJECT),
    ("   \n\t ", REJECT, REJECT),
    ("-- only a comment", REJECT, REJECT),
    ("/* only a comment */", REJECT, REJECT),
    (";", REJECT, REJECT),
    (TWENTY_KB, REJECT, REJECT),
    ("CREATE TABLE t (a INTEGER)", REJECT, ALLOW),
    ("CREATE TEMP TABLE t (a)", REJECT, REJECT),
    ("CREATE TEMPORARY TABLE t (a)", REJECT, REJECT),
    ("CREATE VIRTUAL TABLE t USING fts5(a)", REJECT, REJECT),
    # sqlglot drops STRICT when it re-renders, so the table would be created without it.
    ("CREATE TABLE t (a INTEGER) STRICT", REJECT, REJECT),
    # sqlglot cannot parse WITHOUT ROWID and falls back to its "unparsed statement" node,
    # which step 4 rejects. No form of WITHOUT ROWID reaches the extra CREATE checks.
    ("CREATE TABLE t (a INTEGER PRIMARY KEY) WITHOUT ROWID", REJECT, REJECT),
    ("CREATE TABLE t (strict_mode INTEGER)", REJECT, ALLOW),
    ('SELECT * FROM "Order" WHERE "CustomerId" = 1', ALLOW, ALLOW),
    ("ALTER TABLE t RENAME TO _app_x", REJECT, REJECT),
]


def row_id(row):
    sql = row[0]
    return repr(sql if len(sql) <= 60 else sql[:57] + "...")


@pytest.mark.parametrize(("sql", "viewer", "admin"), TABLE_ROWS, ids=map(row_id, TABLE_ROWS))
def test_guard_table_viewer(sql, viewer, admin):
    result = check(sql, Role.VIEWER)
    if viewer == REJECT:
        assert_rejected(result)
        if admin != REJECT:
            # Refused only because of the role: the reason must say so (§6.2 step 5).
            assert VIEWER_MESSAGE in result.reasons
    else:
        assert_allowed(result)
        assert result.kind is StatementKind.SELECT
        assert result.is_destructive is False


@pytest.mark.parametrize(("sql", "viewer", "admin"), TABLE_ROWS, ids=map(row_id, TABLE_ROWS))
def test_guard_table_admin(sql, viewer, admin):
    result = check(sql, Role.ADMIN)
    if admin == REJECT:
        assert_rejected(result)
    else:
        assert_allowed(result)
        assert result.is_destructive is (admin == DESTRUCTIVE)
        assert VIEWER_MESSAGE not in result.reasons


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_twenty_kb_is_refused_for_its_size_only(role):
    assert len(TWENTY_KB) >= 20_000
    assert_rejected(check(TWENTY_KB, role))
    assert_allowed(check(TWENTY_KB, role, max_sql_chars=100_000))


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_keywords_as_column_names_are_not_mistaken_for_table_options(role):
    # TEMP, STRICT and WITHOUT ROWID are found by keyword position, never by substring.
    sql = "CREATE TABLE t (strict INTEGER, temp TEXT, rowid_copy INTEGER, virtual_x TEXT)"
    result = check(sql, role)
    if role is Role.ADMIN:
        assert_allowed(result)
        assert result.kind is StatementKind.CREATE_TABLE
    else:
        assert_rejected(result)


# --- SQL the query builder produces (§6.27) ----------------------------------------------


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_like_escape_and_placeholders_survive_the_re_render(role):
    result = check(LIKE_QUERY, role)
    assert_allowed(result)
    assert result.kind is StatementKind.SELECT
    assert result.is_destructive is False
    assert "LIKE ? ESCAPE '\\'" in result.normalized_sql
    assert "LIMIT ?" in result.normalized_sql
    assert result.normalized_sql.count("?") == LIKE_QUERY.count("?")


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (("%\\%%", 10), [(1, "100%")]),  # contains a literal %
        (("%\\_%", 10), [(2, "a_b")]),  # contains a literal _
        (("%\\\\%", 10), [(3, "a\\b")]),  # contains a literal backslash
        (("a%", 1), [(2, "a_b")]),  # starts with a, LIMIT ? = 1
    ],
)
def test_re_rendered_like_query_returns_the_same_rows(db, params, expected):
    result = check(LIKE_QUERY, Role.VIEWER)
    assert_allowed(result)
    assert db.execute(LIKE_QUERY, params).fetchall() == expected
    assert db.execute(result.normalized_sql, params).fetchall() == expected


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_every_builder_operator_keeps_its_placeholders_in_order(role):
    sql = (
        'SELECT "t"."id" FROM "t" WHERE "t"."name" LIKE ? ESCAPE \'\\\' '
        'AND "t"."id" BETWEEN ? AND ? AND "t"."id" IN (?, ?) AND "t"."note" <> ? LIMIT ?'
    )
    result = check(sql, role)
    assert_allowed(result)
    assert result.normalized_sql.count("?") == 7
    for fragment in ("LIKE ? ESCAPE '\\'", "BETWEEN ? AND ?", "IN (?, ?)", "<> ?", "LIMIT ?"):
        assert fragment in result.normalized_sql


def test_is_placeholder_is_not_rewritten_to_equals():
    # The data editor matches NULL originals with IS ? (§6.28).
    result = check('DELETE FROM "t" WHERE "id" = ? AND "name" IS ?', Role.ADMIN)
    assert_allowed(result)
    assert "IS ?" in result.normalized_sql
    assert result.normalized_sql.count("?") == 2


# --- SQL the table designer produces (§6.29) ---------------------------------------------

RENAME_COLUMN = 'ALTER TABLE "t" RENAME COLUMN "name" TO "title"'
DROP_COLUMN = 'ALTER TABLE "t" DROP COLUMN "note"'
RENAME_TABLE = 'ALTER TABLE "t" RENAME TO "t2"'
ADD_COLUMN = "ALTER TABLE \"t\" ADD COLUMN \"extra\" TEXT NOT NULL DEFAULT 'it''s'"

# (SQL, destructive for the guard). §6.2 step 9: DROP COLUMN and both renames are.
ALTER_STATEMENTS = [
    (RENAME_COLUMN, True),
    (DROP_COLUMN, True),
    (RENAME_TABLE, True),
    (ADD_COLUMN, False),
    ("ALTER TABLE t RENAME COLUMN name TO title", True),
    ("ALTER TABLE t DROP COLUMN note", True),
    ("ALTER TABLE t RENAME TO t2", True),
    ("ALTER TABLE t ADD COLUMN extra TEXT", False),
]


@pytest.mark.parametrize(("sql", "destructive"), ALTER_STATEMENTS)
def test_alter_table_is_admin_only_and_classified(sql, destructive):
    result = check(sql, Role.ADMIN)
    assert_allowed(result)
    assert result.kind is StatementKind.ALTER_TABLE
    assert result.is_destructive is destructive

    viewer = check(sql, Role.VIEWER)
    assert_rejected(viewer)
    assert VIEWER_MESSAGE in viewer.reasons


def test_re_rendered_rename_column_does_what_was_asked(db):
    db.execute(check(RENAME_COLUMN, Role.ADMIN).normalized_sql)
    columns = [row[1] for row in db.execute("PRAGMA table_info(t)")]
    assert columns == ["id", "title", "note"]
    assert db.execute("SELECT title FROM t WHERE id = 1").fetchall() == [("100%",)]


def test_re_rendered_drop_column_does_what_was_asked(db):
    db.execute(check(DROP_COLUMN, Role.ADMIN).normalized_sql)
    columns = [row[1] for row in db.execute("PRAGMA table_info(t)")]
    assert columns == ["id", "name"]
    assert db.execute("SELECT count(*) FROM t").fetchall() == [(5,)]


def test_re_rendered_rename_table_does_what_was_asked(db):
    db.execute(check(RENAME_TABLE, Role.ADMIN).normalized_sql)
    assert schema_of(db, "t") is None
    assert db.execute("SELECT count(*) FROM t2").fetchall() == [(5,)]


def test_re_rendered_add_column_keeps_the_escaped_default(db):
    db.execute(check(ADD_COLUMN, Role.ADMIN).normalized_sql)
    assert db.execute("SELECT extra FROM t WHERE id = 1").fetchall() == [("it's",)]


# The authorizer never sees a rename's new name (§6.4), so only the guard can stop these.
@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE t RENAME TO _app_x",
        'ALTER TABLE "t" RENAME TO "_app_x"',
        'ALTER TABLE t RENAME TO "_APP_X"',
        "ALTER TABLE t RENAME TO sqlite_x",
        'ALTER TABLE t RENAME TO "SQLITE_X"',
    ],
)
def test_rename_to_a_reserved_name_is_rejected(role, sql):
    assert_rejected(check(sql, role))


# --- Step 1: size ------------------------------------------------------------------------


def sql_of_length(length):
    sql = "SELECT '" + "a" * (length - len("SELECT ''")) + "'"
    assert len(sql) == length
    return sql


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_size_limit_boundary(role):
    limit = 50
    assert_allowed(check(sql_of_length(limit), role, max_sql_chars=limit))
    assert_rejected(check(sql_of_length(limit + 1), role, max_sql_chars=limit))
    # The same string is fine under the default limit: only its length was wrong.
    assert_allowed(check(sql_of_length(limit + 1), role))


def test_size_limit_default_is_the_settings_default():
    parameter = inspect.signature(validate).parameters["max_sql_chars"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    (field,) = (f for f in dataclasses.fields(Settings) if f.name == "max_sql_chars")
    assert parameter.default == field.default


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_default_size_limit_boundary(role):
    (field,) = (f for f in dataclasses.fields(Settings) if f.name == "max_sql_chars")
    assert_allowed(check(sql_of_length(field.default), role))
    assert_rejected(check(sql_of_length(field.default + 1), role))


# --- Steps 2 and 3: parse, exactly one statement -----------------------------------------


@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize("sql", ["SELECT 1;", "SELECT 1;;", "SELECT * FROM t ;\n"])
def test_trailing_semicolon_is_one_statement(role, sql):
    result = check(sql, role)
    assert_allowed(result)
    assert result.kind is StatementKind.SELECT


@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT * FROM t; DELETE FROM t",
        "DELETE FROM t WHERE id = 1; DELETE FROM t",
        "SELECT * FROM (DELETE FROM t RETURNING id)",  # not SQL at all: a parse error
        "SELECT * FROM",
        "SELEKT * FROM t",
    ],
)
def test_several_statements_and_unparsable_sql_are_rejected(role, sql):
    assert_rejected(check(sql, role))


# --- Step 4: statement allowlist ---------------------------------------------------------

# (SQL, kind) for statements an admin may run.
ADMIN_STATEMENTS = [
    ("SELECT * FROM t", StatementKind.SELECT),
    ("SELECT id FROM t UNION SELECT id FROM parent", StatementKind.SELECT),
    ("SELECT id FROM t UNION ALL SELECT id FROM parent", StatementKind.SELECT),
    ("SELECT id FROM t INTERSECT SELECT id FROM parent", StatementKind.SELECT),
    ("SELECT id FROM t EXCEPT SELECT id FROM parent", StatementKind.SELECT),
    ("INSERT INTO t (name) VALUES (?)", StatementKind.INSERT),
    ("INSERT INTO t (name) SELECT name FROM t", StatementKind.INSERT),
    ("UPDATE t SET name = ? WHERE id = ?", StatementKind.UPDATE),
    ("DELETE FROM t WHERE id = ?", StatementKind.DELETE),
    ("CREATE TABLE new_t (a INTEGER)", StatementKind.CREATE_TABLE),
    ("CREATE TABLE IF NOT EXISTS new_t (a INTEGER)", StatementKind.CREATE_TABLE),
    ("CREATE INDEX idx_t_name ON t (name)", StatementKind.CREATE_INDEX),
    ("CREATE UNIQUE INDEX idx_t_name ON t (name)", StatementKind.CREATE_INDEX),
    ("CREATE VIEW v AS SELECT name FROM t", StatementKind.CREATE_VIEW),
    ("ALTER TABLE t ADD COLUMN extra TEXT", StatementKind.ALTER_TABLE),
    ("DROP TABLE t", StatementKind.DROP),
    ("DROP TABLE IF EXISTS t", StatementKind.DROP),
    ("DROP INDEX idx_t_name", StatementKind.DROP),
    ("DROP VIEW v", StatementKind.DROP),
]


@pytest.mark.parametrize(("sql", "kind"), ADMIN_STATEMENTS)
def test_statement_kinds_and_who_may_run_them(sql, kind):
    result = check(sql, Role.ADMIN)
    assert_allowed(result)
    assert result.kind is kind

    viewer = check(sql, Role.VIEWER)
    if kind is StatementKind.SELECT:
        assert_allowed(viewer)
        assert viewer.kind is StatementKind.SELECT
    else:
        assert_rejected(viewer)
        assert VIEWER_MESSAGE in viewer.reasons


@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "DETACH DATABASE x",
        "REINDEX",
        "REINDEX t",
        "ROLLBACK",
        "SET x = 1",
        "VACUUM",
        "ANALYZE",
        "EXPLAIN SELECT * FROM t",
        "PRAGMA table_info(t)",
        "DROP TRIGGER trg",
        "CREATE TEMP VIEW v AS SELECT name FROM t",
        # A trigger body always contains ";", so a real CREATE TRIGGER stops at step 3 and
        # this ";"-less form stops as an unparsed statement. sqlglot 30.21.0 never yields a
        # CREATE of kind TRIGGER, so the "any other kind" rule is tested with SEQUENCE,
        # which is not SQLite syntax but is one statement that parses as a CREATE.
        "CREATE TRIGGER trg AFTER INSERT ON t BEGIN SELECT 1 END",
        "CREATE SEQUENCE s",
    ],
)
def test_statements_outside_the_allowlist_are_rejected(role, sql):
    assert_rejected(check(sql, role))


# --- Step 5: role permissions ------------------------------------------------------------


def test_role_permissions():
    assert set(ROLE_PERMISSIONS) == {Role.VIEWER, Role.ADMIN}
    assert all(isinstance(kinds, frozenset) for kinds in ROLE_PERMISSIONS.values())
    assert ROLE_PERMISSIONS[Role.VIEWER] == {StatementKind.SELECT}
    assert ROLE_PERMISSIONS[Role.ADMIN] == set(StatementKind)


# --- Step 6: forbidden functions and writes inside a SELECT ------------------------------


def test_guard_and_authorizer_share_one_forbidden_set():
    assert sql_guard.FORBIDDEN_FUNCTIONS is policy.FORBIDDEN_FUNCTIONS


@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize("name", sorted(FORBIDDEN_FUNCTIONS))
def test_forbidden_functions_are_rejected_in_any_letter_case(role, name):
    for spelling in (name, name.upper(), name.title()):
        assert_rejected(check("SELECT " + spelling + "('x')", role))


# Each of these is a SELECT a viewer could otherwise run, so only step 6 can stop it.
@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t WHERE name = readfile('x')",
        "SELECT (SELECT load_extension('x'))",
        "WITH x AS (SELECT readfile('f') AS a) SELECT a FROM x",
        "SELECT id FROM t UNION SELECT writefile('x', 'y')",
        "SELECT upper(edit(name)) FROM t",
    ],
)
def test_forbidden_functions_are_found_anywhere_in_a_select(role, sql):
    assert_rejected(check(sql, role))


# Admin only: for a viewer these writes are already refused by the role check (step 5).
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t (name) VALUES (load_extension('x'))",
        "INSERT INTO t (name) VALUES (readfile('x'))",
        "UPDATE t SET name = writefile('x', 'y') WHERE id = 1",
        "DELETE FROM t WHERE name = readfile('x')",
        "CREATE VIEW v AS SELECT readfile('x') AS a",
        "CREATE TABLE copy_t AS SELECT readfile('x') AS a",
    ],
)
def test_forbidden_functions_are_found_inside_admin_writes(sql):
    assert_rejected(check(sql, Role.ADMIN))


@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT upper(name) FROM t",
        "SELECT upper('a')",
        "SELECT count(*), max(id) FROM t",
        'SELECT "load_extension" FROM t',  # a column with that name, not a call
        "SELECT * FROM t WHERE name = 'load_extension(1)'",  # text, not a call
    ],
)
def test_ordinary_functions_and_lookalikes_are_allowed(role, sql):
    assert_allowed(check(sql, role))


# The root is a SELECT, so the role check passes for a viewer: step 6 is what rejects.
@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (DELETE FROM t RETURNING id) SELECT * FROM x",
        "WITH x AS (INSERT INTO t (name) VALUES ('a') RETURNING id) SELECT * FROM x",
        "WITH x AS (UPDATE t SET name = 'a' RETURNING id) SELECT * FROM x",
    ],
)
def test_data_modifying_cte_is_rejected(role, sql):
    assert_rejected(check(sql, role))


# --- Step 7: protected names -------------------------------------------------------------


@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM _app_meta",
        "SELECT * FROM main._app_meta",
        'SELECT * FROM "_app_meta"',
        'SELECT * FROM "_APP_Upper"',
        "SELECT * FROM t WHERE id IN (SELECT k FROM _app_meta)",
        "SELECT id FROM t UNION SELECT k FROM _app_meta",
        "SELECT * FROM sqlite_sequence",
        "SELECT * FROM sqlite_temp_master",
        "UPDATE main.sqlite_master SET sql = ''",
        "UPDATE SQLITE_MASTER SET sql = ''",
        "INSERT INTO sqlite_master (sql) VALUES ('x')",
        "DELETE FROM sqlite_master",
        "DELETE FROM sqlite_schema WHERE name = 't'",
        "INSERT INTO t (name) SELECT k FROM _app_meta",
        "CREATE TABLE _app_x (a)",
        "CREATE TABLE copy_t AS SELECT * FROM _app_meta",
        "CREATE INDEX i ON _app_meta(a)",
        "CREATE VIEW v AS SELECT * FROM _app_meta",
        "DROP TABLE _app_meta",
        # With a column type: without one, sqlglot cannot parse ADD COLUMN at all and the
        # statement would stop at step 4 instead.
        "ALTER TABLE _app_meta ADD COLUMN x TEXT",
        "ALTER TABLE _app_meta RENAME TO t9",
    ],
)
def test_protected_tables_are_rejected(role, sql):
    assert_rejected(check(sql, role))


# Tables and functions whose name starts with pragma_ or sqlite_. The pragma_ forms read
# what a PRAGMA would; sqlite_dbpage reads raw database pages.
@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM pragma_table_info('t')",
        "SELECT * FROM pragma_database_list",
        "SELECT * FROM PRAGMA_TABLE_INFO('_app_meta')",
        "SELECT * FROM sqlite_dbpage('main')",
        # sqlglot turns sqlite_version() into a node of its own, so its parsed name is no
        # longer "sqlite_version": the name has to come from the call as rendered for SQLite.
        "SELECT sqlite_version()",
        "SELECT SQLITE_VERSION()",
        "SELECT sqlite_source_id()",
    ],
)
def test_pragma_and_sqlite_names_are_rejected(role, sql):
    assert_rejected(check(sql, role))


@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM sqlite_master",
        "SELECT * FROM sqlite_schema",
        "SELECT * FROM SQLITE_MASTER",
        "SELECT name FROM sqlite_master WHERE type = 'table'",
        "SELECT * FROM my_app_data",  # "_app_" inside a name is not the reserved prefix
    ],
)
def test_reading_the_schema_table_is_allowed(role, sql):
    result = check(sql, role)
    assert_allowed(result)
    assert result.kind is StatementKind.SELECT


# --- Step 8: unknown tables are a warning, not a rejection -------------------------------


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_known_table_gives_no_warning(role):
    result = check("SELECT * FROM t", role)
    assert_allowed(result)
    assert result.reasons == []


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_unknown_table_is_allowed_with_a_warning_naming_it(role):
    result = check("SELECT * FROM missing_table", role)
    assert_allowed(result)
    assert len(result.reasons) == 1
    assert "missing_table" in result.reasons[0]


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_cte_name_gives_no_unknown_table_warning(role):
    # "x" is defined by the query itself and is not in known_tables.
    assert "x" not in KNOWN_TABLES
    result = check("WITH x AS (SELECT 1) SELECT * FROM x", role)
    assert_allowed(result)
    assert result.reasons == []


def test_unknown_table_warning_applies_to_admin_writes():
    result = check("DELETE FROM missing_table WHERE id = 1", Role.ADMIN)
    assert_allowed(result)
    assert any("missing_table" in reason for reason in result.reasons)


def test_create_table_gives_no_unknown_table_warning():
    result = check("CREATE TABLE brand_new (a INTEGER)", Role.ADMIN)
    assert_allowed(result)
    assert result.reasons == []


# --- Step 9: destructiveness -------------------------------------------------------------

# (SQL, destructive), all as admin. ALTER TABLE is covered in the designer section above.
DESTRUCTIVE_CASES = [
    ("DROP TABLE t", True),
    ("DROP INDEX idx_t_name", True),
    ("DROP VIEW v", True),
    ("DELETE FROM t", True),
    ("DELETE FROM t WHERE id = 1", False),
    ("UPDATE t SET name = 'x'", True),
    ("UPDATE t SET name = 'x' WHERE id = 1", False),
    ("SELECT * FROM t", False),
    ("INSERT INTO t (name) VALUES ('x')", False),
    ("CREATE TABLE new_t (a INTEGER)", False),
    ("CREATE INDEX idx_t_name ON t (name)", False),
    ("CREATE VIEW v AS SELECT name FROM t", False),
]


@pytest.mark.parametrize(("sql", "destructive"), DESTRUCTIVE_CASES)
def test_destructive_flag(sql, destructive):
    result = check(sql, Role.ADMIN)
    assert_allowed(result)
    assert result.is_destructive is destructive


# --- Step 10: normalized SQL -------------------------------------------------------------


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_quoted_names_keep_their_quotes_and_case(role):
    result = check('SELECT * FROM "Order" WHERE "CustomerId" = 1', role)
    assert_allowed(result)
    assert '"Order"' in result.normalized_sql
    assert '"CustomerId"' in result.normalized_sql


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_semicolon_inside_a_string_is_kept(role):
    result = check("SELECT * FROM t WHERE name = 'a;b'", role)
    assert_allowed(result)
    assert "'a;b'" in result.normalized_sql


def test_normalized_sql_is_one_statement_without_the_trailing_semicolon(db):
    result = check("SELECT name FROM t WHERE id = 2;", Role.VIEWER)
    assert_allowed(result)
    assert ";" not in result.normalized_sql
    assert db.execute(result.normalized_sql).fetchall() == [("a_b",)]


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX idx_t_name ON t (name)",
        "CREATE VIEW v AS SELECT name FROM t",
        "CREATE TABLE new_t (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL DEFAULT 'it''s', kind TEXT CHECK (kind IN ('a', 'b')), "
        "t_id INTEGER REFERENCES t (id))",
        "INSERT INTO t (name) VALUES ('new')",
        "UPDATE t SET name = 'changed' WHERE id = 1",
        "DELETE FROM t WHERE id = 1",
        "DROP TABLE t",
    ],
)
def test_normalized_admin_sql_runs_on_sqlite(db, sql):
    result = check(sql, Role.ADMIN)
    assert_allowed(result)
    db.execute(result.normalized_sql)

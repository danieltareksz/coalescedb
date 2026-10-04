"""Tests for db/introspect.py (PROJECT_SPEC.md §6.5)."""

import sqlite3

import pytest

from coalescedb.db.introspect import list_tables, sample_rows, schema_ddl
from coalescedb.errors import DatabaseNotFound, InvalidIdentifier
from coalescedb.models import ColumnInfo, ForeignKeyInfo, TableInfo

INJECTION_NAME = 'x"; DROP TABLE customers; --'

SETUP = """
CREATE TABLE customers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  tier TEXT DEFAULT 'basic',
  joined DATE
);
CREATE TABLE "Order" (
  "OrderId" INTEGER PRIMARY KEY,
  "CustomerId" INTEGER NOT NULL REFERENCES customers (id),
  "First Name" TEXT,
  total REAL DEFAULT 0
);
CREATE TABLE order_items (
  order_id INTEGER REFERENCES "Order",
  sku TEXT,
  qty INTEGER,
  PRIMARY KEY (order_id, sku)
);
CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT);
CREATE TABLE "we""ird" (a);
CREATE TABLE "x""; DROP TABLE customers; --" (payload TEXT);
CREATE TABLE _app_meta (k TEXT);
CREATE VIEW v_customers AS SELECT name FROM customers;
CREATE INDEX idx_customers_name ON customers (name);

INSERT INTO customers (name, tier, joined) VALUES
  ('Ada', 'gold', '2024-01-05'), ('Bo', 'basic', '2024-02-10'), ('Cy', 'basic', NULL);
INSERT INTO "Order" ("CustomerId", "First Name", total) VALUES (1, 'Ada', 9.5), (2, 'Bo', 20);
INSERT INTO order_items (order_id, sku, qty) VALUES
  (1, 'A', 1), (1, 'B', 2), (2, 'A', 3), (2, 'C', 4);
INSERT INTO "x""; DROP TABLE customers; --" (payload) VALUES ('still here');
INSERT INTO _app_meta (k) VALUES ('hidden');
"""

ALL_TABLES = ["Order", "customers", "notes", "order_items", 'we"ird', INJECTION_NAME]

# Least-referenced first (incoming + outgoing foreign keys), ties broken alphabetically:
#   notes 0, we"ird 0, x"; DROP... 0, customers 1, order_items 1, Order 2.
DROP_ORDER = ["notes", 'we"ird', INJECTION_NAME, "customers", "order_items", "Order"]

ROW_COUNTS = {
    "Order": 2,
    "customers": 3,
    "notes": 0,
    "order_items": 4,
    'we"ird': 0,
    INJECTION_NAME: 1,
}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    conn.executescript(SETUP)
    conn.commit()
    conn.close()
    return path


def create_statements(path):
    """{table name: CREATE statement} read with plain sqlite3."""
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'"))
    finally:
        conn.close()


def expected_ddl(path, tables, with_counts):
    statements = create_statements(path)
    blocks = []
    for name in sorted(tables):
        block = statements[name] + ";"
        if with_counts:
            block += "\n-- " + str(ROW_COUNTS[name]) + " rows"
        blocks.append(block)
    return "\n\n".join(blocks)


def customer_count(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM customers").fetchone()[0]
    finally:
        conn.close()


# --- list_tables -----------------------------------------------------------------------


def test_list_tables_returns_user_tables_only_sorted_by_name(db_path):
    tables = list_tables(db_path)
    assert all(isinstance(table, TableInfo) for table in tables)
    # No sqlite_* tables (sqlite_sequence exists because of AUTOINCREMENT), no _app_*
    # tables, and no views.
    assert [table.name for table in tables] == ALL_TABLES


def test_list_tables_columns(db_path):
    tables = {table.name: table for table in list_tables(db_path)}
    assert tables["customers"].columns == [
        ColumnInfo(name="id", type="INTEGER", not_null=False, default=None, pk=True),
        ColumnInfo(name="name", type="TEXT", not_null=True, default=None, pk=False),
        ColumnInfo(name="tier", type="TEXT", not_null=False, default="'basic'", pk=False),
        ColumnInfo(name="joined", type="DATE", not_null=False, default=None, pk=False),
    ]
    assert tables["Order"].columns == [
        ColumnInfo(name="OrderId", type="INTEGER", not_null=False, default=None, pk=True),
        ColumnInfo(name="CustomerId", type="INTEGER", not_null=True, default=None, pk=False),
        ColumnInfo(name="First Name", type="TEXT", not_null=False, default=None, pk=False),
        ColumnInfo(name="total", type="REAL", not_null=False, default="0", pk=False),
    ]
    assert [column.pk for column in tables["order_items"].columns] == [True, True, False]
    assert tables['we"ird'].columns == [
        ColumnInfo(name="a", type="", not_null=False, default=None, pk=False)
    ]
    assert [column.name for column in tables[INJECTION_NAME].columns] == ["payload"]


def test_list_tables_foreign_keys(db_path):
    tables = {table.name: table for table in list_tables(db_path)}
    assert tables["customers"].foreign_keys == []
    assert tables["Order"].foreign_keys == [
        ForeignKeyInfo(column="CustomerId", ref_table="customers", ref_column="id")
    ]
    # REFERENCES "Order" names no column: resolved to the referenced table's primary key.
    assert tables["order_items"].foreign_keys == [
        ForeignKeyInfo(column="order_id", ref_table="Order", ref_column="OrderId")
    ]


def test_list_tables_row_counts(db_path):
    assert {table.name: table.row_count for table in list_tables(db_path)} == ROW_COUNTS


def test_list_tables_on_an_empty_database(tmp_path):
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE only_one (a)")
    conn.execute("DROP TABLE only_one")
    conn.commit()
    conn.close()
    assert list_tables(path) == []
    assert schema_ddl(path) == ""


def test_introspection_does_not_modify_the_database(db_path):
    before = db_path.read_bytes()
    list_tables(db_path)
    schema_ddl(db_path)
    sample_rows(db_path, "customers")
    assert db_path.read_bytes() == before


# --- schema_ddl ------------------------------------------------------------------------


def test_schema_ddl_lists_each_create_statement_with_its_row_count(db_path):
    output = schema_ddl(db_path)
    assert output == expected_ddl(db_path, ALL_TABLES, with_counts=True)
    assert "-- 3 rows" in output
    assert "_app_meta" not in output
    assert "v_customers" not in output
    assert "idx_customers_name" not in output
    assert "sqlite_sequence" not in output


def test_schema_ddl_drops_row_counts_first(db_path):
    full = expected_ddl(db_path, ALL_TABLES, with_counts=True)
    without_counts = expected_ddl(db_path, ALL_TABLES, with_counts=False)
    assert schema_ddl(db_path, max_chars=len(full)) == full  # exactly fits: nothing dropped
    assert schema_ddl(db_path, max_chars=len(full) - 1) == without_counts
    assert schema_ddl(db_path, max_chars=len(without_counts)) == without_counts


def test_schema_ddl_then_drops_least_referenced_tables_one_by_one(db_path):
    remaining = list(ALL_TABLES)
    for dropped in DROP_ORDER:
        current = expected_ddl(db_path, remaining, with_counts=False)
        remaining.remove(dropped)
        smaller = expected_ddl(db_path, remaining, with_counts=False)
        # One character too few for the current set: exactly one more table goes.
        assert schema_ddl(db_path, max_chars=len(current) - 1) == smaller
        assert schema_ddl(db_path, max_chars=len(smaller)) == smaller
    assert remaining == []
    assert schema_ddl(db_path, max_chars=0) == ""


@pytest.mark.parametrize("max_chars", [0, 1, 10, 50, 100, 150, 200, 300, 400, 500, 600, 800])
def test_schema_ddl_never_exceeds_max_chars(db_path, max_chars):
    assert len(schema_ddl(db_path, max_chars=max_chars)) <= max_chars


def test_schema_ddl_max_chars_is_keyword_only(db_path):
    with pytest.raises(TypeError):
        schema_ddl(db_path, 100)


# --- sample_rows -----------------------------------------------------------------------


def test_sample_rows_returns_dicts(db_path):
    assert sample_rows(db_path, "customers") == [
        {"id": 1, "name": "Ada", "tier": "gold", "joined": "2024-01-05"},
        {"id": 2, "name": "Bo", "tier": "basic", "joined": "2024-02-10"},
        {"id": 3, "name": "Cy", "tier": "basic", "joined": None},
    ]


def test_sample_rows_respects_n(db_path):
    assert len(sample_rows(db_path, "order_items")) == 3  # default n
    assert len(sample_rows(db_path, "order_items", n=2)) == 2
    assert len(sample_rows(db_path, "order_items", n=100)) == 4
    assert sample_rows(db_path, "order_items", n=0) == []
    assert sample_rows(db_path, "notes") == []


def test_sample_rows_rejects_a_negative_n(db_path):
    with pytest.raises(ValueError):
        sample_rows(db_path, "customers", n=-1)


def test_sample_rows_handles_existing_names_with_spaces_uppercase_and_quotes(db_path):
    assert sample_rows(db_path, "Order", n=1) == [
        {"OrderId": 1, "CustomerId": 1, "First Name": "Ada", "total": 9.5}
    ]
    assert sample_rows(db_path, 'we"ird') == []


def test_sample_rows_treats_an_injection_style_table_name_as_a_name(db_path):
    assert sample_rows(db_path, INJECTION_NAME) == [{"payload": "still here"}]
    assert customer_count(db_path) == 3


@pytest.mark.parametrize(
    "table",
    [
        "nope",
        "CUSTOMERS",  # exact spelling only
        "_app_meta",
        "sqlite_master",
        "sqlite_sequence",
        "v_customers",  # a view, not a table
        "customers; DROP TABLE customers",
        'customers" --',
        "",
        None,
    ],
)
def test_sample_rows_rejects_unknown_tables(db_path, table):
    with pytest.raises(InvalidIdentifier):
        sample_rows(db_path, table)
    assert customer_count(db_path) == 3


# --- missing file ----------------------------------------------------------------------


def test_missing_file_raises_database_not_found(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(DatabaseNotFound):
        list_tables(missing)
    with pytest.raises(DatabaseNotFound):
        schema_ddl(missing)
    with pytest.raises(DatabaseNotFound):
        sample_rows(missing, "customers")
    assert not missing.exists()

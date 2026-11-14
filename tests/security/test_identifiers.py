"""Security tests for db/identifiers.py (PROJECT_SPEC.md §6.8).

Identifiers (table and column names) cannot be bound with `?`, so these functions are the
only thing standing between a name and the SQL text. Two cases are covered:

- NEW names the app creates: to_snake_identifier / validate_identifier / quote_identifier.
- EXISTING names from an imported database: quote_existing_identifier.
"""

import sqlite3

import pytest

from coalescedb.db.identifiers import (
    IDENT_RE,
    SQLITE_KEYWORDS,
    dedupe,
    quote_existing_identifier,
    quote_identifier,
    to_snake_identifier,
    validate_identifier,
)
from coalescedb.errors import InvalidIdentifier

# The full keyword list from https://www.sqlite.org/lang_keywords.html (147 words).
EXPECTED_KEYWORDS = frozenset(
    """
    ABORT ACTION ADD AFTER ALL ALTER ALWAYS ANALYZE AND AS ASC ATTACH AUTOINCREMENT
    BEFORE BEGIN BETWEEN BY CASCADE CASE CAST CHECK COLLATE COLUMN COMMIT CONFLICT
    CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
    DATABASE DEFAULT DEFERRABLE DEFERRED DELETE DESC DETACH DISTINCT DO DROP EACH
    ELSE END ESCAPE EXCEPT EXCLUDE EXCLUSIVE EXISTS EXPLAIN FAIL FILTER FIRST
    FOLLOWING FOR FOREIGN FROM FULL GENERATED GLOB GROUP GROUPS HAVING IF IGNORE
    IMMEDIATE IN INDEX INDEXED INITIALLY INNER INSERT INSTEAD INTERSECT INTO IS
    ISNULL JOIN KEY LAST LEFT LIKE LIMIT MATCH MATERIALIZED NATURAL NO NOT NOTHING
    NOTNULL NULL NULLS OF OFFSET ON OR ORDER OTHERS OUTER OVER PARTITION PLAN PRAGMA
    PRECEDING PRIMARY QUERY RAISE RANGE RECURSIVE REFERENCES REGEXP REINDEX RELEASE
    RENAME REPLACE RESTRICT RETURNING RIGHT ROLLBACK ROW ROWS SAVEPOINT SELECT SET
    TABLE TEMP TEMPORARY THEN TIES TO TRANSACTION TRIGGER UNBOUNDED UNION UNIQUE
    UPDATE USING VACUUM VALUES VIEW VIRTUAL WHEN WHERE WINDOW WITH WITHOUT
    """.split()
)

BAD_NEW_NAMES = [
    'x"; DROP',  # quote + statement separator
    "select",  # keyword
    "SELECT",  # keyword, and uppercase
    "1abc",  # starts with a digit
    "../x",  # path traversal characters
    "",  # empty
    "abc\n",  # trailing newline: `$` would let this through, `\Z` + fullmatch must not
    "\nabc",
    "Order",  # uppercase (and a keyword)
    "a b",  # space
    "a-b",
    "a.b",
    "a;b",
    "a'b",
    'a"b',
    "a\x00b",  # NUL byte
    "a" * 64,  # one over the 63-character limit
    "é",  # accented letter
    "ａbc",  # fullwidth "a": a unicode lookalike
    "日本",
]

GOOD_NEW_NAMES = ["a", "_x", "due_date_est", "c_1abc", "x9", "a" * 63]

# Names as they might appear in an imported database (§6.3 import_file, §6.19 dumps).
EXISTING_NAMES = [
    "Order",  # keyword, capitalised
    "CustomerId",  # uppercase letters
    "First Name",  # space
    'we"ird',  # embedded double quote
    'x"; DROP TABLE t; --',  # injection attempt stored as a name
    "select",
    "日本",
]


# --- validate_identifier / quote_identifier: NEW names ---------------------------------


@pytest.mark.parametrize("name", BAD_NEW_NAMES)
def test_validate_identifier_rejects_bad_names(name):
    with pytest.raises(InvalidIdentifier):
        validate_identifier(name)


@pytest.mark.parametrize("name", BAD_NEW_NAMES)
def test_quote_identifier_rejects_bad_names(name):
    with pytest.raises(InvalidIdentifier):
        quote_identifier(name)


@pytest.mark.parametrize("name", [None, 123, b"abc"])
def test_validate_identifier_rejects_non_strings(name):
    with pytest.raises(InvalidIdentifier):
        validate_identifier(name)


@pytest.mark.parametrize("name", GOOD_NEW_NAMES)
def test_validate_identifier_accepts_and_returns_name(name):
    assert validate_identifier(name) == name


@pytest.mark.parametrize("name", GOOD_NEW_NAMES)
def test_quote_identifier_wraps_in_double_quotes(name):
    assert quote_identifier(name) == '"' + name + '"'


def test_ident_re_does_not_match_trailing_newline():
    assert IDENT_RE.fullmatch("abc\n") is None
    assert IDENT_RE.match("abc\n") is None  # \Z anchor: safe even if .match() is used


# --- SQLITE_KEYWORDS -------------------------------------------------------------------


def test_keyword_list_is_complete():
    assert len(EXPECTED_KEYWORDS) == 147
    assert {k.upper() for k in SQLITE_KEYWORDS} == EXPECTED_KEYWORDS


@pytest.mark.parametrize("keyword", sorted(EXPECTED_KEYWORDS))
def test_every_keyword_is_rejected_in_any_case(keyword):
    for spelling in (keyword.lower(), keyword.upper(), keyword.title()):
        with pytest.raises(InvalidIdentifier):
            validate_identifier(spelling)


# --- to_snake_identifier ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Due Date (EST)", "due_date_est"),
        ("Café Total", "cafe_total"),  # accents stripped
        ("a   b__c", "a_b_c"),  # underscores collapsed
        ("  Trimmed  ", "trimmed"),
        ("1abc", "c_1abc"),  # digit start gets the c_ prefix
        ("2024 Sales", "c_2024_sales"),
        ("select", "select_col"),  # keyword gets the _col suffix
        ("Order", "order_col"),
        ("already_fine", "already_fine"),
        ('x"; DROP TABLE t; --', "x_drop_table_t"),
        ("../x", "x"),
        ("abc\n", "abc"),
        ("Name (اسم)", "name"),  # Latin part survives, Arabic part is dropped
    ],
)
def test_to_snake_identifier_normalizes(raw, expected):
    assert to_snake_identifier(raw) == expected
    assert validate_identifier(expected) == expected


def test_to_snake_identifier_truncates_to_63():
    result = to_snake_identifier("a" * 200)
    assert result == "a" * 63
    assert validate_identifier(result) == result


def test_to_snake_identifier_long_digit_start_stays_within_63():
    result = to_snake_identifier("9" * 200)
    assert result.startswith("c_9")
    assert len(result) <= 63
    assert validate_identifier(result) == result


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "   \t\n",
        "!!!",
        "___",
        "日本",
        "تاريخ",  # Arabic header ("date")
    ],
)
def test_to_snake_identifier_raises_when_nothing_usable_is_left(raw):
    with pytest.raises(InvalidIdentifier):
        to_snake_identifier(raw)


# --- quote_existing_identifier: EXISTING names -----------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Order", '"Order"'),
        ("CustomerId", '"CustomerId"'),
        ("First Name", '"First Name"'),
        ('we"ird', '"we""ird"'),
        ('x"; DROP TABLE t; --', '"x""; DROP TABLE t; --"'),
    ],
)
def test_quote_existing_identifier_keeps_spelling_and_doubles_quotes(name, expected):
    assert quote_existing_identifier(name, set(EXISTING_NAMES)) == expected


@pytest.mark.parametrize("name", ["Missing", "order", "ORDER", "First  Name", "", 'x"; DROP'])
def test_quote_existing_identifier_rejects_names_not_in_known(name):
    # "order" vs the known "Order": the match is exact, so a different spelling is unknown.
    with pytest.raises(InvalidIdentifier):
        quote_existing_identifier(name, set(EXISTING_NAMES))


def test_quote_existing_identifier_rejects_everything_when_known_is_empty():
    with pytest.raises(InvalidIdentifier):
        quote_existing_identifier("Order", set())


def test_quote_existing_identifier_rejects_nul_byte_even_if_known():
    name = "a\x00b"
    with pytest.raises(InvalidIdentifier):
        quote_existing_identifier(name, {name})


# --- Round trips through a real SQLite database ----------------------------------------
# The SQL below is built only from the output of the two quoting functions, which is the
# one sanctioned way to place an identifier into SQL text.


def _table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return sorted(row[0] for row in rows)


def _column_names(conn, table):
    rows = conn.execute("SELECT name FROM pragma_table_info(?)", (table,)).fetchall()
    return [row[0] for row in rows]


@pytest.mark.parametrize("name", EXISTING_NAMES)
def test_existing_name_round_trips_as_a_name_not_as_sql(name):
    known = set(EXISTING_NAMES)
    quoted = quote_existing_identifier(name, known)
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (id INTEGER)")  # the table an injection would drop
        conn.execute("CREATE TABLE " + quoted + " (" + quoted + " TEXT)")
        conn.execute("INSERT INTO " + quoted + " (" + quoted + ") VALUES (?)", ("v",))

        assert _table_names(conn) == sorted(["t", name])
        assert _column_names(conn, name) == [name]
        assert conn.execute("SELECT " + quoted + " FROM " + quoted).fetchall() == [("v",)]
    finally:
        conn.close()


@pytest.mark.parametrize("name", GOOD_NEW_NAMES)
def test_new_name_round_trips_through_sqlite(name):
    quoted = quote_identifier(name)
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE " + quoted + " (" + quoted + " TEXT)")
        assert _table_names(conn) == [name]
        assert _column_names(conn, name) == [name]
    finally:
        conn.close()


def test_snake_names_made_from_keywords_are_usable_in_sqlite():
    conn = sqlite3.connect(":memory:")
    try:
        for index, keyword in enumerate(sorted(EXPECTED_KEYWORDS)):
            column = quote_identifier(to_snake_identifier(keyword))
            table = quote_identifier("t_" + str(index))
            conn.execute("CREATE TABLE " + table + " (" + column + " TEXT)")
        assert len(_table_names(conn)) == len(EXPECTED_KEYWORDS)
    finally:
        conn.close()


# --- dedupe ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ([], []),
        (["a", "b"], ["a", "b"]),  # nothing to do
        (["title", "title"], ["title", "title_2"]),
        (["title", "title", "title"], ["title", "title_2", "title_3"]),
        (["b", "a", "b", "a"], ["b", "a", "b_2", "a_2"]),  # order preserved
        # A suffix that is already taken is skipped.
        (["title", "title_2", "title"], ["title", "title_2", "title_3"]),
        # ...even when the real "title_2" comes later: first occurrences keep their spelling.
        (["title", "title", "title_2"], ["title", "title_3", "title_2"]),
        # Case-insensitive, because SQLite is; the first occurrence keeps its spelling.
        (["Title", "title"], ["Title", "title_2"]),
        (["Title", "TITLE", "title"], ["Title", "TITLE_2", "title_3"]),
        (["title", "Title_2", "title"], ["title", "Title_2", "title_3"]),
    ],
)
def test_dedupe(names, expected):
    assert dedupe(names) == expected


def test_dedupe_does_not_modify_its_input():
    names = ["title", "title"]
    dedupe(names)
    assert names == ["title", "title"]


def test_dedupe_against_existing_names_keeps_existing_spelling():
    # The idiom from §6.8: existing names first, keep the tail.
    existing = ["Table_2", "Order"]
    new = ["table_2", "customers", "order"]
    result = dedupe(existing + new)
    assert result[: len(existing)] == existing
    assert result[len(existing) :] == ["table_2_2", "customers", "order_2"]


def test_dedupe_keeps_results_within_63_characters():
    long_name = "a" * 63
    result = dedupe([long_name, long_name, long_name])
    assert result[0] == long_name
    assert all(len(name) <= 63 for name in result)
    assert len({name.lower() for name in result}) == 3
    for name in result:
        assert validate_identifier(name) == name


def test_dedupe_output_is_unique_case_insensitively():
    names = ["x", "X", "x_2", "X_2", "x", "y", "Y"]
    result = dedupe(names)
    assert len(result) == len(names)
    assert len({name.lower() for name in result}) == len(names)

"""Schema extraction for the UI and prompts (PROJECT_SPEC.md §6.5).

Everything here reads through open_internal_connection, which is read-only.

Table and column metadata are read with the bound-parameter form
(`SELECT ... FROM pragma_table_info(?)`), so no name is formatted into those queries.
Counting and sampling rows need the table name in the SQL text; it goes through
quote_existing_identifier with the names just read from sqlite_master as `known`.
"""

import sqlite3
from pathlib import Path

from coalescedb.db.connection import open_internal_connection
from coalescedb.db.identifiers import quote_existing_identifier
from coalescedb.models import ColumnInfo, ForeignKeyInfo, TableInfo


def _user_tables(conn: sqlite3.Connection) -> dict[str, str]:
    """{table name: CREATE statement} for user tables, sorted by name.

    Tables only (no views), and without SQLite's own sqlite_* tables or the app's reserved
    _app_* tables.
    """
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
    tables = {
        name: sql or ""
        for name, sql in rows
        if not name.lower().startswith(("sqlite_", "_app_"))
    }
    return dict(sorted(tables.items()))


def _columns(conn: sqlite3.Connection, table: str) -> list[ColumnInfo]:
    rows = conn.execute(
        'SELECT name, type, "notnull", dflt_value, pk FROM pragma_table_info(?) ORDER BY cid',
        (table,),
    ).fetchall()
    return [
        ColumnInfo(name=name, type=type_, not_null=bool(not_null), default=default, pk=pk > 0)
        for name, type_, not_null, default, pk in rows
    ]


def _primary_key(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM pragma_table_info(?) WHERE pk > 0 ORDER BY pk", (table,)
    ).fetchall()
    return [row[0] for row in rows]


def _foreign_key_rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
    """(id, seq, referenced table, column, referenced column or None) per key column."""
    return conn.execute(
        'SELECT id, seq, "table", "from", "to" FROM pragma_foreign_key_list(?) ORDER BY id, seq',
        (table,),
    ).fetchall()


def _foreign_keys(conn: sqlite3.Connection, table: str) -> list[ForeignKeyInfo]:
    keys = []
    for _id, seq, ref_table, column, ref_column in _foreign_key_rows(conn, table):
        if ref_column is None:
            # "REFERENCES other" with no column means other's primary key.
            primary_key = _primary_key(conn, ref_table)
            ref_column = primary_key[seq] if seq < len(primary_key) else ""
        keys.append(ForeignKeyInfo(column=column, ref_table=ref_table, ref_column=ref_column))
    return keys


def _row_count(conn: sqlite3.Connection, table: str, known: set[str]) -> int:
    quoted = quote_existing_identifier(table, known)
    # Safe to join: `quoted` went through quote_existing_identifier against the table names
    # just read from this database.
    return conn.execute("SELECT count(*) FROM " + quoted).fetchone()[0]  # nosec B608


def list_tables(path: Path) -> list[TableInfo]:
    with open_internal_connection(path) as conn:
        names = list(_user_tables(conn))
        known = set(names)
        return [
            TableInfo(
                name=name,
                columns=_columns(conn, name),
                foreign_keys=_foreign_keys(conn, name),
                row_count=_row_count(conn, name, known),
            )
            for name in names
        ]


def schema_ddl(path: Path, *, max_chars: int = 6000) -> str:
    """CREATE statements with row counts, for prompts, cut down to fit max_chars.

    Too long: first every "-- N rows" comment is dropped; then tables are dropped one by one,
    least-referenced first (fewest incoming plus outgoing foreign keys, ties alphabetical).
    """
    with open_internal_connection(path) as conn:
        tables = _user_tables(conn)
        known = set(tables)
        counts = {name: _row_count(conn, name, known) for name in tables}
        references = dict.fromkeys(tables, 0)
        for name in tables:
            # One entry per foreign key, however many columns it spans.
            keys = {(row[0], row[2]) for row in _foreign_key_rows(conn, name)}
            for _key_id, ref_table in keys:
                references[name] += 1  # outgoing
                if ref_table in references:
                    references[ref_table] += 1  # incoming

    def render(names: list[str], with_counts: bool) -> str:
        blocks = []
        for name in names:
            block = tables[name] + ";"
            if with_counts:
                block += f"\n-- {counts[name]} rows"
            blocks.append(block)
        return "\n\n".join(blocks)

    remaining = list(tables)
    output = render(remaining, with_counts=True)
    if len(output) <= max_chars:
        return output

    output = render(remaining, with_counts=False)
    for victim in sorted(tables, key=lambda name: (references[name], name)):
        if len(output) <= max_chars:
            break
        remaining.remove(victim)
        output = render(remaining, with_counts=False)
    return output


def sample_rows(path: Path, table: str, n: int = 3) -> list[dict]:
    """Up to n rows of one table as dicts, so prompts can show real value formats."""
    if not isinstance(n, int) or n < 0:  # a negative LIMIT means "no limit" in SQLite
        raise ValueError("n must be a non-negative whole number")
    with open_internal_connection(path) as conn:
        quoted = quote_existing_identifier(table, set(_user_tables(conn)))
        # Safe to join: `quoted` went through quote_existing_identifier against the table
        # names just read from this database. The LIMIT value is bound.
        cursor = conn.execute("SELECT * FROM " + quoted + " LIMIT ?", (n,))  # nosec B608
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

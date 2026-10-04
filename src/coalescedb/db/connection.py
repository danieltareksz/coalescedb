"""Role-scoped connections to user databases (PROJECT_SPEC.md §6.4).

This is the strongest security layer: SQLite itself refuses what a role may not do, through
an authorizer callback that SQLite consults for every action in every statement. It works
even if every check above it (the SQL guard, the UI) were bypassed.

No SQL is built from variables in this module.
"""

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from coalescedb.errors import DatabaseNotFound, QueryTimeout
from coalescedb.models import Role
from coalescedb.security.policy import FORBIDDEN_FUNCTIONS

_INTERNAL_TIMEOUT_S = 30.0
_PROGRESS_INTERVAL = 10_000  # SQLite virtual-machine steps between deadline checks

# Viewer allowlist: reading only. Anything not listed is denied.
_READ_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)

# Admin denylist. Everything else is allowed, including ordinary schema changes.
_ADMIN_DENIED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_PRAGMA,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_VTABLE,
        sqlite3.SQLITE_DROP_VTABLE,
    }
)

# Where SQLite puts the table (or view) name for each kind of write: first or second
# argument. Views are included so nothing named _app_* can be created or dropped at all.
_TABLE_IN_ARG1 = frozenset(
    {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_DROP_VIEW,
    }
)
_TABLE_IN_ARG2 = frozenset(
    {
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_ALTER_TABLE,
    }
)

# Schema changes. Admins may make them in the database itself, never in SQLite's "temp"
# schema: CREATE TABLE temp.x reaches it without the TEMP keyword, as a plain CREATE_TABLE.
_SCHEMA_CHANGE_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    }
)

# Read-only PRAGMAs the internal connection may run. data_version is here because SQLite
# runs it internally as part of integrity_check and reports it to the authorizer.
_INTERNAL_PRAGMAS = frozenset(
    {"table_info", "foreign_key_list", "index_list", "integrity_check", "data_version"}
)

_Authorizer = Callable[[int, str | None, str | None, str | None, str | None], int]


def _is_forbidden_function(action: int, arg2: str | None) -> bool:
    return action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in FORBIDDEN_FUNCTIONS


def _is_app_table(name: str | None) -> bool:
    # Case-insensitive: "_APP_meta" is the same table to SQLite.
    return (name or "").lower().startswith("_app_")


def _is_temp_schema(name: str | None) -> bool:
    return (name or "").lower() == "temp"


def _viewer_authorizer(
    action: int, arg1: str | None, arg2: str | None, db_name: str | None, source: str | None
) -> int:
    if action not in _READ_ACTIONS or _is_forbidden_function(action, arg2):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _admin_authorizer(
    action: int, arg1: str | None, arg2: str | None, db_name: str | None, source: str | None
) -> int:
    if action in _ADMIN_DENIED_ACTIONS or _is_forbidden_function(action, arg2):
        return sqlite3.SQLITE_DENY
    # No create, drop or alter in the temp schema. SQLite passes the schema name as the
    # fourth argument, except for ALTER TABLE, where it is the first.
    if action in _SCHEMA_CHANGE_ACTIONS and _is_temp_schema(db_name):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_ALTER_TABLE and _is_temp_schema(arg1):
        return sqlite3.SQLITE_DENY
    if action in _TABLE_IN_ARG1 and _is_app_table(arg1):
        return sqlite3.SQLITE_DENY
    if action in _TABLE_IN_ARG2 and _is_app_table(arg2):
        return sqlite3.SQLITE_DENY
    # Not denied here: writes to sqlite_master. SQLite reports every CREATE, DROP and ALTER
    # as one, so denying them would block all schema changes (§6.4).
    # Not visible here: the NEW name in "ALTER TABLE t RENAME TO _app_x". SQLite only ever
    # passes the old name, so that case is the guard's job (§6.2 step 7).
    return sqlite3.SQLITE_OK


def _internal_authorizer(
    action: int, arg1: str | None, arg2: str | None, db_name: str | None, source: str | None
) -> int:
    if action == sqlite3.SQLITE_PRAGMA and arg1 in _INTERNAL_PRAGMAS:
        return sqlite3.SQLITE_OK
    return _viewer_authorizer(action, arg1, arg2, db_name, source)


def _open(path: Path, *, read_only: bool, timeout_s: float) -> sqlite3.Connection:
    path = Path(path)
    if not path.is_file():
        raise DatabaseNotFound()
    # mode=ro makes the file handle itself read-only; mode=rw never creates a missing file.
    # as_uri() percent-encodes the path, so spaces, "#", "?" and "%" in it are safe.
    uri = path.resolve().as_uri() + ("?mode=ro" if read_only else "?mode=rw")
    # isolation_level=None is autocommit: callers that need a transaction issue BEGIN/COMMIT.
    return sqlite3.connect(uri, uri=True, timeout=timeout_s, isolation_level=None)


def _lock_down(conn: sqlite3.Connection, authorizer: _Authorizer) -> None:
    """Install the authorizer. Call after the PRAGMAs: once installed it denies them."""
    if hasattr(conn, "enable_load_extension"):  # absent on some Python builds
        try:
            conn.enable_load_extension(False)
        except sqlite3.Error:
            pass  # the build can't load extensions at all
    conn.set_authorizer(authorizer)


@contextmanager
def _guarded(conn: sqlite3.Connection, timeout_s: float) -> Iterator[sqlite3.Connection]:
    """Yield the connection with a deadline; turn SQLite's interrupt into QueryTimeout."""
    deadline = time.monotonic() + timeout_s

    def past_deadline() -> int:
        return 1 if time.monotonic() > deadline else 0

    conn.set_progress_handler(past_deadline, _PROGRESS_INTERVAL)
    try:
        yield conn
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc) and time.monotonic() > deadline:
            raise QueryTimeout() from exc
        raise
    finally:
        conn.close()


@contextmanager
def open_connection(path: Path, role: Role, timeout_s: float) -> Iterator[sqlite3.Connection]:
    """Open a user database with exactly the powers of `role`."""
    if role is Role.ADMIN:
        conn = _open(path, read_only=False, timeout_s=timeout_s)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA trusted_schema = OFF")
            _lock_down(conn, _admin_authorizer)
        except BaseException:
            conn.close()
            raise
    elif role is Role.VIEWER:
        conn = _open(path, read_only=True, timeout_s=timeout_s)
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA trusted_schema = OFF")
            _lock_down(conn, _viewer_authorizer)
        except BaseException:
            conn.close()
            raise
    else:
        raise ValueError("role must be Role.ADMIN or Role.VIEWER")

    with _guarded(conn, timeout_s) as guarded:
        yield guarded


@contextmanager
def open_internal_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Read-only connection for introspection and import checks. Never given user SQL."""
    conn = _open(path, read_only=True, timeout_s=_INTERNAL_TIMEOUT_S)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA trusted_schema = OFF")
        _lock_down(conn, _internal_authorizer)
    except BaseException:
        conn.close()
        raise

    with _guarded(conn, _INTERNAL_TIMEOUT_S) as guarded:
        yield guarded

"""App metadata database: users, grants, audit log (PROJECT_SPEC.md §6.1).

This is app.db, kept apart from the user databases so no user query can ever reach it.
Low-level CRUD used only by AuthService. Every value is a bound `?` parameter and the SQL
text is fixed: nothing is ever formatted into it.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from coalescedb.errors import UserExists
from coalescedb.models import User

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  is_superadmin INTEGER NOT NULL DEFAULT 0,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until REAL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  db_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin','viewer')),
  PRIMARY KEY (user_id, db_name)
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  user_id INTEGER,
  db_name TEXT,
  action TEXT NOT NULL,
  detail TEXT,
  source TEXT
);
"""

@dataclass(frozen=True)
class UserRow:
    """A row of the users table, including the fields that never leave auth/."""

    id: int
    username: str
    password_hash: str
    is_superadmin: bool
    failed_attempts: int
    locked_until: float | None

    def to_user(self) -> User:
        return User(id=self.id, username=self.username, is_superadmin=self.is_superadmin)


def _user_row(row: tuple | None) -> UserRow | None:
    if row is None:
        return None
    return UserRow(
        id=row[0],
        username=row[1],
        password_hash=row[2],
        is_superadmin=bool(row[3]),
        failed_attempts=row[4],
        locked_until=row[5],
    )


class AppStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # One short-lived connection per operation: safe to call from several threads.
        # isolation_level=None means each statement commits on its own unless we BEGIN.
        conn = sqlite3.connect(self._path, timeout=10.0, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # BEGIN IMMEDIATE takes the write lock up front, so "check, then write" can't be
        # interleaved with another writer.
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # --- users -------------------------------------------------------------------------

    def has_any_user(self) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def get_user_by_id(self, user_id: int) -> UserRow | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, is_superadmin, failed_attempts, "
                "locked_until FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return _user_row(row)

    def get_user_by_username(self, username: str) -> UserRow | None:
        # The column is COLLATE NOCASE, so this comparison ignores case.
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, is_superadmin, failed_attempts, "
                "locked_until FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        return _user_row(row)

    def list_users(self) -> list[UserRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, username, password_hash, is_superadmin, failed_attempts, "
                "locked_until FROM users ORDER BY id"
            ).fetchall()
        return [_user_row(row) for row in rows]

    def insert_user(
        self, username: str, password_hash: str, is_superadmin: bool, created_at: float
    ) -> int:
        """Insert a user and return the new id. Raises UserExists if the name is taken."""
        with self._connect() as conn:
            return self._insert_user(conn, username, password_hash, is_superadmin, created_at)

    def insert_first_user(
        self, username: str, password_hash: str, created_at: float
    ) -> int | None:
        """Insert the first superadmin. Returns None, inserting nothing, if any user exists."""
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
                return None
            return self._insert_user(conn, username, password_hash, True, created_at)

    @staticmethod
    def _insert_user(
        conn: sqlite3.Connection,
        username: str,
        password_hash: str,
        is_superadmin: bool,
        created_at: float,
    ) -> int:
        try:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, is_superadmin, created_at) "
                "VALUES (?, ?, ?, ?)",
                (username, password_hash, int(is_superadmin), created_at),
            )
        except sqlite3.IntegrityError:
            raise UserExists() from None
        return cursor.lastrowid

    def delete_user(self, user_id: int) -> Literal["deleted", "not_found", "last_superadmin"]:
        """Delete a user (their grants go too, by foreign-key cascade).

        Refuses to delete the last superadmin. The check and the delete are one transaction.
        """
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT is_superadmin FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if row is None:
                return "not_found"
            if row[0]:
                superadmins = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE is_superadmin = 1"
                ).fetchone()[0]
                if superadmins <= 1:
                    return "last_superadmin"
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            return "deleted"

    def set_password_hash(self, user_id: int, password_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)
            )

    def record_failure(self, user_id: int, max_failures: int, lock_until: float) -> None:
        """Count one failed attempt; lock the account once max_failures is reached."""
        # One UPDATE, so two simultaneous failures can't both read the same old count.
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET "
                "locked_until = CASE WHEN failed_attempts + 1 >= ? THEN ? ELSE locked_until END, "
                "failed_attempts = failed_attempts + 1 "
                "WHERE id = ?",
                (max_failures, lock_until, user_id),
            )

    def clear_failures(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?",
                (user_id,),
            )

    # --- grants ------------------------------------------------------------------------

    def upsert_grant(self, user_id: int, db_name: str, role: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO grants (user_id, db_name, role) VALUES (?, ?, ?) "
                "ON CONFLICT (user_id, db_name) DO UPDATE SET role = excluded.role",
                (user_id, db_name, role),
            )

    def delete_grant(self, user_id: int, db_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM grants WHERE user_id = ? AND db_name = ?", (user_id, db_name)
            )

    def get_grant(self, user_id: int, db_name: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT role FROM grants WHERE user_id = ? AND db_name = ?", (user_id, db_name)
            ).fetchone()
        return None if row is None else row[0]

    def list_grants(self, user_id: int) -> list[tuple[str, str]]:
        """(db_name, role) pairs for a user, sorted by database name."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT db_name, role FROM grants WHERE user_id = ? ORDER BY db_name", (user_id,)
            ).fetchall()

    # --- audit log ---------------------------------------------------------------------

    def insert_audit(
        self,
        ts: float,
        user_id: int | None,
        db_name: str | None,
        action: str,
        detail: str,
        source: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, user_id, db_name, action, detail, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, user_id, db_name, action, detail, source),
            )

    def select_audit(self, limit: int, db_name: str | None) -> list[tuple]:
        """Newest first. Rows are (id, ts, user_id, db_name, action, detail, source)."""
        with self._connect() as conn:
            if db_name is None:
                return conn.execute(
                    "SELECT id, ts, user_id, db_name, action, detail, source FROM audit_log "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return conn.execute(
                "SELECT id, ts, user_id, db_name, action, detail, source FROM audit_log "
                "WHERE db_name = ? ORDER BY id DESC LIMIT ?",
                (db_name, limit),
            ).fetchall()

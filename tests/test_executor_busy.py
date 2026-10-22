"""Busy handling and BEGIN IMMEDIATE retries (PROJECT_SPEC.md §6.4, §6.6, §11.1).

Every test keeps the two waits far apart: busy_timeout_s is small (SQLite's wait for
another connection's lock) and query_timeout_s is large, unless the test is about the
query timeout itself. The Executor's pause between retries is injected (`sleep=`), so
no test depends on real timing.
"""

import dataclasses
import sqlite3
import time
from contextlib import contextmanager

import pytest

import coalescedb.db.executor as executor_module
from coalescedb.db.connection import open_connection
from coalescedb.db.executor import Executor
from coalescedb.errors import DatabaseBusy, QueryTimeout, RowConflict
from coalescedb.models import Role, Session

BUSY_MESSAGE = "Database busy. Another change is being saved. Try again in a moment."
EXECUTOR_ACTIONS = {"query", "query_rejected", "query_failed", "query_busy"}


# --- helpers ---------------------------------------------------------------------------


JOURNAL_MODES = {"WAL": "PRAGMA journal_mode = WAL", "DELETE": "PRAGMA journal_mode = DELETE"}
BEGIN_MODES = {"IMMEDIATE": "BEGIN IMMEDIATE", "EXCLUSIVE": "BEGIN EXCLUSIVE"}


def make_items(path, rows=3, journal="WAL"):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(JOURNAL_MODES[journal])
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, n INTEGER NOT NULL, label TEXT)"
    )
    conn.executemany("INSERT INTO items (n) VALUES (?)", [(n,) for n in range(rows)])
    conn.close()


def item_values(path):
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT n FROM items ORDER BY id")]
    finally:
        conn.close()


def snapshots(settings, db_name="sales"):
    folder = settings.backups_dir / db_name
    return sorted(folder.iterdir()) if folder.exists() else []


def entries(auth, superadmin, db_name="sales"):
    found = [
        entry
        for entry in auth.read_audit(superadmin, db_name=db_name)
        if entry["action"] in EXECUTOR_ACTIONS
    ]
    found.reverse()
    return found


class Blocker:
    """A second connection that holds SQLite's write lock until released."""

    def __init__(self, path, mode="IMMEDIATE"):
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute(BEGIN_MODES[mode])

    def release(self):
        if self.conn.in_transaction:
            self.conn.execute("ROLLBACK")

    def close(self):
        self.release()
        self.conn.close()


class FakeSleep:
    """Records each pause; optionally releases the blocker at a given pause."""

    def __init__(self, blocker=None, release_at=None):
        self.calls = []
        self.blocker = blocker
        self.release_at = release_at

    def __call__(self, seconds):
        self.calls.append(seconds)
        if self.blocker is not None and len(self.calls) == self.release_at:
            self.blocker.release()


def real_busy_error(tmp_path):
    """A genuine SQLITE_BUSY error raised by SQLite itself (S1), to re-raise in a seam."""
    path = tmp_path / "busy_source.db"
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("PRAGMA journal_mode = WAL")
    holder.execute("BEGIN IMMEDIATE")
    other = sqlite3.connect(path, isolation_level=None, timeout=0.01)
    try:
        other.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        return exc
    finally:
        other.close()
        holder.execute("ROLLBACK")
        holder.close()
    raise AssertionError("expected SQLite to report busy")


# --- fixtures --------------------------------------------------------------------------


@pytest.fixture
def busy_settings(settings):
    return dataclasses.replace(
        settings, busy_timeout_s=0.1, busy_retries=2, query_timeout_s=10.0
    )


@pytest.fixture
def make_executor(busy_settings, registry, auth, backups):
    def make(sleep, **overrides):
        return Executor(
            dataclasses.replace(busy_settings, **overrides), registry, auth, backups,
            sleep=sleep,
        )

    return make


@pytest.fixture
def db(registry, superadmin):
    path = registry.create(superadmin, "sales")
    make_items(path)
    return path


@pytest.fixture
def admin(superadmin):
    return Session(user=superadmin, db_name="sales", role=Role.ADMIN)


@pytest.fixture
def blocker(db):
    held = Blocker(db)
    yield held
    held.close()


INSERT = "INSERT INTO items (n) VALUES (?)"


# --- a write that cannot get the lock (§6.6 retry rules) -------------------------------


def test_write_gives_up_with_database_busy_after_the_retries(make_executor, auth, superadmin,
                                                             db, admin, blocker):
    sleep = FakeSleep()
    executor = make_executor(sleep)
    started = time.monotonic()
    with pytest.raises(DatabaseBusy) as excinfo:
        executor.execute(admin, INSERT, source="manual_sql", params=(9,), confirmed=True)
    assert not isinstance(excinfo.value, QueryTimeout)
    assert not isinstance(excinfo.value, sqlite3.Error)
    assert sleep.calls == [0.1, 0.3]
    assert time.monotonic() - started < 5  # three waits of busy_timeout_s, not query_timeout_s
    blocker.release()
    assert item_values(db) == [0, 1, 2]
    (entry,) = entries(auth, superadmin)
    assert entry["action"] == "query_busy"
    assert entry["detail"]["sql"] == INSERT
    assert "row_count" not in entry["detail"]


def test_later_pauses_are_all_0_3_seconds(make_executor, db, admin, blocker):
    sleep = FakeSleep()
    with pytest.raises(DatabaseBusy):
        make_executor(sleep, busy_retries=4).execute(
            admin, INSERT, source="manual_sql", params=(9,), confirmed=True
        )
    assert sleep.calls == [0.1, 0.3, 0.3, 0.3]


def test_no_retries_when_busy_retries_is_zero(make_executor, db, admin, blocker):
    sleep = FakeSleep()
    with pytest.raises(DatabaseBusy):
        make_executor(sleep, busy_retries=0).execute(
            admin, INSERT, source="manual_sql", params=(9,), confirmed=True
        )
    assert sleep.calls == []


def test_retry_succeeds_when_the_lock_is_freed_in_between(make_executor, auth, superadmin, db,
                                                          admin, blocker):
    sleep = FakeSleep(blocker, release_at=1)
    result = make_executor(sleep).execute(
        admin, INSERT, source="manual_sql", params=(9,), confirmed=True
    )
    assert result.row_count == 1
    assert sleep.calls == [0.1]
    assert item_values(db) == [0, 1, 2, 9]  # inserted exactly once
    (entry,) = entries(auth, superadmin)
    assert entry["action"] == "query"
    assert entry["detail"]["busy_retries_used"] == 1


def test_destructive_write_with_retries_takes_one_snapshot_and_one_entry(
    make_executor, settings, auth, superadmin, db, admin, blocker
):
    sleep = FakeSleep(blocker, release_at=2)
    make_executor(sleep).execute(admin, "DELETE FROM items", source="manual_sql",
                                 confirmed=True, destructive_confirm_text="sales")
    assert sleep.calls == [0.1, 0.3]
    assert item_values(db) == []
    assert len(snapshots(settings)) == 1
    (entry,) = entries(auth, superadmin)
    assert entry["detail"]["busy_retries_used"] == 2


def test_destructive_write_that_stays_busy_still_takes_only_one_snapshot(
    make_executor, settings, auth, superadmin, db, admin, blocker
):
    with pytest.raises(DatabaseBusy):
        make_executor(FakeSleep()).execute(admin, "DELETE FROM items", source="manual_sql",
                                           confirmed=True, destructive_confirm_text="sales")
    assert len(snapshots(settings)) == 1
    assert [entry["action"] for entry in entries(auth, superadmin)] == ["query_busy"]


def test_apply_changes_and_execute_many_retry_begin_immediate(make_executor, db, admin,
                                                              blocker):
    sleep = FakeSleep(blocker, release_at=1)
    executor = make_executor(sleep)
    executor.apply_changes(admin, [("UPDATE items SET n = ? WHERE id = ?", (10, 1))],
                           source="editor", confirmed=True)
    assert sleep.calls == [0.1]
    blocker.conn.execute("BEGIN IMMEDIATE")
    sleep.calls.clear()
    executor.execute_many(admin, INSERT, [(5,), (6,)], source="ingest", confirmed=True)
    assert sleep.calls == [0.1]
    assert item_values(db) == [10, 1, 2, 5, 6]


# --- never retried (§6.6) --------------------------------------------------------------


class RecordingConnection:
    """Wraps a real connection; raises a captured busy error at the first COMMIT.

    A seam: on the WAL files every admin connection uses, SQLite never reports busy
    after BEGIN IMMEDIATE (scratch check S5), so the case is produced this way.
    """

    def __init__(self, conn, error):
        self._conn = conn
        self._error = error
        self.statements = []

    def _record(self, sql):
        self.statements.append(" ".join(sql.split()).upper())
        if self.statements[-1] == "COMMIT" and self._error is not None:
            error, self._error = self._error, None
            raise error

    def execute(self, sql, *args, **kwargs):
        self._record(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        self._record(sql)
        return self._conn.executemany(sql, *args, **kwargs)

    def cursor(self, *args, **kwargs):
        return RecordingCursor(self, self._conn.cursor(*args, **kwargs))

    def __getattr__(self, name):
        return getattr(self._conn, name)


class RecordingCursor:
    def __init__(self, owner, cursor):
        self._owner = owner
        self._cursor = cursor

    def execute(self, sql, *args, **kwargs):
        self._owner._record(sql)
        return self._cursor.execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        self._owner._record(sql)
        return self._cursor.executemany(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


def test_busy_after_begin_immediate_is_rolled_back_and_never_retried(
    make_executor, auth, superadmin, db, admin, tmp_path, monkeypatch
):
    recorded = []
    error = real_busy_error(tmp_path)

    @contextmanager
    def seam(*args, **kwargs):
        with open_connection(*args, **kwargs) as conn:
            wrapper = RecordingConnection(conn, error)
            recorded.append(wrapper)
            yield wrapper

    monkeypatch.setattr(executor_module, "open_connection", seam)
    sleep = FakeSleep()
    with pytest.raises(DatabaseBusy):
        make_executor(sleep).execute(admin, INSERT, source="manual_sql", params=(9,),
                                     confirmed=True)
    (conn,) = recorded  # one connection: nothing was attempted twice
    assert conn.statements.count("BEGIN IMMEDIATE") == 1
    assert sum(sql.startswith("INSERT") for sql in conn.statements) == 1
    assert conn.statements[-1] == "ROLLBACK"  # rolled back explicitly before leaving
    assert sleep.calls == []
    assert item_values(db) == [0, 1, 2]
    assert [entry["action"] for entry in entries(auth, superadmin)] == ["query_busy"]


def test_select_is_never_retried(make_executor, registry, auth, superadmin, settings):
    # A rollback-journal file (as imported) with a writer holding EXCLUSIVE blocks readers.
    path = settings.databases_dir / "legacy.db"
    make_items(path, journal="DELETE")
    holder = Blocker(path, mode="EXCLUSIVE")
    try:
        viewer = auth.create_user(superadmin, "viewer_1", "viewer-pass-22")
        auth.grant(superadmin, viewer.id, "legacy", Role.VIEWER)
        session = Session(user=viewer, db_name="legacy", role=Role.VIEWER)
        sleep = FakeSleep()
        executor = make_executor(sleep)
        with pytest.raises(DatabaseBusy):
            executor.execute(session, "SELECT n FROM items", source="manual_sql")
        with pytest.raises(DatabaseBusy):
            executor.dry_run(session, "SELECT n FROM items")
        assert sleep.calls == []
        actions = [entry["action"] for entry in entries(auth, superadmin, "legacy")]
        assert actions == ["query_busy"]  # dry_run is audited only when refused
    finally:
        holder.close()


def test_query_timeout_is_never_retried(make_executor, db, admin):
    sleep = FakeSleep()
    heavy_write = (
        "UPDATE items SET n = n + 1 WHERE id IN (WITH RECURSIVE s(i) AS "
        "(SELECT 1 UNION ALL SELECT i + 1 FROM s WHERE i < 50000000) SELECT i FROM s)"
    )
    with pytest.raises(QueryTimeout):
        make_executor(sleep, query_timeout_s=0.05).execute(
            admin, heavy_write, source="manual_sql", confirmed=True
        )
    assert sleep.calls == []
    assert item_values(db) == [0, 1, 2]


def test_row_conflict_is_never_retried(make_executor, db, admin):
    sleep = FakeSleep()
    with pytest.raises(RowConflict):
        make_executor(sleep).apply_changes(
            admin, [("UPDATE items SET n = ? WHERE id = ?", (10, 99))],
            source="editor", confirmed=True,
        )
    assert sleep.calls == []


# --- the query timeout restarts once the lock is held (§6.4, scratch check S3) ---------


def test_write_that_gets_the_lock_late_still_has_its_whole_timeout(make_executor, db, admin,
                                                                   blocker):
    # Two busy waits of 0.15 s use up more than the 0.2 s query timeout before the lock
    # is free; the statement itself needs well over 10,000 SQLite steps but only ~10 ms.
    sleep = FakeSleep(blocker, release_at=2)
    executor = make_executor(sleep, busy_timeout_s=0.15, query_timeout_s=0.2)
    many_rows = (
        "INSERT INTO items (n) WITH RECURSIVE s(i) AS "
        "(SELECT 1 UNION ALL SELECT i + 1 FROM s WHERE i < 20000) SELECT i FROM s"
    )
    result = executor.execute(admin, many_rows, source="manual_sql", confirmed=True)
    assert result.row_count == 20000
    assert sleep.calls == [0.1, 0.3]


# --- open_connection (§6.4) ------------------------------------------------------------


def test_open_connection_turns_a_lock_wait_into_database_busy(db, blocker):
    started = time.monotonic()
    with pytest.raises(DatabaseBusy) as excinfo:
        with open_connection(db, Role.ADMIN, 10.0, busy_timeout_s=0.1) as conn:
            conn.execute("BEGIN IMMEDIATE")
    assert excinfo.value.user_message == BUSY_MESSAGE
    assert time.monotonic() - started < 2  # busy_timeout_s, not the 10 s query timeout


def test_open_connection_is_busy_while_opening(settings):
    # S4: switching a rollback-journal file to WAL needs the lock a reader holds.
    path = settings.databases_dir / "legacy.db"
    make_items(path, journal="DELETE")
    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM items").fetchall()
    try:
        with pytest.raises(DatabaseBusy):
            with open_connection(path, Role.ADMIN, 10.0, busy_timeout_s=0.1):
                pass
    finally:
        reader.execute("COMMIT")
        reader.close()


def test_other_operational_errors_stay_raw(db):
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        with open_connection(db, Role.ADMIN, 10.0, busy_timeout_s=0.1) as conn:
            conn.execute("SELECT * FROM nope")
    assert not isinstance(excinfo.value, DatabaseBusy)


def test_open_connection_without_busy_timeout_still_works(db):
    # Regression guard: expected to pass before M5's code exists.
    with open_connection(db, Role.VIEWER, 5.0) as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone() == (3,)

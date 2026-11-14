"""DatabaseLocks, the exclusive lock in delete / rename / restore, and the instance lock
(PROJECT_SPEC.md §6.3, §6.7, §11.1 paragraph for this file).

Busy handling of SQLite's own lock is in test_executor_busy.py (round 1).
"""

import dataclasses
import multiprocessing
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager

import pytest

from coalescedb.db.backup import BackupService
from coalescedb.db.executor import Executor
from coalescedb.db.locks import DatabaseLocks, InstanceLock, acquire_instance_lock
from coalescedb.db.registry import DatabaseRegistry
from coalescedb.errors import AlreadyRunning, DatabaseBusy, PermissionDenied
from coalescedb.models import Role, Session

SHARED_REFUSED = "Database busy. It is being renamed, restored or deleted. Try again in a moment."
EXCLUSIVE_REFUSED = "Database busy. It is still in use. Try again in a moment."
ALREADY_RUNNING = "CoalesceDB is already running with this data folder."
# "Never waits": a refusal must come back at once. 0.5 s leaves room for the app.db reads
# delete and rename make before the lock, while still catching any real wait.
NO_WAIT_S = 0.5


# --- helpers ---------------------------------------------------------------------------


@contextmanager
def held_in_another_thread(locks, db_name, kind="shared"):
    """Hold locks.shared / locks.exclusive(db_name) in a second thread until the block ends."""
    ready, done, errors = threading.Event(), threading.Event(), []

    def hold():
        try:
            with getattr(locks, kind)(db_name):
                ready.set()
                done.wait(10)
        except Exception as exc:  # reported below, so a broken lock can't hang the test
            errors.append(exc)
            ready.set()

    thread = threading.Thread(target=hold)
    thread.start()
    ready.wait(10)
    assert errors == []
    try:
        yield
    finally:
        done.set()
        thread.join()


def refused_at_once(call):
    """call() must raise DatabaseBusy without waiting; returns the error."""
    started = time.monotonic()
    with pytest.raises(DatabaseBusy) as excinfo:
        call()
    assert time.monotonic() - started < NO_WAIT_S
    return excinfo.value


def make_items(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, n INTEGER NOT NULL)")
    conn.executemany("INSERT INTO items (n) VALUES (?)", [(0,), (1,), (2,)])
    conn.commit()
    conn.close()


def item_values(path):
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT n FROM items ORDER BY id")]
    finally:
        conn.close()


def grants(settings):
    conn = sqlite3.connect(settings.app_db_path)
    try:
        return conn.execute("SELECT user_id, db_name, role FROM grants ORDER BY db_name").fetchall()
    finally:
        conn.close()


def tree(folder):
    return sorted(str(path.relative_to(folder)) for path in folder.rglob("*"))


@pytest.fixture
def db(registry, superadmin):
    path = registry.create(superadmin, "sales")
    make_items(path)
    return path


@pytest.fixture
def restorer(settings, registry, auth):
    """A BackupService sharing the registry's locks, as the app's wiring does."""
    return BackupService(settings, locks=registry.locks, auth=auth)


# --- DatabaseLocks ---------------------------------------------------------------------


def test_shared_allows_several_holders():
    locks = DatabaseLocks()
    with held_in_another_thread(locks, "sales"), locks.shared("sales"), locks.shared("sales"):
        pass


def test_exclusive_is_refused_at_once_while_shared_is_held():
    locks = DatabaseLocks()
    with held_in_another_thread(locks, "sales"):
        error = refused_at_once(lambda: locks.exclusive("sales").__enter__())
    assert error.user_message == EXCLUSIVE_REFUSED


def test_exclusive_is_refused_at_once_while_exclusive_is_held():
    locks = DatabaseLocks()
    with held_in_another_thread(locks, "sales", kind="exclusive"):
        error = refused_at_once(lambda: locks.exclusive("sales").__enter__())
    assert error.user_message == EXCLUSIVE_REFUSED


def test_shared_is_refused_at_once_while_exclusive_is_held():
    locks = DatabaseLocks()
    with held_in_another_thread(locks, "sales", kind="exclusive"):
        error = refused_at_once(lambda: locks.shared("sales").__enter__())
    assert error.user_message == SHARED_REFUSED


def test_locks_on_different_databases_are_independent():
    locks = DatabaseLocks()
    with held_in_another_thread(locks, "hr", kind="exclusive"), locks.exclusive("sales"):
        pass
    with held_in_another_thread(locks, "hr"), locks.exclusive("sales"):
        pass


def test_exclusive_is_free_again_once_every_shared_holder_has_left():
    locks = DatabaseLocks()
    with held_in_another_thread(locks, "sales"):
        pass
    with locks.shared("sales"):
        pass
    with locks.exclusive("sales"):
        pass


@pytest.mark.parametrize("kind", ["shared", "exclusive"])
def test_a_lock_is_released_when_its_block_raises(kind):
    locks = DatabaseLocks()
    with pytest.raises(ValueError), getattr(locks, kind)("sales"):
        raise ValueError("boom")
    with locks.exclusive("sales"):  # would be refused if anything were still held
        pass


def test_a_refused_lock_leaves_nothing_behind():
    # Could pass for the wrong reason if a refused exclusive were counted as held; the
    # second half checks that it was not.
    locks = DatabaseLocks()
    with held_in_another_thread(locks, "sales"):
        refused_at_once(lambda: locks.exclusive("sales").__enter__())
    with locks.shared("sales"):  # refused if the failed exclusive had stayed recorded
        pass


# --- registry delete and rename (§6.3) -------------------------------------------------


def test_delete_is_refused_while_the_database_is_in_use(registry, settings, auth, superadmin, db):
    before_grants, before_backups = grants(settings), tree(settings.backups_dir)
    with held_in_another_thread(registry.locks, "sales"):
        refused_at_once(lambda: registry.delete(superadmin, "sales", "sales"))
    assert db.is_file()
    assert item_values(db) == [0, 1, 2]
    assert grants(settings) == before_grants
    assert tree(settings.backups_dir) == before_backups  # no snapshot was taken
    assert not [e for e in auth.read_audit(superadmin) if e["action"] == "db_delete"]


def test_delete_checks_permission_and_the_typed_name_before_the_lock(registry, superadmin, db):
    # PermissionDenied, not DatabaseBusy: the two checks come first (§6.3).
    with held_in_another_thread(registry.locks, "sales"), pytest.raises(PermissionDenied):
        registry.delete(superadmin, "sales", "wrong")


@pytest.mark.parametrize("held", ["sales", "archive"])
def test_rename_is_refused_while_the_old_or_the_new_name_is_in_use(
    registry, settings, superadmin, db, held
):
    before_files, before_grants = tree(settings.databases_dir), grants(settings)
    with held_in_another_thread(registry.locks, held):
        refused_at_once(lambda: registry.rename(superadmin, "sales", "archive"))
    assert tree(settings.databases_dir) == before_files
    assert grants(settings) == before_grants
    assert item_values(db) == [0, 1, 2]


def test_delete_and_rename_accept_the_callers_session_id(registry, superadmin, db):
    registry.rename(superadmin, "sales", "archive", session_id="session-1")
    registry.delete(superadmin, "archive", "archive", session_id="session-1")
    assert registry.list_databases() == []


def test_delete_and_rename_release_the_lock(registry, superadmin, db):
    registry.rename(superadmin, "sales", "archive")
    with registry.locks.exclusive("sales"), registry.locks.exclusive("archive"):
        pass
    registry.delete(superadmin, "archive", "archive")
    with registry.locks.exclusive("archive"):
        pass


# --- restore (§6.7) --------------------------------------------------------------------


def test_restore_is_refused_while_the_database_is_in_use(restorer, registry, settings,
                                                         superadmin, db):
    backup = restorer.snapshot(db, "manual")
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM items")
    conn.commit()
    conn.close()
    before_backups = tree(settings.backups_dir)
    with held_in_another_thread(registry.locks, "sales"):
        refused_at_once(lambda: restorer.restore(superadmin, "sales", backup))
    assert item_values(db) == []  # not replaced
    assert tree(settings.backups_dir) == before_backups  # no pre_restore snapshot
    assert not list(settings.databases_dir.glob(".restore-*"))  # no temporary copy


# --- the Executor meets the exclusive lock ---------------------------------------------


class SnapshotProbe(BackupService):
    """Runs `probe` while the caller (delete or restore) holds the exclusive lock."""

    def __init__(self, settings, probe, **kwargs):
        super().__init__(settings, **kwargs)
        self.probe = probe

    def snapshot(self, db_path, reason):
        self.probe()
        return super().snapshot(db_path, reason)


def test_executor_is_refused_while_delete_holds_the_lock(settings, auth, superadmin, backups):
    seen = []
    registry = DatabaseRegistry(settings, auth, SnapshotProbe(settings, lambda: probe()))
    executor = Executor(settings, registry, auth, backups)
    path = registry.create(superadmin, "sales")
    make_items(path)
    admin = Session(user=superadmin, db_name="sales", role=Role.ADMIN)

    def probe():
        with pytest.raises(DatabaseBusy) as excinfo:
            executor.execute(admin, "INSERT INTO items (n) VALUES (?)", source="manual_sql",
                             params=(9,), confirmed=True)
        seen.append((excinfo.value.user_message, item_values(path)))

    registry.delete(superadmin, "sales", "sales")
    assert seen == [(SHARED_REFUSED, [0, 1, 2])]  # refused, and nothing ran


def test_executor_is_refused_while_restore_holds_the_lock(settings, registry, auth, superadmin,
                                                          backups, db):
    seen = []
    executor = Executor(settings, registry, auth, backups)
    admin = Session(user=superadmin, db_name="sales", role=Role.ADMIN)

    def probe():
        with pytest.raises(DatabaseBusy) as excinfo:
            executor.execute(admin, "SELECT n FROM items", source="manual_sql")
        seen.append(excinfo.value.user_message)

    restorer = SnapshotProbe(settings, probe, locks=registry.locks, auth=auth)
    backup = BackupService(settings).snapshot(db, "manual")
    restorer.restore(superadmin, "sales", backup)
    assert seen == [SHARED_REFUSED]


# --- one shared object (§6.3) ----------------------------------------------------------


def test_executor_without_locks_uses_the_registrys(settings, registry, auth, backups):
    # Already true after round 1; kept as a guard.
    assert Executor(settings, registry, auth, backups).locks is registry.locks


def test_each_class_keeps_the_locks_it_is_given(settings, auth, backups):
    locks = DatabaseLocks()
    service = BackupService(settings, locks=locks)
    registry = DatabaseRegistry(settings, auth, service, locks=locks)
    executor = Executor(settings, registry, auth, service, locks=locks)
    assert service.locks is registry.locks is executor.locks is locks


def test_backup_service_alone_has_its_own_locks(settings):
    assert isinstance(BackupService(settings).locks, DatabaseLocks)


# --- the instance lock (§6.3) ----------------------------------------------------------


def _try_instance_lock(settings, results):
    """Runs in a spawned process: report whether it could take the instance lock."""
    try:
        lock = acquire_instance_lock(settings)
    except AlreadyRunning:
        results.put("refused")
    else:
        lock.release()
        results.put("acquired")


def in_another_process(settings):
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    process = context.Process(target=_try_instance_lock, args=(settings, results))
    process.start()
    process.join(30)
    return results.get(timeout=5)


def test_a_second_process_cannot_take_the_instance_lock(settings):
    lock = acquire_instance_lock(settings)
    try:
        assert in_another_process(settings) == "refused"
    finally:
        lock.release()
    assert in_another_process(settings) == "acquired"


def test_a_second_lock_in_the_same_process_is_refused(settings):
    lock = acquire_instance_lock(settings)
    try:
        with pytest.raises(AlreadyRunning) as excinfo:
            acquire_instance_lock(settings)
        assert excinfo.value.user_message == ALREADY_RUNNING
    finally:
        lock.release()


def test_instance_lock_works_as_a_context_manager(settings):
    with acquire_instance_lock(settings) as lock:
        assert isinstance(lock, InstanceLock)
        with pytest.raises(AlreadyRunning):
            acquire_instance_lock(settings)
    acquire_instance_lock(settings).release()  # free again after the block


def test_instance_lock_file_is_kept_and_private(settings):
    acquire_instance_lock(settings).release()
    assert settings.instance_lock_path.is_file()  # never deleted
    if sys.platform != "win32":
        assert settings.instance_lock_path.stat().st_mode & 0o777 == 0o600


def test_instance_lock_needs_a_path(settings):
    with pytest.raises(ValueError):
        acquire_instance_lock(dataclasses.replace(settings, instance_lock_path=None))

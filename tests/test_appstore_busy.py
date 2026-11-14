"""AppStore's busy_timeout_s keyword (PROJECT_SPEC.md §6.1, §11.1 paragraph for this file).

app.db is always in WAL mode, so only writers can find its lock taken: the tests hold the
write lock from a second connection and check that the store gives up with DatabaseBusy
after busy_timeout_s instead of a raw sqlite3 error after 10 s.
"""

import inspect
import sqlite3
import time

import pytest

from coalescedb.auth.service import AuthService
from coalescedb.auth.store import AppStore
from coalescedb.errors import DatabaseBusy, UserExists

BUSY_MESSAGE = "Database busy. Another change is being saved. Try again in a moment."
FAST_S = 2.0  # well under the 10 s default: proves busy_timeout_s was used


@pytest.fixture
def quick_store(settings):
    """An AppStore that waits 0.1 s for app.db's lock, created before anyone holds it."""
    return AppStore(settings.app_db_path, busy_timeout_s=0.1)


@pytest.fixture
def writer_lock(settings, quick_store):
    conn = sqlite3.connect(settings.app_db_path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    yield conn
    conn.execute("ROLLBACK")
    conn.close()


def busy_at_once(call):
    started = time.monotonic()
    with pytest.raises(DatabaseBusy) as excinfo:
        call()
    assert time.monotonic() - started < FAST_S
    assert not isinstance(excinfo.value, sqlite3.Error)
    assert excinfo.value.user_message == BUSY_MESSAGE


def test_a_single_write_gives_up_with_database_busy(quick_store, writer_lock):
    busy_at_once(lambda: quick_store.insert_audit(1.0, None, None, "test", "{}", "system"))


def test_a_transaction_gives_up_with_database_busy(quick_store, writer_lock):
    # insert_first_user runs inside BEGIN IMMEDIATE (_transaction).
    busy_at_once(lambda: quick_store.insert_first_user("first_admin", "hash", 1.0))
    assert quick_store.has_any_user() is False


def test_auth_service_passes_database_busy_on(settings, quick_store, writer_lock, fast_hashing):
    auth = AuthService(quick_store, settings)
    busy_at_once(lambda: auth.audit(None, "test", None, {}, "system"))


def test_reads_are_not_blocked_by_a_writer(quick_store, writer_lock):
    # WAL: readers never wait for a writer. Could pass for the wrong reason only if reads
    # were skipped; the call returns a real answer.
    assert quick_store.has_any_user() is False


def test_the_store_works_again_once_the_lock_is_freed(settings, quick_store):
    holder = sqlite3.connect(settings.app_db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    busy_at_once(lambda: quick_store.insert_audit(1.0, None, None, "test", "{}", "system"))
    holder.execute("ROLLBACK")
    holder.close()
    quick_store.insert_audit(2.0, None, None, "test", "{}", "system")
    assert len(quick_store.select_audit(10, None)) == 1


def test_other_errors_are_unchanged(quick_store):
    quick_store.insert_user("alice_1", "hash", False, 1.0)
    with pytest.raises(UserExists):
        quick_store.insert_user("ALICE_1", "hash", False, 2.0)


def test_the_default_wait_stays_ten_seconds():
    parameter = inspect.signature(AppStore).parameters["busy_timeout_s"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == 10.0


def test_appstore_without_the_keyword_still_works(settings):
    # Regression guard: expected to pass before round 2's code exists.
    store = AppStore(settings.app_db_path)
    assert store.has_any_user() is False

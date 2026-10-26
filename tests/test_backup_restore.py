"""BackupService.list and BackupService.restore (PROJECT_SPEC.md §6.7, §11.1 paragraph).

Decided with Daniel before these tests were written (M5 round 2):
- every refusal in restore's step 2 (another folder, _deleted, a link, a folder, a name
  that is not a snapshot name, a path that does not exist) raises InvalidIdentifier;
- list() returns timezone-aware datetimes (tzinfo=UTC).

The lock side of restore (refused while the database is in use) is in
test_maintenance_lock.py.
"""

import dataclasses
import sqlite3
import sys
from datetime import UTC, timedelta

import pytest

from coalescedb.db.backup import BackupService
from coalescedb.errors import (
    DatabaseInUse,
    DatabaseNotFound,
    IngestError,
    InvalidIdentifier,
    PermissionDenied,
)
from coalescedb.models import Role

USER_PASSWORD = "alice-pass-22"


# --- helpers ---------------------------------------------------------------------------


def make_items(path, values=(0, 1, 2)):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, n INTEGER NOT NULL)")
    conn.executemany("INSERT INTO items (n) VALUES (?)", [(v,) for v in values])
    conn.commit()
    conn.close()


def set_items(path, values):
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM items")
    conn.executemany("INSERT INTO items (n) VALUES (?)", [(v,) for v in values])
    conn.commit()
    conn.close()


def item_values(path):
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT n FROM items ORDER BY id")]
    finally:
        conn.close()


def backup_names(settings, db_name="sales"):
    folder = settings.backups_dir / db_name
    return sorted(path.name for path in folder.iterdir()) if folder.exists() else []


def temp_copies(settings):
    return sorted(settings.databases_dir.glob(".restore-*"))


def restore_entries(auth, superadmin):
    return [e for e in auth.read_audit(superadmin) if e["action"] == "db_restore"]


# --- fixtures --------------------------------------------------------------------------


@pytest.fixture
def service(settings, registry, auth):
    """BackupService wired as the app does: the registry's locks and the AuthService."""
    return BackupService(settings, locks=registry.locks, auth=auth)


@pytest.fixture
def db(registry, superadmin):
    path = registry.create(superadmin, "sales")
    make_items(path)
    return path


@pytest.fixture
def backup(service, db):
    """A snapshot of [0, 1, 2]; afterwards the live database holds [50]."""
    taken = service.snapshot(db, "manual")
    set_items(db, [50])
    return taken


@pytest.fixture
def alice(auth, superadmin):
    return auth.create_user(superadmin, "alice_1", USER_PASSWORD)


# --- list ------------------------------------------------------------------------------


def test_list_is_newest_first_with_time_reason_and_path(service, db):
    first = service.snapshot(db, "first")
    second = service.snapshot(db, "second")
    third = service.snapshot(db, "third")
    listed = service.list("sales")
    assert [(reason, path) for _, reason, path in listed] == [
        ("third", third), ("second", second), ("first", first),
    ]
    times = [when for when, _, _ in listed]
    assert times == sorted(times, reverse=True)
    for when in times:
        assert when.tzinfo is not None and when.utcoffset() == timedelta(0)
        assert when.tzinfo == UTC


def test_list_skips_everything_that_is_not_a_snapshot(service, settings, db):
    kept = service.snapshot(db, "manual")
    folder = settings.backups_dir / "sales"
    (folder / "notes.txt").write_text("not a backup")
    (folder / "copy.db").write_bytes(kept.read_bytes())
    (folder / (kept.name + ".bak")).write_bytes(kept.read_bytes())
    (folder / "20260101T000000_000000Z_folder.db").mkdir()
    if sys.platform != "win32":
        (folder / "20260101T000000_000001Z_link.db").symlink_to(kept)
    assert [path for _, _, path in service.list("sales")] == [kept]


def test_list_without_a_folder_is_empty(service):
    assert service.list("never_backed_up") == []


@pytest.mark.parametrize("bad", ["../sales", "Sales", "_deleted", "con", ""])
def test_list_validates_the_database_name(service, bad):
    with pytest.raises(InvalidIdentifier):
        service.list(bad)


def test_list_never_looks_inside_deleted(service, registry, superadmin, db):
    service.snapshot(db, "manual")
    registry.delete(superadmin, "sales", "sales")  # moves the folder to _deleted
    assert service.list("sales") == []


# --- restore: the happy path -----------------------------------------------------------


def test_restore_brings_back_the_backup_after_a_pre_restore_snapshot(service, settings,
                                                                     superadmin, db, backup):
    service.restore(superadmin, "sales", backup)
    assert item_values(db) == [0, 1, 2]
    (pre_restore,) = [name for name in backup_names(settings) if name.endswith("_pre_restore.db")]
    assert item_values(settings.backups_dir / "sales" / pre_restore) == [50]


def test_restore_writes_one_audit_entry_with_the_file_name_only(service, auth, superadmin, db,
                                                                backup):
    service.restore(superadmin, "sales", backup)
    (entry,) = restore_entries(auth, superadmin)
    assert entry["db_name"] == "sales"
    assert entry["user_id"] == superadmin.id
    assert entry["detail"] == {"backup": backup.name}


def test_a_database_admin_may_restore(service, auth, superadmin, alice, db, backup):
    auth.grant(superadmin, alice.id, "sales", Role.ADMIN)
    service.restore(alice, "sales", backup)
    assert item_values(db) == [0, 1, 2]


def test_restoring_the_oldest_backup_survives_pruning(settings, registry, auth, superadmin, db):
    # The pre_restore snapshot prunes to backups_to_keep=2 and would delete the oldest
    # backup, the one being restored, if it were not copied first (§6.7 step 5).
    service = BackupService(dataclasses.replace(settings, backups_to_keep=2),
                            locks=registry.locks, auth=auth)
    oldest = service.snapshot(db, "manual")
    set_items(db, [7])
    service.snapshot(db, "manual")
    set_items(db, [8])
    service.restore(superadmin, "sales", oldest)
    assert item_values(db) == [0, 1, 2]
    assert not oldest.exists()  # pruned, as expected, after it had been copied


def test_restore_removes_a_stale_write_ahead_log(service, settings, superadmin, db, backup):
    # Capture a real -wal/-shm pair that holds a later change, then put it back beside the
    # database as a leftover. If restore kept it, SQLite would apply it to the restored file.
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("INSERT INTO items (n) VALUES (99)")
    wal, shm = db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")
    stale = {wal: wal.read_bytes(), shm: shm.read_bytes()}
    conn.close()
    for path, data in stale.items():
        path.write_bytes(data)

    service.restore(superadmin, "sales", backup)
    for path, data in stale.items():
        assert not path.exists() or path.read_bytes() != data, path.name
    assert item_values(db) == [0, 1, 2]


# --- restore: the temporary copy (§6.7 steps 5 and "removed on every failure") ---------


def test_the_temporary_copy_is_never_listed_as_a_database(service, registry, settings,
                                                          superadmin, db, backup, monkeypatch):
    seen = []
    real_snapshot = service.snapshot

    def watching_snapshot(db_path, reason):
        # Step 7 runs after the copy exists (step 5), so this sees the restore midway.
        seen.append((temp_copies(settings), registry.list_databases()))
        return real_snapshot(db_path, reason)

    monkeypatch.setattr(service, "snapshot", watching_snapshot)
    service.restore(superadmin, "sales", backup)
    ((copies, listed),) = seen
    assert len(copies) == 1  # proves the check really ran during the restore
    assert copies[0].name.endswith(".tmp")
    assert listed == ["sales"]
    assert temp_copies(settings) == []  # gone afterwards


def test_the_temporary_copy_is_removed_when_restore_fails(service, settings, superadmin, db,
                                                          backup, monkeypatch):
    def failing_snapshot(db_path, reason):
        raise OSError("disk full")

    monkeypatch.setattr(service, "snapshot", failing_snapshot)
    with pytest.raises(OSError, match="disk full"):
        service.restore(superadmin, "sales", backup)
    assert temp_copies(settings) == []
    assert item_values(db) == [50]  # nothing replaced


def test_a_copy_left_by_a_crash_is_ignored(service, registry, settings, superadmin, db, backup):
    # list_databases already ignores it today (it ends in .tmp); restore is new.
    leftover = settings.databases_dir / ".restore-0123456789abcdef.tmp"
    leftover.write_bytes(b"left behind by a crash")
    assert registry.list_databases() == ["sales"]
    service.restore(superadmin, "sales", backup)
    assert item_values(db) == [0, 1, 2]
    assert leftover.read_bytes() == b"left behind by a crash"  # never touched
    assert registry.list_databases() == ["sales"]


# --- restore: refusals -----------------------------------------------------------------


def assert_nothing_changed(settings, db, before):
    assert item_values(db) == [50]
    assert backup_names(settings) == before
    assert temp_copies(settings) == []


def test_restore_refuses_a_user_without_admin_rights(service, auth, settings, superadmin, alice,
                                                     db, backup):
    before = backup_names(settings)
    with pytest.raises(PermissionDenied):  # no grant at all
        service.restore(alice, "sales", backup)
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)
    with pytest.raises(PermissionDenied):  # viewer
        service.restore(alice, "sales", backup)
    assert_nothing_changed(settings, db, before)
    assert restore_entries(auth, superadmin) == []


def test_restore_refuses_without_auth(settings, registry, superadmin, db, backup):
    unwired = BackupService(settings, locks=registry.locks)
    before = backup_names(settings)
    with pytest.raises(PermissionDenied):
        unwired.restore(superadmin, "sales", backup)
    assert_nothing_changed(settings, db, before)


def bad_backup_paths(settings, service, registry, superadmin, backup):
    """Paths step 2 must refuse, each with InvalidIdentifier."""
    folder = settings.backups_dir / "sales"
    hr = registry.create(superadmin, "hr")
    make_items(hr)
    other = service.snapshot(hr, "manual")
    deleted = settings.backups_dir / "_deleted" / "sales_20260101T000000_000000Z"
    deleted.mkdir(parents=True)
    in_deleted = deleted / backup.name
    in_deleted.write_bytes(backup.read_bytes())
    renamed = folder / "copy.db"
    renamed.write_bytes(backup.read_bytes())
    as_folder = folder / "20260101T000000_000000Z_folder.db"
    as_folder.mkdir()
    paths = {
        "another database's backup": other,
        "the same file reached through ..": folder / ".." / "hr" / other.name,
        "a backup in _deleted": in_deleted,
        "a name that is not a snapshot name": renamed,
        "a folder": as_folder,
        "a path that does not exist": folder / "20260101T000000_000002Z_manual.db",
    }
    if sys.platform != "win32":
        link = folder / "20260101T000000_000001Z_link.db"
        link.symlink_to(backup)
        paths["a link"] = link
    return paths


def test_restore_refuses_every_bad_backup_path(service, registry, auth, settings, superadmin,
                                               db, backup):
    paths = bad_backup_paths(settings, service, registry, superadmin, backup)
    before = backup_names(settings)
    for label, path in paths.items():
        with pytest.raises(InvalidIdentifier):
            service.restore(superadmin, "sales", path)
        assert item_values(db) == [50], label
    assert_nothing_changed(settings, db, before)
    assert restore_entries(auth, superadmin) == []


def test_restore_of_a_missing_database_is_refused(service, superadmin, db, backup):
    db.unlink()
    with pytest.raises(DatabaseNotFound):
        service.restore(superadmin, "sales", backup)


@pytest.mark.parametrize("damage", ["garbage", "truncated"])
def test_restore_refuses_a_damaged_backup(service, settings, superadmin, db, backup, damage):
    good = backup.read_bytes()
    # Same folder and a valid snapshot name, so step 2 accepts it and step 6 must refuse it.
    damaged = backup.with_name("20260101T000000_000000Z_manual.db")
    if damage == "garbage":
        damaged.write_bytes(b"this is not a database at all" * 100)
    else:
        damaged.write_bytes(good[: len(good) // 2])  # valid header, missing pages
    before = backup_names(settings)
    with pytest.raises(IngestError):
        service.restore(superadmin, "sales", damaged)
    assert_nothing_changed(settings, db, before)


# --- restore: the presence check (§6.3 layer 1, §6.7 step 3) ---------------------------


class FakeInUse:
    def __init__(self, count):
        self.count = count
        self.calls = []

    def __call__(self, db_name, *, exclude_session_id=None):
        self.calls.append((db_name, exclude_session_id))
        return self.count


@pytest.mark.parametrize(
    ("count", "words"),
    [(1, "1 other session."), (2, "2 other sessions.")],
)
def test_restore_is_refused_while_other_sessions_have_the_database_open(
    settings, registry, auth, superadmin, db, backup, count, words
):
    in_use = FakeInUse(count)
    service = BackupService(settings, locks=registry.locks, auth=auth, in_use=in_use)
    before = backup_names(settings)
    with pytest.raises(DatabaseInUse) as excinfo:
        service.restore(superadmin, "sales", backup, session_id="session-1")
    assert excinfo.value.active_sessions == count
    assert excinfo.value.user_message == (
        f'Database "sales" is open in {words} '
        "Ask them to switch to another database, then try again."
    )
    assert in_use.calls == [("sales", "session-1")]
    assert_nothing_changed(settings, db, before)


def test_restore_goes_ahead_when_no_other_session_has_the_database(settings, registry, auth,
                                                                    superadmin, db, backup):
    in_use = FakeInUse(0)
    service = BackupService(settings, locks=registry.locks, auth=auth, in_use=in_use)
    service.restore(superadmin, "sales", backup)
    assert in_use.calls == [("sales", None)]
    assert item_values(db) == [0, 1, 2]


def test_permission_is_checked_before_the_presence_check(settings, registry, auth, alice, db,
                                                         backup):
    in_use = FakeInUse(2)
    service = BackupService(settings, locks=registry.locks, auth=auth, in_use=in_use)
    with pytest.raises(PermissionDenied):  # not DatabaseInUse: step 1 comes first
        service.restore(alice, "sales", backup)
    assert in_use.calls == []


def test_the_path_is_checked_before_the_presence_check(settings, registry, auth, superadmin, db,
                                                       backup):
    in_use = FakeInUse(2)
    service = BackupService(settings, locks=registry.locks, auth=auth, in_use=in_use)
    with pytest.raises(InvalidIdentifier):  # not DatabaseInUse: step 2 comes first
        service.restore(superadmin, "sales", backup.with_name("copy.db"))
    assert in_use.calls == []


def test_backup_paths_are_compared_after_resolving(service, settings, superadmin, db, backup):
    # A path that only reaches the right file through "." segments is still accepted.
    winding = settings.backups_dir / "." / "sales" / "." / backup.name
    service.restore(superadmin, "sales", winding)
    assert item_values(db) == [0, 1, 2]

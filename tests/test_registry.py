"""Tests for DatabaseRegistry's lifecycle methods (PROJECT_SPEC.md §6.3).

Path safety is covered in tests/security/test_registry_paths.py.
"""

import hashlib
import os
import re
import sqlite3
import sys

import pytest

from coalescedb.db.connection import open_connection
from coalescedb.db.introspect import list_tables
from coalescedb.db.registry import DatabaseRegistry
from coalescedb.errors import (
    DatabaseExists,
    DatabaseNotFound,
    IngestError,
    PermissionDenied,
)
from coalescedb.models import Role, User

USER_PASSWORD = "viewer-pass-22"
DELETED_FOLDER_RE = re.compile(r"^sales_\d{8}T\d{6}_\d{6}Z\Z")
DELETE_SNAPSHOT_RE = re.compile(r"^\d{8}T\d{6}_\d{6}Z_db_delete\.db\Z")


# --- helpers ---------------------------------------------------------------------------


@pytest.fixture
def alice(auth, superadmin):
    return auth.create_user(superadmin, "alice_1", USER_PASSWORD)


@pytest.fixture
def bob(auth, superadmin):
    return auth.create_user(superadmin, "bob_2", USER_PASSWORD)


def grants(settings):
    """Every row of the grants table, read directly from app.db."""
    conn = sqlite3.connect(settings.app_db_path)
    try:
        return conn.execute(
            "SELECT user_id, db_name, role FROM grants ORDER BY db_name, user_id"
        ).fetchall()
    finally:
        conn.close()


def audit_entries(auth, superadmin, action):
    return [entry for entry in auth.read_audit(superadmin) if entry["action"] == action]


def fill(path, rows=3):
    """Put a small table into an existing database with plain sqlite3."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (n INTEGER)")
    conn.executemany("INSERT INTO items (n) VALUES (?)", [(n,) for n in range(rows)])
    conn.commit()
    conn.close()


def read_items(path):
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT n FROM items ORDER BY n")]
    finally:
        conn.close()


def make_source(path, script):
    """A SQLite file outside the app, as a user might upload it."""
    conn = sqlite3.connect(path)
    conn.executescript(script)
    conn.commit()
    conn.close()
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def database_files(settings):
    return sorted(path.name for path in settings.databases_dir.iterdir())


def deleted_folders(settings):
    folder = settings.backups_dir / "_deleted"
    return sorted(folder.iterdir()) if folder.exists() else []


def forged_superadmin(user):
    return User(id=user.id, username=user.username, is_superadmin=True)


GOOD_SOURCE = """
CREATE TABLE "Order" ("CustomerId" INTEGER, "First Name" TEXT);
INSERT INTO "Order" VALUES (1, 'Ada'), (2, 'Bo');
CREATE VIEW v_orders AS SELECT "First Name" FROM "Order";
"""


# --- create ----------------------------------------------------------------------------


def test_create_makes_an_empty_sqlite_database(registry, settings, superadmin):
    path = registry.create(superadmin, "sales")
    assert path == registry.path_for("sales")
    assert path.read_bytes()[:16] == b"SQLite format 3\x00"
    assert registry.list_databases() == ["sales"]
    assert list_tables(path) == []


def test_created_database_is_usable(registry, superadmin):
    path = registry.create(superadmin, "sales")
    with open_connection(path, Role.ADMIN, 5.0) as conn:
        conn.execute("CREATE TABLE items (n INTEGER)")
        conn.execute("INSERT INTO items (n) VALUES (?)", (7,))
    with open_connection(path, Role.VIEWER, 5.0) as conn:
        assert conn.execute("SELECT n FROM items").fetchall() == [(7,)]


def test_create_grants_the_actor_admin_and_is_audited(registry, auth, settings, superadmin):
    registry.create(superadmin, "sales")
    assert grants(settings) == [(superadmin.id, "sales", "admin")]
    entries = audit_entries(auth, superadmin, "db_create")
    assert len(entries) == 1
    assert entries[0]["db_name"] == "sales"
    assert entries[0]["user_id"] == superadmin.id


def test_create_refuses_an_existing_name(registry, settings, superadmin):
    path = registry.create(superadmin, "sales")
    fill(path)
    with pytest.raises(DatabaseExists):
        registry.create(superadmin, "sales")
    assert read_items(path) == [0, 1, 2]  # the existing database is untouched


def test_create_clears_stray_grants_for_the_name(registry, auth, settings, superadmin, alice):
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)  # no such database yet
    auth.grant(superadmin, alice.id, "other", Role.VIEWER)
    registry.create(superadmin, "sales")
    assert grants(settings) == [
        (alice.id, "other", "viewer"),
        (superadmin.id, "sales", "admin"),
    ]
    assert auth.role_for(alice, "sales") is None


def test_create_moves_a_leftover_backup_folder_to_deleted(registry, settings, superadmin):
    leftover = settings.backups_dir / "sales"
    leftover.mkdir()
    (leftover / "20200101T000000_000000Z_manual.db").write_bytes(b"old backup")

    registry.create(superadmin, "sales")

    assert not leftover.exists()  # the new database starts with no backups
    (moved,) = deleted_folders(settings)
    assert DELETED_FOLDER_RE.fullmatch(moved.name)
    assert (moved / "20200101T000000_000000Z_manual.db").read_bytes() == b"old backup"


# --- delete ----------------------------------------------------------------------------


@pytest.mark.parametrize("confirm_text", ["", "SALES", "sale", "sales ", "yes", None])
def test_delete_needs_the_database_name_typed_exactly(
    registry, auth, settings, superadmin, alice, confirm_text
):
    path = registry.create(superadmin, "sales")
    fill(path)
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)
    before = grants(settings)

    with pytest.raises(PermissionDenied):
        registry.delete(superadmin, "sales", confirm_text)

    assert read_items(path) == [0, 1, 2]
    assert grants(settings) == before
    assert not (settings.backups_dir / "sales").exists()
    assert audit_entries(auth, superadmin, "db_delete") == []


def test_delete_backs_up_revokes_removes_and_audits(
    registry, auth, settings, superadmin, alice, bob
):
    path = registry.create(superadmin, "sales")
    fill(path)
    registry.create(superadmin, "hr")
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)
    auth.grant(superadmin, bob.id, "sales", Role.ADMIN)
    auth.grant(superadmin, alice.id, "hr", Role.VIEWER)

    registry.delete(superadmin, "sales", "sales")

    assert not path.exists()
    assert registry.list_databases() == ["hr"]
    # Every grant on "sales" is gone; grants on other databases are untouched.
    assert grants(settings) == [(superadmin.id, "hr", "admin"), (alice.id, "hr", "viewer")]
    assert auth.role_for(alice, "sales") is None
    # The backup was taken, then its folder was moved out of the way.
    assert not (settings.backups_dir / "sales").exists()
    (moved,) = deleted_folders(settings)
    assert DELETED_FOLDER_RE.fullmatch(moved.name)
    (snapshot,) = list(moved.iterdir())
    assert DELETE_SNAPSHOT_RE.fullmatch(snapshot.name)
    assert read_items(snapshot) == [0, 1, 2]
    entries = audit_entries(auth, superadmin, "db_delete")
    assert len(entries) == 1
    assert entries[0]["db_name"] == "sales"


@pytest.mark.skipif(sys.platform == "win32", reason="open files can't be removed on Windows")
def test_delete_removes_the_wal_and_shm_files(registry, settings, superadmin):
    path = registry.create(superadmin, "sales")
    fill(path)
    wal = path.with_name("sales.db-wal")
    shm = path.with_name("sales.db-shm")
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("INSERT INTO items (n) VALUES (99)")
        conn.commit()
        assert wal.exists() and shm.exists()  # they exist while a connection is open

        registry.delete(superadmin, "sales", "sales")

        assert not path.exists()
        assert not wal.exists()
        assert not shm.exists()
    finally:
        conn.close()
    (moved,) = deleted_folders(settings)
    (snapshot,) = list(moved.iterdir())
    assert read_items(snapshot) == [0, 1, 2, 99]  # the backup includes the WAL-only row


class FailingBackups:
    def snapshot(self, db_path, reason):
        raise RuntimeError("disk full")


def test_failing_snapshot_leaves_database_and_grants_untouched(
    registry, auth, settings, superadmin, alice
):
    path = registry.create(superadmin, "sales")
    fill(path)
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)
    before = grants(settings)
    broken = DatabaseRegistry(settings, auth, FailingBackups())

    with pytest.raises(RuntimeError, match="disk full"):
        broken.delete(superadmin, "sales", "sales")

    assert read_items(path) == [0, 1, 2]
    assert grants(settings) == before
    assert auth.role_for(alice, "sales") is Role.VIEWER
    assert broken.list_databases() == ["sales"]
    assert audit_entries(auth, superadmin, "db_delete") == []


def test_snapshot_happens_before_anything_is_removed(
    auth, settings, backups, superadmin, alice
):
    seen = {}

    class RecordingBackups:
        def snapshot(self, db_path, reason):
            seen["reason"] = reason
            seen["file_existed"] = db_path.exists()
            seen["grants"] = grants(settings)
            return backups.snapshot(db_path, reason)

    registry = DatabaseRegistry(settings, auth, RecordingBackups())
    path = registry.create(superadmin, "sales")
    fill(path)
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)

    registry.delete(superadmin, "sales", "sales")

    assert seen["reason"] == "db_delete"
    assert seen["file_existed"] is True
    assert seen["grants"] == [(superadmin.id, "sales", "admin"), (alice.id, "sales", "viewer")]


def test_recreated_database_starts_with_no_backups(registry, backups, settings, superadmin):
    path = registry.create(superadmin, "sales")
    fill(path)
    registry.delete(superadmin, "sales", "sales")

    new_path = registry.create(superadmin, "sales")
    assert not (settings.backups_dir / "sales").exists()
    assert list_tables(new_path) == []
    backups.snapshot(new_path, "manual")
    assert len(list((settings.backups_dir / "sales").iterdir())) == 1
    assert len(deleted_folders(settings)) == 1  # the old backups are still kept apart


def test_delete_missing_database_raises(registry, superadmin):
    with pytest.raises(DatabaseNotFound):
        registry.delete(superadmin, "ghost", "ghost")


# --- rename ----------------------------------------------------------------------------


def test_rename_moves_file_grants_and_backups(
    registry, auth, backups, settings, superadmin, alice
):
    path = registry.create(superadmin, "sales")
    fill(path)
    registry.create(superadmin, "hr")
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)
    auth.grant(superadmin, alice.id, "hr", Role.ADMIN)
    snapshot = backups.snapshot(path, "manual")

    registry.rename(superadmin, "sales", "crm")

    assert not path.exists()
    assert read_items(registry.path_for("crm")) == [0, 1, 2]
    assert registry.list_databases() == ["crm", "hr"]
    assert grants(settings) == [
        (superadmin.id, "crm", "admin"),
        (alice.id, "crm", "viewer"),
        (superadmin.id, "hr", "admin"),
        (alice.id, "hr", "admin"),
    ]
    assert auth.role_for(alice, "crm") is Role.VIEWER
    assert auth.role_for(alice, "sales") is None
    assert not (settings.backups_dir / "sales").exists()
    assert (settings.backups_dir / "crm" / snapshot.name).exists()
    entries = audit_entries(auth, superadmin, "db_rename")
    assert len(entries) == 1


def test_rename_refuses_an_existing_target_file(registry, settings, superadmin):
    sales = registry.create(superadmin, "sales")
    fill(sales)
    crm = registry.create(superadmin, "crm")
    before = grants(settings)
    with pytest.raises(DatabaseExists):
        registry.rename(superadmin, "sales", "crm")
    with pytest.raises(DatabaseExists):
        registry.rename(superadmin, "sales", "sales")
    assert read_items(sales) == [0, 1, 2]
    assert list_tables(crm) == []
    assert grants(settings) == before


def test_rename_refuses_when_the_target_backup_folder_exists(
    registry, auth, settings, superadmin, alice
):
    path = registry.create(superadmin, "sales")
    fill(path)
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)
    (settings.backups_dir / "crm").mkdir()
    before = grants(settings)

    with pytest.raises(DatabaseExists):
        registry.rename(superadmin, "sales", "crm")

    # Checked before anything moves.
    assert read_items(path) == [0, 1, 2]
    assert registry.list_databases() == ["sales"]
    assert grants(settings) == before
    assert audit_entries(auth, superadmin, "db_rename") == []


def test_rename_clears_stray_grants_for_the_new_name(
    registry, auth, settings, superadmin, alice, bob
):
    registry.create(superadmin, "sales")
    auth.grant(superadmin, bob.id, "sales", Role.VIEWER)
    auth.grant(superadmin, alice.id, "crm", Role.ADMIN)  # stray: no such database yet
    auth.grant(superadmin, bob.id, "crm", Role.ADMIN)  # would collide with bob's moved grant

    registry.rename(superadmin, "sales", "crm")

    assert grants(settings) == [(superadmin.id, "crm", "admin"), (bob.id, "crm", "viewer")]
    assert auth.role_for(alice, "crm") is None
    assert auth.role_for(bob, "crm") is Role.VIEWER


def test_rename_missing_database_raises(registry, superadmin):
    with pytest.raises(DatabaseNotFound):
        registry.rename(superadmin, "ghost", "crm")


# --- import_file -----------------------------------------------------------------------


def test_import_keeps_existing_names_and_rows(registry, auth, settings, superadmin, tmp_path):
    source = make_source(tmp_path / "upload.sqlite", GOOD_SOURCE)
    before = digest(source)

    path = registry.import_file(superadmin, source, "sales")

    assert path == registry.path_for("sales")
    assert registry.list_databases() == ["sales"]
    (table,) = list_tables(path)
    assert table.name == "Order"
    assert [column.name for column in table.columns] == ["CustomerId", "First Name"]
    assert table.row_count == 2
    with open_connection(path, Role.VIEWER, 5.0) as conn:
        assert conn.execute('SELECT "First Name" FROM v_orders').fetchall() == [
            ("Ada",),
            ("Bo",),
        ]
    assert digest(source) == before  # the uploaded file is never modified
    assert database_files(settings) == ["sales.db"]  # no temp file left behind
    assert grants(settings) == [(superadmin.id, "sales", "admin")]
    entries = audit_entries(auth, superadmin, "db_import")
    assert len(entries) == 1
    assert entries[0]["db_name"] == "sales"


def test_import_refuses_an_existing_name(registry, superadmin, tmp_path):
    source = make_source(tmp_path / "upload.sqlite", GOOD_SOURCE)
    path = registry.create(superadmin, "sales")
    fill(path)
    with pytest.raises(DatabaseExists):
        registry.import_file(superadmin, source, "sales")
    assert read_items(path) == [0, 1, 2]


def test_import_clears_stray_grants_and_leftover_backups(
    registry, auth, settings, superadmin, alice, tmp_path
):
    source = make_source(tmp_path / "upload.sqlite", GOOD_SOURCE)
    auth.grant(superadmin, alice.id, "sales", Role.ADMIN)  # stray
    leftover = settings.backups_dir / "sales"
    leftover.mkdir()
    (leftover / "20200101T000000_000000Z_manual.db").write_bytes(b"old backup")

    registry.import_file(superadmin, source, "sales")

    assert grants(settings) == [(superadmin.id, "sales", "admin")]
    assert auth.role_for(alice, "sales") is None
    assert not leftover.exists()  # the imported database starts with no backups
    (moved,) = deleted_folders(settings)
    assert DELETED_FOLDER_RE.fullmatch(moved.name)
    assert (moved / "20200101T000000_000000Z_manual.db").read_bytes() == b"old backup"


def corrupt_source(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE big (a TEXT)")
    conn.executemany("INSERT INTO big (a) VALUES (?)", [("x" * 200,) for _ in range(500)])
    conn.execute("CREATE INDEX idx_big ON big (a)")
    conn.commit()
    conn.close()
    data = bytearray(path.read_bytes())
    start = 4096 * 3 + 50
    data[start : start + 850] = b"\xff" * 850  # keep the header, wreck a page of the table
    path.write_bytes(bytes(data))
    return path


def bad_sources(tmp_path):
    text_file = tmp_path / "notes.sqlite"
    text_file.write_text("this is not a database, it only has the right extension\n" * 20)
    empty_file = tmp_path / "empty.db"
    empty_file.write_bytes(b"")
    with_trigger = make_source(
        tmp_path / "trigger.db",
        "CREATE TABLE t (a); CREATE TABLE log (a);"
        "CREATE TRIGGER trg_audit AFTER INSERT ON t BEGIN INSERT INTO log VALUES (1); END;",
    )
    with_virtual = make_source(
        tmp_path / "virtual.db", "CREATE TABLE t (a); CREATE VIRTUAL TABLE vt_search USING fts5(a);"
    )
    return {
        "text file": (text_file, None),
        "empty file": (empty_file, None),
        "corrupt": (corrupt_source(tmp_path / "corrupt.db"), None),
        "trigger": (with_trigger, "trg_audit"),
        "virtual table": (with_virtual, "vt_search"),
        "missing": (tmp_path / "does_not_exist.db", None),
    }


@pytest.mark.parametrize(
    "kind", ["text file", "empty file", "corrupt", "trigger", "virtual table", "missing"]
)
def test_import_refuses_bad_files_and_leaves_nothing_behind(
    registry, auth, settings, superadmin, alice, tmp_path, kind
):
    source, named = bad_sources(tmp_path)[kind]
    before = digest(source) if source.exists() else None
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)  # stray grant: must survive
    leftover = settings.backups_dir / "sales"
    leftover.mkdir()

    with pytest.raises(IngestError) as excinfo:
        registry.import_file(superadmin, source, "sales")

    if named is not None:
        assert named in excinfo.value.user_message  # the offending object is named
    assert database_files(settings) == []  # no database, no temp file
    assert registry.list_databases() == []
    # A refused file touches nothing else: grants and the backup folder are as they were.
    assert grants(settings) == [(alice.id, "sales", "viewer")]
    assert leftover.exists()
    assert deleted_folders(settings) == []
    assert audit_entries(auth, superadmin, "db_import") == []
    if before is not None:
        assert digest(source) == before


# --- only superadmins ------------------------------------------------------------------


@pytest.mark.parametrize("forged", [False, True])
@pytest.mark.parametrize("operation", ["create", "delete", "rename", "import_file"])
def test_non_superadmins_are_refused(
    registry, auth, settings, superadmin, alice, tmp_path, operation, forged
):
    path = registry.create(superadmin, "sales")
    fill(path)
    auth.grant(superadmin, alice.id, "sales", Role.ADMIN)  # admin on the DB is not enough
    source = make_source(tmp_path / "upload.sqlite", GOOD_SOURCE)
    actor = forged_superadmin(alice) if forged else alice
    before = grants(settings)

    with pytest.raises(PermissionDenied):
        if operation == "create":
            registry.create(actor, "other")
        elif operation == "delete":
            registry.delete(actor, "sales", "sales")
        elif operation == "rename":
            registry.rename(actor, "sales", "crm")
        else:
            registry.import_file(actor, source, "other")

    assert database_files(settings) == ["sales.db"]
    assert read_items(path) == [0, 1, 2]
    assert grants(settings) == before
    assert not (settings.backups_dir / "sales").exists()


# --- rename: a failing grants update puts the file back ---------------------------------


def test_rename_puts_the_file_back_when_the_grants_update_fails(
    registry, auth, backups, settings, superadmin, alice, monkeypatch
):
    path = registry.create(superadmin, "sales")
    fill(path)
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)
    snapshot = backups.snapshot(path, "manual")
    before = grants(settings)

    def failing_rename_grants(actor, old, new):
        raise RuntimeError("app.db is locked")

    monkeypatch.setattr(auth, "rename_grants", failing_rename_grants)

    with pytest.raises(RuntimeError, match="app.db is locked"):
        registry.rename(superadmin, "sales", "crm")

    # The file is back under the old name, with its rows; nothing is left under the new one.
    assert path.exists()
    assert not [name for name in database_files(settings) if name.startswith("crm")]
    assert read_items(path) == [0, 1, 2]
    assert registry.list_databases() == ["sales"]
    # Every grant is unchanged.
    assert grants(settings) == before
    assert auth.role_for(alice, "sales") is Role.VIEWER
    assert auth.role_for(alice, "crm") is None
    # The backup folder was not moved.
    assert (settings.backups_dir / "sales" / snapshot.name).exists()
    assert not (settings.backups_dir / "crm").exists()
    assert audit_entries(auth, superadmin, "db_rename") == []


# --- rename: a failing file move undoes the moves already made --------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="open files can't be moved on Windows")
def test_rename_undoes_completed_moves_when_a_later_move_fails(
    registry, auth, settings, superadmin, alice, monkeypatch
):
    path = registry.create(superadmin, "sales")
    fill(path)
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)
    before = grants(settings)
    real_rename = os.rename

    def rename_that_fails_on_the_wal(source, target, *args, **kwargs):
        if str(source).endswith("sales.db-wal"):
            raise OSError("disk error")
        return real_rename(source, target, *args, **kwargs)

    conn = sqlite3.connect(path)
    try:
        # An open connection in WAL mode keeps sales.db-wal and sales.db-shm on disk.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("INSERT INTO items (n) VALUES (99)")
        conn.commit()
        assert path.with_name("sales.db-wal").exists()
        monkeypatch.setattr(os, "rename", rename_that_fails_on_the_wal)

        with pytest.raises(OSError, match="disk error"):
            registry.rename(superadmin, "sales", "crm")

        monkeypatch.undo()
        # The database is fully back under the old name; nothing is left under the new one.
        assert path.exists()
        assert not [name for name in database_files(settings) if name.startswith("crm")]
        assert registry.list_databases() == ["sales"]
    finally:
        conn.close()
    assert read_items(path) == [0, 1, 2, 99]
    assert grants(settings) == before
    assert auth.role_for(alice, "sales") is Role.VIEWER
    assert audit_entries(auth, superadmin, "db_rename") == []


# --- import_file: virtual tables are detected whatever their stored SQL looks like ------


@pytest.mark.parametrize(
    "stored_sql",
    [
        "CREATE VIRTUAL  TABLE vt_search USING fts5(a)",  # two spaces
        "CREATE\tVIRTUAL TABLE vt_search USING fts5(a)",  # a tab
        "create  virtual\ttable vt_search using fts5(a)",  # lowercase and odd whitespace
        "CREATE/**/VIRTUAL/**/TABLE vt_search USING fts5(a)",  # comments instead of spaces
    ],
)
def test_import_refuses_a_virtual_table_whose_stored_sql_is_disguised(
    registry, auth, settings, superadmin, tmp_path, stored_sql
):
    source = make_source(
        tmp_path / "crafted.db", "CREATE TABLE t (a); CREATE VIRTUAL TABLE vt_search USING fts5(a);"
    )
    conn = sqlite3.connect(source)
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("UPDATE sqlite_master SET sql = ? WHERE name = 'vt_search'", (stored_sql,))
    conn.commit()
    conn.close()

    with pytest.raises(IngestError) as excinfo:
        registry.import_file(superadmin, source, "sales")

    assert "vt_search" in excinfo.value.user_message
    assert database_files(settings) == []
    assert registry.list_databases() == []
    assert grants(settings) == []
    assert audit_entries(auth, superadmin, "db_import") == []


# --- create: a failure part-way leaves nothing behind -----------------------------------


def test_create_leaves_no_file_when_the_grant_fails_and_can_be_retried(
    registry, auth, settings, superadmin, monkeypatch
):
    def failing_grant(actor, user_id, db_name, role):
        raise RuntimeError("app.db is locked")

    monkeypatch.setattr(auth, "grant", failing_grant)
    with pytest.raises(RuntimeError, match="app.db is locked"):
        registry.create(superadmin, "sales")

    assert database_files(settings) == []  # no .db, no -wal, no -shm
    assert registry.list_databases() == []
    assert grants(settings) == []
    assert audit_entries(auth, superadmin, "db_create") == []

    monkeypatch.undo()
    path = registry.create(superadmin, "sales")  # the name is free, so a retry works
    assert registry.list_databases() == ["sales"]
    assert list_tables(path) == []
    assert grants(settings) == [(superadmin.id, "sales", "admin")]

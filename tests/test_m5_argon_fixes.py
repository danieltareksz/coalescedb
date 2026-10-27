"""Fixes from the Gemini Argon review of M5, plus the leads checked with it.

PROJECT_SPEC.md: §6.2 step 9 (INSERT OR REPLACE / ON CONFLICT DO UPDATE destructive; INSERT
target alias refused), §6.4 (timeout test by error code), §6.6 (strict INSERT shape in
execute_many and apply_changes, one row per INSERT, 1,000-statement cap, audit list cap),
§6.7 (linked backups folder, exclusive snapshot names), §3 "File permissions".

Tests marked "seam" rely on a name the spec does not fix; "regression guard" tests are
expected to pass before the fix.
"""

import contextlib
import dataclasses
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

import coalescedb.db.backup as backup_module
import coalescedb.db.executor as executor_module
from coalescedb.auth.store import AppStore
from coalescedb.config import ENV_PREFIX, load_settings
from coalescedb.db.backup import BackupService
from coalescedb.db.executor import Executor
from coalescedb.errors import (
    ExecutionError,
    InvalidIdentifier,
    PermissionDenied,
    QueryTimeout,
    RowConflict,
    SQLRejected,
)
from coalescedb.models import Role, Session
from coalescedb.security.sql_guard import validate

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits (M12)")
CAP_MESSAGE = "At most 1,000 statements can be applied at once."
EXECUTOR_ACTIONS = {"query", "query_rejected", "query_failed", "query_busy", "ingest"}


# --- helpers ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE parent (id INTEGER PRIMARY KEY, code TEXT UNIQUE, n INTEGER NOT NULL);
CREATE TABLE child (id INTEGER PRIMARY KEY,
                    parent_id INTEGER REFERENCES parent(id) ON DELETE CASCADE);
INSERT INTO parent (code, n) VALUES ('a', 1), ('b', 2), ('c', 3);
INSERT INTO child (parent_id) VALUES (1), (1), (2);
"""


def build(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def counts(path):
    conn = sqlite3.connect(path)
    try:
        parents = conn.execute("SELECT count(*) FROM parent").fetchone()[0]
        children = conn.execute("SELECT count(*) FROM child").fetchone()[0]
        return parents, children
    finally:
        conn.close()


def mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


def entries(auth, superadmin):
    found = [e for e in auth.read_audit(superadmin, db_name="sales", limit=10_000)
             if e["action"] in EXECUTOR_ACTIONS]
    found.reverse()
    return found


@pytest.fixture
def umask_022():
    """Make the loose default explicit, so 0o600 can only come from the app."""
    old = os.umask(0o022)
    yield
    os.umask(old)


@pytest.fixture
def db(registry, superadmin):
    path = registry.create(superadmin, "sales")
    build(path)
    return path


@pytest.fixture
def executor(settings, registry, auth, backups):
    return Executor(settings, registry, auth, backups)


@pytest.fixture
def admin(superadmin):
    return Session(user=superadmin, db_name="sales", role=Role.ADMIN)


# --- §6.2 step 9: decision B ------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT OR REPLACE INTO parent (id, code, n) VALUES (1, 'z', 9)",
        "INSERT INTO parent (code, n) VALUES ('a', 9) ON CONFLICT (code) DO UPDATE SET n = 9",
        "INSERT INTO parent (code, n) VALUES ('a', 9) "
        "ON CONFLICT (code) DO UPDATE SET n = excluded.n WHERE n < 5",
    ],
)
def test_replace_and_upsert_are_destructive(sql):
    result = validate(sql, Role.ADMIN, {"parent"})
    assert result.allowed is True
    assert result.is_destructive is True


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO parent (code, n) VALUES ('z', 9)",
        "INSERT OR IGNORE INTO parent (code, n) VALUES ('z', 9)",
        "INSERT OR ABORT INTO parent (code, n) VALUES ('z', 9)",
        "INSERT OR FAIL INTO parent (code, n) VALUES ('z', 9)",
        "INSERT OR ROLLBACK INTO parent (code, n) VALUES ('z', 9)",
        "INSERT INTO parent (code, n) VALUES ('a', 9) ON CONFLICT DO NOTHING",
    ],
)
def test_other_inserts_stay_non_destructive(sql):
    # Regression guard: expected to pass before the fix; must still pass after it.
    result = validate(sql, Role.ADMIN, {"parent"})
    assert result.allowed is True
    assert result.is_destructive is False


def test_replace_into_is_refused():
    # Regression guard: sqlglot 30.21.0 cannot parse it, so step 4 refuses it today.
    result = validate("REPLACE INTO parent (code, n) VALUES ('z', 9)", Role.ADMIN, {"parent"})
    assert result.allowed is False


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO parent AS p (code, n) VALUES ('z', 9)",
        "INSERT INTO parent AS p (code, n) VALUES ('a', 9) "
        "ON CONFLICT (code) DO UPDATE SET n = p.n + 1",
        "INSERT INTO main.parent AS p (code, n) VALUES ('z', 9)",
    ],
)
def test_insert_with_a_target_alias_is_refused(sql):
    result = validate(sql, Role.ADMIN, {"parent"})
    assert result.allowed is False
    assert result.normalized_sql is None


def test_insert_or_replace_through_execute_needs_the_typed_name_and_a_snapshot(
    executor, settings, db, admin
):
    sql = "INSERT OR REPLACE INTO parent (id, code, n) VALUES (?, ?, ?)"
    with pytest.raises(PermissionDenied):
        executor.execute(admin, sql, source="manual_sql", params=(1, "z", 9), confirmed=True)
    assert counts(db) == (3, 3)
    executor.execute(admin, sql, source="manual_sql", params=(1, "z", 9), confirmed=True,
                     destructive_confirm_text="sales")
    assert counts(db) == (3, 1)  # the cascade happened, but only after confirmation
    (snapshot,) = sorted((settings.backups_dir / "sales").iterdir())
    assert snapshot.name.endswith("_destructive_query.db")
    assert counts(snapshot) == (3, 3)


# --- §6.6: strict INSERT shape (findings 1 and 5) ---------------------------------------


NOT_STRICT = [
    "INSERT OR REPLACE INTO parent (id, code, n) VALUES (?, ?, ?)",
    "INSERT OR IGNORE INTO parent (id, code, n) VALUES (?, ?, ?)",
    "INSERT OR ROLLBACK INTO parent (id, code, n) VALUES (?, ?, ?)",
    "INSERT INTO parent (id, code, n) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
    "INSERT INTO parent (id, code, n) VALUES (?, ?, ?) "
    "ON CONFLICT (code) DO UPDATE SET n = excluded.n",
    "INSERT INTO parent (id, code, n) VALUES (?, ?, ?) RETURNING id",
    "WITH x AS (SELECT 1) INSERT INTO parent (id, code, n) VALUES (?, ?, ?)",
]


@pytest.mark.parametrize("sql", NOT_STRICT)
def test_execute_many_refuses_anything_beyond_one_values_row(executor, db, admin, sql):
    consumed = []

    def rows():
        consumed.append(True)
        yield (1, "z", 9)

    with pytest.raises(SQLRejected):
        executor.execute_many(admin, sql, rows(), source="ingest", confirmed=True)
    assert consumed == []
    assert counts(db) == (3, 3)


def test_execute_many_plain_insert_still_works(executor, db, admin):
    # Regression guard: expected to pass before the fix.
    count = executor.execute_many(admin, "INSERT INTO parent (code, n) VALUES (?, ?)",
                                  [("x", 1), ("y", 2)], source="ingest", confirmed=True)
    assert count == 2
    assert counts(db) == (5, 3)


@pytest.mark.parametrize(
    "sql",
    [*NOT_STRICT,
     "INSERT INTO parent (code, n) SELECT code || ?, n FROM parent",
     "INSERT INTO parent DEFAULT VALUES",
     "INSERT INTO parent (code, n) VALUES (?, 1)"],
)
def test_apply_changes_refuses_any_insert_but_the_strict_shape(executor, db, admin, sql):
    params = tuple(range(sql.count("?")))
    with pytest.raises(SQLRejected):
        executor.apply_changes(
            admin,
            [("UPDATE parent SET n = ? WHERE id = ?", (50, 3)), (sql, params)],
            source="editor", confirmed=True,
        )
    assert counts(db) == (3, 3)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT n FROM parent WHERE id = 3").fetchone() == (3,)
    conn.close()


def test_apply_changes_strict_insert_still_works(executor, db, admin):
    # Regression guard: the data editor's form (§6.28) is accepted.
    changed = executor.apply_changes(
        admin, [('INSERT INTO "parent" ("code", "n") VALUES (?, ?)', ("x", 7))],
        source="editor", confirmed=True,
    )
    assert changed == 1
    assert counts(db) == (4, 3)


def test_apply_changes_insert_must_change_exactly_one_row(executor, db, admin, monkeypatch):
    # Seam: the strict shape makes a 0-row INSERT unreachable through the public API, so the
    # shape check (Executor._check_bulk_insert_shape, a private name) is switched off to
    # show the one-row rule holds on its own as a second layer.
    monkeypatch.setattr(Executor, "_check_bulk_insert_shape", staticmethod(lambda sql: None))
    with pytest.raises(RowConflict) as excinfo:
        executor.apply_changes(
            admin,
            [("UPDATE parent SET n = ? WHERE id = ?", (50, 3)),
             ("INSERT OR IGNORE INTO parent (code, n) VALUES (?, ?)", ("a", 9))],
            source="editor", confirmed=True,
        )
    assert excinfo.value.statement_index == 1
    assert counts(db) == (3, 3)


# --- §6.6: statement cap and audit list cap (finding 2) ---------------------------------


def test_apply_changes_refuses_more_than_1000_statements_before_guarding(
    executor, auth, superadmin, db, admin, monkeypatch
):
    guarded = []
    real_validate = executor_module.validate

    def counting_validate(*args, **kwargs):
        guarded.append(True)
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(executor_module, "validate", counting_validate)
    statements = [("INSERT INTO parent (code, n) VALUES (?, ?)", (f"k{i}", i))
                  for i in range(1001)]
    with pytest.raises(SQLRejected) as excinfo:
        executor.apply_changes(admin, statements, source="editor", confirmed=True)
    assert CAP_MESSAGE in excinfo.value.reasons
    assert guarded == []  # refused before any statement was guarded
    assert counts(db) == (3, 3)
    (entry,) = entries(auth, superadmin)
    assert entry["action"] == "query_rejected"
    assert entry["detail"]["statement_count"] == 1001
    assert len(entry["detail"]["sql"]) == 100
    assert len(entry["detail"]["sql_length"]) == 100


def test_apply_changes_accepts_exactly_1000_statements(executor, db, admin):
    # Regression guard: passes today; must keep passing once the cap exists.
    statements = [("INSERT INTO parent (code, n) VALUES (?, ?)", (f"k{i}", i))
                  for i in range(1000)]
    assert executor.apply_changes(admin, statements, source="editor", confirmed=True) == 1000


def test_apply_schema_refuses_more_than_1000_statements_before_guarding(
    executor, db, admin, monkeypatch
):
    guarded = []
    monkeypatch.setattr(executor_module, "validate",
                        lambda *a, **k: guarded.append(True))
    ddl = [f"CREATE TABLE t{i} (a INTEGER)" for i in range(1001)]
    with pytest.raises(SQLRejected) as excinfo:
        executor.apply_schema(admin, ddl, source="sql_import", confirmed=True)
    assert CAP_MESSAGE in excinfo.value.reasons
    assert guarded == []


def test_audit_sql_lists_hold_at_most_100_statements(executor, auth, superadmin, db, admin):
    ddl = [f"CREATE TABLE t{i} (a INTEGER)" for i in range(150)]
    executor.apply_schema(admin, ddl, source="designer", confirmed=True)
    with pytest.raises(SQLRejected):
        executor.apply_schema(admin, [*ddl[:149], "CREATE TABLE _app_x (a INTEGER)"],
                              source="designer", confirmed=True)
    success, rejected = entries(auth, superadmin)
    assert success["action"] == "query"
    assert len(success["detail"]["sql"]) == 100
    assert success["detail"]["statement_count"] == 150
    assert rejected["action"] == "query_rejected"
    assert len(rejected["detail"]["sql"]) == 100
    assert rejected["detail"]["statement_count"] == 150


# --- §6.4: the timeout test uses the error code (lead) ----------------------------------


@pytest.mark.parametrize("sql", ["SELECT interrupted FROM parent", "SELECT * FROM interrupted"])
def test_an_error_mentioning_interrupted_is_an_execution_error(executor, db, admin, sql):
    with pytest.raises(ExecutionError):
        executor.execute(admin, sql, source="manual_sql")


def test_a_real_timeout_is_still_a_query_timeout(settings, registry, auth, backups, db, admin):
    # Regression guard: expected to pass before the fix.
    executor = Executor(dataclasses.replace(settings, query_timeout_s=0.05), registry, auth,
                        backups)
    heavy = ("WITH RECURSIVE s(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM s "
             "WHERE i < 50000000) SELECT count(*) FROM s")
    with pytest.raises(QueryTimeout):
        executor.execute(admin, heavy, source="manual_sql")


# --- §6.7: linked backups folder and exclusive snapshot names (finding 3, lead) ---------


@POSIX
def test_a_linked_backups_folder_is_refused(settings, registry, auth, superadmin, db,
                                            tmp_path):
    service = BackupService(settings, locks=registry.locks, auth=auth)
    good = service.snapshot(db, "manual")
    elsewhere = tmp_path / "elsewhere"
    (settings.backups_dir / "sales").rename(elsewhere)
    (settings.backups_dir / "sales").symlink_to(elsewhere, target_is_directory=True)
    linked = settings.backups_dir / "sales" / good.name
    before = sorted(path.name for path in elsewhere.iterdir())
    with pytest.raises(InvalidIdentifier):
        service.restore(superadmin, "sales", linked)
    with pytest.raises(InvalidIdentifier):
        service.snapshot(db, "manual")
    assert sorted(path.name for path in elsewhere.iterdir()) == before  # nothing written


def test_two_snapshots_in_the_same_microsecond_never_share_a_file(
    settings, backups, db, monkeypatch
):
    # Seam: the clock is frozen for two calls, and Path.exists is made to miss the first
    # file, which is the gap between the old exists() check and the file's creation.
    stamps = iter(["20260101T000000_000000Z", "20260101T000000_000000Z",
                   "20260101T000000_000001Z", "20260101T000000_000002Z"])
    monkeypatch.setattr(backup_module, "utc_timestamp", lambda: next(stamps))
    folder = settings.backups_dir / "sales"
    real_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists",
        lambda self, *a, **k: False if self.parent == folder else real_exists(self, *a, **k),
    )
    first = backups.snapshot(db, "manual")
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM child")
    conn.commit()
    conn.close()
    second = backups.snapshot(db, "manual")
    assert first != second
    assert counts(first) == (3, 3)  # the first snapshot was not overwritten
    assert counts(second) == (3, 0)


# --- §3: file permissions (finding 4), POSIX only ----------------------------------------


@pytest.fixture
def env_data_dir(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)
    target = tmp_path / "data"
    monkeypatch.setenv("COALESCEDB_DATA_DIR", str(target))
    return target


@POSIX
def test_existing_loose_folders_are_tightened(env_data_dir, umask_022):
    for folder in (env_data_dir, env_data_dir / "databases", env_data_dir / "backups"):
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o755)
    settings = load_settings()
    for folder in (settings.data_dir, settings.databases_dir, settings.backups_dir):
        assert mode(folder) == 0o700, folder.name


@POSIX
def test_dev_mode_never_changes_the_current_folder(tmp_path, monkeypatch, umask_022):
    from coalescedb.config import check_data_dir_permissions

    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.chmod(0o755)
    monkeypatch.chdir(repo)
    settings = load_settings()
    assert settings.data_dir == repo.resolve()
    assert mode(repo) == 0o755  # left alone
    assert check_data_dir_permissions(settings) is True  # open: the wiring traces a warning
    repo.chmod(0o700)
    assert check_data_dir_permissions(settings) is False


@POSIX
def test_new_app_db_is_owner_only(settings, umask_022):
    AppStore(settings.app_db_path)
    assert mode(settings.app_db_path) == 0o600


@POSIX
def test_existing_app_db_and_its_log_are_tightened(settings, umask_022):
    path = settings.app_db_path
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("PRAGMA journal_mode = WAL")
    holder.execute("CREATE TABLE keep_open (a)")
    holder.execute("INSERT INTO keep_open VALUES (1)")
    sidecars = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm")]
    try:
        for file in (path, *sidecars):
            file.chmod(0o644)
        AppStore(path)
        for file in (path, *sidecars):
            assert mode(file) == 0o600, file.name
    finally:
        holder.close()


@POSIX
def test_database_files_and_their_logs_are_owner_only(registry, superadmin, admin, umask_022,
                                                      settings, auth, backups):
    path = registry.create(superadmin, "sales")
    assert mode(path) == 0o600
    build(path)
    executor = Executor(settings, registry, auth, backups)
    holder = sqlite3.connect(path)  # keeps the -wal and -shm in place to look at
    holder.execute("SELECT 1 FROM parent").fetchall()
    try:
        executor.execute(admin, "INSERT INTO parent (code, n) VALUES (?, ?)",
                         source="manual_sql", params=("q", 1), confirmed=True)
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists():
                assert mode(sidecar) == 0o600, suffix
    finally:
        holder.close()


@POSIX
def test_imported_database_is_owner_only(registry, superadmin, tmp_path, umask_022):
    source = tmp_path / "upload.db"
    build(source)
    source.chmod(0o644)
    path = registry.import_file(superadmin, source, "imported")
    assert mode(path) == 0o600


@POSIX
def test_snapshots_are_owner_only(backups, db, umask_022):
    assert mode(backups.snapshot(db, "manual")) == 0o600


@POSIX
def test_restored_database_is_owner_only(settings, registry, auth, superadmin, db, umask_022):
    # Regression guard for restore (its copy was already 0o600); the snapshot it takes is new.
    service = BackupService(settings, locks=registry.locks, auth=auth)
    backup = service.snapshot(db, "manual")
    service.restore(superadmin, "sales", backup)
    assert mode(db) == 0o600
    for snapshot in (settings.backups_dir / "sales").iterdir():
        assert mode(snapshot) == 0o600, snapshot.name


@POSIX
def test_backup_folders_are_owner_only(backups, settings, registry, superadmin, db, umask_022):
    # Regression guard: these folders were already created 0o700.
    backups.snapshot(db, "manual")
    registry.delete(superadmin, "sales", "sales")
    assert mode(settings.backups_dir / "_deleted") == 0o700
    with contextlib.suppress(StopIteration):
        moved = next((settings.backups_dir / "_deleted").iterdir())
        assert mode(moved) == 0o700

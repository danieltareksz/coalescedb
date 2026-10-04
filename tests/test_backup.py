"""Tests for BackupService.snapshot and pruning (PROJECT_SPEC.md §6.7).

list() and restore() are built in M5.
"""

import dataclasses
import re
import sqlite3

import pytest

from coalescedb.db.backup import BackupService
from coalescedb.errors import DatabaseNotFound, InvalidIdentifier

SNAPSHOT_NAME_RE = re.compile(r"^\d{8}T\d{6}_\d{6}Z_[a-z_]+\.db\Z")


def make_db(settings, name, rows=3):
    """A database file in databases_dir, built with plain sqlite3."""
    path = settings.databases_dir / (name + ".db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (n INTEGER)")
    conn.executemany("INSERT INTO items (n) VALUES (?)", [(n,) for n in range(rows)])
    conn.commit()
    conn.close()
    return path


def read_items(path):
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT n FROM items ORDER BY n")]
    finally:
        conn.close()


def tree(folder):
    return sorted(str(path.relative_to(folder)) for path in folder.rglob("*"))


# --- snapshot --------------------------------------------------------------------------


def test_snapshot_is_a_faithful_copy_in_the_databases_backup_folder(backups, settings):
    source = make_db(settings, "sales")
    snapshot = backups.snapshot(source, "manual")

    assert snapshot.parent == settings.backups_dir / "sales"
    assert SNAPSHOT_NAME_RE.fullmatch(snapshot.name)
    assert snapshot.name.endswith("_manual.db")
    assert snapshot.read_bytes()[:16] == b"SQLite format 3\x00"
    assert read_items(snapshot) == [0, 1, 2]
    assert read_items(source) == [0, 1, 2]  # the source is untouched


def test_snapshot_does_not_change_when_the_source_changes_later(backups, settings):
    source = make_db(settings, "sales")
    snapshot = backups.snapshot(source, "manual")
    conn = sqlite3.connect(source)
    conn.execute("DELETE FROM items")
    conn.commit()
    conn.close()
    assert read_items(snapshot) == [0, 1, 2]


def test_snapshot_includes_rows_still_in_the_write_ahead_log(backups, settings):
    source = make_db(settings, "sales")
    conn = sqlite3.connect(source)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("INSERT INTO items (n) VALUES (99)")
        conn.commit()  # committed, but still only in sales.db-wal while conn stays open
        snapshot = backups.snapshot(source, "manual")
    finally:
        conn.close()
    assert read_items(snapshot) == [0, 1, 2, 99]


def test_each_snapshot_gets_its_own_file(backups, settings):
    source = make_db(settings, "sales")
    first = backups.snapshot(source, "manual")
    second = backups.snapshot(source, "db_delete")
    assert first != second
    assert sorted(path.name for path in (settings.backups_dir / "sales").iterdir()) == sorted(
        [first.name, second.name]
    )


# --- pruning ---------------------------------------------------------------------------


def test_pruning_keeps_only_the_newest_backups_to_keep(settings):
    backups = BackupService(dataclasses.replace(settings, backups_to_keep=3))
    source = make_db(settings, "sales")
    taken = [backups.snapshot(source, "manual") for _ in range(5)]

    remaining = sorted(path.name for path in (settings.backups_dir / "sales").iterdir())
    assert remaining == sorted(path.name for path in taken[-3:])
    assert all(read_items(path) == [0, 1, 2] for path in taken[-3:])


def test_default_keeps_twenty(backups, settings):
    assert settings.backups_to_keep == 20
    source = make_db(settings, "sales", rows=1)
    for _ in range(22):
        backups.snapshot(source, "manual")
    assert len(list((settings.backups_dir / "sales").iterdir())) == 20


def test_pruning_never_touches_anything_else(settings):
    backups = BackupService(dataclasses.replace(settings, backups_to_keep=2))
    sales = make_db(settings, "sales")
    hr = make_db(settings, "hr")
    hr_snapshots = [backups.snapshot(hr, "manual") for _ in range(2)]

    sales_folder = settings.backups_dir / "sales"
    sales_folder.mkdir()
    (sales_folder / "notes.txt").write_text("keep me")
    (sales_folder / "subfolder").mkdir()
    (sales_folder / "subfolder" / "20200101T000000_000000Z_manual.db").write_bytes(b"old")
    (settings.backups_dir / "stray.db").write_bytes(b"stray")
    deleted = settings.backups_dir / "_deleted" / "sales_20200101T000000_000000Z"
    deleted.mkdir(parents=True)
    (deleted / "20200101T000000_000000Z_db_delete.db").write_bytes(b"old")

    taken = [backups.snapshot(sales, "manual") for _ in range(4)]

    assert all(path.exists() for path in hr_snapshots)
    assert (sales_folder / "notes.txt").read_text() == "keep me"
    assert (sales_folder / "subfolder" / "20200101T000000_000000Z_manual.db").exists()
    assert (settings.backups_dir / "stray.db").exists()
    assert (deleted / "20200101T000000_000000Z_db_delete.db").exists()
    snapshots_left = sorted(path.name for path in sales_folder.glob("*.db"))
    assert snapshots_left == sorted(path.name for path in taken[-2:])


# --- refusals: nothing is created ------------------------------------------------------


@pytest.mark.parametrize(
    "reason", ["", "Bad Reason", "../x", "x.y", "a" * 33, "UPPER", "a-b", None]
)
def test_bad_reason_is_refused_before_anything_is_created(backups, settings, reason):
    source = make_db(settings, "sales")
    with pytest.raises(ValueError):
        backups.snapshot(source, reason)
    assert tree(settings.backups_dir) == []


@pytest.mark.parametrize("file_name", ["Bad Name.db", "con.db", "1abc.db", "UPPER.db"])
def test_bad_database_name_is_refused_before_anything_is_created(
    backups, settings, tmp_path, file_name
):
    source = tmp_path / file_name
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE items (n INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(InvalidIdentifier):
        backups.snapshot(source, "manual")
    assert tree(settings.backups_dir) == []


def test_missing_source_raises_database_not_found(backups, settings):
    with pytest.raises(DatabaseNotFound):
        backups.snapshot(settings.databases_dir / "ghost.db", "manual")
    assert tree(settings.backups_dir) == []
    assert not (settings.databases_dir / "ghost.db").exists()

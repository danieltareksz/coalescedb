"""Security tests for DatabaseRegistry path handling (PROJECT_SPEC.md §6.3).

The registry is the only code that turns a database name into a file path. These tests
check that no name can make it point, create, delete or move anything outside
databases_dir.
"""

import sqlite3
import sys

import pytest

from coalescedb.errors import InvalidIdentifier

BAD_NAMES = [
    "../x",
    "x/../../y",
    "..",
    ".",
    "/etc/passwd",
    "/tmp/x",
    "x\\y",
    "C:\\x",
    "x.db",
    "CON",
    "Sales",  # uppercase
    "sAles",
    "ѕales",  # Cyrillic "ѕ": a unicode lookalike
    "ｓales",  # fullwidth "s"
    "abc\n",
    "a b",
    "x\x00y",
    "",
    "a" * 49,
    # Lowercase Windows reserved device names.
    "con",
    "prn",
    "aux",
    "nul",
    "com1",
    "lpt1",
    None,
]


def tree(tmp_path):
    """Every path under the test's temp folder, except app.db and its sidecar files."""
    return sorted(
        str(path.relative_to(tmp_path))
        for path in tmp_path.rglob("*")
        if not path.name.startswith("app.db")
    )


def make_sqlite_file(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (a)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return path


# --- path_for --------------------------------------------------------------------------


@pytest.mark.parametrize("name", BAD_NAMES)
def test_path_for_rejects_bad_names(registry, name):
    with pytest.raises(InvalidIdentifier):
        registry.path_for(name)


@pytest.mark.parametrize("name", ["a", "sales", "sales_2024", "console", "a" * 48])
def test_path_for_resolves_inside_databases_dir(registry, settings, name):
    path = registry.path_for(name)
    assert path == settings.databases_dir.resolve() / (name + ".db")
    assert path.parent == settings.databases_dir.resolve()
    assert not path.exists()  # path_for never creates anything


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
def test_symlink_pointing_outside_is_refused(registry, settings, superadmin, tmp_path):
    outside = make_sqlite_file(tmp_path / "outside.db")
    before = outside.read_bytes()
    (settings.databases_dir / "evil.db").symlink_to(outside)

    with pytest.raises(InvalidIdentifier):
        registry.path_for("evil")
    assert "evil" not in registry.list_databases()
    with pytest.raises(InvalidIdentifier):
        registry.create(superadmin, "evil")
    with pytest.raises(InvalidIdentifier):
        registry.delete(superadmin, "evil", "evil")
    with pytest.raises(InvalidIdentifier):
        registry.rename(superadmin, "evil", "other")
    assert outside.read_bytes() == before  # the file outside was never touched


# --- list_databases --------------------------------------------------------------------


def test_list_databases_is_empty_at_first(registry):
    assert registry.list_databases() == []


def test_list_databases_returns_only_valid_names_sorted(registry, settings, superadmin):
    registry.create(superadmin, "zeta")
    registry.create(superadmin, "alpha")
    folder = settings.databases_dir
    (folder / "notes.txt").write_text("not a database")
    (folder / "Bad Name.db").write_bytes(b"")
    (folder / "1abc.db").write_bytes(b"")
    (folder / "con.db").write_bytes(b"")
    (folder / "alpha.db.bak").write_bytes(b"")
    (folder / ".import-abc123.tmp").write_bytes(b"")
    (folder / "folder.db").mkdir()  # a directory, not a file
    assert registry.list_databases() == ["alpha", "zeta"]


# --- create / delete / rename / import_file never act on a bad name --------------------


@pytest.mark.parametrize("name", BAD_NAMES)
def test_lifecycle_methods_reject_bad_names(registry, superadmin, tmp_path, name):
    registry.create(superadmin, "sales")
    source = make_sqlite_file(tmp_path / "source.sqlite")
    before = tree(tmp_path)

    with pytest.raises(InvalidIdentifier):
        registry.create(superadmin, name)
    with pytest.raises(InvalidIdentifier):
        registry.delete(superadmin, name, name)
    with pytest.raises(InvalidIdentifier):
        registry.rename(superadmin, name, "other")
    with pytest.raises(InvalidIdentifier):
        registry.rename(superadmin, "sales", name)
    with pytest.raises(InvalidIdentifier):
        registry.import_file(superadmin, source, name)

    assert tree(tmp_path) == before  # nothing created, moved or removed anywhere
    assert registry.list_databases() == ["sales"]

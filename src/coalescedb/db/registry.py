"""Create, list, delete, rename and import database files safely (PROJECT_SPEC.md §6.3).

The registry is the only code that turns a database name into a file path. Every name is
validated first, and the resulting path must sit directly inside databases_dir.
"""

import contextlib
import os
import re
import secrets
import shutil
import sqlite3
from pathlib import Path

from coalescedb.auth.service import AuthService
from coalescedb.config import Settings
from coalescedb.db.backup import BackupService, utc_timestamp
from coalescedb.db.connection import open_internal_connection
from coalescedb.db.identifiers import validate_db_name
from coalescedb.errors import (
    DatabaseExists,
    DatabaseNotFound,
    IngestError,
    InvalidIdentifier,
    PermissionDenied,
)
from coalescedb.models import Role, User

_SQLITE_HEADER = b"SQLite format 3\x00"
_SIDECAR_SUFFIXES = ("-wal", "-shm")  # SQLite's write-ahead-log files, beside the .db
_DELETED_FOLDER = "_deleted"  # a leading "_" can never be a database name
_VIRTUAL_TABLE_RE = re.compile(r"^\s*CREATE\s+VIRTUAL\s+TABLE\b", re.IGNORECASE)


class DatabaseRegistry:
    def __init__(self, settings: Settings, auth: AuthService, backups: BackupService) -> None:
        self._settings = settings
        self._auth = auth
        self._backups = backups

    # --- paths -------------------------------------------------------------------------

    def path_for(self, db_name: str) -> Path:
        validate_db_name(db_name)
        root = self._settings.databases_dir.resolve()
        path = (root / f"{db_name}.db").resolve()
        if path.parent != root:  # defeats ../ and symlinks that point elsewhere
            raise InvalidIdentifier("That database name isn't allowed.")
        return path

    def list_databases(self) -> list[str]:
        names = []
        for entry in self._settings.databases_dir.iterdir():
            if entry.suffix != ".db" or not entry.is_file():
                continue
            try:
                self.path_for(entry.stem)
            except InvalidIdentifier:
                continue
            names.append(entry.stem)
        return sorted(names)

    # --- helpers -----------------------------------------------------------------------

    def _retire_backups(self, db_name: str) -> None:
        """Move backups_dir/<name>/ to backups_dir/_deleted/<name>_<UTC timestamp>/."""
        folder = self._settings.backups_dir / db_name
        if not folder.exists():
            return
        deleted = self._settings.backups_dir / _DELETED_FOLDER
        deleted.mkdir(mode=0o700, exist_ok=True)
        folder.rename(deleted / f"{db_name}_{utc_timestamp()}")

    @staticmethod
    def _sidecars(path: Path) -> list[Path]:
        return [path.with_name(path.name + suffix) for suffix in _SIDECAR_SUFFIXES]

    def _claim_name(self, actor: User, db_name: str) -> None:
        """Clear whatever is left over under a name that is about to come into use."""
        self._retire_backups(db_name)
        self._auth.revoke_all(actor, db_name)  # grant() can't check a database exists

    # --- lifecycle ---------------------------------------------------------------------

    def create(self, actor: User, db_name: str) -> Path:
        self._auth.require_superadmin(actor)
        path = self.path_for(db_name)
        if path.exists():
            raise DatabaseExists()

        self._claim_name(actor, db_name)
        try:
            conn = sqlite3.connect(path)
            try:
                conn.execute("PRAGMA journal_mode = WAL")  # also writes the file header
            finally:
                conn.close()
            self._auth.grant(actor, actor.id, db_name, Role.ADMIN)
            self._auth.audit(actor, "db_create", db_name, {}, "system")
        except BaseException:
            # Leave nothing behind, so the name stays free and create() can be retried.
            for file in (path, *self._sidecars(path)):
                file.unlink(missing_ok=True)
            raise
        return path

    def delete(self, actor: User, db_name: str, confirm_text: str) -> None:
        self._auth.require_superadmin(actor)
        if confirm_text != db_name:
            raise PermissionDenied("Type the database's name to confirm deleting it.")
        path = self.path_for(db_name)
        if not path.is_file():
            raise DatabaseNotFound()

        # If the snapshot raises, nothing below runs: nothing is deleted or revoked.
        self._backups.snapshot(path, "db_delete")
        self._auth.revoke_all(actor, db_name)
        for file in (path, *self._sidecars(path)):
            file.unlink(missing_ok=True)
        self._retire_backups(db_name)
        self._auth.audit(actor, "db_delete", db_name, {}, "system")

    def rename(self, actor: User, old: str, new: str) -> None:
        self._auth.require_superadmin(actor)
        old_path = self.path_for(old)
        new_path = self.path_for(new)
        # Everything is checked before anything moves.
        if not old_path.is_file():
            raise DatabaseNotFound()
        if new_path.exists() or (self._settings.backups_dir / new).exists():
            raise DatabaseExists()

        # Fold the write-ahead log into the main file first, on a connection opened only
        # for this, so the -wal/-shm files are normally gone before anything moves.
        self._checkpoint(old_path)

        backups_dir = self._settings.backups_dir
        moves = [(old_path, new_path)]
        moves += list(zip(self._sidecars(old_path), self._sidecars(new_path), strict=True))
        moves.append((backups_dir / old, backups_dir / new))
        completed: list[tuple[Path, Path]] = []
        try:
            for source, target in moves:
                if source.exists():
                    os.rename(source, target)
                    completed.append((source, target))
            # Grants last: if this fails, every move above is undone.
            self._auth.revoke_all(actor, new)  # clear stray grants so the move can't collide
            self._auth.rename_grants(actor, old, new)
        except BaseException:
            for source, target in reversed(completed):  # undo every move that was made
                with contextlib.suppress(OSError):
                    os.rename(target, source)
            raise
        self._auth.audit(actor, "db_rename", new, {"old": old, "new": new}, "system")

    @staticmethod
    def _checkpoint(path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            with contextlib.suppress(sqlite3.Error):  # busy or not in WAL mode: carry on
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    def import_file(self, actor: User, src: Path, db_name: str) -> Path:
        self._auth.require_superadmin(actor)
        path = self.path_for(db_name)
        if path.exists():
            raise DatabaseExists()
        src = Path(src)
        if not src.is_file():
            raise IngestError("That file couldn't be found.")

        # Work on a copy, under a name list_databases ignores. src is never modified.
        temp = self._settings.databases_dir / f".import-{secrets.token_hex(8)}.tmp"
        try:
            try:
                shutil.copyfile(src, temp)
                self._check_import(temp)
            except IngestError:
                raise
            except (OSError, sqlite3.Error):
                raise IngestError() from None

            # Only a file that passed every check gets this far.
            self._retire_backups(db_name)
            os.replace(temp, path)
        finally:
            for leftover in (temp, *self._sidecars(temp)):
                leftover.unlink(missing_ok=True)

        self._auth.revoke_all(actor, db_name)
        self._auth.grant(actor, actor.id, db_name, Role.ADMIN)
        self._auth.audit(actor, "db_import", db_name, {}, "system")
        return path

    @staticmethod
    def _check_import(path: Path) -> None:
        """Raise IngestError unless the file is a healthy SQLite database we can serve."""
        with path.open("rb") as file:
            if file.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
                raise IngestError("That file isn't a SQLite database.")

        with open_internal_connection(path) as conn:
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise IngestError("That database file is damaged.")
            objects = conn.execute(
                "SELECT type, name, rootpage, sql FROM sqlite_master"
            ).fetchall()

        # The authorizer blocks creating triggers and virtual tables, so a file that already
        # has them is refused, naming them.
        triggers = sorted(name for kind, name, _page, _sql in objects if kind == "trigger")
        # A virtual table has no storage of its own, so SQLite records rootpage 0 for it.
        # That doesn't depend on the stored SQL text, which a crafted file can disguise;
        # the text check is kept as a second signal.
        virtual = sorted(
            name
            for kind, name, rootpage, sql in objects
            if kind == "table" and (not rootpage or _VIRTUAL_TABLE_RE.match(sql or ""))
        )
        if triggers:
            raise IngestError(
                "That database contains triggers, which aren't supported: " + ", ".join(triggers)
            )
        if virtual:
            raise IngestError(
                "That database contains virtual tables, which aren't supported: "
                + ", ".join(virtual)
            )

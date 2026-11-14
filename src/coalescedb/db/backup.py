"""Database snapshots (PROJECT_SPEC.md §6.7).

snapshot() and pruning are built in M3 because the registry's delete() needs them.
list() and restore() arrive in M5.
"""

import contextlib
import os
import re
import secrets
import shutil
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from coalescedb.config import Settings, create_private_file
from coalescedb.db.connection import open_internal_connection
from coalescedb.db.identifiers import validate_db_name
from coalescedb.db.locks import DatabaseLocks
from coalescedb.errors import (
    DatabaseInUse,
    DatabaseNotFound,
    ExecutionError,
    IngestError,
    InvalidIdentifier,
    PermissionDenied,
)
from coalescedb.models import Role, User
from coalescedb.observability.tracing import trace

if TYPE_CHECKING:  # type only: backup.py does not import auth/ at run time
    from coalescedb.auth.service import AuthService

_SQLITE_HEADER = b"SQLite format 3\x00"

_REASON_RE = re.compile(r"^[a-z_]{1,32}\Z")
_SNAPSHOT_NAME_RE = re.compile(r"^\d{8}T\d{6}_\d{6}Z_[a-z_]{1,32}\.db\Z")


def utc_timestamp() -> str:
    """UTC now as <YYYYMMDD>T<HHMMSS>_<microseconds>Z. No colons: Windows forbids them."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")


class BackupService:
    def __init__(
        self,
        settings: Settings,
        *,
        locks: DatabaseLocks | None = None,
        in_use: Callable[..., int] | None = None,
        auth: "AuthService | None" = None,
    ) -> None:
        self._settings = settings
        # None: its own locks, so the class works alone. The app's wiring passes the one
        # shared object (§6.3).
        self.locks = DatabaseLocks() if locks is None else locks
        self._in_use = in_use  # the wiring passes auth.sessions_using; None: no check
        self._auth = auth  # restore() only; None: restore() refuses
        # One snapshot (copy and prune) at a time, so a prune never removes a file that
        # another snapshot of this process is still writing or has just made.
        self._snapshot_lock = threading.Lock()

    def snapshot(self, db_path: Path, reason: str) -> Path:
        """Copy a database into backups_dir/<db>/<timestamp>_<reason>.db, then prune."""
        db_path = Path(db_path)
        if not isinstance(reason, str) or _REASON_RE.fullmatch(reason) is None:
            raise ValueError("reason must be 1 to 32 lowercase letters or underscores")
        if db_path.suffix != ".db":
            raise InvalidIdentifier("That database name isn't allowed.")
        # Validated before the folder path is built, so the name can't steer it elsewhere.
        db_name = validate_db_name(db_path.stem)
        if not db_path.is_file():
            raise DatabaseNotFound()

        folder = self._backup_folder(db_name)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_folder(folder)  # again: mkdir follows a link made in between
        with self._snapshot_lock:
            target = self._new_snapshot_file(folder, reason)
            # Connection.backup() copies a consistent picture of the database, including
            # rows that are still only in its write-ahead log.
            source = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                destination = sqlite3.connect(target)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            finally:
                source.close()
            self._prune(folder)
        return target

    def _backup_folder(self, db_name: str) -> Path:
        """backups_dir/<db_name>, refused if it is a link or resolves anywhere else (§6.7)."""
        folder = self._settings.backups_dir / db_name
        self._check_folder(folder)
        return folder

    def _check_folder(self, folder: Path) -> None:
        refused = InvalidIdentifier("That backup folder can't be used.")
        if folder.is_symlink():
            raise refused
        if folder.exists() and folder.resolve().parent != self._settings.backups_dir.resolve():
            raise refused

    @staticmethod
    def _new_snapshot_file(folder: Path, reason: str) -> Path:
        """Create an empty, owner-only snapshot file under a name nobody else has taken."""
        while True:
            target = folder / f"{utc_timestamp()}_{reason}.db"
            # Exclusive create: if another snapshot took this name in the same microsecond,
            # it fails and the next timestamp is tried. Owner only (§3).
            if create_private_file(target):
                return target

    def _prune(self, folder: Path) -> None:
        """Keep the newest backups_to_keep snapshots in one database's folder."""
        # Only regular files directly inside this folder whose names look like snapshots:
        # never subfolders, links, other files, or anything outside.
        snapshots = sorted(
            path
            for path in folder.iterdir()
            if _SNAPSHOT_NAME_RE.fullmatch(path.name) and path.is_file() and not path.is_symlink()
        )
        excess = len(snapshots) - max(1, self._settings.backups_to_keep)
        for old in snapshots[: max(0, excess)]:  # names start with the timestamp: oldest first
            old.unlink()

    def list(self, db_name: str) -> list[tuple[datetime, str, Path]]:
        """(UTC time, reason, path) of a database's snapshots, newest first."""
        folder = self._settings.backups_dir / validate_db_name(db_name)
        if not folder.is_dir():
            return []
        found = []
        for path in folder.iterdir():
            if (
                _SNAPSHOT_NAME_RE.fullmatch(path.name) is None
                or path.is_symlink()
                or not path.is_file()
            ):
                continue
            stamp, reason = path.stem.split("Z_", 1)
            when = datetime.strptime(stamp + "Z", "%Y%m%dT%H%M%S_%fZ").replace(tzinfo=UTC)
            found.append((when, reason, path))
        return sorted(found, key=lambda item: item[2].name, reverse=True)

    def restore(
        self, actor: User, db_name: str, backup_path: Path, *, session_id: str | None = None
    ) -> None:
        """Replace a database with one of its own snapshots (§6.7)."""
        validate_db_name(db_name)
        # 1. Only an admin of this database, and only if the service is wired to check.
        if self._auth is None or self._auth.role_for(actor, db_name) is not Role.ADMIN:
            raise PermissionDenied("Only an admin of this database can restore a backup.")
        # 2. The backup must be one of this database's own snapshots.
        backup = self._check_backup_path(db_name, backup_path)
        path = self._settings.databases_dir.resolve() / f"{db_name}.db"
        if not path.is_file():
            raise DatabaseNotFound()
        # 3. Other sessions have it open: refuse early, with a number.
        if self._in_use is not None:
            others = self._in_use(db_name, exclude_session_id=session_id)
            if others:
                raise DatabaseInUse(db_name=db_name, active_sessions=others)
        # 4. Nobody may start using it while the file is replaced.
        with self.locks.exclusive(db_name):
            self._replace(actor, db_name, backup, path)

    def _check_backup_path(self, db_name: str, backup_path: Path) -> Path:
        given = Path(backup_path)
        refused = InvalidIdentifier("That backup can't be used.")
        # The link check comes first: resolve() follows links, so a link inside the folder
        # that points at a real snapshot would otherwise pass.
        if given.is_symlink():
            raise refused
        try:
            folder = self._backup_folder(db_name).resolve()
        except InvalidIdentifier:
            raise refused from None
        resolved = given.resolve()
        if resolved.parent != folder or _SNAPSHOT_NAME_RE.fullmatch(resolved.name) is None:
            raise refused
        if not resolved.is_file():
            raise refused
        return resolved

    def _replace(self, actor: User, db_name: str, backup: Path, path: Path) -> None:
        # 5. Copy first: the pre_restore snapshot prunes and could delete this very backup.
        copy = self._settings.databases_dir / f".restore-{secrets.token_hex(8)}.tmp"
        try:
            create_private_file(copy)  # owner only before any byte is copied (§3)
            shutil.copyfile(backup, copy)
            # 6. Check the copy before anything is replaced.
            try:
                self._check_copy(copy)
            finally:
                # The read-only check can leave a log and index file it cannot remove itself.
                self._remove_sidecars(copy)
            # 7. Through self, so a replaced attribute or a subclass sees it.
            self.snapshot(path, reason="pre_restore")
            # 8. A stale log must never be applied to the restored file.
            for suffix in ("-wal", "-shm"):  # not suppressed: a stale log must really go
                path.with_name(path.name + suffix).unlink(missing_ok=True)
            os.replace(copy, path)
        finally:
            with contextlib.suppress(OSError):
                copy.unlink(missing_ok=True)
            self._remove_sidecars(copy)
        # 9. The file name only: never a path. The file is already replaced, so a failing
        # audit write must never look like a failed restore (DatabaseBusy would invite a
        # second restore over the restored data).
        try:
            self._auth.audit(actor, "db_restore", db_name, {"backup": backup.name}, "system")
        except Exception:
            with trace("audit_failed", method="restore", db_name=db_name, user_id=actor.id):
                pass
            raise ExecutionError(
                "The database was restored, but it could not be recorded in the audit log. "
                "Tell your administrator."
            ) from None

    @staticmethod
    def _remove_sidecars(path: Path) -> None:
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(OSError):
                path.with_name(path.name + suffix).unlink(missing_ok=True)

    @staticmethod
    def _check_copy(copy: Path) -> None:
        damaged = IngestError("That backup is damaged and can't be restored.")
        with copy.open("rb") as file:
            if file.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
                raise damaged
        try:
            with open_internal_connection(copy) as conn:
                result = conn.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError:
            raise damaged from None
        if result != [("ok",)]:
            raise damaged

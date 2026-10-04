"""Database snapshots (PROJECT_SPEC.md §6.7).

snapshot() and pruning are built in M3 because the registry's delete() needs them.
list() and restore() arrive in M5.
"""

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from coalescedb.config import Settings
from coalescedb.db.identifiers import validate_db_name
from coalescedb.errors import DatabaseNotFound, InvalidIdentifier

_REASON_RE = re.compile(r"^[a-z_]{1,32}\Z")
_SNAPSHOT_NAME_RE = re.compile(r"^\d{8}T\d{6}_\d{6}Z_[a-z_]{1,32}\.db\Z")


def utc_timestamp() -> str:
    """UTC now as <YYYYMMDD>T<HHMMSS>_<microseconds>Z. No colons: Windows forbids them."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")


class BackupService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

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

        folder = self._settings.backups_dir / db_name
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = folder / f"{utc_timestamp()}_{reason}.db"
        while target.exists():  # two snapshots in the same microsecond
            target = folder / f"{utc_timestamp()}_{reason}.db"

        # Connection.backup() copies a consistent picture of the database, including rows
        # that are still only in its write-ahead log.
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

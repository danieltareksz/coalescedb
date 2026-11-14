"""Maintenance locks and the one-process-per-data-folder lock (PROJECT_SPEC.md §6.3).

DatabaseLocks lives in memory, so it protects only if a single process uses the data folder;
acquire_instance_lock makes sure of that with an OS file lock.
"""

import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Self

from coalescedb.config import Settings
from coalescedb.errors import AlreadyRunning, DatabaseBusy

_SHARED_REFUSED = (
    "Database busy. It is being renamed, restored or deleted. Try again in a moment."
)
_EXCLUSIVE_REFUSED = "Database busy. It is still in use. Try again in a moment."


class DatabaseLocks:
    """One object per process. Thread-safe: one threading.Lock guards its state."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._shared: dict[str, int] = {}
        self._exclusive: set[str] = set()

    @contextmanager
    def shared(self, db_name: str) -> Iterator[None]:
        """Held for the length of ONE operation. Any number of holders at once.

        Refused at once (never waits) while the exclusive lock is held.
        """
        with self._guard:
            if db_name in self._exclusive:
                raise DatabaseBusy(_SHARED_REFUSED)
            self._shared[db_name] = self._shared.get(db_name, 0) + 1
        try:
            yield
        finally:
            with self._guard:
                self._shared[db_name] -= 1
                if self._shared[db_name] == 0:
                    del self._shared[db_name]

    @contextmanager
    def exclusive(self, db_name: str) -> Iterator[None]:
        """Held by delete, rename and restore. Never waits: refused at once if anyone holds
        a shared or exclusive lock on the same name."""
        with self._guard:
            if db_name in self._exclusive or self._shared.get(db_name, 0) > 0:
                raise DatabaseBusy(_EXCLUSIVE_REFUSED)
            self._exclusive.add(db_name)
        try:
            yield
        finally:
            with self._guard:
                self._exclusive.discard(db_name)


class InstanceLock:
    """Holds the open lock file. release() unlocks and closes it; also a context manager."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def _lock(fd: int) -> None:
    """Take the OS lock without waiting. Raises OSError (BlockingIOError on POSIX) if held."""
    if sys.platform == "win32":  # first tested on Windows CI at M12
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def acquire_instance_lock(settings: Settings) -> InstanceLock:
    """Take the lock on settings.instance_lock_path and hold it until release or exit.

    The file is created with mode 0o600, never written to and never deleted. "Held" is
    recognised by the exception type (BlockingIOError), never by an errno number.
    """
    if settings.instance_lock_path is None:
        raise ValueError("settings.instance_lock_path is not set")
    fd = os.open(Path(settings.instance_lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _lock(fd)
    except OSError as exc:
        os.close(fd)
        # On POSIX only BlockingIOError means "held"; anything else is a real error.
        if sys.platform != "win32" and not isinstance(exc, BlockingIOError):
            raise
        raise AlreadyRunning() from exc
    return InstanceLock(fd)

"""In-memory maintenance locks for user databases (PROJECT_SPEC.md §6.3).

Round 1 of M5 builds only what the Executor needs: the shared lock. The exclusive lock,
`InstanceLock` and `acquire_instance_lock` arrive in round 2.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class DatabaseLocks:
    """One object per process. Thread-safe: one threading.Lock guards its state."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._shared: dict[str, int] = {}

    @contextmanager
    def shared(self, db_name: str) -> Iterator[None]:
        """Held for the length of one operation on a database. Any number of holders."""
        with self._guard:
            self._shared[db_name] = self._shared.get(db_name, 0) + 1
        try:
            yield
        finally:
            with self._guard:
                self._shared[db_name] -= 1
                if self._shared[db_name] == 0:
                    del self._shared[db_name]

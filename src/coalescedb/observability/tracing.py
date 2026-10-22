"""Trace lines for guard decisions, executions and LLM calls (PROJECT_SPEC.md §6.17).

One JSON line per trace in traces.jsonl. Only the attributes the caller passes are written;
callers never pass SQL text, parameter values, result rows or error messages. The error
field holds the exception's class name only.

Known limitation: the file is never rotated or trimmed.
"""

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

_lock = threading.Lock()
_path: Path | None = None  # None means tracing is off


def configure_tracing(path: Path | None) -> None:
    """Choose where trace lines go. None turns tracing off."""
    global _path
    with _lock:
        _path = None if path is None else Path(path)


def _append(line: str) -> None:
    with _lock:
        if _path is None:
            return
        try:
            # 0o600 applies when the file is created; Windows ignores the bits.
            fd = os.open(_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as file:
                file.write(line + "\n")
        except OSError:
            pass  # a full disk or a missing folder must not break the action being traced


@contextmanager
def trace(event: str, **attrs) -> Iterator[dict]:
    """Time a block and append one line when it ends. The yielded dict can be filled in."""
    started = time.perf_counter()
    error: str | None = None
    try:
        yield attrs
    except BaseException as exc:
        error = type(exc).__name__  # never the message: it can quote SQL or values
        raise
    finally:
        if _path is not None:
            line = {
                "ts": datetime.now(UTC).isoformat(),
                "event": event,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "attrs": attrs,
                "error": error,
            }
            _append(json.dumps(line, default=str))


def recent(n: int = 100) -> list[dict]:
    """The last n trace lines, oldest first. [] when tracing is off or nothing is written."""
    if n < 1:
        return []
    with _lock:
        path = _path
        if path is None or not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[-n:]]

"""Tests for observability/tracing.py (PROJECT_SPEC.md §6.17)."""

import json
import sys
import threading

import pytest

from coalescedb.observability.tracing import configure_tracing, recent, trace


@pytest.fixture
def trace_file(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure_tracing(path)
    yield path
    configure_tracing(None)


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_one_json_line_per_trace_with_the_fixed_keys(trace_file):
    with trace("guard", allowed=True, kind="select"):
        pass
    (line,) = lines(trace_file)
    assert set(line) == {"ts", "event", "elapsed_ms", "attrs", "error"}
    assert line["event"] == "guard"
    assert line["attrs"] == {"allowed": True, "kind": "select"}
    assert line["error"] is None
    assert line["elapsed_ms"] >= 0


def test_attributes_can_be_added_inside_the_block(trace_file):
    with trace("execute", method="execute") as attrs:
        attrs["row_count"] = 7
    (line,) = lines(trace_file)
    assert line["attrs"] == {"method": "execute", "row_count": 7}


def test_an_error_is_recorded_by_class_name_only_and_re_raised(trace_file):
    with pytest.raises(ValueError, match="secret detail"), trace("execute"):
        raise ValueError("secret detail")
    (line,) = lines(trace_file)
    assert line["error"] == "ValueError"
    assert "secret detail" not in trace_file.read_text()


def test_nothing_is_written_while_tracing_is_off(tmp_path, monkeypatch):
    configure_tracing(None)
    monkeypatch.chdir(tmp_path)
    with trace("guard", allowed=False):
        pass
    assert list(tmp_path.iterdir()) == []
    assert recent() == []


def test_recent_returns_the_last_lines_oldest_first(trace_file):
    for n in range(5):
        with trace("guard", n=n):
            pass
    assert [line["attrs"]["n"] for line in recent(3)] == [2, 3, 4]
    assert len(recent()) == 5


def test_recent_is_empty_when_the_file_does_not_exist_yet(trace_file):
    assert not trace_file.exists()
    assert recent() == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_trace_file_is_private(trace_file):
    with trace("guard"):
        pass
    assert trace_file.stat().st_mode & 0o777 == 0o600


def test_threads_never_interleave_lines(trace_file):
    def work(n):
        for _ in range(20):
            with trace("execute", worker=n, padding="x" * 500):
                pass

    threads = [threading.Thread(target=work, args=(n,)) for n in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    parsed = lines(trace_file)  # every line must be complete JSON
    assert len(parsed) == 200

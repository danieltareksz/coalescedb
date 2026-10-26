"""Fixes from the two independent M5 reviews (PROJECT_SPEC.md §6.7 step 9, §6.17).

1. restore: a failing audit write after the file is replaced raises ExecutionError (never
   DatabaseBusy) and the database IS restored.
2. dry_run: its trace line carries the same always-present keys as every other call.
3. tracing.recent(): a line that is not valid JSON is skipped, not raised.
"""

import json
import sqlite3

import pytest

from coalescedb.db.backup import BackupService
from coalescedb.db.executor import Executor
from coalescedb.errors import DatabaseBusy, ExecutionError, SQLRejected
from coalescedb.models import Role, Session
from coalescedb.observability.tracing import configure_tracing, recent, trace

RESTORED_NOT_AUDITED = (
    "The database was restored, but it could not be recorded in the audit log. "
    "Tell your administrator."
)
ALWAYS_KEYS = {"method", "source", "db_name", "user_id", "statement_count", "busy_retries_used"}
EXECUTE_KEYS = ALWAYS_KEYS | {"kind", "row_count", "truncated"}


# --- helpers ---------------------------------------------------------------------------


def make_items(path, values):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, n INTEGER NOT NULL)")
    conn.executemany("INSERT INTO items (n) VALUES (?)", [(v,) for v in values])
    conn.commit()
    conn.close()


def set_items(path, values):
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM items")
    conn.executemany("INSERT INTO items (n) VALUES (?)", [(v,) for v in values])
    conn.commit()
    conn.close()


def item_values(path):
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT n FROM items ORDER BY id")]
    finally:
        conn.close()


@pytest.fixture
def traces(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure_tracing(path)
    yield path
    configure_tracing(None)


@pytest.fixture
def db(registry, superadmin):
    path = registry.create(superadmin, "sales")
    make_items(path, [0, 1, 2])
    return path


# --- 1. restore with a failing audit write (§6.7 step 9) -------------------------------


@pytest.mark.parametrize(
    "failure",
    [DatabaseBusy(), sqlite3.OperationalError("disk I/O error")],
    ids=["busy", "other"],
)
def test_restore_with_a_failing_audit_still_restores_and_says_so(
    settings, registry, auth, superadmin, db, traces, monkeypatch, failure
):
    service = BackupService(settings, locks=registry.locks, auth=auth)
    backup = service.snapshot(db, "manual")
    set_items(db, [50])
    real_audit = auth.audit

    def audit(user, action, db_name, detail, source, **kwargs):
        if action == "db_restore":
            raise failure
        return real_audit(user, action, db_name, detail, source, **kwargs)

    monkeypatch.setattr(auth, "audit", audit)
    with pytest.raises(ExecutionError) as excinfo:
        service.restore(superadmin, "sales", backup)
    assert not isinstance(excinfo.value, DatabaseBusy)
    assert excinfo.value.user_message == RESTORED_NOT_AUDITED
    assert item_values(db) == [0, 1, 2]  # the database IS restored
    failed = [line for line in recent(1000) if line["event"] == "audit_failed"]
    assert [line["attrs"] for line in failed] == [
        {"method": "restore", "db_name": "sales", "user_id": superadmin.id}
    ]


# --- 2. dry_run's trace line (§6.17) ---------------------------------------------------


def execute_lines(method):
    return [
        line for line in recent(1000)
        if line["event"] == "execute" and line["attrs"].get("method") == method
    ]


@pytest.fixture
def executor(settings, registry, auth, backups):
    return Executor(settings, registry, auth, backups)


@pytest.fixture
def admin(superadmin):
    return Session(user=superadmin, db_name="sales", role=Role.ADMIN)


def test_dry_run_trace_line_has_every_always_present_key(executor, db, admin, traces):
    executor.dry_run(admin, "SELECT n FROM items")
    (line,) = execute_lines("dry_run")
    attrs = line["attrs"]
    assert ALWAYS_KEYS | {"kind"} <= set(attrs) <= EXECUTE_KEYS
    assert attrs["source"] == "nl"
    assert attrs["statement_count"] == 1
    assert attrs["busy_retries_used"] == 0
    assert attrs["db_name"] == "sales"
    assert attrs["user_id"] == admin.user.id
    assert attrs["kind"] == "select"


def test_refused_dry_run_trace_line_still_has_the_always_present_keys(executor, db, admin,
                                                                      traces):
    with pytest.raises(SQLRejected):
        executor.dry_run(admin, "PRAGMA table_info(items)")
    (line,) = execute_lines("dry_run")
    assert ALWAYS_KEYS <= set(line["attrs"]) <= EXECUTE_KEYS
    assert line["error"] == "SQLRejected"


def test_dry_run_and_execute_lines_share_the_always_present_keys(executor, db, admin, traces):
    # The two paths must not drift apart again.
    executor.dry_run(admin, "SELECT n FROM items")
    executor.execute(admin, "SELECT n FROM items", source="manual_sql")
    for method in ("dry_run", "execute"):
        (line,) = execute_lines(method)
        assert ALWAYS_KEYS <= set(line["attrs"]), method


# --- 3. recent() skips lines that are not valid JSON (§6.17) ---------------------------


def write_good_lines(count):
    for n in range(count):
        with trace("guard", n=n):
            pass


def test_recent_skips_a_truncated_last_line(traces):
    write_good_lines(3)
    with traces.open("a", encoding="utf-8") as file:
        file.write('{"ts": "2026-10-26T10:00:00+00:00", "event": "gua')  # cut short
    assert [line["attrs"]["n"] for line in recent()] == [0, 1, 2]


def test_recent_counts_only_valid_lines(traces):
    write_good_lines(3)
    with traces.open("a", encoding="utf-8") as file:
        file.write("not json at all\n")
    write_good_lines(1)  # n = 0 again, after the bad line
    with traces.open("a", encoding="utf-8") as file:
        file.write('{"half": ')
    assert [line["attrs"]["n"] for line in recent(3)] == [1, 2, 0]


def test_recent_with_only_bad_lines_is_empty(traces):
    traces.write_text("garbage\n{\n", encoding="utf-8")
    assert recent() == []


def test_good_lines_still_parse_as_written(traces):
    # Regression guard: expected to pass before the fix.
    write_good_lines(2)
    assert [json.loads(line)["event"] for line in traces.read_text().splitlines()] == [
        "guard", "guard",
    ]

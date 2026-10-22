"""Canaries, layered defence and offline use of the Executor (PROJECT_SPEC.md §0.2, §6.6,
§6.17, §10, §11.1).

A canary is a unique string put where secret data goes (a password, a bound parameter, a
value stored in the database). It must never turn up in the audit log, the traces or an
error message.
"""

import dataclasses
import json
import socket
import sqlite3

import pytest

import coalescedb.db.executor as executor_module
from coalescedb.db.executor import Executor
from coalescedb.errors import CoalesceDBError, ExecutionError, RowConflict
from coalescedb.models import GuardResult, Role, Session, StatementKind
from coalescedb.observability.tracing import configure_tracing, recent
from coalescedb.security.sql_guard import validate

PASSWORD_CANARY = "pw-CANARY-7f3a91"
PARAM_CANARY = "param-CANARY-52c0de"
RESULT_CANARY = "result-CANARY-9e41b7"
LITERAL_CANARY = "literal-CANARY-0d17aa"
LITERAL_SQL = "SELECT 'literal-CANARY-0d17aa' AS x"  # the canary as a SQL literal

GUARD_TRACE_KEYS = {
    "allowed", "kind", "is_destructive", "reason_count", "sql_chars", "tables_touched",
}
EXECUTE_TRACE_KEYS = {
    "method", "source", "db_name", "user_id", "kind", "statement_count", "row_count",
    "truncated", "busy_retries_used",
}


# --- fixtures --------------------------------------------------------------------------


@pytest.fixture
def traces(settings):
    configure_tracing(settings.traces_path)
    yield settings.traces_path
    configure_tracing(None)


@pytest.fixture
def executor(settings, registry, auth, backups):
    return Executor(settings, registry, auth, backups)


@pytest.fixture
def db(registry, superadmin):
    path = registry.create(superadmin, "sales")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, n INTEGER NOT NULL, label TEXT UNIQUE)"
    )
    conn.execute("INSERT INTO items (n, label) VALUES (1, ?)", (RESULT_CANARY,))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def admin(superadmin):
    return Session(user=superadmin, db_name="sales", role=Role.ADMIN)


@pytest.fixture
def viewer(auth, superadmin, db):
    user = auth.create_user(superadmin, "viewer_1", "viewer-pass-22")
    auth.grant(superadmin, user.id, "sales", Role.VIEWER)
    return Session(user=user, db_name="sales", role=Role.VIEWER)


def count(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM items").fetchone()[0]
    finally:
        conn.close()


def app_db_bytes(settings):
    """app.db and its write-ahead log, where recent audit rows live."""
    folder = settings.app_db_path.parent
    return b"".join(
        path.read_bytes() for path in folder.iterdir() if path.name.startswith("app.db")
    )


def exercise(executor, auth, admin, errors):
    """Run every Executor path with the canaries in play; keep each error raised."""
    auth.login("root_admin", PASSWORD_CANARY)
    with pytest.raises(CoalesceDBError) as excinfo:
        auth.login("root_admin", PASSWORD_CANARY + "-wrong")
    errors.append(excinfo.value)

    executor.execute(admin, "SELECT label FROM items", source="manual_sql")
    executor.execute(admin, LITERAL_SQL, source="manual_sql")
    executor.execute(admin, "INSERT INTO items (n, label) VALUES (?, ?)", source="manual_sql",
                     params=(2, PARAM_CANARY), confirmed=True)
    executor.execute_many(admin, "INSERT INTO items (n, label) VALUES (?, ?)",
                          [(3, PARAM_CANARY + "-many")], source="ingest", confirmed=True)
    executor.apply_changes(admin, [("UPDATE items SET label = ? WHERE n = ?",
                                    (PARAM_CANARY + "-edit", 3))],
                           source="editor", confirmed=True)
    executor.preview(admin, "SELECT label FROM items")
    executor.dry_run(admin, "SELECT label FROM items")

    failing = [
        lambda: executor.execute(admin, "INSERT INTO items (n, label) VALUES (?, ?)",
                                 source="manual_sql", params=(4, RESULT_CANARY),
                                 confirmed=True),  # UNIQUE violation on a stored value
        lambda: executor.apply_changes(admin, [("UPDATE items SET label = ? WHERE n = ?",
                                                (PARAM_CANARY, 999))],
                                       source="editor", confirmed=True),  # RowConflict
    ]
    for fail in failing:
        with pytest.raises((ExecutionError, RowConflict)) as excinfo:
            fail()
        errors.append(excinfo.value)


# --- canaries --------------------------------------------------------------------------


@pytest.fixture
def password_user(auth, superadmin):
    # The superadmin's own password becomes the canary.
    auth.change_password(superadmin, superadmin.id, PASSWORD_CANARY,
                         current_password="super-secret-pw-1")
    return superadmin


def test_canaries_never_reach_the_audit_log_traces_or_errors(
    executor, auth, settings, traces, db, admin, password_user
):
    errors = []
    exercise(executor, auth, admin, errors)

    stored = app_db_bytes(settings)
    traced = traces.read_text()
    audited = json.dumps(auth.read_audit(password_user, limit=10_000))
    for canary in (PASSWORD_CANARY, PARAM_CANARY, RESULT_CANARY):
        assert canary.encode() not in stored, canary
        assert canary not in traced, canary
        assert canary not in audited, canary
    for error in errors:
        text = " ".join([str(error), error.user_message, getattr(error, "sqlite_message", "")])
        for canary in (PASSWORD_CANARY, PARAM_CANARY, RESULT_CANARY):
            assert canary not in text, (type(error).__name__, canary)


def test_password_canary_is_not_in_backups(executor, auth, settings, db, admin, password_user):
    executor.execute(admin, "DELETE FROM items", source="manual_sql", confirmed=True,
                     destructive_confirm_text="sales")
    for path in settings.backups_dir.rglob("*"):
        if path.is_file():
            assert PASSWORD_CANARY.encode() not in path.read_bytes(), path.name


def test_traces_hold_no_sql_text_and_only_the_allowed_attributes(executor, traces, db, admin):
    executor.execute(admin, LITERAL_SQL, source="manual_sql")
    executor.execute(admin, "INSERT INTO items (n) VALUES (?)", source="manual_sql",
                     params=(5,), confirmed=True)
    assert LITERAL_CANARY not in traces.read_text()  # SQL text is never traced
    lines = recent(1000)
    events = {line["event"] for line in lines}
    assert {"guard", "execute"} <= events
    for line in lines:
        if line["event"] == "guard":
            assert set(line["attrs"]) <= GUARD_TRACE_KEYS
        if line["event"] == "execute":
            assert set(line["attrs"]) <= EXECUTE_TRACE_KEYS
        assert not any("hash" in key or "sha" in key for key in line["attrs"])


# --- the layers below the guard still hold (§0.2) --------------------------------------


def allow_everything(kind):
    """A broken guard that approves any SQL as `kind`."""

    def fake_validate(sql, role, known_tables, *, max_sql_chars=10_000):
        return GuardResult(allowed=True, kind=kind, normalized_sql=sql, reasons=[],
                           is_destructive=False, tables_touched=[])

    return fake_validate


def test_viewer_write_is_refused_by_the_connection_when_the_guard_fails(
    executor, db, viewer, monkeypatch
):
    monkeypatch.setattr(executor_module, "validate", allow_everything(StatementKind.INSERT))
    with pytest.raises(ExecutionError):
        executor.execute(viewer, "INSERT INTO items (n) VALUES (9)", source="manual_sql",
                         confirmed=True)
    assert count(db) == 1


@pytest.mark.parametrize(
    "sql",
    ["ATTACH DATABASE ':memory:' AS other", "SELECT load_extension('x')"],
)
def test_admin_is_refused_by_the_authorizer_when_the_guard_fails(
    executor, db, admin, monkeypatch, sql
):
    monkeypatch.setattr(executor_module, "validate", allow_everything(StatementKind.SELECT))
    with pytest.raises(ExecutionError) as excinfo:
        executor.execute(admin, sql, source="manual_sql")
    assert "not authorized" in excinfo.value.sqlite_message


def test_no_decision_is_taken_from_tables_touched(executor, db, admin, monkeypatch):
    def validate_without_tables(sql, role, known_tables, *, max_sql_chars=10_000):
        result = validate(sql, role, known_tables, max_sql_chars=max_sql_chars)
        return dataclasses.replace(result, tables_touched=[])

    monkeypatch.setattr(executor_module, "validate", validate_without_tables)
    result = executor.execute(admin, "SELECT label FROM items", source="manual_sql")
    assert result.rows == [(RESULT_CANARY,)]


# --- offline (§0.6, §10) ---------------------------------------------------------------


def test_executor_makes_no_network_connection(executor, auth, db, admin, password_user,
                                               monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network use")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    errors = []
    exercise(executor, auth, admin, errors)
    executor.apply_schema(admin, ["CREATE TABLE extra (a INTEGER)"], source="designer",
                          confirmed=True)

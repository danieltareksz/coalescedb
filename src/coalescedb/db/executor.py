"""The only code that runs SQL against user databases (PROJECT_SPEC.md §6.6).

Every feature goes through here, so the role check, the guard, the confirmations, the
backups and the audit log apply everywhere. Values reach SQLite only as bound parameters.

The one place SQL text is joined is `dry_run`: the fixed keyword "EXPLAIN " in front of SQL
the guard has already approved. No value and no identifier is ever added that way.
"""

import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any, Literal

import sqlglot
from sqlglot import exp

from coalescedb.auth.service import AuthService
from coalescedb.config import Settings
from coalescedb.db.backup import BackupService
from coalescedb.db.connection import is_busy_error, open_connection
from coalescedb.db.introspect import object_names
from coalescedb.db.locks import DatabaseLocks
from coalescedb.db.registry import DatabaseRegistry
from coalescedb.errors import (
    CoalesceDBError,
    DatabaseBusy,
    ExecutionError,
    PermissionDenied,
    RowConflict,
    SQLRejected,
)
from coalescedb.models import GuardResult, QueryResult, Role, Session, StatementKind
from coalescedb.observability.tracing import trace
from coalescedb.security.sql_guard import validate

Source = Literal[
    "manual_sql", "nl", "builder", "editor", "designer", "ingest",
    "sql_import", "live_import", "export", "analyze", "system",
]  # fmt: skip

# Sources that are audited under their own name; every other source is audited as "query".
_OWN_ACTION = frozenset({"export", "ingest", "sql_import", "live_import"})

_SCHEMA_KINDS = frozenset(
    {
        StatementKind.CREATE_TABLE,
        StatementKind.CREATE_INDEX,
        StatementKind.CREATE_VIEW,
        StatementKind.ALTER_TABLE,
    }
)
_CHANGE_KINDS = frozenset({StatementKind.INSERT, StatementKind.UPDATE, StatementKind.DELETE})

_RETRY_PAUSES_S = (0.1, 0.3)  # before the first retry, then before every later one

_SAVED_BUT_NOT_RECORDED = (
    "The change was saved, but it could not be recorded in the audit log. "
    "Tell your administrator."
)
_NOT_RECORDED = "The action could not be recorded in the audit log. Tell your administrator."
_NO_BACKUP = "A backup could not be made, so nothing was changed."


@dataclass
class _Call:
    """What one Executor call knows, for the audit entry and the trace line."""

    method: str
    source: str
    session: Session
    raw_sql: list[str]  # as given
    single: bool  # one statement (audit shows a string) or a batch (a list)
    sql: list[str] = field(default_factory=list)  # guard.normalized_sql, once guarded
    kind: StatementKind | None = None
    is_write: bool = False
    retries: int = 0
    detail: dict[str, Any] = field(default_factory=dict)  # filled in on success
    attrs: dict[str, Any] = field(default_factory=dict)  # the trace attributes
    kinds: list[StatementKind | None] = field(default_factory=list)  # one per statement
    counts: dict[str, int] = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)


class Executor:
    def __init__(
        self,
        settings: Settings,
        registry: DatabaseRegistry,
        auth: AuthService,
        backups: BackupService,
        *,
        locks: DatabaseLocks | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._auth = auth
        self._backups = backups
        # None: the registry's object, so the two can never differ by accident (§6.3).
        self.locks = registry.locks if locks is None else locks
        self._sleep = sleep

    # --- public methods ------------------------------------------------------------------

    def preview(self, session: Session, sql: str) -> GuardResult:
        """Run the guard only. No connection that could run the SQL, no audit entry."""
        db_name = self._require_db(session)
        role = self._require_role(session, db_name)
        with self.locks.shared(db_name):
            path = self._registry.path_for(db_name)
            known = object_names(path, busy_timeout_s=self._settings.busy_timeout_s)
            return self._validate(sql, role, known)

    def execute(
        self,
        session: Session,
        sql: str,
        *,
        source: Source,
        params: Sequence[Any] | None = None,
        max_rows: int | None = None,
        confirmed: bool = False,
        destructive_confirm_text: str | None = None,
    ) -> QueryResult:
        cap = self._row_cap(max_rows)
        call = _Call("execute", source, session, [sql], single=True)

        def body(role: Role, path: Path, known: set[str]) -> QueryResult:
            guard = self._guard(role, known, sql)
            call.sql = [guard.normalized_sql or ""]
            call.kind = guard.kind
            call.is_write = guard.kind is not StatementKind.SELECT
            self._require_confirmed(call, confirmed)
            if guard.is_destructive:
                self._require_typed_name(session, destructive_confirm_text)
                self._snapshot(path, "destructive_query")
            return self._run_statement(call, role, path, call.sql[0], params or (), cap)

        return self._run_call(call, body)

    def execute_many(
        self,
        session: Session,
        sql: str,
        rows: Iterable[Sequence[Any]],
        *,
        source: Source,
        confirmed: bool,
        batch_size: int = 1000,
    ) -> int:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        call = _Call("execute_many", source, session, [sql], single=True)

        def body(role: Role, path: Path, known: set[str]) -> int:
            guard = self._guard(role, known, sql)
            call.sql = [guard.normalized_sql or ""]
            call.kind = guard.kind
            call.is_write = True
            self._check_bulk_insert_shape(call.sql[0])
            self._require_confirmed(call, confirmed)
            return self._run_bulk(call, role, path, call.sql[0], rows, batch_size)

        return self._run_call(call, body)

    def apply_schema(
        self, session: Session, ddl: list[str], *, source: Source, confirmed: bool
    ) -> None:
        call = _Call("apply_schema", source, session, list(ddl), single=False)

        def body(role: Role, path: Path, known: set[str]) -> None:
            guards = self._guard_all(role, known, ddl)
            reasons = []
            for guard in guards:
                if guard.kind not in _SCHEMA_KINDS:
                    reasons.append("Only CREATE TABLE, CREATE INDEX, CREATE VIEW and ALTER TABLE "
                                   "can be applied as a schema change.")
                elif guard.is_destructive:
                    reasons.append("A destructive change can't be applied as a schema change.")
            if reasons:
                raise SQLRejected(reasons=list(dict.fromkeys(reasons)))
            call.is_write = True
            call.sql = [guard.normalized_sql or "" for guard in guards]
            call.kinds = [guard.kind for guard in guards]
            call.counts = dict(Counter(guard.kind.value for guard in guards if guard.kind))
            self._require_confirmed(call, confirmed)
            if known:  # the database has at least one table
                self._snapshot(path, "schema_change")
            self._run_batch(call, role, path, [(sql, ()) for sql in call.sql], conflicts=False)

        self._run_call(call, body)

    def apply_changes(
        self,
        session: Session,
        statements: Sequence[tuple[str, Sequence[Any]]],
        *,
        source: Source,
        confirmed: bool,
    ) -> int:
        call = _Call(
            "apply_changes", source, session, [sql for sql, _ in statements], single=False
        )

        def body(role: Role, path: Path, known: set[str]) -> int:
            guards = self._guard_all(role, known, call.raw_sql)
            reasons = []
            for guard in guards:
                if guard.kind not in _CHANGE_KINDS:
                    reasons.append("Only INSERT, UPDATE and DELETE can be applied as changes.")
                elif guard.is_destructive:
                    reasons.append("A change without a WHERE clause is not allowed here.")
            if reasons:
                raise SQLRejected(reasons=list(dict.fromkeys(reasons)))
            call.is_write = True
            call.sql = [guard.normalized_sql or "" for guard in guards]
            call.kinds = [guard.kind for guard in guards]
            call.counts = dict(Counter(guard.kind.value for guard in guards if guard.kind))
            call.tables = sorted({table for guard in guards for table in guard.tables_touched})
            self._require_confirmed(call, confirmed)
            if StatementKind.DELETE in {guard.kind for guard in guards}:
                self._snapshot(path, "editor_delete")
            pairs = [(sql, params) for sql, (_, params) in zip(call.sql, statements, strict=True)]
            return self._run_batch(call, role, path, pairs, conflicts=True)

        return self._run_call(call, body)

    def dry_run(self, session: Session, sql: str) -> None:
        """Compile `sql` with EXPLAIN without running it (§6.6, used by §6.11).

        Never retried, never in a transaction, and audited only when refused.
        """
        call = _Call("dry_run", "nl", session, [sql], single=True)
        # The same always-present keys as every other call (§6.17); dry_run runs one
        # statement and is never retried.
        with trace(
            "execute",
            method="dry_run",
            source=call.source,
            db_name=session.db_name,
            user_id=session.user.id,
            statement_count=1,
            busy_retries_used=0,
        ) as attrs:
            try:
                db_name = self._require_db(session)
                role = self._require_role(session, db_name)
                with self.locks.shared(db_name):
                    path = self._registry.path_for(db_name)
                    known = object_names(path, busy_timeout_s=self._settings.busy_timeout_s)
                    guard = self._guard(role, known, sql)
                    attrs["kind"] = guard.kind.value if guard.kind else None
                    # A fixed keyword in front of SQL the guard approved; nothing else is
                    # ever joined into SQL text.
                    explain = "EXPLAIN " + (guard.normalized_sql or "")  # nosec B608
                    with open_connection(
                        path,
                        role,
                        self._settings.query_timeout_s,
                        busy_timeout_s=self._settings.busy_timeout_s,
                    ) as conn:
                        try:
                            conn.execute(explain).fetchall()
                        except sqlite3.Error as exc:
                            self._reraise(exc)
            except (SQLRejected, PermissionDenied) as exc:
                self._audit_refusal(call, exc)
                raise

    # --- the shared driver ---------------------------------------------------------------

    def _run_call(self, call: _Call, body: Callable[[Role, Path, set[str]], Any]) -> Any:
        """Role, lock, body, then exactly one audit entry whatever the outcome."""
        session = call.session
        with trace(
            "execute",
            method=call.method,
            source=call.source,
            db_name=session.db_name,
            user_id=session.user.id,
        ) as attrs:
            call.attrs = attrs
            try:
                try:
                    db_name = self._require_db(session)
                    role = self._require_role(session, db_name)
                    with self.locks.shared(db_name):
                        path = self._registry.path_for(db_name)
                        known = object_names(path, busy_timeout_s=self._settings.busy_timeout_s)
                        result = body(role, path, known)
                finally:
                    attrs["busy_retries_used"] = call.retries
                    attrs["statement_count"] = len(call.raw_sql)
                    if call.kind is not None:
                        attrs["kind"] = call.kind.value
            except DatabaseBusy:
                self._audit_quiet(call, "query_busy", self._busy_detail(call))
                raise
            except (SQLRejected, PermissionDenied) as exc:
                self._audit_refusal(call, exc)
                raise
            except Exception as exc:
                self._audit_quiet(call, "query_failed", self._failed_detail(call, exc))
                raise
            self._audit_success(call)
            return result

    # --- steps ---------------------------------------------------------------------------

    def _require_db(self, session: Session) -> str:
        if session.db_name is None:
            raise PermissionDenied("Select a database first.")
        return session.db_name

    def _require_role(self, session: Session, db_name: str) -> Role:
        # Never session.role: the grant may have changed since the session was made.
        role = self._auth.role_for(session.user, db_name)
        if role is None:
            raise PermissionDenied("You don't have access to this database.")
        return role

    def _row_cap(self, max_rows: int | None) -> int:
        if max_rows is not None and max_rows < 1:
            raise ValueError("max_rows must be at least 1")
        wanted = self._settings.max_result_rows if max_rows is None else max_rows
        return min(wanted, self._settings.max_export_rows)

    def _validate(self, sql: str, role: Role, known: set[str]) -> GuardResult:
        with trace("guard") as attrs:
            guard = validate(sql, role, known, max_sql_chars=self._settings.max_sql_chars)
            attrs.update(
                allowed=guard.allowed,
                kind=guard.kind.value if guard.kind else None,
                is_destructive=guard.is_destructive,
                reason_count=len(guard.reasons),
                sql_chars=len(sql),
                tables_touched=list(guard.tables_touched),
            )
        return guard

    def _guard(self, role: Role, known: set[str], sql: str) -> GuardResult:
        guard = self._validate(sql, role, known)
        if not guard.allowed:
            raise SQLRejected(reasons=guard.reasons)
        return guard

    def _guard_all(
        self, role: Role, known: set[str], statements: Sequence[str]
    ) -> list[GuardResult]:
        """Guard every statement; one rejection rejects the whole batch."""
        if not statements:
            raise SQLRejected(reasons=["There are no statements to run."])
        guards = [self._validate(sql, role, known) for sql in statements]
        if not all(guard.allowed for guard in guards):
            reasons = [reason for guard in guards if not guard.allowed for reason in guard.reasons]
            raise SQLRejected(reasons=reasons)
        return guards

    @staticmethod
    def _require_confirmed(call: _Call, confirmed: bool) -> None:
        if call.is_write and not confirmed:
            raise PermissionDenied("confirmation required")

    @staticmethod
    def _require_typed_name(session: Session, text: str | None) -> None:
        if text != session.db_name:
            raise PermissionDenied("Type the database's name to confirm this change.")

    @staticmethod
    def _check_bulk_insert_shape(sql: str) -> None:
        """One INSERT ... VALUES row whose every value is an anonymous "?" (§6.6)."""
        problem = "execute_many needs one INSERT with a single VALUES row of ? placeholders."
        try:
            root = sqlglot.parse_one(sql, read="sqlite")
        except sqlglot.errors.SqlglotError:
            raise SQLRejected(reasons=[problem]) from None
        values = root.args.get("expression") if isinstance(root, exp.Insert) else None
        if not isinstance(values, exp.Values) or len(values.expressions) != 1:
            raise SQLRejected(reasons=[problem])
        row = values.expressions[0]
        cells = row.expressions if isinstance(row, exp.Tuple) else [row]
        if not all(isinstance(cell, exp.Placeholder) and cell.this is None for cell in cells):
            raise SQLRejected(reasons=[problem])

    def _snapshot(self, path: Path, reason: str) -> None:
        try:
            self._backups.snapshot(path, reason)
        except (OSError, sqlite3.Error) as exc:
            raise ExecutionError(_NO_BACKUP) from exc

    # --- running -------------------------------------------------------------------------

    def _open(self, role: Role, path: Path):
        return open_connection(
            path,
            role,
            self._settings.query_timeout_s,
            busy_timeout_s=self._settings.busy_timeout_s,
        )

    def _begin(self, conn: sqlite3.Connection, call: _Call) -> None:
        """BEGIN IMMEDIATE, retried only while SQLite says busy (§6.6)."""
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if not is_busy_error(exc) or call.retries >= self._settings.busy_retries:
                    raise
                self._sleep(_RETRY_PAUSES_S[min(call.retries, len(_RETRY_PAUSES_S) - 1)])
                call.retries += 1
        # Time spent waiting for the write lock never counts as running time.
        conn.restart_deadline()

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # the original error is the one to report

    @staticmethod
    def _reraise(exc: sqlite3.Error) -> None:
        """Busy and timeout errors leave raw (open_connection turns them into DatabaseBusy
        and QueryTimeout); every other sqlite3.Error becomes ExecutionError."""
        if is_busy_error(exc) or "interrupted" in str(exc):
            raise exc
        raise ExecutionError(sqlite_message=str(exc)) from exc

    def _in_transaction(self, conn: sqlite3.Connection, call: _Call, work: Callable[[], Any]):
        """Run `work` inside BEGIN IMMEDIATE ... COMMIT, rolling back on any failure."""
        began = False
        try:
            self._begin(conn, call)
            began = True
            result = work()
            conn.execute("COMMIT")
            return result
        except sqlite3.Error as exc:
            if began:
                self._rollback(conn)
            self._reraise(exc)
        except BaseException:
            if began:
                self._rollback(conn)
            raise

    def _run_statement(
        self, call: _Call, role: Role, path: Path, sql: str, params: Sequence[Any], cap: int
    ) -> QueryResult:
        started = time.perf_counter()
        with self._open(role, path) as conn:

            def work() -> tuple[list[str], list[tuple], int, bool]:
                cursor = conn.execute(sql, params)
                try:
                    if cursor.description is None:  # DDL, or DML without RETURNING
                        return [], [], max(cursor.rowcount, 0), False
                    columns = [column[0] for column in cursor.description]
                    fetched = cursor.fetchmany(cap + 1)
                    rows, truncated = fetched[:cap], len(fetched) > cap
                    changed = len(rows)
                    if call.is_write:
                        # RETURNING: every row is written whatever the cap; count them all.
                        changed += (1 if truncated else 0) + sum(1 for _ in cursor)
                    return columns, rows, changed, truncated
                finally:
                    cursor.close()  # COMMIT fails while a RETURNING cursor is open

            try:
                if call.is_write:
                    columns, rows, row_count, truncated = self._in_transaction(conn, call, work)
                else:
                    columns, rows, row_count, truncated = work()
            except sqlite3.Error as exc:
                self._reraise(exc)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        call.attrs.update(row_count=row_count, truncated=truncated)
        call.detail = {
            "sql": call.sql[0],
            "kind": call.kind.value if call.kind else None,
            "row_count": row_count,
            "elapsed_ms": elapsed_ms,
        }
        return QueryResult(columns, rows, row_count, truncated, elapsed_ms)

    def _run_bulk(
        self,
        call: _Call,
        role: Role,
        path: Path,
        sql: str,
        rows: Iterable[Sequence[Any]],
        batch_size: int,
    ) -> int:
        started = time.perf_counter()
        with self._open(role, path) as conn:

            def work() -> int:
                total = 0
                iterator: Iterator[Sequence[Any]] = iter(rows)
                while batch := list(islice(iterator, batch_size)):
                    cursor = conn.executemany(sql, batch)
                    total += max(cursor.rowcount, 0)
                    cursor.close()
                return total

            total = self._in_transaction(conn, call, work)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        call.attrs.update(row_count=total, truncated=False)
        call.detail = {
            "sql": call.sql[0],
            "kind": call.kind.value if call.kind else None,
            "row_count": total,
            "elapsed_ms": elapsed_ms,
        }
        return total

    def _run_batch(
        self,
        call: _Call,
        role: Role,
        path: Path,
        statements: list[tuple[str, Sequence[Any]]],
        *,
        conflicts: bool,
    ) -> int:
        """Several statements in one transaction. With `conflicts`, every UPDATE and DELETE
        must change exactly one row (apply_changes)."""
        kinds = call.kinds
        started = time.perf_counter()
        with self._open(role, path) as conn:

            def work() -> int:
                changed = 0
                for index, (sql, params) in enumerate(statements):
                    cursor = conn.execute(sql, params)
                    try:
                        if cursor.description is not None:  # RETURNING: count the rows
                            count = sum(1 for _ in cursor)
                        else:
                            count = max(cursor.rowcount, 0)
                    finally:
                        cursor.close()
                    one_row = kinds[index] in (StatementKind.UPDATE, StatementKind.DELETE)
                    if conflicts and one_row and count != 1:
                        raise RowConflict(statement_index=index)
                    changed += count
                return changed

            changed = self._in_transaction(conn, call, work)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        call.attrs.update(row_count=changed)
        if conflicts:
            call.detail = {
                "counts": call.counts,
                "tables": call.tables,
                "row_count": changed,
                "elapsed_ms": elapsed_ms,
            }
        else:
            call.detail = {"sql": call.sql, "counts": call.counts, "elapsed_ms": elapsed_ms}
        return changed

    # --- audit ---------------------------------------------------------------------------

    def _write_audit(self, call: _Call, action: str, detail: dict[str, Any]) -> None:
        self._auth.audit(call.session.user, action, call.session.db_name, detail, call.source)

    def _trace_audit_failed(self, call: _Call) -> None:
        with trace(
            "audit_failed",
            method=call.method,
            db_name=call.session.db_name,
            user_id=call.session.user.id,
        ):
            pass

    def _audit_quiet(self, call: _Call, action: str, detail: dict[str, Any]) -> None:
        """Audit a refusal or failure. If the audit write fails, the original error is the
        one reported."""
        try:
            self._write_audit(call, action, detail)
        except Exception:
            self._trace_audit_failed(call)

    def _audit_success(self, call: _Call) -> None:
        action = call.source if call.source in _OWN_ACTION else "query"
        detail = dict(call.detail)
        if call.retries:
            detail["busy_retries_used"] = call.retries
        try:
            self._write_audit(call, action, detail)
        except Exception as exc:
            self._trace_audit_failed(call)
            # A committed write is never reported as not done: the user would try again and
            # run it twice.
            message = _SAVED_BUT_NOT_RECORDED if call.is_write else _NOT_RECORDED
            raise ExecutionError(message) from exc

    def _audit_refusal(self, call: _Call, exc: CoalesceDBError) -> None:
        limit = self._settings.max_sql_chars
        cut = [sql[:limit] for sql in call.raw_sql]
        lengths = [len(sql) for sql in call.raw_sql]
        reasons = exc.reasons if isinstance(exc, SQLRejected) and exc.reasons else None
        detail: dict[str, Any] = {
            "reasons": list(reasons or [exc.user_message]),
            "sql": cut[0] if call.single else cut,
            "sql_length": lengths[0] if call.single else lengths,
        }
        self._audit_quiet(call, "query_rejected", detail)

    def _busy_detail(self, call: _Call) -> dict[str, Any]:
        limit = self._settings.max_sql_chars
        sql = call.sql or [text[:limit] for text in call.raw_sql]
        return {
            "sql": sql[0] if call.single else sql,
            "busy_retries_used": call.retries,
        }

    def _failed_detail(self, call: _Call, exc: BaseException) -> dict[str, Any]:
        detail: dict[str, Any] = {"error": type(exc).__name__}  # the class name only
        if call.single and call.sql:
            detail["sql"] = call.sql[0]
            detail["kind"] = call.kind.value if call.kind else None
        elif call.method == "apply_schema" and call.sql:
            detail["sql"] = call.sql
            detail["counts"] = call.counts
        elif call.method == "apply_changes" and call.sql:
            detail["counts"] = call.counts
            detail["tables"] = call.tables
        if isinstance(exc, RowConflict):
            detail["statement_index"] = exc.statement_index
        if call.retries:
            detail["busy_retries_used"] = call.retries
        return detail

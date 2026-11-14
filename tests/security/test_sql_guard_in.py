"""SQL guard: a bare table name after IN, and `_app_` function names (§6.2 step 7).

A third file next to test_sql_guard.py and test_sql_guard_names.py, which are locked
(§11.1: existing test files are never edited).

SQLite reads `x IN tbl` as `x IN (SELECT * FROM tbl)`, but sqlglot parses `tbl` as a column,
so the protected-name and schema-prefix checks never saw it: `SELECT 'secret' IN _app_meta`
answered whether a value was in a reserved table, for a viewer too. No later layer stops
that read. The guard now rejects `IN` followed by a name without brackets, for every name.

Every string was parsed with the pinned sqlglot (30.21.0) first; each is one statement.
Only outcomes are asserted, never sqlglot class names.
"""

import pytest

from coalescedb.models import GuardResult, Role, StatementKind
from coalescedb.security.sql_guard import validate

BOTH_ROLES = [Role.VIEWER, Role.ADMIN]

KNOWN_TABLES = {"t"}


def check(sql, role):
    result = validate(sql, role, KNOWN_TABLES)
    assert isinstance(result, GuardResult)
    return result


def assert_rejected(result):
    assert result.allowed is False
    assert result.reasons, "a rejection must say why"
    assert all(isinstance(reason, str) and reason for reason in result.reasons)
    assert result.normalized_sql is None


def assert_allowed(result):
    assert result.allowed is True, result.reasons
    assert isinstance(result.normalized_sql, str)
    assert result.normalized_sql.strip()


# --- IN followed by a bare name: rejected for every name ---------------------------------


@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 IN _app_meta",
        "SELECT 1 IN main._app_meta",
        # Two rules reject this one: the IN rule and the `_app_` function-name rule.
        # `SELECT 1 IN t()` below reaches the IN rule alone.
        "SELECT 1 IN _app_meta()",
        "SELECT 1 IN dbstat",
        "SELECT 1 IN temp.sqlite_temp_master",
        # Not a reserved name: only the IN rule can reject these two.
        "SELECT 1 IN t",
        "SELECT 1 IN t()",
        "SELECT ('a', 'b') IN _app_kv",
        "SELECT 1 NOT IN _app_meta",
        "SELECT * FROM t WHERE a IN _app_meta",
    ],
)
def test_in_followed_by_a_bare_name_is_rejected(role, sql):
    assert_rejected(check(sql, role))


# Admin only: for a viewer these two are already refused by the role check (step 5).
@pytest.mark.parametrize(
    "sql",
    [
        "CREATE VIEW leak AS SELECT a FROM t WHERE a IN _app_meta",
        "DELETE FROM t WHERE a IN _app_meta",
    ],
)
def test_in_followed_by_a_bare_name_is_rejected_inside_admin_writes(sql):
    assert_rejected(check(sql, Role.ADMIN))


# --- `_app_` as a function name ----------------------------------------------------------


def test_app_prefixed_function_is_rejected():
    # No IN here, so only the function-name rule can reject it. sqlglot parses a
    # table-valued call as a function, which the table-name rule does not see.
    assert_rejected(check("SELECT * FROM _app_meta()", Role.ADMIN))


# --- IN with brackets is still allowed ---------------------------------------------------


@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 IN (SELECT a FROM t)",
        "SELECT * FROM t WHERE a IN (1, 2)",
        "SELECT * FROM t WHERE a IN (?, ?)",
        "SELECT * FROM t WHERE a NOT IN (SELECT a FROM t)",
    ],
)
def test_in_with_brackets_is_still_allowed(role, sql):
    result = check(sql, role)
    assert_allowed(result)
    assert result.kind is StatementKind.SELECT
    assert result.reasons == []
    assert result.normalized_sql.count("?") == sql.count("?")

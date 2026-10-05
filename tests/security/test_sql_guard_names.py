"""SQL guard: schema prefixes, reserved object names, dbstat and nesting depth (§6.2).

A second file next to test_sql_guard.py, which is locked (§11.1: existing test files are
never edited). It covers the rules added to §6.2 step 7 after that file was written:

- a schema prefix must be absent or `main`;
- the `_app_` / `sqlite_` / `pragma_` rule applies to indexes and views, not only tables;
- `dbstat` is reserved, as a table and as a function call;
- SQL nested deeper than sqlglot can parse is refused, not crashed on.

Every string was parsed with the pinned sqlglot (30.21.0) first. Only outcomes are
asserted, never sqlglot class names.
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


# --- Schema prefix: absent or main -------------------------------------------------------


# `temp.` creates or reaches a temporary object without the TEMP keyword that step 4 looks
# for. For a viewer the three CREATEs and the INSERT are already refused by the role check.
@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE temp.t2 (a)",
        "CREATE INDEX temp.i ON t (a)",
        "CREATE VIEW temp.v AS SELECT 1",
        "INSERT INTO temp.t VALUES (1)",
    ],
)
def test_temp_schema_prefix_is_rejected(role, sql):
    assert_rejected(check(sql, role))


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_any_schema_prefix_on_a_new_index_name_is_rejected(role):
    # Known limitation (§6.2): sqlglot 30.21.0 reads this as an index named "main" on a
    # table named "i" and cannot re-render it, so even the `main` prefix is refused here.
    assert_rejected(check("CREATE INDEX main.i ON t (a)", role))


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_main_schema_prefix_is_allowed(role):
    result = check("SELECT * FROM main.t", role)
    assert_allowed(result)
    assert result.kind is StatementKind.SELECT
    assert result.reasons == []


# --- Reserved names on indexes and views -------------------------------------------------


@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX _app_i ON t (a)",
        'CREATE INDEX "_APP_I" ON t (a)',
        "CREATE VIEW _app_v AS SELECT 1",
    ],
)
def test_reserved_index_and_view_names_are_rejected(role, sql):
    assert_rejected(check(sql, role))


def test_ordinary_index_is_still_allowed_for_admin():
    result = check("CREATE INDEX i ON t (a)", Role.ADMIN)
    assert_allowed(result)
    assert result.kind is StatementKind.CREATE_INDEX
    assert result.is_destructive is False
    assert result.reasons == []


# --- dbstat ------------------------------------------------------------------------------


# A read-only virtual table listing every table's pages and sizes, `_app_` tables included.
# The authorizer does not refuse it, so the guard is its only layer.
@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM dbstat",
        "SELECT * FROM DBSTAT",
        "SELECT * FROM dbstat('main')",
    ],
)
def test_dbstat_is_rejected(role, sql):
    assert_rejected(check(sql, role))


# --- Nesting depth -----------------------------------------------------------------------


@pytest.mark.parametrize("role", BOTH_ROLES)
def test_sixty_nested_calls_are_rejected_cleanly(role):
    # Deeper than sqlglot 30.21.0 can parse: the guard must answer, not raise.
    sql = "SELECT " + "upper(" * 60 + "'a'" + ")" * 60
    assert_rejected(check(sql, role))


# --- ALTER that is not ALTER TABLE -------------------------------------------------------


# Not SQLite syntax, but sqlglot 30.21.0 parses each as one ALTER statement, so step 4 has
# to look at what is being altered.
@pytest.mark.parametrize("role", BOTH_ROLES)
@pytest.mark.parametrize(
    "sql",
    [
        "ALTER VIEW v RENAME TO w",
        "ALTER VIEW v AS SELECT 1",
        "ALTER INDEX i RENAME TO j",
    ],
)
def test_alter_of_anything_but_a_table_is_rejected(role, sql):
    assert_rejected(check(sql, role))

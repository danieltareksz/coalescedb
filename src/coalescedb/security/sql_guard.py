"""SQL guard: the sqlglot allowlist validator (PROJECT_SPEC.md §6.2).

Layer 3 of 5. SQL is parsed with sqlglot and accepted only if the statement, and what it
touches, is explicitly permitted. Anything the guard does not recognise is rejected. It is
expected that some exotic SQL could slip past a parser; the SQLite authorizer (§6.4) is
there so that doesn't matter.
"""

import logging
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from coalescedb.models import GuardResult, Role, StatementKind
from coalescedb.security.policy import FORBIDDEN_FUNCTIONS, ROLE_PERMISSIONS

# When sqlglot cannot parse a statement it logs a warning that contains the SQL text. The
# guard rejects those statements anyway, and SQL must not end up in logs.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

VIEWER_MESSAGE = "Viewer accounts can only run SELECT queries"

_SELECT_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)
_DML_ROOTS: dict[type[exp.Expression], StatementKind] = {
    exp.Insert: StatementKind.INSERT,
    exp.Update: StatementKind.UPDATE,
    exp.Delete: StatementKind.DELETE,
}
_CREATE_KINDS = {
    "TABLE": StatementKind.CREATE_TABLE,
    "INDEX": StatementKind.CREATE_INDEX,
    "VIEW": StatementKind.CREATE_VIEW,
}
_DROP_KINDS = frozenset({"TABLE", "INDEX", "VIEW"})

# Nodes that change data or schema, or that sqlglot could not parse. None of them may
# appear anywhere inside a statement whose root is a SELECT (step 6).
_WRITE_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
)

_RESERVED_PREFIXES = ("sqlite_", "pragma_", "_app_")
_RESERVED_FUNCTION_PREFIXES = ("sqlite_", "pragma_")
# Reserved as a whole name, as a table or as a function call. dbstat lists every table's
# pages and sizes, and the authorizer does not refuse it, so the guard is its only layer.
_RESERVED_NAMES = frozenset({"dbstat"})
_ALLOWED_SCHEMAS = frozenset({"main"})
_READABLE_SCHEMA_TABLES = frozenset({"sqlite_master", "sqlite_schema"})

# The name at the start of a rendered call, e.g. "SQLITE_VERSION" in "SQLITE_VERSION()".
_CALL_NAME_RE = re.compile(r'\s*["`\[]?([A-Za-z_][A-Za-z0-9_$]*)["`\]]?\s*\(')


def validate(
    sql: str, role: Role, known_tables: set[str], *, max_sql_chars: int = 10_000
) -> GuardResult:
    """Check one SQL statement for `role`. The steps and their order are those of §6.2."""
    # 1. Size.
    if len(sql) > max_sql_chars:
        return _reject(f"The SQL is too long ({len(sql)} characters, limit {max_sql_chars}).")
    try:
        return _validate_parsed(sql, role, known_tables)
    except SqlglotError:
        # 2. Parse. The error text can quote the SQL, so it is not passed on.
        return _reject("The SQL could not be parsed.")
    except RecursionError:
        return _reject("The SQL is nested too deeply to check.")


def _validate_parsed(sql: str, role: Role, known_tables: set[str]) -> GuardResult:
    # 2. Parse.
    statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]

    # 3. Exactly one statement (trailing semicolons leave None entries, dropped above).
    if not statements:
        return _reject("There is no SQL statement to run.")
    if len(statements) > 1:
        return _reject("Only one SQL statement can be run at a time.")
    root = statements[0]

    # 4. Root node allowlist.
    kind, reason = _statement_kind(root)
    if kind is None:
        return _reject(reason)

    # 5. Role check.
    if kind not in ROLE_PERMISSIONS[role]:
        return _reject(VIEWER_MESSAGE, kind)

    # 6. Deep walk for forbidden nodes, and 7. protected names.
    tables: list[str] = []
    index_names: list[str] = []
    cte_names: set[str] = set()
    for node in root.walk():
        if kind is StatementKind.SELECT and isinstance(node, _WRITE_NODES):
            return _reject("A SELECT query cannot contain a statement that changes data.", kind)
        if isinstance(node, exp.Func):
            for name in _function_names(node):
                if name in FORBIDDEN_FUNCTIONS:
                    return _reject(f'The function "{name}" is not allowed.', kind)
                if name.startswith(_RESERVED_FUNCTION_PREFIXES) or name in _RESERVED_NAMES:
                    return _reject(f'The function "{name}" is not allowed.', kind)
        elif isinstance(node, exp.Table):
            # A schema prefix must be absent or "main". "temp." would create or reach a
            # temporary object without the TEMP keyword that step 4 looks for.
            schema = node.db.lower()
            if node.catalog or (schema and schema not in _ALLOWED_SCHEMAS):
                return _reject('Only the "main" schema can be used.', kind)
            if node.name and node.name not in tables:
                tables.append(node.name)
        elif isinstance(node, exp.Index):
            if node.name:
                index_names.append(node.name)
        elif isinstance(node, exp.CTE):
            cte_names.add(node.alias.lower())

    # Known limitation: sqlglot misparses a schema prefix on a new index's name and cannot
    # re-render it, so any prefix there is refused, "main" included.
    if kind is StatementKind.CREATE_INDEX and _index_name_has_schema_prefix(sql):
        return _reject("An index name cannot have a schema prefix.", kind)

    # The rule covers every object name: tables, views (which sqlglot stores as tables)
    # and indexes.
    for name in tables + index_names:
        lowered = name.lower()
        if not lowered.startswith(_RESERVED_PREFIXES) and lowered not in _RESERVED_NAMES:
            continue
        if kind is StatementKind.SELECT and lowered in _READABLE_SCHEMA_TABLES:
            continue
        return _reject(f'The name "{name}" is reserved and cannot be used.', kind)

    # 8. Table existence: a warning only. SQLite produces the real error.
    warnings: list[str] = []
    if kind not in _CREATE_KINDS.values():
        known = {name.lower() for name in known_tables} | cte_names | _READABLE_SCHEMA_TABLES
        new_names = _new_names(root)
        for table in tables:
            if table.lower() not in known and table.lower() not in new_names:
                warnings.append(f'Table "{table}" was not found in this database.')

    # 9. Destructiveness, and 10. normalize. The executor runs normalized_sql.
    return GuardResult(
        allowed=True,
        kind=kind,
        normalized_sql=root.sql(dialect="sqlite"),
        reasons=warnings,
        is_destructive=_is_destructive(root, kind),
        tables_touched=tables,
    )


def _reject(reason: str, kind: StatementKind | None = None) -> GuardResult:
    return GuardResult(
        allowed=False,
        kind=kind,
        normalized_sql=None,  # Rejected SQL has no runnable form (§6.2).
        reasons=[reason],
        is_destructive=False,
        tables_touched=[],
    )


def _statement_kind(root: exp.Expression) -> tuple[StatementKind | None, str]:
    """Step 4: the statement's kind, or None and the reason it is not allowed."""
    not_allowed = "This kind of SQL statement is not allowed."
    if isinstance(root, _SELECT_ROOTS):
        return StatementKind.SELECT, ""
    for node_type, kind in _DML_ROOTS.items():
        if isinstance(root, node_type):
            return kind, ""
    if isinstance(root, exp.Create):
        kind = _CREATE_KINDS.get(str(root.args.get("kind") or "").upper())
        if kind is None:
            return None, "Only tables, indexes and views can be created."
        # sqlglot records TEMP / TEMPORARY, VIRTUAL ... USING and STRICT as properties of
        # the CREATE. None of the statements the app allows has any property, so every
        # property is refused, including ones a later sqlglot version might add.
        # (WITHOUT ROWID never gets here: sqlglot cannot parse it, see exp.Command below.)
        properties = root.args.get("properties")
        if properties is not None and properties.expressions:
            return None, (
                "Temporary, virtual, STRICT and WITHOUT ROWID tables and temporary views "
                "cannot be created."
            )
        return kind, ""
    if isinstance(root, exp.Alter):
        if str(root.args.get("kind") or "").upper() != "TABLE":
            return None, not_allowed
        return StatementKind.ALTER_TABLE, ""
    if isinstance(root, exp.Drop):
        if str(root.args.get("kind") or "").upper() not in _DROP_KINDS:
            return None, "Only tables, indexes and views can be dropped."
        return StatementKind.DROP, ""
    # Everything else, including exp.Command (sqlglot's fallback for statements it cannot
    # parse), PRAGMA, ATTACH / DETACH, transactions, VACUUM, REINDEX and SET.
    return None, not_allowed


def _placeholder(value: object) -> object:
    if isinstance(value, exp.Expression):
        return exp.Null()
    if isinstance(value, list):
        return [_placeholder(item) for item in value]
    return value


def _function_names(node: exp.Func) -> set[str]:
    """Lower-cased names of one call: sqlglot's parsed name and the name it renders with.

    The rendered form is what runs, and sqlglot can turn a call into a node of its own
    (sqlite_version() becomes a "current version" node), so the parsed name is not enough.
    The call is rendered with its arguments replaced by NULL: only the name is needed, and
    rendering the full arguments of every nested call makes long queries very slow.
    """
    names: set[str] = set()
    if isinstance(node, exp.Anonymous):
        names.add(node.name.lower())
    try:
        shell = type(node)(**{key: _placeholder(value) for key, value in node.args.items()})
        rendered = shell.sql(dialect="sqlite")
    except (SqlglotError, TypeError, ValueError, AttributeError, KeyError):
        rendered = node.sql(dialect="sqlite")
    match = _CALL_NAME_RE.match(rendered)
    if match:
        names.add(match.group(1).lower())
    return names


def _index_name_has_schema_prefix(sql: str) -> bool:
    """True if CREATE INDEX names its index as schema.name.

    Read from sqlglot's tokens, not from the parsed tree (which is wrong in this case) and
    never from the raw text: a dot between the INDEX keyword and ON is a schema prefix. A
    dot inside a quoted name is part of that one name token and does not count.
    """
    after_index = False
    for token in sqlglot.tokenize(sql, read="sqlite"):
        if token.token_type is TokenType.INDEX:
            after_index = True
        elif after_index and token.token_type is TokenType.ON:
            return False
        elif after_index and token.token_type is TokenType.DOT:
            return True
    return False


def _new_names(root: exp.Expression) -> set[str]:
    """Lower-cased names that step 8 must not report as missing tables.

    The name in DROP INDEX / DROP VIEW is not a table, and a rename's target is new.
    """
    names: set[str] = set()
    if isinstance(root, exp.Drop) and str(root.args.get("kind") or "").upper() != "TABLE":
        names.update(table.name.lower() for table in root.find_all(exp.Table))
    if isinstance(root, exp.Alter):
        for action in root.args.get("actions") or []:
            if isinstance(action, exp.AlterRename):
                names.update(table.name.lower() for table in action.find_all(exp.Table))
    return names


def _is_destructive(root: exp.Expression, kind: StatementKind) -> bool:
    """Step 9: DROP; DELETE / UPDATE without WHERE; ALTER TABLE other than ADD COLUMN."""
    if kind is StatementKind.DROP:
        return True
    if kind in (StatementKind.DELETE, StatementKind.UPDATE):
        return root.args.get("where") is None
    if kind is StatementKind.ALTER_TABLE:
        # ADD COLUMN is the only action that cannot lose or break anything. DROP COLUMN,
        # RENAME COLUMN, RENAME TO and any action not known here count as destructive.
        actions = root.args.get("actions") or []
        return not actions or any(not isinstance(action, exp.ColumnDef) for action in actions)
    return False

"""Security policy shared by the SQL guard and the SQLite authorizer (PROJECT_SPEC.md §6.2)."""

from coalescedb.models import Role, StatementKind

# Which kinds of statement each role may run. The guard checks this (§6.2 step 5); the
# authorizer and the read-only viewer connection enforce the same split again (§6.4).
ROLE_PERMISSIONS: dict[Role, frozenset[StatementKind]] = {
    Role.VIEWER: frozenset({StatementKind.SELECT}),
    Role.ADMIN: frozenset(StatementKind),  # all kinds in the enum
}

# SQL functions that can load code, read or write files, or reveal build details. Names are
# lowercase; compare with name.lower(). The guard (§6.2) and the authorizer (§6.4) both
# import this set, so the two layers can never disagree.
FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "load_extension",
        "readfile",
        "writefile",
        "edit",
        "fts3_tokenizer",
        "sqlite_compileoption_get",
    }
)

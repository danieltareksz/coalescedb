"""Security policy shared by the SQL guard and the SQLite authorizer (PROJECT_SPEC.md §6.2).

M4 adds ROLE_PERMISSIONS to this file.
"""

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

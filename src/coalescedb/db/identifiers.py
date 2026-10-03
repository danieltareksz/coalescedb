"""Identifier validation and quoting (PROJECT_SPEC.md §6.8).

Values are always bound with `?`. Identifiers (table and column names) cannot be bound, so
every one that reaches SQL text must come out of one of the two quoting functions here:

- quote_identifier:           NEW names the app creates (always simple lowercase names).
- quote_existing_identifier:  names that ALREADY exist in a database, kept as they are.
"""

import re
import unicodedata

from coalescedb.errors import InvalidIdentifier

MAX_IDENTIFIER_LENGTH = 63

IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}\Z")  # \Z, not $: "abc\n" must not match

# Database names. Lives here, not in registry.py, because both the registry (§6.3) and
# AuthService (§6.1) validate with it and the registry already imports AuthService.
# Always used with .fullmatch().
DB_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}\Z")

# Full keyword list from https://www.sqlite.org/lang_keywords.html, stored lowercase.
SQLITE_KEYWORDS: frozenset[str] = frozenset(
    """
    abort action add after all alter always analyze and as asc attach autoincrement
    before begin between by cascade case cast check collate column commit conflict
    constraint create cross current current_date current_time current_timestamp
    database default deferrable deferred delete desc detach distinct do drop each
    else end escape except exclude exclusive exists explain fail filter first
    following for foreign from full generated glob group groups having if ignore
    immediate in index indexed initially inner insert instead intersect into is
    isnull join key last left like limit match materialized natural no not nothing
    notnull null nulls of offset on or order others outer over partition plan pragma
    preceding primary query raise range recursive references regexp reindex release
    rename replace restrict returning right rollback row rows savepoint select set
    table temp temporary then ties to transaction trigger unbounded union unique
    update using vacuum values view virtual when where window with without
    """.split()
)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_KEYWORD_SUFFIX = "_col"
_DIGIT_PREFIX = "c_"


def to_snake_identifier(raw: str) -> str:
    """Turn free text such as "Due Date (EST)" into a safe new name such as "due_date_est".

    Raises InvalidIdentifier when nothing usable is left ("", "!!!", "日本"). It never
    invents a name; callers choose the fallback (§6.13, §6.15).
    """
    if not isinstance(raw, str):
        raise InvalidIdentifier()
    # NFKD splits "é" into "e" + a combining accent; dropping the accent leaves "e".
    decomposed = unicodedata.normalize("NFKD", raw)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    name = _NON_ALNUM_RE.sub("_", without_accents.lower()).strip("_")
    if not name:
        raise InvalidIdentifier("That name has no letters or digits that can be used.")
    if name[0].isdigit():
        name = _DIGIT_PREFIX + name
    name = name[:MAX_IDENTIFIER_LENGTH].rstrip("_")
    if name in SQLITE_KEYWORDS:
        name = name[: MAX_IDENTIFIER_LENGTH - len(_KEYWORD_SUFFIX)] + _KEYWORD_SUFFIX
    return validate_identifier(name)


def validate_identifier(name: str) -> str:
    """Return `name` unchanged if it is a valid NEW identifier, else raise InvalidIdentifier."""
    if not isinstance(name, str) or IDENT_RE.fullmatch(name) is None:
        raise InvalidIdentifier(
            "Names must use lowercase letters, digits and underscores, start with a letter "
            "or underscore, and be at most 63 characters."
        )
    if name in SQLITE_KEYWORDS:
        raise InvalidIdentifier("That name is a reserved SQL word.")
    return name


def quote_identifier(name: str) -> str:
    """Quote a NEW name for use in SQL text. Validates first."""
    return '"' + validate_identifier(name) + '"'


def quote_existing_identifier(name: str, known: set[str]) -> str:
    """Quote a name that ALREADY exists, keeping its exact spelling.

    `known` is the set of real names gathered in the same operation (introspection of the
    target database, plan_sql_import, or sqlalchemy.inspect). Never call this with text
    typed by a user or produced by the model.
    """
    if not isinstance(name, str) or "\x00" in name or name not in known:
        raise InvalidIdentifier("That table or column doesn't exist.")
    return '"' + name.replace('"', '""') + '"'


def dedupe(names: list[str]) -> list[str]:
    """Make names unique: "title", "title" → "title", "title_2".

    Compares case-insensitively, because SQLite does. The first occurrence of each name
    keeps its exact spelling; later ones get a numeric suffix that isn't already taken.
    To dedupe new names against existing ones: dedupe(existing + new)[len(existing):]
    """
    taken = {name.lower() for name in names}  # every spelling in the input is reserved
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            result.append(name)
            continue
        number = 2
        while True:
            suffix = f"_{number}"
            candidate = name[: MAX_IDENTIFIER_LENGTH - len(suffix)] + suffix
            if candidate.lower() not in taken:
                break
            number += 1
        taken.add(candidate.lower())
        seen.add(candidate.lower())
        result.append(candidate)
    return result

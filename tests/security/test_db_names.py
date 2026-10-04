"""Security tests for database names (PROJECT_SPEC.md §6.8): validate_db_name.

Database names become file names, so besides DB_NAME_RE they must not be one of the names
Windows reserves for devices ("con.db" can't be created safely there).
"""

import pytest

from coalescedb.db.identifiers import WINDOWS_RESERVED_NAMES, validate_db_name
from coalescedb.errors import InvalidIdentifier

RESERVED_DB_NAMES = ["con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9"]


def test_windows_reserved_names_is_the_full_lowercase_set():
    expected = {"con", "prn", "aux", "nul"}
    expected |= {"com" + str(n) for n in range(1, 10)}
    expected |= {"lpt" + str(n) for n in range(1, 10)}
    assert isinstance(WINDOWS_RESERVED_NAMES, frozenset)
    assert WINDOWS_RESERVED_NAMES == expected


@pytest.mark.parametrize("name", RESERVED_DB_NAMES + ["CON", "Con", "NUL", "COM1", "LPT1"])
def test_validate_db_name_rejects_windows_reserved_names(name):
    with pytest.raises(InvalidIdentifier):
        validate_db_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "../x",
        "x/../../y",
        "x.db",
        "con.db",
        "Sales",
        "a b",
        "abc\n",
        "",
        "a" * 49,
        "1abc",
        "_abc",
        "ｓales",  # fullwidth "s": a unicode lookalike
        None,
        123,
    ],
)
def test_validate_db_name_rejects_bad_names(name):
    with pytest.raises(InvalidIdentifier):
        validate_db_name(name)


@pytest.mark.parametrize("name", ["a", "sales", "sales_2024", "console", "com10", "lpt", "a" * 48])
def test_validate_db_name_accepts_and_returns_name(name):
    assert validate_db_name(name) == name

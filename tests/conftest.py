"""Shared test fixtures: temp data dirs and auth objects (PROJECT_SPEC.md §2).

Imports of coalescedb.auth happen inside the fixtures, so test files that don't use them
(tests/security/, test_config.py) still run on their own.
"""

import os

import pytest

SUPER_PASSWORD = "super-secret-pw-1"


class FakeClock:
    """Stands in for the wall clock so lockout tests don't have to sleep."""

    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Settings whose data folder is an empty temp directory."""
    from coalescedb.config import ENV_PREFIX, load_settings

    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)
    monkeypatch.setenv("COALESCEDB_DATA_DIR", str(tmp_path / "data"))
    return load_settings()


@pytest.fixture
def fast_hashing(monkeypatch):
    """Use cheap Argon2 settings so hundreds of hashes don't slow the suite down.

    Tests of passwords.py itself don't use this fixture and run with the real defaults.
    """
    import argon2

    cheap = argon2.PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr("coalescedb.auth.passwords._hasher", cheap)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr("coalescedb.auth.service._now", lambda: fake.now)
    return fake


@pytest.fixture
def store(settings):
    from coalescedb.auth.store import AppStore

    return AppStore(settings.app_db_path)


@pytest.fixture
def auth(store, settings, fast_hashing):
    from coalescedb.auth.service import AuthService

    return AuthService(store, settings)


@pytest.fixture
def superadmin(auth):
    return auth.bootstrap_superadmin("root_admin", SUPER_PASSWORD)


@pytest.fixture
def backups(settings):
    from coalescedb.db.backup import BackupService

    return BackupService(settings)


@pytest.fixture
def registry(settings, auth, backups):
    from coalescedb.db.registry import DatabaseRegistry

    return DatabaseRegistry(settings, auth, backups)

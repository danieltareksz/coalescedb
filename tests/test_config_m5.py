"""Settings fields added at M5 (PROJECT_SPEC.md §3).

Kept apart from test_config.py, which is never edited (§11.1).
"""

import os
from pathlib import Path

import pytest

from coalescedb.config import ENV_PREFIX, Settings, load_settings


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the app at an empty temp folder and remove any COALESCEDB_* variables."""
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)
    target = tmp_path / "data"
    monkeypatch.setenv("COALESCEDB_DATA_DIR", str(target))
    return target.resolve()


def test_busy_defaults(data_dir):
    settings = load_settings()
    assert settings.busy_timeout_s == 5.0
    assert settings.busy_retries == 2


def test_instance_lock_path_follows_data_dir(data_dir):
    assert load_settings().instance_lock_path == data_dir / "instance.lock"


def test_instance_lock_path_is_never_read_from_the_environment(data_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("COALESCEDB_INSTANCE_LOCK_PATH", str(tmp_path / "elsewhere.lock"))
    assert load_settings().instance_lock_path == data_dir / "instance.lock"


def test_busy_settings_can_be_set_from_the_environment(data_dir, monkeypatch):
    monkeypatch.setenv("COALESCEDB_BUSY_TIMEOUT_S", "0.5")
    monkeypatch.setenv("COALESCEDB_BUSY_RETRIES", "4")
    settings = load_settings()
    assert settings.busy_timeout_s == 0.5
    assert settings.busy_retries == 4


@pytest.mark.parametrize(
    ("env_name", "raw"),
    [("COALESCEDB_BUSY_TIMEOUT_S", "soon"), ("COALESCEDB_BUSY_RETRIES", "2.5")],
)
def test_bad_busy_values_name_the_variable(data_dir, monkeypatch, env_name, raw):
    monkeypatch.setenv(env_name, raw)
    with pytest.raises(ValueError) as excinfo:
        load_settings()
    assert env_name in str(excinfo.value)
    assert raw not in str(excinfo.value)


def test_settings_built_by_hand_without_the_lock_path_has_none():
    path = Path("x")
    settings = Settings(
        data_dir=path,
        databases_dir=path,
        backups_dir=path,
        app_db_path=path,
        traces_path=path,
        benchmark_cache_path=path,
    )
    assert settings.instance_lock_path is None

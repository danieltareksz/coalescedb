"""Tests for config.py (PROJECT_SPEC.md §3)."""

import dataclasses
import os
import sys
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


# --- Paths and folders -----------------------------------------------------------------


def test_data_dir_env_override_sets_all_derived_paths(data_dir):
    settings = load_settings()
    assert settings.data_dir == data_dir
    assert settings.databases_dir == data_dir / "databases"
    assert settings.backups_dir == data_dir / "backups"
    assert settings.app_db_path == data_dir / "app.db"
    assert settings.traces_path == data_dir / "traces.jsonl"
    assert settings.benchmark_cache_path == data_dir / "benchmark.json"


def test_folders_are_created(data_dir):
    settings = load_settings()
    assert settings.data_dir.is_dir()
    assert settings.databases_dir.is_dir()
    assert settings.backups_dir.is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_folders_are_created_with_mode_0o700(data_dir):
    settings = load_settings()
    for directory in (settings.data_dir, settings.databases_dir, settings.backups_dir):
        assert directory.stat().st_mode & 0o777 == 0o700


def test_load_settings_can_run_twice(data_dir):
    assert load_settings() == load_settings()


def test_dev_mode_uses_current_directory(data_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("COALESCEDB_DATA_DIR")
    monkeypatch.chdir(tmp_path)
    assert load_settings().data_dir == tmp_path.resolve()


def test_derived_paths_cannot_be_overridden_one_by_one(data_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("COALESCEDB_DATABASES_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("COALESCEDB_APP_DB_PATH", str(tmp_path / "other.db"))
    settings = load_settings()
    assert settings.databases_dir == data_dir / "databases"
    assert settings.app_db_path == data_dir / "app.db"


# --- Defaults and environment overrides ------------------------------------------------


def test_defaults_match_the_spec(data_dir):
    settings = load_settings()
    assert settings.ollama_host == "http://127.0.0.1:11434"
    assert settings.ollama_num_ctx == 8192
    assert settings.max_result_rows == 1000
    assert settings.query_timeout_s == 10.0
    assert settings.login_max_failures == 5
    assert settings.model_ladder == (
        "qwen2.5-coder:1.5b-instruct-q4_K_M",
        "qwen2.5-coder:0.5b-instruct-q4_K_M",
    )
    assert settings.finetuned_ladder == ()
    assert settings.ai_mode_override == "auto"


@pytest.mark.parametrize(
    ("env_name", "raw", "field", "expected"),
    [
        ("COALESCEDB_MAX_RESULT_ROWS", "50", "max_result_rows", 50),  # int
        ("COALESCEDB_QUERY_TIMEOUT_S", "2.5", "query_timeout_s", 2.5),  # float
        ("COALESCEDB_OLLAMA_HOST", "http://127.0.0.1:9999", "ollama_host", "http://127.0.0.1:9999"),
        ("COALESCEDB_MODEL_LADDER", "a:1, b:2", "model_ladder", ("a:1", "b:2")),  # tuple
        ("COALESCEDB_FINETUNED_LADDER", "x:1", "finetuned_ladder", ("x:1",)),
        ("COALESCEDB_AI_MODE_OVERRIDE", "force_off", "ai_mode_override", "force_off"),  # Literal
    ],
)
def test_env_override_is_picked_up_with_the_right_type(
    data_dir, monkeypatch, env_name, raw, field, expected
):
    monkeypatch.setenv(env_name, raw)
    value = getattr(load_settings(), field)
    assert value == expected
    assert type(value) is type(expected)


@pytest.mark.parametrize(
    ("env_name", "raw", "expected_text"),
    [
        ("COALESCEDB_MAX_RESULT_ROWS", "abc", "a whole number"),
        ("COALESCEDB_MAX_RESULT_ROWS", "1.5", "a whole number"),
        ("COALESCEDB_QUERY_TIMEOUT_S", "soon", "a number"),
        ("COALESCEDB_AI_MODE_OVERRIDE", "maybe", "one of auto, force_on, force_off"),
    ],
)
def test_bad_value_gives_a_clear_error(data_dir, monkeypatch, env_name, raw, expected_text):
    monkeypatch.setenv(env_name, raw)
    with pytest.raises(ValueError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert env_name in message  # says which variable is wrong
    assert expected_text in message  # says what was expected
    assert raw not in message  # never repeats the value


@pytest.mark.parametrize("raw", ["", " ", ",", " , ,"])
def test_empty_model_ladder_raises(data_dir, monkeypatch, raw):
    monkeypatch.setenv("COALESCEDB_MODEL_LADDER", raw)
    with pytest.raises(ValueError, match="COALESCEDB_MODEL_LADDER"):
        load_settings()


# --- The Settings dataclass ------------------------------------------------------------


def test_settings_refuses_positional_arguments():
    path = Path("x")
    with pytest.raises(TypeError):
        Settings(path, path, path, path, path)


def test_settings_accepts_keyword_arguments():
    path = Path("x")
    settings = Settings(
        data_dir=path,
        databases_dir=path,
        backups_dir=path,
        app_db_path=path,
        traces_path=path,
        benchmark_cache_path=path,
    )
    assert settings.max_result_rows == 1000


def test_settings_is_frozen(data_dir):
    settings = load_settings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.max_result_rows = 5

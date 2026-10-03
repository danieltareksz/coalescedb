"""Settings and paths (PROJECT_SPEC.md §3)."""

import os
import sys
from dataclasses import MISSING, dataclass, fields
from pathlib import Path
from typing import Literal, get_args, get_origin, get_type_hints

import platformdirs

ENV_PREFIX = "COALESCEDB_"


# kw_only: benchmark_cache_path has no default but follows fields that do, which a plain
# dataclass rejects. Settings is always built with keyword arguments (by load_settings).
@dataclass(frozen=True, kw_only=True)
class Settings:
    data_dir: Path  # Dev: ./ ; packaged: platformdirs.user_data_dir("CoalesceDB")
    databases_dir: Path  # data_dir / "databases"
    backups_dir: Path  # data_dir / "backups"
    app_db_path: Path  # data_dir / "app.db"   (users, grants, audit — NOT in databases_dir)
    traces_path: Path  # data_dir / "traces.jsonl"

    ollama_host: str = "http://127.0.0.1:11434"
    # There is no single "ollama_model" setting. After M15, the model in use is decided at
    # runtime by the ladder in §6.22 and stored in AIStatus.active_model. Only until M15
    # exists do callers use Settings.default_model. From M15 on there is no fallback: if
    # active_model is None, complete() raises LLMUnavailable (§6.10).
    ollama_num_ctx: int = 8192  # Must be set explicitly; Ollama's default context is much smaller
    ollama_timeout_s: float = 120.0
    llm_temperature: float = 0.0

    max_result_rows: int = 1000  # Default on-screen cap (Query page)
    query_timeout_s: float = 10.0
    max_upload_mb: int = 25
    max_pdf_pages: int = 60
    max_xlsx_rows: int = 100_000
    max_sql_chars: int = 10_000
    login_max_failures: int = 5
    login_lockout_s: int = 300
    backups_to_keep: int = 20

    # Model ladder & benchmark (§6.22)
    # Ordered list tried by §6.22, biggest first. Both are 4-bit (Q4_K_M).
    model_ladder: tuple[str, ...] = (
        "qwen2.5-coder:1.5b-instruct-q4_K_M",
        "qwen2.5-coder:0.5b-instruct-q4_K_M",
    )
    # Empty until M20 ships fine-tuned models, e.g. ("coalescedb-sql:1.5b", "coalescedb-sql:0.5b").
    # When non-empty, each fine-tuned model is tried in place of the stock model of the same
    # size; if its download or checksum fails, the stock model is used instead.
    finetuned_ladder: tuple[str, ...] = ()
    benchmark_min_gen_tps: float = 20.0  # Generation speed required to enable AI
    benchmark_min_prompt_tps: float = 150.0  # Prompt-reading speed required for PDF import
    benchmark_runs: int = 3  # Median of N runs after one warm-up
    benchmark_cache_path: Path  # data_dir / "benchmark.json"
    ai_mode_override: Literal["auto", "force_on", "force_off"] = "auto"

    # Import / export (§6.19–6.21)
    max_sql_dump_mb: int = 200
    max_export_rows: int = 1_000_000
    live_import_timeout_s: float = 15.0

    # Analytics (§6.23–6.25)
    max_analysis_rows: int = 200_000  # Larger results are sampled, with a visible notice
    max_chart_points: int = 50_000
    min_rows_regression: int = 20  # Also requires ≥ 10 rows per feature
    min_points_forecast: int = 12
    forecast_max_horizon_ratio: float = 0.5  # Horizon ≤ half the history length
    analysis_timeout_s: float = 60.0

    @property
    def default_model(self) -> str:
        # Interim source of truth, used by M7 OllamaClient only until M15 (the speed test,
        # §6.22) exists. From M15 on, complete() never falls back here: if
        # AIStatus.active_model is None it raises LLMUnavailable with AIStatus.reason as
        # the message (§6.10). Evals (§11.2) keep using this as the default for `--model`,
        # since they choose their model explicitly.
        return self.model_ladder[0]


def _coerce(env_name: str, raw: str, hint: object) -> object:
    """Convert an environment variable's text to the type of the Settings field."""
    # The message names the variable and the expected type, but never repeats the value.
    try:
        if hint is int:
            return int(raw)
        if hint is float:
            return float(raw)
        if hint is str:
            return raw
        if get_origin(hint) is tuple:
            return tuple(part.strip() for part in raw.split(",") if part.strip())
        if get_origin(hint) is Literal:
            if raw not in get_args(hint):
                raise ValueError
            return raw
    except ValueError:
        if get_origin(hint) is Literal:
            expected = "one of " + ", ".join(get_args(hint))
        else:
            expected = "a whole number" if hint is int else "a number"
        raise ValueError(f"Invalid value for {env_name}: expected {expected}") from None
    raise ValueError(f"{env_name} cannot be set from the environment")


def load_settings() -> Settings:
    """Build Settings: defaults, then COALESCEDB_* environment variables, then frozen."""
    env_data_dir = os.environ.get(ENV_PREFIX + "DATA_DIR")
    if env_data_dir:
        data_dir = Path(env_data_dir)
    elif getattr(sys, "frozen", False):  # packaged executable
        data_dir = Path(platformdirs.user_data_dir("CoalesceDB"))
    else:
        data_dir = Path.cwd()
    data_dir = data_dir.resolve()

    values: dict[str, object] = {
        "data_dir": data_dir,
        "databases_dir": data_dir / "databases",
        "backups_dir": data_dir / "backups",
        "app_db_path": data_dir / "app.db",
        "traces_path": data_dir / "traces.jsonl",
        "benchmark_cache_path": data_dir / "benchmark.json",
    }

    hints = get_type_hints(Settings)
    for field in fields(Settings):
        if field.default is MISSING:  # the path fields above; they follow data_dir
            continue
        env_name = ENV_PREFIX + field.name.upper()
        raw = os.environ.get(env_name)
        if raw is not None:
            values[field.name] = _coerce(env_name, raw, hints[field.name])

    settings = Settings(**values)
    if not settings.model_ladder:
        raise ValueError(f"{ENV_PREFIX}MODEL_LADDER must name at least one model")

    for directory in (settings.data_dir, settings.databases_dir, settings.backups_dir):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    return settings

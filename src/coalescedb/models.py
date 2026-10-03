"""Shared data types (PROJECT_SPEC.md §5)."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal


class Role(str, Enum):
    ADMIN = "admin"  # DDL + DML + SELECT on granted DB; manages that DB
    VIEWER = "viewer"  # SELECT only on granted DB


@dataclass(frozen=True)
class User:
    id: int
    username: str
    is_superadmin: bool  # Can create DBs, manage users/grants globally


@dataclass(frozen=True)
class Session:
    user: User
    db_name: str | None
    role: Role | None  # Resolved from grants for db_name; NEVER from a UI toggle


class StatementKind(str, Enum):
    SELECT = "select"
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    CREATE_TABLE = "create_table"
    CREATE_INDEX = "create_index"
    CREATE_VIEW = "create_view"
    ALTER_TABLE = "alter_table"
    DROP = "drop"


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    kind: StatementKind | None
    normalized_sql: str | None  # sqlglot re-rendered SQL (what actually runs)
    reasons: list[str]  # Why rejected, or warnings if allowed
    is_destructive: bool  # DROP, DELETE/UPDATE without WHERE, ALTER ... DROP COLUMN
    tables_touched: list[str]


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    row_count: int  # Rows returned (SELECT) or affected (DML)
    truncated: bool  # True if max_result_rows was hit
    elapsed_ms: float


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    not_null: bool
    default: str | None
    pk: bool


@dataclass(frozen=True)
class ForeignKeyInfo:
    column: str
    ref_table: str
    ref_column: str


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: list[ColumnInfo]
    foreign_keys: list[ForeignKeyInfo]
    row_count: int


@dataclass(frozen=True)
class LLMCall:
    text: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: float
    model: str


@dataclass(frozen=True)
class SQLGeneration:
    question: str
    sql: str | None
    guard: GuardResult | None
    attempts: int  # 1 or 2 (self-correction)
    llm_calls: list[LLMCall]
    error: str | None


@dataclass(frozen=True)
class BenchmarkResult:
    model: str
    gen_tps: float  # Median generation tokens/s
    prompt_tps: float  # Median prompt-processing tokens/s
    runs: int
    measured_at: datetime
    machine_fingerprint: str  # Hash of CPU model, total RAM, OS, Ollama version, model digest


AIState = Literal[
    "checking",
    "needs_download_consent",
    "downloading",
    "ready",
    "disabled",
    "ollama_unavailable",
]


@dataclass(frozen=True)
class AIStatus:
    state: AIState
    enabled: bool  # True only when state == "ready"
    active_model: str | None  # The ONLY source of truth for which model the app calls
    pdf_import_enabled: bool  # False if prompt_tps below threshold even when AI is on
    reason: str  # Human-readable, shown in the sidebar
    results: list[BenchmarkResult]
    pending_downloads: list[tuple[str, int]]  # (model name, size in bytes) awaiting consent
    download_progress: tuple[int, int] | None  # (bytes done, total) while downloading

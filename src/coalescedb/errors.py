"""Exception hierarchy (PROJECT_SPEC.md §4).

Every user-facing failure is one of these types. The UI shows `.user_message`; stack traces
are never shown to users.
"""

from coalescedb.models import Role


class CoalesceDBError(Exception):
    """Base class. `.user_message` is the friendly text the UI displays."""

    default_message = "Something went wrong."

    def __init__(self, user_message: str | None = None) -> None:
        self.user_message = user_message or self.default_message
        super().__init__(self.user_message)


class AuthError(CoalesceDBError):
    """Bad credentials. The message never says which part was wrong."""

    default_message = "Invalid username or password."


class AccountLockedError(AuthError):
    default_message = "Too many failed attempts. Try again later."

    def __init__(self, user_message: str | None = None, *, retry_after_s: int) -> None:
        super().__init__(user_message)
        self.retry_after_s = retry_after_s


class PermissionDenied(CoalesceDBError):
    """The user's role lacks the rights for this action."""

    default_message = "You don't have permission to do that."

    def __init__(
        self,
        user_message: str | None = None,
        *,
        required_role: Role | None = None,
        actual_role: Role | None = None,
    ) -> None:
        super().__init__(user_message)
        self.required_role = required_role
        self.actual_role = actual_role


class InvalidIdentifier(CoalesceDBError):
    """Bad database, table, column or user name."""

    default_message = "That name isn't allowed."


class DatabaseNotFound(CoalesceDBError):
    default_message = "That database doesn't exist."


class DatabaseExists(CoalesceDBError):
    default_message = "A database with that name already exists."


class InvalidPassword(CoalesceDBError):
    """Too short or too long. The message never includes the password."""

    default_message = "Passwords must be between 8 and 1024 characters long."


class UserExists(CoalesceDBError):
    default_message = "That username is already taken."


class UserNotFound(CoalesceDBError):
    default_message = "That user doesn't exist."


class SQLRejected(CoalesceDBError):
    """The SQL guard rejected the query."""

    default_message = "That query isn't allowed."

    def __init__(
        self, user_message: str | None = None, *, reasons: list[str] | None = None
    ) -> None:
        super().__init__(user_message)
        self.reasons = list(reasons) if reasons else []


class SQLParseError(SQLRejected):
    default_message = "That query couldn't be understood as SQL."


class QueryTimeout(CoalesceDBError):
    default_message = "The query took too long and was stopped."


class ExecutionError(CoalesceDBError):
    """Wraps sqlite3.Error."""

    default_message = "The database couldn't run that query."

    def __init__(self, user_message: str | None = None, *, sqlite_message: str = "") -> None:
        super().__init__(user_message)
        self.sqlite_message = sqlite_message


class LLMUnavailable(CoalesceDBError):
    """Ollama not reachable, or the model is missing."""

    default_message = "The AI model isn't available right now."


class LLMOutputInvalid(CoalesceDBError):
    """The model's output failed validation after retries."""

    default_message = "The AI model gave an answer that couldn't be used."

    def __init__(self, user_message: str | None = None, *, raw_output: str = "") -> None:
        super().__init__(user_message)
        self.raw_output = raw_output


class IngestError(CoalesceDBError):
    """Unreadable, oversized, encrypted or scanned file."""

    default_message = "That file couldn't be read."

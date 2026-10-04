"""Login, lockout, user and grant management (PROJECT_SPEC.md §6.1).

AuthService is the only place that decides who a user is and what role they have on a
database. Every method re-loads the user from app.db instead of trusting the User object
it is handed: the UI hiding a button is not a security control.
"""

import json
import math
import re
import secrets
import time
from collections.abc import Collection

from coalescedb.auth.passwords import hash_password, needs_rehash, verify_password
from coalescedb.auth.store import AppStore, UserRow
from coalescedb.config import Settings
from coalescedb.db.identifiers import validate_db_name
from coalescedb.errors import (
    AccountLockedError,
    AuthError,
    InvalidIdentifier,
    InvalidPassword,
    PermissionDenied,
    UserNotFound,
)
from coalescedb.models import Role, User

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,31}\Z")  # \Z, not $: "abc\n" must not match
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024  # Argon2 is deliberately slow; don't let it chew on megabytes

_AUDIT_KEYS = ("id", "ts", "user_id", "db_name", "action", "detail", "source")


def _now() -> float:
    return time.time()


class AuthService:
    def __init__(self, store: AppStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings
        # Verified against when the username doesn't exist, so that a failed login takes
        # about the same time whether or not the account is real.
        self._dummy_hash = hash_password(secrets.token_urlsafe(16))

    # --- checks shared by the methods below --------------------------------------------

    @staticmethod
    def _validate_username(username: str) -> str:
        if not isinstance(username, str) or USERNAME_RE.fullmatch(username) is None:
            raise InvalidIdentifier(
                "Usernames must be 3 to 32 characters, start with a letter, and use only "
                "letters, digits, underscore, dot and hyphen."
            )
        return username

    @staticmethod
    def _validate_password(password: str) -> str:
        # The message never includes the password itself.
        if (
            not isinstance(password, str)
            or not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH
        ):
            raise InvalidPassword()
        return password

    @staticmethod
    def _validate_db_name(db_name: str) -> str:
        return validate_db_name(db_name)

    def _load(self, user: User) -> UserRow | None:
        """The user's current row in app.db, or None if they no longer exist."""
        if not isinstance(user, User):
            return None
        return self._store.get_user_by_id(user.id)

    def _require_superadmin(self, actor: User) -> UserRow:
        row = self._load(actor)
        if row is None or not row.is_superadmin:
            raise PermissionDenied("Only a superadmin can do that.")
        return row

    def _require_target(self, user_id: int) -> UserRow:
        row = self._store.get_user_by_id(user_id)
        if row is None:
            raise UserNotFound()
        return row

    def _check_lock(self, row: UserRow) -> None:
        """Raise AccountLockedError while locked; restart the counter once a lock expires."""
        if row.locked_until is None:
            return
        remaining = row.locked_until - _now()
        if remaining > 0:
            raise AccountLockedError(retry_after_s=max(1, math.ceil(remaining)))
        self._store.clear_failures(row.id)

    def _record_failure(self, row: UserRow, reason: str) -> None:
        self._store.record_failure(
            row.id,
            self._settings.login_max_failures,
            _now() + self._settings.login_lockout_s,
        )
        self.audit(row.to_user(), "login_failed", None, {"reason": reason}, "system")

    # --- login -------------------------------------------------------------------------

    def bootstrap_superadmin(self, username: str, password: str) -> User:
        """Create the very first account. Only possible while there are no users."""
        if self._store.has_any_user():
            raise PermissionDenied("Setup has already been completed.")
        self._validate_username(username)
        self._validate_password(password)
        user_id = self._store.insert_first_user(username, hash_password(password), _now())
        if user_id is None:  # someone else got there first
            raise PermissionDenied("Setup has already been completed.")
        user = User(id=user_id, username=username, is_superadmin=True)
        self.audit(user, "bootstrap", None, {"username": username}, "system")
        return user

    def login(self, username: str, password: str) -> User:
        if isinstance(password, str) and len(password) > MAX_PASSWORD_LENGTH:
            # No real password is this long. Refuse before any lookup or hashing, with the
            # same generic error, so nothing is revealed and no work is done.
            self.audit(None, "login_failed", None, {"reason": "password_too_long"}, "system")
            raise AuthError()

        row = None
        if isinstance(username, str) and USERNAME_RE.fullmatch(username) is not None:
            row = self._store.get_user_by_username(username)
        if row is None:
            # Unknown or malformed username: same work, same generic error. The typed
            # text is never stored — people sometimes type their password here.
            verify_password(self._dummy_hash, password)
            self.audit(None, "login_failed", None, {"reason": "unknown_user"}, "system")
            raise AuthError()

        try:
            self._check_lock(row)
        except AccountLockedError:
            self.audit(row.to_user(), "login_failed", None, {"reason": "locked"}, "system")
            raise

        if not verify_password(row.password_hash, password):
            self._record_failure(row, "bad_password")
            raise AuthError()

        self._store.clear_failures(row.id)
        if needs_rehash(row.password_hash):
            self._store.set_password_hash(row.id, hash_password(password))
        user = row.to_user()
        self.audit(user, "login", None, {}, "system")
        return user

    # --- users -------------------------------------------------------------------------

    def create_user(
        self, actor: User, username: str, password: str, superadmin: bool = False
    ) -> User:
        self._require_superadmin(actor)
        self._validate_username(username)
        self._validate_password(password)
        user_id = self._store.insert_user(
            username, hash_password(password), bool(superadmin), _now()
        )
        user = User(id=user_id, username=username, is_superadmin=bool(superadmin))
        detail = {"user_id": user_id, "username": username, "superadmin": bool(superadmin)}
        self.audit(actor, "user_create", None, detail, "system")
        return user

    def delete_user(self, actor: User, user_id: int) -> None:
        self._require_superadmin(actor)
        outcome = self._store.delete_user(user_id)
        if outcome == "not_found":
            raise UserNotFound()
        if outcome == "last_superadmin":
            raise PermissionDenied("The last superadmin can't be deleted.")
        self.audit(actor, "user_delete", None, {"user_id": user_id}, "system")

    def change_password(
        self,
        actor: User,
        user_id: int,
        new_password: str,
        current_password: str | None = None,
    ) -> None:
        actor_row = self._load(actor)
        if actor_row is None:
            raise PermissionDenied()

        if actor_row.id == user_id:
            # Own password: the current one is required, and a wrong one counts toward
            # the lockout exactly like a failed login.
            self._check_lock(actor_row)
            self._validate_password(new_password)
            if not isinstance(current_password, str) or not verify_password(
                actor_row.password_hash, current_password
            ):
                self._record_failure(actor_row, "bad_current_password")
                raise AuthError()
        else:
            # Someone else's password: superadmins only, no current password needed.
            if not actor_row.is_superadmin:
                raise PermissionDenied("Only a superadmin can do that.")
            self._require_target(user_id)
            self._validate_password(new_password)

        self._store.set_password_hash(user_id, hash_password(new_password))
        self._store.clear_failures(user_id)  # also lifts a lockout on a superadmin reset
        self.audit(actor, "password_change", None, {"user_id": user_id}, "system")

    def list_users(self, actor: User) -> list[User]:
        self._require_superadmin(actor)
        return [row.to_user() for row in self._store.list_users()]

    # --- grants ------------------------------------------------------------------------

    def grant(self, actor: User, user_id: int, db_name: str, role: Role) -> None:
        self._require_superadmin(actor)
        self._validate_db_name(db_name)
        role = Role(role)  # ValueError for anything that isn't a real role
        self._require_target(user_id)
        self._store.upsert_grant(user_id, db_name, role.value)
        self.audit(actor, "grant", db_name, {"user_id": user_id, "role": role.value}, "system")

    def revoke(self, actor: User, user_id: int, db_name: str) -> None:
        self._require_superadmin(actor)
        self._validate_db_name(db_name)
        self._require_target(user_id)
        self._store.delete_grant(user_id, db_name)
        self.audit(actor, "revoke", db_name, {"user_id": user_id}, "system")

    def require_superadmin(self, actor: User) -> None:
        """Raise PermissionDenied unless the actor, re-loaded from app.db, is a superadmin."""
        self._require_superadmin(actor)

    def revoke_all(self, actor: User, db_name: str) -> None:
        """Remove every user's grant on one database, in one statement."""
        self._require_superadmin(actor)
        self._validate_db_name(db_name)
        self._store.delete_grants_for_db(db_name)
        self.audit(actor, "revoke_all", db_name, {}, "system")

    def rename_grants(self, actor: User, old: str, new: str) -> None:
        """Move every user's grant from one database name to another, in one statement."""
        self._require_superadmin(actor)
        self._validate_db_name(old)
        self._validate_db_name(new)
        self._store.rename_grants(old, new)
        self.audit(actor, "rename_grants", new, {"old": old, "new": new}, "system")

    def role_for(self, user: User, db_name: str) -> Role | None:
        """The user's role on a database right now. None means no access."""
        self._validate_db_name(db_name)
        row = self._load(user)
        if row is None:
            return None
        if row.is_superadmin:
            return Role.ADMIN
        granted = self._store.get_grant(row.id, db_name)
        return None if granted is None else Role(granted)

    def accessible_databases(
        self, user: User, *, all_databases: Collection[str]
    ) -> list[tuple[str, Role]]:
        row = self._load(user)
        if row is None:
            return []
        if row.is_superadmin:
            return [(name, Role.ADMIN) for name in sorted(set(all_databases))]
        # all_databases is ignored here, so it can never add access.
        return [(name, Role(role)) for name, role in self._store.list_grants(row.id)]

    # --- audit log ---------------------------------------------------------------------

    def audit(
        self, user: User | None, action: str, db_name: str | None, detail: dict, source: str
    ) -> None:
        user_id = None if user is None else user.id
        self._store.insert_audit(_now(), user_id, db_name, action, json.dumps(detail), source)

    def read_audit(
        self, actor: User, limit: int = 500, db_name: str | None = None
    ) -> list[dict]:
        self._require_superadmin(actor)
        entries = []
        for row in self._store.select_audit(max(0, int(limit)), db_name):
            entry = dict(zip(_AUDIT_KEYS, row, strict=True))
            entry["detail"] = json.loads(entry["detail"]) if entry["detail"] else {}
            entries.append(entry)
        return entries

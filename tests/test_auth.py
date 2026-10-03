"""Tests for auth/ (PROJECT_SPEC.md §6.1): passwords, AppStore, AuthService."""

import json
import sqlite3

import argon2
import pytest

import coalescedb.auth.service as service_module
from coalescedb.auth.passwords import hash_password, verify_password
from coalescedb.auth.service import AuthService
from coalescedb.auth.store import AppStore
from coalescedb.db.identifiers import DB_NAME_RE
from coalescedb.errors import (
    AccountLockedError,
    AuthError,
    InvalidIdentifier,
    InvalidPassword,
    PermissionDenied,
    UserExists,
    UserNotFound,
)
from coalescedb.models import Role, User

SUPER_PASSWORD = "super-secret-pw-1"  # same value as the `superadmin` fixture in conftest.py
USER_PASSWORD = "viewer-pass-22"
NEW_PASSWORD = "brand-new-pass-33"
UNKNOWN_ID = 9999

BAD_USERNAMES = [
    "admin'--",
    "x; DROP TABLE users",
    "a b",
    "ab",  # too short (minimum 3)
    "abc\n",  # trailing newline
    "a" * 33,  # too long (maximum 32)
    "1abc",  # must start with a letter
    "_abc",
    "",
    "abc\x00",
    "аbc",  # Cyrillic "а": a unicode lookalike
]

BAD_DB_NAMES = [
    "../x",
    "x.db",
    "Sales",  # uppercase
    "a b",
    "x'; DROP TABLE grants; --",
    "abc\n",
    "",
    "a" * 49,  # maximum is 48
    "1abc",
    "_abc",
]


# --- helpers ---------------------------------------------------------------------------


def db_rows(settings, sql, params=()):
    """Read app.db directly, bypassing AppStore, to check what was really stored."""
    conn = sqlite3.connect(settings.app_db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def count(settings, table_sql):
    return db_rows(settings, table_sql)[0][0]


def lock_state(settings, user_id):
    """(failed_attempts, locked_until) for a user."""
    return db_rows(
        settings, "SELECT failed_attempts, locked_until FROM users WHERE id = ?", (user_id,)
    )[0]


def stored_hash(settings, user_id):
    return db_rows(settings, "SELECT password_hash FROM users WHERE id = ?", (user_id,))[0][0]


def fail_login(auth, username, times):
    for _ in range(times):
        with pytest.raises(AuthError) as excinfo:
            auth.login(username, "wrong-password-0")
        assert type(excinfo.value) is AuthError


@pytest.fixture
def alice(auth, superadmin):
    return auth.create_user(superadmin, "alice_1", USER_PASSWORD)


@pytest.fixture
def bob(auth, superadmin):
    return auth.create_user(superadmin, "bob_2", USER_PASSWORD)


# --- passwords.py (real Argon2 defaults: these tests don't use fast_hashing) -----------


def test_hash_is_argon2id_and_not_the_plain_text():
    hashed = hash_password(USER_PASSWORD)
    assert hashed.startswith("$argon2id$")
    assert USER_PASSWORD not in hashed
    assert hash_password(USER_PASSWORD) != hashed  # a fresh random salt every time


def test_hash_uses_the_library_defaults():
    assert argon2.PasswordHasher().check_needs_rehash(hash_password(USER_PASSWORD)) is False


def test_verify_password():
    hashed = hash_password(USER_PASSWORD)
    assert verify_password(hashed, USER_PASSWORD) is True
    assert verify_password(hashed, USER_PASSWORD + "x") is False
    assert verify_password(hashed, "") is False


@pytest.mark.parametrize("bad_hash", ["", "not-a-hash", "$argon2id$broken", None, 123])
def test_verify_password_returns_false_instead_of_raising(bad_hash):
    assert verify_password(bad_hash, USER_PASSWORD) is False


def test_verify_password_returns_false_for_a_non_string_password():
    assert verify_password(hash_password(USER_PASSWORD), None) is False


# --- AppStore --------------------------------------------------------------------------


def test_store_creates_schema_in_wal_mode(store, settings):
    tables = {row[0] for row in db_rows(settings, "SELECT name FROM sqlite_master")}
    assert {"users", "grants", "audit_log"} <= tables
    assert db_rows(settings, "PRAGMA journal_mode")[0][0] == "wal"


def test_app_db_is_not_inside_the_databases_folder(store, settings):
    assert settings.app_db_path.is_file()
    assert settings.databases_dir not in settings.app_db_path.parents


def test_has_any_user_flips_after_bootstrap(store, auth):
    assert store.has_any_user() is False
    auth.bootstrap_superadmin("root_admin", SUPER_PASSWORD)
    assert store.has_any_user() is True


def test_reopening_the_store_keeps_the_data(settings, auth, superadmin, alice):
    reopened = AuthService(AppStore(settings.app_db_path), settings)
    assert reopened.login("alice_1", USER_PASSWORD) == alice
    assert count(settings, "SELECT COUNT(*) FROM users") == 2


# --- Username rules --------------------------------------------------------------------


@pytest.mark.parametrize("username", BAD_USERNAMES + [None, 123])
def test_bootstrap_rejects_bad_usernames(auth, store, settings, username):
    with pytest.raises(InvalidIdentifier):
        auth.bootstrap_superadmin(username, SUPER_PASSWORD)
    assert store.has_any_user() is False
    assert count(settings, "SELECT COUNT(*) FROM users") == 0  # table still there, empty


@pytest.mark.parametrize("username", BAD_USERNAMES + [None, 123])
def test_create_user_rejects_bad_usernames(auth, superadmin, settings, username):
    with pytest.raises(InvalidIdentifier):
        auth.create_user(superadmin, username, USER_PASSWORD)
    assert count(settings, "SELECT COUNT(*) FROM users") == 1  # only the superadmin


@pytest.mark.parametrize("username", ["abc", "Alice.Smith-2", "a" * 32])
def test_create_user_accepts_good_usernames(auth, superadmin, username):
    user = auth.create_user(superadmin, username, USER_PASSWORD)
    assert user.username == username
    assert auth.login(username, USER_PASSWORD) == user


# --- Password rules --------------------------------------------------------------------


@pytest.mark.parametrize("password", ["Zq9!xyz", "", "a" * 1025, None, 12345678])
def test_bad_passwords_are_rejected_everywhere(auth, superadmin, alice, settings, password):
    with pytest.raises(InvalidPassword):
        auth.create_user(superadmin, "carol_3", password)
    with pytest.raises(InvalidPassword):
        auth.change_password(superadmin, alice.id, password)
    with pytest.raises(InvalidPassword):
        auth.change_password(alice, alice.id, password, current_password=USER_PASSWORD)
    assert count(settings, "SELECT COUNT(*) FROM users") == 2
    assert auth.login("alice_1", USER_PASSWORD) == alice  # password unchanged


def test_bootstrap_rejects_a_short_password(auth, store):
    with pytest.raises(InvalidPassword):
        auth.bootstrap_superadmin("root_admin", "a" * 7)
    assert store.has_any_user() is False


@pytest.mark.parametrize("length", [8, 1024])
def test_password_length_limits_are_inclusive(auth, superadmin, length):
    password = "p" * length
    user = auth.create_user(superadmin, "carol_3", password)
    assert auth.login("carol_3", password) == user


@pytest.mark.parametrize("password", ["Zq9!xyz", "Q" * 1025])
def test_password_error_message_never_contains_the_password(auth, superadmin, password):
    with pytest.raises(InvalidPassword) as excinfo:
        auth.create_user(superadmin, "carol_3", password)
    assert password not in excinfo.value.user_message
    assert password not in str(excinfo.value)
    assert password not in repr(excinfo.value)


# --- Bootstrap -------------------------------------------------------------------------


def test_bootstrap_creates_the_first_superadmin(auth, store):
    user = auth.bootstrap_superadmin("root_admin", SUPER_PASSWORD)
    assert isinstance(user, User)
    assert user.username == "root_admin"
    assert user.is_superadmin is True
    assert auth.login("root_admin", SUPER_PASSWORD) == user


def test_bootstrap_works_only_once(auth, superadmin, settings):
    with pytest.raises(PermissionDenied):
        auth.bootstrap_superadmin("second_root", SUPER_PASSWORD)
    assert count(settings, "SELECT COUNT(*) FROM users") == 1


# --- Login -----------------------------------------------------------------------------


def test_login_returns_the_user(auth, superadmin, alice):
    assert auth.login("alice_1", USER_PASSWORD) == alice
    assert auth.login("root_admin", SUPER_PASSWORD) == superadmin


def test_login_username_is_case_insensitive(auth, alice):
    assert auth.login("ALICE_1", USER_PASSWORD) == alice


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("alice_1", "wrong-password-0"),  # wrong password
        ("no_such_user", USER_PASSWORD),  # unknown user
        ("a b", USER_PASSWORD),  # malformed username
        ("admin'--", USER_PASSWORD),
        ("abc\n", USER_PASSWORD),
        ("", ""),
        (None, None),
    ],
)
def test_login_failures_all_look_the_same(auth, alice, username, password):
    with pytest.raises(AuthError) as excinfo:
        auth.login(username, password)
    # Exactly AuthError: never UserNotFound, InvalidIdentifier or a subclass.
    assert type(excinfo.value) is AuthError
    assert excinfo.value.user_message == AuthError().user_message
    assert "Invalid username or password" in excinfo.value.user_message


def test_login_fails_for_a_deleted_user(auth, superadmin, alice):
    auth.delete_user(superadmin, alice.id)
    with pytest.raises(AuthError) as excinfo:
        auth.login("alice_1", USER_PASSWORD)
    assert type(excinfo.value) is AuthError


# --- Timing path: every failed login costs exactly one password verification -----------


@pytest.fixture
def verify_calls(monkeypatch):
    calls = []
    real = service_module.verify_password

    def counting(stored, plain):
        calls.append(stored)
        return real(stored, plain)

    monkeypatch.setattr(service_module, "verify_password", counting)
    return calls


@pytest.mark.parametrize("username", ["no_such_user", "a b", "admin'--", "abc\n", ""])
def test_unknown_or_malformed_username_still_verifies_once(auth, alice, verify_calls, username):
    with pytest.raises(AuthError):
        auth.login(username, USER_PASSWORD)
    assert len(verify_calls) == 1
    assert verify_calls[0].startswith("$argon2")  # a real hash, so it costs real time


def test_known_username_verifies_once(auth, alice, verify_calls):
    with pytest.raises(AuthError):
        auth.login("alice_1", "wrong-password-0")
    assert len(verify_calls) == 1
    auth.login("alice_1", USER_PASSWORD)
    assert len(verify_calls) == 2


def test_overlong_password_is_refused_without_any_verify(auth, alice, settings, verify_calls):
    too_long = "p" * 1025
    for username in ("alice_1", "no_such_user", "a b"):
        with pytest.raises(AuthError) as excinfo:
            auth.login(username, too_long)
        assert type(excinfo.value) is AuthError
        assert excinfo.value.user_message == AuthError().user_message
    assert len(verify_calls) == 0  # refused before any lookup or hashing
    assert lock_state(settings, alice.id) == (0, None)  # the user row was never touched


# --- Lockout ---------------------------------------------------------------------------


def test_five_failures_lock_the_account(auth, alice, settings, clock):
    assert settings.login_max_failures == 5
    fail_login(auth, "alice_1", 5)
    with pytest.raises(AccountLockedError) as excinfo:
        auth.login("alice_1", USER_PASSWORD)  # the right password is refused while locked
    retry = excinfo.value.retry_after_s
    assert isinstance(retry, int)
    assert 0 < retry <= settings.login_lockout_s
    assert USER_PASSWORD not in excinfo.value.user_message


def test_four_failures_do_not_lock(auth, alice, settings, clock):
    fail_login(auth, "alice_1", 4)
    assert lock_state(settings, alice.id) == (4, None)
    assert auth.login("alice_1", USER_PASSWORD) == alice


def test_attempts_while_locked_do_not_extend_the_lock(auth, alice, settings, clock):
    fail_login(auth, "alice_1", 5)
    before = lock_state(settings, alice.id)
    clock.advance(100)
    with pytest.raises(AccountLockedError) as excinfo:
        auth.login("alice_1", "wrong-password-0")
    assert lock_state(settings, alice.id) == before
    assert excinfo.value.retry_after_s <= settings.login_lockout_s - 100


def test_login_works_again_after_the_lock_expires(auth, alice, settings, clock):
    fail_login(auth, "alice_1", 5)
    clock.advance(settings.login_lockout_s + 1)
    assert auth.login("alice_1", USER_PASSWORD) == alice
    assert lock_state(settings, alice.id) == (0, None)


def test_counter_restarts_at_zero_after_the_lock_expires(auth, alice, settings, clock):
    fail_login(auth, "alice_1", 5)
    clock.advance(settings.login_lockout_s + 1)
    fail_login(auth, "alice_1", 1)  # plain AuthError, not locked again straight away
    assert lock_state(settings, alice.id) == (1, None)


def test_successful_login_resets_the_counter(auth, alice, settings, clock):
    fail_login(auth, "alice_1", 4)
    auth.login("alice_1", USER_PASSWORD)
    assert lock_state(settings, alice.id) == (0, None)
    fail_login(auth, "alice_1", 4)  # would be 8 in a row without the reset
    assert auth.login("alice_1", USER_PASSWORD) == alice


def test_lockout_is_per_user(auth, alice, bob, clock):
    fail_login(auth, "alice_1", 5)
    assert auth.login("bob_2", USER_PASSWORD) == bob


def test_unknown_usernames_are_never_locked(auth, alice, clock):
    fail_login(auth, "no_such_user", 10)


# --- Rehash on login -------------------------------------------------------------------


def test_login_rehashes_when_needed(auth, alice, settings, monkeypatch):
    before = stored_hash(settings, alice.id)
    monkeypatch.setattr(service_module, "needs_rehash", lambda stored: True)
    auth.login("alice_1", USER_PASSWORD)
    after = stored_hash(settings, alice.id)
    assert after != before
    assert verify_password(after, USER_PASSWORD) is True


def test_login_keeps_the_hash_when_no_rehash_is_needed(auth, alice, settings, monkeypatch):
    before = stored_hash(settings, alice.id)
    monkeypatch.setattr(service_module, "needs_rehash", lambda stored: False)
    auth.login("alice_1", USER_PASSWORD)
    assert stored_hash(settings, alice.id) == before


# --- Authorization: every method re-checks the actor against app.db --------------------

SUPERADMIN_ONLY = {
    "create_user": lambda auth, actor, target: auth.create_user(actor, "new_user", USER_PASSWORD),
    "delete_user": lambda auth, actor, target: auth.delete_user(actor, target.id),
    "list_users": lambda auth, actor, target: auth.list_users(actor),
    "grant": lambda auth, actor, target: auth.grant(actor, target.id, "sales", Role.VIEWER),
    "revoke": lambda auth, actor, target: auth.revoke(actor, target.id, "sales"),
    "read_audit": lambda auth, actor, target: auth.read_audit(actor),
    "change_other_password": lambda auth, actor, target: auth.change_password(
        actor, target.id, NEW_PASSWORD
    ),
}

ACTOR_KINDS = ["ordinary", "forged_flag", "made_up", "deleted_superadmin"]


def make_actor(kind, auth, superadmin, alice):
    if kind == "ordinary":
        return alice
    if kind == "forged_flag":  # a real user whose User object claims to be a superadmin
        return User(id=alice.id, username=alice.username, is_superadmin=True)
    if kind == "made_up":
        return User(id=UNKNOWN_ID, username="ghost", is_superadmin=True)
    second = auth.create_user(superadmin, "second_root", SUPER_PASSWORD, superadmin=True)
    auth.delete_user(superadmin, second.id)
    return second  # a stale object for a superadmin who no longer exists


@pytest.mark.parametrize("kind", ACTOR_KINDS)
@pytest.mark.parametrize("operation", sorted(SUPERADMIN_ONLY))
def test_superadmin_only_methods_refuse_everyone_else(
    auth, superadmin, alice, bob, settings, operation, kind
):
    actor = make_actor(kind, auth, superadmin, alice)
    users_before = count(settings, "SELECT COUNT(*) FROM users")
    with pytest.raises(PermissionDenied):
        SUPERADMIN_ONLY[operation](auth, actor, bob)
    assert count(settings, "SELECT COUNT(*) FROM users") == users_before
    assert count(settings, "SELECT COUNT(*) FROM grants") == 0
    assert auth.login("bob_2", USER_PASSWORD) == bob  # password untouched


@pytest.mark.parametrize(
    "operation", ["delete_user", "grant", "revoke", "change_other_password"]
)
def test_non_superadmin_gets_permission_denied_not_user_not_found(auth, alice, operation):
    ghost = User(id=UNKNOWN_ID, username="ghost", is_superadmin=False)
    with pytest.raises(PermissionDenied):
        SUPERADMIN_ONLY[operation](auth, alice, ghost)


@pytest.mark.parametrize("operation", sorted(SUPERADMIN_ONLY))
def test_superadmin_only_methods_work_for_a_superadmin(auth, superadmin, bob, operation):
    SUPERADMIN_ONLY[operation](auth, superadmin, bob)


# --- Changing your own password --------------------------------------------------------


def test_user_changes_own_password_with_current_password(auth, alice):
    auth.change_password(alice, alice.id, NEW_PASSWORD, current_password=USER_PASSWORD)
    with pytest.raises(AuthError):
        auth.login("alice_1", USER_PASSWORD)
    assert auth.login("alice_1", NEW_PASSWORD) == alice


def test_superadmin_changes_own_password_with_current_password(auth, superadmin):
    auth.change_password(
        superadmin, superadmin.id, NEW_PASSWORD, current_password=SUPER_PASSWORD
    )
    assert auth.login("root_admin", NEW_PASSWORD) == superadmin


@pytest.mark.parametrize("current", [None, "wrong-password-0", ""])
def test_own_change_without_the_right_current_password_fails(auth, alice, settings, current):
    with pytest.raises(AuthError) as excinfo:
        auth.change_password(alice, alice.id, NEW_PASSWORD, current_password=current)
    assert type(excinfo.value) is AuthError
    assert lock_state(settings, alice.id) == (1, None)  # counted as a failed attempt
    assert auth.login("alice_1", USER_PASSWORD) == alice  # password unchanged


def test_superadmin_also_needs_current_password_for_own_change(auth, superadmin, settings):
    with pytest.raises(AuthError) as excinfo:
        auth.change_password(superadmin, superadmin.id, NEW_PASSWORD)
    assert type(excinfo.value) is AuthError
    assert lock_state(settings, superadmin.id) == (1, None)
    assert auth.login("root_admin", SUPER_PASSWORD) == superadmin


def test_five_wrong_current_passwords_lock_the_account(auth, alice, settings, clock):
    for _ in range(5):
        with pytest.raises(AuthError) as excinfo:
            auth.change_password(alice, alice.id, NEW_PASSWORD, current_password="wrong-pw-0")
        assert type(excinfo.value) is AuthError
    failed_attempts, locked_until = lock_state(settings, alice.id)
    assert failed_attempts == 5
    assert locked_until is not None
    with pytest.raises(AccountLockedError):
        auth.change_password(alice, alice.id, NEW_PASSWORD, current_password=USER_PASSWORD)
    with pytest.raises(AccountLockedError):
        auth.login("alice_1", USER_PASSWORD)


def test_failed_logins_and_failed_changes_share_one_counter(auth, alice, settings, clock):
    fail_login(auth, "alice_1", 3)
    for _ in range(2):
        with pytest.raises(AuthError):
            auth.change_password(alice, alice.id, NEW_PASSWORD, current_password="wrong-pw-0")
    with pytest.raises(AccountLockedError):
        auth.login("alice_1", USER_PASSWORD)


def test_successful_own_change_resets_the_counter(auth, alice, settings):
    for _ in range(2):
        with pytest.raises(AuthError):
            auth.change_password(alice, alice.id, NEW_PASSWORD, current_password="wrong-pw-0")
    auth.change_password(alice, alice.id, NEW_PASSWORD, current_password=USER_PASSWORD)
    assert lock_state(settings, alice.id) == (0, None)


# --- Superadmin resetting someone else's password --------------------------------------


def test_superadmin_reset_needs_no_current_password(auth, superadmin, alice):
    auth.change_password(superadmin, alice.id, NEW_PASSWORD)
    with pytest.raises(AuthError):
        auth.login("alice_1", USER_PASSWORD)
    assert auth.login("alice_1", NEW_PASSWORD) == alice


def test_superadmin_reset_clears_the_lockout(auth, superadmin, alice, settings, clock):
    fail_login(auth, "alice_1", 5)
    assert lock_state(settings, alice.id)[1] is not None
    auth.change_password(superadmin, alice.id, NEW_PASSWORD)
    assert lock_state(settings, alice.id) == (0, None)
    assert auth.login("alice_1", NEW_PASSWORD) == alice  # straight away, no waiting


# --- Users -----------------------------------------------------------------------------


def test_create_and_list_users(auth, superadmin, alice):
    second = auth.create_user(superadmin, "second_root", SUPER_PASSWORD, superadmin=True)
    assert alice.is_superadmin is False
    assert second.is_superadmin is True
    users = auth.list_users(superadmin)
    assert sorted(users, key=lambda user: user.id) == [superadmin, alice, second]


@pytest.mark.parametrize("duplicate", ["alice_1", "ALICE_1", "Alice_1"])
def test_duplicate_username_raises_user_exists(auth, superadmin, alice, settings, duplicate):
    with pytest.raises(UserExists):
        auth.create_user(superadmin, duplicate, USER_PASSWORD)
    assert count(settings, "SELECT COUNT(*) FROM users") == 2


def test_delete_user(auth, superadmin, alice):
    auth.delete_user(superadmin, alice.id)
    assert auth.list_users(superadmin) == [superadmin]


def test_last_superadmin_cannot_be_deleted(auth, superadmin, alice, settings):
    with pytest.raises(PermissionDenied):
        auth.delete_user(superadmin, superadmin.id)
    assert count(settings, "SELECT COUNT(*) FROM users") == 2
    assert auth.login("root_admin", SUPER_PASSWORD) == superadmin


def test_one_of_two_superadmins_can_be_deleted(auth, superadmin):
    second = auth.create_user(superadmin, "second_root", SUPER_PASSWORD, superadmin=True)
    auth.delete_user(second, superadmin.id)
    assert auth.list_users(second) == [second]
    with pytest.raises(PermissionDenied):
        auth.delete_user(second, second.id)  # now the last one


@pytest.mark.parametrize(
    "operation", ["delete_user", "grant", "revoke", "change_other_password"]
)
def test_unknown_user_id_raises_user_not_found(auth, superadmin, settings, operation):
    ghost = User(id=UNKNOWN_ID, username="ghost", is_superadmin=False)
    with pytest.raises(UserNotFound):
        SUPERADMIN_ONLY[operation](auth, superadmin, ghost)
    assert count(settings, "SELECT COUNT(*) FROM grants") == 0


# --- Grants and role_for ---------------------------------------------------------------


def test_superadmin_is_admin_on_every_database(auth, superadmin):
    assert auth.role_for(superadmin, "sales") is Role.ADMIN
    assert auth.role_for(superadmin, "never_granted") is Role.ADMIN


def test_role_comes_from_the_grant(auth, superadmin, alice):
    assert auth.role_for(alice, "sales") is None
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)
    assert auth.role_for(alice, "sales") is Role.VIEWER
    assert auth.role_for(alice, "other") is None
    auth.grant(superadmin, alice.id, "other", Role.ADMIN)
    assert auth.role_for(alice, "other") is Role.ADMIN


def test_downgrade_takes_effect_immediately(auth, superadmin, alice, settings):
    auth.grant(superadmin, alice.id, "sales", Role.ADMIN)
    assert auth.role_for(alice, "sales") is Role.ADMIN
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)
    assert auth.role_for(alice, "sales") is Role.VIEWER
    assert count(settings, "SELECT COUNT(*) FROM grants") == 1  # replaced, not added


def test_revoke_takes_effect_immediately(auth, superadmin, alice):
    auth.grant(superadmin, alice.id, "sales", Role.ADMIN)
    auth.revoke(superadmin, alice.id, "sales")
    assert auth.role_for(alice, "sales") is None


def test_revoking_a_grant_that_does_not_exist_is_harmless(auth, superadmin, alice):
    auth.revoke(superadmin, alice.id, "sales")
    assert auth.role_for(alice, "sales") is None


def test_deleting_a_user_removes_their_grants(auth, superadmin, alice, settings):
    auth.grant(superadmin, alice.id, "sales", Role.ADMIN)
    auth.delete_user(superadmin, alice.id)
    assert count(settings, "SELECT COUNT(*) FROM grants") == 0  # foreign-key cascade
    assert auth.role_for(alice, "sales") is None  # stale User object: no access


def test_role_for_ignores_a_forged_superadmin_flag(auth, superadmin, alice):
    forged = User(id=alice.id, username=alice.username, is_superadmin=True)
    assert auth.role_for(forged, "sales") is None
    made_up = User(id=UNKNOWN_ID, username="ghost", is_superadmin=True)
    assert auth.role_for(made_up, "sales") is None


def test_grant_rejects_an_unknown_role(auth, superadmin, alice, settings):
    with pytest.raises(ValueError):
        auth.grant(superadmin, alice.id, "sales", "owner")
    assert count(settings, "SELECT COUNT(*) FROM grants") == 0


# --- Database names in grant / revoke / role_for ---------------------------------------


@pytest.mark.parametrize("db_name", BAD_DB_NAMES + [None])
def test_bad_database_names_are_rejected(auth, superadmin, alice, settings, db_name):
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)
    with pytest.raises(InvalidIdentifier):
        auth.grant(superadmin, alice.id, db_name, Role.ADMIN)
    with pytest.raises(InvalidIdentifier):
        auth.revoke(superadmin, alice.id, db_name)
    with pytest.raises(InvalidIdentifier):
        auth.role_for(alice, db_name)
    with pytest.raises(InvalidIdentifier):
        auth.role_for(superadmin, db_name)
    # The grants table still exists and holds only the one real grant.
    assert db_rows(settings, "SELECT user_id, db_name, role FROM grants") == [
        (alice.id, "sales", "viewer")
    ]


@pytest.mark.parametrize("db_name", ["a", "sales_2024", "a" * 48])
def test_good_database_names_are_accepted(auth, superadmin, alice, db_name):
    auth.grant(superadmin, alice.id, db_name, Role.VIEWER)
    assert auth.role_for(alice, db_name) is Role.VIEWER


def test_db_name_re_lives_in_identifiers_and_rejects_trailing_newline():
    assert DB_NAME_RE.fullmatch("sales") is not None
    assert DB_NAME_RE.fullmatch("abc\n") is None
    assert DB_NAME_RE.match("abc\n") is None  # \Z anchor: safe even if .match() is used


# --- accessible_databases --------------------------------------------------------------


def test_superadmin_sees_every_database_as_admin_sorted(auth, superadmin):
    result = auth.accessible_databases(superadmin, all_databases=["zeta", "alpha", "mid"])
    assert result == [("alpha", Role.ADMIN), ("mid", Role.ADMIN), ("zeta", Role.ADMIN)]
    assert auth.accessible_databases(superadmin, all_databases=[]) == []


def test_superadmin_list_is_exactly_all_databases(auth, superadmin):
    auth.grant(superadmin, superadmin.id, "old_deleted_db", Role.VIEWER)
    result = auth.accessible_databases(superadmin, all_databases={"alpha"})
    assert result == [("alpha", Role.ADMIN)]


def test_other_users_see_only_their_grants_sorted(auth, superadmin, alice):
    auth.grant(superadmin, alice.id, "zeta", Role.VIEWER)
    auth.grant(superadmin, alice.id, "alpha", Role.ADMIN)
    expected = [("alpha", Role.ADMIN), ("zeta", Role.VIEWER)]
    # all_databases is ignored for non-superadmins, so it can never add access.
    assert auth.accessible_databases(alice, all_databases=["other", "zeta"]) == expected
    assert auth.accessible_databases(alice, all_databases=[]) == expected


def test_accessible_databases_ignores_a_forged_superadmin_flag(auth, superadmin, alice):
    auth.grant(superadmin, alice.id, "alpha", Role.VIEWER)
    forged = User(id=alice.id, username=alice.username, is_superadmin=True)
    result = auth.accessible_databases(forged, all_databases=["alpha", "secret"])
    assert result == [("alpha", Role.VIEWER)]


def test_deleted_user_has_no_accessible_databases(auth, superadmin, alice):
    auth.grant(superadmin, alice.id, "alpha", Role.VIEWER)
    auth.delete_user(superadmin, alice.id)
    assert auth.accessible_databases(alice, all_databases=["alpha"]) == []


def test_all_databases_is_a_required_keyword_argument(auth, superadmin):
    with pytest.raises(TypeError):
        auth.accessible_databases(superadmin)
    with pytest.raises(TypeError):
        auth.accessible_databases(superadmin, ["alpha"])


# --- Audit log -------------------------------------------------------------------------


def test_login_success_and_failure_are_audited(auth, superadmin, alice):
    auth.login("alice_1", USER_PASSWORD)
    with pytest.raises(AuthError):
        auth.login("alice_1", "wrong-password-0")
    with pytest.raises(AuthError):
        auth.login("no_such_user", USER_PASSWORD)

    entries = auth.read_audit(superadmin)
    assert set(entries[0]) == {"id", "ts", "user_id", "db_name", "action", "detail", "source"}
    assert [e["user_id"] for e in entries if e["action"] == "login"] == [alice.id]
    failed = [e for e in entries if e["action"] == "login_failed"]
    assert len(failed) == 2
    assert failed[0]["user_id"] is None  # newest first: the unknown username
    assert failed[0]["detail"] == {"reason": "unknown_user"}
    assert failed[1]["user_id"] == alice.id


def test_auth_actions_are_audited(auth, superadmin, alice):
    auth.grant(superadmin, alice.id, "sales", Role.VIEWER)
    auth.revoke(superadmin, alice.id, "sales")
    auth.change_password(superadmin, alice.id, NEW_PASSWORD)
    auth.delete_user(superadmin, alice.id)
    actions = {entry["action"] for entry in auth.read_audit(superadmin)}
    assert {
        "bootstrap",
        "user_create",
        "grant",
        "revoke",
        "password_change",
        "user_delete",
    } <= actions


def test_audit_detail_round_trips_as_a_dict(auth, superadmin):
    detail = {"sql": "SELECT 1", "row_count": 1, "reasons": ["a", "b"]}
    auth.audit(superadmin, "query", "sales", detail, "manual_sql")
    auth.audit(None, "benchmark", None, {}, "system")
    system_entry, query_entry = auth.read_audit(superadmin, limit=2)
    assert query_entry["user_id"] == superadmin.id
    assert query_entry["db_name"] == "sales"
    assert query_entry["action"] == "query"
    assert query_entry["detail"] == detail
    assert query_entry["source"] == "manual_sql"
    assert system_entry["user_id"] is None
    assert system_entry["db_name"] is None
    assert system_entry["detail"] == {}


def test_read_audit_limit_and_newest_first(auth, superadmin):
    for number in range(5):
        auth.audit(superadmin, "query", "sales", {"n": number}, "manual_sql")
    entries = auth.read_audit(superadmin, limit=2)
    assert [entry["detail"] for entry in entries] == [{"n": 4}, {"n": 3}]
    assert entries[0]["id"] > entries[1]["id"]


def test_read_audit_filters_by_database(auth, superadmin):
    auth.audit(superadmin, "query", "sales", {}, "manual_sql")
    auth.audit(superadmin, "query", "hr", {}, "manual_sql")
    entries = auth.read_audit(superadmin, db_name="hr")
    assert [entry["db_name"] for entry in entries] == ["hr"]


# --- Canary: secrets never reach the disk ----------------------------------------------


def test_passwords_and_typed_unknown_usernames_never_reach_the_disk(auth, settings):
    canaries = {
        "bootstrap password": "Canary-BOOT-7f3a9c1e",
        "create password": "Canary-MAKE-51b2d8aa",
        "wrong login password": "Canary-WRONG-99aa41",
        "typed unknown username": "typedCanaryName_e4c1",
        "password typed with unknown username": "Canary-GHOST-0d17c3",
        "wrong current password": "Canary-CURR-c08e55",
        "own new password": "Canary-OWN-3be6f201",
        "rejected new password": "Canary-REJ-aa90e7",
        "reset password": "Canary-RESET-62d4b9",
    }
    root = auth.bootstrap_superadmin("root_admin", canaries["bootstrap password"])
    carol = auth.create_user(root, "carol_canary", canaries["create password"])
    auth.login("carol_canary", canaries["create password"])
    with pytest.raises(AuthError):
        auth.login("carol_canary", canaries["wrong login password"])
    with pytest.raises(AuthError):
        auth.login(
            canaries["typed unknown username"], canaries["password typed with unknown username"]
        )
    with pytest.raises(AuthError):
        auth.change_password(
            carol,
            carol.id,
            canaries["rejected new password"],
            current_password=canaries["wrong current password"],
        )
    auth.change_password(
        carol,
        carol.id,
        canaries["own new password"],
        current_password=canaries["create password"],
    )
    auth.change_password(root, carol.id, canaries["reset password"])
    auth.login("carol_canary", canaries["reset password"])

    # The audit log, read the normal way, holds none of them either.
    audit_text = json.dumps(auth.read_audit(root, limit=1000))
    for label, canary in canaries.items():
        assert canary not in audit_text, label

    # Raw bytes of the database file and its write-ahead log (if present).
    data = settings.app_db_path.read_bytes()
    for suffix in ("-wal", "-shm", "-journal"):
        extra = settings.app_db_path.with_name(settings.app_db_path.name + suffix)
        if extra.exists():
            data += extra.read_bytes()
    assert b"carol_canary" in data  # proves we are reading the right file
    for label, canary in canaries.items():
        assert canary.encode("utf-8") not in data, label
        assert canary.encode("utf-16-le") not in data, label

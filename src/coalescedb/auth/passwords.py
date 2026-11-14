"""Password hashing with Argon2id (PROJECT_SPEC.md §6.1)."""

import argon2

# Library defaults, which are Argon2id. Every hash carries its own salt and parameters.
_hasher = argon2.PasswordHasher()


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(stored_hash: str, plain: str) -> bool:
    """True only if `plain` matches. Constant-time; returns False on any error."""
    try:
        return bool(_hasher.verify(stored_hash, plain))
    except Exception:  # wrong password, malformed hash, wrong types: all just "no"
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True if the hash was made with older parameters than the current defaults."""
    try:
        return bool(_hasher.check_needs_rehash(stored_hash))
    except Exception:
        return False

"""Argon2id password hashing and the password policy.

Parameters follow the OWASP Password Storage Cheat Sheet's Argon2id baseline:
m=19456 KiB, t=2, p=1. That is a deliberate choice over argon2-cffi's own heavier
defaults (64 MiB, 4 lanes) because a SkyHub server is routinely a Raspberry Pi -
64 MiB of hashing per login on a Pi Zero is seconds of stall, and a login that
times out gets "fixed" by turning authentication off. The OWASP floor is a real
floor, not a compromise: it is the configuration they publish as sufficient.

argon2-cffi carries the parameters inside the hash string, so raising them later
only needs the constants below changed - `needs_rehash` then upgrades each hash
the next time that password is used.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

# OWASP Argon2id baseline.
MEMORY_COST_KIB = 19456
TIME_COST = 2
PARALLELISM = 1
HASH_LENGTH = 32
SALT_LENGTH = 16

MIN_PASSWORD_LENGTH = 12
# Argon2 hashes whatever it is given, so an unbounded field is a free CPU-burn
# request. Well above any real passphrase, well below a denial of service.
MAX_PASSWORD_LENGTH = 1024

_hasher = PasswordHasher(
    time_cost=TIME_COST,
    memory_cost=MEMORY_COST_KIB,
    parallelism=PARALLELISM,
    hash_len=HASH_LENGTH,
    salt_len=SALT_LENGTH,
    type=Type.ID,
)


class PasswordPolicyError(ValueError):
    """A rejected password. The message is safe to show the operator."""


def validate_password(password: str, username: str = "") -> str:
    """Check a new password against the policy, returning it normalised.

    Length is the only strength rule, per OWASP: composition rules ("one digit,
    one symbol") measurably push people toward P@ssw0rd1 while adding little
    entropy. Length plus a blocklist of the obvious is the current guidance.
    """
    if not isinstance(password, str):
        raise PasswordPolicyError("Password must be text.")

    # Not stripped: a leading or trailing space is a legitimate part of a
    # passphrase, and silently trimming it locks the operator out later.
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )

    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
        )

    if username and password.casefold() == username.casefold():
        raise PasswordPolicyError("Password must not be the username.")

    if password.casefold() in _BANNED:
        raise PasswordPolicyError("That password is too common.")

    return password


# Not a real breach corpus - just the handful someone actually types when a form
# demands twelve characters and they are in a hurry.
_BANNED = frozenset({
    "password1234",
    "passwordpassword",
    "123456789012",
    "qwertyqwerty",
    "administrator",
    "skyhubskyhub",
    "allskyallsky",
})


def hash_password(password: str) -> str:
    """Argon2id hash, salt and parameters included in the returned string."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time verification. False for wrong, malformed and missing alike."""
    if not password_hash or not isinstance(password, str):
        return False

    # An over-long candidate is rejected before hashing: verifying it would cost
    # the same CPU as a legitimate login, which is the whole point of sending it.
    if len(password) > MAX_PASSWORD_LENGTH:
        return False

    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Whether this hash predates the current parameters."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, VerificationError):
        return False
